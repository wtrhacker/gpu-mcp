"""The checked-in SSH helper must release transports across repeated calls."""
import ast
import contextlib
import os
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace
from typing import Optional

import pytest

pytestmark = pytest.mark.contract
SOURCE = Path(__file__).resolve().parents[1] / 'gpu_mcp_server.py'


@pytest.fixture
def helper():
    names = {'_ssh_run', '_record_host_run_error', '_classify_run_exception'}
    tree = ast.parse(SOURCE.read_text())
    module = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                             and node.name in names], type_ignores=[])
    namespace = dict(contextlib=contextlib, Optional=Optional, subprocess=subprocess,
                     GPU_MCP_USER='fixture', SYNC_TIMEOUT_SEC=7, HOST_RUN_ERRORS={})
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    return namespace


@pytest.mark.parametrize('failure', [None, TimeoutError('command timed out'), RuntimeError('authentication failed')])
def test_repeated_calls_release_actual_descriptors_and_threads(helper, failure):
    class Transport:
        def __init__(self):
            self.fds = os.pipe()
            self.closed = threading.Event()
            self.thread = threading.Thread(target=self.closed.wait, daemon=True)
            self.thread.start()

        def close(self):
            if not self.closed.is_set():
                self.closed.set()
                for fd in self.fds:
                    os.close(fd)
                self.thread.join(timeout=1)

    class Connection:
        def __init__(self):
            # A partially opened connection still owns a client transport.
            self.client = Transport()

        def run(self, *args, **kwargs):
            assert kwargs['timeout'] == 7
            if failure:
                raise failure
            return SimpleNamespace(stdout='observed\n')

        def close(self):
            # Fabric may skip client.close() if authentication never completed.
            if not isinstance(failure, RuntimeError):
                self.client.close()

    connections = []

    def connect(*args):
        result = Connection()
        connections.append(result)
        return result

    helper['_conn'] = connect
    try:
        for _ in range(25):
            result = helper['_ssh_run']('fixture', 'read-only probe')
            assert result == (None if failure else 'observed')
            assert all(c.client.closed.is_set() for c in connections), 'an SSH transport was retained'
            assert all(not c.client.thread.is_alive() for c in connections)
            for c in connections:
                for fd in c.client.fds:
                    with pytest.raises(OSError):
                        os.fstat(fd)
    finally:
        for c in connections:
            c.client.close()


def test_connection_creation_failure_keeps_the_original_diagnostic(helper):
    def connect(*args):
        raise RuntimeError('dedicated key is missing')
    helper['_conn'] = connect
    assert helper['_ssh_run']('fixture', 'probe', timeout=2) is None
    assert helper['HOST_RUN_ERRORS'][('fixture', 'probe')] == 'dedicated key is missing'


def test_client_cleanup_still_runs_if_fabric_cleanup_raises(helper):
    calls = []

    class Connection:
        client = SimpleNamespace(close=lambda: calls.append('client closed'))

        def run(self, *args, **kwargs):
            return SimpleNamespace(stdout='ok')

        def close(self):
            calls.append('fabric close')
            raise OSError('close failed')

    helper['_conn'] = lambda *args: Connection()
    assert helper['_ssh_run']('fixture', 'probe') is None
    assert calls == ['fabric close', 'client closed']


@pytest.mark.skipif(not os.environ.get('GPU_MCP_SSH_TEST_HOST'), reason='explicit read-only SSH host opt-in required')
def test_real_ssh_does_not_accumulate_transports(helper):
    from fabric import Connection
    key = Path(os.environ.get('GPU_MCP_SSH_KEY', Path.home() / '.ssh/gpu_mcp_key'))
    assert key.is_file() and not key.is_symlink()
    host = os.environ['GPU_MCP_SSH_TEST_HOST']
    user = os.environ.get('GPU_MCP_USER') or os.environ['USER']
    helper['_conn'] = lambda host, user=user: Connection(host, user=user, connect_timeout=5,
        connect_kwargs={'key_filename':str(key), 'allow_agent':False, 'look_for_keys':False})
    helper['GPU_MCP_USER'] = user
    # Warm imports and the crypto backend before counting resources.
    assert helper['_ssh_run'](host, 'true', user=user, timeout=3) == ''
    time.sleep(.2)
    before_threads = threading.active_count()
    before_fds = len(list(Path('/proc/self/fd').iterdir()))
    for i in range(24):
        result = helper['_ssh_run'](host, 'false' if i % 2 else 'true', user=user, timeout=3)
        assert result is None if i % 2 else result == ''
    deadline = time.monotonic() + 3
    while threading.active_count() > before_threads and time.monotonic() < deadline:
        time.sleep(.05)
    assert threading.active_count() <= before_threads
    assert len(list(Path('/proc/self/fd').iterdir())) <= before_fds + 1
