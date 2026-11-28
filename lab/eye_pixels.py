"""Locate rendered eyes in a screenshot with a known renderer palette."""

from typing import NamedTuple


class EyePixels(NamedTuple):
    bounds: tuple[int, int, int, int]
    peak: tuple[int, int, int]


def _colour_matches(pixel, level, palette):
    # Comparing channel shares lets a fill or core match under dimming.
    # Neutral UI text and desaturated transitions cannot seed an eye.
    if max(pixel) - min(pixel) < 24:
        return False
    return any(
        all(abs(channel * total - target * level) * 12 <= total * level
            for channel, target in zip(pixel, swatch))
        for swatch, total in palette
    )


def _component(pixels, origin, step, core_level):
    pending = [origin]
    x0, y0 = x1, y1 = origin
    peak = (0, 0, 0)
    cores = 0
    area = 0
    while pending:
        x, y = pending.pop()
        pixel = pixels.pop((x, y), None)
        if pixel is None:
            continue
        x0, y0 = min(x, x0), min(y, y0)
        x1, y1 = max(x, x1), max(y, y1)
        peak = tuple(max(old, channel) for old, channel in zip(peak, pixel))
        cores += max(pixel) >= core_level
        area += 1
        pending.extend(((x - step, y), (x + step, y), (x, y - step), (x, y + step)))
    return (cores, area), EyePixels((x0, y0, x1, y1), peak)


def palette_eyes(frame, swatches, *, region=None, step=4):
    """Return (left, right) EyePixels or None for the requested eye rectangle.

    The palette contains the on-screen RGB fill, core and halo. The optional
    rectangle (x0, y0, x1, y1) uses exclusive bottom and right edges. Supply the
    renderer's eye-strip or face rectangle when panels or decorations are
    visible. Report sampled bounds and per-channel peaks of the two eye
    components. The separate cyan boot-screen check in lab.vm still checks
    the entire image, including stray boot text.
    """
    width, height, rgb = frame
    if len(rgb) != width * height * 3 or step < 1:
        raise ValueError("expected packed RGB and a positive sampling step")
    x0, y0, x1, y1 = region if region is not None else (0, 0, width, height)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("eye rectangle outside the frame")
    swatches = tuple(swatches)
    if not swatches or any(len(swatch) != 3 or not sum(swatch) for swatch in swatches):
        raise ValueError("expected nonblack RGB palette swatches")
    palette = tuple((swatch, sum(swatch)) for swatch in swatches)
    core_level = max(max(swatch) for swatch in swatches) // 2
    center = (x0 + x1) / 2
    pixels = {}
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            offset = (y * width + x) * 3
            pixel = rgb[offset:offset + 3]
            level = sum(pixel)
            if level > 120 and _colour_matches(pixel, level, palette):
                pixels[x, y] = pixel

    found = [None, None]
    scores = [(0, 0), (0, 0)]
    while pixels:
        score, eye = _component(pixels, next(iter(pixels)), step, core_level)
        left, top, right, bottom = eye.bounds
        if score[0] < 4 or right - left < 2 * step or top == bottom:
            continue
        if left < center <= right:
            continue
        side = int(left >= center)
        if score > scores[side]:
            found[side], scores[side] = eye, score
    return tuple(found)
