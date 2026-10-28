"""Host inventory, read from a TOML file."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Names end up in URLs and element ids, displays end up in a remote shell
# command, so both are kept to a strict alphabet.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
DISPLAY_RE = re.compile(r"^:\d{1,3}(\.\d{1,2})?$")

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


def load_hosts(path: Path) -> list[Host]:
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    entries = data.get("host", [])
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: expected [[host]] tables")

    hosts: list[Host] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries, 1):
        where = f"{path}: host #{i}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: expected a table")
        unknown = set(entry) - {"name", "address", "user", "port", "display"}
        if unknown:
            raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")

        name = entry.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ConfigError(f"{where}: name must be 1-63 letters, digits, '-', '_' or '.'")
        if name in seen:
            raise ConfigError(f"{where}: duplicate name {name!r}")
        seen.add(name)

        address = entry.get("address")
        if not isinstance(address, str) or not address.strip():
            raise ConfigError(f"{where} ({name}): address is required")

        user = entry.get("user")
        if user is not None and (not isinstance(user, str) or not user):
            raise ConfigError(f"{where} ({name}): user must be a non-empty string")

        port = entry.get("port")
        if port is not None and (not isinstance(port, int) or not 1 <= port <= 65535):
            raise ConfigError(f"{where} ({name}): port must be 1-65535")

        display = entry.get("display", ":0")
        if not isinstance(display, str) or not DISPLAY_RE.match(display):
            raise ConfigError(f"{where} ({name}): display must look like ':0'")

        hosts.append(Host(name=name, address=address.strip(), user=user, port=port, display=display))
    return hosts
