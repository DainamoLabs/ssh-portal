"""One persistent SSH connection per host, shared by terminals, screens and commands."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncssh

from .config import Host

log = logging.getLogger(__name__)

OUTPUT_LIMIT = 1_000_000  # characters per stream returned from a one-shot command


class HostUnavailable(Exception):
    """A host couldn't be reached or refused us. The message is shown in the UI as-is."""


def describe_error(exc: BaseException, host: Host) -> str:
    match exc:
        case asyncssh.HostKeyNotVerifiable():
            return (
                f"Host key not trusted yet. Run `ssh {host.target}` once in a terminal, "
                "check the fingerprint, and accept it."
            )
        case asyncssh.PermissionDenied():
            return (
                f"Key not accepted. Run `ssh-copy-id {host.target}`, "
                "and `ssh-add` if your key has a passphrase."
            )
        case TimeoutError():
            return "Timed out. Is the machine on and connected to the network?"
        case ConnectionRefusedError():
            return "Connection refused. Is openssh-server installed and running?"
        case OSError():
            return f"Unreachable: {exc.strerror or exc}"
        case asyncssh.Error():
            return f"SSH error: {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


def _truncate(text: str | None) -> str:
    text = text or ""
    if len(text) > OUTPUT_LIMIT:
        return text[:OUTPUT_LIMIT] + f"\n… output truncated at {OUTPUT_LIMIT:,} characters"
    return text


class Connections:
    """Opens connections lazily and reuses them until they drop.

    Host keys are checked against ~/.ssh/known_hosts and client keys come from
    ssh-agent or ~/.ssh, exactly like the `ssh` command. `ssh_options` exists so
    tests can point at a throwaway server.
    """

    def __init__(
        self,
        hosts: list[Host],
        *,
        connect_timeout: float = 8.0,
        ssh_options: dict[str, Any] | None = None,
    ) -> None:
        self.hosts = {h.name: h for h in hosts}
        self._connect_timeout = connect_timeout
        self._ssh_options = ssh_options or {}
        self._conns: dict[str, asyncssh.SSHClientConnection] = {}
        self._locks = {name: asyncio.Lock() for name in self.hosts}

    async def get(self, name: str) -> asyncssh.SSHClientConnection:
        host = self.hosts[name]
        async with self._locks[name]:
            conn = self._conns.get(name)
            if conn is not None and not conn.is_closed():
                return conn
            options: dict[str, Any] = {
                "connect_timeout": self._connect_timeout,
                "keepalive_interval": 15,
                "keepalive_count_max": 3,
                **self._ssh_options,
            }
            if host.user:
                options["username"] = host.user
            if host.port:
                options["port"] = host.port
            try:
                conn = await asyncssh.connect(host.address, **options)
            except (OSError, asyncssh.Error) as exc:
                log.info("connect to %s failed: %r", name, exc)
                raise HostUnavailable(describe_error(exc, host)) from exc
            self._conns[name] = conn
            return conn

    async def status(self, name: str) -> dict[str, Any]:
        try:
            await self.get(name)
        except HostUnavailable as exc:
            return {"online": False, "error": str(exc)}
        return {"online": True}

    async def run(self, name: str, command: str, timeout: float) -> dict[str, Any]:
        try:
            conn = await self.get(name)
            result = await conn.run(
                command, check=False, timeout=timeout, encoding="utf-8", errors="replace"
            )
        except HostUnavailable as exc:
            return {"error": str(exc)}
        except asyncssh.TimeoutError as exc:
            return {
                "error": f"Timed out after {timeout:g}s",
                "stdout": _truncate(exc.stdout),
                "stderr": _truncate(exc.stderr),
            }
        except (OSError, asyncssh.Error) as exc:
            return {"error": describe_error(exc, self.hosts[name])}
        return {
            "exit_status": result.exit_status,
            "exit_signal": result.exit_signal[0] if result.exit_signal else None,
            "stdout": _truncate(result.stdout),
            "stderr": _truncate(result.stderr),
        }

    async def close_all(self) -> None:
        conns, self._conns = list(self._conns.values()), {}
        for conn in conns:
            conn.close()
        await asyncio.gather(*(c.wait_closed() for c in conns), return_exceptions=True)
