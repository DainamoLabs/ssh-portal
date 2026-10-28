#!/usr/bin/env bash
# Get this machine ready to be seen and controlled by ssh-portal.
#
# Run it on each machine you want to control, from the account that logs in
# to its desktop:
#
#     sudo bash setup.sh
#
# It installs what's needed and asks two questions. Then, in ssh-portal on
# your main computer, click "Add machine": this machine is listed there. Once
# your key arrives, setup.sh finishes on its own.
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
        -h|--help) sed -n '2,/^[^#]/s/^# \{0,1\}//p' "$0"; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || die "run it with sudo: sudo bash setup.sh"
user="${SUDO_USER:-}"
[ -n "$user" ] && [ "$user" != root ] || die "run it with sudo from the account that logs in to this desktop, not from a root shell"
home=$(getent passwd "$user" | cut -d: -f6)
keys="$home/.ssh/authorized_keys"
lock_conf=/etc/ssh/sshd_config.d/10-ssh-portal.conf
announcement=/etc/avahi/services/ssh-portal.service

installed() {
    [ "$(dpkg-query -W -f='${db:Status-Status}\n' "$@" 2>/dev/null | grep -c '^installed$')" -eq $# ]
}

install_packages() {
    if ! installed openssh-server x11vnc; then
        say "Installing openssh-server and x11vnc. This can take a minute..."
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server x11vnc >/dev/null
    fi
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

# While setup.sh waits, announce this machine over mDNS so "Add machine" in
# ssh-portal lists it. avahi-daemon (on by default in Mint) notices the file
# appearing and disappearing by itself.
announce() {
    [ -d /etc/avahi/services ] && systemctl is-active --quiet avahi-daemon 2>/dev/null || return 1
    local port
    port=$(sshd -T 2>/dev/null | awk '$1 == "port" { print $2; exit }')
    trap 'rm -f "$announcement"' EXIT
    cat >"$announcement" <<EOF
<?xml version="1.0" standalone="no"?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- Written by ssh-portal's setup.sh while it waits to be added. It removes this file when it's done. -->
<service-group>
  <name replace-wildcards="yes">%h</name>
  <service>
    <type>_ssh-portal._tcp</type>
    <port>${port:-22}</port>
    <txt-record>user=$user</txt-record>
  </service>
</service-group>
EOF
}

show_add_details() {
    local listed=$1
    say ""
    say "${bold}This machine is ready to add.${reset}"
    say ""
    if [ "$listed" = yes ]; then
        say "In ssh-portal on your main computer, click \"Add machine\". This machine is"
        say "listed there as ${bold}$(hostname)${reset}. If it isn't, enter its address instead:"
    else
        say "In ssh-portal on your main computer, click \"Add machine\" and enter:"
    fi
    say ""
    say "    Address    $(lan_address)"
    say "    User       $user"
    say ""
    say "When ssh-portal shows a fingerprint, it must be exactly:"
    say ""
    say "    ${bold}$(host_fingerprint)${reset}"
    say ""
    say "If it's different, don't continue: you'd be connecting to something else."
    say ""
}

key_lines() {
    [ -f "$keys" ] && grep -vE '^[[:space:]]*(#|$)' "$keys" | sort -u || true
}

wait_for_key() {
    local before now added logged_in
    # ssh-portal leaves this behind when it logs in with your key while adding
    # the machine (in the per-login runtime directory, so it's gone at logout).
    logged_in="/run/user/$(id -u "$user")/ssh-portal/key-login"
    rm -f "$logged_in"
    before=$(key_lines)
    say "Waiting for ssh-portal to add this machine... ${dim}(press Enter to stop waiting)${reset}"
    while :; do
        if [ -e "$logged_in" ]; then
            say ""
            say "${bold}ssh-portal logged in with your key.${reset}"
            return
        fi
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

    local conf=$lock_conf
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

    # Both questions come first, so once ssh-portal has added this machine
    # there's nothing left to do here.
    local want_lock=no want_autologin=no listed=no
    say ""
    if [ -f "$lock_conf" ]; then
        say "Password logins are already off."
    elif ask "Turn off password logins once your key is installed? (recommended)" y; then
        want_lock=yes
    fi
    if [ -d /etc/lightdm ] && ask "Will this machine run without a monitor and keyboard?" n; then
        want_autologin=yes
    fi

    announce && listed=yes
    show_add_details "$listed"

    local later=""
    [ "$want_lock" = yes ] && later="After adding this machine in ssh-portal, finish with: sudo bash setup.sh --lock"
    trap 'say ""; say "Stopped. $later"; exit 130' INT
    wait_for_key
    trap - INT
    rm -f "$announcement"
    say ""

    if [ "$want_lock" = yes ]; then
        if [ -s "$keys" ]; then
            lock_down
        else
            say "No key is installed for $user yet, so password logins stay on."
            say "$later"
        fi
    fi
    if [ "$want_autologin" = yes ]; then
        say ""
        enable_autologin
    fi
    say ""
    if [ -s "$keys" ]; then
        say "${bold}Done.${reset} You can see and control $(hostname) from ssh-portal now."
    else
        say "${bold}Done for now.${reset} Add this machine in ssh-portal to see and control it."
    fi
}

if [ "$lock" -eq 0 ] && [ -z "$allow_from" ] && [ "$autologin" -eq 0 ]; then
    guided
    exit 0
fi
[ "$lock" -eq 1 ] && lock_down
[ -n "$allow_from" ] && firewall
[ "$autologin" -eq 1 ] && enable_autologin
exit 0
