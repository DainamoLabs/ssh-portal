"""Getting a usable SSH key at startup, without knowing ssh-keygen or ssh-add."""

import io
import subprocess
from types import SimpleNamespace

import asyncssh
import pytest

from ssh_portal import keys

IS_A_TERMINAL = keys._interactive


@pytest.fixture
def term(tmp_path, monkeypatch):
    """A terminal session with an empty home, and a record of the ssh tools it runs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    session = SimpleNamespace(home=tmp_path, ran=[], answers=[], agent_status=1, make_key=None)
    monkeypatch.setattr(keys, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": session.answers.pop(0))

    def run(cmd, **kwargs):
        session.ran.append(cmd)
        if cmd == ["ssh-add", "-l"]:
            return subprocess.CompletedProcess(cmd, session.agent_status)
        if cmd[0] == "ssh-keygen" and session.make_key:
            session.make_key(cmd[cmd.index("-f") + 1])
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(keys.subprocess, "run", run)
    return session


def write_key(path, passphrase=None):
    key = asyncssh.generate_private_key("ssh-ed25519")
    if passphrase:
        data = key.export_private_key("pkcs8-pem", passphrase=passphrase)
    else:
        data = key.export_private_key()
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)


def test_without_a_terminal_nothing_is_asked(term, monkeypatch, capsys):
    monkeypatch.setattr(keys, "_interactive", IS_A_TERMINAL)
    monkeypatch.setattr("sys.stdin", io.StringIO())
    keys.prepare()
    assert term.ran == []
    assert capsys.readouterr().out == ""


def test_a_key_in_the_agent_is_enough(term, capsys):
    term.agent_status = 0
    keys.prepare()
    assert term.ran == [["ssh-add", "-l"]]
    assert capsys.readouterr().out == ""


def test_no_key_offers_to_create_one(term, capsys):
    term.answers = [""]

    def ssh_keygen(path):
        # Like the real one, it fails if the directory isn't there.
        ssh_dir = term.home / ".ssh"
        assert ssh_dir.is_dir() and ssh_dir.stat().st_mode & 0o777 == 0o700
        write_key(ssh_dir / "id_ed25519")

    term.make_key = ssh_keygen
    keys.prepare()
    assert ["ssh-keygen", "-t", "ed25519", "-f", str(term.home / ".ssh" / "id_ed25519")] in term.ran
    assert "doesn't have one yet" in capsys.readouterr().out
    assert term.ran[-1][0] == "ssh-keygen"  # no passphrase, so nothing to unlock


def test_declining_creates_nothing(term):
    term.answers = ["n"]
    keys.prepare()
    assert not any(cmd[0] == "ssh-keygen" for cmd in term.ran)


def test_a_key_with_a_passphrase_is_unlocked_into_the_agent(term):
    write_key(term.home / ".ssh" / "id_ed25519", passphrase="secret")
    keys.prepare()
    assert term.ran[-1] == ["ssh-add", str(term.home / ".ssh" / "id_ed25519")]


def test_without_an_agent_it_points_to_run_sh(term, monkeypatch, capsys):
    monkeypatch.delenv("SSH_AUTH_SOCK")
    write_key(term.home / ".ssh" / "id_ed25519", passphrase="secret")
    keys.prepare()
    assert term.ran == []
    assert "./run.sh" in capsys.readouterr().out


def test_a_key_without_a_passphrase_needs_nothing(term, capsys):
    write_key(term.home / ".ssh" / "id_ed25519")
    keys.prepare()
    assert term.ran == [["ssh-add", "-l"]]
    assert capsys.readouterr().out == ""
