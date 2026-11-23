"""The panel frame header as the renderer writes it and as the face reads it.

being-console --panel encodes its header with panel_encode() in
os/package/being/being-console-panel.h; the face parses it with
parse_header() in crates/face/src/panel.rs. These tests tie the two together:
the byte ranges the encoder writes are exactly the ones the face reads, and a
header the real encoder wrote comes back field for field through
lab/console_preview.py (the reader the live panel tests use).
"""
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from lab import console_preview as preview

ROOT = Path(__file__).resolve().parents[1]
PANEL_H = ROOT / "os/package/being/being-console-panel.h"
PANEL_RS = ROOT / "crates/face/src/panel.rs"
CONSOLE_C = ROOT / "os/package/being/being-console.c"


def function_body(text, start):
    """The text of the braced body that follows `start`."""
    head = text.index(start)
    open_at = text.index("{", head)
    depth = 0
    for index in range(open_at, len(text)):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        if depth == 0:
            return text[open_at:index + 1]
    raise ValueError(f"unbalanced {start}")


def encoder_ranges():
    body = function_body(PANEL_H.read_text(), "static void panel_encode(")
    ranges = {(int(at), int(at) + int(size)) for at, size in re.findall(r"put_le\(out \+ (\d+), [^,]+, (\d+)\)", body)}
    # The title: PANEL_TITLE - 1 bytes, so the last byte stays NUL.
    title = re.findall(r"memcpy\(out \+ (\d+), header->title, PANEL_TITLE - 1\)", body)
    title_bytes = int(re.search(r"PANEL_TITLE = (\d+)", PANEL_H.read_text()).group(1))
    ranges |= {(int(at), int(at) + title_bytes - 1) for at in title}
    return ranges


def face_ranges():
    body = function_body(PANEL_RS.read_text(), "pub fn parse_header(")
    sizes = {"word": 4, "half": 2, "long": 8}
    ranges = {(int(at), int(at) + sizes[kind]) for kind, at in re.findall(r"\b(word|half|long)\(b, (\d+)\)", body)}
    ranges |= {(int(a or 0), int(b)) for a, b in re.findall(r"&b\[(\d*)\.\.(\d+)\]", body)}
    return ranges


class FrameContract(unittest.TestCase):
    def test_the_renderer_writes_the_header_the_face_reads(self):
        self.assertIn('#include "being-console-panel.h"', CONSOLE_C.read_text())
        written, read = encoder_ranges(), face_ranges()
        self.assertTrue(written and read)
        self.assertIn((0, 4), read, "the face checks the magic")
        written.add((0, 4))  # memcpy(out, PANEL_MAGIC, 4)
        # The title: the encoder copies 31 bytes and leaves byte 127 NUL; the
        # face reads all 32.
        written.discard((96, 127))
        written.add((96, 128))
        self.assertEqual(written, read)
        for (a, b), (c, d) in zip(sorted(written), sorted(written)[1:]):
            self.assertLessEqual(b, c, f"overlapping header fields {a}-{b} and {c}-{d}")
        self.assertLessEqual(max(end for _, end in written), 128)

    @unittest.skipUnless(shutil.which("cc"), "no C compiler")
    def test_a_header_the_encoder_wrote_reads_back_field_for_field(self):
        program = r'''
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
@ENCODER@
int main(int argc, char **argv) {
    struct panel_header h;
    uint8_t out[PANEL_HEADER];
    memset(&h, 0, sizeof h);
    h.width = 1280; h.height = 640; h.stride = 5120;
    h.sequence = 0x100000006ull; h.dirty_first = 34; h.dirty_count = 24;
    h.flags = PANEL_VISIBLE | PANEL_PIP_TYPING | PANEL_SCROLLED;
    h.cursor_x = 105; h.cursor_y = -1; h.beat = 9; h.columns = 107; h.rows = 23;
    h.newest_y = 82; h.cell_width = 11; h.cell_height = 24; h.pid = 4242;
    snprintf(h.title, sizeof h.title, "%s", "Pip's terminal");
    panel_encode(&h, out);
    FILE *file = fopen(argv[1], "wb");
    if (!file || fwrite(out, 1, sizeof out, file) != sizeof out) return 1;
    return fclose(file) != 0;
}
'''
        # The header's frame-format part, as being-console.c compiles it (the
        # rest of the header draws with SDL).
        text = PANEL_H.read_text()
        start = text.index("#define PANEL_MAGIC")
        at = text.index("static void panel_encode(")
        body = function_body(text, "static void panel_encode(")
        encoder = text[start:text.index(body, at) + len(body)]
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "encode.c"
            source.write_text(program.replace("@ENCODER@", encoder))
            binary = Path(work) / "encode"
            build = subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-Wno-unused-function",
                                    "-I", str(PANEL_H.parent), "-o", str(binary), str(source)],
                                   capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            header = Path(work) / "header"
            self.assertEqual(subprocess.run([str(binary), str(header)]).returncode, 0)
            raw = header.read_bytes()
        self.assertEqual(len(raw), 128)
        self.assertEqual(preview.parse_panel_header(raw), {
            "width": 1280, "height": 640, "stride": 5120, "sequence": 0x100000006, "dirty_first": 34,
            "dirty_count": 24, "flags": {"visible", "pip_typing", "scrolled"}, "cursor": (105, -1), "beat": 9,
            "columns": 107, "rows": 23, "newest_y": 82, "cell": (11, 24), "pid": 4242,
            "title": "Pip's terminal"})
        # Spot checks at the offsets crates/face/src/panel.rs reads.
        self.assertEqual(struct.unpack_from("<IIIQII", raw, 4), (1280, 640, 5120, 0x100000006, 34, 24))
        self.assertEqual(struct.unpack_from("<HHI", raw, 60), (11, 24, 4242))
        self.assertEqual(raw[68:96], bytes(28), "reserved bytes stay zero")


if __name__ == "__main__":
    unittest.main()
