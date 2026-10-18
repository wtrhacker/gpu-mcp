"""Notification protocol contracts independent of live model/UI evidence."""
from pathlib import Path

import pytest

from gpu_mcp_codex_daemon import DaemonClient, DeliveryUncertain
from gpu_mcp_monitor_display import progress_text, safe_text

pytestmark = pytest.mark.contract


class ProtocolFixture(DaemonClient):
    def __init__(self):
        super().__init__("/unused-fixture")
        self.calls = []
        self.is_archived = False
        self.has_receipt = False
        self.loaded = True
        self.active = False

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/list":
            return {"data": [{"id": "owner"}] if self.is_archived else [], "nextCursor": None}
        if method == "thread/read":
            return {"thread": {"status": {"type": ('active' if self.active else 'idle') if self.loaded else 'notLoaded'}}}
        if method == "thread/turns/list":
            return {"data": [{"items": [{"type": "functionCallOutput", "name": "gpu_mcp_event_event"}]}] if self.has_receipt else [], "nextCursor": None}
        return {}


def event(state="pending", kind="succeeded"):
    return {"origin": "owner", "event_id": "event", "state": state, "kind": kind,
            "payload": {"job_id": "job-fixture", "job_lifecycle": kind, "error_summary": "diagnostic data"}}


def test_idle_or_active_delivery_uses_one_atomic_queued_operation():
    client = ProtocolFixture()
    sends = []
    assert client.deliver(event(), Path("/repo"), before_send=lambda: sends.append(True)) == "accepted"
    starts = [params for method, params in client.calls if method == "turn/start"]
    assert len(starts) == 1 and starts[0]["input"] == [] and len(sends) == 1
    assert starts[0]["toolOutput"]["name"] == "gpu_mcp_event_event"
    instruction = next(iter(starts[0]["additionalContext"].values()))["value"]
    assert "user's current objective in this conversation" in instruction
    assert "Continue any unfinished work" in instruction
    assert "No separate status acknowledgement is required" in instruction


def test_failure_instruction_is_visible_alert_and_scoped():
    client = ProtocolFixture()
    client.deliver(event(kind="failed"), Path("/repo"))
    start = client.calls[-1][1]
    assert start["threadId"] == "owner"
    instruction = next(iter(start["additionalContext"].values()))["value"]
    assert "Alert the human concisely" in instruction
    assert "investigate this session's job" in instruction
    assert "fix what you can within its scope" in instruction


def test_archived_thread_is_not_resumed_or_woken():
    client = ProtocolFixture()
    client.is_archived = True
    assert client.deliver(event(), Path("/repo")) == "archived"
    assert [method for method, _ in client.calls] == ["thread/list"]


@pytest.mark.parametrize("receipt, expected", [(True, "accepted"), (False, "uncertain")])
def test_lost_ack_is_reconciled_by_saved_event_identity_without_new_turn(receipt, expected):
    client = ProtocolFixture()
    client.has_receipt = receipt
    assert client.deliver(event("uncertain"), Path("/repo")) == expected
    assert "turn/start" not in [method for method, _ in client.calls]


def test_unloaded_owner_is_resumed_without_redirecting_event():
    client = ProtocolFixture()
    client.loaded = False
    client.deliver(event(), Path("/repo"))
    assert next(params for method, params in client.calls if method == "thread/resume")["threadId"] == "owner"


def test_progress_counts_are_not_clipped_into_wrong_numbers_and_unknown_stays_unknown():
    assert progress_text(None) == "—"
    assert progress_text({"current": 12345, "total": 67890}, width=8) == "..."
    assert progress_text({"current": 5, "total": 10}, width=20).startswith("5/10 ")
    assert safe_text("text\x1b[31m\x00") == "text?[31m?"


def test_finish_without_result_does_not_certify_scientific_success():
    client = ProtocolFixture()
    notification = event(kind="finished")
    notification["payload"]["terminal_result_known"] = False
    client.deliver(notification, Path("/repo"))
    instruction = next(iter(client.calls[-1][1]["additionalContext"].values()))["value"]
    assert "successful job result is still unknown" in instruction
    from gpu_mcp_monitor_display import task_row
    assert task_row({"job_id": "fixture", "job_lifecycle": "finished"})["status"] == "finished / result unknown"


def test_active_poll_uses_private_history_injection_without_starting_a_turn():
    client = ProtocolFixture()
    client.active = True
    sent = []
    assert client.deliver(event(kind='poll'), Path('/repo'), before_send=lambda: sent.append(True)) == 'accepted'
    methods = [method for method, _ in client.calls]
    assert 'thread/inject_items' in methods and 'turn/start' not in methods
    params = next(params for method, params in client.calls if method == 'thread/inject_items')
    assert params['threadId'] == 'owner'
    assert params['items'][0]['role'] == 'developer' and len(sent) == 1


@pytest.mark.parametrize('loaded', [True, False])
def test_idle_or_unloaded_poll_remains_pending_without_resume_or_model_turn(loaded):
    client = ProtocolFixture()
    client.loaded = loaded
    assert client.deliver(event(kind='poll'), Path('/repo'), before_send=lambda: pytest.fail('nothing should be sent')) == 'pending'
    methods = [method for method, _ in client.calls]
    assert not any(method in methods for method in ('thread/resume', 'turn/start', 'thread/inject_items'))


def test_uncertain_poll_ack_is_not_blindly_injected_again():
    client = ProtocolFixture()
    client.active = True
    client._legacy_receipt = lambda *args, **kwargs: False
    assert client.deliver(event(state='uncertain', kind='poll'), Path('/repo')) == 'uncertain'
    assert 'thread/inject_items' not in [method for method, _ in client.calls]


def test_poll_receipt_checks_private_history_when_turn_list_omits_developer_items():
    client = ProtocolFixture()
    checked = []
    client._legacy_receipt = lambda thread, name, **kwargs: checked.append((thread, name, kwargs)) or True
    assert client.receipt('owner', 'gpu_mcp_event_event', poll=True)
    assert checked == [('owner', 'gpu_mcp_event_event', {'poll': True})]
