#!/usr/bin/env python3
"""Build Shell Lab from the real face sources.

    python3 build.py          write index.html and SOURCES.sha256
    python3 build.py --check  exit 1 if the face sources changed since the last build

The page draws Pip's eyes with crates/face/src/eye.vert and eye.frag, copied
verbatim, with the GLES header render.rs prepends. The expression geometry
comes from the same catalogue the Rust face compiles. Look, grid,
mark level, the eye-strip fraction and the dock transition are read from
render.rs and panel.rs.

Nothing outside this folder is written. Scratch files go to a temporary folder.
"""
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FACE = os.path.normpath(os.path.join(HERE, '..', '..', 'crates', 'face', 'src'))
CATALOGUE = '../expressions.json'
CONCEPTS = '../concept-expressions.json'
TRACKED = ['eye.vert', 'eye.frag', 'eye.rs', 'anim.rs', 'pace.rs', 'render.rs', 'panel.rs',
           'colour.rs', CATALOGUE, CONCEPTS, '../build.rs']
MANIFEST = os.path.join(HERE, 'SOURCES.sha256')
PAGE = os.path.join(HERE, 'index.html')


def read(name):
    with open(os.path.join(FACE, name), encoding='utf-8') as f:
        return f.read()


def sha(name):
    with open(os.path.join(FACE, name), 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def lab_safe(c):
    r, g, b = (round(v * 255) for v in c)
    return b > r + 30 and g > r


def palettes(default):
    # Preserve Shell Lab's approved lab-safe picker while reading its
    # swatches from the same native palette table.
    rows = re.findall(r'Palette::new\("([a-z]+)"\s*,\s*(\[[^\]]+\])\s*,\s*(\[[^\]]+\])\s*,\s*(\[[^\]]+\])\s*\)',
                      read('colour.rs'))
    names = ('fill', 'core', 'halo')
    colours = {name: dict(zip(names, map(json.loads, triples), strict=True)) for name, *triples in rows}
    if colours.get('cyan') != {part: default[part] for part in names}:
        sys.exit('build: the Shell palette and Look::default() disagree on cyan')
    safe = {name: palette for name, palette in colours.items()
            if all(lab_safe(triple) for triple in palette.values()) and min(palette['core']) < 0.95}
    return safe, [name for name in colours if name not in safe]


def floats(text, field):
    m = re.search(field + r':\s*\[([^\]]+)\]', text)
    if not m:
        sys.exit(f'build: {field} not found in render.rs Look::default()')
    return [float(v) for v in m.group(1).split(',')]


def scalar(text, pattern, what):
    m = re.search(pattern, text)
    if not m:
        sys.exit(f'build: {what} not found')
    return m.group(1)


def presets():
    catalogue = json.loads(read(CATALOGUE))
    poses = catalogue['poses']
    defaults = {row['name']: row['left'] + row['right'] for row in poses}
    for row in json.loads(read(CONCEPTS))['poses']:
        if row['name'] in defaults:
            continue
        # The native catalogue wins for tuned poses. Match build.rs for the
        # remaining auditions, which use the concept's eyes with round >= 2.
        eyes = [row['left'].copy(), row['right'].copy()]
        for eye in eyes:
            eye[4] = max(eye[4], 2.0)
        defaults[row['name']] = eyes[0] + eyes[1]
    return {
        'presets': defaults,
        'names': [row['name'] for row in poses if not row.get('private') and not row.get('audition')],
    }


def manifest_now():
    return {n: sha(n) for n in TRACKED}


def face_bundle():
    render_all = read('render.rs')
    i = render_all.find('fn default() -> Look')
    if i < 0:
        sys.exit('build: Look::default() not found in render.rs')
    render = render_all[i:i + 600]
    panel = read('panel.rs')
    look = {
        'fill': floats(render, 'fill'),
        'core': floats(render, 'core'),
        'halo': floats(render, 'halo'),
        'grid': float(scalar(render, r'grid:\s*([0-9.]+),', 'Look grid')),
        'glow_r': float(scalar(render, r'glow_r:\s*([0-9.]+),', 'Look glow_r')),
    }
    header = scalar(render_all, r'"(#version 300 es[^"]*)"', 'GLES header').encode().decode('unicode_escape')
    mark_level = float(scalar(render_all, r'MARK_LEVEL:\s*f32\s*=\s*([0-9.]+)', 'MARK_LEVEL'))
    strip = scalar(panel, r'STRIP_FRACTION:\s*f32\s*=\s*([0-9.]+\s*/\s*[0-9.]+)', 'STRIP_FRACTION')
    num, den = (float(v) for v in strip.split('/'))
    transition = float(scalar(panel, r'TRANSITION:\s*f32\s*=\s*([0-9.]+)', 'TRANSITION'))

    palette, not_yet = palettes(look)
    data = presets()
    hashes = manifest_now()
    return {
        'header': header,
        'vert': read('eye.vert'),
        'frag': read('eye.frag'),
        'look': dict(look, markLevel=mark_level),
        'palette': palette,
        'notYet': not_yet,
        **data,
        'stripFraction': num / den,
        'transition': transition,
        'sources': hashes,
    }


def render_page(bundle):
    js = 'const FACE=' + json.dumps(bundle, separators=(',', ':')) + ';\n'
    for name in ('face-gl.js', 'face-anim.js'):
        with open(os.path.join(HERE, 'src', name), encoding='utf-8') as f:
            js += f.read() + '\n'
    with open(os.path.join(HERE, 'src', 'shell.src.html'), encoding='utf-8') as f:
        page = f.read()
    marker = '/*@@FACE_BUNDLE@@*/'
    if page.count(marker) != 1:
        sys.exit('build: the face bundle marker must appear exactly once in src/shell.src.html')
    return page.replace(marker, js.replace('</script', '<\\/script'))


def render_manifest(hashes):
    return ('# crates/face sources Shell Lab was built from (python3 build.py --check)\n'
            + ''.join(f'{hashes[name]}  {name}\n' for name in TRACKED))


def check():
    bundle = face_bundle()
    for path, expected in ((MANIFEST, render_manifest(bundle['sources'])), (PAGE, render_page(bundle))):
        if not os.path.exists(path):
            sys.exit(f'Shell Lab is missing {os.path.basename(path)}. Run python3 build.py.')
        with open(path, encoding='utf-8') as f:
            if f.read() != expected:
                sys.exit(f'Shell Lab has stale {os.path.basename(path)}. Run python3 build.py.')
    print('Shell Lab matches its preview source and crates/face.')


def build():
    bundle = face_bundle()
    page = render_page(bundle)
    with open(PAGE, 'w', encoding='utf-8') as f:
        f.write(page)
    with open(MANIFEST, 'w', encoding='utf-8') as f:
        f.write(render_manifest(bundle['sources']))
    print(f'wrote index.html ({len(page) // 1024} KB) from {len(bundle["presets"])} presets, palette {", ".join(bundle["palette"])}')


if __name__ == '__main__':
    check() if '--check' in sys.argv else build()
