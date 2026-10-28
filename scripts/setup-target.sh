#!/usr/bin/env bash
# Prepare a Linux Mint machine to be seen and controlled by ssh-portal.
#
# Run this ON THE MACHINE YOU WANT TO CONTROL, with sudo, from the account
# that logs in to its desktop:
#
#   sudo ./setup-target.sh                   install openssh-server and x11vnc,
#                                            print what to do next
#   sudo ./setup-target.sh --lock            once your key works: turn off
#                                            password logins
#
# Optional, any time:
#   sudo ./setup-target.sh --allow-from 192.168.1.0/24
#                                            firewall SSH to your home subnet
#   sudo ./setup-target.sh --autologin       log the desktop in at boot, for
#                                            machines with no keyboard attached
set -euo pipefail

die() { echo "setup-target: $*" >&2; exit 1; }

lock=0
allow_from=""
autologin=0
while [ $# -gt 0 ]; do
    case "$1" in
        --lock) lock=1 ;;
        --allow-from) allow_from="${2:-}"; [ -n "$allow_from" ] || die "--allow-from needs a subnet, like 192.168.1.0/24"; shift ;;
        --autologin) autologin=1 ;;
        -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || die "run with sudo"
user="${SUDO_USER:-}"
[ -n "$user" ] && [ "$user" != root ] || die "run with sudo from your normal account, not from a root shell"
home=$(getent passwd "$user" | cut -d: -f6)

install_packages() {
    echo "Installing openssh-server and x11vnc..."
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server x11vnc >/dev/null
    # Mint 22 / Ubuntu 24.04 start sshd on demand through ssh.socket.
    if ! systemctl is-active --quiet ssh.service && ! systemctl is-active --quiet ssh.socket; then
        systemctl enable --now ssh.socket 2>/dev/null || systemctl enable --now ssh.service
    fi

    local addr
    addr=$(hostname -I | awk '{print $1}')
    echo
    echo "Done. This machine's SSH host key fingerprints:"
    for key in /etc/ssh/ssh_host_*_key.pub; do
        echo "    $(ssh-keygen -lf "$key")"
    done
    cat <<EOF

ssh will show one of these the first time you connect. Check it matches; that
is what proves you're talking to this machine and not something pretending.

Next, on the machine you'll run ssh-portal from:

    ssh-keygen -t ed25519          # only if you don't have a key yet
    ssh-copy-id $user@$addr
    ssh $user@$addr true           # should log in without a password

Then come back here and run:

    sudo $0 --lock

EOF
}

lock_down() {
    local keys="$home/.ssh/authorized_keys"
    [ -s "$keys" ] || die "$keys is empty. Run ssh-copy-id first, or --lock would lock you out."

    local conf=/etc/ssh/sshd_config.d/10-ssh-portal.conf
    cat >"$conf" <<EOF
# Written by ssh-portal's setup-target.sh. Delete this file to undo.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers $user
EOF
    if ! sshd -t; then
        rm -f "$conf"
        die "sshd rejected the new settings, so nothing was changed"
    fi
    if systemctl is-active --quiet ssh.service; then
        systemctl reload ssh.service
    fi
    echo "Password logins are off. Only $user can log in over SSH, and only with a key."
    echo "To undo: sudo rm $conf && sudo systemctl reload ssh"
}

firewall() {
    command -v ufw >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw >/dev/null
    ufw allow from "$allow_from" to any port 22 proto tcp comment "ssh-portal" >/dev/null
    if ! ufw status | grep -q "Status: active"; then
        echo "Turning on ufw. Other incoming connections (file sharing, etc.) will be blocked"
        echo "unless you allow them too, e.g. sudo ufw allow from $allow_from to any port 445"
        ufw default deny incoming >/dev/null
        ufw default allow outgoing >/dev/null
        ufw --force enable >/dev/null
    fi
    echo "SSH is reachable only from $allow_from:"
    ufw status | grep -E "^22|ssh-portal" || true
    if ufw status | grep -qE "^(22|OpenSSH)[^ ]* +ALLOW +Anywhere"; then
        echo "Note: an older rule still allows SSH from anywhere. Remove it with: sudo ufw status numbered"
    fi
}

enable_autologin() {
    [ -d /etc/lightdm ] || die "LightDM not found; --autologin only knows how to configure LightDM"
    mkdir -p /etc/lightdm/lightdm.conf.d
    cat >/etc/lightdm/lightdm.conf.d/70-ssh-portal-autologin.conf <<EOF
# Written by ssh-portal's setup-target.sh. Delete this file to undo.
[Seat:*]
autologin-user=$user
autologin-user-timeout=0
EOF
    echo "From the next boot, $user's desktop starts without a login screen, so"
    echo "ssh-portal can show it. Anyone at this machine's keyboard gets that session."
}

if [ "$lock" -eq 0 ] && [ -z "$allow_from" ] && [ "$autologin" -eq 0 ]; then
    install_packages
fi
[ "$lock" -eq 1 ] && lock_down
[ -n "$allow_from" ] && firewall
[ "$autologin" -eq 1 ] && enable_autologin
exit 0
