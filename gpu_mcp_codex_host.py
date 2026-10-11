"""Authenticated local HTTP host for the shared MCP and separate human view."""
from __future__ import annotations

import asyncio
import contextlib
from functools import wraps
import json
import os
import secrets
import socket
import stat
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
import uvicorn

import gpu_mcp_reservations as reservations
from gpu_mcp_codex_launcher import PROTOCOL_VERSION, code_hash, process_start, runtime_signature


def read_human_log_tail(path):
    """Bound repeated human progress reads even when a job's log is large."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as log:
        if not stat.S_ISREG(os.fstat(log.fileno()).st_mode):
            raise OSError('job log is not a regular file')
        size = log.seek(0, os.SEEK_END)
        start = max(0, size - 32000)
        log.seek(start)
        data = log.read(32000)
    if start:
        # Discard a partial first line rather than inventing a complete record.
        newline = data.find(b'\n')
        data = data[newline + 1:] if newline >= 0 else data
    return ''.join(data.decode(errors='replace').splitlines(keepends=True)[-200:])


def offload_shared_tools(service):
    """Keep synchronous GPU/SSH work off the shared HTTP loop.

    to_thread copies ContextVars, including the native MCP caller metadata.
    Exported server functions and legacy stdio registration remain unchanged.
    """
    def adapter(function):
        @wraps(function)
        async def invoke(*args, **kwargs):
            return await asyncio.to_thread(function, *args, **kwargs)
        return invoke

    for tool in service._tool_manager.list_tools():
        if tool.is_async:
            continue
        service.remove_tool(tool.name)
        service.add_tool(adapter(tool.fn), name=tool.name, title=tool.title,
                         description=tool.description, annotations=tool.annotations,
                         icons=tool.icons, meta=tool.meta,
                         structured_output=tool.fn_metadata.output_schema is not None)


class LocalAuthentication:
    def __init__(self, app, credentials):
        self.app, self.credentials = app, credentials

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        human = scope["path"].startswith("/human/")
        expected = "Bearer " + self.credentials["human_token" if human else "agent_token"]
        headers = dict(scope.get("headers", []))
        actual = headers.get(b"authorization", b"").decode("latin-1")
        if not secrets.compare_digest(actual, expected):
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        return await self.app(scope, receive, send)


def make_app(server, credentials):
    runtime = server.CODEX_RUNTIME
    offload_shared_tools(server.mcp)
    mcp_app = server.mcp.streamable_http_app()

    async def health(request):
        return JSONResponse({"server_instance_id": server.SERVER_INSTANCE_ID,
                             "repo_root": str(server.REPO_ROOT),
                             "policy_state": await asyncio.to_thread(server._policy_state)})

    async def snapshot(request):
        return JSONResponse(await asyncio.to_thread(runtime.snapshot))

    async def stop_if_idle(request):
        return JSONResponse(await asyncio.to_thread(runtime.shutdown_if_idle))

    async def logs(request):
        record = await asyncio.to_thread(runtime.store.get, request.path_params["job_id"])
        if record is None:
            return JSONResponse({"error": "job unavailable"}, status_code=404)
        case_id = request.query_params.get('case_id')
        if case_id is not None:
            case = next((c for c in record.get('cases', []) if c['case_id'] == case_id), None)
            if record.get('job_kind') != 'mps' or case is None:
                return JSONResponse({'error': 'case unavailable'}, status_code=404)
            record = case
        path = await asyncio.to_thread(server._validate_output_path, record.get("output_file"))
        try:
            tail = await asyncio.to_thread(read_human_log_tail, path)
            return JSONResponse({"job_id": request.path_params['job_id'], 'case_id': case_id, "tail": tail})
        except OSError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(mcp_app):
            runtime.start()
            try:
                yield
            finally:
                runtime.stop()

    app = Starlette(routes=[Route("/health", health), Route("/human/snapshot", snapshot),
                           Route("/human/stop-if-idle", stop_if_idle, methods=["POST"]),
                           Route("/human/jobs/{job_id}/logs", logs), Mount("/", app=mcp_app)], lifespan=lifespan)
    return LocalAuthentication(app, credentials)


def run_shared(server, listen_fd):
    root = server.CODEX_RUNTIME.store.root
    credentials = reservations.read_json_file_no_follow(root / "credentials.json")
    listener = socket.socket(fileno=listen_fd)
    host, port = listener.getsockname()
    info = {"protocol_version": PROTOCOL_VERSION, "code_hash": code_hash(),
            "runtime_signature": runtime_signature(),
            "hostname": socket.gethostname(), "pid": os.getpid(), "process_start": process_start(os.getpid()),
            "config_path": str(server.POLICY_CONFIG_PATH), "repo_root": str(server.REPO_ROOT),
            "server_instance_id": server.SERVER_INSTANCE_ID, "url": f"http://{host}:{port}",
            "codex_shared": True, **credentials}
    reservations.atomic_write_json(root / "backend.json", info)
    app = make_app(server, credentials)
    web_server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    server.CODEX_RUNTIME.request_shutdown = lambda: setattr(web_server, "should_exit", True)
    web_server.run(sockets=[listener])
