#!/bin/bash
# Runs inside the builder. The private key lives in the builder volume.
set -euo pipefail

output="$1"
mode="$2"
destination="${3:-/src/out/images}"
compatible=beingos-lab-v1
case "$mode" in
    bundle|test-media) ;;
    installed-bundle) compatible=beingos-installed-v1 ;;
    *) echo "Unknown update build mode: $mode" >&2; exit 2 ;;
esac
image="$output/images/rootfs.squashfs"
host="$output/host/bin"
export PATH="$host:$output/host/sbin:$PATH"
test -f "$image" || { echo "Build the PipOS image first" >&2; exit 1; }

work=$(mktemp -d "${TMPDIR:-/tmp}/beingos-update.XXXXXX")
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/payload" "$destination"

bundle() {
    cp "$1" "$work/payload/rootfs.img"
    cat > "$work/payload/manifest.raucm" <<EOF
[update]
compatible=$compatible
version=$2

[bundle]
format=verity

[image.rootfs]
filename=rootfs.img
EOF
    rauc bundle --cert="$BEINGOS_LAB_KEYS/cert.pem" --key="$BEINGOS_LAB_KEYS/key.pem" \
        "$work/payload" "$3"
}

if [ "$mode" != test-media ]; then
    name=beingos.raucb
    version=lab
    if [ "$mode" = installed-bundle ]; then
        name=beingos-installed.raucb
        version=installed-lab
        # An earlier factory payload cannot bind mounts on an installed boot.
        unsquashfs -cat "$image" usr/share/being/installed-rauc.conf | grep -qx "compatible=$compatible"
        unsquashfs -cat "$image" usr/lib/systemd/system-generators/being-installed-generator >/dev/null
        unsquashfs -cat "$image" usr/lib/being/mind/install_binding.py >/dev/null
    fi
    bundle "$image" "$version" "$work/$name"
    cp "$work/$name" "$destination/.$name.new"
    mv -f "$destination/.$name.new" "$destination/$name"
else
    mkdir "$work/media"
    bundle "$image" lab-good "$work/media/good.raucb"

    # A signed image that boots but cannot reach a healthy mind. A short
    # deadline keeps the three trial boots bounded in this negative test.
    fakeroot sh -eu -c '
        unsquashfs -no-progress -d "$2/root" "$1"
        mkdir -p "$2/root/etc/systemd/system/being-boot-health.service.d"
        ln -sfn /dev/null "$2/root/etc/systemd/system/being-mind.service"
        printf "[Service]\nEnvironment=BEING_SLOT_DEADLINE=12\n" \
            > "$2/root/etc/systemd/system/being-boot-health.service.d/lab-fault.conf"
        mksquashfs "$2/root" "$2/bad.squashfs" -noappend -no-progress -comp gzip
    ' sh "$image" "$work"
    bundle "$work/bad.squashfs" lab-bad "$work/media/bad.raucb"
    printf 'not a signed bundle\n' > "$work/media/unsigned.raucb"
    size=$(( $(du -sm "$work/media" | cut -f1) + 32 ))
    mkfs.ext4 -q -d "$work/media" -L being-updates -m 0 "$work/media.img" "${size}M"
    cp "$work/media.img" "$destination/beingos-update-fixtures.img"
fi
