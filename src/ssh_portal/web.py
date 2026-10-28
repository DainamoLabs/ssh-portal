"""The web UI: a REST API, a terminal WebSocket and a VNC WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import asyncssh
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose, WebSocketDisconnect

from .config import EXAMPLE
from .ssh import Connections, HostUnavailable
from .vnc import Screens, ScreenUnavailable

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Browsers refuse ES modules served with a non-JavaScript type, and Python's
# table doesn't know .mjs on every platform.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")


class LocalOnly:
    """Gate every request and WebSocket behind a per-run secret.

    The portal can run commands on every machine you own, so nothing but your
    own browser tab may reach it. The Host header must be loopback (blocks DNS
    rebinding), a WebSocket's Origin must be this page (browsers don't apply
    the same-origin policy to WebSockets), and a SameSite=Strict cookie must
    carry the secret printed at startup.
    """

    def __init__(self, app: ASGIApp, *, token: str, port: int) -> None:
        self.app = app
        self.token = token.encode()
        self.cookie = f"ssh_portal_{port}"
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)

        if headers.get("host") not in self.hosts:
            await self._deny(scope, receive, send, 400, "Unexpected Host header.")
            return
        if scope["type"] == "websocket" and headers.get("origin") not in self.origins:
            await self._deny(scope, receive, send, 403, "Cross-origin WebSocket refused.")
            return

        if scope["type"] == "http":
            supplied = parse_qs(scope["query_string"].decode()).get("token", [""])[0]
            if supplied and self._matches(supplied):
                # Trade the secret in the URL for a cookie, and drop it from history.
                response = RedirectResponse(scope["path"] or "/", status_code=303)
                response.set_cookie(self.cookie, self.token.decode(), httponly=True, samesite="strict")
                await response(scope, receive, send)
                return

        if not self._matches(cookie_parser(headers.get("cookie", "")).get(self.cookie, "")):
            await self._deny(
                scope, receive, send, 401,
                "Not signed in. Open the link printed in the terminal where ssh-portal is running.",
            )
            return
        await self.app(scope, receive, send)

    def _matches(self, value: str) -> bool:
        return secrets.compare_digest(value.encode(), self.token)

    @staticmethod
    async def _deny(scope: Scope, receive: Receive, send: Send, status: int, message: str) -> None:
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
        else:
            await PlainTextResponse(message, status_code=status)(scope, receive, send)


class SecurityHeaders:
    def __init__(self, app: ASGIApp, *, port: int) -> None:
        self.app = app
        ws = f"ws://127.0.0.1:{port} ws://localhost:{port}"
        self.headers = [
            (
                b"content-security-policy",
                (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    f"img-src 'self' data: blob:; connect-src 'self' {ws}; "
                    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
                ).encode(),
            ),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *self.headers]
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RunRequest(BaseModel):
    hosts: list[str] = Field(min_length=1)
    command: str = Field(min_length=1, max_length=10_000)
    timeout: float = Field(default=60, gt=0, le=3600)


async def _until_first_done(*coros: Awaitable[None]) -> None:
    """Run both directions of a bridge; when either side ends, stop the other."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, WebSocketDisconnect, OSError, asyncssh.Error):
                await task


def create_app(
    connections: Connections,
    *,
    token: str,
    port: int,
    config_path: Path | None = None,
    on_ready: Callable[[], None] | None = None,
) -> ASGIApp:
    screens = Screens(connections)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if on_ready:
            asyncio.get_running_loop().call_later(0.5, on_ready)
        yield
        await connections.close_all()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def require_host(name: str) -> None:
        if name not in connections.hosts:
            raise HTTPException(404, f"No host named {name!r}")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/hosts")
    async def hosts() -> dict[str, Any]:
        return {
            "config_path": str(config_path) if config_path else None,
            "example": EXAMPLE,
            "hosts": [
                {"name": h.name, "address": h.address, "user": h.user, "port": h.port, "display": h.display}
                for h in connections.hosts.values()
            ],
        }

    @app.get("/api/hosts/{name}/status")
    async def status(name: str) -> dict[str, Any]:
        # Per host, so one machine that's switched off doesn't hold up the rest.
        require_host(name)
        return await connections.status(name)

    @app.post("/api/run")
    async def run(req: RunRequest) -> dict[str, Any]:
        for name in req.hosts:
            require_host(name)
        names = list(dict.fromkeys(req.hosts))
        results = await asyncio.gather(*(connections.run(n, req.command, req.timeout) for n in names))
        return dict(zip(names, results))

    @app.post("/api/hosts/{name}/screen")
    async def prepare_screen(name: str) -> dict[str, bool]:
        """Start x11vnc ahead of the WebSocket so the UI can show why it failed."""
        require_host(name)
        try:
            await screens.ensure(name)
        except ScreenUnavailable as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"ok": True}

    @app.websocket("/ws/term/{name}")
    async def terminal(ws: WebSocket, name: str, cols: int = 80, rows: int = 24) -> None:
        if name not in connections.hosts:
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            conn = await connections.get(name)
            proc = await conn.create_process(
                term_type="xterm-256color",
                term_size=(max(cols, 1), max(rows, 1)),
                encoding=None,
            )
        except (HostUnavailable, OSError, asyncssh.Error) as exc:
            await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
            await ws.close(code=1011)
            return

        async def to_browser() -> None:
            while data := await proc.stdout.read(65536):
                await ws.send_bytes(data)

        async def from_browser() -> None:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes"):
                    proc.stdin.write(msg["bytes"])
                    await proc.stdin.drain()
                elif msg.get("text"):
                    ctl = json.loads(msg["text"])
                    if ctl.get("type") == "resize":
                        proc.change_terminal_size(max(int(ctl["cols"]), 1), max(int(ctl["rows"]), 1))

        try:
            await _until_first_done(to_browser(), from_browser())
        finally:
            proc.close()
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.close()

    @app.websocket("/ws/vnc/{name}")
    async def vnc(ws: WebSocket, name: str) -> None:
        if name not in connections.hosts:
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            reader, writer = await screens.open(name)
        except (ScreenUnavailable, HostUnavailable, OSError, asyncssh.Error) as exc:
            log.info("screen for %s unavailable: %s", name, exc)
            await ws.close(code=1011)
            return

        async def to_browser() -> None:
            while data := await reader.read(65536):
                await ws.send_bytes(data)

        async def from_browser() -> None:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes"):
                    writer.write(msg["bytes"])
                    await writer.drain()

        try:
            await _until_first_done(to_browser(), from_browser())
        finally:
            writer.close()
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.close()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return SecurityHeaders(LocalOnly(app, token=token, port=port), port=port)
