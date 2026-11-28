#!/usr/bin/env bash
# Build the installable PipOS ISO for the MacBook from the laptop build.
#
#     lab/make-iso.sh [OUTPUT_DIR]          # default: out/iso
#
# The result is a hybrid image: an ISO 9660 file system with an El Torito
# EFI boot entry, and a GPT whose appended partitions are what a USB stick
# needs. Written to a stick (dd or balenaEtcher) it presents:
#
#   1  ISO9660     the ISO 9660 data: README.txt and BUILD.txt
#   2  try-esp     EFI System (FAT): EFI/BOOT/bootx64.efi and grub.cfg;
#                  the El Torito EFI entry points at this same partition
#   3  try-system  the laptop rootfs.squashfs, read-only
#   4  try-models  the laptop models.ext4 (label being-try-models), read-only
#   5  try-logs    FAT32 PIPLOGS for first-boot diagnostics
#
# These are the partition names and roles of the laptop Try image
# (os/board/beingos/try-image.sh), so the running system, the installer and
# being-usb-logs find their partitions on the stick just as on that image:
# the kernel mounts root=PARTUUID=, the Try generator mounts /models by its
# PARTUUID, and PIPLOGS is the partition named try-logs. xorriso cannot name
# GPT partitions or choose their GUIDs, so sfdisk sets both afterwards; it
# changes only GPT names, GUIDs and attributes, never where anything lies.
#
# Inputs, read only, from the Colima builder volume (BEING_ISO_IMAGES,
# default /work/output/laptop/images): efi-part/EFI/BOOT/bootx64.efi,
# rootfs.squashfs and models.ext4. No Buildroot make runs here. Everything
# runs inside the builder VM, in an image derived from beingos-builder with
# xorriso, mtools, dosfstools and fdisk added; nothing is installed on the Mac.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PROFILE=${BEING_BUILDER_PROFILE:-beingos}
VOLUME=${BEING_BUILDER_VOLUME:-beingos-work}
BASE_IMAGE=beingos-builder
TOOLS_IMAGE=pipos-iso-tools
IMAGES=${BEING_ISO_IMAGES:-/work/output/laptop/images}
HOST=${BEING_ISO_HOST:-/work/output/laptop/host}
OUTPUT=${1:-$ROOT/out/iso}
NAME=${BEING_ISO_NAME:-pipos-macbook}

# A cable development build (BEING_LAPTOP_CABLE=1) or a diagnostic build
# (BEING_LAPTOP_DIAGNOSTIC=1, which keeps a passwordless root console) is never
# made into an installable ISO: refused here by name, and inside by what its
# rootfs holds.
case "$IMAGES/" in
    */laptop-cable/*)
        echo "Refusing: $IMAGES is the cable development build, which never becomes an ISO." >&2
        exit 1 ;;
    */laptop-diagnostic/*)
        echo "Refusing: $IMAGES is the diagnostic build, which never becomes an ISO." >&2
        exit 1 ;;
esac

mkdir -p "$OUTPUT"
OUTPUT=$(cd "$OUTPUT" && pwd)
case "$OUTPUT" in
    "$ROOT"/*) ;;
    *) echo "The output directory must be inside the project (the builder sees only it): $OUTPUT" >&2; exit 2 ;;
esac
RELATIVE=${OUTPUT#"$ROOT"/}

docker_() { docker --context "colima-$PROFILE" "$@"; }

if ! colima status --profile "$PROFILE" >/dev/null 2>&1; then
    echo "The builder VM ($PROFILE) is not running: start it with lab/build.sh up" >&2
    exit 1
fi
if ! docker_ image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
    echo "No $BASE_IMAGE image in the builder VM: run lab/build.sh up first" >&2
    exit 1
fi

# The ISO tools image: rebuilt whenever beingos-builder changes.
base_id=$(docker_ image inspect --format '{{.Id}}' "$BASE_IMAGE")
tools_base=$(docker_ image inspect --format '{{index .Config.Labels "pipos.base"}}' "$TOOLS_IMAGE" 2>/dev/null || true)
if [ "$tools_base" != "$base_id" ]; then
    echo "Building the $TOOLS_IMAGE image..."
    printf '%s\n' \
        "FROM $BASE_IMAGE" \
        "USER root" \
        "RUN apt-get update && apt-get install -y --no-install-recommends xorriso mtools dosfstools fdisk \\" \
        "    && rm -rf /var/lib/apt/lists/*" \
        "LABEL pipos.base=$base_id" \
        "USER builder" |
        docker_ build --quiet -t "$TOOLS_IMAGE" - >/dev/null
fi

# The tree's source id, taken before the ISO is started (lab/source_id.py).
tree_id=$(cd "$ROOT" && python3 -m lab.source_id 2>/dev/null) || tree_id=""
# A new ISO never sits beside the old one's source-id.
rm -f "$OUTPUT/$NAME.iso.source-id" "$OUTPUT/.$NAME.iso.built-from"

# The builder volume is mounted read-only: this script cannot change a build.
docker_ run --rm -i \
    -v "$VOLUME:/work:ro" -v "$ROOT:/src" \
    -e IMAGES="$IMAGES" -e HOST="$HOST" -e OUT="/src/$RELATIVE" -e NAME="$NAME" \
    "$TOOLS_IMAGE" bash -euo pipefail <<'INNER'
board=/src/os/board/beingos
bootloader=$IMAGES/efi-part/EFI/BOOT/bootx64.efi
rootfs=$IMAGES/rootfs.squashfs
models=$IMAGES/models.ext4
for artifact in "$bootloader" "$rootfs" "$models"; do
    if [ ! -s "$artifact" ]; then
        echo "Missing build artifact: $artifact (run lab/build.sh laptop first)" >&2
        exit 1
    fi
done
# Buildroot's own e2fsprogs knows every feature its models.ext4 uses.
export PATH="$HOST/bin:$HOST/sbin:$PATH"

stage=$(mktemp -d /tmp/pipos-iso.XXXXXX)
trap 'rm -rf -- "$stage"' EXIT

# Never an ISO of a rootfs that carries the cable development link.
unsquashfs -l -no-progress "$rootfs" > "$stage/rootfs-listing"
if grep -Eq '^squashfs-root/usr/share/being/cable(/|$)' "$stage/rootfs-listing"; then
    echo "Refusing: this rootfs holds the cable development link (BEING_LAPTOP_CABLE=1); it never becomes an ISO." >&2
    exit 1
fi
if grep -qx 'squashfs-root/usr/share/being/diagnostic' "$stage/rootfs-listing"; then
    echo "Refusing: this rootfs is the diagnostic build (BEING_LAPTOP_DIAGNOSTIC=1); it never becomes an ISO." >&2
    exit 1
fi

uuid() { cat /proc/sys/kernel/random/uuid; }
disk_uuid=$(uuid); esp_uuid=$(uuid); root_uuid=$(uuid); models_uuid=$(uuid); logs_uuid=$(uuid)

# The boot menu, with the laptop kernel arguments from slots.sh.
BEINGOS_VARIANT=laptop
. "$board/slots.sh"
sed -e "s/ISO_ROOT_UUID/$root_uuid/g" -e "s/ISO_MODELS_UUID/$models_uuid/g" "$board/iso-grub.cfg" |
    boot_console > "$stage/grub.cfg"
if grep -Eq 'ISO_[A-Z]+_UUID|BEING_[A-Z]+_ARGS' "$stage/grub.cfg"; then
    echo "grub.cfg still holds a placeholder" >&2
    exit 1
fi
if command -v grub-script-check >/dev/null; then
    grub-script-check "$stage/grub.cfg"
fi

# EFI System partition (31 MiB FAT16; GRUB needs well under 1 MiB). It is
# kept below 32 MiB so that the El Torito entry can state its exact size
# (at most 65535 sectors): firmware that boots the stick through El Torito
# then sees the same start and size as the GPT entry, and GRUB recognises
# the partition as gpt2 instead of the whole disk.
mkfs.vfat -C -F 16 -n PIPOS-EFI "$stage/esp.img" 31744 >/dev/null
mmd -i "$stage/esp.img" ::/EFI ::/EFI/BOOT
mcopy -i "$stage/esp.img" "$bootloader" ::/EFI/BOOT/bootx64.efi
mcopy -i "$stage/esp.img" "$stage/grub.cfg" ::/EFI/BOOT/grub.cfg

# Installed disks use LABEL=models: the Try copy gets its own identity.
cp --sparse=always "$models" "$stage/models.ext4"
tune2fs -L being-try-models -U random "$stage/models.ext4" >/dev/null

# PIPLOGS (128 MiB FAT32) with the same README as the laptop Try stick.
mkfs.vfat -C -F 32 -n PIPLOGS "$stage/logs.img" 131072 >/dev/null
sed 's/$/\r/' "$board/try-logs-README.txt" > "$stage/README.txt"
mcopy -i "$stage/logs.img" "$stage/README.txt" ::/README.txt

# The ISO 9660 side: what a file browser shows if it mounts the ISO itself.
mkdir "$stage/iso"
cat > "$stage/iso/README.txt" <<'EOF'
PipOS for the MacBook Pro (13-inch, Mid 2012)
=============================================

This file is an installable hybrid image. Do not copy it onto a USB stick
as a file: write the whole image to the stick (balenaEtcher, or dd on a
Mac or Linux computer). See docs/ISO.md in the PipOS source.

Then hold the Option key while the MacBook starts and choose "EFI Boot".
Booting from a CD or DVD is not supported.
EOF
# The system's own identity, for BUILD.txt and the lab's source-id sidecar.
image_file() { unsquashfs -cat "$rootfs" "$1" 2>/dev/null | head -n 1 | tr -cd '0-9a-f' || true; }
build_id=$(image_file etc/being/build-id)
source_id=$(image_file etc/being/source-id)
# Without one in the system itself: the id lab/build.sh recorded beside these
# images when the laptop build that made them finished (taken before it began).
if [ ${#source_id} -ne 64 ] && [ -f "$IMAGES/being-source-id" ]; then
    source_id=$(head -n 1 "$IMAGES/being-source-id" | tr -cd '0-9a-f')
fi
[ ${#source_id} -eq 64 ] || source_id=""
{
    echo "built_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "build_id=${build_id:-unknown}"
    echo "source_id=${source_id:-unknown}"
    echo "disk_guid=$disk_uuid"
    echo "system_partuuid=$root_uuid"
    echo "models_partuuid=$models_uuid"
    echo "logs_partuuid=$logs_uuid"
    (cd "$IMAGES" && sha256sum efi-part/EFI/BOOT/bootx64.efi rootfs.squashfs bzImage 2>/dev/null || true) |
        sed 's/^\([0-9a-f]*\)  \(.*\)$/sha256 \2 \1/'
} | sed 's/$/\r/' > "$stage/iso/BUILD.txt"
sed -i 's/$/\r/' "$stage/iso/README.txt"

esp_type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B
linux_type=0FC63DAF-8483-4772-8E79-3D69D8477DE4
data_type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7
iso="$stage/$NAME.iso"
# -partition_offset 16 makes GPT entry 1 a mountable ISO 9660 volume of its
# own (another computer shows README.txt instead of an unreadable disk).
# Cylinders of 64 x 32 sectors are 1 MiB: -partition_cyl_align all pads the
# ISO 9660 data and every appended partition to 1 MiB, so each partition
# starts on a 1 MiB boundary, as flash memory prefers.
xorriso -report_about WARNING -as mkisofs \
    -o "$iso" -V PIPOS -r -J -joliet-long -no-pad \
    -partition_offset 16 -partition_cyl_align all -partition_hd_cyl 64 -partition_sec_hd 32 \
    -append_partition 1 "$esp_type" "$stage/esp.img" \
    -append_partition 2 "$linux_type" "$rootfs" \
    -append_partition 3 "$linux_type" "$stage/models.ext4" \
    -append_partition 4 "$data_type" "$stage/logs.img" \
    -appended_part_as_gpt \
    -e --interval:appended_partition_1:all:: -no-emul-boot \
    "$stage/iso"

# xorriso numbers appended partitions 1-4 as GPT entries 2-5: entry 1
# ("Gap0") covers the ISO 9660 data.
# Names, GUIDs and the read-only attribute the Try image's genimage sets.
sfdisk -q --disk-id "$iso" "$disk_uuid"
sfdisk -q --part-label "$iso" 1 ISO9660
for change in "2 try-esp $esp_uuid" "3 try-system $root_uuid" "4 try-models $models_uuid" \
              "5 try-logs $logs_uuid"; do
    set -- $change
    sfdisk -q --part-label "$iso" "$1" "$2"
    sfdisk -q --part-uuid "$iso" "$1" "$3"
done
sfdisk -q --part-attrs "$iso" 3 "GUID:60"
sfdisk -q --part-attrs "$iso" 4 "GUID:60"

# Check the result: GPT, the four Try partitions with their contents, and
# an El Torito EFI entry that still points at the EFI partition.
sfdisk --verify "$iso" >/dev/null
sfdisk --json "$iso" > "$stage/table.json"
python3 - "$iso" "$stage/table.json" "$esp_uuid" "$root_uuid" "$models_uuid" "$logs_uuid" <<'PY'
import json, sys
iso, table, *uuids = sys.argv[1:]
parts = {int(p["node"].rsplit(".iso", 1)[1]): p for p in json.load(open(table))["partitiontable"]["partitions"]}
want = {2: ("try-esp", "C12A7328-F81F-11D2-BA4B-00A0C93EC93B", b"", 0),
        3: ("try-system", "0FC63DAF-8483-4772-8E79-3D69D8477DE4", b"hsqs", 0),
        4: ("try-models", "0FC63DAF-8483-4772-8E79-3D69D8477DE4", b"\x53\xef", 1080),
        5: ("try-logs", "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7", b"FAT32", 82)}
if sorted(parts) != [1, 2, 3, 4, 5]:
    sys.exit(f"unexpected GPT entries: {sorted(parts)}")
with open(iso, "rb") as disk:
    for (number, (name, kind, magic, at)), guid in zip(sorted(want.items()), uuids):
        part = parts.get(number)
        if not part or part.get("name") != name or part["type"].upper() != kind or part["uuid"].lower() != guid:
            sys.exit(f"partition {number} is not {name}: {part}")
        disk.seek(part["start"] * 512 + at)
        if magic and disk.read(len(magic)) != magic:
            sys.exit(f"partition {number} ({name}) does not hold its file system")
    for at in (0, parts[1]["start"] * 512):
        disk.seek(at + 0x8001)
        if disk.read(5) != b"CD001":
            sys.exit(f"no ISO 9660 volume descriptor at byte {at}")
    if any(parts[number]["start"] % 2048 for number in want):
        sys.exit("a partition does not start on a 1 MiB boundary")
print("GPT:", ", ".join(f"{n} {parts[n]['name']}" for n in sorted(want)))
PY
esp_sectors=$(sfdisk --json "$iso" | python3 -c 'import json, sys; print(json.load(sys.stdin)["partitiontable"]["partitions"][1]["size"])')
xorriso -indev "$iso" -report_el_torito plain 2>/dev/null |
    grep -Eq "^El Torito boot img : +1 +UEFI +y +none +0x0000 +0x00 +$esp_sectors " ||
    { echo "The El Torito EFI entry is missing or does not state the EFI partition's size" >&2; exit 1; }

(cd "$stage" && sha256sum "$NAME.iso" > "$NAME.iso.sha256")
mv -f "$iso" "$OUT/$NAME.iso"
mv -f "$stage/$NAME.iso.sha256" "$OUT/$NAME.iso.sha256"
# The id of the source the ISO's system was built from, for the Mac side to
# record as the ISO's source-id.
echo "$source_id" > "$OUT/.$NAME.iso.built-from"
INNER

# lab/source_id.py compares IMAGE.source-id with the tree before a QEMU run.
# The ISO records the source its laptop build came from (never the tree's id
# as it is now, which could name code the ISO does not hold), and the tree's
# id taken before the ISO was started says whether that build is fresh.
built_from=$(tr -cd '0-9a-f' < "$OUTPUT/.$NAME.iso.built-from" 2>/dev/null || true)
rm -f "$OUTPUT/.$NAME.iso.built-from"
if [ ${#built_from} -eq 64 ]; then
    if (cd "$ROOT" && python3 -m lab.source_id stamp "$OUTPUT/$NAME.iso" "$built_from") >/dev/null; then
        if [ "$built_from" = "$tree_id" ]; then
            echo "Source id ${built_from:0:12} recorded in $NAME.iso.source-id (the tree as it was when the ISO was started)."
        else
            echo "Note: $NAME.iso holds a laptop build from source ${built_from:0:12}, but the tree was ${tree_id:0:12} when the ISO was started; rebuild with lab/build.sh laptop for a fresh ISO." >&2
        fi
    else
        echo "Note: could not write $NAME.iso.source-id." >&2
    fi
else
    echo "Note: the laptop build records no source id (rebuild it with lab/build.sh laptop); $NAME.iso carries none, and BEING_GATE=1 refuses it." >&2
fi

ls -lh "$OUTPUT/$NAME.iso"
cat "$OUTPUT/$NAME.iso.sha256"
