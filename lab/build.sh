#!/bin/bash
# Build the PipOS image.
#
# The build runs in a Linux container inside its own Colima profile ("beingos").
# Sources, downloads and build output stay in a volume in that profile; only the
# finished images are copied to out/images/ here.

set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

PROFILE="beingos"
IMAGE="beingos-builder"
VOLUME="beingos-work"
BUILDROOT_VERSION="2026.08.2"
# Pinned after the first download; a different file is refused.
BUILDROOT_SHA256="bc9497c8e3a734c277e541e2f7fcf9a6275dc92f75bd2f753b05981c54be8705"
DEFCONFIG="beingos_x86_64_defconfig"
OUT="/work/output/x86_64"

usage() {
    cat <<'EOF'
Usage: lab/build.sh [command]

  model FILE.gguf  use this model as the being's reflex model (copied to out/models/)
  build            build the image and copy it to out/images/ (the default)
  try-image        build a USB image for a temporary Try PipOS session
  try-screen       build a separate Try USB image with the local keyboard menu
  candidate NAME [--cache-overlay]
                   freeze inputs and build matched media in out/candidates/NAME
  laptop [ARGS...] build the laptop image (no lab access) as out/images/beingos-laptop.img
  laptop-try       build the laptop Try USB (local screen menu) as out/images/beingos-laptop-try.img
  laptop-config    re-apply beingos_laptop_defconfig in the laptop output tree
  laptop-make ARGS...
                   run make in the laptop output tree
                   With BEING_LAPTOP_MAINTENANCE=1 the laptop commands use a separate
                   tree with owner-enrolled LAN maintenance (OpenSSH) built in, and
                   name their images beingos-laptop-maintenance*.img.
  bundle           sign a lab update for the current build
  installed-bundle sign an update for systems installed from Try media
  test-media       build a disk of signed update fixtures for L3 tests
  installed-test-media INSTALLED FACTORY [IMAGE]
                   build the installed-channel QA disk from two project-relative bundles
  make ARGS...     run make in the Buildroot output tree, e.g. `make linux-rebuild`
  config           re-apply the defconfig from os/configs/
  shell            open a shell in the builder
  up               start the builder VM and prepare Buildroot, nothing more
  down             stop the builder VM
  destroy          delete the builder VM and everything built in it
EOF
}

docker_() { docker --context "colima-$PROFILE" "$@"; }

ensure_vm() {
    if ! colima status --profile "$PROFILE" >/dev/null 2>&1; then
        echo "Starting the builder VM..."
        colima start --profile "$PROFILE" --cpu 12 --memory 24 --disk 100 \
            --vm-type vz --arch aarch64 --mount-type virtiofs --activate=false \
            --mount "$ROOT:w" >/dev/null
    fi
}

ensure_image() {
    if ! docker_ image inspect "$IMAGE" >/dev/null 2>&1; then
        echo "Building the builder image..."
        docker_ build --quiet -t "$IMAGE" lab/builder >/dev/null
    fi
}

# Run a command in the builder. The project is mounted at /src.
run() {
    # A plain string, not an array: macOS ships bash 3.2, which rejects an
    # empty array under `set -u`.
    local tty=""
    if [ -t 0 ] && [ -t 1 ]; then tty="-it"; fi
    docker_ run --rm $tty \
        -v "$VOLUME:/work" -v "$ROOT:/src" \
        -e BR2_DL_DIR=/work/dl -e BR2_CCACHE_DIR=/work/ccache -e BEINGOS_LAB_KEYS=/work/beingos-keys \
        "$IMAGE" "$@"
}

ensure_buildroot() {
    run bash -euo pipefail -c '
        version="'"$BUILDROOT_VERSION"'"
        want="'"$BUILDROOT_SHA256"'"
        if [ ! -d "/work/buildroot-$version" ]; then
            cd /work
            echo "Downloading Buildroot $version..."
            curl -fsSLO "https://buildroot.org/downloads/buildroot-$version.tar.xz"
            got="$(sha256sum "buildroot-$version.tar.xz" | cut -d" " -f1)"
            if [ -n "$want" ] && [ "$got" != "$want" ]; then
                echo "Buildroot download has the wrong checksum: $got" >&2
                rm -f "buildroot-$version.tar.xz"
                exit 1
            fi
            echo "sha256 $got"
            tar -xf "buildroot-$version.tar.xz"
            rm "buildroot-$version.tar.xz"
        fi
        ln -sfn "buildroot-$version" /work/buildroot
        mkdir -p /work/dl /work/ccache
    '
}

up() {
    ensure_vm
    ensure_image
    ensure_buildroot
    # The private lab key stays in the builder volume. Only the certificate is
    # installed in the image. Rebuilding reuses the same trust root.
    run sh -eu -c '
        if [ ! -d "$BEINGOS_LAB_KEYS" ]; then
            umask 077
            keys=$(mktemp -d /work/.beingos-keys.XXXXXX)
            trap '\''rm -rf "$keys"'\'' EXIT
            openssl req -new -x509 -newkey rsa:3072 -noenc -days 3650 \
                -subj "/CN=PipOS Lab Updates" -keyout "$keys/key.pem" \
                -out "$keys/cert.pem" 2>/dev/null
            mv "$keys" "$BEINGOS_LAB_KEYS"
        fi
        test -s "$BEINGOS_LAB_KEYS/key.pem" && test -s "$BEINGOS_LAB_KEYS/cert.pem"
    '
}

br_make() {
    # MAKE_JOBS is set only for trees configured with per-package directories,
    # where Buildroot can build independent packages at the same time.
    run make -C /work/buildroot O="$OUT" BR2_EXTERNAL=/src/os ${MAKE_JOBS:-} "$@"
}

refresh_python_ssl() {
    # Enabling a Python extension in the defconfig need not rebuild a cached package.
    if run bash -c '
        grep -qx BR2_PACKAGE_PYTHON3_SSL=y "$1/.config" &&
        compgen -G "$1/build/python3-*/.stamp_target_installed" >/dev/null &&
        ! compgen -G "$1/target/usr/lib/python*/lib-dynload/_ssl.*.so" >/dev/null
    ' python-ssl "$OUT"; then
        br_make python3-reconfigure
    fi
}

# The laptop image: its own defconfig and its own output tree, so the lab
# tree (and the QEMU suites built from it) never changes.
LAPTOP_NAME=beingos-laptop
laptop_select() {
    DEFCONFIG="beingos_laptop_defconfig"
    OUT="/work/output/laptop"
    # The laptop defconfig uses per-package directories: build packages in parallel.
    MAKE_JOBS="-j12"
    case "${BEING_LAPTOP_MAINTENANCE:-0}" in
        0) ;;
        1) OUT="/work/output/laptop-maintenance"; LAPTOP_NAME=beingos-laptop-maintenance ;;
        *) echo "BEING_LAPTOP_MAINTENANCE must be 0 or 1" >&2; exit 2 ;;
    esac
}

# Set options in the laptop .config, then let Kconfig resolve them.
laptop_set() {
    run bash -euo pipefail -c '
        cfg=$1/.config; shift
        for kv in "$@"; do
            key=${kv%%=*}
            sed -i -e "/^$key=/d" -e "/^# $key is not set\$/d" "$cfg"
            echo "$kv" >> "$cfg"
        done
    ' laptop-set "$OUT" "$@"
    br_make olddefconfig
}

laptop_config() {
    br_make "$DEFCONFIG"
    if [ "${BEING_LAPTOP_MAINTENANCE:-0}" = 1 ]; then
        laptop_set BR2_PACKAGE_OPENSSH=y BR2_PACKAGE_OPENSSH_SERVER=y \
            BR2_PACKAGE_OPENSSH_KEY_UTILS=y BR2_PACKAGE_BEING_LAN_MAINTENANCE=y
    fi
    # The b43 firmware is owner-sourced and staged with lab/b43_firmware.py.
    if [ -d out/firmware-overlay/usr/lib/firmware/b43 ]; then
        laptop_set 'BR2_ROOTFS_OVERLAY="/src/out/firmware-overlay"'
    else
        echo "Note: no out/firmware-overlay/usr/lib/firmware/b43; the internal Wi-Fi will lack firmware (docs/HARDWARE.md)." >&2
    fi
    # Refuse a laptop configuration that carries lab access.
    run bash -euo pipefail -c '
        cfg=$1/.config
        for key in BR2_PACKAGE_BEING_LAB_BODY BR2_TARGET_ENABLE_ROOT_LOGIN BR2_TARGET_GENERIC_GETTY; do
            if grep -qx "$key=y" "$cfg"; then echo "Laptop config has lab-only $key=y" >&2; exit 1; fi
        done
        if [ "$2" != 1 ] && grep -qx "BR2_PACKAGE_OPENSSH=y" "$cfg"; then
            echo "Laptop config has OpenSSH without BEING_LAPTOP_MAINTENANCE=1" >&2; exit 1
        fi
    ' laptop-check "$OUT" "${BEING_LAPTOP_MAINTENANCE:-0}"
}

# Refuse a laptop tree that still holds lab-only files, then a lab boot config.
laptop_verify() {
    run bash -euo pipefail -c '
        t=$1/target bad=0
        lab="usr/lib/systemd/system/being-lab-body.service usr/lib/being/sim/body.py"
        [ "$2" = 1 ] || lab="$lab usr/sbin/sshd usr/sbin/being-lab-access usr/lib/systemd/system/being-lab-observe.service usr/lib/systemd/system/being-lab-dev.service"
        for f in $lab; do
            if [ -e "$t/$f" ]; then echo "Laptop image contains lab-only /$f" >&2; bad=1; fi
        done
        if grep -q "ttyS0\|^serial " "$1/images/efi-part/EFI/BOOT/grub.cfg" "$t/usr/share/being/installed-grub.cfg"; then
            echo "Laptop boot configuration still names the serial console" >&2; bad=1
        fi
        exit $bad
    ' laptop-verify "$OUT" "${BEING_LAPTOP_MAINTENANCE:-0}"
}

cmd="${1:-build}"
[ $# -gt 0 ] && shift
case "$cmd" in
    candidate)
        . lab/candidate-build.sh
        build_candidate "$@"
        ;;
    up)
        up
        ;;
    config)
        up
        br_make "$DEFCONFIG"
        ;;
    model)
        src="${1:?usage: lab/build.sh model FILE.gguf}"
        [ -f "$src" ] || { echo "no such file: $src" >&2; exit 1; }
        mkdir -p out/models
        cp "$src" out/models/reflex.gguf
        python3 lab/manifest.py "$src"
        ;;
    build)
        up
        # Apply the defconfig when there is no configuration yet, or it changed.
        run bash -c "[ -f $OUT/.config ] && [ $OUT/.config -nt /src/os/configs/$DEFCONFIG ]" || br_make "$DEFCONFIG"
        refresh_python_ssl
        br_make "$@"
        mkdir -p out/images
        run bash -c "cp --sparse=always $OUT/images/beingos.img /src/out/images/beingos.img.new && mv -f /src/out/images/beingos.img.new /src/out/images/beingos.img"
        ls -lh out/images/beingos.img
        ;;
    try-image|try-screen)
        up
        run bash -c "[ -f $OUT/.config ] && [ $OUT/.config -nt /src/os/configs/$DEFCONFIG ]" || br_make "$DEFCONFIG"
        refresh_python_ssl
        br_make being-rebuild all
        mkdir -p out/images
        screen=${BEING_TRY_SCREEN:-0}
        if [ "$cmd" = try-screen ]; then
            screen=1
        fi
        case "$screen" in
            0) output=beingos-try.img ;;
            1) output=beingos-try-screen.img ;;
            *) echo "BEING_TRY_SCREEN must be 0 or 1" >&2; exit 2 ;;
        esac
        run env BEING_TRY_SCREEN="$screen" sh /src/os/board/beingos/try-image.sh \
            "$OUT/host" "$OUT/images/efi-part/EFI/BOOT/bootx64.efi" \
            "$OUT/images/rootfs.squashfs" "$OUT/images/models.ext4" /src/out/images
        ls -lh "out/images/$output"
        ;;
    laptop-config)
        laptop_select
        up
        laptop_config
        ;;
    laptop)
        laptop_select
        up
        laptop_config
        refresh_python_ssl
        br_make "$@"
        laptop_verify
        mkdir -p out/images
        run bash -c "cp --sparse=always $OUT/images/beingos.img /src/out/images/$LAPTOP_NAME.img.new && mv -f /src/out/images/$LAPTOP_NAME.img.new /src/out/images/$LAPTOP_NAME.img"
        ls -lh "out/images/$LAPTOP_NAME.img"
        ;;
    laptop-try)
        laptop_select
        up
        laptop_config
        refresh_python_ssl
        br_make being-rebuild all
        laptop_verify
        mkdir -p out/images
        # The laptop has no serial line, so its Try media always has the screen menu.
        run bash -euo pipefail -c '
            stage=$(mktemp -d /src/out/images/.laptop-try.XXXXXX)
            trap "rm -rf \"\$stage\"" EXIT
            BEING_TRY_SCREEN=1 BEING_TRY_LAPTOP=1 sh /src/os/board/beingos/try-image.sh \
                "$1/host" "$1/images/efi-part/EFI/BOOT/bootx64.efi" \
                "$1/images/rootfs.squashfs" "$1/images/models.ext4" "$stage"
            mv -f "$stage/beingos-laptop-try.img" "/src/out/images/$2-try.img"
        ' laptop-try "$OUT" "$LAPTOP_NAME"
        ls -lh "out/images/$LAPTOP_NAME-try.img"
        ;;
    laptop-make)
        laptop_select
        up
        br_make "$@"
        ;;
    bundle|installed-bundle|test-media)
        up
        run bash /src/lab/updates.sh "$OUT" "$cmd"
        ;;
    installed-test-media)
        if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then usage >&2; exit 2; fi
        up
        run sh -c 'export PATH="$1/host/sbin:$1/host/bin:$PATH"; shift; cd /src; exec sh lab/installed-update-media.sh "$@"' \
            installed-test-media "$OUT" "$1" "$2" "${3:-out/images/beingos-installed-update-qa.img}"
        ;;
    make)
        up
        br_make "$@"
        ;;
    shell)
        up
        run bash
        ;;
    down)
        colima stop --profile "$PROFILE"
        ;;
    destroy)
        colima delete --profile "$PROFILE"
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
