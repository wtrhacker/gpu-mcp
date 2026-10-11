"""MPS parent/case management through existing GPU MCP policy and leases."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import zlib

import gpu_mcp_mps_worker as worker
import gpu_mcp_reservations as leases


def control_root():
    if os.environ.get('PYTEST_CURRENT_TEST') and os.environ.get('GPU_MCP_TEST_MPS_CONTROL_ROOT'):
        return Path(os.environ['GPU_MCP_TEST_MPS_CONTROL_ROOT'])
    return Path.home() / '.local/state/gpu-mcp/mps-jobs'


class MpsManager:
    def __init__(self, server):
        self.server = server

    def read(self, job_id):
        s = self.server
        if s.CODEX_RUNTIME is not None:
            record = s.CODEX_RUNTIME.read(job_id)
        else:
            try:
                record = leases.read_json_file_no_follow(control_root() / job_id / 'job.json')
            except (OSError, ValueError):
                return None
        return record if record and record.get('job_kind') == 'mps' and record['repo_root'] == str(s.REPO_ROOT) else None

    def save(self, record):
        s = self.server
        if s.CODEX_RUNTIME is not None:
            record = s.CODEX_RUNTIME.persist(record)
        path = control_root() / record['job_id'] / 'job.json'
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        leases.atomic_write_json(path, record)
        try:
            leases.atomic_write_json(leases.job_record_path(s.REPO_ROOT, record['job_id']), record)
        except OSError:
            pass
        return record

    def rpc(self, action, spec, request=None):
        s = self.server
        if not s._is_allowed_host(spec['host']):
            raise ValueError('MPS host is not approved')
        source = Path(worker.__file__).read_text()
        sources = {'worker': source, 'guard': Path(s.gpu_mcp_guard.__file__).read_text()}
        data = self.wire_data(spec, sources, request)
        bootstrap = ('import base64,json,zlib,sys;'
            'payload=json.loads(zlib.decompress(base64.b64decode(sys.argv[1])));sources=payload["sources"];'
            'ns={"__name__":"gpu_mcp_mps_worker"};'
            'exec(compile(sources["worker"],"<gpu-mcp-mps-worker>","exec"),ns);'
            'print(json.dumps(ns["rpc"](sys.argv[2],payload["spec"],sources,payload["request"])))')
        command = shlex.join([s.PYTHON, '-I', '-c', bootstrap, data, action])
        result = s._host_run(spec['host'], command, timeout=25)
        if result is None:
            raise RuntimeError(s._host_run_error(spec['host'], command) or 'MPS control acknowledgement is unavailable')
        return json.loads(result)

    @staticmethod
    def wire_data(spec, sources, request=None):
        value = json.dumps({'spec':spec, 'sources':sources, 'request':request}).encode()
        data = base64.b64encode(zlib.compress(value)).decode()
        if len(data) > 100000 or len(value) > 2 * 1024 * 1024:
            raise ValueError('MPS request is too large; split the declared sweep')
        return data

    def validate_runs(self, runs):
        s = self.server
        if not isinstance(runs, list) or not 1 <= len(runs) <= 1000:
            raise ValueError('runs must contain 1 to 1000 declared cases')
        cases, ids, logs = [], set(), set()
        allowed = {'case_id', 'script_path', 'args', 'output_file', 'job_title'}
        for i, raw in enumerate(runs):
            if not isinstance(raw, dict) or set(raw) - allowed:
                raise ValueError('each run accepts only case_id, script_path, args, output_file and job_title')
            case_id = raw.get('case_id', f'case-{i + 1:04}')
            if not isinstance(case_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', case_id) or case_id in ids:
                raise ValueError('case_id must be unique and contain only letters, digits, underscores or hyphens')
            ids.add(case_id)
            if not isinstance(raw.get('script_path'), str) or not raw['script_path'].strip():
                raise ValueError('each case requires a script_path')
            script = s._validate_python_script_path(raw.get('script_path'))
            issues = s.scan_python_gpu_script_safety(str(script))
            if issues:
                raise ValueError(s._script_rejection_message(script, issues))
            args = raw.get('args', [])
            if not isinstance(args, list) or any(not isinstance(v, (str, int, float, bool)) for v in args):
                raise ValueError('case arguments must be a list of scalar values')
            if not isinstance(raw.get('output_file'), str):
                raise ValueError('each case requires its own output_file')
            output = s._validate_output_path(raw['output_file'])
            s._prepare_output_parent(output)
            if output.exists() or output.is_symlink() or str(output) in logs:
                raise ValueError('case output_file must be distinct and must not already exist')
            logs.add(str(output))
            cases.append({'case_id': case_id, 'script_path': str(script),
                'script_hash': hashlib.sha256(s._read_script_no_follow(script).encode()).hexdigest(),
                'args': [str(v) for v in args], 'output_file': str(output),
                'job_title': s._validated_job_title(raw.get('job_title'))})
        return cases

    def capability(self, host, protocol):
        if os.environ.get('PYTEST_CURRENT_TEST') and os.environ.get('GPU_MCP_TEST_MPS_DRIVER') == 'cpu':
            return {'mps_binary': '', 'test_driver': True}
        code = ('import json,shutil,subprocess;'
            'p=shutil.which("nvidia-cuda-mps-control");'
            'print(json.dumps({"mps_binary":p,"version":subprocess.run([p,"-v"],capture_output=True,text=True).stdout if p else "",'
            '"driver":subprocess.run(["nvidia-smi","--query-gpu=driver_version","--format=csv,noheader"],capture_output=True,text=True).stdout}))')
        raw = self.server._host_run(host, shlex.join([self.server.PYTHON, '-I', '-c', code]), timeout=10)
        if raw is None:
            raise ValueError('cannot inspect MPS support on the selected host')
        value = json.loads(raw)
        if not value.get('mps_binary'):
            raise ValueError('nvidia-cuda-mps-control is unavailable on the selected host')
        binary = Path(value['mps_binary'])
        if not binary.is_absolute() or any(binary.is_relative_to(root) for root in
                (*self.server.APPROVED_SCRIPT_ROOTS, *self.server.APPROVED_WRITE_ROOTS, *self.server.APPROVED_OUTPUT_ROOTS)):
            raise ValueError('MPS control binary must be outside scientific script, write and log roots')
        match = re.search(r'(\d{3})\.\d+', value.get('driver', ''))
        if not match or int(match[1]) < 470:
            raise ValueError('MPS requires an identifiable driver release 470 or newer for safe client termination')
        try:
            self.rpc('preflight', {'host':host})
        except (OSError, RuntimeError) as exc:
            raise ValueError('Linux PID handles are unavailable on the selected host') from exc
        return value

    def launch(self, host, gpu_index, runs, max_concurrent=4, protocol=2, output_file=None,
               job_role=None, expected_duration_sec=None, cadence_hint_sec=None, smoke_job_id=None,
               smoke_skip_reason=None, smoke_cadence_representative=False, job_title=None):
        s = self.server
        if not s._is_allowed_host(host) or not isinstance(gpu_index, int) or isinstance(gpu_index, bool) or gpu_index < 0:
            raise ValueError('select an approved host and a non-negative GPU index')
        if not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool) or not 1 <= max_concurrent <= 32:
            raise ValueError('max_concurrent must be an integer from 1 to 32')
        if isinstance(protocol, bool) or protocol != 2:
            raise ValueError('this release supports MPS protocol 2; protocol 3 requires separate acceptance')
        cases = self.validate_runs(runs)
        leases.ensure_repo_job_state_root(s.REPO_ROOT)
        parent_log = s._validate_output_path(output_file if output_file is not None else
            str(s.APPROVED_OUTPUT_ROOTS[0] / ('gpu_mps_' + secrets.token_hex(12) + '.log')))
        s._prepare_output_parent(parent_log)
        if parent_log.exists() or any(c['output_file'] == str(parent_log) for c in cases):
            raise ValueError('the parent log must be new and distinct from case logs')
        role, defaulted, error = s._resolve_job_role(job_role, None)
        if error:
            raise ValueError(error)
        for name, value in [('expected_duration_sec', expected_duration_sec), ('cadence_hint_sec', cadence_hint_sec)]:
            _, error = (s._validate_cadence_hint(value) if name == 'cadence_hint_sec'
                        else s._validate_positive_int(value, name=name))
            if error:
                raise ValueError(error)
        cadence, basis, smoke = s._phase7_cadence_basis(job_role=role, expected_duration_sec=expected_duration_sec,
            cadence_hint_sec=cadence_hint_sec, smoke_job_id=smoke_job_id,
            smoke_cadence_representative=smoke_cadence_representative, smoke_skip_reason=smoke_skip_reason)
        if role == 'main' and not smoke and not s._nonempty_text(smoke_skip_reason):
            raise ValueError('main MPS sweep requires successful smoke evidence or smoke_skip_reason')
        host = s._canonical_policy_host(host)
        support = self.capability(host, protocol)
        job, attempt = leases.generate_job_id(), leases.generate_attempt_id()
        key = leases.reservation_key(host, gpu_index)
        record = leases.build_job_record(job_id=job, attempt_id=attempt, reservation_key_value=key,
            host=host, gpu_index=gpu_index, script_path=cases[0]['script_path'], args=[], output_file=parent_log,
            server_instance_id=s.SERVER_INSTANCE_ID, next_poll_after=s._next_poll_after(cadence))
        record.update(job_kind='mps', repo_root=str(s.REPO_ROOT), job_lifecycle='launching',
            active_reservation=True, poll_interval_sec=cadence, job_role=role, job_role_defaulted=defaulted,
            phase7_poll_discipline=True, cadence_basis=basis, job_title=s._validated_job_title(job_title),
            smoke_job_id=smoke_job_id, smoke_skip_reason=s._nonempty_text(smoke_skip_reason),
            expected_duration_sec=expected_duration_sec,
            cases=[], max_concurrent=max_concurrent, launch_mode='ssh' if not s._is_local_host(host) else 'local',
            ready_file=str(leases.outcome_record_path(s.REPO_ROOT, job, attempt).with_name('mps-ready')))
        nonce = secrets.token_hex(24)
        token = hashlib.sha256((job + attempt).encode()).hexdigest()[:20]
        spec = {'job_id': job, 'active_attempt_id': attempt, 'nonce': nonce,
            'server_instance_id': s.SERVER_INSTANCE_ID, 'host': host, 'gpu_index': gpu_index,
            'reservation_key': key, 'repo_root': str(s.REPO_ROOT),
            'script_roots': [str(p) for p in s.APPROVED_SCRIPT_ROOTS],
            'write_roots': [str(p) for p in s.APPROVED_WRITE_ROOTS],
            'output_roots': [str(p) for p in s.APPROVED_OUTPUT_ROOTS], 'runs': cases,
            'max_concurrent': max_concurrent, 'protocol': protocol, 'output_file': str(parent_log),
            'ready_file': record['ready_file'], 'pipe_directory': f'/tmp/gpu-mcp-mps-{os.getuid()}-{token}',
            'job_env': s._gpu_job_env(gpu_index), **support}
        if os.environ.get('PYTEST_CURRENT_TEST'):
            spec['worker_state_root'] = str(control_root() / 'workers')
        # Internal authority must remain outside every approved scientific write root.
        worker.directory(spec)
        self.wire_data(dict(spec, gpu_uuid='GPU-' + '0'*36), {'worker':Path(worker.__file__).read_text(),
                       'guard':Path(s.gpu_mcp_guard.__file__).read_text()})
        if control_root().resolve().is_relative_to(s.REPO_ROOT) or any(control_root().resolve().is_relative_to(p)
                for p in (*s.APPROVED_WRITE_ROOTS, *s.APPROVED_OUTPUT_ROOTS)):
            raise ValueError('MPS control storage overlaps scientific job writes')
        metadata = leases.build_shared_metadata(job_id=job, attempt_id=attempt, reservation_key_value=key,
            host=host, gpu_index=gpu_index, repo=s.REPO_ROOT, script_path=cases[0]['script_path'],
            owner_user=s.GPU_MCP_USER, server_instance_id=s.SERVER_INSTANCE_ID)
        if not s._job_checkins_healthy(key):
            raise ValueError('reservation heartbeat manager is unhealthy')
        acquired, error = leases.acquire_reservation(registry_root=leases.reservation_registry_root(),
            reservation_key_value=key, metadata=metadata)
        if not acquired:
            raise ValueError(error or 'GPU is already reserved')
        launched = False
        try:
            if support.get('test_driver'):
                spec['gpu_uuid'] = 'GPU-' + token
            else:
                spec['gpu_uuid'], _ = s._build_manager().probe_gpu(host, gpu_index)
                raw = s._host_run(host, f'nvidia-smi -i {gpu_index} --query-gpu=compute_cap --format=csv,noheader', timeout=10)
                if raw is None or float(raw.strip()) < 7:
                    raise ValueError('safe per-client MPS termination requires a Volta-or-newer GPU')
            record['mps_spec'] = spec
            metadata.update(job_kind='mps', mps_ref={k:spec[k] for k in (
                'job_id','active_attempt_id','host','gpu_index','reservation_key','repo_root','server_instance_id')})
            if 'worker_state_root' in spec:
                metadata['mps_ref']['worker_state_root'] = spec['worker_state_root']
            leases.atomic_write_json(leases.reservation_dir(leases.reservation_registry_root(), key) / 'metadata.json', metadata)
            record = s._register_codex_job(record)
            self.save(record)
            s.HEARTBEAT_MANAGER.register(key, interval_sec=600)
            launched = True  # From this point a lost reply must never trigger blind release/relaunch.
            reply = self.rpc('launch', spec)
            record.update(remote_pid=(reply.get('parent') or {}).get('pid'), parent=reply.get('parent'), job_lifecycle='running')
            s.HEARTBEAT_MANAGER.update_owned_metadata(key, {'remote_pid': record['remote_pid']}, expected_identity=(job,attempt))
            self.save(record)
            return self.response(record, 'launched')
        except BaseException as exc:
            if not launched:
                leases.remove_reservation_if_launch_failed(registry_root=leases.reservation_registry_root(), reservation_key_value=key)
                s.HEARTBEAT_MANAGER.unregister(key)
                if record.get('origin_thread_id'):
                    record.update(active_reservation=False, job_lifecycle='finished', next_poll_after=None,
                                  reason='launch refused before execution: ' + str(exc)[:500])
                    self.save(record)
                raise
            record.update(job_lifecycle='launch_outcome_unknown', reason=str(exc)[:500])
            self.save(record)
            return self.response(record, 'launch_outcome_unknown', record['reason'])

    def response(self, record, status='ok', reason='', case=None):
        counts = {state: sum(c.get('state') == state for c in record.get('cases', []))
                  for state in ('waiting', 'launching', 'running', 'succeeded', 'failed', 'cancelled')}
        result = {'status': status, 'reason': reason or record.get('reason', ''), 'job_id': record['job_id'],
            'attempt_id': record['active_attempt_id'], 'job_kind': 'mps', 'job_lifecycle': record['job_lifecycle'],
            'host': record['host'], 'gpu_index': record['gpu_index'], 'case_counts': counts,
            'max_concurrent': record['max_concurrent'], 'active_reservation': record.get('active_reservation'),
            'output_file': record['output_file'], 'next_poll_after': record.get('next_poll_after'),
            'last_status_checked_at': record.get('last_status_checked_at'), 'outcome': record.get('outcome')}
        if case:
            result['case'] = case
        evidence = self.server._successful_smoke_evidence(record, record['job_lifecycle'])
        if evidence:
            result['smoke_evidence'] = evidence
        return result

    def release(self, record, observed):
        s = self.server
        if not record.get('active_reservation') or observed['state'] != 'gone':
            return
        metadata = s._read_reservation_metadata(record['reservation_key'])
        if not s._job_record_matches_current_metadata(record, metadata):
            record['active_reservation'] = False
            return
        if record['server_instance_id'] == s.SERVER_INSTANCE_ID:
            if not s._job_checkins_healthy(record['reservation_key']):
                return
            removed, _ = s._remove_owned_finished_reservation(record, metadata)
            if removed:
                s.HEARTBEAT_MANAGER.unregister(record['reservation_key'])
                record['active_reservation'] = False
        elif s._metadata_stale(metadata):
            removed, _ = s._cleanup_stale_gone_reservation(record['reservation_key'], metadata)
            if removed:
                record['active_reservation'] = False

    def manage(self, record, action, case_id=None, reason=None, cadence_hint_sec=None, early_poll_reason=None,
               expected_duration_sec=None):
        s = self.server
        if action != 'status':
            if record['server_instance_id'] != s.SERVER_INSTANCE_ID:
                raise ValueError('only the launching server may control MPS cases')
            if not s._job_checkins_healthy(record.get('reservation_key')):
                raise ValueError('reservation heartbeat manager is unhealthy')
            if record.get('active_reservation') and not s._job_record_matches_current_metadata(record,
                    s._read_reservation_metadata(record['reservation_key'])):
                raise ValueError('reservation no longer belongs to this MPS job')
        if case_id is not None and not any(c['case_id'] == case_id for c in record.get('cases') or record['mps_spec']['runs']):
            raise ValueError('case unavailable in this sweep')
        if action == 'update_cadence':
            if case_id is not None:
                raise ValueError('checking intervals belong to the parent sweep')
            cadence, error = s._validate_cadence_hint(cadence_hint_sec)
            if error or cadence is None or not s._nonempty_text(reason):
                raise ValueError(error or 'cadence update needs a reason and positive interval')
            duration, error = s._validate_positive_int(expected_duration_sec, name='expected_duration_sec')
            if error:
                raise ValueError(error)
            _, basis, _ = s._phase7_cadence_basis(job_role=record['job_role'], expected_duration_sec=duration,
                cadence_hint_sec=cadence, smoke_job_id=record.get('smoke_job_id'), smoke_cadence_representative=False,
                smoke_skip_reason=record.get('smoke_skip_reason'))
            record.update(poll_interval_sec=cadence, next_poll_after=s._next_poll_after(cadence),
                          cadence_hint_sec=cadence, cadence_basis=basis, cadence_update_reason=reason.strip(),
                          last_cadence_update_at=leases.iso_timestamp())
            if duration is not None:
                record['expected_duration_sec'] = duration
            self.save(record)
            return self.response(record, 'cadence_updated')
        due = s._parse_next_poll_after(record.get('next_poll_after'))
        try:
            signature = Path(record['ready_file']).stat().st_mtime_ns
        except OSError:
            signature = None
        if record.get('job_lifecycle') == 'finished' and not record.get('active_reservation') and action == 'status':
            return self.response(record, case=next((c for c in record['cases'] if c['case_id'] == case_id), None))
        if (action == 'status' and due and due > datetime.now(timezone.utc)
                and not s._nonempty_text(early_poll_reason) and signature == record.get('mps_ready_signature') and not record.get('outcome')):
            return self.response(record, 'not_due', case=next((c for c in record['cases'] if c['case_id'] == case_id), None))
        observed = self.rpc('status', record['mps_spec'])
        record.update(cases=observed.get('cases', []), last_status_checked_at=leases.iso_timestamp(),
            last_process_observation={'status': observed['state'], 'reason': observed.get('reason', '')},
            mps_paused=observed.get('paused', False), reason=observed.get('reason', ''))
        record['case_retries_pending'] = {key:value for key,value in record.get('case_retries_pending', {}).items()
            if value['request_id'] not in observed.get('requests_applied', [])}
        record['requests_applied'] = observed.get('requests_applied', [])
        record['case_counts'] = {state: sum(c['state'] == state for c in record['cases'])
                                for state in ('waiting', 'launching', 'running', 'succeeded', 'failed', 'cancelled')}
        record['mps_ready_signature'] = signature
        if s._nonempty_text(early_poll_reason):
            record['last_early_poll_reason'] = early_poll_reason.strip()
            record['last_early_poll_at'] = record['last_status_checked_at']
        if observed.get('parent'):
            record.update(parent=observed['parent'], remote_pid=observed['parent']['pid'])
            if record['server_instance_id'] == s.SERVER_INSTANCE_ID and record.get('active_reservation'):
                metadata = s._read_reservation_metadata(record['reservation_key'])
                if s._job_record_matches_current_metadata(record, metadata):
                    s.HEARTBEAT_MANAGER.update_owned_metadata(record['reservation_key'], {'remote_pid': record['remote_pid']},
                        expected_identity=(record['job_id'],record['active_attempt_id']))
                else:
                    record['active_reservation'] = False
        if observed.get('outcome'):
            outcome = observed['outcome']
            worker.validate_outcome(record['mps_spec'], observed)
            record.update(outcome=outcome, job_lifecycle='succeeded' if outcome['terminal_status'] == 'success' else 'failed', next_poll_after=None)
            self.save(record)  # Protected terminal/event transaction precedes GPU release.
        else:
            record['job_lifecycle'] = ('running' if observed['state'] == 'alive' else
                'process_gone_unknown_outcome' if observed['state'] == 'gone' else 'launch_outcome_unknown')
            record['next_poll_after'] = None if observed['state'] == 'gone' else s._next_poll_after(record['poll_interval_sec'])
        self.save(record)
        runtime = s.CODEX_RUNTIME
        if runtime is not None:
            for c in record['cases']:
                if c['state'] == 'failed' and c['case_id'] not in record['case_retries_pending']:
                    runtime.store.notice(record, 'case_failed.' + c['case_id'],
                        f"Run {c['case_id']} failed (exit {c.get('exit_code')}). Log: {c['output_file']}", key=c['attempt_id'],
                        details={'case_id':c['case_id'], 'case_attempt_id':c['attempt_id'], 'case_exit_code':c.get('exit_code')})
            if record.get('mps_paused'):
                runtime.alert(record, 'mps_failure', 'MPS sweep paused: ' + record['reason'])
        if observed.get('outcome'):
            self.release(record, observed)  # All required notices are durable too.
            self.save(record)
        case = next((c for c in record['cases'] if c['case_id'] == case_id), None) if case_id else None
        if action == 'status':
            return self.response(record, case=case)
        if action in {'stop', 'retry'}:
            orphan_stop = action == 'stop' and case_id is None and observed.get('parent') and not any(
                p == observed['parent'] for p in observed.get('live_processes', []))
            if (observed['state'] != 'alive' and not orphan_stop) or not record.get('active_reservation'):
                raise ValueError('MPS reservation has ended or its parent cannot be controlled; launch a new sweep for late retries')
            request = {'request_id': secrets.token_hex(16), 'case_id': case_id,
                       'expected_attempt': case['attempt_id'] if case else None}
            if action == 'retry':
                if not case or case['state'] not in {'succeeded', 'failed', 'cancelled'}:
                    raise ValueError('retry requires a terminal case and proof its prior process is gone')
                if worker.same_process(case.get('process')) and s._is_local_host(record['host']):
                    raise ValueError('prior case process is still alive')
                script = s._validate_python_script_path(case['script_path'])
                issues = s.scan_python_gpu_script_safety(str(script))
                if issues:
                    raise ValueError(s._script_rejection_message(script, issues))
                attempt = 'case-attempt-' + secrets.token_hex(8)
                output = s._validate_output_path(case['output_file'] + '.' + attempt + '.log')
                s._prepare_output_parent(output)
                request['replacement'] = {'attempt_id': attempt, 'output_file': str(output),
                    'script_hash': hashlib.sha256(s._read_script_no_follow(script).encode()).hexdigest()}
            reply = self.rpc(action, record['mps_spec'], request=request)
            if action == 'retry':
                record['case_retries_pending'][case_id] = {'request_id':request['request_id'],
                    'expected_attempt':case['attempt_id'], 'replacement_attempt':attempt}
                self.save(record)
            return dict(self.response(record, action + '_queued', case=case), request_id=reply['request_id'])
        if action == 'finish':
            if case_id is not None or observed['state'] != 'gone':
                raise ValueError('finish requires the whole MPS group proven gone')
            record.update(job_lifecycle='finished', next_poll_after=None)
            self.save(record)
            self.release(record, observed)
            self.save(record)
            return self.response(record, 'finished')
        raise ValueError('invalid MPS lifecycle action')
