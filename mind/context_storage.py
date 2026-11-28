"""Select one boot's working directories for Mind and llama-server."""

import os
import re
import shutil
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import fscrypt
from .install_binding import installed_mapping
from .storage import atomic_write, private_directory
from .try_mode import CMDLINE, installed_trial_pending


MIND = Path("/mind")
RUNTIME = Path("/run/being-context")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
PATHS_FILE = "paths.env"
BOOT_NAME = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
MODES = {"encrypted", "ram"}


@dataclass(frozen=True)
class WorkingFiles:
    text: Path
    slots: Path

    @classmethod
    def legacy(cls, state):
        return cls(state, state / "cortex")

    @classmethod
    def for_process(cls, state, environ=os.environ):
        """The appliance specifies both paths; standalone development uses its state directory."""
        names = ("BEING_CONTEXT_DIR", "BEING_SLOT_DIR")
        mode = environ.get("BEING_CONTEXT_MODE")
        if mode is None and not any(name in environ for name in names):
            return cls.legacy(state)
        if mode is None or mode != read_mode(mind=state):
            raise ValueError("PipOS working directories are unavailable")
        files = cls.for_boot(mode, state, RUNTIME)
        if tuple(environ.get(name) for name in names) != (str(files.text), str(files.slots)):
            raise ValueError("PipOS working directories do not match the current boot")
        return files

    @classmethod
    def for_boot(cls, mode, mind, runtime):
        if mode == "ram":
            return cls(runtime / "text", runtime / "slots")
        boot = BOOT_ID.read_text().strip()
        if not BOOT_NAME.fullmatch(boot):
            raise ValueError("Invalid kernel boot ID")
        return cls(mind / "context-cache" / boot, mind / "cortex" / "boot-cache" / boot)


def _environment(mode, files):
    return (f"BEING_CONTEXT_MODE={mode}\n"
            f"BEING_CONTEXT_DIR={files.text}\nBEING_SLOT_DIR={files.slots}\n")


def read_mode(runtime=RUNTIME, mind=MIND):
    try:
        descriptor = os.open(runtime / PATHS_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="ascii") as source:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return None
            data = source.read(1024)
        for mode in MODES:
            if data.startswith(f"BEING_CONTEXT_MODE={mode}\n"):
                files = WorkingFiles.for_boot(mode, mind, runtime)
                if data == _environment(mode, files):
                    with directory(files.text), directory(files.slots):
                        return mode
    except (OSError, ValueError):
        pass
    return None


def installed_ready(mind):
    try:
        args = CMDLINE.read_text()
        installed_mapping(args)
        trial = [word for word in args.split() if word.startswith("being.trial=")]
        return (trial in (["being.trial=0"], ["being.trial=1"])
                and mind.is_mount() and not installed_trial_pending(mind))
    except (OSError, ValueError):
        return False


@contextmanager
def directory(path, *, device=None, private=True):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        denied = 0o077 if private else 0o022
        if info.st_uid != os.geteuid() or info.st_mode & denied or (device is not None and info.st_dev != device):
            raise PermissionError("Unexpected working directory ownership, mode or volume")
        yield descriptor
    finally:
        os.close(descriptor)


def reclaim_old(parent, current, mount_fd, device):
    """Reclaim previous boot caches after their keys have gone away."""
    for path in parent.iterdir():
        if path.name == current or not BOOT_NAME.fullmatch(path.name):
            continue
        try:
            with directory(path, device=device) as descriptor:
                identifier = fscrypt.policy_key(descriptor)
                if identifier is None or fscrypt.key_status(mount_fd, identifier) != "absent":
                    continue
            shutil.rmtree(path)
        except (OSError, ValueError):
            # A failed reclamation must not cause a plaintext or unusable cache this boot.
            print("PipOS could not reclaim a previous boot's encrypted working files.", file=sys.stderr)


def encrypted_files(mind):
    cortex = mind / "cortex"
    cortex.mkdir(mode=0o700, exist_ok=True)
    files = WorkingFiles.for_boot("encrypted", mind, RUNTIME)
    parents = (files.text.parent, files.slots.parent)
    with directory(mind, private=False) as mount_fd, directory(cortex, private=False) as cortex_fd:
        device = os.fstat(mount_fd).st_dev
        if os.fstat(cortex_fd).st_dev != device:
            raise OSError("The Cortex directory is on another volume")
        for path in parents:
            private_directory(path)
        created = []
        try:
            for path in (files.text, files.slots):
                try:
                    path.mkdir(mode=0o700)
                    created.append(path)
                except FileExistsError:
                    pass
            with directory(files.text, device=device) as text_fd, directory(files.slots, device=device) as slot_fd:
                configure_policies(mount_fd, ((files.text, text_fd), (files.slots, slot_fd)), created)
        except (OSError, ValueError, AttributeError):
            for path in created:
                try:
                    path.rmdir()
                except OSError:
                    pass
            raise
        for parent in parents:
            reclaim_old(parent, files.text.name, mount_fd, device)
    return files


def configure_policies(mount_fd, candidates, created):
    keys = {descriptor: fscrypt.policy_key(descriptor) for _, descriptor in candidates}
    if any(keys[descriptor] is None and path not in created for path, descriptor in candidates):
        raise ValueError("An existing working directory has no encryption policy")
    known = {identifier for identifier in keys.values() if identifier is not None}
    if len(known) > 1:
        raise ValueError("Working directories have different encryption keys")
    fresh = not known
    identifier = known.pop() if known else fscrypt.add_key(mount_fd)
    try:
        if fscrypt.key_status(mount_fd, identifier) != "present":
            raise OSError("Working directory encryption key is unavailable")
        for descriptor, existing in keys.items():
            if existing is None:
                fscrypt.set_policy(descriptor, identifier)
            if fscrypt.policy_key(descriptor) != identifier:
                raise OSError("Working directory encryption policy could not be confirmed")
    except (OSError, ValueError):
        if fresh:
            try:
                fscrypt.remove_key(mount_fd, identifier)
            except OSError:
                pass
        raise


def prepare(mind=MIND, runtime=RUNTIME):
    private_directory(runtime)
    # Both services see a single selection for the whole boot, even if this
    # unit itself is restarted while they already hold files open.
    try:
        (runtime / PATHS_FILE).lstat()
    except FileNotFoundError:
        pass
    else:
        existing = read_mode(runtime, mind)
        if existing is None:
            raise ValueError("The published working directories are unavailable")
        if existing == "encrypted":
            encrypted_files(mind)
        return
    mode = "ram"
    files = WorkingFiles.for_boot(mode, mind, runtime)
    if installed_ready(mind):
        try:
            files = encrypted_files(mind)
            mode = "encrypted"
        except (OSError, ValueError, AttributeError):
            print("Encrypted working files unavailable. Using RAM for this boot.", file=sys.stderr)
    if mode == "ram":
        private_directory(files.text)
        private_directory(files.slots)
    atomic_write(runtime / PATHS_FILE, _environment(mode, files))


if __name__ == "__main__":
    prepare()
