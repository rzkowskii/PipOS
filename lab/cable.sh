#!/bin/bash
# The Mac's side of the cable development link (docs/LAB.md, "Cable
# development build"). Pip, booted from a BEING_LAPTOP_CABLE=1 Try stick,
# answers SSH on 169.254.77.1 and fe80::77:1 over an Ethernet cable; the Mac
# keeps its own self-assigned 169.254.x address on that port.
#
#   lab/cable.sh ssh-config     print the Host pip / pip4 blocks for ~/.ssh/config,
#                               and write the host key pin ~/.ssh/known_hosts_pipos
#   lab/cable.sh ping           ping Pip over IPv6 and IPv4 on the cable port
#   lab/cable.sh ssh [CMD...]   a root shell on Pip (or run CMD there)
#   lab/cable.sh push [--no-restart]
#                               copy this tree's mind/*.py onto Pip's writable
#                               copy of its code, then restart the running
#                               organs that run mind code: being-mind,
#                               being-voice, being-hands, being-eyes,
#                               being-local-screen and being-wifi (not
#                               being-usb-logs, which holds the PIPLOGS mount)
#   lab/cable.sh pull-logs      copy /run/being-diag and this boot's PIPLOGS
#                               folder to out/hardware-logs/cable-DATE/
#
# PIPOS_CABLE_IF names the Mac's cable port (default en0). PIPOS_CABLE_HOST
# chooses pip (IPv6, the default) or pip4 (IPv4). PIPOS_CABLE_IDENTITY and
# PIPOS_CABLE_HOST_KEY override the login key and the host key's .pub file.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

PORT_IF=${PIPOS_CABLE_IF:-en0}
HOST=${PIPOS_CABLE_HOST:-pip}
IDENTITY=${PIPOS_CABLE_IDENTITY:-$HOME/.ssh/pipos_cable_ed25519}
HOST_KEY=${PIPOS_CABLE_HOST_KEY:-$HOME/.config/pipos-cable/ssh_host_ed25519_key.pub}
PINS=${PIPOS_CABLE_KNOWN_HOSTS:-$HOME/.ssh/known_hosts_pipos}
ADDRESS4=169.254.77.1
ADDRESS6=fe80::77:1

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

case "$PORT_IF" in
    *[!A-Za-z0-9]*|"") echo "PIPOS_CABLE_IF must be an interface name such as en0" >&2; exit 2 ;;
esac
case "$HOST" in
    pip|pip4) ;;
    *) echo "PIPOS_CABLE_HOST must be pip or pip4" >&2; exit 2 ;;
esac

# The two Host blocks ("%%" is a literal % in ssh_config). Both pin the one
# host key every cable stick carries (HostKeyAlias pip), whichever address
# reaches it.
ssh_config() {
    cat <<EOF
Host pip
  HostName $ADDRESS6%%$PORT_IF
  User root
  IdentityFile $IDENTITY
  IdentitiesOnly yes
  HostKeyAlias pip
  UserKnownHostsFile $PINS
  StrictHostKeyChecking yes
  ServerAliveInterval 15
Host pip4
  HostName $ADDRESS4
  BindInterface $PORT_IF
  User root
  IdentityFile $IDENTITY
  IdentitiesOnly yes
  HostKeyAlias pip
  UserKnownHostsFile $PINS
  StrictHostKeyChecking yes
  ServerAliveInterval 15
EOF
}

# The pin file, from the host key's public half (never the private key).
write_pins() {
    [ -f "$HOST_KEY" ] || { echo "No host key at $HOST_KEY (see docs/LAB.md, Cable development build)." >&2; exit 1; }
    local line
    line=$(head -n 1 "$HOST_KEY")
    case "$line" in
        "ssh-ed25519 "*) ;;
        *) echo "$HOST_KEY is not an Ed25519 public key." >&2; exit 1 ;;
    esac
    mkdir -p "$(dirname "$PINS")"
    (umask 022; printf 'pip,%s,%s %s\n' "$ADDRESS4" "$ADDRESS6" "$(printf '%s\n' "$line" | cut -d' ' -f1,2)" \
        > "$PINS.new")
    mv -f "$PINS.new" "$PINS"
}

# ssh with this script's own configuration, so ~/.ssh/config need not change.
# The temporary file is a global: the EXIT trap runs after run_ssh returns,
# where a local would be unbound (set -u) and the file left behind.
SSH_CONFIG_FILE=
trap 'rm -f "${SSH_CONFIG_FILE:-}"' EXIT
run_ssh() {
    [ -f "$IDENTITY" ] || { echo "No login key at $IDENTITY (see docs/LAB.md, Cable development build)." >&2; exit 1; }
    [ -s "$PINS" ] || write_pins
    [ -n "$SSH_CONFIG_FILE" ] || SSH_CONFIG_FILE=$(mktemp "${TMPDIR:-/tmp}/pipos-cable-ssh.XXXXXX")
    ssh_config > "$SSH_CONFIG_FILE"
    ssh -F "$SSH_CONFIG_FILE" "$@"
}

ssh_pip() {
    run_ssh "$HOST" "$@"
}

cmd=${1:-help}
[ $# -gt 0 ] && shift
case "$cmd" in
    ssh-config)
        write_pins
        echo "Wrote the host key pin to $PINS. Add these blocks to ~/.ssh/config:" >&2
        ssh_config
        ;;
    ping)
        echo "IPv6 $ADDRESS6%$PORT_IF:"
        ping6 -c 3 "$ADDRESS6%$PORT_IF" || echo "No answer over IPv6." >&2
        echo "IPv4 $ADDRESS4 on $PORT_IF:"
        ping -c 3 -b "$PORT_IF" "$ADDRESS4" || echo "No answer over IPv4." >&2
        ;;
    ssh)
        ssh_pip "$@"
        ;;
    push)
        restart=1
        # Every organ that runs mind/ code from /usr/lib/being, except
        # being-usb-logs: it owns the PIPLOGS mount, so restart it by hand.
        organs="being-mind.service being-voice.service being-hands.service being-eyes.service"
        organs="$organs being-local-screen.service being-wifi.service"
        case "${1:-}" in
            "") ;;
            --no-restart) restart=0 ;;
            *) echo "usage: lab/cable.sh push [--no-restart]" >&2; exit 2 ;;
        esac
        # The image installs mind/*.py only (os/package/being/being.mk), so
        # that is what is pushed: plain ustar owned by root, with no macOS
        # metadata files. The workbench's .pyc files check each module's
        # time stamp, so a restart loads what changed.
        files=()
        for path in mind/*.py; do files+=("${path#mind/}"); done
        [ "${#files[@]}" -gt 0 ] || { echo "No mind/*.py here." >&2; exit 1; }
        remote='set -eu
grep -q " /usr/lib/being " /proc/self/mountinfo || {
    echo "The workbench is not mounted on Pip (/usr/lib/being is the read-only image)." >&2; exit 1; }
tar -x -C /usr/lib/being/mind -f -
echo "Pushed $(ls /usr/lib/being/mind/*.py | wc -l) modules."'
        if [ "$restart" = 1 ]; then
            remote="$remote
# try-restart: an organ that is not running stays off.
systemctl try-restart $organs
# Organs that were off show inactive; the push fails only if the mind or the voice is down.
systemctl is-active $organs || systemctl is-active --quiet being-mind.service being-voice.service"
        fi
        COPYFILE_DISABLE=1 tar --format ustar --no-mac-metadata --uid 0 --gid 0 --uname root --gname root \
            -c -f - -C mind "${files[@]}" | ssh_pip "$remote"
        ;;
    pull-logs)
        dest="out/hardware-logs/cable-$(date -u +%Y%m%d-%H%M%S)"
        mkdir -p "$dest"
        # Only what exists: /run/being-diag and this boot's PIPLOGS folder.
        ssh_pip 'set -eu
paths=
[ -d /run/being-diag ] && paths="run/being-diag"
if [ -s /run/being-usb-logs/folder ]; then
    folder=$(head -n 1 /run/being-usb-logs/folder)
    case "$folder" in
        /*) [ -d "$folder" ] && paths="$paths ${folder#/}" ;;
    esac
fi
[ -n "$paths" ] || { echo "Nothing to copy: no /run/being-diag and no PIPLOGS boot folder." >&2; exit 1; }
tar -c -C / -f - $paths' | tar -x -C "$dest" -f -
        echo "Copied to $ROOT/$dest"
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        echo "unknown command: $cmd" >&2
        usage >&2
        exit 2
        ;;
esac
