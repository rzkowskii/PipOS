"""Confined, bounded PDF-to-text extraction for an existing Keep object.

The public API passes only a catalog digest to a root launcher.  The launcher
verifies and stages exactly that object in private RAM, then starts the parser
as the locked ``being-pdf`` user with no view of /mind, devices, or the
network.  Owner filenames never enter argv, unit properties, or diagnostics.
"""

import asyncio
import hashlib
import json
import os
import pwd
import re
import selectors
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .keep import Item, Store, document_revision

EXECUTABLE = "/usr/sbin/being-keep-pdf"
UNIT = "being-keep-pdf"
USER = "being-pdf"
PDFTOTEXT = "/usr/bin/pdftotext"
MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024
MAX_REPLY = 2 * MAX_OUTPUT + 4096  # JSON escapes newlines, tabs, quotes and backslashes
MAX_PAGES = 40
DIGEST = re.compile(r"[0-9a-f]{64}")


def _item(state_dir, digest):
    if str(state_dir) != "/mind" or not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError("The PDF extraction request is invalid.")
    reference = document_revision(state_dir, digest)
    if reference is None:
        raise ValueError("That file is no longer in the Keep catalog.")
    item, _ = reference
    if Path(item.filename).suffix.casefold() != ".pdf":
        raise ValueError("That kept file is not a PDF.")
    if not 0 < item.size <= MAX_INPUT:
        raise ValueError("That PDF is too large for bounded text extraction.")
    return item


def _stage(state_dir, item, destination):
    target = Path(destination) / "input.pdf"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    hashed = hashlib.sha256()
    copied = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            with Store(state_dir).open(item.digest) as source:
                while copied < item.size:
                    block = source.read(min(1024 * 1024, item.size - copied))
                    if not block:
                        raise OSError("The kept PDF ended while it was being staged.")
                    copied += len(block)
                    hashed.update(block)
                    output.write(block)
                if source.read(1) or copied != item.size or hashed.hexdigest() != item.digest:
                    raise OSError("The kept PDF did not match its catalog digest.")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _command(source, unit):
    return (
        "systemd-run", f"--unit={unit}", "--quiet", "--wait", "--pipe", "--collect",
        "--service-type=exec", "--property=Type=exec", f"--property=User={USER}",
        f"--property=Group={USER}", "--property=PrivateMounts=yes", "--property=PrivateNetwork=yes",
        "--property=PrivateDevices=yes", "--property=PrivateTmp=yes", "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes", "--property=NoNewPrivileges=yes", "--property=DevicePolicy=closed",
        "--property=CapabilityBoundingSet=", "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=RestrictNamespaces=yes", "--property=RestrictSUIDSGID=yes",
        "--property=LockPersonality=yes", "--property=SystemCallArchitectures=native",
        "--property=ProtectKernelTunables=yes", "--property=ProtectKernelModules=yes",
        "--property=ProtectKernelLogs=yes", "--property=ProtectControlGroups=yes",
        "--property=ProtectClock=yes", "--property=ProtectProc=invisible", "--property=ProcSubset=pid",
        "--property=InaccessiblePaths=/mind /models /var -/boot -/bootstate -/media -/mnt /etc/being",
        # Hide every other host runtime file. The one verified blob is bound
        # separately at /input.pdf; PrivateTmp supplies disposable scratch.
        "--property=TemporaryFileSystem=/run:ro", "--property=UMask=0077",
        f"--property=BindReadOnlyPaths={source}:/input.pdf", "--property=MemoryMax=128M",
        "--property=MemorySwapMax=0", "--property=TasksMax=8", "--property=CPUQuota=50%",
        "--property=RuntimeMaxSec=20s", "--property=TimeoutStopSec=3s",
        "--setenv=PYTHONPATH=/usr/lib/being", "--setenv=PYTHONDONTWRITEBYTECODE=1",
        EXECUTABLE, "worker",
    )


def _bounded_process(command, limit, timeout):
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    output = bytearray()
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise subprocess.TimeoutExpired(command, timeout)
            block = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(output)))
            if not block:
                break
            output.extend(block)
            if len(output) > limit:
                raise ValueError("PDF text exceeds the output limit.")
        code = process.wait(timeout=max(0, deadline - time.monotonic()))
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
    return code, bytes(output)


def launch(state_dir, digest):
    if os.geteuid() != 0:
        raise ValueError("PDF extraction needs the installed private helper.")
    if not os.path.isfile(PDFTOTEXT) or not os.access(PDFTOTEXT, os.X_OK):
        raise ValueError("PDF text extraction is not installed on this image.")
    item = _item(state_dir, digest)
    account = pwd.getpwnam(USER)
    unit = f"{UNIT}-{os.getpid()}-{uuid.uuid4().hex[:12]}"

    def interrupted(*_):
        raise InterruptedError("PDF extraction was cancelled")

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        with tempfile.TemporaryDirectory(prefix="being-pdf-", dir="/run") as directory:
            os.chmod(directory, 0o700)
            source = _stage(state_dir, item, directory)
            os.chown(source, account.pw_uid, account.pw_gid)
            complete = False
            try:
                returncode, output = _bounded_process(_command(source, unit), MAX_REPLY, 25)
                complete = returncode == 0
            except subprocess.TimeoutExpired:
                raise ValueError("PDF extraction took too long and was stopped.") from None
            finally:
                if not complete:
                    try:
                        subprocess.run(("systemctl", "stop", f"{unit}.service"), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
                    except (OSError, subprocess.SubprocessError):
                        pass
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if returncode or len(output) > MAX_REPLY:
        raise ValueError("The confined PDF extractor failed safely.")
    try:
        answer = json.loads(output.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("The confined PDF extractor returned an invalid answer.") from None
    if not isinstance(answer, dict) or set(answer) != {"text", "pages"}:
        raise ValueError("The confined PDF extractor returned an invalid answer.")
    text, pages = answer["text"], answer["pages"]
    if (not isinstance(text, str) or len(text.encode("utf-8")) > MAX_OUTPUT
            or type(pages) is not int or not 1 <= pages <= MAX_PAGES):
        raise ValueError("The confined PDF extractor exceeded its output limits.")
    return text


def worker():
    if not os.path.isfile(PDFTOTEXT) or not os.access(PDFTOTEXT, os.X_OK):
        raise ValueError("PDF parser unavailable")
    code, raw = _bounded_process((PDFTOTEXT, "-f", "1", "-l", str(MAX_PAGES), "-enc", "UTF-8",
                                  "-nopgbrk", "/input.pdf", "-"), MAX_OUTPUT, 15)
    if code:
        raise ValueError("PDF parser rejected the document")
    text = raw.decode("utf-8")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in text):
        raise ValueError("PDF text contains unsafe control characters")
    # Form feeds are removed by -nopgbrk. Report the configured upper bound;
    # callers must not represent it as an observed document page count.
    print(json.dumps({"text": text, "pages": MAX_PAGES}, ensure_ascii=False, separators=(",", ":")))


async def extract(state_dir, item):
    """Return bounded UTF-8 text for one exact current PDF catalog item."""
    if not isinstance(item, Item):
        raise ValueError("Choose one exact kept PDF first.")
    if not available():
        raise ValueError("PDF text extraction is not installed on this image.")
    launch_task = asyncio.create_task(asyncio.create_subprocess_exec(
        EXECUTABLE, "extract", "--mind-dir", str(state_dir), "--digest", item.digest,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=MAX_REPLY + 1))
    process = None
    try:
        process = await asyncio.shield(launch_task)
        output = bytearray()
        async with asyncio.timeout(33):
            while block := await process.stdout.read(min(65536, MAX_REPLY + 1 - len(output))):
                output.extend(block)
                if len(output) > MAX_REPLY:
                    raise ValueError("The confined PDF extractor exceeded its output limit.")
            returncode = await process.wait()
    except BaseException:
        try:
            if process is None:
                process = await launch_task
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 7)
                except TimeoutError:
                    process.kill()
                    await asyncio.wait_for(process.wait(), 2)
        except (OSError, TimeoutError):
            pass
        raise
    return _answer(output, returncode)


def _answer(output, returncode):
    if returncode or len(output) > MAX_REPLY:
        raise ValueError("The confined PDF extractor could not read that document.")
    answer = json.loads(output.decode("utf-8"))
    if not isinstance(answer, dict) or set(answer) != {"ok", "text"} or answer.get("ok") is not True:
        raise ValueError("The confined PDF extractor returned an invalid answer.")
    text = answer["text"]
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_OUTPUT:
        raise ValueError("The confined PDF extractor exceeded its output limit.")
    return text


def available():
    """Whether the fixed parser and privileged launcher are installed."""
    return (os.path.isfile(EXECUTABLE) and os.access(EXECUTABLE, os.X_OK)
            and os.path.isfile(PDFTOTEXT) and os.access(PDFTOTEXT, os.X_OK))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="being-keep-pdf")
    sub = parser.add_subparsers(dest="action", required=True)
    extract_parser = sub.add_parser("extract")
    extract_parser.add_argument("--mind-dir", required=True)
    extract_parser.add_argument("--digest", required=True)
    sub.add_parser("worker")
    args = parser.parse_args(argv)
    try:
        if args.action == "worker":
            worker()
        else:
            text = launch(args.mind_dir, args.digest)
            print(json.dumps({"ok": True, "text": text}, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError):
        print('{"ok":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
