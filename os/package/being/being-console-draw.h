/* The console's look: palette, backdrop, header with the being's eyes, cell
 * text and the footer. Everything is drawn in software into an RGBA32 surface
 * (bytes R, G, B, A), which the GL path uploads as one texture.
 *
 * The geometry below is mirrored by console_layout() in
 * lab/tests/test_local_screen.py, which reads the text back off the screen:
 * change both together. Maths comes from SDL so the build needs no -lm. */
#include <stdint.h>

#include "being-console-font.h"

enum { FACE_SMALL, FACE_MEDIUM, FACE_LARGE };
enum { WEIGHT_REGULAR, WEIGHT_BOLD };

struct rgb { uint8_t r, g, b; };

/* Per face: header band, footer bar, side margin, space above and below the text. */
struct chrome { int header, footer, pad_x, pad_top, pad_bottom; };
static const struct chrome console_chrome[CONSOLE_FONT_FACES] = {
    { 48, 32, 20, 12, 8 },
    { 76, 48, 64, 20, 12 },
    { 110, 68, 96, 28, 16 },
};

/* SGR colours. Index 0 is the default; 1-8 are 30-37 (or 40-47), 9-16 are 90-97 (or 100-107). */
static const struct rgb palette_fg[17] = {
    { 214, 222, 233 },
    { 44, 52, 68 }, { 236, 104, 104 }, { 92, 212, 150 }, { 236, 184, 92 },
    { 98, 152, 240 }, { 184, 134, 240 }, { 40, 196, 240 }, { 214, 222, 233 },
    { 124, 138, 158 }, { 255, 140, 130 }, { 134, 236, 178 }, { 255, 210, 132 },
    { 142, 186, 255 }, { 214, 172, 255 }, { 118, 230, 255 }, { 246, 249, 252 },
};
static const struct rgb palette_bg[17] = {
    { 0, 0, 0 },
    { 7, 10, 17 }, { 58, 20, 26 }, { 14, 46, 36 }, { 54, 40, 14 },
    { 16, 30, 60 }, { 40, 26, 58 }, { 9, 44, 58 }, { 30, 39, 54 },
    { 50, 60, 78 }, { 236, 104, 104 }, { 92, 212, 150 }, { 236, 184, 92 },
    { 98, 152, 240 }, { 184, 134, 240 }, { 40, 196, 240 }, { 232, 238, 246 },
};
static const struct rgb canvas_ink = { 8, 12, 20 };      /* text drawn on a reversed default background */
static const struct rgb canvas_mean = { 10, 15, 25 };    /* what dim text fades toward */
static const struct rgb footer_fill = { 12, 17, 27 };    /* flat: the lab finds the console by it */
static const struct rgb footer_rule = { 27, 37, 53 };
static const struct rgb keycap_fill = { 23, 32, 47 };
static const struct rgb keycap_edge = { 48, 63, 86 };
static const struct rgb hint_text = { 128, 142, 162 };
static const struct rgb accent = { 40, 196, 240 };
static const struct rgb wordmark_text = { 238, 243, 249 };
/* The eyes, as the face draws them (crates/face/src/render.rs: Look). */
static const struct rgb eye_fill = { 20, 199, 252 };
static const struct rgb eye_core = { 102, 235, 255 };
static const struct rgb eye_halo = { 8, 128, 255 };
/* Header eye size and spacing, as fractions of the header band. */
#define EYE_RX 0.150f
#define EYE_RY 0.112f
#define EYE_SPACING 2.85f /* centre to centre, in eye radii */

struct canvas {
    uint8_t *pixels;
    int pitch, width, height;
};

static uint8_t *glyph_alpha[CONSOLE_FONT_FACES][2];

static bool load_glyphs(void)
{
    for (int face = 0; face < CONSOLE_FONT_FACES; ++face) {
        const struct console_face *info = &console_faces[face];
        size_t area = (size_t)info->width * (size_t)info->height;
        for (int weight = 0; weight < 2; ++weight) {
            uint8_t *alpha = malloc(area * CONSOLE_FONT_GLYPHS);
            if (!alpha)
                return false;
            for (int glyph = 0; glyph < CONSOLE_FONT_GLYPHS; ++glyph) {
                const char *source = info->glyphs[weight][glyph];
                for (size_t i = 0; i < area; ++i) {
                    char digit = source[i];
                    int value = digit >= 'a' ? digit - 'a' + 10 : digit - '0';
                    alpha[(size_t)glyph * area + i] = (uint8_t)(value * 17);
                }
            }
            glyph_alpha[face][weight] = alpha;
        }
    }
    return true;
}

static int glyph_index(uint32_t codepoint)
{
    if (codepoint >= 0x20 && codepoint < 0x7f)
        return (int)codepoint - 0x20;
    switch (codepoint) {
    case 0xa0: return 0;
    case 0x2714: codepoint = 0x2713; break;          /* heavy check */
    case 0x2715: case 0x2716: case 0x2718: codepoint = 0x2717; break;
    case 0x25b6: case 0x25ba: codepoint = 0x25b8; break;
    case 0x2219: case 0x22c5: codepoint = 0x00b7; break;
    default: break;
    }
    for (int index = 0x7f - 0x20; index < CONSOLE_FONT_GLYPHS; ++index)
        if (console_font_codepoints[index] == codepoint)
            return index;
    return '?' - 0x20;
}

static inline uint8_t *pixel_at(const struct canvas *canvas, int x, int y)
{
    return canvas->pixels + (size_t)y * (size_t)canvas->pitch + (size_t)x * 4;
}

static inline void blend(uint8_t *pixel, struct rgb color, int alpha)
{
    pixel[0] = (uint8_t)(pixel[0] + ((int)color.r - pixel[0]) * alpha / 255);
    pixel[1] = (uint8_t)(pixel[1] + ((int)color.g - pixel[1]) * alpha / 255);
    pixel[2] = (uint8_t)(pixel[2] + ((int)color.b - pixel[2]) * alpha / 255);
}

static struct rgb mix(struct rgb a, struct rgb b, float t)
{
    return (struct rgb){ (uint8_t)(a.r + (b.r - a.r) * t + 0.5f), (uint8_t)(a.g + (b.g - a.g) * t + 0.5f),
                         (uint8_t)(a.b + (b.b - a.b) * t + 0.5f) };
}

static void fill_rect(const struct canvas *canvas, int x, int y, int width, int height, struct rgb color)
{
    int x0 = SDL_max(x, 0), y0 = SDL_max(y, 0);
    int x1 = SDL_min(x + width, canvas->width), y1 = SDL_min(y + height, canvas->height);
    for (int row = y0; row < y1; ++row) {
        uint8_t *pixel = pixel_at(canvas, x0, row);
        for (int column = x0; column < x1; ++column, pixel += 4) {
            pixel[0] = color.r;
            pixel[1] = color.g;
            pixel[2] = color.b;
            pixel[3] = 255;
        }
    }
}

/* How much of pixel (px, py) a rounded rectangle covers. Radii run top-left,
 * top-right, bottom-right, bottom-left. */
static float rounded_cover(float px, float py, float x, float y, float width, float height, const float radius[4])
{
    float cx = px + 0.5f, cy = py + 0.5f;
    if (cx < x - 0.5f || cy < y - 0.5f || cx > x + width + 0.5f || cy > y + height + 0.5f)
        return 0.0f;
    float edge = SDL_min(SDL_min(cx - x, x + width - cx), SDL_min(cy - y, y + height - cy));
    float cover = SDL_clamp(edge + 0.5f, 0.0f, 1.0f);
    bool left = cx < x + width / 2, top = cy < y + height / 2;
    float r = radius[top ? (left ? 0 : 1) : (left ? 3 : 2)];
    if (r > 0.0f) {
        float ox = left ? x + r : x + width - r, oy = top ? y + r : y + height - r;
        if ((left ? cx < ox : cx > ox) && (top ? cy < oy : cy > oy)) {
            float distance = SDL_sqrtf((cx - ox) * (cx - ox) + (cy - oy) * (cy - oy));
            cover = SDL_min(cover, SDL_clamp(r - distance + 0.5f, 0.0f, 1.0f));
        }
    }
    return cover;
}

static void fill_rounded(const struct canvas *canvas, int x, int y, int width, int height, const float radius[4],
                         struct rgb color)
{
    float largest = SDL_max(SDL_max(radius[0], radius[1]), SDL_max(radius[2], radius[3]));
    if (largest <= 0.0f) {
        fill_rect(canvas, x, y, width, height, color);
        return;
    }
    int x0 = SDL_max(x, 0), y0 = SDL_max(y, 0);
    int x1 = SDL_min(x + width, canvas->width), y1 = SDL_min(y + height, canvas->height);
    int corner = (int)SDL_ceilf(largest);
    for (int row = y0; row < y1; ++row) {
        bool band = row < y + corner || row >= y + height - corner;
        uint8_t *pixel = pixel_at(canvas, x0, row);
        for (int column = x0; column < x1; ++column, pixel += 4) {
            if (!band || (column >= x + corner && column < x + width - corner)) {
                pixel[0] = color.r;
                pixel[1] = color.g;
                pixel[2] = color.b;
                continue;
            }
            float cover = rounded_cover((float)column, (float)row, (float)x, (float)y, (float)width, (float)height,
                                        radius);
            blend(pixel, color, (int)(cover * 255.0f + 0.5f));
        }
    }
}

static void outline_rounded(const struct canvas *canvas, int x, int y, int width, int height, float radius,
                            struct rgb fill, struct rgb edge)
{
    const float outer[4] = { radius, radius, radius, radius };
    const float inner[4] = { radius - 1, radius - 1, radius - 1, radius - 1 };
    for (int row = SDL_max(y, 0); row < SDL_min(y + height, canvas->height); ++row) {
        for (int column = SDL_max(x, 0); column < SDL_min(x + width, canvas->width); ++column) {
            float a = rounded_cover((float)column, (float)row, (float)x, (float)y, (float)width, (float)height, outer);
            float b = rounded_cover((float)column, (float)row, (float)x + 1, (float)y + 1, (float)width - 2,
                                    (float)height - 2, inner);
            uint8_t *pixel = pixel_at(canvas, column, row);
            blend(pixel, edge, (int)((a - b) * 255.0f + 0.5f));
            blend(pixel, fill, (int)(b * 255.0f + 0.5f));
        }
    }
}

static void draw_glyph(const struct canvas *canvas, int face, int weight, int glyph, int x, int y, struct rgb color)
{
    const struct console_face *info = &console_faces[face];
    const uint8_t *alpha = glyph_alpha[face][weight] + (size_t)glyph * (size_t)(info->width * info->height);
    for (int row = 0; row < info->height; ++row) {
        int py = y + row;
        if (py < 0 || py >= canvas->height)
            continue;
        for (int column = 0; column < info->width; ++column) {
            int px = x + column, a = alpha[row * info->width + column];
            if (a && px >= 0 && px < canvas->width)
                blend(pixel_at(canvas, px, py), color, a);
        }
    }
}

/* A glyph magnified by `zoom` with bilinear filtering, for the wordmark. */
static void draw_glyph_scaled(const struct canvas *canvas, int face, int weight, int glyph, int x, int y, float zoom,
                              struct rgb color)
{
    if (zoom == 1.0f) {
        draw_glyph(canvas, face, weight, glyph, x, y, color);
        return;
    }
    const struct console_face *info = &console_faces[face];
    const uint8_t *alpha = glyph_alpha[face][weight] + (size_t)glyph * (size_t)(info->width * info->height);
    int width = (int)(info->width * zoom + 0.5f), height = (int)(info->height * zoom + 0.5f);
    for (int row = 0; row < height; ++row) {
        float sy = SDL_clamp((row + 0.5f) / zoom - 0.5f, 0.0f, info->height - 1.0f);
        int y0 = (int)sy, y1 = SDL_min(y0 + 1, info->height - 1);
        float fy = sy - y0;
        for (int column = 0; column < width; ++column) {
            int px = x + column, py = y + row;
            if (px < 0 || py < 0 || px >= canvas->width || py >= canvas->height)
                continue;
            float sx = SDL_clamp((column + 0.5f) / zoom - 0.5f, 0.0f, info->width - 1.0f);
            int x0 = (int)sx, x1 = SDL_min(x0 + 1, info->width - 1);
            float fx = sx - x0;
            float top = alpha[y0 * info->width + x0] * (1 - fx) + alpha[y0 * info->width + x1] * fx;
            float bottom = alpha[y1 * info->width + x0] * (1 - fx) + alpha[y1 * info->width + x1] * fx;
            int a = (int)(top * (1 - fy) + bottom * fy + 0.5f);
            if (a)
                blend(pixel_at(canvas, px, py), color, a);
        }
    }
}

/* The next code point of a UTF-8 string. */
static uint32_t next_codepoint(const char **text)
{
    const unsigned char *s = (const unsigned char *)*text;
    uint32_t code = s[0];
    int extra = code >= 0xf0 ? 3 : code >= 0xe0 ? 2 : code >= 0xc0 ? 1 : 0;
    code &= extra ? 0x3fu >> extra : 0x7fu;
    int used = 1;
    for (; extra > 0 && (s[used] & 0xc0) == 0x80; --extra, ++used)
        code = (code << 6) | (s[used] & 0x3f);
    *text += used;
    return code;
}

static int draw_text(const struct canvas *canvas, int face, int weight, int x, int y, const char *text,
                     struct rgb color)
{
    while (*text) {
        draw_glyph(canvas, face, weight, glyph_index(next_codepoint(&text)), x, y, color);
        x += console_faces[face].width;
    }
    return x;
}

static int text_width(int face, const char *text)
{
    int width = 0;
    while (*text) {
        next_codepoint(&text);
        width += console_faces[face].width;
    }
    return width;
}

/* Ink columns of a glyph, for the proportionally set wordmark. */
static void glyph_ink(int face, int weight, int glyph, int *first, int *last)
{
    const struct console_face *info = &console_faces[face];
    const uint8_t *alpha = glyph_alpha[face][weight] + (size_t)glyph * (size_t)(info->width * info->height);
    *first = info->width;
    *last = -1;
    for (int row = 0; row < info->height; ++row)
        for (int column = 0; column < info->width; ++column)
            if (alpha[row * info->width + column] > 40) {
                *first = SDL_min(*first, column);
                *last = SDL_max(*last, column);
            }
}

/* The backdrop: a deep blue-black that is a little lighter at the top, a
 * faint cyan bloom under the header, a hairline under the header, the
 * footer bar, and the wordmark. Ordered dither keeps the gradient free of
 * bands on an 8-bit panel. */
static void paint_backdrop(const struct canvas *canvas, int face, int header, int footer, int pad_x)
{
    static const uint8_t bayer[4][4] = { { 0, 8, 2, 10 }, { 12, 4, 14, 6 }, { 3, 11, 1, 9 }, { 15, 7, 13, 5 } };
    int width = canvas->width, height = canvas->height;
    float glow_x = width * 0.30f, glow_y = (float)header, spread_x = width * 0.55f, spread_y = height * 0.42f;
    for (int y = 0; y < height - footer; ++y) {
        float t = (float)y / (float)SDL_max(height - 1, 1);
        float base_r = 11.0f - 5.0f * t, base_g = 16.0f - 7.0f * t, base_b = 28.0f - 12.0f * t;
        float dy = (y - glow_y) / spread_y;
        uint8_t *pixel = pixel_at(canvas, 0, y);
        for (int x = 0; x < width; ++x, pixel += 4) {
            float dx = (x - glow_x) / spread_x;
            float glow = SDL_expf(-(dx * dx + dy * dy) * 1.6f);
            float dither = (bayer[y & 3][x & 3] - 7.5f) / 16.0f;
            pixel[0] = (uint8_t)SDL_clamp(base_r + 1.0f * glow + dither + 0.5f, 0.0f, 255.0f);
            pixel[1] = (uint8_t)SDL_clamp(base_g + 7.0f * glow + dither + 0.5f, 0.0f, 255.0f);
            pixel[2] = (uint8_t)SDL_clamp(base_b + 11.0f * glow + dither + 0.5f, 0.0f, 255.0f);
            pixel[3] = 255;
        }
    }
    /* A hairline under the header that fades out toward both sides. */
    if (header > 0 && header < height) {
        uint8_t *pixel = pixel_at(canvas, 0, header - 1);
        for (int x = 0; x < width; ++x, pixel += 4) {
            float edge = SDL_min((float)x, (float)(width - 1 - x)) / (width * 0.35f);
            float strength = 0.75f * SDL_min(edge, 1.0f);
            blend(pixel, (struct rgb){ 44, 74, 98 }, (int)(strength * 255.0f));
        }
    }
    fill_rect(canvas, 0, height - footer, width, footer, footer_fill);
    fill_rect(canvas, 0, height - footer, width, 1, footer_rule);

    /* "PipOS", set proportionally from the bold face's ink, "OS" in the eyes' cyan. */
    int mark = face == FACE_SMALL ? FACE_MEDIUM : FACE_LARGE;
    float zoom = face == FACE_LARGE ? 1.45f : 1.0f;
    const struct console_face *info = &console_faces[mark];
    int eyes_width = (int)(header * EYE_RX * (2.0f + EYE_SPACING));
    float x = pad_x + eyes_width + header * 0.30f;
    float cap = 0.73f * info->size * zoom;
    float top = header / 2.0f + cap / 2.0f - info->baseline * zoom - 1.0f;
    float tracking = SDL_max(1.0f, info->size * zoom / 10.0f);
    /* "Pip" in the text colour, "OS" in the eyes' cyan. */
    const char *word = "PipOS";
    const int accent_from = 3;
    for (int i = 0; word[i]; ++i) {
        int glyph = glyph_index((unsigned char)word[i]), first, last;
        glyph_ink(mark, WEIGHT_BOLD, glyph, &first, &last);
        struct rgb color = i >= accent_from ? accent : wordmark_text;
        draw_glyph_scaled(canvas, mark, i >= accent_from ? WEIGHT_REGULAR : WEIGHT_BOLD, glyph, (int)(x - first * zoom),
                          (int)top, zoom, color);
        x += (last - first + 1) * zoom + tracking;
    }
}

/* One of the being's eyes, as the face draws its resting look: an ellipse
 * whose lower lid is cut on a slant that rises toward the outer side, so each
 * eye's lowest point sits near the nose, with a soft halo. `open` closes it
 * toward its lower lid; `side` is -1 for the left eye. */
static void draw_eye(const struct canvas *canvas, int clip_bottom, float cx, float cy, float rx, float ry, float open,
                     float side)
{
    float shut = SDL_max(open, 0.17f);
    float ery = ry * shut;
    float ecy = cy + ry * (1.0f - shut) * 0.45f;
    float reach = rx * 1.1f;
    int x0 = SDL_max((int)(cx - rx - reach), 0), x1 = SDL_min((int)(cx + rx + reach) + 1, canvas->width);
    int y0 = SDL_max((int)(ecy - ery - reach), 0), y1 = SDL_min((int)(ecy + ery + reach) + 1, clip_bottom);
    for (int y = y0; y < y1; ++y) {
        for (int x = x0; x < x1; ++x) {
            float px = x + 0.5f - cx, py = y + 0.5f - ecy;
            float ux = px / rx, uy = py / ery;
            float k0 = SDL_sqrtf(ux * ux + uy * uy);
            float k1 = SDL_sqrtf(ux * ux / (rx * rx) + uy * uy / (ery * ery));
            float d = k1 > 1e-6f ? k0 * (k0 - 1.0f) / k1 : -SDL_min(rx, ery);
            /* The lower lid rises toward the outer side. */
            float lid = ery * (0.84f - 0.26f * side * px / rx);
            float d_lid = (py - lid) * 0.92f;
            /* A rounded crease where the lid meets the ellipse. */
            float k = rx * 0.35f, h = SDL_max(k - SDL_fabsf(d - d_lid), 0.0f) / k;
            d = SDL_max(d, d_lid) + h * h * k * 0.25f;
            float cover = SDL_clamp(0.5f - d, 0.0f, 1.0f);
            float outside = SDL_max(SDL_max(d, SDL_fabsf(px) - rx), 0.0f), fade = SDL_max(1.0f - outside / reach, 0.0f);
            float halo = 0.42f * SDL_expf(-outside / (rx * 0.30f)) * fade * fade * (1.0f - cover);
            uint8_t *pixel = pixel_at(canvas, x, y);
            if (halo > 0.004f) {
                pixel[0] = (uint8_t)SDL_min(255.0f, pixel[0] + eye_halo.r * halo * 0.6f);
                pixel[1] = (uint8_t)SDL_min(255.0f, pixel[1] + eye_halo.g * halo * 0.6f);
                pixel[2] = (uint8_t)SDL_min(255.0f, pixel[2] + eye_halo.b * halo * 0.6f);
            }
            if (cover > 0.0f) {
                float heat = SDL_clamp(1.0f - k0, 0.0f, 1.0f);
                blend(pixel, mix(eye_fill, eye_core, heat * 0.85f), (int)(cover * 255.0f + 0.5f));
            }
        }
    }
}

/* The two header eyes. `look` slides them sideways, -1 to 1. */
static void draw_header_eyes(const struct canvas *canvas, int header, int pad_x, float open, float look)
{
    float rx = header * EYE_RX, ry = header * EYE_RY;
    float spacing = rx * EYE_SPACING;
    float cx = pad_x + rx + look * rx * 0.22f, cy = header * 0.5f;
    draw_eye(canvas, header - 1, cx, cy, rx, ry, open, -1.0f);
    draw_eye(canvas, header - 1, cx + spacing, cy, rx, ry, open, 1.0f);
}

/* The footer: key caps with what they do, and a note when scrolled back. */
static void draw_footer(const struct canvas *canvas, int face, int footer, int pad_x, bool panel_controls,
                        bool scrolled)
{
    int text_face = face == FACE_LARGE ? FACE_MEDIUM : FACE_SMALL;
    const struct console_face *info = &console_faces[text_face];
    int y = canvas->height - footer;
    int cap_height = info->height + (face == FACE_SMALL ? 2 : 6);
    int cap_y = y + (footer - cap_height) / 2 + 1;
    int text_y = cap_y + (cap_height - info->height) / 2;
    int pad = info->width;
    int gap = info->width, group = info->width * 3;
    const char *items[3][3] = {
        { "F10", NULL, "Mic" },
        { "F9", NULL, "Panel" },
        { "PgUp", "PgDn", "Scroll" },
    };
    int x = pad_x;
    for (int item = panel_controls ? 0 : 2; item < 3; ++item) {
        int needed = text_width(text_face, items[item][2]) + gap;
        for (int key = 0; key < 2 && items[item][key]; ++key)
            needed += text_width(text_face, items[item][key]) + pad + gap / 2;
        if (x + needed > canvas->width - pad_x)
            break;
        for (int key = 0; key < 2 && items[item][key]; ++key) {
            int width = text_width(text_face, items[item][key]) + pad;
            outline_rounded(canvas, x, cap_y, width, cap_height, cap_height * 0.28f, keycap_fill, keycap_edge);
            draw_text(canvas, text_face, WEIGHT_BOLD, x + pad / 2, text_y, items[item][key], palette_fg[8]);
            x += width + gap / 2;
        }
        x = draw_text(canvas, text_face, WEIGHT_REGULAR, x + gap / 2, text_y, items[item][2], hint_text) + group;
    }
    if (scrolled) {
        const char *note = "↑ Earlier output";
        int width = text_width(text_face, note);
        draw_text(canvas, text_face, WEIGHT_REGULAR, canvas->width - pad_x - width, text_y, note, accent);
    }
}
