"""Prepare or sign an optional Open English WordNet dictionary pack.

The source is intentionally supplied, not downloaded. A prepared database must carry
metadata source="Open English WordNet" and license="CC BY 4.0", plus a
definitions(word, word_key, part_of_speech, definition, sense_rank) table.
The pinned release route verifies the official WN-LMF archive and both
upstream licence notices before conversion.
"""

import argparse
import gzip
import hashlib
import io
import os
import shutil
import sqlite3
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from lab.package_model import render_descriptor, sign
from mind.library import DICTIONARY_MAX_BYTES, OEWN_LICENSE, OEWN_SOURCE
from mind.model_pack import FORMAT_V2, check_descriptor, envelope_name, metadata_bytes, verify_envelope

MAX_LMF_BYTES = 1024 * 1024 * 1024
MAX_SYNSETS = 250_000
MAX_DEFINITIONS = 500_000
OEWN_2025 = "oewn-2025-edition"
OEWN_2025_ARCHIVE = "english-wordnet-2025.xml.gz"
OEWN_2025_DOCTYPE = b'<!DOCTYPE LexicalResource SYSTEM "http://globalwordnet.github.io/schemas/WN-LMF-1.3.dtd">'
OEWN_2025_INPUTS = {
    OEWN_2025_ARCHIVE: (11363503, "9ca6d1dcb75f822fdd66617f7d9da48142ace38dd544d6ad5e2feca1674ad3fe"),
    "LICENSE.md": (32768, "672cc8b5663e8dc74c4b07a9dcf477193853575b119908fd3dc0aeeb60a9dbbb"),
    "WNDB_License.txt": (8192, "df30ec18fbabcdaf031b79ea026d3e6b959010cffe6dd7be9ac137822175b904"),
}


def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _text(element):
    return " ".join("".join(element.itertext()).split())


def prepare_oewn_2025(source_dir, xml, license_file):
    """Read the pinned archive and write parseable XML and both licence notices."""
    inputs = {}
    for name, (limit, expected) in OEWN_2025_INPUTS.items():
        path = source_dir / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"expected a regular release input: {name}")
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit or hashlib.sha256(content).hexdigest() != expected:
            raise ValueError(f"Open English WordNet release hash mismatch: {name}")
        inputs[name] = content

    with gzip.GzipFile(fileobj=io.BytesIO(inputs[OEWN_2025_ARCHIVE])) as source, xml.open("wb") as target:
        declaration, doctype = source.readline(256), source.readline(256)
        if not declaration.startswith(b'<?xml ') or doctype.rstrip(b"\r\n") != OEWN_2025_DOCTYPE:
            raise ValueError("Open English WordNet XML header differs from the pinned release")
        # The corpus uses no custom entities. Do not load the external schema.
        target.write(declaration)
        remaining = MAX_LMF_BYTES - len(declaration)
        for block in iter(lambda: source.read(1 << 20), b""):
            remaining -= len(block)
            if remaining < 0:
                raise ValueError("Open English WordNet XML exceeds the Tier 1 input bound")
            target.write(block)

    attribution = (
        b"PipOS dictionary adapted from Open English WordNet, 2025 edition, "
        b"by the Open English WordNet team and derived from Princeton University WordNet 3.1.\n"
        b"PipOS extracts headwords and definitions, normalizes spacing and lookup keys, "
        b"bounds definition length, and stores the results in SQLite.\n"
        b"Source: https://github.com/globalwordnet/english-wordnet/releases/tag/2025-edition\n\n"
    )
    license_file.write_bytes(
        attribution + inputs["LICENSE.md"] + b"\n\n--- WNDB_License.txt ---\n\n" + inputs["WNDB_License.txt"])


def convert_lmf(source, destination, snapshot):
    """Convert the canonical WN-LMF XML shape to the bounded runtime schema.

    The converter reads local XML and performs no network access. Synsets
    are read first, then lexical entries, because canonical OEWN XML places
    entries before their referenced synsets. Bounds fail closed on a corpus
    with an unexpectedly different scale or shape.
    """
    source, destination = Path(source), Path(destination)
    if (not source.is_file() or source.is_symlink() or not 0 < source.stat().st_size <= MAX_LMF_BYTES):
        raise ValueError("WN-LMF XML is missing, linked, empty or over 1 GiB")
    with open(source, "rb") as stream:
        tail = b""
        for block in iter(lambda: stream.read(1 << 20), b""):
            combined = (tail + block).upper()
            if b"<!DOCTYPE" in combined:
                raise ValueError("WN-LMF XML must not contain a document type declaration")
            tail = combined[-16:]
    synsets = {}
    for _, element in ET.iterparse(source, events=("end",)):
        kind = _tag(element)
        if kind == "Synset":
            synset_id = element.get("id")
            definitions = [_text(child) for child in element if _tag(child) == "Definition"]
            definition = next((value for value in definitions if value), "")
            if synset_id and definition:
                if len(synsets) >= MAX_SYNSETS:
                    raise ValueError("WN-LMF XML exceeds the Tier 1 synset bound")
                synsets[synset_id] = (element.get("partOfSpeech") or "", definition[:500])
            element.clear()
        elif kind == "LexicalEntry":
            element.clear()
    if not synsets:
        raise ValueError("WN-LMF XML contains no defined synsets")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("dictionary conversion destination already exists")
    db = sqlite3.connect(destination)
    try:
        db.executescript("""
            PRAGMA page_size=4096;
            PRAGMA journal_mode=OFF;
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE definitions(
                word TEXT NOT NULL,
                word_key TEXT NOT NULL,
                part_of_speech TEXT NOT NULL,
                definition TEXT NOT NULL,
                sense_rank INTEGER NOT NULL CHECK(sense_rank > 0)
            );
            CREATE INDEX definitions_word ON definitions(word_key, sense_rank);
        """)
        db.executemany("INSERT INTO metadata VALUES (?, ?)",
                       (("source", OEWN_SOURCE), ("license", OEWN_LICENSE), ("snapshot", snapshot)))
        ranks, count = defaultdict(int), 0
        for _, element in ET.iterparse(source, events=("end",)):
            kind = _tag(element)
            if kind != "LexicalEntry":
                if kind == "Synset":
                    element.clear()
                continue
            lemma = next((child for child in element if _tag(child) == "Lemma"), None)
            word = " ".join((lemma.get("writtenForm", "") if lemma is not None else "").split())
            word_key = unicodedata.normalize("NFKC", word).casefold()
            if word and 0 < len(word) <= 100 and 0 < len(word_key) <= 100:
                for sense in (child for child in element if _tag(child) == "Sense"):
                    target = synsets.get(sense.get("synset"))
                    if target is None:
                        continue
                    part, definition = target
                    ranks[word_key] += 1
                    db.execute("INSERT INTO definitions VALUES (?, ?, ?, ?, ?)",
                               (word, word_key, part or lemma.get("partOfSpeech") or "word",
                                definition, ranks[word_key]))
                    count += 1
                    if count > MAX_DEFINITIONS:
                        raise ValueError("WN-LMF XML exceeds the Tier 1 definition bound")
            element.clear()
        if not count:
            raise ValueError("WN-LMF XML produced no word definitions")
        db.commit()
        db.execute("VACUUM")
    except Exception:
        db.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        db.close()
    validate(destination)
    return count


def digest(path):
    value = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            size += len(block)
            value.update(block)
    return size, value.hexdigest()


def validate(source):
    source = Path(source)
    if not source.is_file() or source.is_symlink() or not 0 < source.stat().st_size <= DICTIONARY_MAX_BYTES:
        raise ValueError("dictionary is missing, linked, empty or over the 512 MiB Tier 1 budget")
    with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro&immutable=1", uri=True)) as db:
        metadata = dict(db.execute("SELECT key, value FROM metadata WHERE key IN ('source', 'license')"))
        columns = {row[1] for row in db.execute("PRAGMA table_info(definitions)")}
        if metadata != {"source": OEWN_SOURCE, "license": OEWN_LICENSE}:
            raise ValueError("dictionary metadata is not Open English WordNet under CC BY 4.0")
        required = {"word", "word_key", "part_of_speech", "definition", "sense_rank"}
        if not required.issubset(columns):
            raise ValueError("dictionary definitions table has the wrong schema")
        integrity = db.execute("PRAGMA integrity_check(1)").fetchone()
        if integrity != ("ok",):
            raise ValueError("dictionary SQLite integrity check failed")


def stage(source, license_file, output, snapshot, *, certificate=None, key=None):
    if (certificate is None) != (key is None):
        raise ValueError("signing requires both a certificate and a key")
    if not isinstance(snapshot, str) or not 0 < len(snapshot) <= 96 or not snapshot.strip() or not snapshot.isprintable():
        raise ValueError("supply a nonempty printable snapshot name of up to 96 characters")
    licence_bytes = metadata_bytes(Path(license_file), 64 * 1024)
    licence = licence_bytes.decode("utf-8")
    if ("Attribution 4.0 International" not in licence
            or "creativecommons.org/licenses/by/4.0" not in licence.casefold()):
        raise ValueError("supplied licence is not identifiable as CC BY 4.0")
    output = Path(output)
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("dictionary staging directory must be empty")
    source = Path(source)
    if source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= DICTIONARY_MAX_BYTES:
        raise ValueError("dictionary is missing, linked, empty or over the 512 MiB Tier 1 budget")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".oewn-stage-", dir=output.parent) as folder:
        stage_root = Path(folder) / "pack"
        base = stage_root / "library/oewn"
        base.mkdir(parents=True)
        database = base / "dictionary.sqlite3"
        shutil.copyfile(source, database, follow_symlinks=False)
        validate(database)
        (base / "LICENSE").write_bytes(licence_bytes)
        files = []
        for name in ("LICENSE", "dictionary.sqlite3"):
            size, sha256 = digest(base / name)
            files.append({"path": name, "bytes": size, "sha256": sha256})
        pack = {"format": FORMAT_V2, "role": "library", "dir": "library/oewn",
                "name": OEWN_SOURCE, "license": OEWN_LICENSE, "snapshot": snapshot, "files": files}
        check_descriptor(pack, "library")
        info = render_descriptor(pack)
        descriptor = base / "pack.json"
        descriptor.write_bytes(info)
        if certificate is not None:
            envelope = base / envelope_name("library")
            envelope.write_bytes(sign(info, certificate, key))
            verify_envelope(stage_root, envelope, certificate, descriptor, "library")
        os.replace(stage_root, output)
    return pack


def package(source, license_file, output, certificate, key, snapshot):
    return stage(source, license_file, output, snapshot, certificate=certificate, key=key)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--source", type=Path, help="prepared dictionary SQLite database")
    inputs.add_argument("--lmf", type=Path, help="canonical Open English WordNet WN-LMF XML")
    inputs.add_argument("--oewn-2025", type=Path, help="directory holding the pinned XML archive and both upstream licences")
    parser.add_argument("--license", type=Path)
    parser.add_argument("--snapshot")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--prepare-only", action="store_true", help="write the checked database and unsigned descriptor")
    parser.add_argument("--certificate", type=Path)
    parser.add_argument("--key", type=Path)
    args = parser.parse_args(argv)
    if args.prepare_only and (args.certificate is not None or args.key is not None):
        parser.error("--prepare-only is used without signing credentials")
    if not args.prepare_only and (args.certificate is None or args.key is None):
        parser.error("signing requires both --certificate and --key")
    if args.oewn_2025 is not None:
        if args.license is not None or args.snapshot is not None:
            parser.error("--oewn-2025 supplies its own licence notices and snapshot label")
    elif args.license is None or not args.snapshot:
        parser.error("--source and --lmf require --license and --snapshot")
    try:
        if args.source is not None:
            pack = stage(args.source, args.license, args.output, args.snapshot,
                         certificate=args.certificate, key=args.key)
        else:
            with tempfile.TemporaryDirectory(prefix="being-oewn-") as folder:
                scratch = Path(folder)
                converted = scratch / "dictionary.sqlite3"
                lmf, licence, snapshot = args.lmf, args.license, args.snapshot
                if args.oewn_2025 is not None:
                    lmf, licence, snapshot = scratch / "wordnet.xml", scratch / "LICENSE", OEWN_2025
                    prepare_oewn_2025(args.oewn_2025, lmf, licence)
                convert_lmf(lmf, converted, snapshot)
                pack = stage(converted, licence, args.output, snapshot,
                             certificate=args.certificate, key=args.key)
    except (EOFError, OSError, ValueError, sqlite3.Error, ET.ParseError) as error:
        parser.exit(1, f"Could not package dictionary: {error}\n")
    state = "Prepared unsigned" if args.prepare_only else "Signed"
    print(f"{state} {pack['name']} ({pack['snapshot']}) under {pack['license']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
