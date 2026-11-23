"""Age encryption for portable backups with one native X25519 identity."""

import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

from .storage import sync_directory


RECIPIENT = re.compile(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}")
IDENTITY = re.compile(rb"AGE-SECRET-KEY-1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}")
IDENTITY_LIMIT = 4096
# A passphrase-protected identity is an age file encrypted with scrypt.
PROTECTED_HEADERS = (b"age-encryption.org/v1", b"-----BEGIN AGE ENCRYPTED FILE-----")


class BackupCryptoError(Exception):
    """Authentication failed or the encryption tool could not run."""


def _run(command, error, **streams):
    try:
        result = subprocess.run(command, stderr=subprocess.DEVNULL, check=False, **streams)
    except FileNotFoundError:
        raise BackupCryptoError(f"The {command[0]} utility is required.") from None
    except OSError:
        raise BackupCryptoError(f"Could not run {command[0]}.") from None
    if result.returncode:
        raise BackupCryptoError(error)
    return result.stdout


def protected(contents):
    return contents.lstrip().startswith(PROTECTED_HEADERS)


def _unlock(contents):
    """Decrypt a passphrase-protected identity. age asks for the passphrase on the terminal."""
    return _run(["age", "--decrypt"], "The passphrase did not unlock the age key.",
                input=contents, stdout=subprocess.PIPE)


def _read_identity(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise BackupCryptoError("The age key must be a regular file.")
            contents = source.read(IDENTITY_LIMIT + 1)
    except OSError:
        raise BackupCryptoError("Could not read the age key.") from None
    if len(contents) <= IDENTITY_LIMIT and protected(contents):
        contents = _unlock(contents)
    content_lines = [line.strip() for line in contents.splitlines()
                     if line.strip() and not line.lstrip().startswith(b"#")]
    if len(contents) > IDENTITY_LIMIT or len(content_lines) != 1 or not IDENTITY.fullmatch(content_lines[0]):
        raise BackupCryptoError("Expected a file with one native age X25519 key.")
    return content_lines[0] + b"\n"


def is_protected_file(path):
    """Whether a key file is passphrase-protected. Unreadable files count as not."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return False
            return protected(source.read(IDENTITY_LIMIT + 1))
    except OSError:
        return False


def unlock_identity(path, destination):
    """Unlock a passphrase-protected key once, into a new owner-only file.

    age asks for the passphrase on the terminal now, so the prompt is never
    drawn over by a progress screen later. `destination` belongs in RAM.
    """
    key = _read_identity(path)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(key)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        raise BackupCryptoError("Could not keep the unlocked age key in memory.") from None
    return Path(destination)


def recipient_for(identity):
    """Derive the public age recipient from a private key file."""
    public = _run(["age-keygen", "-y"], "The age key is invalid.",
                  input=_read_identity(identity), stdout=subprocess.PIPE)
    recipient = public.decode("ascii", errors="replace").strip()
    if not RECIPIENT.fullmatch(recipient):
        raise BackupCryptoError("The age key has no native X25519 recipient.")
    return recipient


def generate_key(path):
    """Create a private key without overwriting an existing path and return its public recipient."""
    path = Path(path)
    try:
        with tempfile.NamedTemporaryFile(mode="w+b", prefix=".being-backup-key-", dir=path.parent) as output:
            info = os.fstat(output.fileno())
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise BackupCryptoError("The key destination must support owner-only file permissions.")
            _run(["age-keygen"], "Could not generate an age key.", stdout=output)
            os.fsync(output.fileno())
            recipient = recipient_for(output.name)
            os.link(output.name, path)
        sync_directory(path.parent)
    except FileExistsError:
        raise BackupCryptoError("An age key already exists at the chosen path.") from None
    except OSError:
        raise BackupCryptoError("Could not publish the private age key. Choose a writable private filesystem.") from None
    return recipient


def generate_protected_key(path):
    """Create a passphrase-protected private key and return its public recipient.

    age-keygen writes the key into this process only; age asks for the
    passphrase twice on the terminal and writes the armoured result.
    """
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise BackupCryptoError("An age key already exists at the chosen path.")
    key = _run(["age-keygen"], "Could not generate an age key.", stdout=subprocess.PIPE)
    lines = [line.strip() for line in key.splitlines() if line.strip() and not line.startswith(b"#")]
    if len(lines) != 1 or not IDENTITY.fullmatch(lines[0]):
        raise BackupCryptoError("Could not generate an age key.")
    public = _run(["age-keygen", "-y"], "The age key is invalid.", input=lines[0] + b"\n", stdout=subprocess.PIPE)
    recipient = public.decode("ascii", errors="replace").strip()
    if not RECIPIENT.fullmatch(recipient):
        raise BackupCryptoError("The age key has no native X25519 recipient.")
    try:
        with tempfile.NamedTemporaryFile(mode="w+b", prefix=".being-backup-key-", dir=path.parent) as output:
            _run(["age", "--encrypt", "--passphrase", "--armor"], "Could not protect the age key.",
                 input=lines[0] + b"\n", stdout=output)
            output.flush()
            os.fsync(output.fileno())
            os.link(output.name, path)
        sync_directory(path.parent)
    except FileExistsError:
        raise BackupCryptoError("An age key already exists at the chosen path.") from None
    except OSError:
        raise BackupCryptoError("Could not save the protected age key.") from None
    return recipient


def encrypt(recipient, source, destination):
    """Encrypt from one binary file to another using a native age public recipient."""
    if not RECIPIENT.fullmatch(recipient):
        raise BackupCryptoError("Expected a native age X25519 recipient starting with age1.")
    _run(["age", "--encrypt", "--recipient", recipient], "Could not encrypt the backup.",
         stdin=source, stdout=destination)


def decrypt(identity, source, destination):
    """Decrypt to a staging file and return only after age checks the full ciphertext."""
    key = _read_identity(identity)
    fd_root = "/proc/self/fd" if sys.platform == "linux" else "/dev/fd"
    try:
        reader, writer = os.pipe()
        try:
            # Pass only the validated native key to age. It accepts other identity
            # formats, including ones that can launch external plugins.
            with os.fdopen(writer, "wb") as key_stream:
                key_stream.write(key)
            _run(["age", "--decrypt", "--identity", f"{fd_root}/{reader}"],
                 "The backup could not be authenticated with this key.",
                 stdin=source, stdout=destination, pass_fds=(reader,))
        finally:
            os.close(reader)
    except OSError:
        raise BackupCryptoError("Could not pass the private key to age.") from None
