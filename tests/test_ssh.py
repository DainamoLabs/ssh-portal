import json
import os
import shutil
import signal
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

import asyncssh
import pytest

import fakesshd
from ssh_portal.ssh import HostUnavailable
from ssh_portal.vnc import Screens, ScreenUnavailable

TESTS = Path(__file__).parent

# What the x11vnc start script needs from the remote machine, and nothing else,
# so the tests decide whether "x11vnc" exists.
TOOLS = ["sh", "id", "mkdir", "chmod", "cat", "rm", "setsid", "grep", "head", "tail", "sleep", "printf"]


@pytest.fixture
def remote_env():
    """A fake remote user: a PATH without x11vnc and a short XDG_RUNTIME_DIR.

    Unix socket paths are limited to 108 bytes, so this lives directly under /tmp.
    """
    root = Path(tempfile.mkdtemp(prefix="sp"))
    bin_dir = root / "bin"
    bin_dir.mkdir()
    for tool in TOOLS:
        (bin_dir / tool).symlink_to(shutil.which(tool))
    runtime = root / "run"
    runtime.mkdir(mode=0o700)
    env = {"PATH": str(bin_dir), "XDG_RUNTIME_DIR": str(runtime), "FAKE_X11VNC_ARGS": str(root / "args.json")}
    yield root, env
    for pidfile in runtime.glob("ssh-portal/*.pid"):
        with suppress(ProcessLookupError, ValueError):
            os.kill(int(pidfile.read_text()), signal.SIGTERM)
    shutil.rmtree(root)


def install_fake_x11vnc(root: Path) -> None:
    script = root / "bin" / "x11vnc"
    script.write_text(f"#!/bin/sh\nexec {sys.executable} -I {TESTS / 'fake_x11vnc.py'} \"$@\"\n")
    script.chmod(0o755)


async def test_run_returns_output_and_exit_status(sshd):
    conns = sshd.connections()
    ok = await conns.run("local", "echo hello; echo oops >&2", timeout=10)
    assert ok == {"exit_status": 0, "exit_signal": None, "stdout": "hello\n", "stderr": "oops\n"}
    failed = await conns.run("local", "exit 3", timeout=10)
    assert failed["exit_status"] == 3
    await conns.close_all()


async def test_connection_is_reused(sshd):
    conns = sshd.connections()
    first = await conns.get("local")
    assert await conns.get("local") is first
    await conns.close_all()


async def test_run_times_out_and_keeps_partial_output(sshd):
    conns = sshd.connections()
    result = await conns.run("local", "echo started; sleep 5", timeout=0.5)
    assert result["error"] == "Timed out after 0.5s"
    assert result["stdout"] == "started\n"
    await conns.close_all()


async def test_unknown_host_key_explains_how_to_trust_it(sshd, tmp_path):
    (tmp_path / "other_known_hosts").write_text("")
    conns = fakesshd.Connections(
        [sshd.host()], ssh_options={**sshd.ssh_options(), "known_hosts": str(tmp_path / "other_known_hosts")}
    )
    with pytest.raises(HostUnavailable, match=r"Host key not trusted yet. Run `ssh .*127.0.0.1 -p \d+` once"):
        await conns.get("local")
    assert (await conns.status("local"))["needs"] == "trust"


async def test_rejected_key_suggests_ssh_copy_id(sshd):
    stranger = asyncssh.generate_private_key("ssh-ed25519")
    conns = fakesshd.Connections([sshd.host()], ssh_options={**sshd.ssh_options(), "client_keys": [stranger]})
    with pytest.raises(HostUnavailable, match="ssh-copy-id"):
        await conns.get("local")
    assert (await conns.status("local"))["needs"] == "key"


async def test_closed_port_says_sshd_is_not_running(sshd, free_port):
    conns = sshd.connections(fakesshd.Host(name="local", address="127.0.0.1", port=free_port))
    assert await conns.status("local") == {
        "online": False,
        "error": "Connection refused. Is openssh-server installed and running?",
    }


async def test_screen_reports_missing_x11vnc(tmp_path, remote_env):
    _, env = remote_env
    server = await fakesshd.start(tmp_path, env)
    screens = Screens(server.connections())
    with pytest.raises(ScreenUnavailable, match="x11vnc is not installed"):
        await screens.ensure("local")
    await server.close()


async def test_screen_starts_x11vnc_on_a_private_socket_and_reuses_it(tmp_path, remote_env):
    root, env = remote_env
    install_fake_x11vnc(root)
    server = await fakesshd.start(tmp_path, env)
    conns = server.connections()
    screens = Screens(conns)

    path = await screens.ensure("local")
    assert path == f"{env['XDG_RUNTIME_DIR']}/ssh-portal/vnc-0.sock"
    assert (Path(path).parent.stat().st_mode & 0o777) == 0o700

    args = json.loads(Path(env["FAKE_X11VNC_ARGS"]).read_text())
    # The flags that keep x11vnc off every TCP port; see vnc.py.
    for flag, value in [("-rfbport", "0"), ("-rfbportv6", "0"), ("-unixsock", path), ("-display", ":0")]:
        assert args[args.index(flag) + 1] == value
    assert "-noipv6" in args

    reader, writer = await screens.open("local")
    assert await reader.readexactly(12) == b"RFB 003.008\n"
    writer.write(b"ping")
    assert await reader.readexactly(9) == b"echo:ping"
    writer.close()

    # A second start finds the running server instead of launching another.
    pid = (Path(path).parent / "vnc-0.pid").read_text()
    assert await screens.ensure("local", fresh=True) == path
    assert (Path(path).parent / "vnc-0.pid").read_text() == pid

    await conns.close_all()
    await server.close()


async def test_screen_explains_why_x11vnc_failed(tmp_path, remote_env):
    root, env = remote_env
    install_fake_x11vnc(root)
    server = await fakesshd.start(tmp_path, {**env, "FAKE_X11VNC_FAIL": "1"})
    screens = Screens(server.connections())
    with pytest.raises(ScreenUnavailable) as exc:
        await screens.ensure("local")
    message = str(exc.value)
    assert "Is someone logged in to the desktop on display :0?" in message
    assert "XOpenDisplay failed" in message
    await server.close()
