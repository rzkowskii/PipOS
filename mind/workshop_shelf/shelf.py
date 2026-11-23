"""Pip's built-in shelf: tiny console programs that can be "installed" offline.

Runs inside the workshop cage as being-shell. It only copies files from the
read-only shelf into the RAM workshop; nothing is downloaded, ever.

    shelf.py list
    shelf.py install NAME
    shelf.py uninstall NAME
    shelf.py run NAME [ARGS...]
    shelf.py manifest            (build time: rewrite each app.json's size and sha256)
"""

import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

SHELF = Path(os.environ.get("BEING_SHELF") or Path(__file__).resolve().parent)
NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def workshop():
    return Path(os.environ.get("HOME") or "/workshop")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def catalogue(shelf=None):
    """Every shelf program, as {name: manifest}, in name order."""
    shelf = Path(shelf or SHELF)
    found = {}
    for manifest in sorted(shelf.glob("*/app.json")):
        try:
            info = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = info.get("name")
        if isinstance(name, str) and name == manifest.parent.name and set(name) <= NAME_CHARS:
            info["dir"] = str(manifest.parent)
            found[name] = info
    return found


def find(word, shelf=None):
    word = " ".join(word.lower().split())
    for name, info in catalogue(shelf).items():
        if word == name or word in [alias.lower() for alias in info.get("aliases", ())]:
            return info
    return None


def verified(info):
    source = Path(info["dir"]) / info["entry"]
    return source.is_file() and source.stat().st_size == info.get("size") and digest(source) == info.get("sha256")


def installed_dir(name):
    return workshop() / "apps" / name


def _plain_dir(path):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    return stat.S_ISDIR(mode)


def install(word, out=print):
    info = find(word)
    if info is None:
        out(f"{word} isn't on my shelf. I can't download programs: I'm offline.")
        out("Type: shelf   to see what I have.")
        return 1
    name = info["name"]
    if not verified(info):
        out(f"My copy of {name} doesn't match its checksum, so I won't install it.")
        return 1
    apps = workshop() / "apps"
    if _plain_dir(apps) is False:
        out("Something that isn't a folder is in the way of apps/. Not installing.")
        return 1
    apps.mkdir(mode=0o700, exist_ok=True)
    target = installed_dir(name)
    state = _plain_dir(target)
    if state is False:
        out(f"Something that isn't a folder is in the way of apps/{name}. Not installing.")
        return 1
    entry = target / info["entry"]
    if state:
        if entry.is_file() and not entry.is_symlink() and digest(entry) == info["sha256"]:
            out(f"{name} is already installed. Type: run {name}")
            return 0
        out(f"{name} is installed and has been changed. Type: uninstall {name}   for a fresh copy.")
        return 1
    target.mkdir(mode=0o700)
    shutil.copyfile(Path(info["dir"]) / info["entry"], entry, follow_symlinks=False)
    os.chmod(entry, 0o600)
    out(f"{name} comes from my built-in shelf. No internet was used.")
    out(f"Installed {name} ({info['size']} bytes) into apps/{name}. {info.get('keys', '')}".rstrip())
    out(f"Type: run {name}")
    return 0


def uninstall(word, out=print):
    info = find(word)
    name = info["name"] if info else word
    target = installed_dir(name)
    if not (info and _plain_dir(target)):
        out(f"{name} isn't installed.")
        return 1
    shutil.rmtree(target)
    out(f"Uninstalled {name}.")
    return 0


def run(word, args):
    info = find(word)
    if info is None:
        print(f"{word} isn't on my shelf.")
        return 1
    entry = installed_dir(info["name"]) / info["entry"]
    if not entry.is_file() or entry.is_symlink():
        print(f"{info['name']} isn't installed. Type: install {info['name']}")
        return 1
    os.execv(sys.executable, [sys.executable, str(entry), *args])
    return 1  # not reached


def listing(out=print):
    out("On my shelf (offline, nothing is downloaded):")
    for name, info in catalogue().items():
        mark = "installed" if _plain_dir(installed_dir(name)) else ""
        out(f"  {name:<10} {info.get('summary', '')} {mark}".rstrip())
    out("Type: install NAME")
    return 0


def manifest(shelf=None):
    for path in sorted(Path(shelf or SHELF).glob("*/app.json")):
        info = json.loads(path.read_text(encoding="utf-8"))
        source = path.parent / info["entry"]
        info["size"] = source.stat().st_size
        info["sha256"] = digest(source)
        path.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return 0


def main(argv):
    if not argv:
        return listing()
    command, rest = argv[0], argv[1:]
    if command in ("list", "shelf"):
        return listing()
    if command == "manifest":
        return manifest()
    if not rest:
        print(f"Usage: {command} NAME")
        return 2
    if command == "install":
        return install(" ".join(rest))
    if command == "uninstall":
        return uninstall(" ".join(rest))
    if command == "run":
        return run(rest[0], rest[1:])
    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
