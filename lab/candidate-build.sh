# Sourced by lab/build.sh. The Linux build also runs this file inside a
# disposable container, with /src read-only and /release as its only output.
candidate_artifacts() {
    local buildroot_version=$1 tar_sha=$2 defconfig=$3 lane=$4
    local work=/tmp/beingos-candidate output=/tmp/beingos-candidate/output
    local archive="buildroot-$buildroot_version.tar.xz"
    for tool in mkfs.ext4 tune2fs; do
        if ! command -v "$tool" >/dev/null; then
            echo "Missing builder tool: $tool. Rebuild lab/builder before compiling." >&2
            return 1
        fi
    done
    local tree
    if [ "$lane" = cache-overlay ]; then
        tree="/work/buildroot-$buildroot_version"
        output=/work/output/x86_64
        test -s "$output/.config" && test -x "$output/host/bin/x86_64-buildroot-linux-gnu-gcc"
        test -d "$tree"
        cp "$output/.config" /release/buildroot-original.config
    else
        mkdir "$work"
        cd "$work"
        if [ -f "$BR2_DL_DIR/$archive" ]; then
            cp "$BR2_DL_DIR/$archive" "$archive"
        else
            curl --fail --location --retry 2 --output "$archive" "https://buildroot.org/downloads/$archive"
        fi
        echo "$tar_sha  $archive" | sha256sum --check -
        tar -xf "$archive"
        tree="$work/buildroot-$buildroot_version"
    fi
    local -a buildroot=(make -C "$tree" "O=$output" BR2_EXTERNAL=/src/os BR2_JLEVEL=4)
    "${buildroot[@]}" "$defconfig"
    cp "$output/.config" /release/buildroot.config
    if [ "$lane" = cache-overlay ]; then
        # Rebuild local source, reapply the kernel config and re-detect Python's
        # TLS modules. dosfstools installs its selected tools (fsck.fat for the
        # boot-state volume) only at install time, so reapply it as well. The
        # toolchain and other upstream package builds stay in cache.
        "${buildroot[@]}" libopenssl-reconfigure linux-reconfigure python3-reconfigure dosfstools-reconfigure \
            cortex-reconfigure being-rebuild
    fi
    "${buildroot[@]}" all

    local images="$output/images"
    cp --sparse=always "$images/beingos.img" /release/beingos.img
    BEING_TRY_SCREEN=1 sh /src/os/board/beingos/try-image.sh "$output/host" "$images/efi-part/EFI/BOOT/bootx64.efi" \
        "$images/rootfs.squashfs" "$images/models.ext4" /release
    local mode
    for mode in bundle installed-bundle test-media; do
        bash /src/lab/updates.sh "$output" "$mode" /release
    done
    PATH="$output/host/bin:$output/host/sbin:$PATH" sh /src/lab/installed-update-media.sh \
        /release/beingos-installed.raucb /release/beingos.raucb /release/beingos-installed-update-qa.img
}

build_candidate() (
    local lane=clean
    if [ "${2:-}" = --cache-overlay ]; then lane=cache-overlay; fi
    if [ "$#" -lt 1 ] || [ "$#" -gt 2 ] || { [ "$#" -eq 2 ] && [ "$lane" = clean ]; } ||
       ! [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
        echo 'Usage: lab/build.sh candidate NAME [--cache-overlay]' >&2
        exit 2
    fi
    local dest="$ROOT/out/candidates/$1"
    local frozen="$dest/source" started_vm=no overlay_volume=
    command -v 7zz >/dev/null || command -v 7z >/dev/null || {
        echo '7-Zip is required to verify the models on the built disks.' >&2
        exit 1
    }
    test -s out/models/reflex.gguf && test -s out/models/manifest.json || {
        echo 'Prepare the model with lab/build.sh model FILE.gguf first.' >&2
        exit 1
    }
    mkdir -p "$ROOT/out/candidates"
    mkdir "$dest" # An existing name belongs to an earlier run.
    mkdir "$frozen" "$dest/media" "$dest/scope"
    cp DESIGN-AND-PLAN.md docs/DESIGN-COVERAGE.md docs/USB-READINESS.md "$dest/scope/"

    # Copy exactly the inputs we hashed. An edit during copying fails the
    # second comparison. Later work in the shared checkout is independent.
    python3 -m lab.candidate_manifest snapshot --root "$ROOT" --output "$dest/source.json"
    python3 -c 'import json, os, sys
with open(sys.argv[1]) as source:
    for path in json.load(source)["files"]:
        sys.stdout.buffer.write(os.fsencode(path) + b"\0")' "$dest/source.json" |
        rsync -a --from0 --files-from=- "$ROOT/" "$frozen/"
    (
        cd "$frozen"
        python3 -m lab.candidate_manifest snapshot --root "$frozen" --output "$dest/frozen.json"
    )
    cmp "$dest/source.json" "$dest/frozen.json"

    cleanup_candidate() {
        if [ -s "$dest/container.id" ]; then
            docker_ rm -f "$(cat "$dest/container.id")" >/dev/null 2>&1 || true
        fi
        if [ -n "$overlay_volume" ]; then
            docker_ volume rm "$overlay_volume" >/dev/null
        fi
        if [ "$started_vm" = yes ] && colima status --profile "$PROFILE" >/dev/null 2>&1; then
            local active
            if active=$(docker_ ps -q) && [ -z "$active" ]; then
                colima stop --profile "$PROFILE"
            else
                echo 'The builder is still in use. The remaining owner must stop the profile.' >&2
            fi
        fi
    }
    trap cleanup_candidate EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP
    if ! colima status --profile "$PROFILE" >/dev/null 2>&1; then started_vm=yes; fi
    up
    docker_ build --iidfile "$dest/builder-id" "$frozen/lab/builder"
    # Keep compiler intermediates in the disposable container. Timebox the
    # compile and packaging so an unattended failed build cannot run forever.
    local -a run_options=(--rm --init --cidfile "$dest/container.id"
        -v "$frozen:/src:ro" -v "$dest/media:/release"
        -e BR2_DL_DIR=/work/dl -e BR2_CCACHE_DIR=/work/ccache \
        -e BEINGOS_LAB_KEYS=/work/beingos-keys -e PYTHONDONTWRITEBYTECODE=1)
    local builder_id
    builder_id=$(cat "$dest/builder-id")
    if [ "$lane" = cache-overlay ]; then
        overlay_volume=$(docker_ volume create "beingos-candidate-$$")
        docker_ run "${run_options[@]}" --user 0:0 --cap-add SYS_ADMIN --security-opt apparmor=unconfined \
            -v "$VOLUME:/base:ro" -v "$overlay_volume:/scratch" "$builder_id" \
            timeout --kill-after=60s 2h bash -euo pipefail -c '
                mkdir /scratch/upper /scratch/work
                chown --reference=/base /scratch/upper
                LIBMOUNT_FORCE_MOUNT2=always mount -t overlay beingos-candidate \
                    -o lowerdir=/base,upperdir=/scratch/upper,workdir=/scratch/work /work
                trap "umount /work" EXIT
                runuser -u builder -- bash /src/lab/candidate-build.sh "$@"
            ' candidate-cache "$BUILDROOT_VERSION" "$BUILDROOT_SHA256" "$DEFCONFIG" "$lane" \
            2>&1 | tee "$dest/build.log"
    else
        docker_ run "${run_options[@]}" -v "$VOLUME:/work" "$builder_id" \
            timeout --kill-after=60s 6h bash /src/lab/candidate-build.sh \
            "$BUILDROOT_VERSION" "$BUILDROOT_SHA256" "$DEFCONFIG" "$lane" 2>&1 | tee "$dest/build.log"
    fi

    cd "$frozen"
    local -a cache_evidence=()
    if [ "$lane" = cache-overlay ]; then
        cache_evidence=(--extra "prior_buildroot_config=$dest/media/buildroot-original.config")
    fi
    python3 -m lab.candidate_models --source "$dest/source.json" \
        --factory "$dest/media/beingos.img" --try-image "$dest/media/beingos-try-screen.img" \
        > "$dest/packaged-models.json"
    python3 -m lab.candidate_manifest seal --root "$frozen" --source "$dest/source.json" --model-audit "$dest/packaged-models.json" \
        --factory "$dest/media/beingos.img" --try-image "$dest/media/beingos-try-screen.img" \
        --factory-bundle "$dest/media/beingos.raucb" --installed-bundle "$dest/media/beingos-installed.raucb" \
        --update-fixtures "$dest/media/beingos-update-fixtures.img" \
        --extra "installed_fixtures=$dest/media/beingos-installed-update-qa.img" \
        --extra "design=$dest/scope/DESIGN-AND-PLAN.md" \
        --extra "coverage=$dest/scope/DESIGN-COVERAGE.md" \
        --extra "readiness=$dest/scope/USB-READINESS.md" \
        --extra "buildroot_config=$dest/media/buildroot.config" \
        ${cache_evidence[@]+"${cache_evidence[@]}"} \
        --output "$dest/release.json"
    echo "Packaged $dest/release.json"
)

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    set -euo pipefail
    candidate_artifacts "$@"
fi
