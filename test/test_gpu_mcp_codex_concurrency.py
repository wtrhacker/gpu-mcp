"""Shared HTTP stays responsive during slow tools and filesystem callbacks."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace
import uuid

import httpx
from mcp.server.fastmcp import FastMCP
import pytest

from gpu_mcp_codex_host import make_app
from gpu_mcp_codex_state import Origin

pytestmark = pytest.mark.contract


def host():
    service = FastMCP('concurrency-regression', json_response=True, stateless_http=True)
    runtime = SimpleNamespace(snapshot=lambda: {'jobs': []}, start=lambda: None,
                              stop=lambda: None, shutdown_if_idle=lambda: {'status': 'stopping'})
    server = SimpleNamespace(mcp=service, CODEX_RUNTIME=runtime, SERVER_INSTANCE_ID='fixture',
                             REPO_ROOT='/fixture', _policy_state=lambda: 'active')
    return service, runtime, server


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8123',
                             headers={'Authorization': 'Bearer agent',
                                      'Accept': 'application/json, text/event-stream'})


async def invoke(http, name, origin, *, arguments=None, request_id=1):
    response = await http.post('/mcp', json={'jsonrpc': '2.0', 'id': request_id,
        'method': 'tools/call', 'params': {'name': name, 'arguments': arguments or {},
                                          '_meta': {'threadId': origin}}})
    response.raise_for_status()
    result = response.json()['result']
    assert not result.get('isError'), result
    return json.loads(result['content'][0]['text'])


def test_slow_tool_does_not_block_health_or_another_sessions_call():
    service, runtime, server = host()
    entered, release = threading.Event(), threading.Event()

    @service.tool()
    def slow_probe() -> str:
        origin = Origin.from_meta(service.get_context().request_context.meta).thread_id
        entered.set()
        assert release.wait(4)
        return json.dumps({'origin': origin})

    @service.tool()
    def fast_probe() -> str:
        return json.dumps({'origin': Origin.from_meta(service.get_context().request_context.meta).thread_id})

    app = make_app(server, {'agent_token': 'agent', 'human_token': 'human'})
    a, b = str(uuid.uuid4()), str(uuid.uuid4())

    async def scenario():
        async with app.app.router.lifespan_context(app.app), client(app) as http:
            began = time.monotonic()
            slow = asyncio.create_task(invoke(http, 'slow_probe', a))
            # Release from a real thread so the old blocking implementation
            # fails promptly instead of deadlocking the test event loop.
            timer = threading.Timer(2, release.set)
            timer.start()
            try:
                while not entered.is_set():
                    if slow.done():
                        await slow
                    assert time.monotonic() - began < 5, 'slow handler never entered'
                    await asyncio.sleep(0.01)
                health = await http.get('/health')
                other = await invoke(http, 'fast_probe', b, request_id=2)
                elapsed = time.monotonic() - began
                original = await slow
            finally:
                release.set()
                timer.join()
                await slow
            assert health.status_code == 200
            assert other['origin'] == b and original['origin'] == a
            assert elapsed < 0.75, f'shared HTTP blocked for {elapsed:.2f}s'

    asyncio.run(scenario())


def test_slow_human_snapshot_does_not_block_health():
    service, runtime, server = host()
    entered, release = threading.Event(), threading.Event()

    def snapshot():
        entered.set()
        assert release.wait(4)
        return {'jobs': []}

    runtime.snapshot = snapshot
    app = make_app(server, {'agent_token': 'agent', 'human_token': 'human'})

    async def scenario():
        async with app.app.router.lifespan_context(app.app), client(app) as http:
            began = time.monotonic()
            slow = asyncio.create_task(http.get('/human/snapshot', headers={'Authorization': 'Bearer human'}))
            timer = threading.Timer(2, release.set)
            timer.start()
            try:
                while not entered.is_set():
                    if slow.done():
                        await slow
                    assert time.monotonic() - began < 5, 'snapshot handler never entered'
                    await asyncio.sleep(0.01)
                result = await http.get('/health')
                elapsed = time.monotonic() - began
                await slow
            finally:
                release.set()
                timer.join()
                await slow
            assert result.status_code == 200
            assert elapsed < 0.75, f'human read blocked shared HTTP for {elapsed:.2f}s'

    asyncio.run(scenario())


def test_async_adapters_preserve_tool_schema_metadata_and_default_arguments():
    service, runtime, server = host()

    @service.tool(title='Probe title', meta={'purpose': 'schema-regression'})
    def echo_probe(value: int = 7, job_title: str | None = None) -> str:
        """Original tool description."""
        return json.dumps({'value': value, 'job_title': job_title})

    async def scenario():
        before = [tool.model_dump() for tool in await service.list_tools()]
        app = make_app(server, {'agent_token': 'agent', 'human_token': 'human'})
        async with app.app.router.lifespan_context(app.app), client(app) as http:
            after = [tool.model_dump() for tool in await service.list_tools()]
            assert before == after
            result = await invoke(http, 'echo_probe', str(uuid.uuid4()), arguments={'job_title': 'A title'})
            assert result == {'value': 7, 'job_title': 'A title'}
        assert echo_probe(3) == json.dumps({'value': 3, 'job_title': None})

    asyncio.run(scenario())
