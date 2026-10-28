"""Find machines on the LAN that are running setup.sh, waiting to be added.

While setup.sh waits for your key it announces the machine over mDNS, the way
printers announce themselves. The portal lists those announcements by asking
the avahi daemon Mint already runs, through `avahi-browse`, so it opens no
listening socket of its own.

Anything on your network can make an announcement, so one only saves typing
an address. Trusting the machine still means comparing its fingerprint with
the one setup.sh shows on that machine's own screen.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from dataclasses import dataclass

from .config import ConfigError, parse_host

SERVICE = "_ssh-portal._tcp"
BROWSE = ("avahi-browse", "--resolve", "--parsable", "--terminate", "--no-db-lookup", SERVICE)
BROWSE_TIMEOUT = 4.0
MAX_MACHINES = 32


@dataclass(frozen=True)
class Nearby:
    name: str
    address: str
    user: str | None = None
    port: int | None = None
    hostname: str | None = None


def _unescape(text: str) -> str:
    # avahi-browse --parsable writes \DDD (decimal) for every byte outside
    # [A-Za-z0-9_-], and a backslash before "." and "\" themselves.
    def byte(m: re.Match[bytes]) -> bytes:
        code = m[1]
        return bytes([int(code)]) if code.isdigit() and int(code) < 256 else code[-1:]

    return re.sub(rb"\\(\d{3}|.)", byte, text.encode()).decode("utf-8", "replace")


def _txt(field: str) -> dict[str, str]:
    records = {}
    for quoted in re.findall(r'"((?:\\.|[^"\\])*)"', field):
        key, sep, value = _unescape(quoted).partition("=")
        if sep:
            records.setdefault(key.lower(), value)
    return records


def _name(service_name: str, hostname: str) -> str:
    # Two machines both called "mint" are announced as "mint" and "mint #2".
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", service_name).strip("-.")
    return name or hostname.removesuffix(".local")


def parse(output: str) -> list[Nearby]:
    """Turn `avahi-browse --resolve --parsable` output into machines, one per announcement."""
    found: dict[tuple[str, str], dict] = {}
    for line in output.splitlines():
        fields = line.split(";", 9)
        if len(fields) != 10 or fields[0] != "=":
            continue
        _, _, _, name, _, _, hostname, address, port, txt = fields
        # The same machine appears once per network interface and IP version.
        entry = found.setdefault((name, hostname), {"name": name, "hostname": hostname, "txt": _txt(txt), "ipv4": None})
        if entry["ipv4"] is None and re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", address):
            entry["ipv4"] = address
        entry["port"] = port

    machines = []
    for entry in found.values():
        hostname = _unescape(entry["hostname"])
        fields = {
            "name": _name(_unescape(entry["name"]), hostname),
            # A link-local IPv6 address is useless without its interface, so
            # fall back to the .local name, which the system resolver handles.
            "address": entry["ipv4"] or hostname,
            "user": entry["txt"].get("user") or None,
            "port": int(entry["port"]) if entry["port"].isdigit() and entry["port"] != "22" else None,
        }
        try:
            host = parse_host({k: v for k, v in fields.items() if v is not None})
        except ConfigError:
            try:
                # An odd user name shouldn't hide the machine; the portal's default user applies.
                host = parse_host({k: v for k, v in fields.items() if v is not None and k != "user"})
            except ConfigError:
                continue
        machines.append(Nearby(host.name, host.address, host.user, host.port, hostname))
        if len(machines) == MAX_MACHINES:
            break
    return sorted(machines, key=lambda m: m.name.lower())


async def nearby() -> list[Nearby] | None:
    """Machines announcing themselves right now, or None if avahi isn't available here."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *BROWSE,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None

    lines: list[bytes] = []

    async def collect() -> None:
        assert proc.stdout is not None
        while line := await proc.stdout.readline():
            lines.append(line)

    try:
        # --terminate exits once avahi has heard back, usually within a second.
        await asyncio.wait_for(collect(), BROWSE_TIMEOUT)
    except TimeoutError:
        pass
    finally:
        with suppress(ProcessLookupError):
            proc.kill()
        status = await proc.wait()

    output = b"".join(lines).decode("utf-8", "replace")
    if not output and status not in (0, -9):
        return None  # "Daemon not running", most likely
    return parse(output)
