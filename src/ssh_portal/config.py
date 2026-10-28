"""Host inventory, kept in a TOML file that you can edit by hand or from the UI."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Names end up in URLs and element ids; addresses and users end up in shell
# commands the UI suggests (`ssh-copy-id you@address`); displays end up in a
# remote shell command. So all of them are kept to a strict alphabet, with no
# spaces, quotes or leading dashes.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
ADDRESS_RE = re.compile(r"^(?!-)[A-Za-z0-9_.:%-]{1,253}$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
DISPLAY_RE = re.compile(r"^:\d{1,3}(\.\d{1,2})?$")
HOST_HEADER_RE = re.compile(r"^\s*\[\[\s*host\s*\]\]\s*(#.*)?$")

NEW_FILE_HEADER = "# Machines for ssh-portal. Edit by hand, or add and remove them in the UI.\n"

EXAMPLE = """\
[[host]]
name = "office"          # shown in the UI; letters, digits, - _ .
address = "192.168.1.20" # IP, hostname, or an alias from ~/.ssh/config
user = "you"             # optional, defaults to ~/.ssh/config or your username
# port = 22
# display = ":0"         # X display of the logged-in desktop
"""


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "ssh-portal" / "hosts.toml"


class ConfigError(ValueError):
    pass


def _toml_str(value: str) -> str:
    # A JSON string is a valid TOML basic string for the ASCII values the
    # validators allow; the result is re-parsed before it's written anyway.
    return json.dumps(value)


@dataclass(frozen=True)
class Host:
    name: str
    address: str
    user: str | None = None
    port: int | None = None
    display: str = ":0"

    @property
    def target(self) -> str:
        """How you'd type this host on an ssh command line."""
        target = f"{self.user}@{self.address}" if self.user else self.address
        return f"{target} -p {self.port}" if self.port else target

    def to_toml(self) -> str:
        lines = ["[[host]]", f"name = {_toml_str(self.name)}", f"address = {_toml_str(self.address)}"]
        if self.user:
            lines.append(f"user = {_toml_str(self.user)}")
        if self.port:
            lines.append(f"port = {self.port}")
        if self.display != ":0":
            lines.append(f"display = {_toml_str(self.display)}")
        return "\n".join(lines) + "\n"


def parse_host(entry: object) -> Host:
    """Validate one [[host]] table. Messages are written to be shown to a person as-is."""
    if not isinstance(entry, dict):
        raise ConfigError("Expected a [[host]] table")
    unknown = set(entry) - {"name", "address", "user", "port", "display"}
    if unknown:
        raise ConfigError(f"Unknown settings: {', '.join(sorted(unknown))}")

    name = entry.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ConfigError("Name can only use letters, digits, '-', '_' and '.' (up to 63 characters)")

    address = entry.get("address")
    if not isinstance(address, str) or not address.strip():
        raise ConfigError("Address is required")
    address = address.strip()
    if not ADDRESS_RE.match(address):
        raise ConfigError("Address must be an IP address or hostname")

    user = entry.get("user")
    if user is not None and (not isinstance(user, str) or not USER_RE.match(user)):
        raise ConfigError("User must be a login name, like the one you sign in to that machine with")

    port = entry.get("port")
    if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535):
        raise ConfigError("Port must be a number from 1 to 65535")

    display = entry.get("display", ":0")
    if not isinstance(display, str) or not DISPLAY_RE.match(display):
        raise ConfigError("Display must look like :0")

    return Host(name=name, address=address, user=user, port=port, display=display)


def parse_hosts(text: str, source: str) -> list[Host]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: {exc}") from exc

    entries = data.get("host", [])
    if not isinstance(entries, list):
        raise ConfigError(f"{source}: expected [[host]] tables")

    hosts: list[Host] = []
    for i, entry in enumerate(entries, 1):
        try:
            host = parse_host(entry)
        except ConfigError as exc:
            raise ConfigError(f"{source}: host #{i}: {exc}") from exc
        if any(h.name == host.name for h in hosts):
            raise ConfigError(f"{source}: host #{i}: a machine named {host.name!r} already exists")
        hosts.append(host)
    return hosts


def load_hosts(path: Path) -> list[Host]:
    if not path.exists():
        return []
    return parse_hosts(path.read_text(), str(path))


def add_host(path: Path, host: Host) -> None:
    """Append one [[host]] table, leaving the rest of the file (comments too) untouched."""
    text = path.read_text() if path.exists() else NEW_FILE_HEADER
    existing = parse_hosts(text, str(path))
    if any(h.name == host.name for h in existing):
        raise ConfigError(f"A machine named {host.name!r} already exists")
    updated = text.rstrip("\n") + ("\n\n" if text.strip() else "") + host.to_toml()
    _write_checked(path, updated, [*existing, host])


def remove_host(path: Path, name: str) -> None:
    """Cut one [[host]] table out of the file, with any comment lines directly above it."""
    text = path.read_text() if path.exists() else ""
    existing = parse_hosts(text, str(path))
    index = next((i for i, h in enumerate(existing) if h.name == name), None)
    if index is None:
        raise ConfigError(f"{path} has no machine named {name!r}. Was it edited by hand?")

    lines = text.splitlines(keepends=True)
    headers = [i for i, line in enumerate(lines) if HOST_HEADER_RE.match(line)]
    if len(headers) != len(existing):
        raise ConfigError(f"{path} is laid out in a way the UI can't edit safely. Remove {name!r} by hand.")

    def with_leading_comments(i: int) -> int:
        while i > 0 and lines[i - 1].lstrip().startswith("#"):
            i -= 1
        return i

    start = with_leading_comments(headers[index])
    end = with_leading_comments(headers[index + 1]) if index + 1 < len(headers) else len(lines)
    updated = re.sub(r"\n{3,}", "\n\n", "".join(lines[:start] + lines[end:])).rstrip()
    _write_checked(path, updated + "\n" if updated else "", existing[:index] + existing[index + 1 :])


def _write_checked(path: Path, text: str, expected: list[Host]) -> None:
    # Never write a file that wouldn't load back as exactly what was intended.
    if parse_hosts(text, str(path)) != expected:
        raise ConfigError(f"{path} couldn't be updated safely. Edit it by hand.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic: a crash leaves the old file or the new one, never half of each
