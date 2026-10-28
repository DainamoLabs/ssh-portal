"""Browser <-> portal <-> SSH, end to end, through a real uvicorn server."""

import asyncio
import json
import shutil

import httpx2
import pytest
import uvicorn
from websockets.asyncio.client import connect

import fakesshd
from ssh_portal.web import create_app
from test_ssh import install_fake_x11vnc, remote_env  # noqa: F401  (fixture)

TOKEN = "bridge-test-token"


@pytest.fixture
async def portal(tmp_path, remote_env, free_port):  # noqa: F811
    root, env = remote_env
    install_fake_x11vnc(root)
    (root / "bin" / "stty").symlink_to(shutil.which("stty"))
    sshd = await fakesshd.start(tmp_path, env)
    app = create_app(sshd.connections(), token=TOKEN, port=free_port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=free_port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    yield f"127.0.0.1:{free_port}"
    server.should_exit = True
    await task
    await sshd.close()


def ws_connect(addr: str, path: str):
    port = addr.rsplit(":", 1)[1]
    return connect(
        f"ws://{addr}{path}",
        origin=f"http://{addr}",
        additional_headers={"Cookie": f"ssh_portal_{port}={TOKEN}"},
    )


async def read_until(ws, needle: bytes, timeout: float = 5) -> bytes:
    seen = b""
    async with asyncio.timeout(timeout):
        while needle not in seen:
            msg = await ws.recv()
            seen += msg if isinstance(msg, bytes) else msg.encode()
    return seen


async def test_terminal_runs_a_shell_and_follows_resizes(portal):
    async with ws_connect(portal, "/ws/term/local?cols=100&rows=30") as ws:
        await ws.send(b"stty size; echo done-$((6*7))\r")
        assert b"30 100" in await read_until(ws, b"done-42")

        await ws.send(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        await ws.send(b"stty size\r")
        await read_until(ws, b"40 120")


async def test_terminal_closes_when_the_shell_exits(portal):
    async with ws_connect(portal, "/ws/term/local") as ws:
        await ws.send(b"exit\r")
        async with asyncio.timeout(5):
            async for _ in ws:
                pass
        assert ws.close_code == 1000


async def test_vnc_socket_carries_raw_rfb_bytes(portal):
    async with ws_connect(portal, "/ws/vnc/local") as ws:
        assert await ws.recv() == b"RFB 003.008\n"
        await ws.send(b"hello")
        assert await read_until(ws, b"echo:hello")


async def test_run_and_screen_endpoints(portal):
    port = portal.rsplit(":", 1)[1]
    async with httpx2.AsyncClient(base_url=f"http://{portal}", cookies={f"ssh_portal_{port}": TOKEN}) as client:
        res = await client.post("/api/run", json={"hosts": ["local"], "command": "echo $((2+2))"})
        assert res.json()["local"]["stdout"] == "4\n"
        assert (await client.get("/api/hosts/local/status")).json() == {"online": True}
        assert (await client.post("/api/hosts/local/screen")).json() == {"ok": True}
