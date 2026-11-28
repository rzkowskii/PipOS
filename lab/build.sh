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
                   (with no ARGS it rebuilds the being package first, then everything)
  laptop-try       build the laptop Try USB (local screen menu) as out/images/beingos-laptop-try.img
                   With BEING_TRY_BENCH=1 the Try commands build a bench stick instead
                   (entries other than bench+ run bench mode;
                   /models/bench holds out/models/bench),
                   named with a -bench suffix, e.g. beingos-laptop-try-bench.img.
                   With BEING_LAPTOP_DIAGNOSTIC=1 it instead uses a separate
                   maintenance tree and writes beingos-laptop-diagnostic-try.img.
                   That image has a tty2 root console and diagnostic tools,
                   cannot install, and cannot be signed or exported as a product.
  laptop-config    re-apply beingos_laptop_defconfig in the laptop output tree
  laptop-make ARGS...
                   run make in the laptop output tree
                   With BEING_LAPTOP_MAINTENANCE=1 the laptop commands use a separate
                   tree with owner-enrolled LAN maintenance (OpenSSH) built in, and
                   name their images beingos-laptop-maintenance*.img.
                   With BEING_LAPTOP_CABLE=1 they use the cable development tree
                   (/work/output/laptop-cable): a Try boot of beingos-laptop-cable-try.img
                   gives root SSH over an Ethernet cable from link-local addresses only,
                   with the key cable-key imported, plus a writable copy of the code and
                   the audio debugging kernel options (docs/LAB.md). Never installed,
                   never made into an ISO, never signed as an update.
  cable-key PUBKEY HOSTKEY
                   import the cable login public key and the cable SSH host key (an
                   unencrypted OpenSSH Ed25519 private key), both kept outside this
                   repository, into the builder for BEING_LAPTOP_CABLE=1 builds
  release-key DIR [DIR2]
                   on this Mac: make the owner's passphrase-protected release key and
                   certificate in DIR (outside this repository), copied to DIR2 too
  release-cert FILE
                   import the release certificate (never the key) into the builder;
                   laptop images then trust only it for updates
  laptop-bundle KEY [VERSION]
                   sign the laptop rootfs as an update for installed laptops, with the
                   owner's release key (asks for its passphrase)
  laptop-rotation-bundle OLD_KEY OLD_CERT VERSION
                   sign a laptop rootfs that trusts the replacement certificate
                   using the current installed certificate and key
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

Every image these commands write to out/images carries IMAGE.source-id, the
source id (python3 -m lab.source_id) of the tree as it was when the build
started, so the lab suites can tell a fresh image from an old one. Only a
run that rebuilds the being package and then the image ("all") records one:
plain "build", or "build"/"laptop" with other targets, may assemble an image
from older code, so it gets none (BEING_GATE=1 refuses such an image).

The commands that rebuild the being package (try-image, try-screen, laptop,
laptop-try, and build or laptop given being-rebuild) first clean its build
directory, so every source file is copied in again: Buildroot copies a local
source tree with rsync --update, which keeps a build-directory file that is
newer than its source. BEING_FAST_REBUILD=1 skips the clean (an incremental
rebuild that may keep such files and therefore records no source id).
EOF
}

docker_() { docker --context "colima-$PROFILE" "$@"; }

# The tree's source id (lab/source_id.py), taken before a build starts:
# a build takes most of an hour and the tree can change under it, so an id
# taken afterwards could name code the image never contained. Empty when it
# cannot be taken; the image then carries no id and BEING_GATE=1 refuses it.
SOURCE_ID=""
BUILD_EMBED_SOURCE_ID=""
take_source_id() {
    local ident
    if ident=$(python3 -m lab.source_id 2>/dev/null) && printf '%s\n' "$ident" | grep -Eqx '[0-9a-f]{64}'; then
        SOURCE_ID=$ident
        echo "Source id ${SOURCE_ID:0:12}, taken before the build."
    else
        SOURCE_ID=""
        echo "Note: python3 -m lab.source_id gave no source id; the image will carry none (BEING_GATE=1 refuses it)." >&2
    fi
}

# Beside the images in the builder's output tree: the id of the tree the
# last finished build there came from (lab/make-iso.sh reads it). Removed
# before a build starts, so a build that stops part way leaves no id behind.
BUILD_SOURCE_ID=being-source-id
CORTEX_UNITS=(being-cortex.service being-dream-cortex.service being-model-check.service)
forget_build_source_id() {
    run rm -f "$OUT/images/$BUILD_SOURCE_ID"
}

verify_packaged_cortex() {
    run bash -euo pipefail -c '
        out=$1 expected=$2
        shift 2
        dump() { "$out/host/bin/unsquashfs" -cat "$out/images/rootfs.squashfs" "$1"; }
        compare() {
            if ! dump "$1" | cmp -s "$2" -; then
                echo "Packaged /$1 differs from $2" >&2
                exit 1
            fi
        }
        if [ "$(dump etc/being/source-id)" != "$expected" ]; then
            echo "The packaged rootfs has a missing or different source ID" >&2
            exit 1
        fi
        compare usr/share/being/spoken-guide.json /src/docs/manual/spoken-guide.json
        if grep -qx BR2_PACKAGE_CORTEX=y "$out/.config"; then
            for unit in "$@"; do
                compare "usr/lib/systemd/system/$unit" "/src/os/package/cortex/$unit"
            done
            compare usr/share/being/chat-template.jinja /src/config/chat-template.jinja
        fi
    ' verify-packaged-cortex "$OUT" "$SOURCE_ID" "${CORTEX_UNITS[@]}"
}

record_build_source_id() {
    [ -n "$SOURCE_ID" ] && [ "$BEING_FRESH" = 1 ] || return 0
    verify_packaged_cortex
    run sh -euc 'printf "%s\n" "$2" > "$1/images/$3"' record-source-id "$OUT" "$SOURCE_ID" "$BUILD_SOURCE_ID"
}

# IMAGE.source-id beside an image in out/images. unstamp_image runs before
# the image is replaced, so the new image never sits beside an old id.
unstamp_image() {
    rm -f "$1.source-id"
}
stamp_image() {
    unstamp_image "$1"
    if [ -z "$SOURCE_ID" ]; then
        echo "Note: $1 has no source id." >&2
        return 0
    fi
    if [ "$BEING_FRESH" != 1 ]; then
        echo "Note: $1 has no source id: this run did not rebuild the being package and then the image," \
            "so the image may hold older code (lab/build.sh $cmd being-rebuild all records one)." >&2
        return 0
    fi
    python3 -m lab.source_id stamp "$1" "$SOURCE_ID" >/dev/null ||
        { echo "Note: could not write $1.source-id." >&2; return 0; }
    echo "Source id ${SOURCE_ID:0:12} recorded in $1.source-id"
}

ensure_vm() {
    if ! colima status --profile "$PROFILE" >/dev/null 2>&1; then
        # Sizes apply when the VM is first made (or after it was stopped);
        # a running builder is never resized. The Mac Studio has 16 cores
        # and 128 GB: leave two cores to macOS.
        echo "Starting the builder VM..."
        colima start --profile "$PROFILE" --cpu "${BEING_BUILDER_CPUS:-14}" \
            --memory "${BEING_BUILDER_MEMORY:-48}" --disk 100 \
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
        -e BR2_DL_DIR=/work/dl -e BR2_CCACHE_DIR=/work/ccache -e BEINGOS_LAB_KEYS=/work/beingos-keys -e BEING_PACK_ROLES="${BEING_PACK_ROLES:-}" \
        -e BEINGOS_RELEASE_CERT="${BEINGOS_RELEASE_CERT:-}" \
        -e BEINGOS_REQUIRE_RELEASE_KEY="${BEINGOS_REQUIRE_RELEASE_KEY:-0}" \
        -e BEINGOS_CABLE_KEYS="$CABLE_DIR" \
        -e BEING_SOURCE_ID="${BUILD_EMBED_SOURCE_ID:-}" \
        "$IMAGE" "$@"
}

# The owner's release certificate, once imported (release-cert). Only the
# certificate is ever inside the builder; the key stays on the owner's media.
RELEASE_DIR=/work/beingos-release
RELEASE_CN="PipOS Laptop Release"
# The cable development keys, once imported (cable-key): the login public key
# and the SSH host key that BEING_LAPTOP_CABLE=1 images carry (post-build.sh).
CABLE_DIR=/work/beingos-cable

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

# Refuse to start a build that would fill the builder's disk part way.
check_space() {
    local need="${BEING_BUILD_MIN_FREE_GB:-25}"
    run bash -c '
        free=$(df -Pk /work | awk "NR == 2 { print int(\$4 / 1048576) }")
        if [ "$free" -lt "$1" ]; then
            echo "The builder has ${free} GB free; a full build needs about $1 GB." >&2
            echo "Free space in the beingos-work volume first (BEING_BUILD_MIN_FREE_GB overrides)." >&2
            exit 1
        fi
    ' check-space "$need"
}

br_make() {
    # MAKE_JOBS is set only for trees configured with per-package directories,
    # where Buildroot can build independent packages at the same time.
    run make -C /work/buildroot O="$OUT" BR2_EXTERNAL=/src/os ${MAKE_JOBS:-} "$@"
}

# The being package uses Buildroot's local site method, whose rsync copies
# only files newer than the build directory's copy (rsync --update): a
# build-directory file with a later time (a workspace Cargo.toml, say) is
# kept even after its source changed. Cleaning the build directory first
# makes the next build copy every file in again. BEING_FAST_REBUILD=1 skips it.
fast_rebuild() {
    case "${BEING_FAST_REBUILD:-0}" in
        0) return 1 ;;
        1) return 0 ;;
        *) echo "BEING_FAST_REBUILD must be 0 or 1" >&2; exit 2 ;;
    esac
}
# Refuse a bad BEING_FAST_REBUILD before anything starts.
check_fast_rebuild() {
    fast_rebuild || true
}
being_clean() {
    if fast_rebuild; then
        echo "BEING_FAST_REBUILD=1: the being package is rebuilt in place and may keep older files." >&2
    else
        echo "Cleaning the being package so its sources are copied in afresh (BEING_FAST_REBUILD=1 skips this)."
        br_make being-dirclean
    fi
}
# Whether this run rebuilt the being package and then assembled the images
# from it. Only then do the images hold the tree SOURCE_ID names: the being
# package is the whole repository (mind, crates, config, sim and the unit
# files), and Buildroot never copies a local source in again by itself, so a
# plain "make all" can assemble an image from code copied in days ago.
BEING_FRESH=0
# Rebuild the being package from a fresh copy of the tree.
being_rebuild() {
    being_clean
    br_make being-rebuild
}
# The being package from a fresh copy, then the images from it.
being_rebuild_all() {
    being_rebuild
    if ! fast_rebuild; then BEING_FRESH=1; fi
    br_make_recorded all
}

# Keep the on-device source ID and the host sidecar under the same rules.
br_make_recorded() {
    if [ "$BEING_FRESH" = 1 ]; then
        BUILD_EMBED_SOURCE_ID="$SOURCE_ID" br_make "$@"
    else
        br_make "$@"
    fi
}
# Run local package cleans and rebuilds separately so a parallel make cannot
# assemble the image before they finish.
br_make_targets() {
    local target pending=() cleaned=0
    for target in "$@"; do
        case "$target" in
            being-dirclean|being-rebuild)
                if [ "${#pending[@]}" -gt 0 ]; then
                    br_make_recorded "${pending[@]}"
                    pending=()
                fi
                if [ "$target" = being-dirclean ]; then
                    br_make being-dirclean
                    cleaned=1
                else
                    being_rebuild
                    if ! fast_rebuild; then cleaned=1; fi
                fi
                ;;
            all)
                if [ "$cleaned" = 1 ]; then BEING_FRESH=1; fi
                pending+=("$target")
                ;;
            *) pending+=("$target") ;;
        esac
    done
    if [ "${#pending[@]}" -gt 0 ]; then
        br_make_recorded "${pending[@]}"
    elif [ "$#" -eq 0 ]; then
        br_make_recorded
    fi
}

refresh_python_extensions() {
    # Enabling a Python extension in the defconfig need not rebuild a cached package.
    if run bash -c '
        compgen -G "$1/build/python3-*/.stamp_target_installed" >/dev/null &&
        { grep -qx BR2_PACKAGE_PYTHON3_SSL=y "$1/.config" &&
          ! compgen -G "$1/target/usr/lib/python*/lib-dynload/_ssl.*.so" >/dev/null ||
          grep -qx BR2_PACKAGE_PYTHON3_ZLIB=y "$1/.config" &&
          ! compgen -G "$1/target/usr/lib/python*/lib-dynload/zlib.*.so" >/dev/null; }
    ' python-extensions "$OUT"; then
        br_make python3-reconfigure
    fi
}

# Configuration changes Buildroot does not notice by itself. A new kernel
# patch needs a fresh kernel source tree, and systemd must be configured
# again to pick up libseccomp. Kernel option changes are noticed (the
# config files are make dependencies).
refresh_stale() {
    local targets
    targets=$(run bash -c '
        o=$1 out=
        shift
        kernel=$(ls -d "$o"/build/linux-[0-9]* 2>/dev/null | head -n 1)
        if [ -n "$kernel" ] && [ -f /src/os/patches/linux/0001-hwmon-applesmc-battery-charge-limit.patch ] &&
            ! grep -q battery_charge_limit "$kernel/drivers/hwmon/applesmc.c" 2>/dev/null; then
            out="$out linux-dirclean"
        fi
        if grep -qx BR2_PACKAGE_LIBSECCOMP=y "$o/.config" 2>/dev/null; then
            for shared in "$o"/target/usr/lib/systemd/libsystemd-shared-*.so; do
                if [ -f "$shared" ] && ! grep -q libseccomp "$shared"; then out="$out systemd-reconfigure"; fi
                break
            done
        fi
        if grep -qx BR2_PACKAGE_CORTEX=y "$o/.config" 2>/dev/null &&
            compgen -G "$o/build/cortex-*/.stamp_target_installed" >/dev/null; then
            cortex="$o/target"
            if grep -qx BR2_PER_PACKAGE_DIRECTORIES=y "$o/.config"; then
                cortex="$o/per-package/cortex/target"
            fi
            stale=
            for unit in "$@"; do
                if ! cmp -s "/src/os/package/cortex/$unit" "$cortex/usr/lib/systemd/system/$unit"; then
                    stale=1
                    break
                fi
            done
            if [ "$stale" = 1 ] || ! cmp -s /src/config/chat-template.jinja "$cortex/usr/share/being/chat-template.jinja"; then
                out="$out cortex-reinstall"
            fi
        fi
        echo $out
    ' refresh-stale "$OUT" "${CORTEX_UNITS[@]}")
    if [ -n "$targets" ]; then
        echo "Refreshing packages whose configuration changed: $targets"
        # shellcheck disable=SC2086
        br_make $targets
    fi
}

# The laptop image: its own defconfig and its own output tree, so the lab
# tree (and the QEMU suites built from it) never changes.
LAPTOP_NAME=beingos-laptop
laptop_select() {
    DEFCONFIG="beingos_laptop_defconfig"
    OUT="/work/output/laptop"
    # The laptop defconfig uses per-package directories: build packages in
    # parallel. Four at once, each with BR2_JLEVEL=6 jobs, keeps 12-14 CPUs
    # busy without the 150-job pile-up (and LLVM link memory) of -j12 x 13.
    MAKE_JOBS="-j${BEING_LAPTOP_PACKAGE_JOBS:-4}"
    # Laptop images trust the owner's release certificate when one has been
    # imported (post-build.sh); otherwise the lab certificate, marked as such.
    BEINGOS_RELEASE_CERT="$RELEASE_DIR/cert.pem"
    case "${BEING_LAPTOP_MAINTENANCE:-0}" in
        0) ;;
        1) OUT="/work/output/laptop-maintenance"; LAPTOP_NAME=beingos-laptop-maintenance ;;
        *) echo "BEING_LAPTOP_MAINTENANCE must be 0 or 1" >&2; exit 2 ;;
    esac
    check_cable
    check_diagnostic
    if [ "$CABLE" = 1 ]; then
        OUT="/work/output/laptop-cable"
        LAPTOP_NAME=beingos-laptop-cable
    fi
    if [ "$DIAGNOSTIC" = 1 ]; then
        OUT="/work/output/laptop-diagnostic"
        LAPTOP_NAME=beingos-laptop-diagnostic
    fi
}

# BEING_LAPTOP_CABLE=1: the cable development build (docs/LAB.md). A stick
# for the owner's own bench, never for installing: it cannot be combined with
# LAN maintenance or with a build meant to be installed.
CABLE="${BEING_LAPTOP_CABLE:-0}"
check_cable() {
    case "$CABLE" in
        0) return 0 ;;
        1) ;;
        *) echo "BEING_LAPTOP_CABLE must be 0 or 1" >&2; exit 2 ;;
    esac
    if [ "${BEING_LAPTOP_MAINTENANCE:-0}" != 0 ]; then
        echo "Refusing: BEING_LAPTOP_CABLE=1 and BEING_LAPTOP_MAINTENANCE=1 are separate builds; choose one." >&2
        exit 2
    fi
    if [ "${BEINGOS_REQUIRE_RELEASE_KEY:-0}" = 1 ]; then
        echo "Refusing: a cable development stick is never installed, so it cannot be a BEINGOS_REQUIRE_RELEASE_KEY=1 build." >&2
        exit 2
    fi
}

# BEING_LAPTOP_DIAGNOSTIC=1: an owner-local, Try-only WP-C8 measurement image.
DIAGNOSTIC="${BEING_LAPTOP_DIAGNOSTIC:-0}"
check_diagnostic() {
    case "$DIAGNOSTIC" in
        0) return 0 ;;
        1) ;;
        *) echo "BEING_LAPTOP_DIAGNOSTIC must be 0 or 1" >&2; exit 2 ;;
    esac
    if [ "${BEING_LAPTOP_MAINTENANCE:-0}" != 0 ] || [ "$CABLE" != 0 ]; then
        echo "Refusing: diagnostic, cable and LAN-maintenance images are separate builds." >&2
        exit 2
    fi
    if [ "${BEINGOS_REQUIRE_RELEASE_KEY:-0}" = 1 ]; then
        echo "Refusing: a diagnostic stick is never installed or release-signed." >&2
        exit 2
    fi
    case "${cmd:-}" in
        laptop-config|laptop-make|laptop-try) ;;
        *) echo "Refusing: BEING_LAPTOP_DIAGNOSTIC=1 supports only laptop-config, laptop-make and laptop-try." >&2; exit 2 ;;
    esac
}

# Set options in the laptop .config, then let Kconfig resolve them.
# KEY=n writes "# KEY is not set", which Kconfig keeps over a default y.
laptop_set() {
    run bash -euo pipefail -c '
        cfg=$1/.config; shift
        for kv in "$@"; do
            key=${kv%%=*}
            sed -i -e "/^$key=/d" -e "/^# $key is not set\$/d" "$cfg"
            if [ "$kv" = "$key=n" ]; then
                echo "# $key is not set" >> "$cfg"
            else
                echo "$kv" >> "$cfg"
            fi
        done
    ' laptop-set "$OUT" "$@"
    br_make olddefconfig
}

# The kernel fragments the laptop defconfig names, plus the cable build's own.
cable_kernel_fragments() {
    local fragments
    fragments=$(sed -n 's/^BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES="\(.*\)"$/\1/p' "os/configs/$DEFCONFIG")
    [ -n "$fragments" ] || { echo "os/configs/$DEFCONFIG names no kernel fragment" >&2; exit 1; }
    printf '%s' "$fragments \$(BR2_EXTERNAL_BEINGOS_PATH)/board/beingos/linux-cable.config"
}

diagnostic_kernel_fragments() {
    local fragments
    fragments=$(sed -n 's/^BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES="\(.*\)"$/\1/p' "os/configs/$DEFCONFIG")
    [ -n "$fragments" ] || { echo "os/configs/$DEFCONFIG names no kernel fragment" >&2; exit 1; }
    printf '%s' "$fragments \$(BR2_EXTERNAL_BEINGOS_PATH)/board/beingos/linux-diagnostic.config"
}

laptop_config() {
    br_make "$DEFCONFIG"
    if [ "${BEING_LAPTOP_MAINTENANCE:-0}" = 1 ]; then
        laptop_set BR2_PACKAGE_OPENSSH=y BR2_PACKAGE_OPENSSH_SERVER=y \
            BR2_PACKAGE_OPENSSH_KEY_UTILS=y BR2_PACKAGE_BEING_LAN_MAINTENANCE=y
    fi
    # The cable development build: OpenSSH's server for the cable link only
    # (LAN maintenance, which OpenSSH would switch on by default, stays out),
    # the debugging kernel fragment, and "cable" for the board scripts.
    if [ "$CABLE" = 1 ]; then
        local fragments
        fragments=$(cable_kernel_fragments)
        laptop_set BR2_PACKAGE_OPENSSH=y BR2_PACKAGE_OPENSSH_SERVER=y BR2_PACKAGE_OPENSSH_CLIENT=y \
            BR2_PACKAGE_OPENSSH_KEY_UTILS=y BR2_PACKAGE_BEING_LAN_MAINTENANCE=n BR2_PACKAGE_BEING_CABLE_DEV=y \
            "BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES=\"$fragments\"" \
            'BR2_ROOTFS_POST_SCRIPT_ARGS="laptop cable"'
    fi
    if [ "$DIAGNOSTIC" = 1 ]; then
        local fragments
        fragments=$(diagnostic_kernel_fragments)
        laptop_set BR2_PACKAGE_BEING_DIAGNOSTIC=y \
            BR2_PACKAGE_LINUX_TOOLS_PERF=y BR2_PACKAGE_LINUX_TOOLS_PERF_NEEDS_HOST_PYTHON3=y \
            BR2_PACKAGE_FIO=y BR2_PACKAGE_SMARTMONTOOLS=y \
            BR2_PACKAGE_DMIDECODE=y BR2_PACKAGE_BUSYBOX_SHOW_OTHERS=y BR2_PACKAGE_HDPARM=y \
            BR2_PACKAGE_RT_TESTS=y BR2_PACKAGE_LIBV4L=y BR2_PACKAGE_LIBV4L_UTILS=y \
            BR2_PACKAGE_MSR_TOOLS=y BR2_PACKAGE_EDID_DECODE=y \
            BR2_PACKAGE_BEING_TURBOSTAT=y BR2_GCC_ENABLE_OPENMP=y BR2_PACKAGE_STREAM_BENCHMARK=y \
            BR2_PACKAGE_ALSA_UTILS_APLAY=y BR2_PACKAGE_ALSA_UTILS_BAT=y \
            BR2_TARGET_ENABLE_ROOT_LOGIN=y BR2_TARGET_GENERIC_GETTY=y \
            'BR2_TARGET_GENERIC_GETTY_PORT="tty2"' \
            "BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES=\"$fragments\"" \
            'BR2_ROOTFS_POST_SCRIPT_ARGS="laptop diagnostic"'
    fi
    # The b43 firmware is owner-sourced and staged with lab/b43_firmware.py.
    if [ -d out/firmware-overlay/usr/lib/firmware/b43 ]; then
        laptop_set 'BR2_ROOTFS_OVERLAY="/src/out/firmware-overlay"'
    else
        echo "Note: no out/firmware-overlay/usr/lib/firmware/b43; the internal Wi-Fi will lack firmware (docs/HARDWARE.md)." >&2
    fi
    # Refuse a laptop configuration that carries lab access, and the cable
    # link anywhere but the cable tree.
    run bash -euo pipefail -c '
        cfg=$1/.config maintenance=$2 cable=$3 diagnostic=$4
        for key in BR2_PACKAGE_BEING_LAB_BODY BR2_TARGET_ENABLE_ROOT_LOGIN BR2_TARGET_GENERIC_GETTY; do
            if [ "$diagnostic" != 1 ] && grep -qx "$key=y" "$cfg"; then echo "Laptop config has lab-only $key=y" >&2; exit 1; fi
        done
        if [ "$maintenance" != 1 ] && [ "$cable" != 1 ] && grep -qx "BR2_PACKAGE_OPENSSH=y" "$cfg"; then
            echo "Laptop config has OpenSSH without BEING_LAPTOP_MAINTENANCE=1" >&2; exit 1
        fi
        if [ "$cable" != 1 ] && grep -qx "BR2_PACKAGE_BEING_CABLE_DEV=y" "$cfg"; then
            echo "Laptop config has the cable development link without BEING_LAPTOP_CABLE=1" >&2; exit 1
        fi
        if [ "$cable" = 1 ]; then
            if [ "$maintenance" = 1 ] || grep -qx "BR2_PACKAGE_BEING_LAN_MAINTENANCE=y" "$cfg"; then
                echo "The cable development build must not carry LAN maintenance" >&2; exit 1
            fi
            for key in BR2_PACKAGE_OPENSSH_SERVER BR2_PACKAGE_BEING_CABLE_DEV; do
                grep -qx "$key=y" "$cfg" || { echo "The cable development build lacks $key=y" >&2; exit 1; }
            done
            grep -q "^BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES=.*/linux-cable.config\"\$" "$cfg" ||
                { echo "The cable development build lacks the linux-cable.config kernel fragment" >&2; exit 1; }
            grep -qx "BR2_ROOTFS_POST_SCRIPT_ARGS=\"laptop cable\"" "$cfg" ||
                { echo "The cable development build does not tell the board scripts" >&2; exit 1; }
        elif grep -q "linux-cable.config\|^BR2_ROOTFS_POST_SCRIPT_ARGS=.*cable" "$cfg"; then
            echo "Laptop config has the cable kernel fragment or board option without BEING_LAPTOP_CABLE=1" >&2; exit 1
        fi
        if [ "$diagnostic" = 1 ]; then
            for key in BR2_PACKAGE_BEING_DIAGNOSTIC BR2_PACKAGE_LINUX_TOOLS_PERF \
                       BR2_PACKAGE_LINUX_TOOLS_PERF_NEEDS_HOST_PYTHON3 BR2_PACKAGE_FIO \
                       BR2_PACKAGE_SMARTMONTOOLS BR2_PACKAGE_DMIDECODE BR2_PACKAGE_HDPARM \
                       BR2_PACKAGE_RT_TESTS BR2_PACKAGE_LIBV4L_UTILS BR2_PACKAGE_MSR_TOOLS \
                       BR2_PACKAGE_EDID_DECODE BR2_PACKAGE_BEING_TURBOSTAT \
                       BR2_GCC_ENABLE_OPENMP BR2_PACKAGE_STREAM_BENCHMARK BR2_PACKAGE_ALSA_UTILS_BAT \
                       BR2_TARGET_ENABLE_ROOT_LOGIN BR2_TARGET_GENERIC_GETTY; do
                grep -qx "$key=y" "$cfg" || { echo "Diagnostic config lacks $key=y" >&2; exit 1; }
            done
            grep -q "linux-diagnostic.config" "$cfg" || { echo "Diagnostic config lacks its kernel fragment" >&2; exit 1; }
            grep -qx 'BR2_ROOTFS_POST_SCRIPT_ARGS="laptop diagnostic"' "$cfg" || exit 1
        elif grep -q "linux-diagnostic.config\|^BR2_PACKAGE_BEING_DIAGNOSTIC=y\|diagnostic" "$cfg"; then
            echo "Laptop config has diagnostic options without BEING_LAPTOP_DIAGNOSTIC=1" >&2; exit 1
        fi
    ' laptop-check "$OUT" "${BEING_LAPTOP_MAINTENANCE:-0}" "$CABLE" "$DIAGNOSTIC"
}

# Refuse a laptop tree that still holds lab-only files, then a lab boot config.
# The cable files belong to the cable tree alone, and sshd to the maintenance
# and cable trees.
laptop_verify() {
    run bash -euo pipefail -c '
        out=$1 t=$1/target bad=0 maintenance=$2 cable=$3 diagnostic=$4
        units=usr/lib/systemd/system
        cable_files="usr/share/being/cable $units/being-cable-link.service $units/being-cable-ssh.service $units/being-cable-workbench.service"
        [ -x "$t/usr/sbin/smartctl" ] || { echo "Laptop image lacks /usr/sbin/smartctl" >&2; bad=1; }
        set --
        [ "$maintenance" != 1 ] || set -- "$@" --maintenance
        [ "$cable" != 1 ] || set -- "$@" --cable
        if [ "$diagnostic" != 1 ]; then
            python3 /src/lab/candidate_parity.py laptop-check "$t" "$@" || bad=1
        fi
        if [ "$cable" = 1 ]; then
            for f in usr/sbin/sshd $cable_files usr/share/being/cable/authorized_keys usr/share/being/cable/sshd_config \
                     usr/share/being/cable/ssh_host_ed25519_key usr/share/being/cable/ssh_host_ed25519_key.pub; do
                if [ ! -e "$t/$f" ]; then echo "The cable development image lacks /$f" >&2; bad=1; fi
            done
        fi
        # A diagnostic image has no installed-grub.cfg (post-build removes it).
        grubs="$out/images/efi-part/EFI/BOOT/grub.cfg"
        [ "$diagnostic" = 1 ] || grubs="$grubs $t/usr/share/being/installed-grub.cfg"
        if grep -q "ttyS0\|^serial " $grubs; then
            echo "Laptop boot configuration still names the serial console" >&2; bad=1
        fi
        if grep -q "oops=panic\|BEING_[A-Z]*_ARGS" $grubs; then
            echo "Laptop boot configuration panics on an oops or holds an unexpanded token" >&2; bad=1
        fi
        if [ "$diagnostic" = 1 ]; then
            for f in usr/share/being/diagnostic usr/bin/perf usr/bin/llama-bench usr/bin/fio \
                     usr/sbin/smartctl usr/sbin/dmidecode sbin/hdparm usr/bin/cyclictest \
                     usr/bin/v4l2-ctl usr/sbin/rdmsr usr/bin/edid-decode usr/bin/arecord usr/bin/alsabat \
                     usr/bin/turbostat usr/bin/stream usr/share/being/stream-5.10.c; do
                [ -e "$t/$f" ] || { echo "Diagnostic image lacks /$f" >&2; bad=1; }
            done
            [ ! -e "$t/usr/sbin/being-installer" ] || { echo "Diagnostic image still carries the installer" >&2; bad=1; }
            [ ! -e "$t/usr/share/being/install" ] || { echo "Diagnostic image still carries installer payloads" >&2; bad=1; }
            [ ! -e "$t/usr/share/being/installed-rauc.conf" ] || { echo "Diagnostic image carries installed RAUC metadata" >&2; bad=1; }
            grep -qx compatible=beingos-diagnostic-never-install "$t/etc/rauc/system.conf" || {
                echo "Diagnostic image lacks its non-installable identity" >&2; bad=1;
            }
        else
            # Its own update identity, so no lab bundle can ever install on it.
            grep -qx compatible=beingos-laptop-v1 "$t/etc/rauc/system.conf" &&
                grep -qx compatible=beingos-laptop-installed-v1 "$t/usr/share/being/installed-rauc.conf" &&
                grep -qx laptop "$t/usr/share/being/variant" || {
                echo "Laptop image does not carry the laptop update identity" >&2; bad=1
            }
        fi
        if grep -qx trust=lab "$t/usr/share/being/update-trust" 2>/dev/null; then
            echo "Note: this laptop image trusts the lab key for updates (no release certificate imported). Use it for Try only." >&2
        fi
        exit $bad
    ' laptop-verify "$OUT" "${BEING_LAPTOP_MAINTENANCE:-0}" "$CABLE" "$DIAGNOSTIC"
}

# release-key DIR [DIR2], on the Mac (not in the builder): the owner's update
# signing key. RSA 4096, encrypted with a passphrase that only the owner
# types, written outside this repository. DIR2 receives a second copy (keep
# the two on different offline media). Nothing is imported automatically.
release_key() {
    local dir="${1:?usage: lab/build.sh release-key DIR [DIR2]}" second="${2:-}" ossl="" candidate
    for candidate in "${OPENSSL:-}" /opt/homebrew/opt/openssl@3/bin/openssl /usr/local/opt/openssl@3/bin/openssl openssl; do
        [ -n "$candidate" ] || continue
        if "$candidate" version 2>/dev/null | grep -q '^OpenSSL [3-9]'; then ossl="$candidate"; break; fi
    done
    [ -n "$ossl" ] || { echo "release-key needs OpenSSL 3 (set OPENSSL=/path/to/openssl)." >&2; exit 1; }
    local target repo
    repo=$(cd "$ROOT" && pwd -P)
    for target in "$dir" $second; do
        case "$target" in
            /*) ;;
            *) echo "Give the key folder as an absolute path, on the owner's own media." >&2; exit 2 ;;
        esac
        case "$target/" in
            "$ROOT/"*|"$repo/"*) echo "Refusing: $target is inside the repository. The release key must live on the owner's own media." >&2; exit 1 ;;
        esac
        mkdir -p "$target"
        target=$(cd "$target" && pwd -P)
        case "$target/" in
            "$repo/"*) echo "Refusing: $target is inside the repository (through a link). Nothing was written there." >&2; exit 1 ;;
        esac
        if [ -e "$target/pipos-release-key.pem" ] || [ -e "$target/pipos-release-cert.pem" ]; then
            echo "Refusing: $target already holds a PipOS release key or certificate." >&2; exit 1
        fi
    done
    dir=$(cd "$dir" && pwd -P)
    local work
    work=$(mktemp -d "$dir/.pipos-release.XXXXXX")
    trap 'rm -rf "$work"' EXIT
    chmod 0700 "$work"
    cat > "$work/req.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = $RELEASE_CN
[v3]
basicConstraints = critical, CA:TRUE
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
EOF
    local pass=()
    if [ -n "${BEING_RELEASE_PASS_FILE:-}" ]; then pass=(-pass "file:$BEING_RELEASE_PASS_FILE"); fi
    echo "Choose a passphrase for the PipOS release key. Write it down and keep it apart from the key."
    (umask 077; "$ossl" genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -aes-256-cbc \
        ${pass[@]+"${pass[@]}"} -out "$work/pipos-release-key.pem")
    grep -q '^-----BEGIN ENCRYPTED PRIVATE KEY-----$' "$work/pipos-release-key.pem" ||
        { echo "The key was not written encrypted; nothing kept." >&2; exit 1; }
    local passin=()
    if [ -n "${BEING_RELEASE_PASS_FILE:-}" ]; then passin=(-passin "file:$BEING_RELEASE_PASS_FILE"); fi
    # 25 years: RAUC checks the certificate at each bundle's signing time.
    "$ossl" req -new -x509 -sha256 -days 9131 -config "$work/req.cnf" \
        -key "$work/pipos-release-key.pem" ${passin[@]+"${passin[@]}"} -out "$work/pipos-release-cert.pem"
    chmod 0400 "$work/pipos-release-key.pem"
    chmod 0444 "$work/pipos-release-cert.pem"
    "$ossl" x509 -in "$work/pipos-release-cert.pem" -noout -fingerprint -sha256 > "$work/fingerprint.txt"
    cat > "$work/README.txt" <<EOF
PipOS release key: the only key a PipOS laptop installed with a release
certificate accepts updates from. Lose it (or its passphrase) and the being
can only be changed by reinstalling, which erases it. Leak it and anyone can
replace Pip's system.

  pipos-release-key.pem   private key, AES-256 encrypted with your passphrase
  pipos-release-cert.pem  certificate (public), $RELEASE_CN
  fingerprint.txt         its SHA-256 fingerprint, also shown in F2 Setup

Keep two copies on two offline USB sticks, apart from each other and apart
from the passphrase. Never copy the key into the BeingOS folder.

Import the certificate (not the key) into the builder:
  lab/build.sh release-cert /path/to/pipos-release-cert.pem
Sign an update for an installed laptop:
  lab/build.sh laptop-bundle /path/to/pipos-release-key.pem
EOF
    for name in pipos-release-key.pem pipos-release-cert.pem fingerprint.txt README.txt; do
        mv "$work/$name" "$dir/$name"
    done
    if [ -n "$second" ]; then
        second=$(cd "$second" && pwd -P)
        (umask 077; cp -p "$dir/pipos-release-key.pem" "$dir/pipos-release-cert.pem" "$dir/fingerprint.txt" "$dir/README.txt" "$second/")
        cmp -s "$dir/pipos-release-key.pem" "$second/pipos-release-key.pem" ||
            { echo "The second copy in $second does not match; check that medium." >&2; exit 1; }
    fi
    rm -rf "$work"
    trap - EXIT
    echo "Release key made in $dir${second:+ and $second}."
    cat "$dir/fingerprint.txt"
    echo "Next: lab/build.sh release-cert $dir/pipos-release-cert.pem"
}

# release-cert FILE: copy the owner's release certificate into the builder
# volume. Refuses a private key, a different subject, or silently replacing
# a different certificate (rotation needs a bundle signed by the old key).
release_cert() {
    local file="${1:?usage: lab/build.sh release-cert FILE}"
    [ -f "$file" ] || { echo "No such file: $file" >&2; exit 1; }
    if grep -q 'PRIVATE KEY' "$file"; then
        echo "Refusing: $file holds a private key. Import only pipos-release-cert.pem." >&2; exit 1
    fi
    docker_ run --rm -i -v "$VOLUME:/work" -e REPLACE="${BEING_REPLACE_RELEASE_CERT:-0}" "$IMAGE" bash -euo pipefail -c '
        dir=$1 cn=$2
        mkdir -p "$dir"
        new=$(mktemp "$dir/.cert.XXXXXX")
        trap "rm -f \"$new\"" EXIT
        cat > "$new"
        [ "$(grep -c -- "-----BEGIN CERTIFICATE-----" "$new")" = 1 ] ||
            { echo "Refusing: import exactly one release certificate." >&2; exit 1; }
        subject=$(openssl x509 -in "$new" -noout -subject -nameopt RFC2253)
        [ "$subject" = "subject=CN=$cn" ] || { echo "Refusing: the certificate is $subject, not CN=$cn" >&2; exit 1; }
        if [ -s "$dir/cert.pem" ] && ! cmp -s "$new" "$dir/cert.pem" && [ "$REPLACE" != 1 ]; then
            echo "Refusing: a different release certificate is already imported." >&2
            echo "Installed laptops trust the old one; replacing it needs a rotation bundle (BEING_REPLACE_RELEASE_CERT=1)." >&2
            exit 1
        fi
        chmod 0644 "$new"
        mv -f "$new" "$dir/cert.pem"
        openssl x509 -in "$dir/cert.pem" -noout -fingerprint -sha256
    ' release-cert "$RELEASE_DIR" "$RELEASE_CN" < "$file"
    echo "Imported. The next laptop builds trust only this certificate for updates."
}

# The real path of an existing file, links resolved, for the repository test.
real_path() {
    python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

# cable-key PUBKEY HOSTKEY: copy the cable development keys into the builder
# volume. Both stay outside this repository; only the builder volume and the
# cable images ever hold them. PUBKEY is one Ed25519 public key (its comment
# is dropped). HOSTKEY is an unencrypted OpenSSH Ed25519 private key, which
# every cable stick presents, so the Mac can pin it once.
cable_key() {
    if [ "$#" -ne 2 ]; then
        echo "usage: lab/build.sh cable-key PUBKEY HOSTKEY" >&2
        exit 2
    fi
    local pub=$1 host=$2 file repo key host_pub fingerprint
    repo=$(cd "$ROOT" && pwd -P)
    for file in "$pub" "$host"; do
        [ -f "$file" ] || { echo "No such file: $file" >&2; exit 1; }
        case "$(real_path "$file")/" in
            "$repo/"*|"$ROOT/"*) echo "Refusing: $file is inside the repository. Keep the cable keys outside it." >&2; exit 1 ;;
        esac
    done
    if grep -q 'PRIVATE KEY' "$pub"; then
        echo "Refusing: $pub holds a private key. Give the .pub file of the login key." >&2; exit 1
    fi
    key=$(PYTHONDONTWRITEBYTECODE=1 python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from mind.lab_access import public_key
try:
    print(public_key(open(sys.argv[2], encoding="ascii").read().strip()))
except (OSError, UnicodeDecodeError, ValueError) as error:
    sys.exit(f"Refusing {sys.argv[2]}: {error}")
' "$ROOT" "$pub") || exit 1
    if [ "$(head -n 1 "$host")" != "-----BEGIN OPENSSH PRIVATE KEY-----" ]; then
        echo "Refusing: $host is not an OpenSSH private key (ssh-keygen -t ed25519 -N '' makes one)." >&2; exit 1
    fi
    # Only an unencrypted key gives its public half with an empty passphrase.
    if ! host_pub=$(ssh-keygen -y -P '' -f "$host" 2>/dev/null); then
        echo "Refusing: $host is encrypted or unreadable; the stick needs an unencrypted host key (ssh-keygen -N '')." >&2
        exit 1
    fi
    host_pub=$(printf '%s\n' "$host_pub" | PYTHONDONTWRITEBYTECODE=1 python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from mind.lab_access import public_key
try:
    print(public_key(sys.stdin.read().strip()))
except ValueError:
    sys.exit("Refusing: the host key is not an Ed25519 key.")
' "$ROOT") || exit 1
    if [ "$key" = "$host_pub" ]; then
        echo "Refusing: the login key and the host key are the same key; make two." >&2; exit 1
    fi
    up
    { printf '%s\n' "$key"; cat "$host"; } | docker_ run --rm -i -v "$VOLUME:/work" "$IMAGE" bash -euo pipefail -c '
        dir=$1
        umask 077
        mkdir -p "$dir"
        chmod 0700 "$dir"
        key=$(mktemp "$dir/.authorized_key.XXXXXX")
        host=$(mktemp "$dir/.ssh_host_ed25519_key.XXXXXX")
        trap "rm -f \"$key\" \"$host\"" EXIT
        IFS= read -r line
        case "$line" in
            "ssh-ed25519 "*) printf "%s\n" "$line" > "$key" ;;
            *) echo "Refusing: not an Ed25519 public key" >&2; exit 1 ;;
        esac
        cat > "$host"
        head -n 1 "$host" | grep -qx -- "-----BEGIN OPENSSH PRIVATE KEY-----" ||
            { echo "Refusing: not an OpenSSH private key" >&2; exit 1; }
        chmod 0600 "$key" "$host"
        mv -f "$key" "$dir/authorized_key"
        mv -f "$host" "$dir/ssh_host_ed25519_key"
    ' cable-key "$CABLE_DIR"
    fingerprint=$(printf '%s\n' "$host_pub" | ssh-keygen -l -f - 2>/dev/null || true)
    echo "Imported the cable keys. The next BEING_LAPTOP_CABLE=1 build carries them."
    echo "Host key: ${fingerprint:-$host_pub}"
    echo "Its pin for ~/.ssh/known_hosts_pipos (lab/cable.sh ssh-config writes it):"
    printf 'pip,169.254.77.1,fe80::77:1 %s\n' "$host_pub"
}

# Sign the laptop rootfs for installed laptops. The optional rotation PEM is
# the public certificate of the old signer. The root always trusts the
# imported certificate, which is the replacement certificate during rotation.
# The encrypted key and the passphrase reach the builder only on stdin, into
# a RAM file inside a throwaway container; neither is written to the volume.
laptop_bundle() {
    local key="${1:?usage: lab/build.sh laptop-bundle KEY [VERSION]}" version="${2:-}" rotation_pem="${3:-}"
    if [ "$CABLE" = 1 ]; then
        echo "Refusing: a cable development build is never signed as an update (unset BEING_LAPTOP_CABLE)." >&2
        exit 1
    fi
    [ -f "$key" ] || { echo "No such key file: $key" >&2; exit 1; }
    key=$(real_path "$key")
    case "$key" in
        "$(cd "$ROOT" && pwd -P)/"*) echo "Refusing: the release key must not be inside the repository." >&2; exit 1 ;;
    esac
    [ -n "$version" ] || version="laptop-$(date -u +%Y%m%d%H%M)"
    printf '%s' "$version" | LC_ALL=C grep -Eq '^[A-Za-z0-9._+-]{1,64}$' ||
        { echo "The version may hold only letters, digits and . _ + -" >&2; exit 2; }
    local output=beingos-laptop-update.raucb path
    if [ -n "$rotation_pem" ]; then
        output="beingos-laptop-key-rotation-$version.raucb"
        for path in "out/images/$output" "out/images/$output.provenance.json"; do
            if [ -e "$path" ] || [ -L "$path" ]; then
                echo "A rotation bundle or receipt already uses this version. Choose a new version." >&2
                exit 1
            fi
        done
    fi
    local passphrase prompt="Release key passphrase: "
    if [ -n "$rotation_pem" ]; then
        prompt="Current release key passphrase: "
    fi
    if [ -n "${BEING_RELEASE_PASS_FILE:-}" ]; then
        IFS= read -r passphrase < "$BEING_RELEASE_PASS_FILE"
    else
        IFS= read -rs -p "$prompt" passphrase < /dev/tty; echo >&2
    fi
    mkdir -p out/images
    { printf '%s\n' "$passphrase"; cat "$key"; } | docker_ run --rm -i \
        -e "BEING_ROTATION_CERT=$rotation_pem" -v "$VOLUME:/work" -v "$ROOT:/src" "$IMAGE" bash -euo pipefail -c '
        out=$1 dir=$2 version=$3 output=$4 cn=$5
        IFS= read -r passphrase
        key_dir=$(mktemp -d /dev/shm/pipos-signing.XXXXXX)
        trap "rm -rf \"$key_dir\"" EXIT
        work=$(mktemp -d "$out/.pipos-bundle.XXXXXX")
        trap "rm -rf \"$key_dir\" \"$work\"" EXIT
        (umask 077; cat > "$key_dir/key.pem")
        export PATH="$out/host/bin:$out/host/sbin:$PATH"
        image="$out/images/rootfs.squashfs"
        cert="$work/installed-cert.pem"
        test -f "$image" || { echo "Build the laptop image first (lab/build.sh laptop-try)." >&2; exit 1; }
        test -s "$dir/cert.pem" || { echo "Import the release certificate first (lab/build.sh release-cert)." >&2; exit 1; }
        cp "$dir/cert.pem" "$cert"
        mkdir "$work/payload"
        cp "$image" "$work/payload/rootfs.img"
        image="$work/payload/rootfs.img"
        [ "$(grep -c -- "-----BEGIN CERTIFICATE-----" "$cert")" = 1 ] ||
            { echo "Expected exactly one imported release certificate." >&2; exit 1; }
        signer="$cert"
        if [ -n "$BEING_ROTATION_CERT" ]; then
            signer="$work/previous-cert.pem"
            printf "%s\n" "$BEING_ROTATION_CERT" > "$signer"
            for certificate in "$signer" "$cert"; do
                subject=$(openssl x509 -in "$certificate" -noout -subject -nameopt RFC2253)
                [ "$subject" = "subject=CN=$cn" ] ||
                    { echo "Both rotation certificates must identify the PipOS laptop release." >&2; exit 1; }
            done
            openssl verify -CAfile "$cert" "$cert" >/dev/null
            openssl x509 -in "$cert" -noout -pubkey > "$work/replacement.pub"
        fi
        RAUC_KEY_PASSPHRASE="$passphrase" openssl pkey -in "$key_dir/key.pem" -passin env:RAUC_KEY_PASSPHRASE -pubout > "$work/key.pub"
        openssl x509 -in "$signer" -noout -pubkey > "$work/signer.pub"
        cmp -s "$work/key.pub" "$work/signer.pub" ||
            { echo "The supplied private key does not match the signing certificate." >&2; exit 1; }
        if [ -n "$BEING_ROTATION_CERT" ] && cmp -s "$work/signer.pub" "$work/replacement.pub"; then
            echo "A rotation needs a distinct replacement key." >&2; exit 1
        fi
        # Only a laptop rootfs that trusts the imported certificate is signed.
        unsquashfs -cat "$image" usr/share/being/installed-rauc.conf | grep -qx compatible=beingos-laptop-installed-v1 ||
            { echo "This rootfs is not a laptop image." >&2; exit 1; }
        unsquashfs -cat "$image" usr/share/being/update-trust | grep -qx trust=release ||
            { echo "This laptop rootfs trusts the lab key; rebuild it after release-cert." >&2; exit 1; }
        # A cable development rootfs is never signed, whatever tree it is in.
        unsquashfs -l -no-progress "$image" > "$work/listing"
        if grep -Eq "^squashfs-root/usr/share/being/cable(/|\$)" "$work/listing"; then
            echo "Refusing: this rootfs holds the cable development link; it is never signed as an update." >&2; exit 1
        fi
        unsquashfs -cat "$image" etc/rauc/keyring.pem > "$work/keyring.pem"
        cmp -s "$work/keyring.pem" "$cert" || { echo "This rootfs trusts a different certificate." >&2; exit 1; }
        build=$(unsquashfs -cat "$image" etc/being/build-id)
        source=$(unsquashfs -cat "$image" etc/being/source-id)
        for id in "$build" "$source"; do
            printf "%s\n" "$id" | grep -Eqx "[a-f0-9]{64}" &&
                test "${#id}" -eq 64 ||
                { echo "The laptop payload needs valid build and source IDs." >&2; exit 1; }
        done
        cat > "$work/payload/manifest.raucm" <<EOF
[update]
compatible=beingos-laptop-installed-v1
version=$version
build=$build

[bundle]
format=verity

[image.rootfs]
filename=rootfs.img
EOF
        bundle="$work/$output"
        RAUC_KEY_PASSPHRASE="$passphrase" rauc bundle --cert="$signer" --key="$key_dir/key.pem" \
            "$work/payload" "$bundle"
        rauc info --keyring="$signer" "$bundle" >/dev/null
        if [ -n "$BEING_ROTATION_CERT" ]; then
            status=0
            rauc info --keyring="$cert" "$bundle" >/dev/null 2>&1 || status=$?
            # RAUC info returns 1 when check_bundle fails. A terminated or
            # missing verifier has a different status.
            if [ "$status" != 1 ]; then
                echo "Could not establish that RAUC rejects the rotation bridge with the replacement certificate." >&2
                exit 1
            fi
        fi
        root_hash=$(sha256sum "$image" | cut -d" " -f1)
        squashfs_hash=$(PYTHONPATH=/src PYTHONDONTWRITEBYTECODE=1 python3 -c "
import sys
from pathlib import Path
from lab.hardware_image import copy_partition
path = Path(sys.argv[1])
print(copy_partition(path, dict(Offset=0, Size=path.stat().st_size, Path=path.name), None, squashfs=True))
" "$image")
        rm -f "$image"
        # Extract through RAUC with signature verification enabled. Compare
        # what a receiver gets with the filesystem inspected before signing.
        rauc extract --keyring="$signer" "$bundle" "$work/extracted"
        extracted_hash=$(sha256sum "$work/extracted/rootfs.img" | cut -d" " -f1)
        test "$extracted_hash" = "$root_hash" ||
            { echo "The signed bundle contains a different root filesystem." >&2; exit 1; }
        bundle_hash=$(sha256sum "$bundle" | cut -d" " -f1)
        openssl x509 -in "$signer" -outform DER > "$work/signer.der"
        signer_hash=$(sha256sum "$work/signer.der" | cut -d" " -f1)
        result="/src/out/images/$output"
        publish=$(mktemp -d /src/out/images/.pipos-output.XXXXXX)
        trap "rm -rf \"$key_dir\" \"$work\" \"$publish\"" EXIT
        {
            printf "%s\n" \
                "{" \
                "  \"channel\": \"beingos-laptop-installed-v1\"," \
                "  \"version\": \"$version\"," \
                "  \"source_id\": \"$source\"," \
                "  \"build_id\": \"$build\"," \
                "  \"rootfs_sha256\": \"$root_hash\"," \
                "  \"payload_verified_at_signing\": true," \
                "  \"squashfs_bytes_sha256\": \"$squashfs_hash\"," \
                "  \"bundle_sha256\": \"$bundle_hash\","
            if [ -n "$BEING_ROTATION_CERT" ]; then
                openssl x509 -in "$cert" -outform DER > "$work/replacement.der"
                replacement_hash=$(sha256sum "$work/replacement.der" | cut -d" " -f1)
                printf "%s\n" "  \"rotation\": {\"from_der_sha256\": \"$signer_hash\", \"to_der_sha256\": \"$replacement_hash\"},"
            fi
            printf "%s\n" "  \"signer_der_sha256\": \"$signer_hash\"" "}"
        } > "$publish/receipt.json"
        cp "$bundle" "$publish/$output"
        exec 9>"$dir/.laptop-bundle-output.lock"
        flock -n 9 || { echo "Another laptop bundle is being published." >&2; exit 1; }
        if [ -n "$BEING_ROTATION_CERT" ]; then
            for path in "$result" "$result.provenance.json"; do
                if [ -e "$path" ] || [ -L "$path" ]; then
                    echo "A rotation bundle or receipt already uses this version." >&2; exit 1
                fi
            done
        fi
        mv -f "$publish/$output" "$result"
        mv -f "$publish/receipt.json" "$result.provenance.json"
    ' laptop-bundle "$OUT" "$RELEASE_DIR" "$version" "$output" "$RELEASE_CN"
    ls -lh "out/images/$output"{,.provenance.json}
}

laptop_rotation_bundle() {
    if [ "$#" -ne 3 ] || [ -z "$3" ]; then
        echo "usage: lab/build.sh laptop-rotation-bundle OLD_KEY OLD_CERT VERSION" >&2
        exit 2
    fi
    laptop_select
    if [ "$CABLE" = 1 ]; then
        echo "Refusing: a cable development build is never signed as an update." >&2
        exit 1
    fi
    local previous_pem
    if [ ! -f "$2" ] || grep -q "PRIVATE KEY" "$2" ||
        [ "$(grep -c -- '-----BEGIN CERTIFICATE-----' "$2")" != 1 ]; then
        echo "Provide exactly one public certificate for the installed signing key." >&2
        exit 1
    fi
    previous_pem=$(openssl x509 -in "$2" -outform PEM)
    up
    laptop_bundle "$1" "$3" "$previous_pem"
}

# BEING_TRY_BENCH=1 makes the Try commands build a bench stick. run() does
# not forward it: only try-image.sh reads it, so no installed image or
# update bundle ever carries the bench quants.
TRY_BENCH="${BEING_TRY_BENCH:-0}"
check_try_bench() {
    case "$TRY_BENCH" in
        0|1) ;;
        *) echo "BEING_TRY_BENCH must be 0 or 1" >&2; exit 2 ;;
    esac
}
# A bench stick is never mistaken for (or overwrites) the ordinary Try media.
try_bench_suffix() {
    if [ "$TRY_BENCH" = 1 ]; then printf %s -bench; fi
}

cmd="${1:-build}"
[ $# -gt 0 ] && shift
case "$cmd" in
    candidate)
        if [ "${BEING_LAPTOP_CABLE:-0}" != 0 ] || [ "${BEING_LAPTOP_DIAGNOSTIC:-0}" != 0 ]; then
            echo "Refusing: a release candidate never carries cable or diagnostic development tooling." >&2
            exit 2
        fi
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
        check_fast_rebuild
        take_source_id
        up
        # Apply the defconfig when there is no configuration yet, or it changed.
        run bash -c "[ -f $OUT/.config ] && [ $OUT/.config -nt /src/os/configs/$DEFCONFIG ]" || br_make "$DEFCONFIG"
        check_space
        refresh_python_extensions
        refresh_stale
        forget_build_source_id
        br_make_targets "$@"
        record_build_source_id
        mkdir -p out/images
        unstamp_image out/images/beingos.img
        run bash -c "cp --sparse=always $OUT/images/beingos.img /src/out/images/beingos.img.new && mv -f /src/out/images/beingos.img.new /src/out/images/beingos.img"
        stamp_image out/images/beingos.img
        ls -lh out/images/beingos.img
        ;;
    try-image|try-screen)
        check_try_bench
        check_fast_rebuild
        take_source_id
        up
        run bash -c "[ -f $OUT/.config ] && [ $OUT/.config -nt /src/os/configs/$DEFCONFIG ]" || br_make "$DEFCONFIG"
        check_space
        refresh_python_extensions
        refresh_stale
        forget_build_source_id
        # Two steps: with per-package directories a parallel "being-rebuild all"
        # can assemble the image before the rebuild has installed.
        being_rebuild_all
        record_build_source_id
        mkdir -p out/images
        screen=${BEING_TRY_SCREEN:-0}
        if [ "$cmd" = try-screen ]; then
            screen=1
        fi
        case "$screen" in
            0) output=beingos-try ;;
            1) output=beingos-try-screen ;;
            *) echo "BEING_TRY_SCREEN must be 0 or 1" >&2; exit 2 ;;
        esac
        output="$output$(try_bench_suffix).img"
        unstamp_image "out/images/$output"
        # The builder sees only the variables run() names: pass the bench
        # choice to try-image.sh explicitly.
        run env BEING_TRY_SCREEN="$screen" BEING_TRY_BENCH="$TRY_BENCH" sh /src/os/board/beingos/try-image.sh \
            "$OUT/host" "$OUT/images/efi-part/EFI/BOOT/bootx64.efi" \
            "$OUT/images/rootfs.squashfs" "$OUT/images/models.ext4" /src/out/images
        stamp_image "out/images/$output"
        ls -lh "out/images/$output"
        ;;
    laptop-config)
        laptop_select
        up
        laptop_config
        ;;
    laptop)
        check_fast_rebuild
        take_source_id
        laptop_select
        up
        laptop_config
        check_space
        refresh_python_extensions
        refresh_stale
        forget_build_source_id
        # The being package is a local source tree: rebuild it so the image
        # always holds the files that were tested.
        if [ "$#" -eq 0 ]; then
            # Two steps: with per-package directories a parallel "being-rebuild all"
            # can assemble the image before the rebuild has installed.
            being_rebuild_all
        else
            br_make_targets "$@"
        fi
        laptop_verify
        record_build_source_id
        mkdir -p out/images
        unstamp_image "out/images/$LAPTOP_NAME.img"
        run bash -c "cp --sparse=always $OUT/images/beingos.img /src/out/images/$LAPTOP_NAME.img.new && mv -f /src/out/images/$LAPTOP_NAME.img.new /src/out/images/$LAPTOP_NAME.img"
        stamp_image "out/images/$LAPTOP_NAME.img"
        ls -lh "out/images/$LAPTOP_NAME.img"
        ;;
    laptop-try)
        check_try_bench
        check_fast_rebuild
        take_source_id
        laptop_select
        up
        laptop_config
        check_space
        refresh_python_extensions
        refresh_stale
        forget_build_source_id
        # Two steps: with per-package directories a parallel "being-rebuild all"
        # can assemble the image before the rebuild has installed.
        being_rebuild_all
        laptop_verify
        record_build_source_id
        mkdir -p out/images
        unstamp_image "out/images/$LAPTOP_NAME-try$(try_bench_suffix).img"
        # The laptop has no serial line, so its Try media always has the screen menu.
        run bash -euo pipefail -c '
            stage=$(mktemp -d /src/out/images/.laptop-try.XXXXXX)
            trap "rm -rf \"\$stage\"" EXIT
            BEING_TRY_SCREEN=1 BEING_TRY_LAPTOP=1 BEING_TRY_BENCH="$3" BEING_TRY_CABLE="$5" \
                BEING_TRY_DIAGNOSTIC="$6" \
                sh /src/os/board/beingos/try-image.sh \
                "$1/host" "$1/images/efi-part/EFI/BOOT/bootx64.efi" \
                "$1/images/rootfs.squashfs" "$1/images/models.ext4" "$stage"
            source=beingos-laptop-try$4.img
            [ "$6" != 1 ] || source=beingos-laptop-diagnostic-try$4.img
            mv -f "$stage/$source" "/src/out/images/$2-try$4.img"
        ' laptop-try "$OUT" "$LAPTOP_NAME" "$TRY_BENCH" "$(try_bench_suffix)" "$CABLE" "$DIAGNOSTIC"
        stamp_image "out/images/$LAPTOP_NAME-try$(try_bench_suffix).img"
        ls -lh "out/images/$LAPTOP_NAME-try$(try_bench_suffix).img"
        ;;
    laptop-make)
        laptop_select
        up
        # A hand-run make can change the images: the record of the source
        # they came from no longer holds (lab/make-iso.sh then stamps none).
        forget_build_source_id
        br_make "$@"
        ;;
    release-key)
        release_key "$@"
        ;;
    release-cert)
        up
        release_cert "$@"
        ;;
    cable-key)
        cable_key "$@"
        ;;
    laptop-bundle)
        if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
            echo "usage: lab/build.sh laptop-bundle KEY [VERSION]" >&2
            exit 2
        fi
        laptop_select
        up
        laptop_bundle "$@"
        ;;
    laptop-rotation-bundle)
        laptop_rotation_bundle "$@"
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
        forget_build_source_id
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
