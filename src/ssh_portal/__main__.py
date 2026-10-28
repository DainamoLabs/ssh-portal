"""Command line entry point: `ssh-portal` serves the UI, `ssh-portal --check` diagnoses hosts."""

from __future__ import annotations

import argparse
import asyncio
import errno
import logging
import secrets
import socket
import sys
import webbrowser
from pathlib import Path

import uvicorn

from .config import EXAMPLE, ConfigError, default_config_path, load_hosts
from .ssh import Connections


async def check(connections: Connections) -> int:
    """Connect to every host and report what, if anything, is in the way."""
    names = list(connections.hosts)
    statuses = await asyncio.gather(*(connections.status(n) for n in names))
    failures = 0
    for name, status in zip(names, statuses):
        host = connections.hosts[name]
        if not status["online"]:
            failures += 1
            print(f"  ✗ {name} ({host.target}): {status['error']}")
            continue
        probe = await connections.run(name, "command -v x11vnc", timeout=10)
        if probe.get("exit_status") == 0:
            print(f"  ✓ {name} ({host.target}): SSH ok, x11vnc installed")
        else:
            print(f"  ! {name} ({host.target}): SSH ok, but x11vnc is missing (sudo apt install x11vnc)")
    await connections.close_all()
    return 1 if failures else 0


def listen(port: int) -> socket.socket:
    """Bind the UI's socket up front: port 0 picks a free one, and the real
    port has to be known before the app is built (link, Host and Origin checks)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        sock.close()
        if exc.errno == errno.EADDRINUSE:
            sys.exit(f"ssh-portal: port {port} is in use. Pick another, or --port 0 for any free port.")
        raise
    # Listen now, so opening the printed link before the server has finished
    # starting waits in the queue instead of being refused.
    sock.listen(128)
    return sock


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ssh-portal",
        description="See and control the Linux machines on your LAN from one browser tab.",
    )
    parser.add_argument("--config", type=Path, default=default_config_path(), help="hosts file (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8765, help="local port for the UI; 0 picks a free one (default: %(default)s)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    parser.add_argument("--check", action="store_true", help="test SSH to every host and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="log SSH and server activity")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        hosts = load_hosts(args.config)
    except ConfigError as exc:
        sys.exit(f"ssh-portal: {exc}")
    if not hosts:
        print(f"No hosts configured yet. Create {args.config} like this:\n\n{EXAMPLE}")
        if args.check:
            sys.exit(1)

    connections = Connections(hosts)
    if args.check:
        sys.exit(asyncio.run(check(connections)))

    # Imported here so `--check` doesn't pay for FastAPI.
    from .web import create_app

    # Loopback only, on purpose: to use the portal from another machine,
    # forward the port over SSH instead of opening it to the network.
    sock = listen(args.port)
    port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    url = f"http://127.0.0.1:{port}/?token={token}"
    app = create_app(
        connections,
        token=token,
        port=port,
        config_path=args.config,
        on_ready=None if args.no_browser else lambda: webbrowser.open(url),
    )
    print(f"ssh-portal: {len(hosts)} host(s) from {args.config}")
    print(f"Open {url}")
    print("Anyone with this link can control your machines. Ctrl+C to stop.", flush=True)
    config = uvicorn.Config(app, log_level="info" if args.verbose else "warning")
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
