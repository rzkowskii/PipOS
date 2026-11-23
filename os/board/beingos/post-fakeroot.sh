#!/bin/sh
# Runs inside Buildroot's fakeroot, after file ownership and modes are applied
# and just before the root filesystem image is made. $1 is the target tree.
#
# PipOS needs no setuid or setgid program: every organ runs from systemd as
# root or as its own user, and no one logs in. A setuid binary is the classic
# way out of a cage, so Pip's workshop (docs/design/voice-apps.md section 5)
# must not find one. BusyBox is setuid by default in Buildroot; this removes
# that bit and every other, and lists what it removed in the build log.
set -eu

target=${1:?usage: post-fakeroot.sh TARGET_DIR}
removed=$(find "$target" -xdev -type f \( -perm -4000 -o -perm -2000 \) -print)
if [ -n "$removed" ]; then
    printf '%s\n' "$removed" | while IFS= read -r path; do
        chmod u-s,g-s "$path"
        echo "post-fakeroot: removed setuid/setgid from /${path#"$target"/}"
    done
fi
left=$(find "$target" -xdev -type f \( -perm -4000 -o -perm -2000 \) -print)
if [ -n "$left" ]; then
    echo "post-fakeroot: setuid or setgid files remain:" >&2
    printf '%s\n' "$left" >&2
    exit 1
fi
