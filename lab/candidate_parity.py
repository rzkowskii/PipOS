"""Lab-versus-laptop root file system parity for a candidate built from one snapshot.

The laptop image must be the lab image minus the lab's own access (the lab
body, sshd, lab-access, serial and VT gettys) plus the laptop's own kernel
and boot settings. Anything else that differs is a mistake: code that QEMU
tested but the Mac will not run, or the reverse.

    # inside the builder, for each image (unsquashfs into a scratch folder first):
    python3 -m lab.candidate_parity listing EXTRACTED_ROOT > rootfs.json
    # then:
    python3 -m lab.candidate_parity compare lab-rootfs.json laptop-rootfs.json --output parity.json

`compare` exits 1 when a difference matches no allow-list rule. Each rule says
why it may differ. Standard library only.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

KIND = "pipos-rootfs-listing-v1"

# Buildroot's executable symlinks are /bin -> usr/bin, /sbin -> usr/sbin,
# and /usr/sbin -> bin. The pathname in the report stays as recorded.
ALIASES = {
    "bin": "usr/bin",
    "sbin": "usr/bin",
    "usr/sbin": "usr/bin",
    "lib": "usr/lib",
    "lib64": "usr/lib",
    "usr/lib64": "usr/lib",
}
LAB_BODY = (
    "usr/lib/being/sim", "usr/lib/being/sim/*",
    "usr/lib/systemd/system/being-lab-body.service", "etc/systemd/system/*being-lab-body.service",
)
LAB_ACCESS = (
    "usr/bin/being-lab-access", "usr/lib/systemd/system/being-lab-*", "etc/systemd/system/*being-lab-*",
)
OPENSSH = (
    *(f"usr/bin/{binary}" for binary in
      ("scp", "sftp", "ssh", "ssh-add", "ssh-agent", "ssh-keygen", "ssh-keyscan", "ssh-pkcs11-helper", "sshd")),
    *(f"usr/libexec/{binary}" for binary in
      ("sftp-server", "ssh-keysign", "ssh-pkcs11-helper", "ssh-sk-helper", "sshd-auth", "sshd-session")),
    # systemd installs its own shared ssh_config.d proxy entry even without
    # the OpenSSH package. Keep that directory and proxy under normal parity.
    "etc/ssh/moduli", "etc/ssh/ssh_config", "etc/ssh/sshd_config",
    "etc/ssh/sshd_config.d", "etc/ssh/sshd_config.d/*", "etc/ssh/ssh_host_*",
    "usr/lib/systemd/system/sshd*", "etc/systemd/system/*.wants/sshd*",
)
CABLE = (
    "usr/share/being/cable", "usr/share/being/cable/*",
    "usr/lib/systemd/system/being-cable-*", "etc/systemd/system/*being-cable-*",
)
LAB_LOGIN = (
    "etc/systemd/system/getty.target.wants", "etc/systemd/system/getty.target.wants/*",
    "usr/lib/systemd/system/getty@.service.d/buildroot-console.conf",
    "usr/lib/systemd/system/serial-getty@.service.d/buildroot-console.conf",
)

# (pattern, which side may have it: "lab", "laptop" or "both", reason).
# "lab" means the path may exist only in the lab image; "both" means it exists
# in both but its content may differ.
ALLOWED = (
    *((pattern, "lab", "the simulated lab body") for pattern in LAB_BODY),
    *((pattern, "lab", "LAN maintenance access") for pattern in LAB_ACCESS),
    *((pattern, "lab", "OpenSSH") for pattern in OPENSSH),
    *((pattern, "lab", "lab login configuration") for pattern in LAB_LOGIN),
    ("etc/systemd/system/autovt@.service", "laptop", "mask the laptop's login consoles"),
    ("etc/systemd/system/console-getty.service", "laptop", "mask the laptop's login consoles"),
    ("etc/systemd/system/container-getty@.service", "laptop", "mask the laptop's login consoles"),
    ("etc/systemd/system/getty@.service", "laptop", "mask the laptop's login consoles"),
    ("etc/systemd/system/serial-getty@.service", "laptop", "mask the laptop's login consoles"),
    ("etc/shadow", "both", "root is locked on the laptop"),
    ("etc/inittab", "both", "no consoles on the laptop"),
    ("boot/bzImage", "both", "the laptop kernel (linux-laptop.config)"),
    ("boot/*", "both", "the laptop kernel"),
    ("usr/lib/modules/*", "both", "laptop kernel modules"),
    ("usr/lib/modules-load.d/being-msr.conf", "laptop", "read-only CPU registers for laptop body care"),
    ("usr/lib/firmware/b43/*", "laptop", "owner-staged b43 firmware (laptop only)"),
    ("usr/lib/firmware/b43", "laptop", "owner-staged b43 firmware (laptop only)"),
    ("usr/share/being/variant", "both", "lab or laptop"),
    ("usr/share/being/installed-grub.cfg", "both", "no serial console on the laptop"),
    ("usr/share/being/install/bootx64.efi", "both", "no serial module in the laptop bootloader"),
    ("usr/share/being/installed-rauc.conf", "both", "separate laptop installed update channel"),
    ("usr/share/being/update-trust", "both", "variant-specific update channel and certificate metadata"),
    ("etc/being/build-id", "both", "names the defconfig it was built from"),
    ("etc/rauc/system.conf", "both", "laptop RAUC compatible strings (plan 1.2)"),
    ("etc/rauc/*.pem", "both", "the laptop ships only the release certificate (plan 1.2)"),
    ("usr/lib/tests/*", "lab", "lab-only test kernel modules"),
    ("etc/os-release", "both", "variant name"),
    ("usr/lib/os-release", "both", "variant name"),
)


def file_record(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return {"type": "link", "target": os.readlink(path)}
    if stat.S_ISDIR(info.st_mode):
        return {"type": "dir", "mode": stat.S_IMODE(info.st_mode)}
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
        return {"type": "file", "mode": stat.S_IMODE(info.st_mode), "sha256": digest.hexdigest()}
    return {"type": "special", "mode": stat.S_IMODE(info.st_mode)}


def listing(root):
    root = Path(root)
    return {"kind": KIND, "entries": {path.relative_to(root).as_posix(): file_record(path)
                                     for path in tree_paths(root)}}


def canonical(path):
    for prefix, destination in ALIASES.items():
        if path.startswith(prefix + "/"):
            return destination + path[len(prefix):]
    return path


def laptop_violations(paths, *, maintenance=False, cable=False):
    rules = (
        (LAB_BODY, "lab-only"),
        (LAB_LOGIN, "lab-only"),
        (() if maintenance else LAB_ACCESS, "lab-only"),
        (() if maintenance or cable else OPENSSH, "lab-only"),
        (() if cable else CABLE, "cable development"),
    )
    bad = {}
    for path in sorted(paths):
        name = canonical(path)
        for patterns, reason in rules:
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
                bad[path] = reason
                break
    return bad


def tree_paths(root):
    def fail(error):
        raise error

    for directory, dirs, files in os.walk(root, onerror=fail):
        for name in dirs + files:
            yield Path(directory) / name


def rule_for(path, side):
    """The allow-list rule that explains a difference, or None.

    side is "lab" (only in the lab image), "laptop" (only in the laptop image)
    or "both" (in both, with different content). A "both" rule also covers a
    path present on one side only (laptop kernel modules, for example).
    """
    name = canonical(path)
    for pattern, allowed, reason in ALLOWED:
        if fnmatch.fnmatchcase(name, pattern) and allowed in (side, "both"):
            return f"{pattern}: {reason}"
    return None


def compare(lab, laptop):
    for data in (lab, laptop):
        if data.get("kind") != KIND:
            raise ValueError("expected two rootfs listings from lab.candidate_parity listing")
    a, b = lab["entries"], laptop["entries"]
    explained, unexplained = [], []
    forbidden = laptop_violations(b)
    same = 0
    for path in sorted(a.keys() | b.keys()):
        if path in forbidden:
            unexplained.append({"path": path, "forbidden": forbidden[path]})
            continue
        if a.get(path) == b.get(path):
            same += 1
            continue
        side = "lab" if path not in b else "laptop" if path not in a else "both"
        rule = rule_for(path, side)
        row = {"path": path, "only_in" if side != "both" else "differs": side if side != "both" else True}
        if rule:
            explained.append(dict(row, rule=rule))
        else:
            unexplained.append(row)
    # A folder on one side only is fine when everything inside it is allowed.
    for row in reversed(unexplained.copy()):
        side = row.get("only_in")
        entries = a if side == "lab" else b if side == "laptop" else None
        if entries is None or entries[row["path"]].get("type") != "dir":
            continue
        inside = [other for other in explained + unexplained
                  if other is not row and other["path"].startswith(row["path"] + "/")]
        if inside and all(other in explained for other in inside):
            unexplained.remove(row)
            explained.append(dict(row, rule="holds only allowed differences"))
    return {"kind": "pipos-rootfs-parity-v1", "same": same, "explained": explained,
            "unexplained": unexplained, "ok": not unexplained}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("listing", help="hash every entry of an extracted root")
    make.add_argument("root", type=Path)
    check = commands.add_parser("compare", help="lab listing versus laptop listing")
    check.add_argument("lab", type=Path)
    check.add_argument("laptop", type=Path)
    check.add_argument("--output", type=Path)
    target = commands.add_parser("laptop-check", help="find lab/cable paths in a laptop target before hashing")
    target.add_argument("root", type=Path)
    target.add_argument("--maintenance", action="store_true")
    target.add_argument("--cable", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "listing":
        json.dump(listing(args.root), sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    if args.command == "laptop-check":
        try:
            paths = (path.relative_to(args.root).as_posix() for path in tree_paths(args.root))
            bad = laptop_violations(paths, maintenance=args.maintenance, cable=args.cable)
        except OSError as error:
            print(f"Laptop target scan failed: {error}", file=sys.stderr)
            return 1
        for path, reason in bad.items():
            print(f"Laptop image contains {reason} /{path}", file=sys.stderr)
        return int(bool(bad))
    try:
        result = compare(json.loads(args.lab.read_text()), json.loads(args.laptop.read_text()))
    except (OSError, ValueError) as error:
        print(f"parity failed: {error}", file=sys.stderr)
        return 1
    text = json.dumps(result, indent=1, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text)
    for row in result["unexplained"]:
        print(f"UNEXPLAINED {row}", file=sys.stderr)
    print(f"parity: {result['same']} entries identical, {len(result['explained'])} allowed differences, "
          f"{len(result['unexplained'])} unexplained")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
