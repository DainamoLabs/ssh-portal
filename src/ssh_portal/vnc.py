"""Start x11vnc on a host on demand, reachable only through SSH.

x11vnc listens on a Unix socket inside /run/user/<uid> (mode 0700), with every
TCP listener turned off, and the portal reaches it with an SSH streamlocal
channel. Nothing new is exposed on the network and no other local user can
connect, so the VNC server itself needs no password.

Verified against x11vnc 0.9.16 / libvncserver 0.9.14 (Mint 22, Ubuntu 24.04):
`-rfbport 0` still leaves a passwordless IPv6 listener on [::]:5900, and
neither `-no6` nor `-noipv6` removes it. `-rfbportv6 0`, a libvncserver
option missing from the x11vnc man page, is what closes it.
"""

from __future__ import annotations

import asyncio
import shlex
from collections import defaultdict

import asyncssh

from .ssh import Connections, HostUnavailable

# Runs under the remote user's /bin/sh with the X display as $1. Prints the
# socket path on success; on failure prints a short reason to stderr.
START_SCRIPT = r"""
set -u
display="$1"
num="${display#:}"; num="${num%%.*}"
runtime="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
dir="$runtime/ssh-portal"
sock="$dir/vnc-$num.sock"
pidfile="$dir/vnc-$num.pid"
log="$dir/vnc-$num.log"

umask 077
mkdir -p "$dir" && chmod 700 "$dir" || exit 2

if [ -S "$sock" ] && [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$sock"
    exit 0
fi

if ! command -v x11vnc >/dev/null 2>&1; then
    echo "x11vnc is not installed. Run on that machine: sudo apt install x11vnc" >&2
    exit 3
fi

# LightDM on Mint keeps the session's cookie under /run/user/<uid>/gdm; fall
# back to ~/.Xauthority, then to x11vnc's own search.
auth=guess
for f in "$runtime/gdm/Xauthority" "$HOME/.Xauthority"; do
    if [ -r "$f" ]; then auth="$f"; break; fi
done

rm -f "$sock" "$pidfile"
setsid x11vnc -display "$display" -auth "$auth" \
    -rfbport 0 -rfbportv6 0 -noipv6 -unixsock "$sock" \
    -shared -forever -nopw -repeat -noxdamage -xrandr -nowf \
    </dev/null >"$log" 2>&1 &
echo $! >"$pidfile"

explain() {
    reason=$(grep -iE 'fail|error|unable|cannot|refused|no such' "$log" | head -n 4)
    [ -n "$reason" ] || reason=$(tail -n 6 "$log")
    printf '%s\n%s\n' "$1" "$reason" >&2
    exit 4
}

i=0
while [ ! -S "$sock" ]; do
    kill -0 "$(cat "$pidfile")" 2>/dev/null ||
        explain "x11vnc exited. Is someone logged in to the desktop on display $display?"
    i=$((i + 1))
    [ "$i" -le 100 ] || explain "x11vnc did not open its socket within 10 seconds."
    sleep 0.1
done
echo "$sock"
"""


class ScreenUnavailable(Exception):
    """The desktop on a host can't be shown. The message is shown in the UI as-is."""


class Screens:
    def __init__(self, connections: Connections) -> None:
        self._connections = connections
        self._sockets: dict[str, str] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def forget(self, name: str) -> None:
        self._sockets.pop(name, None)

    async def ensure(self, name: str, *, fresh: bool = False) -> str:
        """Return the remote socket path, starting x11vnc if it isn't running."""
        async with self._locks[name]:
            if not fresh and name in self._sockets:
                return self._sockets[name]
            host = self._connections.hosts[name]
            try:
                conn = await self._connections.get(name)
                result = await conn.run(
                    f"sh -s -- {shlex.quote(host.display)}",
                    input=START_SCRIPT,
                    check=False,
                    timeout=20,
                )
            except HostUnavailable as exc:
                raise ScreenUnavailable(str(exc)) from exc
            except (OSError, asyncssh.Error) as exc:
                raise ScreenUnavailable(f"Couldn't start x11vnc: {exc}") from exc
            if result.exit_status != 0:
                reason = str(result.stderr).strip() or f"exit status {result.exit_status}"
                raise ScreenUnavailable(reason)
            path = str(result.stdout).strip()
            self._sockets[name] = path
            return path

    async def open(self, name: str) -> tuple[asyncssh.SSHReader[bytes], asyncssh.SSHWriter[bytes]]:
        """Open a raw RFB byte stream to the host's desktop."""
        path = await self.ensure(name)
        conn = await self._connections.get(name)
        try:
            return await conn.open_unix_connection(path)
        except asyncssh.ChannelOpenError:
            # x11vnc went away (logout, crash, reboot): start it again once.
            path = await self.ensure(name, fresh=True)
            conn = await self._connections.get(name)
            try:
                return await conn.open_unix_connection(path)
            except asyncssh.ChannelOpenError as exc:
                raise ScreenUnavailable(f"Couldn't reach x11vnc: {exc.reason}") from exc
