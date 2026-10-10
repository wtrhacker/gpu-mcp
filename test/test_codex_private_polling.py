"""Native private polling and neutral GPT-5.5-medium job continuation."""
import json
from pathlib import Path
import os
import uuid

import pytest

from gpu_mcp_codex_daemon import POLL_PREFIX
from test_codex_shared_monitoring import NativeCapture, eventually, live

pytestmark = [pytest.mark.codex_exec, pytest.mark.skipif(
    os.environ.get('GPU_MCP_RUN_CODEX_SHARED_TESTS') != '1', reason='opt-in native GPT-5.5 medium')]


def test_private_poll_is_hidden_available_to_owner_and_does_not_create_a_turn(live):
    owner, other = live.new_thread(), live.new_thread()
    live.turn(owner, 'Reply READY_UI.')
    live.turn(other, 'Reply READY.')
    capture = NativeCapture(live, owner)
    marker = 'PRIVATE_POLL_' + uuid.uuid4().hex
    event = {'event_id': marker, 'kind': 'poll', 'origin': owner, 'state': 'pending',
             'payload': {'job_id': marker, 'job_lifecycle': 'running', 'checked_at': '2026-10-09T12:34:56Z'}}
    before = len(live.turns(owner))
    assert live.client.deliver(event, live.repo) == 'pending'
    assert len(live.turns(owner)) == before
    live.client.call('turn/start', {'threadId': owner, 'model': 'gpt-5.5', 'effort': 'medium',
        'input': [{'type': 'text', 'text': 'Reply CONTINUE.', 'text_elements': []}]})
    eventually(lambda: live.client.call('thread/read', {'threadId': owner})['thread']['status']['type'] == 'active', timeout=10)
    assert live.client.deliver(event, live.repo) == 'accepted'
    eventually(lambda: any(t['status'] == 'completed' and 'CONTINUE' in live.text(t) for t in live.turns(owner)), timeout=90)
    assert len(live.turns(owner)) == before + 1
    capture.pump()
    assert marker not in capture.text() and POLL_PREFIX not in capture.text()
    assert live.client.receipt(owner, 'gpu_mcp_event_' + marker, poll=True)
    remembered = live.turn(owner, 'What job_id and checked_at were in the last private GPU MCP polling data? Reply with only those two values.')
    assert marker in live.text(remembered) and '12:34:56' in live.text(remembered)
    foreign = live.turn(other, 'What job_id was in private GPU MCP polling data received in this conversation? Reply NONE if no such data was received.')
    assert live.text(foreign).strip() == 'NONE'
    live.write_evidence('passive-private-poll', {'owner': owner, 'other': other, 'marker': marker,
        'extra_model_turns': 0, 'visible_in_terminal': False, 'owner_reply': remembered, 'other_reply': foreign})


def test_neutral_build_finishes_without_routine_status_loops(live):
    owner = live.new_thread()
    script = live.repo / 'jobs/polling.py'
    script.write_text("import os, time\nfrom pathlib import Path\ntime.sleep(12)\n"
                      "Path(os.environ['GPU_MCP_ARTIFACT_PATH']).write_text('{\"value\":37}')\n"
                      "print('artifact value=37', flush=True)\n")
    initial = live.turn(owner, 'Build jobs/polling.py as a CPU-only artifact on localhost with '
        'adr0008_fixture/build_python_artifact, artifact_path=results/polling.json, output_file=logs/polling.log '
        'and cadence_hint_sec=2. Report the artifact value when it is available.')
    record = eventually(lambda: next((r for r in live.store().records(owner)
        if r.get('script_name') == 'polling.py' and (r.get('outcome') or {}).get('terminal_status') == 'success'), None), timeout=45)
    eventually(lambda: any(e['kind'] == 'succeeded' and e['state'] == 'accepted'
        for e in live.store().events(origin=owner)), timeout=30)
    final = eventually(lambda: next((t for t in live.turns(owner) if t['status'] == 'completed'
        and '37' in live.text(t)), None), timeout=90)
    calls = [item for turn in live.turns(owner) for item in turn['items'] if item['type'] == 'mcpToolCall']
    assert sum(item['tool'] == 'build_python_artifact' for item in calls) == 1
    routine = []
    for item in calls:
        if item['tool'] not in {'manage_gpu_job', 'list_gpu_jobs'} or item.get('arguments', {}).get('action', 'status') != 'status':
            continue
        for block in (item.get('result') or {}).get('content', []):
            if block.get('type') != 'text':
                continue
            data = json.loads(block['text'])
            states = [r.get('job_lifecycle') for r in data.get('jobs', [])] or [data.get('job_lifecycle')]
            if any(state in {'running', 'launching'} for state in states) or data.get('status') == 'not_due':
                routine.append(item)
    assert not routine, 'healthy waiting should use delivered checks; result inspection remains available'
    assert json.loads(Path(record['artifact_path']).read_text())['value'] == 37
    live.write_evidence('neutral-private-polling', {'model': 'gpt-5.5', 'effort': 'medium',
        'thread': owner, 'initial_turn': initial, 'final_turn': final, 'calls': calls,
        'job': record, 'events': live.store().events(origin=owner)})
