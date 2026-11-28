#!/bin/sh
# Create a test disk containing the exact supplied installed and factory bundles.
set -eu

if [ "$#" -ne 3 ]; then
    echo "Usage: $0 INSTALLED_RAUCB FACTORY_RAUCB OUTPUT_IMAGE" >&2
    exit 2
fi

installed=$1
factory=$2
output=$3
for bundle in "$installed" "$factory"; do
    if [ ! -f "$bundle" ] || [ ! -s "$bundle" ]; then
        echo "Missing bundle: $bundle" >&2
        exit 1
    fi
    if [ -e "$output" ] && [ "$output" -ef "$bundle" ]; then
        echo "The fixture image must be separate from the input bundles." >&2
        exit 1
    fi
done

mkdir -p "$(dirname "$output")"
scratch=$(mktemp -d "$(dirname "$output")/.beingos-update-qa.XXXXXX")
trap 'rm -rf "$scratch"' EXIT
trap 'exit 1' HUP INT TERM
mkdir "$scratch/files"
cp "$installed" "$scratch/files/installed.raucb"
cp "$factory" "$scratch/files/lab.raucb"
printf 'unsigned fixture\n' > "$scratch/files/unsigned.raucb"
installed_bytes=$(wc -c < "$scratch/files/installed.raucb")
factory_bytes=$(wc -c < "$scratch/files/lab.raucb")
size_mib=$(( (installed_bytes + factory_bytes + 1048575) / 1048576 + 32 ))
# The fixture holds three large files. Keep the block size and inode budget
# fixed so the default small-volume metadata cannot consume the data reserve.
mkfs.ext4 -q -b 4096 -N 1024 -d "$scratch/files" -L being-install-qa -m 0 "$scratch/media.img" "${size_mib}M"
mv -f "$scratch/media.img" "$output"
