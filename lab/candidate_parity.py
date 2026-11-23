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

# (pattern, which side may have it: "lab", "laptop" or "both", reason).
# "lab" means the path may exist only in the lab image; "both" means it exists
# in both but its content may differ.
ALLOWED = (
    ("usr/lib/systemd/system/being-lab-*", "lab", "lab body and lab access units"),
    ("etc/systemd/system/*.wants/being-lab-*", "lab", "lab unit enablement"),
    ("usr/lib/being/sim/*", "lab", "the simulated lab body"),
    ("usr/lib/being/sim", "lab", "the simulated lab body"),
    ("usr/sbin/being-lab-access", "lab", "LAN maintenance access"),
    ("usr/sbin/sshd", "lab", "OpenSSH server"),
    ("usr/bin/ssh*", "lab", "OpenSSH tools"),
    ("usr/libexec/ssh*", "lab", "OpenSSH helpers"),
    ("usr/libexec/sftp-server", "lab", "OpenSSH helpers"),
    ("etc/ssh", "lab", "OpenSSH configuration"),
    ("etc/ssh/*", "lab", "OpenSSH configuration"),
    ("usr/lib/systemd/system/sshd*", "lab", "OpenSSH units"),
    ("etc/systemd/system/*.wants/sshd*", "lab", "OpenSSH enablement"),
    ("etc/systemd/system/getty.target.wants/*", "lab", "login gettys"),
    ("etc/systemd/system/getty.target.wants", "lab", "login gettys"),
    ("usr/lib/systemd/system/serial-getty@*", "lab", "serial login"),
    ("etc/systemd/system/serial-getty@*", "lab", "serial login"),
    ("etc/shadow", "both", "root is locked on the laptop"),
    ("etc/inittab", "both", "no consoles on the laptop"),
    ("boot/bzImage", "both", "the laptop kernel (linux-laptop.config)"),
    ("boot/*", "both", "the laptop kernel"),
    ("lib/modules/*", "both", "laptop kernel modules"),
    ("usr/lib/modules/*", "both", "laptop kernel modules"),
    ("usr/lib/firmware/b43/*", "laptop", "owner-staged b43 firmware (laptop only)"),
    ("usr/lib/firmware/b43", "laptop", "owner-staged b43 firmware (laptop only)"),
    ("lib/firmware/b43/*", "laptop", "owner-staged b43 firmware (laptop only)"),
    ("lib/firmware/b43", "laptop", "owner-staged b43 firmware (laptop only)"),
    ("usr/share/being/variant", "both", "lab or laptop"),
    ("usr/share/being/installed-grub.cfg", "both", "no serial console on the laptop"),
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
    entries = {}
    for directory, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(dirs) + sorted(files):
            path = Path(directory) / name
            entries[path.relative_to(root).as_posix()] = file_record(path)
    return {"kind": KIND, "entries": entries}


def rule_for(path, side):
    """The allow-list rule that explains a difference, or None.

    side is "lab" (only in the lab image), "laptop" (only in the laptop image)
    or "both" (in both, with different content). A "both" rule also covers a
    path present on one side only (laptop kernel modules, for example).
    """
    for pattern, allowed, reason in ALLOWED:
        if fnmatch.fnmatchcase(path, pattern) and allowed in (side, "both"):
            return f"{pattern}: {reason}"
    return None


def compare(lab, laptop):
    for data in (lab, laptop):
        if data.get("kind") != KIND:
            raise ValueError("expected two rootfs listings from lab.candidate_parity listing")
    a, b = lab["entries"], laptop["entries"]
    explained, unexplained = [], []
    for path in sorted(a.keys() | b.keys()):
        if a.get(path) == b.get(path):
            continue
        side = "lab" if path not in b else "laptop" if path not in a else "both"
        rule = rule_for(path, side)
        row = {"path": path, "only_in" if side != "both" else "differs": side if side != "both" else True}
        if rule:
            explained.append(dict(row, rule=rule))
        else:
            unexplained.append(row)
    # A folder on one side only is fine when everything inside it is allowed.
    for row in list(unexplained):
        side = row.get("only_in")
        entries = a if side == "lab" else b if side == "laptop" else None
        if entries is None or entries[row["path"]].get("type") != "dir":
            continue
        inside = [other for other in explained + unexplained
                  if other is not row and other["path"].startswith(row["path"] + "/")]
        if inside and all(other in explained for other in inside):
            unexplained.remove(row)
            explained.append(dict(row, rule="holds only allowed differences"))
    same = sum(1 for path in a.keys() & b.keys() if a[path] == b[path])
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
    args = parser.parse_args(argv)
    if args.command == "listing":
        json.dump(listing(args.root), sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 0
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
