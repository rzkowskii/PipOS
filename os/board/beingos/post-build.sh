#!/bin/sh
# Runs after the root filesystem tree is assembled, before images are made.
set -eu

BOARD_DIR="$(dirname "$0")"
# Buildroot passes BR2_ROOTFS_POST_SCRIPT_ARGS after the target directory:
# the variant, then "cable" for the cable development build (BEING_LAPTOP_CABLE=1).
BEINGOS_VARIANT="${2:-lab}"
BEINGOS_CABLE="${3:-}"
. "$BOARD_DIR/slots.sh"

fail() {
    echo "post-build: $*" >&2
    exit 1
}

case "$BEINGOS_VARIANT" in
    lab|laptop) ;;
    *) fail "unknown PipOS variant '$BEINGOS_VARIANT' (lab or laptop)" ;;
esac
case "$BEINGOS_CABLE" in
    "") ;;
    cable) [ "$BEINGOS_VARIANT" = laptop ] || fail "the cable development build is a laptop build" ;;
    *) fail "unknown build option '$BEINGOS_CABLE' (only cable)" ;;
esac

if ! find "$TARGET_DIR/usr/lib" -path '*/lib-dynload/_ssl.*.so' -print -quit | grep -q .; then
    echo "The target Python SSL extension is required. Reconfigure the python3 package before packaging PipOS." >&2
    exit 1
fi

# Each slot supplies its own kernel and system image.
install -D -m 0644 "$BINARIES_DIR/bzImage" "$TARGET_DIR/boot/bzImage"

# /workshop is Pip's RAM-only workshop (workshop.mount); the root is
# read-only, so its mount point is made here.
mkdir -p "$TARGET_DIR/mind" "$TARGET_DIR/models" "$TARGET_DIR/bootstate" \
    "$TARGET_DIR/var" "$TARGET_DIR/mnt/updates" "$TARGET_DIR/etc/rauc" "$TARGET_DIR/workshop"
fstab="$TARGET_DIR/etc/fstab"
awk '$2 != "/" && $2 != "/var" && $2 != "/mind" && $2 != "/models" && $2 != "/bootstate"' "$fstab" > "$fstab.new"
cat >> "$fstab.new" <<'EOF'
/dev/root / squashfs ro 0 0
tmpfs /var tmpfs mode=0755,nosuid,nodev 0 0
LABEL=mind /mind ext4 rw,noatime,nodev,nosuid,noexec,discard 0 2
LABEL=models /models ext4 ro,noatime,nodev,nosuid,noexec 0 2
PARTLABEL=esp /bootstate vfat rw,umask=0077,nodev,nosuid,noexec 0 2
EOF
mv "$fstab.new" "$fstab"

# Update identity. The laptop has its own RAUC compatible strings, so a lab
# bundle (or a lab rootfs signed for "installed" systems) is refused by RAUC
# before any of its code runs, and being-update-ready checks the variant too.
if [ "$BEINGOS_VARIANT" = laptop ]; then
    compatible=beingos-laptop-v1
    installed_compatible=beingos-laptop-installed-v1
else
    compatible=beingos-lab-v1
    installed_compatible=beingos-installed-v1
fi

# The update trust root. The laptop ships only the owner's release
# certificate (made by `lab/build.sh release-key`, imported into the builder
# with `lab/build.sh release-cert`). Until the owner has made one, a laptop
# image falls back to the lab certificate and says so in
# /usr/share/being/update-trust: fine for Try media, refused for an image
# meant to be installed (BEINGOS_REQUIRE_RELEASE_KEY=1).
release_cn="PipOS Laptop Release"
keyring="$BEINGOS_LAB_KEYS/cert.pem"
trust=lab
if [ "$BEINGOS_VARIANT" = laptop ]; then
    if [ -n "${BEINGOS_RELEASE_CERT:-}" ] && [ -s "$BEINGOS_RELEASE_CERT" ]; then
        keyring="$BEINGOS_RELEASE_CERT"
        trust=release
    elif [ "${BEINGOS_REQUIRE_RELEASE_KEY:-0}" = 1 ]; then
        fail "this laptop image is meant to be installed, but no release certificate was imported (lab/build.sh release-cert)"
    else
        echo "post-build: WARNING: no owner release certificate; this laptop image trusts the lab key for updates. Do not install it." >&2
    fi
fi
[ -s "$keyring" ] || fail "missing update certificate $keyring"
if [ "$trust" = release ]; then
    subject=$(openssl x509 -in "$keyring" -noout -subject -nameopt RFC2253) || fail "cannot read $keyring"
    [ "$subject" = "subject=CN=$release_cn" ] || fail "the release certificate's subject is '$subject', expected CN=$release_cn"
    if openssl pkey -in "$keyring" -noout 2>/dev/null; then
        fail "$keyring holds a private key; import only the certificate"
    fi
    # The model pack is signed with the builder's key. Its check must read
    # model-pack.pem, or a release keyring would leave the cortex unstartable.
    grep -q '/usr/share/being/model-pack.pem' "$TARGET_DIR/usr/lib/being/mind/model_pack.py" ||
        fail "mind/model_pack.py still checks the model pack against the update keyring"
fi

render_rauc() {
    # Only bundles signed by the release certificate itself are accepted.
    signer=
    [ "$trust" != release ] || signer=$release_cn
    awk -v compatible="$1" -v signer="$signer" '
        $0 == "compatible=beingos-lab-v1" { print "compatible=" compatible; next }
        { print }
        $0 == "use-bundle-signing-time=true" && signer != "" { print "allowed-signer-cns=" signer }
    ' "$BOARD_DIR/rauc.conf"
}

mkdir -p "$TARGET_DIR/usr/share/being"
render_rauc "$compatible" > "$TARGET_DIR/etc/rauc/system.conf.tmpl"
slot_ids "$TARGET_DIR/etc/rauc/system.conf.tmpl" > "$TARGET_DIR/etc/rauc/system.conf"
rm -f "$TARGET_DIR/etc/rauc/system.conf.tmpl"
render_rauc "$installed_compatible" > "$TARGET_DIR/usr/share/being/installed-rauc.conf"
grep -qx "compatible=$compatible" "$TARGET_DIR/etc/rauc/system.conf" || fail "system.conf has the wrong compatible string"
grep -qx "compatible=$installed_compatible" "$TARGET_DIR/usr/share/being/installed-rauc.conf" ||
    fail "installed-rauc.conf has the wrong compatible string"
boot_console < "$BOARD_DIR/grub.cfg" > "$TARGET_DIR/usr/share/being/installed-grub.cfg"
chmod 0644 "$TARGET_DIR/usr/share/being/installed-grub.cfg"
install -D -m 0644 "$BINARIES_DIR/efi-part/EFI/BOOT/bootx64.efi" "$TARGET_DIR/usr/share/being/install/bootx64.efi"
install -D -m 0644 "$keyring" "$TARGET_DIR/etc/rauc/keyring.pem"
# The signed model pack is staged by the builder (post-image.sh) with the lab
# key on every build, so it has its own certificate, separate from updates.
install -D -m 0644 "$BEINGOS_LAB_KEYS/cert.pem" "$TARGET_DIR/usr/share/being/model-pack.pem"
printf '%s\n' "$BEINGOS_VARIANT" > "$TARGET_DIR/usr/share/being/variant"
fingerprint=$(openssl x509 -in "$keyring" -noout -fingerprint -sha256 | sed 's/^[^=]*=//; s/://g' | tr 'A-F' 'a-f')
cat > "$TARGET_DIR/usr/share/being/update-trust" <<EOF
variant=$BEINGOS_VARIANT
compatible=$compatible
installed_compatible=$installed_compatible
trust=$trust
certificate_sha256=$fingerprint
EOF
install -D -m 0755 "$BOARD_DIR/being-update-ready" "$TARGET_DIR/usr/libexec/being-update-ready"
install -D -m 0644 "$BOARD_DIR/watchdog.conf" "$TARGET_DIR/etc/systemd/system.conf.d/50-being-watchdog.conf"

# Only the owner-provisioned PipOS listeners are allowed to start.
ln -sfn /dev/null "$TARGET_DIR/etc/systemd/system/sshd.service"
if [ "$BEINGOS_VARIANT" = laptop ]; then
    # No login prompt anywhere on the laptop, on any terminal. Root is locked too.
    for unit in getty@ serial-getty@ autovt@ console-getty container-getty@; do
        ln -sfn /dev/null "$TARGET_DIR/etc/systemd/system/$unit.service"
    done
    # /dev/cpu/*/msr for bodycare. Writes stay off (msr.allow_writes=off).
    mkdir -p "$TARGET_DIR/usr/lib/modules-load.d"
    printf '%s\n' '# Read access to CPU registers for bodycare (plan section 4, Mode B).' msr \
        > "$TARGET_DIR/usr/lib/modules-load.d/being-msr.conf"
fi
# The cable development link (docs/LAB.md). Its login key and host key come
# from the builder volume (lab/build.sh cable-key), never from the source
# tree, and only the cable build may carry them: every other image is refused
# if it holds any cable file or unit.
cable="$TARGET_DIR/usr/share/being/cable"
if [ "$BEINGOS_CABLE" = cable ]; then
    keys=${BEINGOS_CABLE_KEYS:-/work/beingos-cable}
    [ -s "$keys/authorized_key" ] && [ -s "$keys/ssh_host_ed25519_key" ] ||
        fail "no cable keys in $keys: run lab/build.sh cable-key PUBKEY HOSTKEY first"
    [ -x "$TARGET_DIR/usr/sbin/sshd" ] || fail "the cable build needs OpenSSH's server (BR2_PACKAGE_OPENSSH_SERVER)"
    [ -s "$cable/sshd_config" ] || fail "the cable build needs BR2_PACKAGE_BEING_CABLE_DEV (no $cable/sshd_config)"
    for unit in being-cable-link being-cable-ssh being-cable-workbench; do
        [ -f "$TARGET_DIR/usr/lib/systemd/system/$unit.service" ] || fail "the cable build is missing $unit.service"
    done
    # Exactly one Ed25519 public key (its comment dropped) and one unencrypted
    # OpenSSH Ed25519 host key, whose public half is read from the key itself.
    python3 - "$keys/authorized_key" "$keys/ssh_host_ed25519_key" "$cable" <<'CABLE_KEYS' || exit 1
import base64, binascii, os, struct, sys

client_path, host_path, folder = sys.argv[1:]
HEADER = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
# fe80:* is a pattern, as in sshd_config: a CIDR never matches a scoped
# link-local peer (fe80::x%enp1s0f0).
PEERS = "169.254.0.0/16,fe80:*"


def fail(message):
    sys.exit(f"post-build: {message}")


def public(blob, what):
    if not blob.startswith(HEADER) or len(blob) != len(HEADER) + 32:
        fail(f"{what} is not an Ed25519 key")
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


def strings(data, at, count):
    found = []
    for _ in range(count):
        (length,) = struct.unpack_from(">I", data, at)
        if at + 4 + length > len(data):
            raise ValueError("truncated")
        found.append(data[at + 4:at + 4 + length])
        at += 4 + length
    return found, at


try:
    lines = open(client_path, encoding="ascii").read().splitlines()
    host_text = open(host_path, encoding="ascii").read().strip().splitlines()
except (OSError, UnicodeDecodeError) as error:
    fail(f"cannot read the cable keys: {error}")
words = lines[0].split() if len(lines) == 1 else []
if len(words) < 2 or words[0] != "ssh-ed25519":
    fail(f"{client_path} must hold exactly one ssh-ed25519 public key")
try:
    client = public(base64.b64decode(words[1], validate=True), client_path)
except (ValueError, binascii.Error):
    fail(f"{client_path} is not an Ed25519 public key")

if (len(host_text) < 3 or host_text[0] != "-----BEGIN OPENSSH PRIVATE KEY-----"
        or host_text[-1] != "-----END OPENSSH PRIVATE KEY-----"):
    fail(f"{host_path} is not an OpenSSH private key")
magic = b"openssh-key-v1\x00"
try:
    raw = base64.b64decode("".join(host_text[1:-1]), validate=True)
    if not raw.startswith(magic):
        raise ValueError("no magic")
    (cipher, kdf, _), at = strings(raw, len(magic), 3)
    (count,) = struct.unpack_from(">I", raw, at)
    (host_blob,), _ = strings(raw, at + 4, 1)
except (ValueError, binascii.Error, struct.error):
    fail(f"{host_path} is not an OpenSSH private key")
if cipher != b"none" or kdf != b"none" or count != 1:
    fail(f"{host_path} must be one unencrypted host key (ssh-keygen -N '')")
host = public(host_blob, host_path)
if host == client:
    fail("the cable login key and the host key must be different keys")


def write(name, contents, mode):
    path = os.path.join(folder, name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as out:
        os.fchmod(out.fileno(), mode)
        out.write(contents)


write("authorized_keys", f'from="{PEERS}",restrict,pty {client}\n', 0o644)
write("ssh_host_ed25519_key", "\n".join(host_text) + "\n", 0o600)
write("ssh_host_ed25519_key.pub", host + "\n", 0o644)
CABLE_KEYS
    chmod 0755 "$cable"
    echo "post-build: cable development build: this image holds an SSH host key, keep it private" >&2
else
    [ ! -e "$cable" ] ||
        fail "this image holds cable development files (/usr/share/being/cable); only BEING_LAPTOP_CABLE=1 builds may"
    for unit in "$TARGET_DIR"/usr/lib/systemd/system/being-cable-*.service; do
        [ ! -e "$unit" ] || fail "this image holds the cable development unit /${unit#"$TARGET_DIR"/}"
    done
fi
python3 "$BOARD_DIR/build-id.py" "$TARGET_DIR"
