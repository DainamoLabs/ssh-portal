"""Stands in for x11vnc: records its arguments, then greets like an RFB server on -unixsock."""

import json
import os
import socket
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_X11VNC_ARGS"], "w") as f:
    json.dump(args, f)

if os.environ.get("FAKE_X11VNC_FAIL"):
    print("*** XOpenDisplay failed (:0)", file=sys.stderr)
    sys.exit(1)

server = socket.socket(socket.AF_UNIX)
server.bind(args[args.index("-unixsock") + 1])
server.listen()
while True:
    conn, _ = server.accept()
    with conn:
        conn.sendall(b"RFB 003.008\n")
        data = conn.recv(1024)
        conn.sendall(b"echo:" + data)
