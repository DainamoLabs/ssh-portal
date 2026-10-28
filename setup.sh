#!/usr/bin/env bash
# Get this machine ready to be seen and controlled by ssh-portal.
#
# Run it on each machine you want to control, from the account that logs in
# to its desktop:
#
#     sudo bash setup.sh
#
# It installs what's needed, shows what to enter in "Add machine" on your
# main computer, waits for your key to arrive, then offers to turn off
# password logins.
#
# Single steps, for later or for scripting:
#
#     sudo bash setup.sh --lock          turn off password logins (keys only)
#     sudo bash setup.sh --autologin     start the desktop at boot, for a
#                                        machine with no monitor or keyboard
#     sudo bash setup.sh --allow-from 192.168.1.0/24
#                                        only accept SSH from your home network
set -euo pipefail

if [ -t 1 ]; then
    bold=$(tput bold 2>/dev/null || true)
    dim=$(tput dim 2>/dev/null || true)
    reset=$(tput sgr0 2>/dev/null || true)
else
    bold="" dim="" reset=""
fi

die() { echo "setup.sh: $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

# ask "Question?" y|n  -> succeeds on yes; the second argument is the default.
ask() {
    local hint reply
    if [ "$2" = y ]; then hint="[Y/n]"; else hint="[y/N]"; fi
    read -r -p "$1 $hint " reply || reply=""
    [[ "${reply:-$2}" =~ ^[Yy] ]]
}

lock=0
allow_from=""
autologin=0
while [ $# -gt 0 ]; do
    case "$1" in
        --lock) lock=1 ;;
        --allow-from) allow_from="${2:-}"; [ -n "$allow_from" ] || die "--allow-from needs a subnet, like 192.168.1.0/24"; shift ;;
        --autologin) autologin=1 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || die "run it with sudo: sudo bash setup.sh"
user="${SUDO_USER:-}"
[ -n "$user" ] && [ "$user" != root ] || die "run it with sudo from the account that logs in to this desktop, not from a root shell"
home=$(getent passwd "$user" | cut -d: -f6)
keys="$home/.ssh/authorized_keys"

install_packages() {
    say "Installing openssh-server and x11vnc. This can take a minute..."
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server x11vnc >/dev/null
    # Mint 22 / Ubuntu 24.04 start sshd on demand through ssh.socket.
    if ! systemctl is-active --quiet ssh.service && ! systemctl is-active --quiet ssh.socket; then
        systemctl enable --now ssh.socket 2>/dev/null || systemctl enable --now ssh.service
    fi
}

lan_address() {
    local addr
    # The address this machine would use to reach the internet, i.e. its LAN address.
    addr=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')
    [ -n "$addr" ] || addr=$(hostname -I 2>/dev/null | awk '{print $1}')
    printf '%s' "${addr:-unknown}"
}

host_fingerprint() {
    # ssh-portal asks for the ED25519 key when there's a choice.
    if [ -f /etc/ssh/ssh_host_ed25519_key.pub ]; then
        ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}'
    else
        for key in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$key"; done
    fi
}

show_add_details() {
    local name addr
    name=$(hostname)
    addr=$(lan_address)
    say ""
    say "${bold}This machine is ready to add.${reset}"
    say ""
    say "In ssh-portal on your main computer, click \"Add machine\" and enter:"
    say ""
    say "    Name       $name   ${dim}(or anything you like)${reset}"
    say "    Address    $addr"
    if systemctl is-active --quiet avahi-daemon 2>/dev/null; then
        say "               ${dim}or $name.local, which keeps working if the address changes${reset}"
    fi
    say "    User       $user"
    say ""
    say "When it asks you to confirm the machine, the fingerprint must be exactly:"
    say ""
    say "    ${bold}$(host_fingerprint)${reset}"
    say ""
    say "If it's different, don't trust it: you'd be connecting to something else."
    say ""
}

key_lines() {
    [ -f "$keys" ] && grep -vE '^[[:space:]]*(#|$)' "$keys" | sort -u || true
}

wait_for_key() {
    local before now added
    before=$(key_lines)
    say "Waiting for your key to arrive... ${dim}(press Enter if it's already installed)${reset}"
    while :; do
        now=$(key_lines)
        added=$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$now") | grep -v '^$' || true)
        if [ -n "$added" ]; then
            say ""
            say "${bold}Your key arrived:${reset}"
            printf '%s\n' "$added" | ssh-keygen -lf - | sed 's/^/    /'
            return
        fi
        if read -r -t 2 _; then
            return
        fi
    done
}

lock_down() {
    [ -s "$keys" ] || die "no keys are installed for $user yet, so --lock would lock you out. Add this machine in ssh-portal first."

    local conf=/etc/ssh/sshd_config.d/10-ssh-portal.conf
    cat >"$conf" <<EOF
# Written by ssh-portal's setup.sh. Delete this file to undo.
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
    say "Password logins are off. Only $user can log in over SSH, and only with a key."
    say "${dim}To undo: sudo rm $conf && sudo systemctl reload ssh${reset}"
}

firewall() {
    command -v ufw >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw >/dev/null
    ufw allow from "$allow_from" to any port 22 proto tcp comment "ssh-portal" >/dev/null
    if ! ufw status | grep -q "Status: active"; then
        say "Turning on ufw. Other incoming connections (file sharing, etc.) will be blocked"
        say "unless you allow them too, e.g. sudo ufw allow from $allow_from to any port 445"
        ufw default deny incoming >/dev/null
        ufw default allow outgoing >/dev/null
        ufw --force enable >/dev/null
    fi
    say "SSH is reachable only from $allow_from:"
    ufw status | grep -E "^22|ssh-portal" || true
    if ufw status | grep -qE "^(22|OpenSSH)[^ ]* +ALLOW +Anywhere"; then
        say "Note: an older rule still allows SSH from anywhere. Remove it with: sudo ufw status numbered"
    fi
}

enable_autologin() {
    [ -d /etc/lightdm ] || die "LightDM not found; --autologin only knows how to configure LightDM"
    mkdir -p /etc/lightdm/lightdm.conf.d
    cat >/etc/lightdm/lightdm.conf.d/70-ssh-portal-autologin.conf <<EOF
# Written by ssh-portal's setup.sh. Delete this file to undo.
[Seat:*]
autologin-user=$user
autologin-user-timeout=0
EOF
    say "From the next boot, $user's desktop starts without a login screen, so"
    say "ssh-portal can show it. Anyone at this machine's keyboard gets that session."
    say "With no monitor plugged in, many machines start with a tiny or blank screen;"
    say "a dummy HDMI plug (a few dollars) fixes that."
}

guided() {
    [ -t 0 ] || die "run it in a terminal, or use --lock / --autologin / --allow-from"
    install_packages
    show_add_details

    trap 'say ""; say "Stopped. After adding this machine in ssh-portal, finish with: sudo bash setup.sh --lock"; exit 130' INT
    wait_for_key
    trap - INT
    say ""

    if [ ! -s "$keys" ]; then
        say "No key is installed for $user yet, so password logins stay on."
        say "After adding this machine in ssh-portal, finish with: sudo bash setup.sh --lock"
    elif ask "Turn off password logins now, so only your key can log in?" y; then
        lock_down
    else
        say "Skipped. To do it later: sudo bash setup.sh --lock"
    fi

    if [ -d /etc/lightdm ]; then
        say ""
        if ask "Will this machine run without a monitor and keyboard?" n; then
            enable_autologin
        fi
    fi
    say ""
    say "${bold}Done.${reset} You can see and control $(hostname) from ssh-portal now."
}

if [ "$lock" -eq 0 ] && [ -z "$allow_from" ] && [ "$autologin" -eq 0 ]; then
    guided
    exit 0
fi
[ "$lock" -eq 1 ] && lock_down
[ -n "$allow_from" ] && firewall
[ "$autologin" -eq 1 ] && enable_autologin
exit 0
