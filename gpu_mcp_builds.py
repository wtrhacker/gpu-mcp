"""Managed artifact builds using the existing policy, transport and GPU leases."""
from __future__ import annotations

import base64
import fcntl
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shlex
import zlib
import xml.etree.ElementTree as ET

import gpu_mcp_build_worker as worker
import gpu_mcp_reservations as reservations


def control_root():
    if os.environ.get('PYTEST_CURRENT_TEST') and os.environ.get('GPU_MCP_TEST_BUILD_CONTROL_ROOT'):
        return Path(os.environ['GPU_MCP_TEST_BUILD_CONTROL_ROOT'])
    if os.environ.get('PYTEST_CURRENT_TEST') and os.environ.get('GPU_MCP_TEST_RESERVATION_ROOT'):
        return reservations.reservation_registry_root().parent / 'build-control'
    return Path.home() / '.local/state/gpu-mcp/build-jobs'


def records_for_repo(repo_root):
    records = []
    for path in control_root().glob('job-*/job.json'):
        try:
            record = reservations.read_json_file_no_follow(path)
            if record.get('repo_root') == str(Path(repo_root).resolve()):
                records.append(record)
        except (OSError, ValueError):
            continue
    return records


class BuildManager:
    def __init__(self, server):
        self.server = server

    def root(self):
        return control_root()

    def read(self, job_id):
        runtime = getattr(self.server, 'CODEX_RUNTIME', None)
        if runtime is not None:
            record = runtime.read(job_id)
            return record if record and record.get('job_kind') == 'build' else None
        if not reservations.is_job_id(job_id):
            return None
        try:
            record = reservations.read_json_file_no_follow(self.root() / job_id / 'job.json')
        except (OSError, ValueError):
            return None
        if record.get('repo_root') != str(self.server.REPO_ROOT):
            return None
        return record

    def save(self, record):
        runtime = getattr(self.server, 'CODEX_RUNTIME', None)
        if runtime is not None:
            record = runtime.persist(record)
        path = self.root() / record['job_id'] / 'job.json'
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        reservations.atomic_write_json(path, record)
        # A discoverable mirror for existing hooks/UI; never read as authority.
        mirror = reservations.job_record_path(self.server.REPO_ROOT, record['job_id'])
        try:
            reservations.atomic_write_json(mirror, record)
        except OSError:
            pass  # A deleted/unwritable display mirror cannot disable management.

    def rpc(self, action, spec):
        server = self.server
        if not server._is_allowed_host(spec['host']):
            raise ValueError('build host is not in the active policy')
        source = Path(worker.__file__).read_text()
        compressed = base64.b64encode(zlib.compress(source.encode())).decode()
        bootstrap = (
            'import base64,json,zlib,sys;'
            'source=zlib.decompress(base64.b64decode(sys.argv[1])).decode();'
            'ns={"__name__":"gpu_mcp_build_worker"};'
            'exec(compile(source,"<gpu-mcp-build-worker>","exec"),ns);'
            'ns["rpc"](sys.argv[2],json.loads(sys.argv[3]),source)'
        )
        command = shlex.join([server.PYTHON, '-I', '-c', bootstrap, compressed,
                              action, json.dumps(spec)])
        raw = server._host_run(spec['host'], command, timeout=20)
        if raw is None:
            raise RuntimeError(server._host_run_error(spec['host'], command) or 'build worker transport failed')
        return json.loads(raw)

    def spec(self, record):
        keys = ('job_id', 'active_attempt_id', 'nonce', 'host', 'repo_root',
                'script_path', 'script_roots', 'write_roots', 'output_roots',
                'args', 'artifact_path', 'output_file', 'build_mode', 'gpu_index',
                'gpu_uuid', 'gpu_devices', 'worker_state_root', 'ready_file')
        return {key: record[key] for key in keys if key in record}

    def probe_gpu(self, host, index, *, allow_existing_cuda=False):
        """Check identity, capacity and admission without controlling other processes.

        Builds always require an idle card. Managed MPS may explicitly share a
        Default-mode card with ordinary CUDA processes; those processes remain
        outside its private service and are never part of its cleanup.
        """
        if type(allow_existing_cuda) is not bool:
            raise ValueError('allow_existing_cuda must be a boolean')
        server = self.server
        command = f'nvidia-smi -i {index} -q -x'
        raw = server._host_run(host, command, timeout=10)
        if raw is None:
            detail = server._host_run_error(host, command)
            raise ValueError(f'could not inspect the selected GPU: {detail}')
        try:
            device = ET.fromstring(raw).find('gpu')
            uuid = device.findtext('uuid', '')
            minor = device.findtext('minor_number', '')
            name = device.findtext('product_name', '')
            free = int(device.findtext('fb_memory_usage/free', '').split()[0])
        except (ET.ParseError, AttributeError, ValueError, IndexError) as exc:
            raise ValueError('selected GPU returned incomplete identity/memory information') from exc
        policy = server.CONFIG_POLICY
        if policy.allowed_gpu_names and name not in policy.allowed_gpu_names:
            raise ValueError('selected GPU model is not approved')
        if free < policy.min_free_memory_mib:
            raise ValueError('selected GPU has insufficient free memory')
        if not minor.isdigit() or not uuid.startswith('GPU-'):
            raise ValueError('unsupported GPU device mapping')
        if allow_existing_cuda and device.findtext('compute_mode', '').strip().lower() != 'default':
            raise ValueError('sharing with existing CUDA processes requires Default compute mode; GPU MCP does not change it')
        fields = 'gpu_uuid,pid,process_name' if allow_existing_cuda else 'gpu_uuid,pid'
        process_command = f'nvidia-smi --query-compute-apps={fields} --format=csv,noheader'
        processes = server._host_run(host, process_command, timeout=10)
        if processes is None:
            raise ValueError('could not establish selected GPU compute-process availability')
        rows = [line.split(',', 2) for line in processes.splitlines() if line.strip()]
        if allow_existing_cuda and any(len(row) != 3 or not row[0].strip().startswith('GPU-') for row in rows):
            raise ValueError('cannot identify existing CUDA processes from the GPU query; inspect the GPU and retry')
        compute = [row for row in rows if row[0].strip() == uuid]
        if allow_existing_cuda:
            for row in compute:
                if (len(row) != 3 or not row[1].strip().isdigit() or int(row[1]) <= 0
                        or not row[2].strip() or row[2].strip().startswith('[')
                        or row[2].strip().lower() in {'n/a', 'not found', 'unknown', '?'}):
                    raise ValueError('cannot identify an existing CUDA process; inspect the GPU and retry')
                if 'nvidia-cuda-mps' in Path(row[2].strip()).name.lower():
                    raise ValueError('selected GPU already has an MPS server; sharing permits ordinary CUDA processes, not another MPS service')
        elif compute:
            if getattr(server, 'CODEX_RUNTIME', None) is not None:
                raise ValueError('selected GPU already has a compute process')
            pids = ', '.join(row[1].strip() if len(row) > 1 else '?' for row in compute)
            raise ValueError(f'selected GPU already has a compute process (PID {pids})')
        return uuid, [f'/dev/nvidia{minor}', '/dev/nvidiactl', '/dev/nvidia-uvm']

    def reserve(self, record):
        server = self.server
        if record['build_mode'] == 'cpu_only':
            return
        key = reservations.reservation_key(record['host'], record['gpu_index'])
        if not server._job_checkins_healthy(key):
            raise ValueError('GPU heartbeat manager is unhealthy')
        record['reservation_key'] = key
        metadata = reservations.build_shared_metadata(
            job_id=record['job_id'], attempt_id=record['active_attempt_id'],
            reservation_key_value=key, host=record['host'], gpu_index=record['gpu_index'],
            repo=server.REPO_ROOT, script_path=record['script_path'],
            owner_user=server.GPU_MCP_USER, server_instance_id=server.SERVER_INSTANCE_ID)
        metadata.update(job_kind='build', build_mode=record['build_mode'], build_spec=self.spec(record))
        acquired, reason = reservations.acquire_reservation(
            registry_root=reservations.reservation_registry_root(), reservation_key_value=key,
            metadata=metadata)
        if not acquired:
            raise ValueError(reason or 'GPU is already reserved')
        try:
            uuid, devices = self.probe_gpu(record['host'], record['gpu_index'])
            record.update(gpu_uuid=uuid, gpu_devices=devices, active_reservation=True)
            server.HEARTBEAT_MANAGER.register(key, interval_sec=reservations.DEFAULT_HEARTBEAT_INTERVAL_SEC)
        except BaseException:
            reservations.remove_reservation_if_launch_failed(
                registry_root=reservations.reservation_registry_root(), reservation_key_value=key)
            raise

    def release(self, record):
        if not record.get('active_reservation'):
            return
        server = self.server
        metadata, reason = server._owned_lifecycle_metadata(record)
        if metadata is None:
            raise ValueError(reason)
        server.HEARTBEAT_MANAGER.unregister(record['reservation_key'])
        removed, reason = server._remove_owned_finished_reservation(record, metadata)
        if not removed:
            raise ValueError(reason)
        record['active_reservation'] = False

    def launch(self, host, script_path, artifact_path, args=None, build_mode='cpu_only',
               gpu_index=None, output_file=None, expected_duration_sec=None,
               cadence_hint_sec=None, previous=None, job_title=None):
        server = self.server
        policy = server.CONFIG_POLICY
        if not policy.build_enabled:
            raise ValueError('artifact builds are disabled; build_enabled requires policy approval')
        job_title = server._validated_job_title(job_title if job_title is not None else (previous or {}).get('job_title'))
        if build_mode not in {'cpu_only', 'gpu_assisted'}:
            raise ValueError('build_mode must be cpu_only or gpu_assisted')
        if build_mode == 'cpu_only' and gpu_index is not None:
            raise ValueError('cpu_only builds must not specify gpu_index')
        if build_mode == 'gpu_assisted' and (isinstance(gpu_index, bool) or not isinstance(gpu_index, int) or gpu_index < 0):
            raise ValueError('gpu_assisted builds require a nonnegative gpu_index')
        host = server._canonical_policy_host(host)
        if not server._is_allowed_host(host):
            raise ValueError('host is not in the approved policy')
        if worker.inside(self.root(), policy.write_roots):
            raise ValueError('build authority overlaps approved write roots')
        script = server._validate_python_script_path(script_path)
        source = server._read_script_no_follow(script)
        worker.validate_source(source, str(script))
        artifact = server._resolve_under(artifact_path, list(policy.write_roots), 'artifact_path', False)
        output = server._validate_output_path(output_file)
        if not worker.inside(artifact, policy.write_roots) or not worker.inside(output, policy.output_roots):
            raise ValueError('a requested output escaped the captured policy roots')
        server._prepare_output_parent(output)
        if args is not None and (not isinstance(args, list) or any(not isinstance(x, (str, int, float, bool)) for x in args)):
            raise ValueError('args must be a list of scalar arguments')
        duration, error = server._validate_positive_int(expected_duration_sec, name='expected_duration_sec')
        if error:
            raise ValueError(error)
        cadence, error = server._validate_cadence_hint(cadence_hint_sec)
        if error:
            raise ValueError(error)
        job_id = previous['job_id'] if previous else reservations.generate_job_id()
        record = dict(previous or {})
        record.update(schema_version=1, job_kind='build', job_id=job_id,
                      active_attempt_id=reservations.generate_attempt_id(), nonce=secrets.token_urlsafe(24),
                      host=host, repo_root=str(server.REPO_ROOT), server_instance_id=server.SERVER_INSTANCE_ID,
                      owner_user=server.GPU_MCP_USER, build_mode=build_mode, gpu_index=gpu_index,
                      reservation_key=None, active_reservation=False, script_path=str(script),
                      script_name=script.name, args=[str(x) for x in (args or [])],
                      artifact_path=str(artifact), output_file=str(output),
                      script_roots=list(map(str, policy.script_roots)),
                      write_roots=list(map(str, policy.write_roots)),
                      output_roots=list(map(str, policy.output_roots)),
                      expected_duration_sec=duration, poll_interval_sec=cadence or 30,
                      next_poll_after=server._next_poll_after(cadence or 30),
                      job_lifecycle='launching', outcome=None, last_status_checked_at=None,
                      phase7_poll_discipline=True)
        record['created_at'] = reservations.iso_timestamp()
        if job_title is not None:
            record['job_title'] = job_title
        record['ready_file'] = str(reservations.outcome_record_path(
            server.REPO_ROOT, job_id, record['active_attempt_id']))
        if os.environ.get('PYTEST_CURRENT_TEST') and os.environ.get('GPU_MCP_TEST_BUILD_WORKER_ROOT'):
            record['worker_state_root'] = os.environ['GPU_MCP_TEST_BUILD_WORKER_ROOT']
        self.reserve(record)
        try:
            record = server._register_codex_job(record)
            self.save(record)
        except BaseException:
            # No launch request has been sent; rollback is unambiguous here.
            if record.get('active_reservation'):
                server.HEARTBEAT_MANAGER.unregister(record['reservation_key'])
                reservations.remove_reservation_if_launch_failed(
                    registry_root=reservations.reservation_registry_root(),
                    reservation_key_value=record['reservation_key'])
            raise
        try:
            ack = self.rpc('launch', self.spec(record))
        except Exception as exc:
            # The remote request may have started a worker. Keep the reservation
            # and protected attempt for subsequent identity-based reconciliation.
            record['job_lifecycle'] = 'launch_outcome_unknown'
            record['reason'] = str(exc)
            self.save(record)
            return self.response(record, 'launch_outcome_unknown')
        record.update(job_lifecycle='running', remote_pid=ack['supervisor']['pid'],
                      supervisor=ack['supervisor'])
        if record['active_reservation']:
            server.HEARTBEAT_MANAGER.update_owned_metadata(record['reservation_key'], {
                'remote_pid': record['remote_pid'], 'job_kind': 'build',
                'build_spec': self.spec(record)})
        self.save(record)
        return self.response(record, 'retried' if previous else 'launched')

    def inspect(self, record):
        return self.rpc('status', self.spec(record))

    def response(self, record, status='ok', reason=None):
        owned = record['server_instance_id'] == self.server.SERVER_INSTANCE_ID
        return {'status': status, 'job_id': record['job_id'], 'job_kind': 'build',
                'attempt_id': record['active_attempt_id'], 'build_mode': record['build_mode'],
                'job_lifecycle': record['job_lifecycle'], 'host': record['host'],
                'gpu_index': record['gpu_index'], 'reservation_key': record.get('reservation_key'),
                'active_reservation': record.get('active_reservation', False),
                'owned_by_current_server': owned, 'next_poll_after': record.get('next_poll_after'),
                'last_status_checked_at': record.get('last_status_checked_at'),
                'output_file': record['output_file'], 'artifact': (record.get('outcome') or {}).get('artifact'),
                'outcome': record.get('outcome'), 'reason': reason or record.get('reason'),
                'allowed_actions': ['status', 'stop', 'retry', 'finish', 'update_cadence'] if owned else ['status']}

    def manage(self, record, action, cadence_hint_sec=None, expected_duration_sec=None,
               reason=None, early_poll_reason=None):
        # CPU jobs have no GPU lease lock. Serialize transitions against their
        # protected record so simultaneous retry/stop requests cannot fork work.
        path = self.root() / record['job_id'] / 'control.lock'
        with path.open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            current = self.read(record['job_id'])
            if current is None:
                raise ValueError('build authority is unavailable')
            return self._manage(current, action, cadence_hint_sec, expected_duration_sec,
                                reason, early_poll_reason)

    def _manage(self, record, action, cadence_hint_sec=None, expected_duration_sec=None,
                reason=None, early_poll_reason=None):
        server = self.server
        if action != 'status' and record['server_instance_id'] != server.SERVER_INSTANCE_ID:
            raise ValueError('only the owning server may change this build')
        if record['build_mode'] == 'gpu_assisted' and action != 'status' and not server._job_checkins_healthy(record.get('reservation_key')):
            raise ValueError('GPU heartbeat manager is unhealthy')
        if action == 'update_cadence':
            cadence, error = server._validate_cadence_hint(cadence_hint_sec)
            if error or cadence is None or not reason:
                raise ValueError(error or 'cadence update needs a positive cadence and reason')
            record.update(poll_interval_sec=cadence, next_poll_after=server._next_poll_after(cadence))
            self.save(record)
            return self.response(record, 'cadence_updated')
        if action == 'status' and record.get('outcome'):
            if getattr(server, 'CODEX_RUNTIME', None) is not None:
                self.save(record)
            if record.get('active_reservation') and record['server_instance_id'] == server.SERVER_INSTANCE_ID:
                self.release(record)
                self.save(record)
            return self.response(record)
        due = server._parse_next_poll_after(record.get('next_poll_after'))
        hint = Path(record['ready_file']).exists()
        if action == 'status' and due and due > datetime.now(timezone.utc) and not early_poll_reason and not hint:
            return self.response(record, 'not_due')
        observed = self.inspect(record)
        record['last_status_checked_at'] = reservations.iso_timestamp()
        if observed['state'] == 'gone':
            outcome = observed.get('outcome')
            if outcome:
                record.update(outcome=outcome, job_lifecycle='succeeded' if outcome['terminal_status'] == 'success' else 'failed',
                              next_poll_after=None)
            else:
                record.update(job_lifecycle='process_gone_unknown_outcome', next_poll_after=None)
            if getattr(server, 'CODEX_RUNTIME', None) is not None:
                self.save(record)  # Includes CPU terminal/uncertainty event before schedule retirement.
            if record['server_instance_id'] == server.SERVER_INSTANCE_ID:
                self.release(record)
        elif observed['state'] == 'alive':
            record['job_lifecycle'] = 'running'
            record['next_poll_after'] = server._next_poll_after(record['poll_interval_sec'])
        else:
            record['job_lifecycle'] = 'launch_outcome_unknown'
            if getattr(server, 'CODEX_RUNTIME', None) is not None:
                record['reason'] = observed.get('reason', 'build inspection is uncertain')
                record['next_poll_after'] = server._next_poll_after(record['poll_interval_sec'])
        self.save(record)
        if action == 'stop':
            if observed['state'] == 'alive':
                self.rpc('stop', self.spec(record))
                record['job_lifecycle'] = 'stopping'
                self.save(record)
                return self.response(record, 'stop_requested')
            if observed['state'] == 'unknown':
                raise ValueError('cannot prove build process identity; stop refused')
            return self.response(record, 'stop_not_needed')
        if action in {'finish', 'retry'}:
            if observed['state'] != 'gone':
                raise ValueError(f'{action} requires proof that all previous build processes are gone')
            if action == 'finish':
                record.update(job_lifecycle='finished', next_poll_after=None)
                self.save(record)
                return self.response(record, 'finished')
            return self.launch(record['host'], record['script_path'], record['artifact_path'],
                               record['args'], record['build_mode'], record['gpu_index'],
                               record['output_file'], record.get('expected_duration_sec'),
                               record['poll_interval_sec'], previous=record)
        return self.response(record)
