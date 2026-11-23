#!/usr/bin/env bash
# Render the PipOS media gallery into docs/media/ on this Mac (plan task 7.11).
#
#   lab/media.sh            rebuild only if the face or console sources changed
#   lab/media.sh --force    rebuild everything
#   lab/media.sh --check    exit 1 if the gallery is out of date (for scripts/check)
#
# What it makes:
#   docs/media/expressions.png   every expression, from `face --sheet`
#   docs/media/tour.mp4          a 30 s scripted tour (`face --script tour`), if ffmpeg is installed
#   docs/media/tour-poster.png   one frame of the tour (always)
#   docs/media/try-menu.png      the Try menu as the laptop draws it (lab/console_preview.py), if
#                                Homebrew SDL2 is installed
#   docs/media/SOURCES.sha256    the sources the gallery was made from
#
# Nothing here boots a VM or touches out/hardware-usb. It needs cargo; ffmpeg and
# sdl2-config are optional and a missing one is reported, never faked.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
MEDIA="$ROOT/docs/media"
STAMP="$MEDIA/SOURCES.sha256"
mode=${1:-}
case "$mode" in
    ""|--force|--check) ;;
    *) echo "usage: lab/media.sh [--force|--check]" >&2; exit 2 ;;
esac

sources() {
    cd "$ROOT"
    find crates/face crates/bus os/package/being -type f \
        \( -name '*.rs' -o -name '*.frag' -o -name '*.vert' -o -name '*.c' -o -name '*.h' -o -name Cargo.toml \) |
        LC_ALL=C sort | xargs shasum -a 256
    shasum -a 256 mind/local_menu.py mind/console_ui.py lab/console_preview.py lab/media.sh
}

current=$(sources | shasum -a 256 | cut -d' ' -f1)
recorded=$(sed -n 's/^# gallery //p' "$STAMP" 2>/dev/null || true)
if [ "$mode" = --check ]; then
    if [ "$current" = "$recorded" ]; then
        echo "media: up to date"
        exit 0
    fi
    echo "media: out of date (run lab/media.sh)" >&2
    exit 1
fi
if [ "$mode" != --force ] && [ "$current" = "$recorded" ]; then
    echo "media: up to date ($MEDIA)"
    exit 0
fi

mkdir -p "$MEDIA"
work=$(mktemp -d "${TMPDIR:-/tmp}/pipos-media.XXXXXX")
trap 'rm -rf "$work"' EXIT
missing=()

echo "media: building the face"
(cd "$ROOT" && cargo build --quiet -p face)
face="$ROOT/target/debug/face"

echo "media: expression sheet"
"$face" --sheet "$work/expressions.png" --size 640x400
mv "$work/expressions.png" "$MEDIA/expressions.png"

echo "media: 30 s tour"
"$face" --script tour --frames "$work/tour" --fps 30 --size 1280x800 --exit-after 30
frames=("$work"/tour/frame-*.png)
if [ ! -e "${frames[0]}" ]; then
    echo "media: the face wrote no tour frames" >&2
    exit 1
fi
cp "${frames[$(( ${#frames[@]} / 3 ))]}" "$MEDIA/tour-poster.png"
if command -v ffmpeg >/dev/null; then
    ffmpeg -loglevel error -y -framerate 30 -i "$work/tour/frame-%05d.png" \
        -c:v libx264 -pix_fmt yuv420p -movflags +faststart "$work/tour.mp4"
    mv "$work/tour.mp4" "$MEDIA/tour.mp4"
else
    missing+=("ffmpeg (tour.mp4 not made)")
fi

echo "media: Try menu"
if command -v sdl2-config >/dev/null; then
    (cd "$ROOT" && python3 -m lab.console_preview --size 1280x800 --wait 2 --out "$work/try-menu.png" -- \
        python3 -c 'from mind import local_menu; local_menu.welcome(); input()')
    mv "$work/try-menu.png" "$MEDIA/try-menu.png"
else
    missing+=("sdl2-config (try-menu.png not made)")
fi

{
    echo "# gallery $current"
    echo "# made $(date -u +%Y-%m-%dT%H:%M:%SZ)${missing[*]:+; missing: ${missing[*]}}"
    sources
} > "$STAMP"
echo "media: wrote $MEDIA"
if [ "${#missing[@]}" -gt 0 ]; then
    printf 'media: not made, missing %s\n' "${missing[@]}" >&2
fi
