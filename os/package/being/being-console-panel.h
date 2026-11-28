/* Panel mode (`being-console --panel NAME`): the frame file the face composites
 * under the eye strip, and the chrome the renderer draws around the text.
 * See docs/design/voice-apps.md sections 3.1-3.3 and the workshop decisions.
 *
 * The renderer runs as the unprivileged user being-panel, opens no display,
 * no DRM device and no keyboard, and reads the app's bytes from --io (hands
 * relays them from the pty it holds). Everything it draws lands in
 * /run/being-panel/NAME.frame, which only it writes.
 *
 * Frame file: a 128-byte header, then `height` rows of `stride` bytes of
 * RGBA (bytes R, G, B, A; A is always 255). All integers little-endian.
 *
 *   offset size  field
 *        0    4  magic "BPN1"
 *        4    4  width in pixels (fixed for the file's life)
 *        8    4  height in pixels (fixed)
 *       12    4  stride: bytes per row, width * 4
 *       16    8  sequence: odd while a frame is being written, even when whole
 *       24    4  dirty_first: first pixel row this frame changed
 *       28    4  dirty_count: pixel rows this frame changed (0: header only)
 *       32    4  flags (PANEL_* below)
 *       36    4  cursor_x: left of the cursor cell in pixels, -1 when hidden
 *       40    4  cursor_y: top of the cursor cell in pixels, -1 when hidden
 *       44    4  beat: +1 every 500 ms while visible
 *       48    4  columns of the text grid (hands sizes the pty from these)
 *       52    4  rows of the text grid
 *       56    4  newest_y: top of the newest output line in pixels, -1 none
 *       60    2  cell_width in pixels
 *       62    2  cell_height in pixels
 *       64    4  pid of the renderer
 *       68   28  reserved, zero
 *       96   32  title, UTF-8, NUL padded (at most 31 bytes)
 *
 * Seqlock. The renderer writes the odd sequence, then the dirty rows, then the
 * header fields, then the even sequence, each with its own pwrite(). A reader
 * reads the header, then the rows it wants, then the sequence again, and drops
 * the frame when the first sequence was odd or the two differ. A reader that
 * did not see the previous frame (the new sequence is not its last one + 2)
 * uploads every row, since dirty_first/dirty_count only cover the last frame.
 *
 * The file appears whole: it is written as NAME.frame.tmp and renamed. The
 * renderer removes it when its input ends or it is told to stop. A file left
 * by a renderer that died has a stale beat (visible) or a dead pid (hidden).
 *
 * While hidden (SIGUSR2) the renderer still reads and parses its input, so the
 * program never blocks, and rewrites the header when a flag changes, but draws
 * nothing and bumps no beat. SIGUSR1 shows it again with a full frame.
 *
 * In-band control from hands, which strips every OSC a program writes before
 * relaying: ESC ] 7770 ; key=value [; key=value ...] BEL (or ESC \). Values
 * hold no ';' and no control bytes. Keys:
 *   pip=typing|paused|idle   job=busy|done|failed|idle   human=0|1   focus=0|1
 *   title=TEXT (31 bytes)    status=TEXT (63 bytes; empty: the default)
 *   wipe=1 (scrollback, screen and the frame's pixels, for real)
 *   scroll=up|down|end (a page; end returns to the newest output)
 * ED 3 (ESC [ 3 J) also wipes the scrollback for real in panel mode.
 */
#include <sys/stat.h>

#define PANEL_MAGIC "BPN1"
enum { PANEL_HEADER = 128, PANEL_TITLE = 32, PANEL_STATUS = 64, PANEL_OSC = 256 };
enum {
    PANEL_VISIBLE = 1u << 0,
    PANEL_FOCUS = 1u << 1,
    PANEL_PIP_TYPING = 1u << 2,
    PANEL_PIP_PAUSED = 1u << 3,
    PANEL_JOB_BUSY = 1u << 4,
    PANEL_HUMAN_TYPING = 1u << 5,
    PANEL_SCROLLED = 1u << 6,
    PANEL_JOB_DONE = 1u << 7,
    PANEL_JOB_FAILED = 1u << 8,
};
/* The chrome: status bar on top, key hints at the bottom (voice-apps.md 3.2). */
enum { PANEL_STATUS_BAR = 28, PANEL_FOOTER = 40, PANEL_PAD_TOP = 6, PANEL_PAD_BOTTOM = 4 };
enum { PANEL_FRAME_MS = 34, PANEL_BEAT_MS = 500 }; /* at most 30 frames a second */

/* What hands controls in-band. Changes set `changed`. */
struct panel_control {
    bool pip_typing, pip_paused, job_busy, job_done, job_failed, human_typing, unfocused;
    char title[PANEL_TITLE];
    char status[PANEL_STATUS];
    int scroll_request; /* +1 a page up, -1 a page down, -2 to the end */
    bool wipe_request, wiped, changed;
};

struct panel_header {
    uint32_t width, height, stride;
    uint64_t sequence;
    uint32_t dirty_first, dirty_count, flags;
    int32_t cursor_x, cursor_y;
    uint32_t beat, columns, rows;
    int32_t newest_y;
    uint16_t cell_width, cell_height;
    uint32_t pid;
    char title[PANEL_TITLE];
};

static void put_le(uint8_t *at, uint64_t value, int bytes)
{
    for (int i = 0; i < bytes; ++i)
        at[i] = (uint8_t)(value >> (8 * i));
}

static void panel_encode(const struct panel_header *header, uint8_t out[PANEL_HEADER])
{
    memset(out, 0, PANEL_HEADER);
    memcpy(out, PANEL_MAGIC, 4);
    put_le(out + 4, header->width, 4);
    put_le(out + 8, header->height, 4);
    put_le(out + 12, header->stride, 4);
    put_le(out + 16, header->sequence, 8);
    put_le(out + 24, header->dirty_first, 4);
    put_le(out + 28, header->dirty_count, 4);
    put_le(out + 32, header->flags, 4);
    put_le(out + 36, (uint32_t)header->cursor_x, 4);
    put_le(out + 40, (uint32_t)header->cursor_y, 4);
    put_le(out + 44, header->beat, 4);
    put_le(out + 48, header->columns, 4);
    put_le(out + 52, header->rows, 4);
    put_le(out + 56, (uint32_t)header->newest_y, 4);
    put_le(out + 60, header->cell_width, 2);
    put_le(out + 62, header->cell_height, 2);
    put_le(out + 64, header->pid, 4);
    memcpy(out + 96, header->title, PANEL_TITLE - 1);
}

/* Copies printable UTF-8 into a fixed field, cut at a character boundary. */
static void panel_copy_text(char *field, size_t size, const char *text, size_t length)
{
    size_t used = 0;
    for (size_t i = 0; i < length && used + 1 < size; ++i) {
        unsigned char byte = (unsigned char)text[i];
        if (byte < 0x20 || byte == 0x7f)
            continue;
        field[used++] = (char)byte;
    }
    /* Drop a character the cut left incomplete. */
    size_t lead = used;
    while (lead > 0 && used - lead < 4 && ((unsigned char)field[lead - 1] & 0xc0) == 0x80)
        lead--;
    if (lead > 0) {
        unsigned char byte = (unsigned char)field[lead - 1];
        size_t need = byte >= 0xf0 ? 4 : byte >= 0xe0 ? 3 : byte >= 0xc0 ? 2 : 1;
        if (used - (lead - 1) < need)
            used = lead - 1;
    }
    field[used] = '\0';
}

static bool panel_word(const char *value, size_t length, const char *word)
{
    return length == strlen(word) && memcmp(value, word, length) == 0;
}

/* One OSC string, without its ESC ] and terminator. Only 7770 is ours. */
static void panel_osc(struct panel_control *control, const char *text, size_t length)
{
    if (length < 5 || memcmp(text, "7770;", 5) != 0)
        return;
    size_t at = 5;
    while (at < length) {
        size_t end = at;
        while (end < length && text[end] != ';')
            end++;
        const char *pair = text + at, *equals = memchr(pair, '=', end - at);
        if (equals) {
            size_t key = (size_t)(equals - pair);
            const char *value = equals + 1;
            size_t size = (size_t)(text + end - value);
            bool known = true;
            if (key == 3 && memcmp(pair, "pip", 3) == 0) {
                control->pip_typing = panel_word(value, size, "typing");
                control->pip_paused = panel_word(value, size, "paused");
            } else if (key == 3 && memcmp(pair, "job", 3) == 0) {
                control->job_busy = panel_word(value, size, "busy");
                control->job_done = panel_word(value, size, "done");
                control->job_failed = panel_word(value, size, "failed");
            } else if (key == 5 && memcmp(pair, "human", 5) == 0) {
                control->human_typing = panel_word(value, size, "1");
            } else if (key == 5 && memcmp(pair, "focus", 5) == 0) {
                control->unfocused = panel_word(value, size, "0");
            } else if (key == 5 && memcmp(pair, "title", 5) == 0) {
                panel_copy_text(control->title, sizeof(control->title), value, size);
            } else if (key == 6 && memcmp(pair, "status", 6) == 0) {
                panel_copy_text(control->status, sizeof(control->status), value, size);
            } else if (key == 4 && memcmp(pair, "wipe", 4) == 0) {
                known = panel_word(value, size, "1");
                control->wipe_request |= known;
            } else if (key == 6 && memcmp(pair, "scroll", 6) == 0) {
                control->scroll_request = panel_word(value, size, "up") ? 1 : panel_word(value, size, "down") ? -1 :
                                          panel_word(value, size, "end") ? -2 : control->scroll_request;
            } else {
                known = false;
            }
            control->changed |= known;
        }
        at = end + 1;
    }
}

static uint32_t panel_flags(const struct panel_control *control, bool visible, bool scrolled)
{
    uint32_t flags = 0;
    if (visible)
        flags |= PANEL_VISIBLE;
    if (visible && !control->unfocused)
        flags |= PANEL_FOCUS;
    if (control->pip_typing)
        flags |= PANEL_PIP_TYPING;
    if (control->pip_paused)
        flags |= PANEL_PIP_PAUSED;
    if (control->job_busy)
        flags |= PANEL_JOB_BUSY;
    if (control->job_done)
        flags |= PANEL_JOB_DONE;
    if (control->job_failed)
        flags |= PANEL_JOB_FAILED;
    if (control->human_typing)
        flags |= PANEL_HUMAN_TYPING;
    if (scrolled)
        flags |= PANEL_SCROLLED;
    return flags;
}

/* The right-hand status, and the colour of its dot. hands sends the exact
 * wording for runs and countdowns ("Running 0:12 · Esc stops"); these are
 * what the flags alone say. */
static const char *panel_status(const struct panel_control *control, struct rgb *dot)
{
    if (control->pip_paused) {
        *dot = palette_fg[4];
        return control->status[0] ? control->status : "Paused · say carry on";
    }
    if (control->pip_typing) {
        *dot = accent;
        return control->status[0] ? control->status : "Pip typing";
    }
    if (control->job_busy) {
        *dot = palette_fg[3];
        return control->status[0] ? control->status : "Running · Esc stops";
    }
    *dot = hint_text;
    return control->status[0] ? control->status : "Ready";
}

/* The text face for chrome: one size down from the cell face. */
static int panel_chrome_face(int face)
{
    return face == FACE_LARGE ? FACE_MEDIUM : FACE_SMALL;
}

/* The area under the text: the console's deep blue-black, a little lighter at
 * the top, dithered so the gradient does not band. */
static void paint_panel_backdrop(const struct canvas *canvas)
{
    static const uint8_t bayer[4][4] = { { 0, 8, 2, 10 }, { 12, 4, 14, 6 }, { 3, 11, 1, 9 }, { 15, 7, 13, 5 } };
    for (int y = 0; y < canvas->height; ++y) {
        float t = (float)y / (float)SDL_max(canvas->height - 1, 1);
        float base_r = 10.0f - 5.0f * t, base_g = 17.0f - 8.0f * t, base_b = 25.0f - 12.0f * t;
        uint8_t *pixel = pixel_at(canvas, 0, y);
        for (int x = 0; x < canvas->width; ++x, pixel += 4) {
            float dither = (bayer[y & 3][x & 3] - 7.5f) / 16.0f;
            pixel[0] = (uint8_t)SDL_clamp(base_r + dither + 0.5f, 0.0f, 255.0f);
            pixel[1] = (uint8_t)SDL_clamp(base_g + dither + 0.5f, 0.0f, 255.0f);
            pixel[2] = (uint8_t)SDL_clamp(base_b + dither + 0.5f, 0.0f, 255.0f);
            pixel[3] = 255;
        }
    }
}

/* The status bar: the app's title on the left, Pip's status on the right.
 * Program output never reaches these rows, so a script cannot fake them. */
static void draw_panel_status(const struct canvas *canvas, int face, int pad_x, const char *title,
                              const struct panel_control *control)
{
    int text_face = panel_chrome_face(face);
    const struct console_face *info = &console_faces[text_face];
    int bar = SDL_min(PANEL_STATUS_BAR, canvas->height);
    fill_rect(canvas, 0, 0, canvas->width, bar, (struct rgb){13, 24, 33});
    fill_rect(canvas, 0, bar - 1, canvas->width, 1, (struct rgb){26, 42, 53});
    int y = (bar - info->height) / 2;
    draw_text(canvas, text_face, WEIGHT_BOLD, pad_x, y, title, wordmark_text);
    struct rgb dot;
    const char *status = panel_status(control, &dot);
    int width = text_width(text_face, status);
    int x = canvas->width - pad_x - width;
    int start = x - 2 * info->width - 5;
    if (start < pad_x + text_width(text_face, title) + info->width)
        return;
    int chip_height = SDL_min(info->height + 6, bar - 4);
    outline_rounded(canvas, start, (bar - chip_height) / 2, width + 2 * info->width + 12,
                    chip_height, chip_height / 2.0f, (struct rgb){8, 17, 24}, (struct rgb){26, 42, 53});
    draw_text(canvas, text_face, WEIGHT_REGULAR, x, y, status, control->pip_typing || control->pip_paused ||
              control->job_busy ? wordmark_text : hint_text);
    draw_text(canvas, text_face, WEIGHT_REGULAR, x - 2 * info->width, y, "●", dot);
}

/* The footer: Esc Stop Pip · F3 Hide · F10 Mic · PgUp/PgDn (fn F3 and fn F10
 * where the top row is printed-first, see function_keys_need_fn()), and a
 * note when scrolled back. Hints that do not fit are left out from the right. */
static void draw_panel_footer(const struct canvas *canvas, int face, int pad_x, bool scrolled, bool fn)
{
    int text_face = panel_chrome_face(face);
    const struct console_face *info = &console_faces[text_face];
    int footer = SDL_min(PANEL_FOOTER, canvas->height);
    int y = canvas->height - footer;
    fill_rect(canvas, 0, y, canvas->width, footer, footer_fill);
    fill_rect(canvas, 0, y, canvas->width, 1, footer_rule);
    int cap_height = info->height + (text_face == FACE_SMALL ? 6 : 8);
    int cap_y = y + (footer - cap_height) / 2 + 1;
    int text_y = cap_y + (cap_height - info->height) / 2;
    int pad = info->width, gap = info->width, group = info->width * 3;
    const char *items[4][3] = {
        { "Esc", NULL, "Stop Pip" },
        { "F3", NULL, "Hide" },
        { "F10", NULL, "Mic" },
        { "PgUp", "PgDn", "Scroll" },
    };
    function_key_caps("F3", fn, items[1]);
    function_key_caps("F10", fn, items[2]);
    const char *note = "↑ Earlier output";
    int right = canvas->width - pad_x - (scrolled ? text_width(text_face, note) + group : 0);
    int x = pad_x;
    for (int item = 0; item < 4; ++item) {
        int needed = text_width(text_face, items[item][2]) + gap;
        for (int key = 0; key < 2 && items[item][key]; ++key)
            needed += text_width(text_face, items[item][key]) + pad + gap / 2;
        if (x + needed > right)
            break;
        for (int key = 0; key < 2 && items[item][key]; ++key) {
            int width = text_width(text_face, items[item][key]) + pad;
            outline_rounded(canvas, x, cap_y, width, cap_height, cap_height * 0.28f, keycap_fill, keycap_edge);
            draw_text(canvas, text_face, WEIGHT_BOLD, x + pad / 2, text_y, items[item][key], palette_fg[8]);
            x += width + gap / 2;
        }
        x = draw_text(canvas, text_face, WEIGHT_REGULAR, x + gap / 2, text_y, items[item][2], hint_text) + group;
    }
    if (scrolled)
        draw_text(canvas, text_face, WEIGHT_REGULAR, canvas->width - pad_x - text_width(text_face, note), text_y,
                  note, accent);
}

/* -- the frame file ---------------------------------------------------------- */

struct panel_file {
    int directory, fd;
    char name[48], temporary[56];
    dev_t device;
    ino_t inode;
};

static bool panel_pwrite(int fd, const void *bytes, size_t length, off_t offset)
{
    const uint8_t *at = bytes;
    while (length > 0) {
        ssize_t written = pwrite(fd, at, length, offset);
        if (written < 0 && errno == EINTR)
            continue;
        if (written <= 0)
            return false;
        at += written;
        length -= (size_t)written;
        offset += written;
    }
    return true;
}

static bool panel_write_sequence(int fd, uint64_t sequence)
{
    uint8_t bytes[8];
    put_le(bytes, sequence, 8);
    return panel_pwrite(fd, bytes, sizeof(bytes), 16);
}

/* One frame under the seqlock: odd sequence, rows, header, even sequence. */
static bool panel_write_frame(const struct panel_file *file, struct panel_header *header, const uint8_t *pixels,
                              uint32_t first, uint32_t count)
{
    uint8_t bytes[PANEL_HEADER];
    header->sequence |= 1;
    if (!panel_write_sequence(file->fd, header->sequence))
        return false;
    if (count && !panel_pwrite(file->fd, pixels + (size_t)first * header->stride, (size_t)count * header->stride,
                               PANEL_HEADER + (off_t)first * header->stride))
        return false;
    header->dirty_first = first;
    header->dirty_count = count;
    panel_encode(header, bytes);
    if (!panel_pwrite(file->fd, bytes, sizeof(bytes), 0))
        return false;
    header->sequence++;
    return panel_write_sequence(file->fd, header->sequence);
}

/* Writes the whole first frame to NAME.frame.tmp and renames it into place. */
static bool panel_create(struct panel_file *file, struct panel_header *header, const uint8_t *pixels)
{
    file->fd = openat(file->directory, file->temporary, O_RDWR | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0640);
    if (file->fd < 0)
        return false;
    off_t size = PANEL_HEADER + (off_t)header->stride * header->height;
    uint8_t bytes[PANEL_HEADER];
    struct stat status;
    header->sequence = 2;
    header->dirty_first = 0;
    header->dirty_count = header->height;
    panel_encode(header, bytes);
    if (ftruncate(file->fd, size) != 0 ||
        !panel_pwrite(file->fd, pixels, (size_t)header->stride * header->height, PANEL_HEADER) ||
        !panel_pwrite(file->fd, bytes, sizeof(bytes), 0) || fstat(file->fd, &status) != 0 ||
        renameat(file->directory, file->temporary, file->directory, file->name) != 0) {
        unlinkat(file->directory, file->temporary, 0);
        close(file->fd);
        file->fd = -1;
        return false;
    }
    file->device = status.st_dev;
    file->inode = status.st_ino;
    return true;
}

/* Removes the frame file if it is still this renderer's. */
static void panel_remove(struct panel_file *file)
{
    struct stat status;
    if (file->fd < 0)
        return;
    if (fstatat(file->directory, file->name, &status, AT_SYMLINK_NOFOLLOW) == 0 && status.st_dev == file->device &&
        status.st_ino == file->inode)
        unlinkat(file->directory, file->name, 0);
    close(file->fd);
    file->fd = -1;
}
