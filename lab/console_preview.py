"""See the installer console the way the laptop shows it, on this Mac.

    python3 -m lab.console_preview [--size 1280x800] [--wait 1.5] [--feed '2\\r']
                                   [--out shot.png] [--window] [--no-panel] -- COMMAND [ARGS...]

Builds being-console against Homebrew SDL2 (sdl2-config) when its sources
change, runs COMMAND inside it exactly as the laptop does (in a pty, with
BEING_CONSOLE_UI=1), and saves what is on the screen after --wait seconds as a
PNG. By default nothing opens on the Mac's own screen: being-console draws into
its software surface at --size and writes that out, which is the same picture
the laptop's GL path uploads. --window opens the real GL window instead.
--feed types into the child after half a second (\\r is Enter). --text prints
what lab.tests.test_local_screen.console_lines reads back off the picture.

    python3 -m lab.console_preview font WOFF_DIRECTORY

Regenerates os/package/being/being-console-font.h from JetBrains Mono (SIL Open
Font License 1.1) .woff files: the latin 400 and 700 subsets that the
@fontsource/jetbrains-mono npm package ships. The box drawing, block and
symbol glyphs the font lacks are drawn here so they meet their neighbours
exactly. Python standard library only: the woff is unpacked with zlib and the
outlines are rasterised with an exact-area scanline filler.
"""

import argparse
import math
import os
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "os/package/being"
FONT_HEADER = PACKAGE / "being-console-font.h"
SOURCES = [PACKAGE / name for name in ("being-console.c", "being-console-gl.h", "being-console-font.h",
                                         "being-console-draw.h")]

# Cell faces: (name, cell width, cell height, pixel size, baseline, cursor band).
# Keep in step with being-console-draw.h and CONSOLE_FACES in test_local_screen.
FACES = (
    ("small", 8, 16, 13, 12, 2),
    ("medium", 11, 24, 18, 17, 2),
    ("large", 16, 34, 26, 25, 3),
)
EXTRA = "–—‘’“”•·…‹›×°↑↓"
DRAWN = "─│┌┐└┘├┤┬┴┼━┃╭╮╰╯═║█▓▒░▏▎▍▌▋▊▉●○✓✗→←⏎▸"


# -- reading the font ---------------------------------------------------------

def woff_tables(path):
    data = Path(path).read_bytes()
    if data[:4] != b"wOFF":
        raise SystemExit(f"{path} is not a woff file")
    count = struct.unpack(">H", data[12:14])[0]
    tables = {}
    for index in range(count):
        tag, offset, compressed, size, _ = struct.unpack(">4sIIII", data[44 + 20 * index:64 + 20 * index])
        body = data[offset:offset + compressed]
        tables[tag.decode()] = zlib.decompress(body) if compressed < size else body
    return tables


def character_map(tables):
    table = tables["cmap"]
    for index in range(struct.unpack(">H", table[2:4])[0]):
        _, _, offset = struct.unpack(">HHI", table[4 + 8 * index:12 + 8 * index])
        if struct.unpack(">H", table[offset:offset + 2])[0] != 4:
            continue
        segments = struct.unpack(">H", table[offset + 6:offset + 8])[0] // 2
        ends = struct.unpack(f">{segments}H", table[offset + 14:offset + 14 + 2 * segments])
        base = offset + 16 + 2 * segments
        starts = struct.unpack(f">{segments}H", table[base:base + 2 * segments])
        deltas = struct.unpack(f">{segments}h", table[base + 2 * segments:base + 4 * segments])
        ranges_at = base + 4 * segments
        ranges = struct.unpack(f">{segments}H", table[ranges_at:ranges_at + 2 * segments])
        mapping = {}
        for k in range(segments):
            for code in range(starts[k], ends[k] + 1):
                if code == 0xFFFF:
                    continue
                if ranges[k] == 0:
                    glyph = (code + deltas[k]) & 0xFFFF
                else:
                    at = ranges_at + 2 * k + ranges[k] + 2 * (code - starts[k])
                    glyph = struct.unpack(">H", table[at:at + 2])[0]
                    glyph = (glyph + deltas[k]) & 0xFFFF if glyph else 0
                if glyph:
                    mapping[code] = glyph
        return mapping
    raise SystemExit("the font has no format 4 character map")


class Font:
    def __init__(self, path):
        tables = woff_tables(path)
        self.units = struct.unpack(">H", tables["head"][18:20])[0]
        long_offsets = struct.unpack(">h", tables["head"][50:52])[0]
        glyphs = struct.unpack(">H", tables["maxp"][4:6])[0]
        loca = tables["loca"]
        if long_offsets:
            self.offsets = struct.unpack(f">{glyphs + 1}I", loca[:4 * (glyphs + 1)])
        else:
            self.offsets = [value * 2 for value in struct.unpack(f">{glyphs + 1}H", loca[:2 * (glyphs + 1)])]
        self.glyf = tables["glyf"]
        self.advance = struct.unpack(">H", tables["hmtx"][0:2])[0]
        self.x_height, self.cap_height = struct.unpack(">hh", tables["OS/2"][86:90])
        self.map = character_map(tables)

    def contours(self, glyph, transform=(1, 0, 0, 1, 0, 0)):
        """The glyph's closed outlines as lists of (x, y, on curve) in font units."""
        start, end = self.offsets[glyph], self.offsets[glyph + 1]
        if start == end:
            return []
        data = self.glyf[start:end]
        count = struct.unpack(">h", data[:2])[0]
        a, b, c, d, e, f = transform

        def place(x, y):
            return a * x + c * y + e, b * x + d * y + f

        if count >= 0:
            ends = struct.unpack(f">{count}H", data[10:10 + 2 * count])
            at = 10 + 2 * count
            at += 2 + struct.unpack(">H", data[at:at + 2])[0]
            total = ends[-1] + 1 if ends else 0
            flags = []
            while len(flags) < total:
                flag = data[at]
                at += 1
                flags.append(flag)
                if flag & 8:
                    flags.extend([flag] * data[at])
                    at += 1
            coordinates = []
            for short, same in ((2, 16), (4, 32)):
                value, values = 0, []
                for flag in flags:
                    if flag & short:
                        delta = data[at]
                        at += 1
                        value += delta if flag & same else -delta
                    elif not flag & same:
                        value += struct.unpack(">h", data[at:at + 2])[0]
                        at += 2
                    values.append(value)
                coordinates.append(values)
            outlines, first = [], 0
            for last in ends:
                outlines.append([(*place(coordinates[0][i], coordinates[1][i]), bool(flags[i] & 1))
                                 for i in range(first, last + 1)])
                first = last + 1
            return outlines
        outlines, at = [], 10
        while True:
            flag, component = struct.unpack(">HH", data[at:at + 4])
            at += 4
            if flag & 1:
                dx, dy = struct.unpack(">hh", data[at:at + 4])
                at += 4
            else:
                dx, dy = struct.unpack(">bb", data[at:at + 2])
                at += 2
            m = (1.0, 0.0, 0.0, 1.0)
            if flag & 8:
                s = struct.unpack(">h", data[at:at + 2])[0] / 16384
                at += 2
                m = (s, 0.0, 0.0, s)
            elif flag & 0x40:
                sx, sy = struct.unpack(">hh", data[at:at + 4])
                at += 4
                m = (sx / 16384, 0.0, 0.0, sy / 16384)
            elif flag & 0x80:
                m = tuple(value / 16384 for value in struct.unpack(">hhhh", data[at:at + 8]))
                at += 8
            inner = (a * m[0] + c * m[1], b * m[0] + d * m[1], a * m[2] + c * m[3], b * m[2] + d * m[3],
                     *place(dx, dy))
            outlines.extend(self.contours(component, inner))
            if not flag & 0x20:
                return outlines


def flatten(outline, steps=6):
    """A TrueType quadratic outline as a polygon."""
    points = []
    count = len(outline)
    expanded = []
    for index, (x, y, on) in enumerate(outline):
        nx, ny, next_on = outline[(index + 1) % count]
        expanded.append((x, y, on))
        if not on and not next_on:
            expanded.append(((x + nx) / 2, (y + ny) / 2, True))
    start = next(i for i, point in enumerate(expanded) if point[2])
    expanded = expanded[start:] + expanded[:start]
    expanded.append(expanded[0])
    previous = expanded[0]
    points.append(previous[:2])
    index = 1
    while index < len(expanded):
        point = expanded[index]
        if point[2]:
            points.append(point[:2])
            previous = point
            index += 1
        else:
            end = expanded[index + 1]
            for step in range(1, steps + 1):
                t = step / steps
                u = 1 - t
                points.append((u * u * previous[0] + 2 * u * t * point[0] + t * t * end[0],
                               u * u * previous[1] + 2 * u * t * point[1] + t * t * end[1]))
            previous = end
            index += 2
    return points


# -- rasterising -------------------------------------------------------------

def rasterize(polygons, width, height, rows=16):
    """Coverage 0..1 of each pixel by the polygons (nonzero winding), exact in x."""
    coverage = [0.0] * (width * height)
    lines = [[] for _ in range(height * rows)]
    for polygon in polygons:
        for (x0, y0), (x1, y1) in zip(polygon, polygon[1:] + polygon[:1]):
            if y0 == y1:
                continue
            direction = 1 if y1 > y0 else -1
            top, bottom = min(y0, y1), max(y0, y1)
            first = max(0, math.ceil(top * rows - 0.5))
            last = min(height * rows, math.ceil(bottom * rows - 0.5))
            slope = (x1 - x0) / (y1 - y0)
            for k in range(first, last):
                y = (k + 0.5) / rows
                lines[k].append((x0 + (y - y0) * slope, direction))
    weight = 1.0 / rows
    for k, crossings in enumerate(lines):
        if not crossings:
            continue
        crossings.sort()
        row = (k // rows) * width
        winding = 0
        for (x, direction), following in zip(crossings, crossings[1:] + [(None, 0)]):
            winding += direction
            if winding and following[0] is not None:
                left, right = max(x, 0.0), min(following[0], float(width))
                while left < right:
                    pixel = int(left)
                    span = min(right, pixel + 1.0) - left
                    coverage[row + pixel] += span * weight
                    left = pixel + 1.0
    return [min(value, 1.0) for value in coverage]


def union(*layers):
    return [max(values) for values in zip(*layers)]


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def circle(cx, cy, radius, steps=40, clockwise=True):
    points = [(cx + radius * math.cos(2 * math.pi * i / steps), cy + radius * math.sin(2 * math.pi * i / steps))
              for i in range(steps)]
    return points if clockwise else points[::-1]


def stroke(points, width):
    """A polyline with round joins and caps, as separately filled polygons."""
    shapes = []
    half = width / 2
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        nx, ny = -(y1 - y0) / length * half, (x1 - x0) / length * half
        shapes.append([(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)])
    shapes.extend(circle(x, y, half, 24) for x, y in points)
    return shapes


def drawn_glyph(char, face, bold):
    """Coverage for a glyph the font lacks, laid out on the whole cell."""
    _, width, height, size, baseline, _ = face
    light = 1 if size < 22 else 2
    heavy = light + 2
    stem = size * (0.105 if bold else 0.085)
    middle_x = width // 2 + (0.5 if light % 2 else 0.0)
    middle_y = height // 2 + (0.5 if light % 2 else 0.0)
    x_middle = baseline - 0.55 * size / 2

    def fill(*polygons):
        return union(*(rasterize([polygon], width, height) for polygon in polygons))

    def arms(left=0, right=0, up=0, down=0):
        shapes = []
        for weight, shape in ((left, lambda w: rect(0, middle_y - w / 2, middle_x + w / 2, middle_y + w / 2)),
                              (right, lambda w: rect(middle_x - w / 2, middle_y - w / 2, width, middle_y + w / 2)),
                              (up, lambda w: rect(middle_x - w / 2, 0, middle_x + w / 2, middle_y + w / 2)),
                              (down, lambda w: rect(middle_x - w / 2, middle_y - w / 2, middle_x + w / 2, height))):
            if weight:
                shapes.append(shape(weight))
        return fill(*shapes)

    boxes = {
        "─": dict(left=light, right=light), "│": dict(up=light, down=light),
        "━": dict(left=heavy, right=heavy), "┃": dict(up=heavy, down=heavy),
        "┌": dict(right=light, down=light), "┐": dict(left=light, down=light),
        "└": dict(right=light, up=light), "┘": dict(left=light, up=light),
        "├": dict(up=light, down=light, right=light), "┤": dict(up=light, down=light, left=light),
        "┬": dict(left=light, right=light, down=light), "┴": dict(left=light, right=light, up=light),
        "┼": dict(left=light, right=light, up=light, down=light),
    }
    if char in boxes:
        return arms(**boxes[char])
    gap = light + 1
    if char == "═":
        return fill(rect(0, middle_y - gap - light / 2, width, middle_y - gap + light / 2),
                    rect(0, middle_y + gap - light / 2, width, middle_y + gap + light / 2))
    if char == "║":
        return fill(rect(middle_x - gap - light / 2, 0, middle_x - gap + light / 2, height),
                    rect(middle_x + gap - light / 2, 0, middle_x + gap + light / 2, height))
    if char in "╭╮╰╯":
        sx = 1 if char in "╭╰" else -1  # which way the horizontal arm leaves
        sy = 1 if char in "╭╮" else -1  # which way the vertical arm leaves
        radius = min(middle_x, width - middle_x, middle_y) - 0.5
        cx, cy = middle_x + sx * radius, middle_y + sy * radius
        outer, inner = radius + light / 2, radius - light / 2
        # The quarter circle runs from the vertical arm's end to the horizontal one's.
        start, end = math.atan2(0, -sx), math.atan2(-sy, 0)
        sweep = (end - start + math.pi) % (2 * math.pi) - math.pi
        arc = [(math.cos(start + sweep * i / 16), math.sin(start + sweep * i / 16)) for i in range(17)]
        ring = [(cx + outer * u, cy + outer * v) for u, v in arc] + \
               [(cx + inner * u, cy + inner * v) for u, v in reversed(arc)]
        hx0, hx1 = sorted((cx, width if sx > 0 else 0))
        vy0, vy1 = sorted((cy, height if sy > 0 else 0))
        horizontal = rect(hx0, middle_y - light / 2, hx1, middle_y + light / 2)
        vertical = rect(middle_x - light / 2, vy0, middle_x + light / 2, vy1)
        return union(rasterize([ring], width, height), fill(horizontal, vertical))
    if char == "█":
        return [1.0] * (width * height)
    if char in "▓▒░":
        return [{"▓": 0.75, "▒": 0.5, "░": 0.25}[char]] * (width * height)
    if char in "▏▎▍▌▋▊▉":
        eighths = "▏▎▍▌▋▊▉".index(char) + 1
        return fill(rect(0, 0, width * eighths / 8, height))
    if char in "●○":
        radius = width * 0.36
        if char == "●":
            return fill(circle(width / 2, x_middle, radius))
        ring = max(1.0, stem)
        return rasterize([circle(width / 2, x_middle, radius),
                          circle(width / 2, x_middle, radius - ring, clockwise=False)], width, height)
    if char == "✓":
        return fill(*stroke([(width * 0.16, x_middle + 0.02 * size), (width * 0.42, x_middle + 0.2 * size),
                             (width * 0.86, x_middle - 0.3 * size)], stem * 1.15))
    if char == "✗":
        r = min(width * 0.36, 0.24 * size)
        return fill(*stroke([(width / 2 - r, x_middle - r), (width / 2 + r, x_middle + r)], stem * 1.1),
                    *stroke([(width / 2 + r, x_middle - r), (width / 2 - r, x_middle + r)], stem * 1.1))
    if char in "→←":
        sign = 1 if char == "→" else -1
        tip = width / 2 + sign * width * 0.4
        tail = width / 2 - sign * width * 0.4
        head = width * 0.32
        return fill(*stroke([(tail, x_middle), (tip, x_middle)], stem),
                    *stroke([(tip - sign * head, x_middle - head), (tip, x_middle), (tip - sign * head, x_middle + head)],
                            stem))
    if char == "⏎":
        right, left = width * 0.84, width * 0.16
        top, low = x_middle - 0.3 * size, x_middle + 0.12 * size
        head = width * 0.26
        return fill(*stroke([(right, top), (right, low), (left, low)], stem),
                    *stroke([(left + head, low - head), (left, low), (left + head, low + head)], stem))
    if char == "▸":
        r = width * 0.3
        return fill([(width / 2 - r * 0.8, x_middle - r), (width / 2 + r, x_middle), (width / 2 - r * 0.8, x_middle + r)])
    raise KeyError(char)


def font_glyph(font, char, face, bold):
    _, width, height, size, baseline, _ = face
    scale = size / font.units
    outlines = font.contours(font.map[ord(char)])
    left = (width - font.advance * scale) / 2
    best = None
    # Nudge each glyph by a fraction of a pixel so its stems land on whole
    # pixels: the sharpest of a few positions wins. A poor man's hinting.
    for shift in (-0.3, -0.15, 0.0, 0.15, 0.3) if outlines else (0.0,):
        polygons = [[(left + shift + x * scale, baseline - y * scale) for x, y in flatten(outline)]
                    for outline in outlines]
        coverage = rasterize(polygons, width, height)
        sharpness = sum(value * value for value in coverage) - 0.02 * abs(shift)
        if best is None or sharpness > best[0]:
            best = (sharpness, coverage)
    return best[1]


def characters():
    return [chr(code) for code in range(32, 127)] + list(EXTRA) + list(DRAWN)


def build_font(directory):
    directory = Path(directory)
    fonts = {
        weight: Font(next(directory.glob(f"*latin-{weight}-normal.woff")))
        for weight in ("400", "700")
    }
    license_path = directory.parent / "LICENSE"
    notice = license_path.read_text(encoding="utf-8") if license_path.exists() else ""
    chars = characters()
    lines = [
        "/* being-console's text face. Generated by `python3 -m lab.console_preview font`; do not edit.",
        " *",
        " * Letters, digits and punctuation are JetBrains Mono Regular and Bold",
        " * (https://github.com/JetBrains/JetBrainsMono), rasterised for three cell sizes,",
        " * so this table is a Modified Version of that Font Software under the SIL Open",
        " * Font License 1.1, reproduced in full below. The box drawing, block and symbol",
        " * glyphs it lacks are drawn by the generator and carry the same licence.",
        " *",
        " * Each glyph covers its whole cell, one hex digit of coverage (0-f) per pixel,",
        " * one string per pixel row. The header after each glyph's comment is",
        " * `<width>x<height> <weight> U+<code>`; lab/tests/test_local_screen.py reads",
        " * these tables to read text back off the screen.",
        " *",
    ]
    lines += [(" * " + line).rstrip() for line in notice.replace("*/", "* /").splitlines()]
    lines += [" */", "", "#define CONSOLE_FONT_GLYPHS %d" % len(chars),
              "#define CONSOLE_FONT_FACES %d" % len(FACES), ""]
    lines.append("static const unsigned int console_font_codepoints[CONSOLE_FONT_GLYPHS] = {")
    codes = [f"0x{ord(char):04x}" for char in chars]
    for start in range(0, len(codes), 12):
        lines.append("    " + ", ".join(codes[start:start + 12]) + ",")
    lines += ["};", ""]
    for face in FACES:
        name, width, height, size, baseline, cursor = face
        for weight in ("regular", "bold"):
            font = fonts["400" if weight == "regular" else "700"]
            lines.append(f"static const char *const console_font_{name}_{weight}[CONSOLE_FONT_GLYPHS] = {{")
            for char in chars:
                if ord(char) in font.map and char not in DRAWN:
                    coverage = font_glyph(font, char, face, weight == "bold")
                else:
                    coverage = drawn_glyph(char, face, weight == "bold")
                label = char if char.isprintable() and char not in "*/\\" else ""
                lines.append(f"    /* {width}x{height} {weight} U+{ord(char):04X} {label} */")
                for y in range(height):
                    row = "".join("%x" % round(min(max(coverage[y * width + x], 0.0), 1.0) ** 0.9 * 15)
                                  for x in range(width))
                    lines.append(f'    "{row}"' + ("," if y == height - 1 else ""))
            lines += ["};", ""]
            print(f"{name} {weight}: {len(chars)} glyphs", file=sys.stderr)
    lines.append("struct console_face {")
    lines.append("    int width, height, size, baseline, cursor;")
    lines.append("    const char *const *glyphs[2];")
    lines.append("};")
    lines.append("")
    lines.append("static const struct console_face console_faces[CONSOLE_FONT_FACES] = {")
    for name, width, height, size, baseline, cursor in FACES:
        lines.append(f"    {{ {width}, {height}, {size}, {baseline}, {cursor}, "
                     f"{{ console_font_{name}_regular, console_font_{name}_bold }} }},")
    lines += ["};", ""]
    FONT_HEADER.write_text("\n".join(lines), encoding="utf-8")
    print(FONT_HEADER)


# -- running the console -----------------------------------------------------

def build(build_dir):
    """The Mac (or host) build of being-console, rebuilt when its sources change."""
    import fcntl
    binary = Path(build_dir) / "being-console"
    binary.parent.mkdir(parents=True, exist_ok=True)
    with open(binary.parent / ".build-lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # previews may run side by side
        if binary.exists() and all(binary.stat().st_mtime >= source.stat().st_mtime
                                   for source in SOURCES if source.exists()):
            return binary
        flags = subprocess.run(["sdl2-config", "--cflags", "--libs"], capture_output=True, text=True,
                               check=True).stdout.split()
        partial = binary.with_suffix(".partial")
        command = ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-o", str(partial),
                   str(PACKAGE / "being-console.c"), *flags]
        if sys.platform != "darwin":
            command.append("-lutil")
        subprocess.run(command, check=True)
        partial.replace(binary)
    return binary


def read_ppm(data):
    fields, at = [], 0
    while len(fields) < 4:
        while data[at:at + 1].isspace():
            at += 1
        end = at
        while not data[end:end + 1].isspace():
            end += 1
        fields.append(data[at:end])
        at = end
    width, height = int(fields[1]), int(fields[2])
    return width, height, data[at + 1:at + 1 + width * height * 3]


def capture(command, size=(1280, 800), wait=1.5, feed="", window=False, build_dir=None, panel=True):
    """Run COMMAND in being-console and return the frame (width, height, rgb).

    With panel (the default), being-console gets a screen directory that says
    "show", as on the laptop, so the footer offers F10 and F9 too."""
    binary = build(build_dir or ROOT / "out/console-preview")
    with tempfile.TemporaryDirectory(prefix="being-console-preview-") as scratch:
        shot = Path(scratch) / "screen.ppm"
        arguments = [str(binary), "--snapshot", str(shot), "--exit-after", str(wait)]
        if not window:
            arguments += ["--headless", f"{size[0]}x{size[1]}"]
        if feed:
            arguments += ["--feed", feed]
        environment = dict(os.environ)
        environment.pop("BEING_SCREEN_DIR", None)
        if panel:
            gate = Path(scratch) / "screen"
            gate.mkdir()
            (gate / "show").write_text("1\n")
            environment["BEING_SCREEN_DIR"] = str(gate)
        subprocess.run([*arguments, "--", *command], env=environment, timeout=wait + 30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if not shot.exists():
            raise SystemExit("being-console did not write a snapshot")
        return read_ppm(shot.read_bytes())


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["font"]:
        if len(argv) != 2:
            raise SystemExit("usage: python3 -m lab.console_preview font WOFF_DIRECTORY")
        build_font(argv[1])
        return 0
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--size", default="1280x800", help="drawable size, WIDTHxHEIGHT")
    parser.add_argument("--wait", type=float, default=1.5, help="seconds before the snapshot")
    parser.add_argument("--feed", default="", help="keys to type after half a second (\\r is Enter)")
    parser.add_argument("--out", type=Path, default=Path("console-preview.png"))
    parser.add_argument("--window", action="store_true", help="open the real GL window")
    parser.add_argument("--text", action="store_true", help="print the text read back off the frame")
    parser.add_argument("--no-panel", action="store_true",
                        help="no screen directory, so the footer leaves out F10 and F9")
    parser.add_argument("--build-dir", type=Path, default=None)
    parser.add_argument("command", nargs="+")
    options = parser.parse_args(argv)
    width, height = (int(value) for value in options.size.lower().split("x"))
    feed = options.feed.encode().decode("unicode_escape")
    frame = capture(options.command, (width, height), options.wait, feed, options.window, options.build_dir,
                    not options.no_panel)
    from lab.vm import png
    options.out.parent.mkdir(parents=True, exist_ok=True)
    options.out.write_bytes(png(*frame))
    print(options.out)
    if options.text:
        from lab.tests.test_local_screen import console_lines
        print("\n".join(console_lines(frame)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
