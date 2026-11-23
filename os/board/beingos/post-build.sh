#!/bin/sh
# Runs after the root filesystem tree is assembled, before images are made.
set -eu

BOARD_DIR="$(dirname "$0")"
# Buildroot passes BR2_ROOTFS_POST_SCRIPT_ARGS after the target directory.
BEINGOS_VARIANT="${2:-lab}"
. "$BOARD_DIR/slots.sh"

if ! find "$TARGET_DIR/usr/lib" -path '*/lib-dynload/_ssl.*.so' -print -quit | grep -q .; then
    echo "The target Python SSL extension is required. Reconfigure the python3 package before packaging PipOS." >&2
    exit 1
fi

# Each slot supplies its own kernel and system image.
install -D -m 0644 "$BINARIES_DIR/bzImage" "$TARGET_DIR/boot/bzImage"

mkdir -p "$TARGET_DIR/mind" "$TARGET_DIR/models" "$TARGET_DIR/bootstate" \
    "$TARGET_DIR/var" "$TARGET_DIR/mnt/updates" "$TARGET_DIR/etc/rauc"
fstab="$TARGET_DIR/etc/fstab"
awk '$2 != "/" && $2 != "/var" && $2 != "/mind" && $2 != "/models" && $2 != "/bootstate"' "$fstab" > "$fstab.new"
cat >> "$fstab.new" <<'EOF'
/dev/root / squashfs ro 0 0
tmpfs /var tmpfs mode=0755,nosuid,nodev 0 0
LABEL=mind /mind ext4 rw,noatime,nodev,nosuid,noexec,discard 0 2
LABEL=models /models ext4 ro,noatime,nodev,nosuid,noexec 0 2
PARTLABEL=esp /bootstate vfat rw,umask=0077,nodev,nosuid,noexec 0 2
EOF
mv "$fstab.new" "$fstab"

slot_ids "$BOARD_DIR/rauc.conf" > "$TARGET_DIR/etc/rauc/system.conf"
mkdir -p "$TARGET_DIR/usr/share/being"
sed 's/^compatible=beingos-lab-v1$/compatible=beingos-installed-v1/' "$BOARD_DIR/rauc.conf" \
    > "$TARGET_DIR/usr/share/being/installed-rauc.conf"
boot_console < "$BOARD_DIR/grub.cfg" > "$TARGET_DIR/usr/share/being/installed-grub.cfg"
chmod 0644 "$TARGET_DIR/usr/share/being/installed-grub.cfg"
install -D -m 0644 "$BINARIES_DIR/efi-part/EFI/BOOT/bootx64.efi" "$TARGET_DIR/usr/share/being/install/bootx64.efi"
install -D -m 0644 "$BEINGOS_LAB_KEYS/cert.pem" "$TARGET_DIR/etc/rauc/keyring.pem"
install -D -m 0755 "$BOARD_DIR/being-update-ready" "$TARGET_DIR/usr/libexec/being-update-ready"
install -D -m 0644 "$BOARD_DIR/watchdog.conf" "$TARGET_DIR/etc/systemd/system.conf.d/50-being-watchdog.conf"

# Only the owner-provisioned PipOS listeners are allowed to start.
ln -sfn /dev/null "$TARGET_DIR/etc/systemd/system/sshd.service"
if [ "$BEINGOS_VARIANT" = laptop ]; then
    # No login prompt anywhere on the laptop, on any terminal. Root is locked too.
    for unit in getty@ serial-getty@ autovt@ console-getty container-getty@; do
        ln -sfn /dev/null "$TARGET_DIR/etc/systemd/system/$unit.service"
    done
fi
python3 "$BOARD_DIR/build-id.py" "$TARGET_DIR"
