"""Daemon availability, independent upgrades and absence of lifecycle mutations."""
import json
from types import SimpleNamespace

import pytest

from gpu_mcp_codex_versions import CodexVersionError, pin_current_cli, require_match

pytestmark = pytest.mark.contract


def runner_for(client, daemon):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "status": "running", "cliVersion": client, "appServerVersion": daemon}) if argv[-1] == "version" else "pinned")
    return run, calls


def test_matching_versions_use_the_existing_daemon_without_update():
    run, calls = runner_for("0.160.1", "0.160.1")
    assert require_match(cli="/fixture/codex", runner=run)["cliVersion"] == "0.160.1"
    assert len(calls) == 1 and calls[0][-1] == "version"


@pytest.mark.parametrize('client,daemon', [('0.160.1', '0.161.0'), ('0.161.0', '0.160.1'), ('1.0.0', '0.999.0')])
def test_version_difference_does_not_require_global_repair(client, daemon):
    run, calls = runner_for(client, daemon)
    assert require_match(cli='/fixture/codex', runner=run)['appServerVersion'] == daemon
    assert len(calls) == 1 and calls[0][-1] == 'version'


def test_unavailable_daemon_is_actionable_without_lifecycle_calls():
    run, calls = runner_for("0.160.1", "0.161.0")
    def offline(argv, **kwargs):
        response = run(argv, **kwargs)
        response.stdout = json.dumps({'status': 'notRunning'})
        return response
    with pytest.raises(CodexVersionError, match='unavailable'):
        require_match(cli='/fixture/codex', runner=offline)
    assert len(calls) == 1


class Sessions:
    def __init__(self, state):
        self.state = state
    def call(self, method, params):
        return {"data": ["fixture-thread"]} if method == "thread/loaded/list" else {"thread": {"status": {"type": self.state}}}
    def close(self):
        pass


@pytest.mark.parametrize("state", ["active", "idle", "systemError"])
def test_retired_pin_refuses_before_reading_or_mutating_any_global_state(state):
    run, calls = runner_for("0.160.1", "0.161.0")
    with pytest.raises(CodexVersionError, match="pinning has been retired"):
        pin_current_cli(cli="/fixture/codex", runner=run, client_factory=lambda: Sessions(state))
    assert calls == []
