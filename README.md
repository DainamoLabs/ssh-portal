# ssh-portal

See and control the Linux machines on your LAN from one browser tab, instead
of keeping a monitor on each one.

- **Screens**: a live wall of every machine's desktop. Click one to take over
  its mouse and keyboard.
- **Terminals**: a shell on any machine, in tabs.
- **Run command**: run the same command on several machines at once and
  compare the results.

Built for Linux Mint / Cinnamon on X11 (anything with X11 and LightDM should
work). Wayland desktops are not supported.

## How it works

```
 your browser ──> ssh-portal (127.0.0.1 only) ══ SSH ══> each machine
                                                          ├─ shell / commands
                                                          └─ x11vnc on a private Unix socket
```

SSH is the only way in. The portal keeps one SSH connection per machine and
carries everything over it:

- Terminals and commands are ordinary SSH sessions.
- Screens come from x11vnc, which the portal starts on demand. It listens on
  a Unix socket in `/run/user/<uid>/` (readable only by you) with every TCP
  port turned off, and is reached through SSH socket forwarding. No VNC port
  is ever open on your network.
- The web UI listens on 127.0.0.1 only. Each run prints a one-time link; the
  secret in it becomes a SameSite=Strict cookie. Requests with any other Host
  header, and WebSockets from any other origin, are refused, so web pages you
  visit can't reach the portal.
- Host keys are checked against `~/.ssh/known_hosts` and your key comes from
  ssh-agent or `~/.ssh`, just like the `ssh` command. Nothing is stored by
  the portal.

## Setup

### 1. On each machine you want to control

Copy `scripts/setup-target.sh` over and run it from the account that logs in
to that machine's desktop:

```sh
sudo ./setup-target.sh
```

It installs `openssh-server` and `x11vnc`, prints the machine's SSH host key
fingerprint, and tells you what to run next.

### 2. On the machine you'll run ssh-portal from

```sh
ssh-keygen -t ed25519            # once; use a passphrase, ssh-agent remembers it
ssh-copy-id you@192.168.1.20     # check the fingerprint matches step 1
ssh you@192.168.1.20 true        # should not ask for a password
```

### 3. Back on each machine: turn off password logins

```sh
sudo ./setup-target.sh --lock                       # keys only, no root, only you
sudo ./setup-target.sh --allow-from 192.168.1.0/24  # optional: firewall SSH to your LAN
```

### 4. List your machines

`~/.config/ssh-portal/hosts.toml`:

```toml
[[host]]
name = "office"
address = "192.168.1.20"   # IP, hostname, or an alias from ~/.ssh/config
user = "you"               # the account logged in to that desktop

[[host]]
name = "media-pc"
address = "media.lan"
user = "you"
# port = 22
# display = ":0"
```

### 5. Run it

```sh
./run.sh                     # starts on a free port and opens your browser
uv run ssh-portal --check    # or first, test SSH to every machine
```

`run.sh` passes extra arguments through, e.g. `./run.sh --config ~/hosts.toml`
or `./run.sh --port 8765` for a fixed port.

## Machines without a monitor

The portal shows the desktop of whoever is logged in, so a machine with no
monitor or keyboard needs two things:

1. **A desktop session at boot.** `sudo ./setup-target.sh --autologin` sets
   LightDM to log you in automatically. The trade-off is that anyone at that
   machine's keyboard gets your session.
2. **A display to draw on.** With nothing plugged in, many graphics drivers
   start X at a small fallback resolution or with no screen at all. A
   **dummy HDMI plug** (a few dollars) is the reliable fix: the machine thinks
   a 1080p monitor is attached and keeps full graphics acceleration.

## Troubleshooting

Every error in the UI says what to do. The common ones:

| Message | Fix |
| --- | --- |
| Host key not trusted yet | `ssh you@machine` once in a terminal and accept the fingerprint |
| Key not accepted | `ssh-copy-id you@machine`, and `ssh-add` if your key has a passphrase |
| Connection refused | run `setup-target.sh` on that machine (openssh-server isn't running) |
| x11vnc is not installed | `sudo apt install x11vnc` on that machine |
| x11vnc exited. Is someone logged in…? | log in on that machine, or use `--autologin` |

x11vnc's log on each machine is at `/run/user/<uid>/ssh-portal/vnc-0.log`.

## Development

```sh
uv sync
uv run pytest                 # includes end-to-end tests against a throwaway SSH server
./scripts/fetch-vendor.sh     # re-download the pinned noVNC, xterm.js and fonts
```

The browser libraries live in `src/ssh_portal/static/vendor/` so the UI never
loads code from the internet. noVNC is MPL-2.0, xterm.js MIT, Barlow OFL-1.1;
their licenses are next to them.
