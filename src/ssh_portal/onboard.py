"""Bring a new machine under the portal: confirm who it is, then install your key.

These are the steps `ssh` and `ssh-copy-id` walk you through in a terminal,
with the same safeguards:

- A host key is trusted only after you've compared its fingerprint with the
  one the machine printed itself (setup-target.sh shows it), and only the
  exact key you compared is saved.
- A key that differs from the one already on file is refused, never
  replaced: that's what impersonating your machine would look like.
- A password is used for the one login that copies your public key, then
  dropped. It's never written anywhere.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import asyncssh

from .config import Host
from .ssh import Connections, describe_error

KEY_FILES = ("id_ed25519", "id_ecdsa", "id_ed25519_sk", "id_ecdsa_sk", "id_rsa")

# What `ssh-keygen -lf` calls each key type, so the UI can say which line to compare.
KEY_TYPE_NAMES = {
    "ssh-ed25519": "ED25519",
    "ssh-rsa": "RSA",
    "ecdsa-sha2-nistp256": "ECDSA",
    "ecdsa-sha2-nistp384": "ECDSA",
    "ecdsa-sha2-nistp521": "ECDSA",
}

NO_KEY = "There's no SSH key on this computer yet. Create one in a terminal with: ssh-keygen -t ed25519"

# Appends the public key on stdin to ~/.ssh/authorized_keys, like ssh-copy-id.
INSTALL_KEY = r"""
set -e
umask 077
IFS= read -r key
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
keys="$HOME/.ssh/authorized_keys"
touch "$keys"
chmod 600 "$keys"
if ! grep -qxF "$key" "$keys"; then
    # Don't glue the new key onto a last line that has no newline.
    if [ -s "$keys" ] && [ -n "$(tail -c 1 "$keys")" ]; then echo >>"$keys"; fi
    printf '%s\n' "$key" >>"$keys"
fi
"""


class OnboardingError(Exception):
    """Something the person adding a machine needs to fix. Shown in the UI as-is."""


async def find_public_key() -> str | None:
    """The public half of a key the portal logs in with, as an authorized_keys line."""
    ssh_dir = Path.home() / ".ssh"
    for name in KEY_FILES:
        private, public = ssh_dir / name, ssh_dir / f"{name}.pub"
        if private.exists() and public.exists():
            line = public.read_text().strip().split("\n", 1)[0]
            try:
                asyncssh.import_public_key(line)
            except (asyncssh.KeyImportError, ValueError):
                continue
            return line

    # No key file: fall back to whatever ssh-agent holds (a hardware key, say).
    if not os.environ.get("SSH_AUTH_SOCK"):
        return None
    try:
        agent = await asyncssh.connect_agent()
        try:
            keys = await agent.get_keys()
        finally:
            agent.close()
            await agent.wait_closed()
    except (OSError, asyncssh.Error):
        return None
    for key in keys:
        # The SSH wire format starts with the key type as a length-prefixed string.
        size = int.from_bytes(key.public_data[:4], "big")
        kind = key.public_data[4 : 4 + size].decode()
        return f"{kind} {base64.b64encode(key.public_data).decode()} {key.get_comment() or ''}".strip()
    return None


def _known_hosts_pattern(address: str, port: int | None) -> str:
    return address if port in (None, 22) else f"[{address}]:{port}"


def _same_key(a: asyncssh.SSHKey, b: asyncssh.SSHKey) -> bool:
    return a.public_data == b.public_data


def _append_known_host(path: Path, pattern: str, key: asyncssh.SSHKey) -> None:
    # Hashed like OpenSSH's HashKnownHosts (on by default in Mint), so the file
    # doesn't double as a list of your machines.
    salt = os.urandom(20)
    digest = hmac.new(salt, pattern.encode(), hashlib.sha1).digest()
    line = f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(digest).decode()} "
    line += key.export_public_key("openssh").decode().strip()

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    needs_newline = False
    if path.exists() and path.stat().st_size:
        with path.open("rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_newline = f.read(1) != b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(("\n" if needs_newline else "") + line + "\n")


class Onboarding:
    def __init__(
        self,
        connections: Connections,
        *,
        public_key: Callable[[], Awaitable[str | None]] = find_public_key,
        timeout: float = 8,
    ) -> None:
        self._connections = connections
        self._public_key = public_key
        self._timeout = timeout
        self._lock = asyncio.Lock()

    def _trusted_keys(self, address: str, port: int | None) -> list[asyncssh.SSHKey]:
        path = self._connections.known_hosts_path
        if not path.exists():
            return []
        try:
            known = asyncssh.read_known_hosts(str(path))
        except (OSError, ValueError, asyncssh.KeyImportError) as exc:
            raise OnboardingError(f"Couldn't read {path}: {exc}") from exc
        return list(known.match(address, "", None if port in (None, 22) else port)[0])

    async def _host_key(self, address: str, port: int | None, algs: list[str] | None = None) -> asyncssh.SSHKey:
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(
                address,
                port or (),
                server_host_key_algs=algs or (),
                config=self._connections.ssh_options.get("config", ()),
            ),
            self._timeout,
        )
        if key is None:
            raise OnboardingError("The machine didn't present a host key.")
        return key

    async def _fetch(self, address: str, port: int | None, known: list[asyncssh.SSHKey]) -> asyncssh.SSHKey:
        if known:
            # Ask for the same kind of key that's on file, as ssh would.
            return await self._host_key(address, port, [a.decode() for k in known for a in k.sig_algorithms])
        try:
            # ED25519 when there's a choice: one short line to compare.
            return await self._host_key(address, port, ["ssh-ed25519"])
        except asyncssh.KeyExchangeFailed:
            return await self._host_key(address, port)

    async def check(self, address: str, port: int | None, user: str | None) -> dict[str, Any]:
        """Report how far along a machine is: reachable, identity, key login, screen sharing."""
        host = Host(name="new", address=address, user=user, port=port)
        result: dict[str, Any] = {"have_key": await self._public_key() is not None}

        known = self._trusted_keys(address, port)
        try:
            key = await self._fetch(address, port, known)
        except asyncssh.KeyExchangeFailed:
            # It no longer offers the kind of key that's on file.
            return {**result, "reachable": True, "identity": "changed"}
        except (OSError, asyncssh.Error) as exc:
            return {**result, "reachable": False, "error": describe_error(exc, host)}

        result |= {
            "reachable": True,
            "fingerprint": key.get_fingerprint("sha256"),
            "key_type": KEY_TYPE_NAMES.get(key.algorithm.decode(), key.algorithm.decode()),
        }
        if not known:
            return {**result, "identity": "unknown"}
        if not any(_same_key(k, key) for k in known):
            return {**result, "identity": "changed"}
        result["identity"] = "trusted"

        options = {"connect_timeout": self._timeout, **self._connections.ssh_options}
        if user:
            options["username"] = user
        try:
            conn = await asyncssh.connect(address, port=port or (), **options)
        except asyncssh.PermissionDenied:
            return {**result, "login": "needs_key"}
        except (OSError, asyncssh.Error) as exc:
            return {**result, "login": "error", "error": describe_error(exc, host)}
        async with conn:
            probe = await conn.run("command -v x11vnc", check=False, timeout=self._timeout)
        return {**result, "login": "ok", "x11vnc": probe.exit_status == 0}

    async def trust(self, address: str, port: int | None, fingerprint: str) -> None:
        """Save the machine's host key, if it's still the one whose fingerprint you checked."""
        async with self._lock:
            known = self._trusted_keys(address, port)
            try:
                key = await self._fetch(address, port, known)
            except asyncssh.KeyExchangeFailed:
                key = None
            except (OSError, asyncssh.Error) as exc:
                raise OnboardingError(describe_error(exc, Host(name="new", address=address, port=port))) from exc

            pattern = _known_hosts_pattern(address, port)
            if known:
                if key is not None and any(_same_key(k, key) for k in known):
                    return
                raise OnboardingError(
                    "This machine's key doesn't match the one you trusted before. If you reinstalled it, "
                    f"remove the old key with `ssh-keygen -R '{pattern}'` and check again. "
                    "If you didn't, something may be impersonating it."
                )
            if key is None or key.get_fingerprint("sha256") != fingerprint:
                raise OnboardingError(
                    "The machine presented a different key from the one you compared. Check again before trusting it."
                )
            _append_known_host(self._connections.known_hosts_path, pattern, key)

    async def install_key(self, address: str, port: int | None, user: str | None, password: str) -> None:
        """Log in once with a password and add your public key to authorized_keys."""
        host = Host(name="new", address=address, user=user, port=port)
        public_key = await self._public_key()
        if not public_key:
            raise OnboardingError(NO_KEY)

        options = {
            "connect_timeout": self._timeout,
            **self._connections.ssh_options,
            "client_keys": None,
            "agent_path": None,
            "password": password,
            "preferred_auth": "keyboard-interactive,password",
        }
        if user:
            options["username"] = user
        try:
            conn = await asyncssh.connect(address, port=port or (), **options)
        except asyncssh.PermissionDenied as exc:
            raise OnboardingError(
                "Password not accepted. If it's right, password logins may already be off on that machine "
                "(setup-target.sh --lock); copy your key from a terminal that still has access instead."
            ) from exc
        except asyncssh.HostKeyNotVerifiable as exc:
            raise OnboardingError("Confirm the machine's identity first.") from exc
        except (OSError, asyncssh.Error) as exc:
            raise OnboardingError(describe_error(exc, host)) from exc

        async with conn:
            result = await conn.run(
                f"sh -c {shlex.quote(INSTALL_KEY)}", input=public_key + "\n", check=False, timeout=self._timeout
            )
        if result.exit_status != 0:
            reason = str(result.stderr).strip() or f"exit status {result.exit_status}"
            raise OnboardingError(f"Couldn't install your key: {reason}")
