"""Prepare or sign a locally supplied Simple English Wikipedia library.

No corpus is downloaded. The preferred input is one named official compressed
snapshot with its published size and digests pinned below. The caller also
supplies the CC BY-SA 4.0 licence text and written attribution. Conversion is
deliberately conservative: only main-namespace pages whose opening prose can
be made markup-free are kept.
"""

import argparse
import bz2
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import xml.etree.ElementTree as ET
from contextlib import closing, contextmanager
from pathlib import Path

from lab.package_model import render_descriptor, sign
from mind.library import ENCYCLOPEDIA_MAX_BYTES, SIMPLE_LICENSE, SIMPLE_SOURCE
from mind.model_pack import FORMAT_V2, check_descriptor, envelope_name, metadata_bytes, verify_envelope

MAX_XML_BYTES = 8 * 1024 * 1024 * 1024
MAX_PAGES = 500_000
MAX_REDIRECTS = 1_000_000
MAX_TEXT = 2 * 1024 * 1024
# Wikimedia dumpstatus.json and the edition's published checksum files reported
# these fields on 26 Nov 2026. Do not move "latest" without a source review.
PINNED_SNAPSHOT = "2026-11-01"
PINNED_DUMP = "simplewiki-20261101-pages-articles.xml.bz2"
PINNED_DUMP_BYTES = 360_149_050
PINNED_DUMP_MD5 = "ec73eb9f3e3778e3061a4586f95aa309"
PINNED_DUMP_SHA1 = "0b040b8e4e6e58de4bfbafee6050bd6bbf991f19"
PINNED_DUMP_URL = f"https://dumps.wikimedia.org/simplewiki/20261101/{PINNED_DUMP}"
MARKUP = re.compile(r"(?:\{\{|\}\}|\[\[|\]\]|<[^>]+>|'''?|={2,}|__\w+__)")
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
REF = re.compile(r"<ref\b[^>]*?(?:/>|>.*?</ref\s*>)", re.DOTALL | re.IGNORECASE)
LINK = re.compile(r"\[\[(?:[^]|]+\|)?([^]]+)\]\]")
EXTERNAL = re.compile(r"\[(?:https?://\S+)(?:\s+([^]]+))?\]")
EMPHASIS = re.compile(r"(?<!')(?P<mark>'{2,3})(?!')([^\n]{1,200}?)(?<!')(?P=mark)(?!')")
SECTION = re.compile(r"(?m)^[ \t]*={2,}[^\n]+={2,}[ \t]*$")
QUALIFIER = re.compile(r"\s+\([^()]{1,100}\)$")
SENTENCE = re.compile(r"(?<=[.!?])\s+")


class _BoundedReader:
    """A file-like reader that refuses an oversized decompressed stream."""

    def __init__(self, source, limit):
        self.source, self.limit, self.seen = source, limit, 0

    def read(self, size=-1):
        remaining = self.limit - self.seen
        # Never ask the decompressor to allocate beyond the bound even when a
        # consumer uses read-all semantics.
        request = remaining + 1 if size < 0 else min(size, remaining + 1)
        block = self.source.read(request)
        self.seen += len(block)
        if self.seen > self.limit:
            raise ValueError("decompressed Simple English XML exceeds 8 GiB")
        return block

    def close(self):
        self.source.close()


def _stream_hashes(stream, *names):
    values, size = [hashlib.new(name) for name in names], 0
    stream.seek(0)
    for block in iter(lambda: stream.read(1 << 20), b""):
        size += len(block)
        for value in values:
            value.update(block)
    stream.seek(0)
    return (size, *(value.hexdigest() for value in values))


@contextmanager
def _regular_input(path):
    source = Path(path)
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(source, flags)
    stream = os.fdopen(fd, "rb")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Simple English input is not a regular file")
        yield source, stream, info.st_size
    finally:
        stream.close()


def verify_pinned_dump(path):
    with _regular_input(path) as (source, stream, size):
        if source.name != PINNED_DUMP or size != PINNED_DUMP_BYTES:
            raise ValueError(f"pinned dump must be the {PINNED_DUMP_BYTES}-byte file {PINNED_DUMP}")
        measured = _stream_hashes(stream, "sha1", "md5")
        if measured != (PINNED_DUMP_BYTES, PINNED_DUMP_SHA1, PINNED_DUMP_MD5):
            raise ValueError("pinned Simple English dump official digest differs")
    return Path(path)


def verify_uncompressed_xml(path, expected):
    with _regular_input(path) as (_source, stream, size):
        if not 0 < size <= MAX_XML_BYTES:
            raise ValueError("Simple English XML is empty or over 8 GiB")
        if _stream_hashes(stream, "sha256")[1] != expected:
            raise ValueError("uncompressed XML SHA-256 differs")
    return Path(path)


def validate_attribution(path, snapshot, *, pinned=False):
    return _check_attribution(metadata_bytes(Path(path), 8192), snapshot, pinned=pinned)


def _check_attribution(notice, snapshot, *, pinned=False):
    text = " ".join(notice.decode("utf-8").split())
    source = "dumps.wikimedia.org/simplewiki/20261101/" if pinned else "dumps.wikimedia.org/"
    required = ("Simple English Wikipedia contributors", snapshot, "CC BY-SA 4.0", source)
    if (not text or len(text) > 2000 or any(value.casefold() not in text.casefold() for value in required)
            or not any(word in text.casefold() for word in ("adapted", "modified", "extracted"))):
        raise ValueError("attribution must name contributors, snapshot, licence, dump URL and the extraction")
    return text


def validate_license(path):
    notice = metadata_bytes(Path(path), 64 * 1024)
    text = notice.decode("utf-8")
    if ("Attribution-ShareAlike 4.0 International" not in text
            or "creativecommons.org/licenses/by-sa/4.0" not in text.casefold()):
        raise ValueError("supplied licence is not identifiable as CC BY-SA 4.0")
    return notice


@contextmanager
def xml_input(path, *, compressed=False, xml_sha256=None):
    with _regular_input(path) as (source, raw, size):
        if compressed:
            if source.name != PINNED_DUMP or size != PINNED_DUMP_BYTES:
                raise ValueError(f"pinned dump must be the {PINNED_DUMP_BYTES}-byte file {PINNED_DUMP}")
            if _stream_hashes(raw, "sha1", "md5") != (
                    PINNED_DUMP_BYTES, PINNED_DUMP_SHA1, PINNED_DUMP_MD5):
                raise ValueError("pinned Simple English dump official digest differs")
            stream = _BoundedReader(bz2.BZ2File(raw, "rb"), MAX_XML_BYTES)
        else:
            if not 0 < size <= MAX_XML_BYTES:
                raise ValueError("Simple English XML is empty or over 8 GiB")
            if _stream_hashes(raw, "sha256")[1] != xml_sha256:
                raise ValueError("uncompressed XML SHA-256 differs")
            stream = raw
        try:
            yield stream
        finally:
            if stream is not raw:
                stream.close()


def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _child(element, name):
    return next((child for child in element.iter() if _tag(child) == name), None)


def _key(title):
    return " ".join(title.replace("_", " ").split()).casefold()


def _skip_leading_blocks(text):
    """Discard only balanced leading templates and tables."""
    text = text.lstrip()
    while text.startswith(("{{", "{|")):
        stack = []
        pairs = {"{{": "}}", "{|": "|}"}
        for match in re.finditer(r"\{\{|\{\||\}\}|\|\}", text):
            token = match[0]
            if token in pairs:
                stack.append(pairs[token])
            elif stack[-1] == token:
                stack.pop()
                if not stack:
                    text = text[match.end():].lstrip()
                    break
        else:
            return ""
    return text


def spoken_opening(raw):
    """Return bounded first and following prose, or None for unsafe markup."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_TEXT:
        return None
    text = COMMENT.sub(" ", REF.sub(" ", raw))
    text = SECTION.split(_skip_leading_blocks(text), maxsplit=1)[0]
    text = EMPHASIS.sub(r"\2", text)
    text = LINK.sub(r"\1", text)
    text = EXTERNAL.sub(lambda match: match[1] or "", text)
    text = re.sub(r"(?m)^[ \t]*(?:\||\!|\*|#|;|:).*$", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if MARKUP.search(text):
        return None
    sentences = [part.strip() for part in SENTENCE.split(text) if part.strip()]
    if not sentences:
        return None
    first, used = [], 0
    for sentence in sentences:
        words = sentence.split()
        if not sentence.endswith((".", "!", "?")) or used + len(words) > 45:
            break
        if len(sentence) > 500:
            return None
        first.append(sentence)
        used += len(words)
        if len(first) == 2 or used >= 45:
            break
    spoken = " ".join(first)
    more = ""
    for sentence in sentences[len(first):len(first) + 3]:
        next_excerpt = f"{more} {sentence}".strip()
        if len(next_excerpt) > 600 or not sentence.endswith((".", "!", "?")):
            break
        more = next_excerpt
    return (spoken, more) if 1 <= len(spoken) <= 600 else None


def _linked_redirects(redirects, article_keys):
    """Keep only short, acyclic chains that reach a retained article."""
    live = []
    targets = {}
    for title, key, target in redirects:
        if key not in article_keys and "#" not in target:
            targets.setdefault(key, (title, _key(target)))
    for key, (title, target) in targets.items():
        visited = {key}
        for _ in range(12):
            if target in article_keys:
                live.append((title, key, target))
                break
            if target in visited or target not in targets:
                break
            visited.add(target)
            target = targets[target][1]
    return live


def convert(xml, destination, *, snapshot, credit, preview=False, compressed=False, xml_sha256=None):
    source = Path(xml)
    credit_text = validate_attribution(credit, snapshot, pinned=compressed)
    pages, redirects, alternatives, dropped = [], [], [], 0
    with xml_input(source, compressed=compressed, xml_sha256=xml_sha256) as stream:
        for _event, element in ET.iterparse(stream, events=("end",)):
            if _tag(element) != "page":
                continue
            title_node, ns_node, text_node = _child(element, "title"), _child(element, "ns"), _child(element, "text")
            title = " ".join(((title_node.text if title_node is not None else "") or "").split())
            if (not title or len(title) > 256
                    or (ns_node is not None and (ns_node.text or "0") != "0")):
                element.clear()
                continue
            redirect = _child(element, "redirect")
            if redirect is not None and redirect.get("title"):
                redirects.append((title, _key(title), redirect.get("title")))
            else:
                result = spoken_opening((text_node.text if text_node is not None else "") or "")
                if result is None:
                    dropped += 1
                else:
                    pages.append((title, _key(title), *result))
                    base = _key(QUALIFIER.sub("", title))
                    if base and base != _key(title):
                        alternatives.append((base, _key(title)))
            if len(pages) > MAX_PAGES or len(redirects) > MAX_REDIRECTS:
                raise ValueError("corpus exceeds the bounded Simple English tier")
            element.clear()
    article_keys = {page[1] for page in pages}
    redirects = _linked_redirects(redirects, article_keys)
    counts = {"articles": len(article_keys), "redirects": len(redirects), "dropped": dropped}
    if preview:
        return counts
    destination = Path(destination)
    if destination.exists():
        raise ValueError("conversion destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(destination)
    try:
        db.executescript("""
          PRAGMA page_size=4096; PRAGMA journal_mode=OFF;
          CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
          CREATE TABLE articles(title TEXT NOT NULL, title_key TEXT PRIMARY KEY, spoken TEXT NOT NULL, more TEXT NOT NULL) WITHOUT ROWID;
          CREATE TABLE redirects(title TEXT NOT NULL, title_key TEXT PRIMARY KEY, target_key TEXT NOT NULL) WITHOUT ROWID;
          CREATE TABLE alternatives(query_key TEXT NOT NULL, title_key TEXT NOT NULL,
              PRIMARY KEY(query_key, title_key)) WITHOUT ROWID;
        """)
        db.executemany("INSERT INTO metadata VALUES (?,?)", (("source", SIMPLE_SOURCE), ("license", SIMPLE_LICENSE),
                       ("snapshot", snapshot), ("credit", credit_text), ("schema", "2")))
        db.executemany("INSERT OR IGNORE INTO articles VALUES (?,?,?,?)", pages)
        db.executemany("INSERT OR IGNORE INTO redirects VALUES (?,?,?)", redirects)
        db.executemany("INSERT OR IGNORE INTO alternatives VALUES (?,?)", alternatives)
        db.commit()
        db.execute("VACUUM")
    except Exception:
        db.close()
        destination.unlink(missing_ok=True)
        raise
    db.close()
    if destination.stat().st_size > ENCYCLOPEDIA_MAX_BYTES:
        destination.unlink(missing_ok=True)
        raise ValueError("converted library exceeds the 1 GiB capacity ceiling")
    return counts


def digest(path):
    value, size = hashlib.sha256(), 0
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            size += len(block)
            value.update(block)
    return size, value.hexdigest()


def validate(database, snapshot):
    source = Path(database)
    if source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= ENCYCLOPEDIA_MAX_BYTES:
        raise ValueError("Simple English database is missing, linked, empty or over 1 GiB")
    with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro&immutable=1", uri=True)) as db:
        metadata = dict(db.execute("SELECT key, value FROM metadata WHERE key IN ('source','license','snapshot','credit','schema')"))
        if (metadata.get("source") != SIMPLE_SOURCE or metadata.get("license") != SIMPLE_LICENSE
                or metadata.get("snapshot") != snapshot or not metadata.get("credit")
                or metadata.get("schema") != "2"):
            raise ValueError("database metadata does not identify the supplied Simple English snapshot")
        articles = {row[1] for row in db.execute("PRAGMA table_info(articles)")}
        redirects = {row[1] for row in db.execute("PRAGMA table_info(redirects)")}
        alternatives = {row[1] for row in db.execute("PRAGMA table_info(alternatives)")}
        if not {"title", "title_key", "spoken", "more"}.issubset(articles) \
                or not {"title", "title_key", "target_key"}.issubset(redirects) \
                or not {"query_key", "title_key"}.issubset(alternatives):
            raise ValueError("Simple English database has the wrong schema")
        if db.execute("SELECT 1 FROM articles WHERE length(spoken) NOT BETWEEN 1 AND 600 "
                      "OR length(more) > 600 LIMIT 1").fetchone():
            raise ValueError("Simple English article exceeds its spoken bounds")
        if db.execute("SELECT 1 FROM alternatives x LEFT JOIN articles a ON a.title_key=x.title_key "
                      "WHERE a.title_key IS NULL LIMIT 1").fetchone():
            raise ValueError("Simple English alternative has no article")
        if db.execute("SELECT 1 FROM redirects r LEFT JOIN articles a ON a.title_key=r.target_key "
                      "WHERE a.title_key IS NULL LIMIT 1").fetchone():
            raise ValueError("Simple English redirect has no article")
        if db.execute("PRAGMA integrity_check(1)").fetchone() != ("ok",):
            raise ValueError("Simple English database integrity check failed")
        return metadata


def package(database, license_file, attribution, output, certificate, key, snapshot):
    if (certificate is None) != (key is None):
        raise ValueError("signing requires both a certificate and a key")
    licence = validate_license(license_file)
    notice = metadata_bytes(Path(attribution), 8192)
    credit = _check_attribution(notice, snapshot)
    output = Path(output)
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("library staging directory must be empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".simple-en-stage-", dir=output.parent) as folder:
        stage = Path(folder) / "pack"
        base = stage / "library/simple-en"
        base.mkdir(parents=True)
        staged_db = base / "articles.sqlite3"
        shutil.copyfile(database, staged_db, follow_symlinks=False)
        if validate(staged_db, snapshot)["credit"] != credit:
            raise ValueError("written attribution differs from the converted corpus credit")
        (base / "LICENSE").write_bytes(licence)
        (base / "ATTRIBUTION").write_bytes(notice)
        files = []
        for name in ("ATTRIBUTION", "LICENSE", "articles.sqlite3"):
            size, sha256 = digest(base / name)
            files.append({"path": name, "bytes": size, "sha256": sha256})
        pack = {"format": FORMAT_V2, "role": "library_simple", "dir": "library/simple-en",
                "name": SIMPLE_SOURCE, "license": SIMPLE_LICENSE, "snapshot": snapshot, "files": files}
        check_descriptor(pack, "library_simple")
        info = render_descriptor(pack)
        descriptor = base / "pack.json"
        descriptor.write_bytes(info)
        if certificate is not None:
            envelope = base / envelope_name("library_simple")
            envelope.write_bytes(sign(info, certificate, key))
            verify_envelope(stage, envelope, certificate, descriptor, "library_simple")
        os.replace(stage, output)
    return pack


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    intake = parser.add_mutually_exclusive_group(required=True)
    intake.add_argument("--dump", type=Path, help=f"official pinned {PINNED_DUMP}")
    intake.add_argument("--xml", type=Path, help="legacy uncompressed XML (requires --xml-sha256)")
    parser.add_argument("--xml-sha256", help="expected SHA-256 for an explicitly reviewed uncompressed XML")
    parser.add_argument("--license", required=True, type=Path)
    parser.add_argument("--attribution", required=True, type=Path)
    parser.add_argument("--snapshot", help="required provenance label for --xml; pinned automatically for --dump")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--intake-check", action="store_true",
                        help="verify pinned input/licensing and print non-mutating capacity bounds")
    parser.add_argument("--prepare-only", action="store_true",
                        help="write the checked database and unsigned descriptor")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--certificate", type=Path)
    parser.add_argument("--key", type=Path)
    args = parser.parse_args(argv)
    source, compressed = (args.dump, True) if args.dump is not None else (args.xml, False)
    if compressed and args.snapshot not in (None, PINNED_SNAPSHOT):
        parser.error(f"the pinned dump is snapshot {PINNED_SNAPSHOT}")
    if compressed:
        args.snapshot = PINNED_SNAPSHOT
    if not compressed:
        if not args.snapshot:
            parser.error("--xml requires its reviewed --snapshot provenance label")
        if not args.xml_sha256 or not re.fullmatch(r"[0-9a-f]{64}", args.xml_sha256):
            parser.error("--xml requires a lowercase --xml-sha256")
    validate_attribution(args.attribution, args.snapshot, pinned=compressed)
    validate_license(args.license)
    if args.intake_check:
        if compressed:
            verify_pinned_dump(source)
        else:
            verify_uncompressed_xml(source, args.xml_sha256)
    if args.intake_check:
        maximum_payload = (ENCYCLOPEDIA_MAX_BYTES + args.license.stat().st_size
                           + args.attribution.stat().st_size)
        print(json.dumps({"snapshot": args.snapshot, "input": source.name,
                          "input_bytes": source.stat().st_size,
                          "maximum_converted_bytes": ENCYCLOPEDIA_MAX_BYTES,
                          "maximum_payload_bytes_before_signed_metadata": maximum_payload,
                          "installed_fit_check": "being-library import-simple --check --source STAGE/library/simple-en",
                          "official_url": PINNED_DUMP_URL if compressed else None}, sort_keys=True))
        return 0
    if args.preview:
        counts = convert(source, Path(), snapshot=args.snapshot, credit=args.attribution,
                         preview=True, compressed=compressed, xml_sha256=args.xml_sha256)
        print(f"Would keep {counts['articles']} articles and {counts['redirects']} redirects; drop {counts['dropped']} unsafe pages.")
        return 0
    if args.output is None:
        parser.error("--output is required unless --preview or --intake-check is used")
    if args.prepare_only and (args.certificate is not None or args.key is not None):
        parser.error("--prepare-only is used without signing credentials")
    if not args.prepare_only and None in (args.certificate, args.key):
        parser.error("signing requires both --certificate and --key")
    with tempfile.TemporaryDirectory(prefix="being-simple-en-") as folder:
        database = Path(folder) / "articles.sqlite3"
        counts = convert(source, database, snapshot=args.snapshot, credit=args.attribution,
                         compressed=compressed, xml_sha256=args.xml_sha256)
        package(database, args.license, args.attribution, args.output, args.certificate, args.key, args.snapshot)
    state = "Prepared unsigned" if args.prepare_only else "Staged signed"
    print(f"{state} {counts['articles']} Simple English articles and {counts['redirects']} redirects.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
