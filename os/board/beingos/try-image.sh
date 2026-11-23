#!/bin/sh
# Assemble read-only Try media from the files produced by Buildroot.
set -eu

if [ "$#" -ne 5 ]; then
    echo "usage: $0 HOST_DIR BOOTX64_EFI ROOTFS_SQUASHFS MODELS_EXT4 OUTPUT_DIR" >&2
    exit 2
fi

board_dir=$(dirname "$0")
host_dir=$(realpath "$1")
bootloader=$(realpath "$2")
rootfs=$(realpath "$3")
models=$(realpath "$4")
output_dir=$5
screen_option=
output_name=beingos-try.img
case "${BEING_TRY_SCREEN:-0}" in
    0) ;;
    1) screen_option=being.screen=1; output_name=beingos-try-screen.img ;;
    *) echo "BEING_TRY_SCREEN must be 0 or 1" >&2; exit 2 ;;
esac
# BEING_TRY_BENCH=1: a bench stick. Every entry boots with being.bench=1
# (being-bench.service), and the models partition also carries the quant
# candidates in out/models/bench as the signed bench pack (/models/bench).
bench_option=
case "${BEING_TRY_BENCH:-0}" in
    0) ;;
    1) bench_option=being.bench=1 ;;
    *) echo "BEING_TRY_BENCH must be 0 or 1" >&2; exit 2 ;;
esac
try_options="$screen_option${screen_option:+${bench_option:+ }}$bench_option"
# BEING_TRY_LAPTOP=1: media from the laptop image, with no serial console.
BEINGOS_VARIANT=lab
case "${BEING_TRY_LAPTOP:-0}" in
    0) ;;
    1) BEINGOS_VARIANT=laptop; output_name=beingos-laptop-try.img ;;
    *) echo "BEING_TRY_LAPTOP must be 0 or 1" >&2; exit 2 ;;
esac
# A bench stick has its own name (lab/build.sh expects it).
if [ -n "$bench_option" ]; then
    output_name="${output_name%.img}-bench.img"
fi
. "$board_dir/slots.sh"

for artifact in "$bootloader" "$rootfs" "$models"; do
    if [ ! -f "$artifact" ] || [ ! -s "$artifact" ]; then
        echo "Missing build artifact: $artifact" >&2
        exit 1
    fi
done

export PATH="$host_dir/bin:$host_dir/sbin:$PATH"
mkdir -p "$output_dir"
stage=$(mktemp -d "$output_dir/.beingos-try.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
trap 'exit 1' HUP INT TERM

read -r disk_uuid < /proc/sys/kernel/random/uuid
read -r esp_uuid < /proc/sys/kernel/random/uuid
read -r root_uuid < /proc/sys/kernel/random/uuid
read -r models_uuid < /proc/sys/kernel/random/uuid
read -r logs_uuid < /proc/sys/kernel/random/uuid

insert_ids() {
    sed -e "s/TRY_DISK_UUID/$disk_uuid/g" -e "s/TRY_ESP_UUID/$esp_uuid/g" \
        -e "s/TRY_ROOT_UUID/$root_uuid/g" -e "s/TRY_MODELS_UUID/$models_uuid/g" \
        -e "s/TRY_LOGS_UUID/$logs_uuid/g" -e "s/TRY_SCREEN/$try_options/g" "$1"
}

# Laptop Try media carries a FAT32 PIPLOGS partition for first-boot
# diagnostics (being-usb-logs.service). The lab Try layout stays as it was.
try_logs() {
    if [ "$BEINGOS_VARIANT" = laptop ]; then
        cat
    else
        sed '/^#BEGIN try-logs/,/^#END try-logs/d'
    fi
}

boot_dir="$stage/input/efi-part/EFI/BOOT"
mkdir -p "$boot_dir" "$stage/root" "$stage/output"
cp "$bootloader" "$boot_dir/bootx64.efi"
insert_ids "$board_dir/try-grub.cfg" | boot_console > "$boot_dir/grub.cfg"
insert_ids "$board_dir/try-genimage.cfg" | try_logs > "$stage/genimage.cfg"
if [ "$BEINGOS_VARIANT" = laptop ]; then
    mkdir -p "$stage/input/logs-part"
    sed 's/$/\r/' "$board_dir/try-logs-README.txt" > "$stage/input/logs-part/README.txt"
fi
ln -s "$rootfs" "$stage/input/rootfs.squashfs"
# Installed images use LABEL=models. Give the Try copy a separate identity.
if [ -n "$bench_option" ]; then
    # The same packs post-image.sh stages, plus the bench pack, signed with
    # the same builder key, so the rootfs's descriptors still match.
    src=$(cd "$board_dir/../../.." && pwd)
    if [ ! -s "$src/out/models/bench/manifest.json" ]; then
        echo "No bench models in out/models/bench: run lab/quant_candidates.py first." >&2
        exit 1
    fi
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$src" python3 -m lab.package_model \
        --source "$src/out/models" --output "$stage/models-root" --bench \
        --certificate "$BEINGOS_LAB_KEYS/cert.pem" --key "$BEINGOS_LAB_KEYS/key.pem"
    # The same ext4 metadata allowance as post-image.sh: the bench quants make
    # this partition about 5 GB, where the journal and inodes alone pass 140 MB.
    models_mb=$(du -sm "$stage/models-root" | cut -f1)
    models_mb=$(( models_mb + models_mb / 16 + 128 ))
    mkfs.ext4 -q -d "$stage/models-root" -L being-try-models -m 0 "$stage/input/models.ext4" "${models_mb}M"
    rm -rf "$stage/models-root"
    tune2fs -U random "$stage/input/models.ext4"
else
    cp --sparse=always "$models" "$stage/input/models.ext4"
    tune2fs -L being-try-models -U random "$stage/input/models.ext4"
fi

"$host_dir/bin/genimage" \
    --rootpath "$stage/root" --tmppath "$stage/tmp" --inputpath "$stage/input" \
    --outputpath "$stage/output" --config "$stage/genimage.cfg"
mv -fT "$stage/output/beingos-try.img" "$output_dir/$output_name"
