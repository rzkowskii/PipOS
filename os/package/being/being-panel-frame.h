/* being-panel-frame.h: the frame file and in-band protocol of
 * `being-console --panel`, the headless renderer behind Pip's workshop apps.
 *
 * NOT the frame file the renderer writes. being-console.c includes
 * being-console-panel.h, whose panel_encode() layout (width at 4, sequence at
 * 16, flags at 32, title at 96, no status field) is what crates/face/src/panel.rs
 * reads; tests/test_panel_frame_contract.py ties those two together. The
 * struct below is an earlier draft of the layout, still mirrored by
 * mind/hands.py read_frame_header() and checked only against that reader by
 * tests/test_hands.py (PanelFrameTest). Do not use it to read a real frame;
 * it is to be removed with read_frame_header(). The OSC 7770 notes below
 * still describe the in-band protocol.
 *
 * ---------------------------------------------------------------- the process
 *
 *   being-console --panel NAME --size WxH --io -
 *
 * - NAME is terminal, monitor or clock. No COMMAND is given and no pty is
 *   made: hands owns the shell and its pty. The renderer only parses bytes.
 * - --io - : the app's bytes arrive on stdin (hands writes them through
 *   systemd-run --pipe). End of input means: draw a last frame with flags 0
 *   (not visible), then exit 0.
 * - It runs as the unprivileged user being-panel in its own transient unit,
 *   because it parses bytes that caged programs wrote. A parser bug must not
 *   become root. No SDL video, no DRM, no keyboard: SDL_INIT_TIMER at most, or
 *   no SDL at all (the existing headless canvas path is enough).
 * - It writes only /run/being-panel/NAME.frame (created 0640, group
 *   being-face or world-readable 0644 if the face runs as root). It creates
 *   the file at full size once, mmaps it, and never truncates it while open.
 * - Loop: block in poll() on stdin and a signal pipe. Draw at most 30 frames
 *   a second and only the rows that changed. While not visible, sleep in
 *   poll() with no timeout, but still parse input so the grid stays exact.
 * - Bump `beat` every 500 ms while running, so the face can hide a renderer
 *   that died (a beat older than 2.5 s means hide, never freeze on screen).
 * - Without --panel, being-console behaves byte for byte as before (the
 *   Try/installer KMS path and test_local_screen 19/19 are unchanged).
 *
 * ------------------------------------------------------------- the chrome
 *
 * The renderer draws its own chrome; program output can never draw over it:
 * - a 28 px status bar: TITLE on the left, STATUS on the right (both set by
 *   hands through OSC 7770 below, never from the shell's bytes);
 * - a 40 px footer: "Esc Stop Pip · F3 Hide · F10 Mic · PgUp/PgDn";
 * - the text grid in between, in the medium 11x24 font: about 107 columns by
 *   23 rows at 1280x640. hands sets the pty to 107x23 (GRID_* in hands.py).
 *
 * ------------------------------------------------------ OSC 7770 (in band)
 *
 *   ESC ] 7770 ; key=value ; key=value ... BEL      (or ESC \ as terminator)
 *
 * Only hands can send these: it strips OSC 7770 and 7771 from everything the
 * shell side writes before relaying it. Keys (unknown keys are ignored):
 *   visible=0|1          show the panel (flag BP_VISIBLE)
 *   focus=0|1            keys go to this app (BP_FOCUS)
 *   pip=typing|paused|idle   (BP_PIP_TYPING / BP_PIP_PAUSED)
 *   job=busy|idle        a program Pip started is in the foreground (BP_JOB_BUSY)
 *   human=0|1            the owner is typing (BP_HUMAN_TYPING)
 *   title=TEXT           up to 31 bytes of UTF-8, shown left in the status bar
 *   status=TEXT          up to 39 bytes, shown right ("● Pip typing", "Paused ·
 *                        say carry on", "Running 0:12 · Esc stops", "Running in
 *                        4 · Esc or say stop", "Done · exit 0")
 *   scroll=up|down       one page of scrollback (sets BP_SCROLLED until the
 *                        next output, which returns to the live screen)
 *   wipe=1               forget: zero the whole grid AND the scrollback
 *                        history for real (memset), not ED 2J/3J, then redraw
 *                        every row. Needed for "forget that script".
 * Values never contain ';' or control bytes (hands guarantees it).
 * OSC 7771 is the shell's prompt marker; hands consumes it and the renderer
 * never sees it. Any other OSC is consumed and ignored, as today.
 *
 * --------------------------------------------------------- the frame file
 *
 * One struct being_panel_header (128 bytes, little-endian) then `height`
 * rows of `stride` bytes, RGBA8888, top row first. Seqlock:
 *   writer: sequence = odd (release) ; write dirty rows and header fields ;
 *           sequence = even (release)
 *   reader: s1 = sequence (acquire); if odd, skip this frame ; copy the header
 *           and rows dirty_first .. dirty_first + dirty_count ; s2 = sequence ;
 *           s1 != s2 means drop what was copied and try on the next frame.
 * dirty_* describe the rows changed by the last even sequence only, so a
 * reader that missed a sequence (s1 > last seen + 2) must re-upload all rows.
 */
#ifndef BEING_PANEL_FRAME_H
#define BEING_PANEL_FRAME_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define BP_MAGIC "BPN1"
#define BP_VERSION 1u
#define BP_HEADER_SIZE 128u
#define BP_TITLE_BYTES 32u
#define BP_STATUS_BYTES 40u
#define BP_OSC_FLAGS 7770
#define BP_OSC_PROMPT 7771

enum being_panel_flags {
    BP_VISIBLE = 1u << 0,
    BP_FOCUS = 1u << 1,
    BP_PIP_TYPING = 1u << 2,
    BP_PIP_PAUSED = 1u << 3,
    BP_JOB_BUSY = 1u << 4,
    BP_HUMAN_TYPING = 1u << 5,
    BP_SCROLLED = 1u << 6,
};

struct being_panel_header {
    char magic[4];          /*   0  "BPN1" */
    uint32_t version;       /*   4  BP_VERSION */
    uint32_t width;         /*   8  pixels */
    uint32_t height;        /*  12  pixels */
    uint32_t stride;        /*  16  bytes per row, >= width * 4 */
    uint32_t flags;         /*  20  enum being_panel_flags */
    uint64_t sequence;      /*  24  seqlock: odd while writing */
    uint32_t dirty_first;   /*  32  first row changed by this sequence */
    uint32_t dirty_count;   /*  36  rows changed (0: header only) */
    uint32_t beat;          /*  40  +1 every 500 ms while alive */
    int32_t cursor_x;       /*  44  cursor cell, or -1 when hidden */
    int32_t cursor_y;       /*  48 */
    uint32_t reserved;      /*  52  zero */
    char title[BP_TITLE_BYTES];    /*  56  NUL-terminated UTF-8 */
    char status[BP_STATUS_BYTES];  /*  88  NUL-terminated UTF-8 */
};

_Static_assert(sizeof(struct being_panel_header) == BP_HEADER_SIZE, "panel header must be 128 bytes");
_Static_assert(offsetof(struct being_panel_header, sequence) == 24, "sequence offset");
_Static_assert(offsetof(struct being_panel_header, title) == 56, "title offset");
_Static_assert(offsetof(struct being_panel_header, status) == 88, "status offset");

static inline size_t being_panel_file_size(uint32_t width, uint32_t height)
{
    return BP_HEADER_SIZE + (size_t)width * 4u * height;
}

static inline void being_panel_init(struct being_panel_header *header, uint32_t width, uint32_t height)
{
    memset(header, 0, sizeof(*header));
    memcpy(header->magic, BP_MAGIC, 4);
    header->version = BP_VERSION;
    header->width = width;
    header->height = height;
    header->stride = width * 4u;
    header->cursor_x = header->cursor_y = -1;
}

/* Writer side of the seqlock. `frame` is the mmapped file. */
static inline void being_panel_begin(struct being_panel_header *frame)
{
    __atomic_store_n(&frame->sequence, frame->sequence | 1u, __ATOMIC_RELEASE);
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
}

static inline void being_panel_rows(struct being_panel_header *frame, uint32_t first, uint32_t count,
                                    const uint8_t *rgba, size_t source_stride)
{
    uint8_t *pixels = (uint8_t *)frame + BP_HEADER_SIZE;
    if (first >= frame->height)
        return;
    if (count > frame->height - first)
        count = frame->height - first;
    for (uint32_t row = 0; row < count; ++row)
        memcpy(pixels + (size_t)(first + row) * frame->stride, rgba + (size_t)row * source_stride,
               frame->width * 4u);
    frame->dirty_first = first;
    frame->dirty_count = count;
}

static inline void being_panel_text(char *field, size_t size, const char *text)
{
    size_t length = 0;
    memset(field, 0, size);
    /* Copy whole UTF-8 sequences only, so a cut never leaves half a character. */
    while (text[length] && length < size - 1)
        ++length;
    while (length > 0 && length < strlen(text) && ((unsigned char)text[length] & 0xC0) == 0x80)
        --length;
    memcpy(field, text, length);
}

static inline void being_panel_end(struct being_panel_header *frame)
{
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    __atomic_store_n(&frame->sequence, (frame->sequence | 1u) + 1u, __ATOMIC_RELEASE);
}

/* Apply one key=value of an OSC 7770 sequence to a header. Returns a BP_ACT_*
 * the renderer must also carry out (wipe or scroll), or BP_ACT_NONE.
 * Apply it to the renderer's private copy of the header, never to the
 * mmapped one: the flags, title and status reach the face only when the
 * copy is published between being_panel_begin() and being_panel_end(), so
 * the face never reads a torn title or status. */
enum { BP_ACT_NONE, BP_ACT_WIPE, BP_ACT_SCROLL_UP, BP_ACT_SCROLL_DOWN };

static inline int being_panel_osc(struct being_panel_header *header, const char *key, const char *value)
{
    uint32_t bit = 0;
    int on = strcmp(value, "1") == 0;
    if (strcmp(key, "visible") == 0)
        bit = BP_VISIBLE;
    else if (strcmp(key, "focus") == 0)
        bit = BP_FOCUS;
    else if (strcmp(key, "human") == 0)
        bit = BP_HUMAN_TYPING;
    else if (strcmp(key, "job") == 0) {
        bit = BP_JOB_BUSY;
        on = strcmp(value, "busy") == 0;
    } else if (strcmp(key, "pip") == 0) {
        header->flags &= ~(uint32_t)(BP_PIP_TYPING | BP_PIP_PAUSED);
        if (strcmp(value, "typing") == 0)
            header->flags |= BP_PIP_TYPING;
        else if (strcmp(value, "paused") == 0)
            header->flags |= BP_PIP_PAUSED;
        return BP_ACT_NONE;
    } else if (strcmp(key, "title") == 0) {
        being_panel_text(header->title, sizeof(header->title), value);
        return BP_ACT_NONE;
    } else if (strcmp(key, "status") == 0) {
        being_panel_text(header->status, sizeof(header->status), value);
        return BP_ACT_NONE;
    } else if (strcmp(key, "wipe") == 0) {
        return on ? BP_ACT_WIPE : BP_ACT_NONE;
    } else if (strcmp(key, "scroll") == 0) {
        header->flags |= BP_SCROLLED;
        return strcmp(value, "up") == 0 ? BP_ACT_SCROLL_UP : BP_ACT_SCROLL_DOWN;
    }
    if (bit) {
        if (on)
            header->flags |= bit;
        else
            header->flags &= ~bit;
    }
    return BP_ACT_NONE;
}

#endif
