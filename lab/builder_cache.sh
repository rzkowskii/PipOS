#!/bin/bash
# Fill the offline builder's download caches from this Mac.
#
# The builder container has no network. Buildroot source tarballs live in
# /work/dl/<package>/ and the Rust crates in /work/dl/br-cargo-home, both in the
# beingos-work volume. A tree that adds a package or a crate fails offline
# ("unable to resolve host address", "failed to get `<crate>` as a dependency").
# This script finds what is missing, fetches it here, checks every tarball
# against the hash Buildroot pins for it, and copies it into the volume.
#
#   lab/builder_cache.sh check [TREE]   list what TREE (default laptop-cable) lacks
#   lab/builder_cache.sh fill  [TREE]   fetch, verify and load it, plus the crates
#
# TREE is an output tree under /work/output that has been configured once
# (laptop, laptop-cable, laptop-maintenance, x86_64). A tarball whose hash
# does not match Buildroot's .hash file is never loaded. Crates come from
# `cargo fetch --locked` against Cargo.lock, which cargo checks itself.

set -euo pipefail
cd "$(dirname "$0")/.."

PROFILE="beingos"
IMAGE="beingos-builder"
VOLUME="beingos-work"
BUILDROOT_VERSION="2026.08.2"
MIRROR="https://sources.buildroot.net"
mode="${1:-check}"
tree="${2:-laptop-cable}"

docker_() { docker --context "colima-$PROFILE" "$@"; }

case "$mode" in check|fill) ;; *) echo "usage: $0 check|fill [TREE]" >&2; exit 2 ;; esac
if ! docker_ info >/dev/null 2>&1; then
    echo "Start the builder first (any lab/build.sh command starts it)." >&2
    exit 1
fi

# One line per missing tarball: package, download subdirectory, file, algorithm, hash.
# show-info needs the external tree at /src, as in lab/build.sh.
missing=$(docker_ run --rm -i -v "$VOLUME:/work" -v "$PWD:/src" "$IMAGE" bash -s "$tree" "$BUILDROOT_VERSION" <<'EOF'
set -euo pipefail
tree=$1 version=$2
cd "/work/buildroot-$version"
test -f "/work/output/$tree/.config" || { echo "no configured tree /work/output/$tree" >&2; exit 1; }
make -s O="/work/output/$tree" show-info > /tmp/show-info.json
python3 - "$tree" <<'PY'
import glob, json, os, sys
have = set()
for _, _, files in os.walk("/work/dl"):
    have.update(files)
info = json.load(open("/tmp/show-info.json"))
index = {}
# Buildroot's own hashes, then the tree's (BR2_GLOBAL_PATCH_DIR pins the kernel).
for hashfile in (glob.glob("package/*/*.hash") + glob.glob("linux/*.hash") + glob.glob("boot/*/*.hash")
                 + glob.glob("/src/os/package/*/*.hash") + glob.glob("/src/os/patches/*/*.hash")):
    for line in open(hashfile):
        parts = line.split()
        if len(parts) == 3:
            index.setdefault(parts[2], []).append(hashfile)


def hashes_for(source):
    return index.get(source, [])
seen = set()
for name, package in info.items():
    for download in package.get("downloads") or []:
        source = download.get("source")
        uris = download.get("uris") or []
        if not source or source in have or source in seen or (uris and uris[0].startswith("local")):
            continue
        seen.add(source)
        # The package whose .hash file pins this tarball also names its
        # download subdirectory (linux-headers downloads into linux/).
        found = None
        for hashfile in hashes_for(source):
            for line in open(hashfile):
                parts = line.split()
                if len(parts) == 3 and parts[2] == source and parts[0] in ("sha256", "sha512"):
                    owner = os.path.basename(os.path.dirname(hashfile))
                    # The kernel and its headers share the linux/ download directory.
                    found = ("linux" if owner in ("linux", "linux-headers") else owner, parts[0], parts[1])
                    break
            if found:
                break
        subdir, algo, digest = found or (name[5:] if name.startswith("host-") else name, "none", "-")
        print(name, subdir, source, algo, digest, sep="\t")
PY
EOF
)

if [ -z "$missing" ]; then
    echo "No source tarballs missing for $tree."
else
    echo "Missing source tarballs for $tree:"
    printf '%s\n' "$missing" | awk -F'\t' '{printf "  %-20s %s (%s)\n", $1, $3, $4}'
fi
[ "$mode" = fill ] || exit 0

work=$(mktemp -d "${TMPDIR:-/tmp}/pipos-builder-cache.XXXXXX")
trap 'rm -rf "$work"' EXIT

if [ -n "$missing" ]; then
    while IFS=$'\t' read -r _ subdir source algo digest; do
        if [ "$algo" = none ]; then
            echo "Refusing $source: Buildroot pins no hash for it; fetch and check it by hand." >&2
            exit 1
        fi
        mkdir -p "$work/dl/$subdir"
        curl -fsSL --retry 2 --max-time 900 -o "$work/dl/$subdir/$source" "$MIRROR/$subdir/$source"
        bits=${algo#sha}
        got=$(shasum -a "$bits" "$work/dl/$subdir/$source" | awk '{print $1}')
        if [ "$got" != "$digest" ]; then
            echo "Refusing $source: $algo $got, Buildroot pins $digest." >&2
            exit 1
        fi
        echo "Verified $subdir/$source ($algo)."
    done <<< "$missing"
    COPYFILE_DISABLE=1 tar -C "$work/dl" -cf - . |
        docker_ run --rm -i -v "$VOLUME:/work" "$IMAGE" tar -xf - -C /work/dl --skip-old-files
    echo "Loaded into /work/dl."
fi

# Crates: Cargo.lock decides, and `cargo build --locked` resolves dev-dependencies too.
CARGO_HOME="$work/cargo" cargo fetch --locked --quiet
before=$(docker_ run --rm -v "$VOLUME:/work" "$IMAGE" sh -c 'ls /work/dl/br-cargo-home/registry/cache/*/ 2>/dev/null | wc -l')
COPYFILE_DISABLE=1 tar -C "$work/cargo" -cf - registry/cache registry/index |
    docker_ run --rm -i -v "$VOLUME:/work" "$IMAGE" sh -c \
        'mkdir -p /work/dl/br-cargo-home && tar -xf - -C /work/dl/br-cargo-home --skip-old-files'
after=$(docker_ run --rm -v "$VOLUME:/work" "$IMAGE" sh -c 'ls /work/dl/br-cargo-home/registry/cache/*/ | wc -l')
echo "Crate cache: $before files before, $after after."
