"""Stage one explicitly approved HTTPS release for the normal RAUC update flow."""

import fcntl
import hashlib
import http.client
import json
import os
import re
import select
import socket
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import backup, bus, install_binding, network_enforce, network_policy, update_slots
from .storage import private_directory, sync_directory
from .weather import LookupDenied, _end_socket_io, _PublicHTTPSConnection

SOURCE = Path("/etc/being/update-source.json")
LOCAL_SOURCE = Path("network/update-source.json")
MAX_SOURCE_BYTES = 8192
MAX_BUNDLE_BYTES = 4 * 1024**3
MAX_REDIRECTS = 3
MAX_TRANSFER_SECONDS = 1800
CONNECT_SECONDS = 8
IO_SECONDS = 4
DNS_SECONDS = 5
CHUNK = 128 * 1024
IDLE_STATES = {"idle", "drowsy", "asleep"}
BEAT_MAX_AGE = 2.0
LOCAL_BUF = 64 * 1024
STAGING = "update-download"
MIN_ARCHIVE_FREE = 128 * 1024**2
RESOLVER = ("import json,socket,sys; print(json.dumps(socket.getaddrinfo(sys.argv[1], int(sys.argv[2]), "
            "type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)[:32]))")
HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", re.ASCII)


@dataclass(frozen=True)
class Release:
    url: str
    bytes: int
    sha256: str
    hosts: frozenset[str]

    @property
    def host(self):
        return urlsplit(self.url).hostname


def _endpoint(url):
    problem = "An update source must be an ordinary HTTPS address on port 443."
    if (not isinstance(url, str) or not 0 < len(url) <= 4096
            or not url.isascii() or any(not "!" <= char <= "~" for char in url) or "\\" in url):
        raise ValueError(problem)
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or not HOST.fullmatch(parsed.hostname)
            or parsed.port not in (None, 443) or "@" in parsed.netloc or parsed.fragment or not parsed.path.startswith("/")):
        raise ValueError(problem)
    return parsed


def approved_release(path=SOURCE):
    """Read a release approval installed by the maintainer. No approval ships by default."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("The update source approval must be a root-owned regular file.")
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("The update source approval is too large.")
    record = json.loads(raw, object_pairs_hook=network_policy.unique_object)
    if not isinstance(record, dict) or record.keys() != {"version", "url", "bytes", "sha256", "redirect_hosts"}:
        raise ValueError("The update source approval is incomplete.")
    if type(record["version"]) is not int or record["version"] != 1:
        raise ValueError("Unsupported update source approval version.")
    parsed = _endpoint(record["url"])
    size, digest, redirects = record["bytes"], record["sha256"], record["redirect_hosts"]
    if (type(size) is not int or not 0 < size <= MAX_BUNDLE_BYTES or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(redirects, list) or len(redirects) > MAX_REDIRECTS
            or any(not isinstance(host, str) or not HOST.fullmatch(host) for host in redirects)):
        raise ValueError("Invalid update size, checksum or redirect approval.")
    return Release(record["url"], size, digest, frozenset([parsed.hostname, *redirects]))


def internet_enabled(mind):
    return (network_policy.load_policy(mind).effective_mode == network_policy.INTERNET
            and network_enforce.runtime_state()["mode"] == network_policy.INTERNET)


class Foreground:
    """A short, read-only subscription to reserved Brainstem state and heartbeats."""

    def __init__(self, pipe):
        self.pipe = pipe
        self.buffer = b""
        self.state = None
        self.heartbeat = None

    def check(self, *, waiting=False):
        busy = False
        received = 0
        while received < LOCAL_BUF:
            try:
                block = self.pipe.recv(4096)
            except BlockingIOError:
                break
            if not block:
                raise ValueError("Pip's activity signal closed. Try downloading again after it recovers.")
            received += len(block)
            self.buffer += block
            lines = self.buffer.split(b"\n")
            self.buffer = lines.pop()
            if len(self.buffer) > LOCAL_BUF:
                raise ValueError("Pip's activity signal is unavailable.")
            for line in lines:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Pip's activity signal is unavailable.")
                if event.get("t") == "stem.beat":
                    self.heartbeat = time.monotonic()
                if event.get("t") in ("stem.beat", "state.changed"):
                    self.state = event.get("state")
                    busy |= not isinstance(self.state, str) or self.state not in IDLE_STATES
        if busy:
            raise ValueError("Pip needed the foreground. Select Download again when Pip is idle.")
        if waiting and self.heartbeat is None:
            return
        if self.heartbeat is None or time.monotonic() - self.heartbeat > BEAT_MAX_AGE or self.state not in IDLE_STATES:
            raise ValueError("Pip's activity signal is unavailable.")


@contextmanager
def foreground_monitor():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as pipe:
        pipe.settimeout(CONNECT_SECONDS)
        pipe.connect(bus.socket_path())
        pipe.sendall(b'{"t":"hello","name":"update-download","sub":["state.changed","stem.beat"]}\n')
        pipe.setblocking(False)
        monitor = Foreground(pipe)
        # Wait for the first heartbeat, which is normally sent every 500 ms.
        # The initial snapshot may arrive in its own packet.
        deadline = time.monotonic() + BEAT_MAX_AGE
        while monitor.heartbeat is None:
            ready, _, _ = select.select([pipe], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                raise ValueError("Pip's activity signal is unavailable.")
            monitor.check(waiting=True)
        yield monitor.check


def _resolve(host, port, **_protocol):
    """Stop a stalled system resolver before returning any candidate address."""
    try:
        reply = subprocess.run([sys.executable, "-I", "-c", RESOLVER, host, str(port)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=DNS_SECONDS, check=True)
    except subprocess.SubprocessError as exc:
        raise OSError("Could not resolve the approved update source in time.") from exc
    return [(family, kind, proto, name, tuple(address))
            for family, kind, proto, name, address in json.loads(reply.stdout)]


@contextmanager
def _socket_deadline(sock, deadline):
    watchdog = threading.Timer(max(0, deadline - time.monotonic()), _end_socket_io, args=(sock,))
    watchdog.daemon = True
    try:
        watchdog.start()
        yield
    finally:
        watchdog.cancel()
        if watchdog.ident is not None:
            watchdog.join()


def _download(release, stream, permitted, progress):
    url = release.url
    deadline = time.monotonic() + MAX_TRANSFER_SECONDS
    for hop in range(MAX_REDIRECTS + 1):
        parsed = _endpoint(url)
        if parsed.hostname not in release.hosts:
            raise ValueError("The update redirected outside the approved release hosts.")
        dial_deadline = min(deadline, time.monotonic() + CONNECT_SECONDS)
        connection = _PublicHTTPSConnection(parsed.hostname, deadline=dial_deadline,
                                            permitted=permitted, resolver=_resolve)
        try:
            connection.connect()
            sock = connection.sock
            sock.settimeout(IO_SECONDS)
            permitted()
            target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            with _socket_deadline(sock, min(deadline, time.monotonic() + CONNECT_SECONDS)):
                connection.request("GET", target, headers={"Accept-Encoding": "identity"})
                response = connection.getresponse()
            with response:
                if response.status in (301, 302, 303, 307, 308):
                    locations = response.headers.get_all("Location", [])
                    if hop == MAX_REDIRECTS or len(locations) != 1 or len(locations[0]) > 4096:
                        raise ValueError("The approved update returned an invalid redirect.")
                    url = urljoin(url, locations[0])
                    continue
                if response.status != 200:
                    raise ValueError("The approved source could not provide the update.")
                with _socket_deadline(sock, deadline):
                    _copy_response(release, response, stream, permitted, progress)
                return
        finally:
            connection.close()


def _copy_response(release, response, stream, permitted, progress):
    length = response.headers.get_all("Content-Length", [])
    if (length and length != [str(release.bytes)] or response.getheader("Content-Encoding", "identity") != "identity"
            or response.getheader("Content-Range") is not None):
        raise ValueError("The update response did not match the approved release size or format.")
    digest, size = hashlib.sha256(), 0
    next_progress = 0.0
    while True:
        permitted()
        block = response.read1(min(CHUNK, release.bytes - size + 1))
        permitted()
        if not block:
            break
        size += len(block)
        if size > release.bytes:
            raise ValueError("The update exceeded the approved release size.")
        stream.write(block)
        digest.update(block)
        now = time.monotonic()
        if now >= next_progress or size == release.bytes:
            progress(size, release.bytes)
            next_progress = now + 0.2
    if size != release.bytes or digest.hexdigest() != release.sha256:
        raise ValueError("The downloaded update did not match the approved release checksum and size.")


def _bound_archive(mind):
    guids, _ = install_binding.installed_mapping(update_slots.CMDLINE.read_text())
    device = os.stat(mind).st_dev
    if not os.path.ismount(mind) or f"{os.major(device)}:{os.minor(device)}" != install_binding.node_number(
            str(install_binding.PARTUUIDS / guids[-1])):
        raise ValueError("The installed mind partition is unavailable for staging the update.")


@contextmanager
def _stage_lock(directory):
    private_directory(directory)
    with os.fdopen(os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), "rb") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError("The update download lock is unavailable.")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another update download is in progress.") from None
        yield directory / "release.part", directory / "release.raucb"


def _clear_previous(pending, bundle, install_idle):
    if os.path.lexists(bundle) and not install_idle():
        raise ValueError("The previous update may still be in use. Check Update again after the installer is idle.")
    pending.unlink(missing_ok=True)
    bundle.unlink(missing_ok=True)


def release_previous(mind, *, install_idle):
    """Reclaim an interrupted stage only after any installer using it has stopped."""
    mind = Path(mind)
    directory = mind / STAGING
    if not os.path.lexists(directory):
        return
    _bound_archive(mind)
    with _stage_lock(directory) as (pending, bundle):
        _clear_previous(pending, bundle, install_idle)


def _reserve_bytes(mind):
    archive_bytes = 0
    checkpoint = Path(backup.UPDATE_CHECKPOINT_DIR) / backup.UPDATE_CHECKPOINT_FILE
    for name in (backup.DB_FILE, f"{backup.DB_FILE}-wal", backup.LISTS_FILE, f"{backup.LISTS_FILE}-wal", checkpoint):
        try:
            archive_bytes += (mind / name).stat().st_size
        except FileNotFoundError:
            continue
    return MIN_ARCHIVE_FREE + archive_bytes


@contextmanager
def staged_bundle(mind, release, *, install_idle, progress=lambda _done, _total: None, cancel=lambda: False):
    """Keep a verified private bundle while the update screen or RAUC can use it."""
    mind = Path(mind)
    update_slots.check_update_admission()
    _bound_archive(mind)
    if not internet_enabled(mind):
        raise ValueError("Downloading an update requires Internet mode.")
    directory = mind / STAGING
    with _stage_lock(directory) as (pending, bundle):
        _clear_previous(pending, bundle, install_idle)
        free = os.statvfs(directory)
        reserve = _reserve_bytes(mind)
        if release.bytes + reserve > free.f_bavail * free.f_frsize:
            raise ValueError("There is not enough space to stage the update and keep the archive writable.")

        def report(done, total):
            current = os.statvfs(directory)
            if total - done + reserve > current.f_bavail * current.f_frsize:
                raise ValueError("The archive partition ran low on space during the update download.")
            progress(done, total)

        try:
            with foreground_monitor() as foreground:
                def permitted():
                    if cancel():
                        raise ValueError("Download stopped.")
                    if not internet_enabled(mind):
                        raise ValueError("Internet mode was turned off. Try downloading again when it is enabled.")
                    foreground()
                    return True

                fd = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    try:
                        _download(release, stream, permitted, report)
                    except (http.client.HTTPException, LookupDenied, OSError) as cause:
                        raise ValueError("The update transfer did not finish. Select Download again to start over.") from cause
                    stream.flush()
                    os.fsync(stream.fileno())
                    permitted()
            os.replace(pending, bundle)
            sync_directory(directory)
            yield bundle
        finally:
            pending.unlink(missing_ok=True)
            if os.path.lexists(bundle) and install_idle():
                bundle.unlink()
                sync_directory(directory)
