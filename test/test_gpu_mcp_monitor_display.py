"""Human display facts stay distinct from inferred process health."""
from datetime import datetime, timezone

from gpu_mcp_monitor_display import elapsed, task_row


def test_elapsed_does_not_claim_script_runtime_from_a_submission_timestamp():
    record = {'job_id': 'example', 'job_lifecycle': 'running',
              'created_at': '2026-10-08T21:23:55Z'}
    now = datetime(2026, 10, 8, 21, 29, 21, tzinfo=timezone.utc)
    assert elapsed(record, now) == '—'
    completed = dict(record, job_lifecycle='failed', outcome={
        'started_at': '2026-10-08T21:23:56Z', 'ended_at': '2026-10-08T21:29:04Z'})
    assert elapsed(completed, now) == '00:05:08'


def test_running_without_an_observation_is_explicitly_last_known():
    row = task_row({'job_id': 'example', 'job_lifecycle': 'running',
                    'last_status_checked_at': None})
    assert row['status'] == 'running (last known)'


def test_human_title_script_arguments_and_reservation_are_separate():
    record = {'job_id': 'example', 'job_title': 'Sweep learning rate, seed 3',
              'script_path': '/repo/jobs/sweep.py', 'args': ['--lr', '0.01', '--label', 'two words'],
              'job_lifecycle': 'running', 'active_reservation': True, 'gpu_index': 0}
    row = task_row(record)
    assert row['title'] == 'Sweep learning rate, seed 3'
    assert row['script'] == '/repo/jobs/sweep.py'
    assert row['args'] == "--lr 0.01 --label 'two words'"
    assert row['reservation'] == 'held'
    assert task_row(dict(record, active_reservation=False))['reservation'] == 'released'
    assert task_row(dict(record, gpu_index=None))['reservation'] == 'none (CPU)'


def test_old_records_have_a_useful_title_without_inventing_purpose():
    assert task_row({'job_id': 'example', 'script_path': '/repo/jobs/sweep.py'})['title'] == 'sweep.py'


def test_reported_progress_keeps_its_bar_when_a_phase_message_is_present():
    from gpu_mcp_monitor_display import progress_text, parse_log_progress
    value = parse_log_progress('ordinary output\nGPU_MCP_PROGRESS {"current": 5, "total": 10, "message": "epoch 1"}\n')
    assert value == {'current': 5, 'total': 10, 'message': 'epoch 1'}
    rendered = progress_text(value)
    assert rendered.startswith('5/10 ') and '█' in rendered and '░' in rendered
    assert parse_log_progress('GPU_MCP_PROGRESS {"current": true, "total": 10}') is None
    assert parse_log_progress('GPU_MCP_PROGRESS {"current": -1, "total": 10}') is None
    assert parse_log_progress('GPU_MCP_PROGRESS {broken}') is None
    assert parse_log_progress('unstructured training output') is None


def test_roles_are_not_inferred_from_progress_or_short_run_duration():
    assert task_row({'job_id': 'x', 'job_role': 'smoke'})['role'] == 'Smoke'
    assert task_row({'job_id': 'x', 'job_role': 'main'})['role'] == 'Main'
    assert task_row({'job_id': 'x', 'job_role': 'one_off'})['role'] == 'One-off'
    assert task_row({'job_id': 'x', 'job_kind': 'build'})['role'] == 'Build'
    assert task_row({'job_id': 'x', 'expected_duration_sec': 10})['role'] == ''


def test_history_search_matches_actual_metadata_and_all_query_words():
    from gpu_mcp_monitor_display import matches_job
    job = {'job_id': 'job-history-17', 'job_title': 'Learning rate sweep', 'job_role': 'smoke',
           'script_path': 'jobs/train.py', 'args': ['--lr', '0.01', '--seed', '3'],
           'host': 'dna', 'gpu_index': 0, 'job_lifecycle': 'failed'}
    assert matches_job(job, 'SMOKE failed 0.01')
    assert matches_job(job, 'train.py --seed 3')
    assert matches_job(job, 'history-17')
    assert not matches_job(job, 'main failed')
    assert not matches_job(job, 'smoke succeeded')
