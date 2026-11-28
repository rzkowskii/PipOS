"""Audit an actual PipOS disk image without mounting or booting it."""

import argparse
from configparser import ConfigParser, Error as ConfigError
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading


DRIVERS = {
    "b43": "b43",
    "bcma": "bcma",
    "gigabit_ethernet": "tg3",
    "hda_controller": "snd-hda-intel",
    "cirrus_cs420x": "snd-hda-codec-cs420x",
    "cirrus_cs421x": "snd-hda-codec-cs421x",
    "intel_graphics": "i915",
    "apple_smc": "applesmc",
    "cpu_temperature": "coretemp",
    "acpi_lid": "button",
    "acpi_mains": "ac",
    "acpi_battery": "battery",
    "acpi_smart_battery": "sbs",
    "acpi_video": "video",
    "backlight": "backlight",
    "rapl": "intel_rapl_msr",
    "package_thermal": "x86_pkg_temp_thermal",
}
TOOLS = (
    "iw", "wpa_supplicant", "wpa_cli", "ip", "ethtool", "lspci", "networkctl",
    "arecord", "aplay", "amixer", "alsactl", "journalctl", "sshd", "ssh-keygen",
    "rauc", "being-installer", "being-lab-access", "being-network", "nft",
)
ALBA_RETRAIN_REVISION = "cf910fb59f1d79ab3b625a63993a108751c6c25d"
ALBA_VOICE_PREFIX = "voice/piper/default/"
# The pinned Piper retrain prepared with research/alba-public/prepare.py.
# The ONNX digest includes the appended Sherpa metadata. The runtime card
# digest includes the compatibility alias map.
ALBA_RETRAIN_FILES = {
    "en_GB-alba-medium.onnx": (63222120, "6ce4571da18ebae67be27eb4197d2c17822af853a3e86dcdd8655cca33efc045"),
    "en_GB-alba-medium.onnx.json": (5796, "c56088c7a07c1b1dcaa6146f344d436f4699632c7bc913b2d96eca7d6a79e708"),
    "tokens.txt": (1008, "d9cda511aa50e7bdfb83258ff24c58df701ca1ad339db1e1ab0e614d52e26fec"),
    "MODEL_CARD": (501, "0190e1016a03c46c23f8e920f395a83fc82705ed1b49ba1ded185d7d8357dd6a"),
    "BASE-VOICE-MODEL_CARD": (1422, "62cafa98605100626d876aadcfd3dfa961cac263fa338b1fe17fe0989545e700"),
    "UPSTREAM-README.md": (497, "33e93643fce5180886300f9f0070eeeab8af148efb8e84361f8904def80fd9fb"),
}
ALBA_PRIVATE_MODEL = (63201430, "c904d007a8047ab13628b021351b983d0a2627c0d7a81c64a6fe9ad661adb1cf")
ALBA_REQUIRED_ESPEAK_DATA = {"en_dict", "intonations", "phondata", "phonindex", "phontab", "lang/gmw/en-GB-x-rp"}
VOICE_DESCRIPTOR = "usr/share/being/model-packs/tts/pack.json"
MODEL_CERTIFICATE = "usr/share/being/model-pack.pem"


def seven(tool, *args):
    return subprocess.run([tool, *args], capture_output=True, timeout=30, check=True).stdout


def entries(raw):
    for block in raw.decode("utf-8").split("\n\n"):
        fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
        if "Path" in fields:
            yield fields


def entries_for(tool, kind, path):
    return list(entries(seven(tool, "l", "-ba", "-slt", f"-t{kind}", str(path))))


def member(tool, kind, path, name):
    return seven(tool, "x", "-so", "-spd", f"-t{kind}", str(path), name)


def member_fingerprint(tool, kind, path, name, length, *, seconds=30):
    """Hash an exact member without loading an entire voice or GGUF into RAM."""
    from lab.candidate_manifest import fingerprint

    command = [tool, "x", "-so", "-spd", f"-t{kind}", "--", str(path), name]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        timeout = threading.Timer(seconds, process.kill)
        timeout.start()
        try:
            result = fingerprint(process.stdout, length)
            if process.stdout.read(1):
                raise ValueError(f"Oversized extraction of {name}.")
            if process.wait() != 0:
                raise ValueError(f"7-Zip did not finish reading {name}.")
            return result
        finally:
            timeout.cancel()
            timeout.join()
            if process.poll() is None:
                process.kill()


def copy_partition(source, partition, destination, *, squashfs=False):
    """Copy a read-only image range into a private temporary file."""
    offset, length = (int(partition[field]) for field in ("Offset", "Size"))
    if offset < 0 or length <= 0 or offset + length > source.stat().st_size:
        raise ValueError(f"Invalid partition extent for {partition['Path']}.")
    with source.open("rb") as image:
        if squashfs:
            image.seek(offset)
            header = image.read(96)
            if len(header) != 96 or header[:4] != b"hsqs":
                raise ValueError(f"Expected SquashFS at {partition['Path']}.")
            used, = struct.unpack_from("<Q", header, 40)
            if not len(header) <= used <= length:
                raise ValueError(f"Invalid SquashFS byte count for {partition['Path']}.")
            length = used
        image.seek(offset)
        digest = hashlib.sha256()
        with destination.open("wb") if destination is not None else nullcontext() as temporary:
            while length:
                block = image.read(min(length, 1 << 20))
                if not block:
                    raise ValueError(f"Short read in {partition['Path']}.")
                if temporary is not None:
                    temporary.write(block)
                digest.update(block)
                length -= len(block)
        return digest.hexdigest()


def module_name(path):
    return Path(path).name.split(".ko", 1)[0].replace("-", "_")


def kernel_drivers(paths, builtin):
    built = {module_name(line) for line in builtin.splitlines() if line.endswith(".ko")}
    modules = {module_name(path) for path in paths if re.search(r"\.ko(?:\.(?:gz|xz|zst))?$", path)}
    return {
        key: "built-in" if module_name(name) in built else "module" if module_name(name) in modules else None
        for key, name in DRIVERS.items()
    }


def certificate_sha256(pem):
    lines = pem.strip().splitlines()
    if (len(lines) < 3 or lines[0] != b"-----BEGIN CERTIFICATE-----" or lines[-1] != b"-----END CERTIFICATE-----"
            or any(re.fullmatch(rb"[A-Za-z0-9+/=]+", line) is None for line in lines[1:-1])):
        raise ValueError("Expected a single public PEM certificate.")
    der = subprocess.run(["openssl", "x509", "-outform", "DER"], input=pem,
                         stdout=subprocess.PIPE, check=True, timeout=10).stdout
    return hashlib.sha256(der).hexdigest()


def certificate_public_key(pem):
    return subprocess.run(["openssl", "x509", "-noout", "-pubkey"], input=pem,
                          stdout=subprocess.PIPE, timeout=10, check=True).stdout


def artifact_identity(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Expected a regular artifact file: {path.name}.")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def artifact_sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def root_report(tool, path, *, ordinary_role=None, release_signer=None):
    kind = "SquashFS"
    paths = {item["Path"] for item in entries_for(tool, kind, path)}
    if ordinary_role is not None:
        from lab.candidate_manifest import check_ordinary_root
        check_ordinary_root(paths, ordinary_role)
    release_trust = {}
    if release_signer is not None:
        trust = member(tool, kind, path, "usr/share/being/update-trust").splitlines()
        required = {b"trust=release", b"variant=laptop", b"installed_compatible=beingos-laptop-installed-v1"}
        if not required.issubset(trust):
            raise ValueError("The image does not identify the laptop release update channel.")
        actual_signer = certificate_sha256(member(tool, kind, path, "etc/rauc/keyring.pem"))
        if actual_signer != release_signer:
            raise ValueError("The image does not trust the supplied release certificate.")
        model_certificate = "usr/share/being/model-pack.pem"
        release_trust["update_certificate_sha256"] = actual_signer
        if model_certificate in paths:
            release_trust["model_pack_certificate_sha256"] = certificate_sha256(
                member(tool, kind, path, model_certificate))
    version_file = next((name for name in sorted(paths) if name.endswith("/modules.builtin")), None)
    builtin = member(tool, kind, path, version_file).decode() if version_file else ""
    build_file = "etc/being/build-id"
    gallium = next((name for name in sorted(paths) if re.fullmatch(r"usr/lib/libgallium-[\d.]+\.so", name)), None)
    crocus = "usr/lib/dri/crocus_dri.so" in paths
    if not crocus and gallium:
        binary = member(tool, kind, path, gallium)
        crocus = all(marker in binary for marker in (b"crocus_resolve_color\0", b"crocus_hiz_exec\0"))
    sdl = next((name for name in sorted(paths, reverse=True)
                if re.fullmatch(r"usr/lib/libSDL2-2\.0\.so(?:\.\d+)+", name)), None)
    env_file = "etc/being/env"
    env = member(tool, kind, path, env_file).decode() if env_file in paths else ""
    iw = next((name for name in ("usr/bin/iw", "usr/sbin/iw") if name in paths), None)
    metrics = "usr/lib/being/mind/hardware_metrics.py"
    return {
        "build_id": member(tool, kind, path, build_file).decode().strip() if build_file in paths else None,
        **release_trust,
        "source_id": (member(tool, kind, path, "etc/being/source-id").decode().strip()
                      if "etc/being/source-id" in paths else None),
        "kernel": Path(version_file).parent.name if version_file else None,
        "drivers": kernel_drivers(paths, builtin),
        "b43_firmware": sorted(name for name in paths if "/firmware/b43/" in name
                               and re.search(r"\.fw(?:\.(?:xz|zst))?$", name)),
        "tools": sorted(name for name in TOOLS if any(f"{prefix}/{name}" in paths for prefix in ("usr/bin", "usr/sbin"))),
        "mesa_crocus_marker": crocus,
        "sdl2_kmsdrm_marker": bool(sdl and b"KMSDRM" in member(tool, kind, path, sdl)),
        "iw_station_dump_marker": bool(iw and b"List all stations known" in member(tool, kind, path, iw)),
        "legacy_lab_timeout_overrides": dict(re.findall(r"^BEING_(THINK|SPEAK)_TIMEOUT=(\d+)$", env, re.MULTILINE)),
        "networkd": "usr/lib/systemd/systemd-networkd" in paths,
        "boot_network_firewall": "usr/lib/systemd/system/being-network-lockdown.service" in paths,
        "timesyncd_enabled_by_preset": "etc/systemd/system/sysinit.target.wants/systemd-timesyncd.service" in paths,
        "installed_update_binding": "usr/lib/being/mind/install_binding.py" in paths,
        "installer_writer": "usr/lib/being/mind/install_apply.py" in paths,
        "lan_observer": "usr/lib/systemd/system/being-lab-observe.service" in paths,
        "lan_development": "usr/lib/systemd/system/being-lab-dev.service" in paths,
        "hardware_metrics_sha256": hashlib.sha256(member(tool, kind, path, metrics)).hexdigest() if metrics in paths else None,
        "hardware_timing": "usr/lib/being/mind/hardware_timing.py" in paths,
    }


def _member_index(entries):
    by_path = {}
    for entry in entries:
        by_path.setdefault(entry["Path"], []).append(entry)
    return by_path


def _listed_size(index, name):
    matches = index.get(name, ())
    if (len(matches) != 1 or not matches[0].get("Mode", "").startswith("-")
            or matches[0].get("Symbolic Link")):
        raise ValueError(f"Expected one regular packaged file: {name}.")
    return int(matches[0].get("Size", ""))


def _metadata(tool, kind, filesystem, index, name, limit):
    size = _listed_size(index, name)
    if not 0 < size <= limit:
        raise ValueError(f"Missing or oversized packaged metadata: {name}.")
    data = member(tool, kind, filesystem, name)
    if len(data) != size:
        raise ValueError(f"Packaged metadata size differs from the listing: {name}.")
    return data


def read_voice_binding(tool, system):
    from mind.model_pack import MAX_DESCRIPTOR

    index = _member_index(entries_for(tool, "SquashFS", system))
    return (_metadata(tool, "SquashFS", system, index, MODEL_CERTIFICATE, 16 * 1024),
            _metadata(tool, "SquashFS", system, index, VOICE_DESCRIPTOR, MAX_DESCRIPTOR))


def signed_voice_files(tool, filesystem, index, bindings):
    from mind import model_pack

    name = ALBA_VOICE_PREFIX + model_pack.envelope_name("tts")
    envelope = _metadata(tool, "Ext", filesystem, index, name, model_pack.MAX_DESCRIPTOR + 8192)
    systems = {}
    with tempfile.TemporaryDirectory(prefix="pipos-voice-audit-") as folder:
        keyring = Path(folder) / "model-pack.pem"
        canonical = Path(folder) / "pack.json"
        for role, (certificate, descriptor) in bindings.items():
            keyring.write_bytes(certificate)
            canonical.write_bytes(descriptor)
            pack = model_pack.authenticate(envelope, keyring, canonical, "tts")
            systems[role] = {"signer_der_sha256": certificate_sha256(certificate),
                             "descriptor_sha256": hashlib.sha256(descriptor).hexdigest()}
    if pack.get("revision") != ALBA_RETRAIN_REVISION:
        raise ValueError("The signed voice descriptor does not identify the pinned Alba revision.")
    files = {name: (size, sha256) for name, size, sha256 in model_pack.files_of(pack)}
    if any(files.get(name) != identity for name, identity in ALBA_RETRAIN_FILES.items()):
        raise ValueError("The signed voice descriptor does not pin the expected Alba retrain files.")
    required = ("NOTICES.md", *(f"espeak-ng-data/{name}" for name in sorted(ALBA_REQUIRED_ESPEAK_DATA)))
    if any(name not in files or files[name][0] <= 0 for name in required):
        raise ValueError("The signed Alba voice pack is missing pronunciation data or notices.")
    evidence = {"systems": systems, "envelope_sha256": hashlib.sha256(envelope).hexdigest()}
    if "espeak_source" in pack:
        evidence["espeak_source"] = pack["espeak_source"]
    return files, evidence


def verify_alba_retrain(tool, filesystem, *, bindings=None):
    members = entries_for(tool, "Ext", filesystem)
    by_path = _member_index(members)
    selected, evidence = signed_voice_files(tool, filesystem, by_path, bindings) if bindings else (ALBA_RETRAIN_FILES, {})
    verified = {}
    for name, (size, sha256) in selected.items():
        path = ALBA_VOICE_PREFIX + name
        if _listed_size(by_path, path) != size:
            raise ValueError(f"Packaged Alba file has the wrong size: {path}.")
        record = member_fingerprint(tool, "Ext", filesystem, path, size)
        if record["sha256"] != sha256:
            raise ValueError(f"Packaged Alba file differs from the pinned voice pack: {path}.")
        verified[name] = record
    old_size, old_sha = ALBA_PRIVATE_MODEL
    for entry in members:
        if entry.get("Mode", "").startswith("-") and entry.get("Size") == str(old_size):
            record = member_fingerprint(tool, "Ext", filesystem, entry["Path"], old_size)
            if record["sha256"] == old_sha:
                raise ValueError(f"The legacy private Alba model is present: {entry['Path']}.")
    return {"upstream_revision": ALBA_RETRAIN_REVISION, "files": verified, **evidence}


def inspect(path, tool, *, require_ordinary=False, release_signer=None, require_alba_retrain=False):
    if not path.is_file():
        raise ValueError("Provide a regular PipOS image file.")
    # Use a generated GPT partition name, independent of UUIDs and sector offsets.
    partitions = entries_for(tool, "GPT", path)
    parts = {item["Path"].split(".", 1)[1].removesuffix(".img"): item
             for item in partitions
             if re.fullmatch(r"\d+\.[\w-]+\.img", item["Path"])}
    roots = [name for name in ("try-system", "system-a", "system-b") if name in parts]
    esp = parts.get("try-esp") or parts.get("esp")
    if not roots or esp is None:
        raise ValueError("Expected a PipOS GPT with EFI and system partitions.")
    with tempfile.TemporaryDirectory(prefix="being-image-audit-") as temporary:
        directory = Path(temporary)
        boot = directory / "boot.fat"
        copy_partition(path, esp, boot)
        boot_paths = {item["Path"].lower() for item in entries_for(tool, "FAT", boot)}
        grub = member(tool, "FAT", boot, "EFI/BOOT/grub.cfg").decode()
        reports = {}
        inspections = {}
        voice_roots = {}
        for name in roots:
            system = directory / "system.squashfs"
            digest = copy_partition(path, parts[name], system, squashfs=True)
            if digest not in inspections:
                inspections[digest] = root_report(tool, system, ordinary_role=name if require_ordinary else None,
                                                  release_signer=release_signer)
                if require_alba_retrain:
                    voice_roots[digest] = read_voice_binding(tool, system)
            reports[name] = {"squashfs_bytes_sha256": digest, **inspections[digest]}
        alba = {}
        if require_alba_retrain:
            labels = [item for item in partitions if re.fullmatch(r"\d+\.(?:try-)?models\.img", item["Path"])]
            if len(labels) != 1:
                raise ValueError("Expected exactly one models partition for the Alba retrain audit.")
            models = directory / "models.ext4"
            copy_partition(path, labels[0], models)
            bindings = {name: voice_roots[report["squashfs_bytes_sha256"]] for name, report in reports.items()}
            alba["alba_retrain"] = verify_alba_retrain(tool, models, bindings=bindings)
    return {
        "image": path.name,
        "image_bytes": path.stat().st_size,
        "efi_x86_64": "efi/boot/bootx64.efi" in boot_paths,
        "grub_outputs": sorted(set(re.findall(r"^terminal_output(?:\s+)(.*)$", grub, re.MULTILINE))),
        "kernel_consoles": sorted(set(re.findall(r"\bconsole=([^\s;]+)", grub))),
        "system_partitions": reports,
        **alba,
    }


def verify_laptop_bundle(rauc, bundle, certificate, provenance, *, reject_certificate=None):
    """Verify the bundle through RAUC and compare the extracted signed payload."""
    with tempfile.TemporaryDirectory(prefix="pipos-bundle-audit-") as scratch:
        directory = Path(scratch)
        keyring = directory / "release.pem"
        keyring.write_bytes(certificate)
        extracted = directory / "bundle"
        subprocess.run([rauc, "extract", f"--keyring={keyring}", str(bundle.absolute()), str(extracted)],
                       stdout=subprocess.DEVNULL, timeout=300, check=True)

        manifest = ConfigParser(interpolation=None)
        try:
            with (extracted / "manifest.raucm").open(encoding="utf-8") as stream:
                manifest.read_file(stream)
            for section, key, expected in (
                ("update", "compatible", provenance["channel"]),
                ("update", "version", provenance["version"]),
                ("update", "build", provenance["build_id"]),
                ("bundle", "format", "verity"),
                ("image.rootfs", "filename", "rootfs.img"),
            ):
                if manifest.get(section, key) != expected:
                    raise ValueError(f"Signed bundle manifest does not match {section}.{key}.")
        except ConfigError as error:
            raise ValueError(f"Invalid signed bundle manifest: {error}") from error

        rootfs = extracted / "rootfs.img"
        rootfs_size = artifact_identity(rootfs)[2]
        if artifact_sha256(rootfs) != provenance["rootfs_sha256"]:
            raise ValueError("Signed root filesystem does not match the receipt hash.")
        rootfs_range = {"Offset": 0, "Size": rootfs_size, "Path": rootfs.name}
        if copy_partition(rootfs, rootfs_range, None, squashfs=True) != provenance["squashfs_bytes_sha256"]:
            raise ValueError("Signed root filesystem does not match the Try SquashFS.")
        if reject_certificate is not None:
            replacement = directory / "replacement.pem"
            replacement.write_bytes(reject_certificate)
            status = subprocess.run([rauc, "info", f"--keyring={replacement}", str(bundle.absolute())],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            if status.returncode == 0:
                raise ValueError("The replacement certificate accepts the old-signed rotation bridge.")
            if status.returncode != 1:
                raise ValueError("RAUC did not complete the replacement-certificate rejection check.")


def match_laptop_bundle(report, bundle, *, release_signer=None, previous_certificate=None, verify=None):
    """Match local package provenance to the inspected Try root and bundle bytes."""
    receipt_path = bundle.with_name(bundle.name + ".provenance.json")
    before = artifact_identity(bundle), artifact_identity(receipt_path)
    with receipt_path.open("rb") as source:
        raw = source.read(4097)
    if len(raw) > 4096:
        raise ValueError("Oversized laptop bundle provenance receipt.")
    receipt = json.loads(raw)
    root = report["system_partitions"].get("try-system")
    if not root or not isinstance(receipt, dict) or receipt.get("channel") != "beingos-laptop-installed-v1":
        raise ValueError("Expected a laptop Try system and an installed laptop bundle receipt.")
    bundle_hash = artifact_sha256(bundle)
    for field, expected in (
        ("source_id", root["source_id"]),
        ("build_id", root["build_id"]),
        ("squashfs_bytes_sha256", root["squashfs_bytes_sha256"]),
        ("bundle_sha256", bundle_hash),
    ):
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected) or receipt.get(field) != expected:
            raise ValueError(f"Laptop bundle provenance does not match {field}.")
    for field, pattern in (
        ("rootfs_sha256", r"[a-f0-9]{64}"),
        ("signer_der_sha256", r"[a-f0-9]{64}"),
        ("version", r"[A-Za-z0-9._+-]{1,64}"),
    ):
        value = receipt.get(field)
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError(f"Invalid laptop bundle provenance field: {field}.")
    rotation = receipt.get("rotation")
    if previous_certificate is None:
        if "rotation" in receipt:
            raise ValueError("Rotation receipt requires --previous-release-certificate.")
        if release_signer is not None and receipt["signer_der_sha256"] != release_signer:
            raise ValueError("The bundle receipt does not name the supplied release certificate.")
    else:
        if release_signer is None or verify is None:
            raise ValueError("Rotation requires the replacement certificate and RAUC verification.")
        _, replacement_certificate = verify
        previous_signer = certificate_sha256(previous_certificate)
        if (certificate_sha256(replacement_certificate) != release_signer or previous_signer == release_signer
                or certificate_public_key(previous_certificate) == certificate_public_key(replacement_certificate)):
            raise ValueError("Rotation certificates must match the pins and hold distinct signing keys.")
        if (not isinstance(rotation, dict)
                or set(rotation) != {"from_der_sha256", "to_der_sha256"}
                or rotation.get("from_der_sha256") != previous_signer
                or rotation.get("to_der_sha256") != release_signer
                or receipt["signer_der_sha256"] != previous_signer):
            raise ValueError("Invalid laptop bundle certificate rotation receipt.")
    matched = {field: receipt[field] for field in (
        "channel", "version", "source_id", "build_id", "squashfs_bytes_sha256",
        "rootfs_sha256", "bundle_sha256", "signer_der_sha256",
    )}
    if rotation is not None:
        matched["rotation"] = rotation
    matched["payload_verified_at_signing"] = receipt.get("payload_verified_at_signing") is True
    if verify is not None:
        rauc, certificate = verify
        if previous_certificate is None:
            verify_laptop_bundle(rauc, bundle, certificate, matched)
        else:
            verify_laptop_bundle(rauc, bundle, previous_certificate, matched, reject_certificate=certificate)
    if before != (artifact_identity(bundle), artifact_identity(receipt_path)):
        raise ValueError("The laptop bundle or receipt changed during inspection.")
    return matched


def rotation_base(tool, path, signer, target):
    image = path.resolve()
    signature = artifact_identity(image)
    previous = inspect(image, tool, require_ordinary=True, release_signer=signer)["system_partitions"].get("try-system")
    if previous is None:
        raise ValueError("The previous release image must be a laptop Try image.")
    model_signer = previous.get("model_pack_certificate_sha256")
    if not model_signer or model_signer != target.get("model_pack_certificate_sha256"):
        raise ValueError("The replacement root does not preserve the installed model-pack certificate.")
    digest = artifact_sha256(image)
    if signature != artifact_identity(image):
        raise ValueError("The previous release image changed during inspection.")
    return {"image_sha256": digest, "source_id": previous["source_id"],
            "model_pack_certificate_sha256": model_signer}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--require-ordinary", action="store_true", help="reject cable development roots")
    parser.add_argument("--require-alba-retrain", action="store_true",
                        help="verify the signed Alba retrain against every packaged system and reject the known private model")
    parser.add_argument("--match-laptop-bundle", type=Path, help="match the installed bundle and its local receipt")
    parser.add_argument("--release-certificate", type=Path, help="pin the target image keyring to this public PEM")
    parser.add_argument("--previous-release-certificate", type=Path,
                        help="audit a certificate rotation; pin the signed bundle to this previous PEM")
    parser.add_argument("--previous-image", type=Path,
                        help="rotation: check the previous Try image's update and model-pack certificates")
    parser.add_argument("--verify-laptop-bundle", action="store_true",
                        help="verify and extract the matched bundle using RAUC and the supplied certificate")
    args = parser.parse_args()
    if args.release_certificate and not args.match_laptop_bundle:
        parser.error("--release-certificate requires --match-laptop-bundle")
    if args.previous_release_certificate and not args.match_laptop_bundle:
        parser.error("--previous-release-certificate requires --match-laptop-bundle")
    if args.previous_release_certificate and not args.verify_laptop_bundle:
        parser.error("--previous-release-certificate requires --verify-laptop-bundle")
    if args.previous_release_certificate and not args.release_certificate:
        parser.error("--previous-release-certificate requires --release-certificate")
    if args.previous_image and not args.previous_release_certificate:
        parser.error("--previous-image requires --previous-release-certificate")
    rauc = None
    if args.verify_laptop_bundle:
        if not args.release_certificate:
            parser.error("--verify-laptop-bundle requires --release-certificate and --match-laptop-bundle")
        rauc = shutil.which("rauc")
        if not rauc:
            parser.error("Install RAUC to verify the signed laptop bundle.")
    tool = shutil.which("7zz") or shutil.which("7z")
    if not tool:
        parser.error("Install 7-Zip to inspect GPT, FAT and SquashFS image files.")
    try:
        certificate = args.release_certificate.read_bytes() if args.release_certificate else None
        signer = certificate_sha256(certificate) if certificate is not None else None
        previous_certificate = (args.previous_release_certificate.read_bytes()
                                if args.previous_release_certificate else None)
        previous_signer = (certificate_sha256(previous_certificate)
                           if previous_certificate is not None else None)
        if previous_signer is not None and previous_signer == signer:
            raise ValueError("Previous and new release certificates must be distinct.")
        image = args.image.resolve()
        identity = artifact_identity(image) if args.match_laptop_bundle is not None or args.require_alba_retrain else None
        report = inspect(image, tool,
                         require_ordinary=args.require_ordinary or args.match_laptop_bundle is not None,
                         release_signer=signer, require_alba_retrain=args.require_alba_retrain)
        if identity is not None:
            report["image_sha256"] = artifact_sha256(image)
        if args.match_laptop_bundle is not None:
            report["laptop_bundle_provenance"] = match_laptop_bundle(
                report, args.match_laptop_bundle, release_signer=signer,
                previous_certificate=previous_certificate, verify=(rauc, certificate) if rauc else None)
            report["release_certificate_pinned"] = signer is not None
            report["bundle_verified_by_rauc"] = rauc is not None
            if args.previous_image:
                report["key_rotation_base"] = rotation_base(
                    tool, args.previous_image, previous_signer, report["system_partitions"]["try-system"])
        if identity is not None and identity != artifact_identity(image):
            raise ValueError("The disk image changed during inspection.")
        print(json.dumps(report, indent=2))
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f"Image audit failed. {error}")


if __name__ == "__main__":
    main()
