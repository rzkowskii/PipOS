#!/bin/sh
# Assembles the disk image. Runs from the Buildroot top directory.
set -eu

BOARD_DIR="$(dirname "$0")"
# Buildroot passes BR2_ROOTFS_POST_SCRIPT_ARGS after the images directory.
BEINGOS_VARIANT="${2:-lab}"
. "$BOARD_DIR/slots.sh"

boot="$BINARIES_DIR/efi-part/EFI/BOOT"
slot_ids "$BOARD_DIR/grub.cfg" | boot_console > "$boot/grub.cfg"
slot_ids "$BOARD_DIR/genimage.cfg" > "$BINARIES_DIR/genimage.cfg"
"$HOST_DIR/bin/grub-editenv" "$boot/grubenv" create
"$HOST_DIR/bin/grub-editenv" "$boot/grubenv" set \
    'ORDER=A B' GOOD=A A_OK=1 A_TRY=0 A_LEFT=0 B_OK=1 B_TRY=0 B_LEFT=0

# Stage the qualified signed packs in Buildroot output. Frozen source inputs
# may be read-only. Both factory and Try take their models from this image.
# Every role is staged (conversation, dream, vad, asr, tts, embeddings,
# speaker; docs/PIP-FUNCTIONS.md 5.2), each with its own signed descriptor.
src="$BR2_EXTERNAL_BEINGOS_PATH/.."
test -x "$TARGET_DIR/usr/bin/openssl" && test -f "$TARGET_DIR/usr/lib/being/mind/model_pack.py" &&
    cmp "$src/os/package/being/default-model/pack.json" "$TARGET_DIR/usr/share/being/default-model/pack.json" &&
    cmp "$src/os/package/being/30-model-pack.conf" "$TARGET_DIR/usr/lib/systemd/system/being-cortex.service.d/30-model-pack.conf" || {
    echo "Rebuild being and the target OpenSSL CLI before packaging signed model media." >&2
    exit 1
}
# The role descriptors the system checks its packs against. An image that
# ships none still verifies each pack by its signature alone.
shipped="$TARGET_DIR/usr/share/being/model-packs"
for descriptor in "$src"/os/package/being/model-packs/*/pack.json; do
    role=$(basename "$(dirname "$descriptor")")
    if [ -d "$shipped" ]; then
        cmp "$descriptor" "$shipped/$role/pack.json" || {
            echo "The image ships a different $role pack descriptor: rebuild being." >&2
            exit 1
        }
    else
        echo "post-image: WARNING: this image ships no model-packs descriptors; $role is checked by signature only." >&2
    fi
done
bench=
# BEING_PACK_ROLES limits which packs are staged (comma-separated), e.g. to
# keep downloadable release media under 2 GB without the dream model.
case "${BEING_TRY_BENCH:-0}" in
    0) ;;
    1) bench=--bench ;;
    *) echo "BEING_TRY_BENCH must be 0 or 1" >&2; exit 2 ;;
esac
MODELS=$(mktemp -d "$BINARIES_DIR/.beingos-model.XXXXXX")
trap 'rm -rf "$MODELS"' EXIT
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$src" python3 -m lab.package_model \
    --source "$src/out/models" --output "$MODELS" $bench ${BEING_PACK_ROLES:+--roles "$BEING_PACK_ROLES"} \
    --certificate "$BEINGOS_LAB_KEYS/cert.pem" --key "$BEINGOS_LAB_KEYS/key.pem"
rm -f "$BINARIES_DIR/models.ext4" "$BINARIES_DIR/mind.ext4"
# ext4's own metadata grows with the partition: from 2 GB up a 64 MB journal,
# a 256-byte inode for every 16 KB and the backup group descriptors come to
# over 100 MB, more than a fixed 96 MB margin leaves (mkfs -d then fails).
MODELS_MB=$(du -sm "$MODELS" | cut -f1)
MODELS_MB=$(( MODELS_MB + MODELS_MB / 16 + 128 ))
mkfs.ext4 -q -d "$MODELS" -L models -m 0 "$BINARIES_DIR/models.ext4" "${MODELS_MB}M"

# The mind starts blank. A blank mind is how a being is born.
# 1 GB here: the model's saved cache alone runs to tens of megabytes. On the
# laptop this partition is grown to fill the disk.
mkfs.ext4 -q -L mind -m 1 "$BINARIES_DIR/mind.ext4" 1024M

support/scripts/genimage.sh -c "$BINARIES_DIR/genimage.cfg"
