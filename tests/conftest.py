from __future__ import annotations

import socket

import pytest

import fakesshd


@pytest.fixture
async def sshd(tmp_path):
    server = await fakesshd.start(tmp_path)
    yield server
    await server.close()


@pytest.fixture
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
