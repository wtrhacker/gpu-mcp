"""Upgrade existing Codex registrations without changing policy or live jobs."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import gpu_mcp_reservations as leases
import gpu_mcp_codex_upgrade as migration
from gpu_mcp_config import load_policy
from gpu_mcp_policy_approval import approve_policy

pytestmark = pytest.mark.contract


@pytest.fixture
def installed(tmp_path, monkeypatch):
    repo, runtime = tmp_path / 'repo', tmp_path / 'runtime'
    for path in (repo / '.codex', repo / 'jobs', repo / 'results', repo / 'logs', runtime):
        path.mkdir(parents=True)
    for name in ('gpu_mcp_server.py', 'gpu_mcp_codex_launcher.py'):
        (runtime / name).write_text('# installed runtime\n')
    policy = repo / 'gpu-mcp.toml'
    policy.write_text(f"schema_version = 1\nrepo_root = {str(repo)!r}\nnodes = ['fixture']\n"
                      "script_roots = ['jobs']\nwrite_roots = ['results']\noutput_roots = ['logs']\n"
                      "min_free_memory_mib = 0\nsync_timeout_sec = 2\nbuild_enabled = true\n")
    registration = repo / '.codex/config.toml'
    registration.write_text(f'''# Keep the user's settings and approval modes.
sandbox_mode = "workspace-write"
[mcp_servers.gpu-cluster-mcp]
command = "/original/python"
args = ["{runtime / 'gpu_mcp_server.py'}", "--config", "{policy}"]
cwd = "{repo}"
startup_timeout_sec = 20
tool_timeout_sec = 360
[mcp_servers.gpu-cluster-mcp.env]
GPU_MCP_PYTHON = "/original/job-python"
[mcp_servers.gpu-cluster-mcp.tools.reload_policy]
approval_mode = "prompt"
[mcp_servers.other]
command = "/keep/this/server"
''')
    store, registry, builds = tmp_path / 'approvals.json', tmp_path / 'reservations', tmp_path / 'builds'
    approve_policy(load_policy(policy), store_path=store)
    monkeypatch.setenv('GPU_MCP_TEST_RESERVATION_ROOT', str(registry))
    monkeypatch.setenv('GPU_MCP_TEST_BUILD_CONTROL_ROOT', str(builds))
    return SimpleNamespace(repo=repo, runtime=runtime, policy=policy, registration=registration,
                           store=store, registry=registry, builds=builds)


def run(installed, **kwargs):
    return migration.upgrade(installed.policy, store_path=installed.store, **kwargs)


def job(installed, *, gpu=0, build=False, mirror=True):
    job_id, attempt = leases.generate_job_id(), leases.generate_attempt_id()
    record = dict(job_id=job_id, active_attempt_id=attempt, host='fixture', gpu_index=gpu,
                  remote_pid=12345, repo_root=str(installed.repo), job_lifecycle='running',
                  reservation_key=leases.reservation_key('fixture', gpu), active_reservation=True)
    if build:
        record.update(job_kind='build', gpu_index=None, reservation_key=None, active_reservation=False, nonce='build-identity')
        leases.atomic_write_json(installed.builds / job_id / 'job.json', record)
    else:
        leases.atomic_write_json(installed.registry / record['reservation_key'] / 'metadata.json',
                                 dict(record, repo=str(installed.repo), attempt_id=attempt))
    if mirror:
        leases.atomic_write_json(leases.job_record_path(installed.repo, job_id), record)
    return record


def outcome(installed, record, **changes):
    result = dict(job_id=record['job_id'], attempt_id=record['active_attempt_id'], host=record['host'],
                  gpu_index=record['gpu_index'], remote_pid=record['remote_pid'],
                  reservation_key=record['reservation_key'], terminal_status='success', ended_at='2026-10-09T12:00:00Z')
    result.update(changes)
    leases.atomic_write_json(leases.outcome_record_path(installed.repo, record['job_id'], record['active_attempt_id']), result)


def test_preview_apply_and_repeat_preserve_every_other_setting(installed):
    original, policy, approval = (p.read_bytes() for p in (installed.registration, installed.policy, installed.store))
    assert run(installed)['status'] == 'ready'
    assert installed.registration.read_bytes() == original
    result = run(installed, apply=True)
    assert result['status'] == 'upgraded'
    assert installed.registration.read_bytes() == original.replace(b'gpu_mcp_server.py', b'gpu_mcp_codex_launcher.py')
    assert installed.policy.read_bytes() == policy and installed.store.read_bytes() == approval
    assert (Path(result['backup_directory']) / 'config.toml').read_bytes() == original
    assert run(installed, apply=True)['status'] == 'already_shared'


def test_unfinished_gpu_job_blocks_upgrade_without_touching_registration(installed):
    record = job(installed)
    original = installed.registration.read_bytes()
    result = run(installed, apply=True)
    assert result['status'] == 'blocked'
    assert result['active_legacy_jobs'][0]['job_id'] == record['job_id']
    assert installed.registration.read_bytes() == original


@pytest.mark.parametrize('mirror', [True, False])
def test_cpu_build_without_gpu_booking_blocks_upgrade(installed, mirror):
    record = job(installed, build=True, mirror=mirror)
    result = run(installed, apply=True)
    assert result['status'] == 'blocked'
    assert record['job_id'] in {item['job_id'] for item in result['active_legacy_jobs']}


def test_unreserved_unfinished_job_is_also_detected(installed):
    record = job(installed)
    (installed.registry / record['reservation_key'] / 'metadata.json').unlink()
    assert run(installed, apply=True)['status'] == 'blocked'


def test_terminal_gpu_booking_remains_for_proof_based_cleanup(installed):
    record = job(installed)
    outcome(installed, record)
    metadata = installed.registry / record['reservation_key'] / 'metadata.json'
    original = metadata.read_bytes()
    result = run(installed, apply=True)
    assert result['status'] == 'upgraded'
    assert len(result['completed_legacy_bookings']) == 1
    assert metadata.read_bytes() == original


@pytest.mark.parametrize('changed', [{'attempt_id': 'another-attempt'}, {'remote_pid': 12346},
                                     {'host': 'other'}, {'gpu_index': 9}, {'ended_at': None}])
def test_mismatched_outcome_does_not_allow_switching(installed, changed):
    record = job(installed)
    outcome(installed, record, **changed)
    assert run(installed, apply=True)['status'] == 'blocked'


def test_build_completion_hint_is_not_proof(installed):
    record = job(installed, build=True)
    outcome(installed, record)
    assert run(installed, apply=True)['status'] == 'blocked'


def test_reconciled_build_can_upgrade(installed):
    record = job(installed, build=True)
    record.update(job_lifecycle='succeeded', outcome=dict(job_id=record['job_id'],
                  attempt_id=record['active_attempt_id'], nonce=record['nonce'], terminal_status='success', ended_at=123.0))
    leases.atomic_write_json(installed.builds / record['job_id'] / 'job.json', record)
    assert run(installed, apply=True)['status'] == 'upgraded'


def test_other_repo_job_does_not_block(installed):
    record = job(installed, mirror=False)
    metadata = installed.registry / record['reservation_key'] / 'metadata.json'
    data = json.loads(metadata.read_text())
    data['repo'] = str(installed.repo.parent / 'other')
    leases.atomic_write_json(metadata, data)
    assert run(installed, apply=True)['status'] == 'upgraded'


def test_missing_runtime_and_relative_registration_are_actionable(installed):
    (installed.runtime / 'gpu_mcp_codex_launcher.py').unlink()
    with pytest.raises(ValueError, match='installed'):
        run(installed, apply=True)
    text = installed.registration.read_text().replace(str(installed.runtime / 'gpu_mcp_server.py'), 'gpu_mcp_server.py')
    installed.registration.write_text(text)
    with pytest.raises(ValueError, match='absolute'):
        run(installed, apply=True)


@pytest.mark.parametrize('changed_path', ['policy', 'registration'])
def test_change_after_preview_prevents_apply(installed, monkeypatch, changed_path):
    original = migration.upgrade_plan
    def raced(*args, **kwargs):
        result = original(*args, **kwargs)
        with getattr(installed, changed_path).open('a') as handle:
            handle.write('\n# concurrent change\n')
        return result
    monkeypatch.setattr(migration, 'upgrade_plan', raced)
    with pytest.raises(ValueError, match='changed'):
        run(installed, apply=True)
    assert 'gpu_mcp_server.py' in installed.registration.read_text()


def test_unapproved_policy_refuses_without_rewriting(installed):
    installed.store.write_text('{}\n')
    original = installed.registration.read_bytes()
    with pytest.raises(ValueError, match='not approved'):
        run(installed, apply=True)
    assert installed.registration.read_bytes() == original


def test_job_started_after_planning_blocks_apply(installed, monkeypatch):
    original = migration.upgrade_plan
    def raced(*args, **kwargs):
        result = original(*args, **kwargs)
        job(installed)
        return result
    monkeypatch.setattr(migration, 'upgrade_plan', raced)
    assert run(installed, apply=True)['status'] == 'blocked'
    assert 'gpu_mcp_server.py' in installed.registration.read_text()


def test_existing_shared_registration_keeps_its_backend_jobs(installed):
    run(installed, apply=True)
    record = job(installed)
    assert run(installed, apply=True)['status'] == 'already_shared'
    assert (installed.registry / record['reservation_key'] / 'metadata.json').exists()
