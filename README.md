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
  ssh-agent or `~/.ssh`, just like the `ssh` command. When you add a machine,
  its key is saved only after you've confirmed its fingerprint, and a key
  that differs from the one on file is refused, never replaced.
- Passwords are used once to install your key and never stored. The portal
  stores nothing secret; the machine list holds only names and addresses.
- While `setup.sh` waits to be added, it announces the machine over mDNS, the
  way printers do, so **Add machine** can list it. The portal asks the avahi
  daemon your desktop already runs rather than listening itself. Anything on
  your network could announce itself, so the listing only saves typing an
  address: trust still comes from comparing the fingerprint.

## Setup

Two scripts: `run.sh` runs on your main computer, `setup.sh` on each machine
you want to control.

### On your main computer

```sh
./run.sh
```

The first time, it offers to create an SSH key (the portal logs in with it)
and asks you for a passphrase to protect it. Each time after that it asks for
the passphrase once, if your desktop hasn't unlocked the key already. Then it
opens the portal in your browser. See [Running it](#running-it) for options.

### For each machine you want to control

1. **On that machine**, logged in to its desktop, run:

   ```sh
   sudo bash setup.sh
   ```

   It installs `openssh-server` and `x11vnc` and asks two questions: whether
   to turn off password logins once your key is in (say yes), and whether the
   machine runs without a monitor (see [below](#machines-without-a-monitor)).
   Then it shows a fingerprint and waits. To get the file there, copy it on a
   USB stick, or, if this repository is public, download it:

   ```sh
   wget https://raw.githubusercontent.com/DainamoLabs/ssh-portal/master/setup.sh
   ```

2. **In the portal**, click **Add machine**. The machine is listed there while
   `setup.sh` runs. Click it, then:

   - **Compare the fingerprint** with the one `setup.sh` shows. It must match
     exactly. This is what stops anything else on your network from posing as
     that machine, so do compare it.
   - **Enter your password** for that machine. It's used once to copy your key
     there, and never stored.

   Click **It matches, add machine**. The machine is saved to
   `~/.config/ssh-portal/hosts.toml` and its screen appears right away.

That's it. Back on the machine, `setup.sh` sees your key arrive, turns off
password logins and exits by itself.

If the machine isn't listed (avahi isn't running on it, a firewall blocks
mDNS, or it's on another subnet), choose **Enter its address instead** and
type the address `setup.sh` shows. **Remove**, in a machine's full-screen view, takes it out of the list
again. If a listed machine stops accepting the portal (you reinstalled it,
say), its tile offers **Finish setup**, which runs the same two steps.

Each part of `setup.sh` can also be run on its own:

```sh
sudo bash setup.sh --lock                       # keys only, no root, only you
sudo bash setup.sh --autologin                  # start the desktop at boot
sudo bash setup.sh --allow-from 192.168.1.0/24  # optional: firewall SSH to your LAN
```

### Editing the machine list by hand

The UI only appends and removes `[[host]]` entries, so your own comments in
`~/.config/ssh-portal/hosts.toml` survive. You can also edit it directly
(restart ssh-portal afterwards), and set up keys yourself with `ssh-copy-id`:

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

### Running it

```sh
./run.sh                     # starts on a free port and opens your browser
uv run ssh-portal --check    # test SSH to every machine from the terminal
```

`run.sh` passes extra arguments through, e.g. `./run.sh --config ~/hosts.toml`
or `./run.sh --port 8765` for a fixed port. Started outside a desktop session
(over SSH, or on a text console), it runs its own ssh-agent for as long as
the portal runs.

## Machines without a monitor

The portal shows the desktop of whoever is logged in, so a machine with no
monitor or keyboard needs two things:

1. **A desktop session at boot.** Answer yes when `setup.sh` asks, or run
   `sudo bash setup.sh --autologin`. It sets LightDM to log you in
   automatically. The trade-off is that anyone at that
   machine's keyboard gets your session.
2. **A display to draw on.** With nothing plugged in, many graphics drivers
   start X at a small fallback resolution or with no screen at all. A
   **dummy HDMI plug** (a few dollars) is the reliable fix: the machine thinks
   a 1080p monitor is attached and keeps full graphics acceleration.

## Troubleshooting

Every error in the UI says what to do. The common ones:

| Message | Fix |
| --- | --- |
| Finish setting up *machine* | click **Finish setup** on its tile: compare the fingerprint, enter its password once |
| Connection refused | run `setup.sh` on that machine (openssh-server isn't running) |
| Machine isn't listed in Add machine | use **Enter its address instead** with the address `setup.sh` shows |
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
