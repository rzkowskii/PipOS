"""Guest-side probes used by the privacy acceptance suite."""

import json
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.request import Request, urlopen

from mind.reminders import Reminders
from mind.usb_logs import Journal


CANARY = "Quixotically"
CANARY_ROOT = "quixot"
CONTROL_ROOT = "zanzibar"
MIND = Path("/mind")
SLOT = "privacy-probe.slot"
WORK = Path(__file__).parent


def cortex(path, body):
    request = Request(f"http://127.0.0.1:8080{path}", json.dumps(body).encode(),
                      {"Content-Type": "application/json"})
    with urlopen(request, timeout=45) as response:
        return json.load(response)


def saved_prompt(path, reported_count=None):
    # llama.cpp v1.0.0's text sequence state has a 12-byte header. Its first
    # payload is server_tokens (null token, version, token count, token IDs,
    # zero media count). The KV state and recurrent checkpoints follow it.
    with path.open("rb") as stream:
        header = stream.read(24)
        assert len(header) == 24, "truncated llama sequence state"
        magic, version, payload_words, sentinel, packed_version, count = struct.unpack("<IIIiII", header)
        assert (magic, version) == (0x67677371, 4), "unknown llama sequence state format"
        assert (sentinel, packed_version) == (-1, 1), "unknown llama prompt format"
        assert count <= 65536, "unexpected prompt length"
        assert reported_count is None or reported_count == count, "slot receipt disagrees with saved token count"
        assert payload_words == count + 4, "expected a text-only prompt"
        tokens = struct.unpack(f"<{count}i", stream.read(count * 4))
        assert all(token >= 0 for token in tokens), "unexpected non-text token"
        assert stream.read(4) == b"\0" * 4, "unexpected llama media state"
        return tokens


def cached_canary(tokens):
    text = cortex("/detokenize", {"tokens": tokens})["content"] if tokens else ""
    return CANARY_ROOT in text.casefold()


def slot_canary(slot_dir, expected_present):
    # Saving reads the live slot. It neither erases the cache nor sends a new
    # completion that could obscure a failed erase by replacing the prompt.
    snapshot = slot_dir / SLOT
    try:
        result = cortex("/slots/0?action=save", {"filename": SLOT})
        count = result.get("n_saved")
        assert type(count) is int and result.get("id_slot") == 0, "invalid slot receipt"
        tokens = saved_prompt(snapshot, count)
    finally:
        snapshot.unlink(missing_ok=True)
    assert cached_canary(tokens) == expected_present, "unexpected canary in slot 0 token IDs"


def seed_reminder():
    with closing(Reminders(MIND)) as reminders:
        reminders.add(CANARY, time.time() + 86400, relative=True)


def contains(path, prefix):
    return path.exists() and prefix.encode() in path.read_bytes().lower()


def archive_canary(root, expected_present):
    archive = root / "mind.db"
    archive_files = (archive, root / "mind.db-wal", root / "mind.db-shm")
    with closing(sqlite3.connect(archive.as_uri() + "?mode=ro", uri=True)) as db:
        for table in ("episodes", "facts"):
            row = db.execute(f"SELECT 1 FROM {table} WHERE lower(text) LIKE ?",
                             (f"%{CANARY_ROOT}%",)).fetchone()
            assert bool(row) == expected_present, f"unexpected canary in {table}"
            fts = table + "_fts"
            query = f"SELECT 1 FROM {fts} WHERE {fts} MATCH ?"
            row = db.execute(query, (CANARY_ROOT + "*",)).fetchone()
            assert bool(row) == expected_present, f"unexpected canary in {fts}"
            assert db.execute(query, (CONTROL_ROOT + "*",)).fetchone(), f"missing retained control in {fts}"
    assert any(contains(path, CONTROL_ROOT) for path in archive_files), "missing retained archive bytes"
    if not expected_present:
        for path in archive_files:
            assert not contains(path, CANARY_ROOT), f"canary remains in {path.name}"


def check_working_files(stage, context_dir, slot_dir):
    before = stage == "before"
    context = context_dir / "context.json"
    assert contains(context, CANARY_ROOT) == before, "unexpected canary in boot-local context"
    reminders = MIND / "reminders.db"
    with closing(sqlite3.connect(reminders.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT 1 FROM reminders WHERE lower(text) = ?", (CANARY.casefold(),)).fetchone()
        assert bool(row) == before, "unexpected canary in reminders"
    if not before:
        assert not contains(reminders, CANARY_ROOT), "canary remains in reminders database bytes"
        assert not contains(MIND / "context.json", CANARY_ROOT), "canary remains in legacy context"
        for saved_slot in dict.fromkeys((slot_dir / "being.slot", slot_dir / "identity.slot",
                                        MIND / "cortex" / "being.slot")):
            try:
                tokens = saved_prompt(saved_slot)
            except FileNotFoundError:
                continue
            assert not cached_canary(tokens), f"canary remains in {saved_slot} token IDs"
    slot_canary(slot_dir, before)


def check_diagnostics():
    output = WORK / "journal.txt"
    journal = Journal(output, WORK / "journal.cursor")
    try:
        # Use the production PIPLOGS export format and add positive redaction
        # controls to the exporter input. The real journal must be clean.
        raw = subprocess.check_output(["journalctl", "--boot", "--no-pager", "--output=short-monotonic"])
        assert CANARY_ROOT.encode() not in raw.lower(), "canary reached the running guest journal"
        for offset in range(0, len(raw), 65536):
            journal.feed(raw[offset:offset + 65536], free=journal.cap * 2)
        journal.feed((f'privacy-fixture: heard: "{CANARY}" (engine fixture)\n'
                      f'privacy-fixture: final transcript text: {CANARY!r}\n').encode(), free=journal.cap * 2)
    finally:
        journal.stop()
    exported = output.read_bytes().lower()
    assert b"privacy-fixture: heard:" in exported, "export redaction control was missing"
    assert CANARY_ROOT.encode() not in exported, "canary reached the PIPLOGS export"


def bus_snapshot():
    with socket.socket(socket.AF_UNIX) as bus:
        bus.settimeout(3)
        bus.connect("/run/being/bus.sock")
        hello = {"t": "hello", "name": "privacy-fixture", "ack": True,
                 "sub": ["state.changed", "app.privacy", "mind.ready"]}
        with bus.makefile("rwb") as stream:
            stream.write((json.dumps(hello) + "\n").encode())
            stream.flush()
            snapshot = {}
            for line in stream:
                event = json.loads(line)
                topic = event.pop("t")
                if topic == "hello.ok":
                    return snapshot
                snapshot[topic] = event
        raise RuntimeError("Brainstem closed the bus before acknowledging the privacy probe")


def main():
    stage = sys.argv[1]
    if stage == "seed":
        seed_reminder()
        archive_canary(MIND, True)
    elif stage in ("before", "after"):
        check_working_files(stage, Path(sys.argv[2]), Path(sys.argv[3]))
        archive_canary(MIND, stage == "before")
    elif stage == "diagnostics":
        check_diagnostics()
    elif stage == "snapshot":
        print(json.dumps(bus_snapshot()))
    else:
        raise ValueError("unknown privacy probe stage")


if __name__ == "__main__":
    main()
