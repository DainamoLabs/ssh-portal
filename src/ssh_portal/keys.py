"""Make sure there's an SSH key the portal can log in with, before it starts.

Started from a terminal, ssh-portal offers to create a key if this computer
has none, and unlocks a key that has a passphrase with ssh-add, so nobody
has to know those commands. Without a terminal it changes nothing, and the
UI explains what's missing instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import asyncssh

from .onboard import KEY_FILES


def _agent_has_keys() -> bool | None:
    """Whether ssh-agent holds a key; None if there's no agent to ask."""
    if not os.environ.get("SSH_AUTH_SOCK"):
        return None
    try:
        status = subprocess.run(["ssh-add", "-l"], capture_output=True, timeout=10).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {0: True, 1: False}.get(status)  # 2: the agent didn't answer


def _key_files() -> list[Path]:
    ssh_dir = Path.home() / ".ssh"
    return [ssh_dir / name for name in KEY_FILES if (ssh_dir / name).exists()]


def _loads_without_passphrase(path: Path) -> bool:
    try:
        asyncssh.read_private_key(path)
    except (OSError, ValueError, asyncssh.KeyImportError):
        return False
    return True


def _yes(question: str) -> bool:
    try:
        reply = input(f"{question} [Y/n] ")
    except EOFError:
        return False
    return reply.strip().lower() in ("", "y", "yes")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def prepare() -> None:
    if not _interactive():
        return
    agent = _agent_has_keys()
    if agent:
        return

    keys = _key_files()
    if not keys:
        print("ssh-portal logs in to your machines with an SSH key, and this computer doesn't have one yet.")
        if not _yes("Create one now?"):
            return
        path = Path.home() / ".ssh" / "id_ed25519"
        # Given a path, ssh-keygen won't create ~/.ssh itself, and a fresh account may not have one.
        path.parent.mkdir(mode=0o700, exist_ok=True)
        print("Choose a passphrase to protect it.")
        if subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(path)]).returncode != 0:
            return
        print()
        keys = [path]

    if any(_loads_without_passphrase(path) for path in keys):
        return
    if agent is None:
        print(
            "Your SSH key has a passphrase, and there's no ssh-agent to unlock it into. "
            "Start ssh-portal with ./run.sh, which starts one for it."
        )
        return
    print("Unlocking your SSH key for this session:")
    if subprocess.run(["ssh-add", str(keys[0])]).returncode == 0:
        print()
