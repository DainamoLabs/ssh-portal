"""A throwaway SSH server for tests.

It runs commands and PTY shells as the current user and allows Unix-socket
forwarding, which is everything ssh-portal asks of a stock OpenSSH sshd. Keys
and known_hosts are generated fresh for every server.
"""

from __future__ import annotations

import asyncio
import fcntl
import getpass
import os
import pty
import shutil
import struct
import termios
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import asyncssh

from ssh_portal.config import Host
from ssh_portal.ssh import Connections

# Resolved now, because tests may give commands a PATH without it.
BASH = shutil.which("bash")


@dataclass
class FakeServer:
    port: int
    known_hosts: Path
    client_key: asyncssh.SSHKey
    server: asyncssh.SSHAcceptor
    clients: list[Connections] = field(default_factory=list)

    def host(self, name: str = "local", **overrides) -> Host:
        return Host(name=name, address="127.0.0.1", port=self.port, user=getpass.getuser(), **overrides)

    def connections(self, *hosts: Host) -> Connections:
        conns = Connections(list(hosts) or [self.host()], connect_timeout=5, ssh_options=self.ssh_options())
        self.clients.append(conns)
        return conns

    def ssh_options(self) -> dict:
        return {
            "known_hosts": str(self.known_hosts),
            "client_keys": [self.client_key],
            "agent_path": None,
            "config": [],
        }

    async def close(self) -> None:
        # Since Python 3.12 wait_closed() also waits for open connections.
        for conns in self.clients:
            await conns.close_all()
        self.server.close()
        await self.server.wait_closed()


class _Server(asyncssh.SSHServer):
    def unix_connection_requested(self, dest_path: str) -> bool:
        return True


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def _exec(process: asyncssh.SSHServerProcess, env: dict[str, str]) -> None:
    proc = await asyncio.create_subprocess_shell(
        process.command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=env["HOME"],
    )

    async def pump_in() -> None:
        with suppress(BrokenPipeError, ConnectionResetError):
            while data := await process.stdin.read(65536):
                proc.stdin.write(data)
                await proc.stdin.drain()
        proc.stdin.close()

    async def pump_out(src: asyncio.StreamReader, dst) -> None:
        while data := await src.read(65536):
            dst.write(data)

    feeder = asyncio.ensure_future(pump_in())
    await asyncio.gather(pump_out(proc.stdout, process.stdout), pump_out(proc.stderr, process.stderr))
    code = await proc.wait()
    feeder.cancel()
    process.exit(code)


async def _shell(process: asyncssh.SSHServerProcess, env: dict[str, str]) -> None:
    master, slave = pty.openpty()
    cols, rows, *_ = process.term_size
    _set_winsize(master, cols or 80, rows or 24)
    proc = await asyncio.create_subprocess_exec(
        BASH, "--noprofile", "--norc", "-i",
        stdin=slave, stdout=slave, stderr=slave,
        env={**env, "TERM": process.term_type or "xterm", "PS1": "fake$ "},
        cwd=env["HOME"],
        start_new_session=True,
        preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
    )
    os.close(slave)
    loop = asyncio.get_running_loop()
    output_done = loop.create_future()

    def readable() -> None:
        try:
            data = os.read(master, 65536)
        except OSError:
            data = b""
        if data:
            process.stdout.write(data)
        else:
            loop.remove_reader(master)
            if not output_done.done():
                output_done.set_result(None)

    loop.add_reader(master, readable)

    async def pump_in() -> None:
        while True:
            try:
                data = await process.stdin.read(65536)
            except asyncssh.TerminalSizeChanged as change:
                _set_winsize(master, change.width, change.height)
                continue
            if not data:
                return
            os.write(master, data)

    feeder = asyncio.ensure_future(pump_in())
    await output_done
    code = await proc.wait()
    feeder.cancel()
    os.close(master)
    process.exit(code)


async def start(tmp: Path, env: dict[str, str] | None = None) -> FakeServer:
    """Listen on a free loopback port. `env` overrides the environment that commands see."""
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    command_env = {
        "HOME": str(Path.home()),
        "USER": getpass.getuser(),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
        **(env or {}),
    }

    async def handle(process: asyncssh.SSHServerProcess) -> None:
        if process.command is None:
            await _shell(process, command_env)
        else:
            await _exec(process, command_env)

    server = await asyncssh.create_server(
        _Server,
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        authorized_client_keys=asyncssh.import_authorized_keys(client_key.export_public_key().decode()),
        process_factory=handle,
        encoding=None,
        line_editor=False,
    )
    port = server.get_port()
    known_hosts = tmp / "known_hosts"
    known_hosts.write_text(f"[127.0.0.1]:{port} {host_key.export_public_key().decode()}")
    return FakeServer(port=port, known_hosts=known_hosts, client_key=client_key, server=server)
