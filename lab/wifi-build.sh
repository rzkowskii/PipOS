#!/bin/bash
# Extend the existing Buildroot output in a disposable filesystem overlay.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" = --in-builder ]; then
    output_name=${2:?missing image name}
    cd /work
    test -f /work/output/x86_64/.config
    configure_python=no
    if ! grep -qx 'BR2_PACKAGE_PYTHON3_SSL=y' /work/output/x86_64/.config ||
        ! compgen -G '/work/output/x86_64/target/usr/lib/python*/lib-dynload/_hashlib.*.so' >/dev/null; then
        configure_python=yes
    fi
    python3 - <<'PY'
from pathlib import Path

path = Path("/work/output/x86_64/.config")
options = {
    "BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES": '"/src/lab/wifi-linux.config"',
    "BR2_PACKAGE_HOSTAPD": "y",
    "BR2_PACKAGE_HOSTAPD_DRIVER_NL80211": "y",
    "BR2_PACKAGE_HOSTAPD_WPA3": "y",
    "BR2_PACKAGE_WPA_SUPPLICANT": "y",
    "BR2_PACKAGE_WPA_SUPPLICANT_NL80211": "y",
    "BR2_PACKAGE_WPA_SUPPLICANT_WPA3": "y",
    "BR2_PACKAGE_WPA_SUPPLICANT_CLI": "y",
    "BR2_PACKAGE_IW": "y",
    "BR2_PACKAGE_IPROUTE2": "y",
    "BR2_PACKAGE_DNSMASQ": "y",
    "BR2_PACKAGE_DNSMASQ_DHCP": "y",
    "BR2_PACKAGE_SYSTEMD_NETWORKD": "y",
    "BR2_PACKAGE_SYSTEMD_RESOLVED": "y",
    "BR2_PACKAGE_PYTHON3_SSL": "y",
}
lines = [
    line for line in path.read_text().splitlines()
    if line.removeprefix("# ").split("=", 1)[0].split(" ", 1)[0] not in options
]
lines.extend(f"{key}={value}" for key, value in options.items())
path.write_text("\n".join(lines) + "\n")
PY
    make -C /work/buildroot O=/work/output/x86_64 BR2_EXTERNAL=/src/os olddefconfig
    make -C /work/buildroot O=/work/output/x86_64 BR2_EXTERNAL=/src/os -j4 linux-reconfigure
    if [ "$configure_python" = yes ]; then
        make -C /work/buildroot O=/work/output/x86_64 BR2_EXTERNAL=/src/os -j4 python3-reconfigure
    fi
    make -C /work/buildroot O=/work/output/x86_64 BR2_EXTERNAL=/src/os -j4 being-rebuild
    make -C /work/buildroot O=/work/output/x86_64 BR2_EXTERNAL=/src/os -j4
    grep -qx 'CONFIG_MAC80211_HWSIM=m' /work/output/x86_64/build/linux-*/.config
    mkdir -p /src/out/images
    cp --sparse=always /work/output/x86_64/images/beingos.img "/src/out/images/.$output_name.new"
    mv -f "/src/out/images/.$output_name.new" "/src/out/images/$output_name"
    exit
fi

profile=beingos
context=colima-beingos
scratch=""
output_name=${1:-beingos-wifi.img}
if ! [[ $output_name =~ ^beingos-wifi[a-zA-Z0-9_-]*\.img$ ]]; then
    echo "Use a beingos-wifi*.img filename for the lab image." >&2
    exit 1
fi

if colima status --profile "$profile" >/dev/null 2>&1; then
    echo "The PipOS builder is already running. Finish that build first." >&2
    exit 1
fi
cleanup() {
    if [ -n "$scratch" ]; then
        docker --context "$context" rm -f "$scratch" >/dev/null 2>&1 || true
        docker --context "$context" volume rm "$scratch" >/dev/null || true
    fi
    lab/build.sh down
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

lab/build.sh up
scratch=$(docker --context "$context" volume create "beingos-wifi-$$")
docker --context "$context" run --rm --name "$scratch" --user 0:0 \
    --cap-add SYS_ADMIN --security-opt apparmor=unconfined \
    -v beingos-work:/base:ro -v "$scratch:/scratch" -v "$PWD:/src" \
    -e BR2_DL_DIR=/work/dl -e BR2_CCACHE_DIR=/work/ccache \
    -e BEINGOS_LAB_KEYS=/work/beingos-keys \
    beingos-builder bash -euo pipefail -c '
        cd /
        mkdir /scratch/upper /scratch/work
        LIBMOUNT_FORCE_MOUNT2=always mount -t overlay beingos-wifi \
            -o lowerdir=/base,upperdir=/scratch/upper,workdir=/scratch/work /work
        trap "umount /work" EXIT
        runuser -u builder -- bash /src/lab/wifi-build.sh --in-builder "$1"
    ' wifi-builder "$output_name"
