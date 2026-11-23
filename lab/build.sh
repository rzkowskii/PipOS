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
                   (every entry runs bench mode; /models/bench holds out/models/bench),
                   named with a -bench suffix, e.g. beingos-laptop-try-bench.img.
  laptop-config    re-apply beingos_laptop_defconfig in the laptop output tree
  laptop-make ARGS...
                   run make in the laptop output tree
                   With BEING_LAPTOP_MAINTENANCE=1 the laptop commands use a separate
                   tree with owner-enrolled LAN maintenance (OpenSSH) built in, and
                   name their images beingos-laptop-maintenance*.img.
  release-key DIR [DIR2]
                   on this Mac: make the owner's passphrase-protected release key and
                   certificate in DIR (outside this repository), copied to DIR2 too
  release-cert FILE
                   import the release certificate (never the key) into the builder;
                   laptop images then trust only it for updates
  laptop-bundle KEY [VERSION]
                   sign the laptop rootfs as an update for installed laptops, with the
                   owner's release key (asks for its passphrase)
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
        "$IMAGE" "$@"
}

# The owner's release certificate, once imported (release-cert). Only the
# certificate is ever inside the builder; the key stays on the owner's media.
RELEASE_DIR=/work/beingos-release
RELEASE_CN="PipOS Laptop Release"

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

# Configuration changes Buildroot does not notice by itself. A new kernel
# patch needs a fresh kernel source tree, and systemd must be configured
# again to pick up libseccomp. Kernel option changes are noticed (the
# config files are make dependencies).
refresh_stale() {
    local targets
    targets=$(run bash -c '
        o=$1 out=
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
        echo $out
    ' refresh-stale "$OUT")
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
        if grep -q "oops=panic\|BEING_[A-Z]*_ARGS" "$1/images/efi-part/EFI/BOOT/grub.cfg" "$t/usr/share/being/installed-grub.cfg"; then
            echo "Laptop boot configuration panics on an oops or holds an unexpanded token" >&2; bad=1
        fi
        # Its own update identity, so no lab bundle can ever install on it.
        grep -qx compatible=beingos-laptop-v1 "$t/etc/rauc/system.conf" &&
            grep -qx compatible=beingos-laptop-installed-v1 "$t/usr/share/being/installed-rauc.conf" &&
            grep -qx laptop "$t/usr/share/being/variant" || {
            echo "Laptop image does not carry the laptop update identity" >&2; bad=1
        }
        if grep -qx trust=lab "$t/usr/share/being/update-trust" 2>/dev/null; then
            echo "Note: this laptop image trusts the lab key for updates (no release certificate imported). Use it for Try only." >&2
        fi
        exit $bad
    ' laptop-verify "$OUT" "${BEING_LAPTOP_MAINTENANCE:-0}"
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

# laptop-bundle KEY [VERSION]: sign the laptop rootfs for installed laptops.
# The encrypted key and the passphrase reach the builder only on stdin, into
# a RAM file inside a throwaway container; neither is written to the volume.
laptop_bundle() {
    local key="${1:?usage: lab/build.sh laptop-bundle KEY [VERSION]}" version="${2:-}"
    [ -f "$key" ] || { echo "No such key file: $key" >&2; exit 1; }
    case "$(cd "$(dirname "$key")" && pwd -P)/" in
        "$(cd "$ROOT" && pwd -P)/"*) echo "Refusing: the release key must not be inside the repository." >&2; exit 1 ;;
    esac
    [ -n "$version" ] || version="laptop-$(date -u +%Y%m%d%H%M)"
    printf '%s' "$version" | LC_ALL=C grep -Eq '^[A-Za-z0-9._+-]{1,64}$' ||
        { echo "The version may hold only letters, digits and . _ + -" >&2; exit 2; }
    local passphrase
    if [ -n "${BEING_RELEASE_PASS_FILE:-}" ]; then
        IFS= read -r passphrase < "$BEING_RELEASE_PASS_FILE"
    else
        IFS= read -rs -p "Release key passphrase: " passphrase < /dev/tty; echo >&2
    fi
    mkdir -p out/images
    { printf '%s\n' "$passphrase"; cat "$key"; } | docker_ run --rm -i \
        -v "$VOLUME:/work" -v "$ROOT:/src" "$IMAGE" bash -euo pipefail -c '
        out=$1 dir=$2 version=$3
        IFS= read -r passphrase
        work=$(mktemp -d /dev/shm/pipos-bundle.XXXXXX 2>/dev/null || mktemp -d)
        trap "rm -rf \"$work\"" EXIT
        (umask 077; cat > "$work/key.pem")
        export PATH="$out/host/bin:$out/host/sbin:$PATH"
        image="$out/images/rootfs.squashfs"
        cert="$dir/cert.pem"
        test -f "$image" || { echo "Build the laptop image first (lab/build.sh laptop-try)." >&2; exit 1; }
        test -s "$cert" || { echo "Import the release certificate first (lab/build.sh release-cert)." >&2; exit 1; }
        # The key must match the certificate the image trusts.
        RAUC_KEY_PASSPHRASE="$passphrase" openssl pkey -in "$work/key.pem" -passin env:RAUC_KEY_PASSPHRASE -pubout > "$work/key.pub"
        openssl x509 -in "$cert" -noout -pubkey > "$work/cert.pub"
        cmp -s "$work/key.pub" "$work/cert.pub" || { echo "This key does not belong to the imported release certificate." >&2; exit 1; }
        # Only a laptop rootfs that trusts this certificate is signed.
        unsquashfs -cat "$image" usr/share/being/installed-rauc.conf | grep -qx compatible=beingos-laptop-installed-v1 ||
            { echo "This rootfs is not a laptop image." >&2; exit 1; }
        unsquashfs -cat "$image" usr/share/being/update-trust | grep -qx trust=release ||
            { echo "This laptop rootfs trusts the lab key; rebuild it after release-cert." >&2; exit 1; }
        unsquashfs -cat "$image" etc/rauc/keyring.pem > "$work/keyring.pem"
        cmp -s "$work/keyring.pem" "$cert" || { echo "This rootfs trusts a different certificate." >&2; exit 1; }
        build=$(unsquashfs -cat "$image" etc/being/build-id 2>/dev/null | head -c 64 || true)
        mkdir "$work/payload"
        cp "$image" "$work/payload/rootfs.img"
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
        RAUC_KEY_PASSPHRASE="$passphrase" rauc bundle --cert="$cert" --key="$work/key.pem" \
            "$work/payload" "$work/beingos-laptop-update.raucb"
        rauc info --keyring="$cert" "$work/beingos-laptop-update.raucb" >/dev/null
        cp "$work/beingos-laptop-update.raucb" /src/out/images/.beingos-laptop-update.raucb.new
        mv -f /src/out/images/.beingos-laptop-update.raucb.new /src/out/images/beingos-laptop-update.raucb
    ' laptop-bundle "$OUT" "$RELEASE_DIR" "$version"
    ls -lh out/images/beingos-laptop-update.raucb
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
        check_space
        refresh_python_ssl
        refresh_stale
        br_make "$@"
        mkdir -p out/images
        run bash -c "cp --sparse=always $OUT/images/beingos.img /src/out/images/beingos.img.new && mv -f /src/out/images/beingos.img.new /src/out/images/beingos.img"
        ls -lh out/images/beingos.img
        ;;
    try-image|try-screen)
        check_try_bench
        up
        run bash -c "[ -f $OUT/.config ] && [ $OUT/.config -nt /src/os/configs/$DEFCONFIG ]" || br_make "$DEFCONFIG"
        check_space
        refresh_python_ssl
        refresh_stale
        # Two steps: with per-package directories a parallel "being-rebuild all"
        # can assemble the image before the rebuild has installed.
        br_make being-rebuild
        br_make all
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
        # The builder sees only the variables run() names: pass the bench
        # choice to try-image.sh explicitly.
        run env BEING_TRY_SCREEN="$screen" BEING_TRY_BENCH="$TRY_BENCH" sh /src/os/board/beingos/try-image.sh \
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
        check_space
        refresh_python_ssl
        refresh_stale
        # The being package is a local source tree: rebuild it so the image
        # always holds the files that were tested.
        if [ "$#" -eq 0 ]; then
            # Two steps: with per-package directories a parallel "being-rebuild all"
            # can assemble the image before the rebuild has installed.
            br_make being-rebuild
            br_make all
        else
            br_make "$@"
        fi
        laptop_verify
        mkdir -p out/images
        run bash -c "cp --sparse=always $OUT/images/beingos.img /src/out/images/$LAPTOP_NAME.img.new && mv -f /src/out/images/$LAPTOP_NAME.img.new /src/out/images/$LAPTOP_NAME.img"
        ls -lh "out/images/$LAPTOP_NAME.img"
        ;;
    laptop-try)
        check_try_bench
        laptop_select
        up
        laptop_config
        check_space
        refresh_python_ssl
        refresh_stale
        # Two steps: with per-package directories a parallel "being-rebuild all"
        # can assemble the image before the rebuild has installed.
        br_make being-rebuild
        br_make all
        laptop_verify
        mkdir -p out/images
        # The laptop has no serial line, so its Try media always has the screen menu.
        run bash -euo pipefail -c '
            stage=$(mktemp -d /src/out/images/.laptop-try.XXXXXX)
            trap "rm -rf \"\$stage\"" EXIT
            BEING_TRY_SCREEN=1 BEING_TRY_LAPTOP=1 BEING_TRY_BENCH="$3" sh /src/os/board/beingos/try-image.sh \
                "$1/host" "$1/images/efi-part/EFI/BOOT/bootx64.efi" \
                "$1/images/rootfs.squashfs" "$1/images/models.ext4" "$stage"
            mv -f "$stage/beingos-laptop-try$4.img" "/src/out/images/$2-try$4.img"
        ' laptop-try "$OUT" "$LAPTOP_NAME" "$TRY_BENCH" "$(try_bench_suffix)"
        ls -lh "out/images/$LAPTOP_NAME-try$(try_bench_suffix).img"
        ;;
    laptop-make)
        laptop_select
        up
        br_make "$@"
        ;;
    release-key)
        release_key "$@"
        ;;
    release-cert)
        up
        release_cert "$@"
        ;;
    laptop-bundle)
        laptop_select
        up
        laptop_bundle "$@"
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
