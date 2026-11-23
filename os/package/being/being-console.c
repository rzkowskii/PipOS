#define _DEFAULT_SOURCE
#define _DARWIN_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#define SDL_MAIN_HANDLED
#include <SDL.h>

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <poll.h>
#include <pwd.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#ifdef __APPLE__
#include <util.h>
#else
#include <pty.h>
#endif
#ifdef __linux__
#include <sys/prctl.h>
#endif

#include "being-console-gl.h"
#include "being-console-draw.h"
#include "being-console-panel.h"

/* The child's terminal. Escape sequences understood (the console contract with
 * mind/console_ui.py, which only sends them when BEING_CONSOLE_UI=1):
 * SGR 0 1 2 7 22 27 39 49 30-37 90-97 40-47 100-107; ED 2J (and 0J); EL K;
 * CUP H and row;colH; CUU/CUD/CUF/CUB; CHA G; DECTCEM ?25l / ?25h. Others are
 * consumed and ignored. Text is UTF-8. */
enum { COLUMNS = 200, HISTORY = 400, MAX_ROWS = HISTORY / 2 };
enum { ATTR_BOLD = 1, ATTR_DIM = 2, ATTR_REVERSE = 4 };
enum { GROUND, ESCAPE, ESCAPE_SKIP, CSI, OSC };

struct cell {
    uint8_t glyph, fg, bg, attr;
};

struct terminal {
    struct cell cells[HISTORY][COLUMNS];
    size_t top;       /* history line shown on the screen's first row */
    int row, column;  /* the cursor, on the screen */
    int rows, columns;
    int scroll;       /* lines scrolled back by Page Up */
    int state;
    int params[16], param_count;
    bool private_mode, param_started;
    uint32_t codepoint;
    int utf8_tail;
    struct cell pen;
    bool cursor_hidden;
    /* Panel mode only (NULL otherwise): OSC 7770 from hands, a real ED 3. */
    struct panel_control *control;
    char osc[PANEL_OSC];
    size_t osc_length;
    bool osc_overflow;
};

struct layout {
    int width, height, face, cell_width, cell_height, columns, rows, x0, y0, header, footer, pad_x;
};

struct session {
    pid_t pid;
    int master;
    bool running;
    int status;
    char input[1024];
    size_t input_size;
};

/* The header eyes blink now and then and sometimes glance aside. */
struct eyes {
    Uint32 next_blink, blink_start, next_glance, glance_start;
    int blinks_left;
    float glance;
    uint32_t seed;
};
enum { BLINK_MS = 190, GLANCE_MS = 1700, GLANCE_MOVE_MS = 260 };
/* As crates/face/src/link.rs: no lid reading this long after the start, ask
 * for the panel; nobody takes the request this long after, draw anyway. */
enum { LID_GRACE_MS = 4000, RECOVER_WAIT_MS = 2000 };
/* A verbose boot (being.verbose=1) leaves its messages on the screen this long
 * first, as the face does; being.verbose_hold=SECONDS changes it, 0 waits for a key. */
enum { VERBOSE_HOLD_S = 20 };
#define VERBOSE_RELEASED "/run/being-verbose-released"

struct options {
    const char *snapshot;
    double exit_after;
    int headless_width, headless_height;
    const char *feed;
};

static volatile sig_atomic_t stopping;
static struct terminal console_terminal;

static void stop(int signal_number)
{
    stopping = signal_number;
}

static void reap(struct session *session)
{
    pid_t waited = waitpid(session->pid, &session->status, WNOHANG);
    if (waited != 0 && !(waited < 0 && errno == EINTR))
        session->running = false;
}

static void signal_child(const struct session *session, int signal_number)
{
    // forkpty makes the child a session leader. Just after the fork it may still be setting up.
    if (kill(-session->pid, signal_number) < 0 && errno == ESRCH && session->running)
        kill(session->pid, signal_number);
}

static void finish_session(struct session *session)
{
    if (session->running) {
        signal_child(session, SIGTERM);
        const struct timespec interval = { .tv_nsec = 20000000 };
        for (int step = 0; step < 50; ++step) {
            if (session->running)
                reap(session);
            if (!session->running && kill(-session->pid, 0) < 0 && errno == ESRCH)
                break;
            if (step == 25)
                signal_child(session, SIGKILL);
            nanosleep(&interval, NULL);
        }
    }
    close(session->master);
}

static struct layout measure(const struct display *display)
{
    struct layout geometry = {0};
    if (display->window)
        SDL_GL_GetDrawableSize(display->window, &geometry.width, &geometry.height);
    else
        geometry.width = display->headless_width, geometry.height = display->headless_height;
    int width = geometry.width, height = geometry.height;
    int face = width >= 1480 && height >= 1000 ? FACE_LARGE : width >= 750 && height >= 550 ? FACE_MEDIUM : FACE_SMALL;
    const struct chrome *chrome = &console_chrome[face];
    geometry.face = face;
    geometry.cell_width = console_faces[face].width;
    geometry.cell_height = console_faces[face].height;
    geometry.header = chrome->header;
    geometry.footer = chrome->footer;
    geometry.pad_x = chrome->pad_x;
    geometry.columns = SDL_clamp((width - 2 * chrome->pad_x) / geometry.cell_width, 1, COLUMNS);
    geometry.rows = SDL_clamp((height - chrome->header - chrome->footer - chrome->pad_top - chrome->pad_bottom) /
                                  geometry.cell_height, 1, MAX_ROWS);
    geometry.x0 = chrome->pad_x;
    geometry.y0 = chrome->header + chrome->pad_top;
    return geometry;
}

static void size_pty(int master, const struct layout *geometry)
{
    struct winsize size = { .ws_row = (unsigned short)geometry->rows, .ws_col = (unsigned short)geometry->columns };
    ioctl(master, TIOCSWINSZ, &size);
}

/* -- the terminal ---------------------------------------------------------- */

static struct cell *line_at(struct terminal *terminal, size_t line)
{
    return terminal->cells[line % HISTORY];
}

static struct cell blank(const struct terminal *terminal)
{
    return (struct cell){ .bg = terminal->pen.bg };
}

static void clear_cells(struct terminal *terminal, size_t line, int from, int to)
{
    struct cell *cells = line_at(terminal, line), empty = blank(terminal);
    for (int column = SDL_max(from, 0); column < SDL_min(to, COLUMNS); ++column)
        cells[column] = empty;
}

static int max_scroll(const struct terminal *terminal)
{
    return (int)SDL_min(terminal->top, (size_t)(HISTORY - terminal->rows));
}

static void scroll_up(struct terminal *terminal, int lines)
{
    for (int i = 0; i < lines; ++i) {
        terminal->top++;
        clear_cells(terminal, terminal->top + (size_t)terminal->rows - 1, 0, COLUMNS);
        if (terminal->scroll > 0)
            terminal->scroll = SDL_min(terminal->scroll + 1, max_scroll(terminal));
    }
}

static void newline(struct terminal *terminal)
{
    terminal->column = 0;
    if (terminal->row < terminal->rows - 1)
        terminal->row++;
    else
        scroll_up(terminal, 1);
}

static void resize_terminal(struct terminal *terminal, int rows, int columns)
{
    if (!terminal->rows) {
        terminal->rows = rows;
        terminal->columns = columns;
        return;
    }
    if (rows > terminal->rows) {
        for (int row = terminal->rows; row < rows; ++row) {
            struct cell *cells = line_at(terminal, terminal->top + (size_t)row);
            memset(cells, 0, sizeof(terminal->cells[0]));
        }
    } else if (terminal->row >= rows) {
        terminal->top += (size_t)(terminal->row - rows + 1);
        terminal->row = rows - 1;
    }
    terminal->rows = rows;
    terminal->columns = columns;
    terminal->column = SDL_min(terminal->column, columns);
    terminal->scroll = SDL_min(terminal->scroll, max_scroll(terminal));
}

static void put_cell(struct terminal *terminal, int glyph)
{
    if (terminal->column >= terminal->columns)
        newline(terminal);
    struct cell cell = terminal->pen;
    cell.glyph = (uint8_t)glyph;
    line_at(terminal, terminal->top + (size_t)terminal->row)[terminal->column++] = cell;
}

static bool row_used(struct terminal *terminal, int row)
{
    const struct cell *cells = line_at(terminal, terminal->top + (size_t)row);
    for (int column = 0; column < COLUMNS; ++column)
        if (cells[column].glyph || cells[column].bg)
            return true;
    return false;
}

/* Forgets everything: screen, scrollback, pen and cursor (panel wipe=1). */
static void wipe_terminal(struct terminal *terminal)
{
    memset(terminal->cells, 0, sizeof(terminal->cells));
    terminal->top = 0;
    terminal->row = terminal->column = 0;
    terminal->scroll = 0;
    terminal->pen = (struct cell){0};
    terminal->cursor_hidden = false;
}

/* ED 3 in panel mode: the scrollback goes for real; the screen stays. */
static void wipe_scrollback(struct terminal *terminal)
{
    static struct cell screen[MAX_ROWS][COLUMNS];
    for (int row = 0; row < terminal->rows; ++row)
        memcpy(screen[row], line_at(terminal, terminal->top + (size_t)row), sizeof(screen[0]));
    memset(terminal->cells, 0, sizeof(terminal->cells));
    terminal->top = 0;
    for (int row = 0; row < terminal->rows; ++row)
        memcpy(terminal->cells[row], screen[row], sizeof(screen[0]));
    memset(screen, 0, sizeof(screen));
    terminal->scroll = 0;
}

static void erase_display(struct terminal *terminal, int mode)
{
    if (mode == 3 && terminal->control) {
        wipe_scrollback(terminal);
        terminal->control->wiped = true; /* scrolled-back pixels go from the frame too */
        return;
    }
    if (mode == 2 || mode == 3) {
        /* Keep what was on the screen in the scrollback, then start clean. */
        int used = 0;
        for (int row = 0; row < terminal->rows; ++row)
            if (row_used(terminal, row))
                used = row + 1;
        terminal->top += (size_t)used;
        for (int row = 0; row < terminal->rows; ++row)
            clear_cells(terminal, terminal->top + (size_t)row, 0, COLUMNS);
        return;
    }
    if (mode == 0) {
        clear_cells(terminal, terminal->top + (size_t)terminal->row, terminal->column, COLUMNS);
        for (int row = terminal->row + 1; row < terminal->rows; ++row)
            clear_cells(terminal, terminal->top + (size_t)row, 0, COLUMNS);
    } else if (mode == 1) {
        for (int row = 0; row < terminal->row; ++row)
            clear_cells(terminal, terminal->top + (size_t)row, 0, COLUMNS);
        clear_cells(terminal, terminal->top + (size_t)terminal->row, 0, terminal->column + 1);
    }
}

static int param(const struct terminal *terminal, int index, int fallback)
{
    return index < terminal->param_count && terminal->params[index] > 0 ? terminal->params[index] : fallback;
}

static void select_graphic_rendition(struct terminal *terminal)
{
    if (terminal->param_count == 0)
        terminal->params[terminal->param_count++] = 0;
    struct cell *pen = &terminal->pen;
    for (int i = 0; i < terminal->param_count; ++i) {
        int code = terminal->params[i];
        if (code == 0)
            *pen = (struct cell){0};
        else if (code == 1)
            pen->attr |= ATTR_BOLD;
        else if (code == 2)
            pen->attr |= ATTR_DIM;
        else if (code == 7)
            pen->attr |= ATTR_REVERSE;
        else if (code == 22)
            pen->attr &= (uint8_t)~(ATTR_BOLD | ATTR_DIM);
        else if (code == 27)
            pen->attr &= (uint8_t)~ATTR_REVERSE;
        else if (code >= 30 && code <= 37)
            pen->fg = (uint8_t)(code - 29);
        else if (code == 39)
            pen->fg = 0;
        else if (code >= 40 && code <= 47)
            pen->bg = (uint8_t)(code - 39);
        else if (code == 49)
            pen->bg = 0;
        else if (code >= 90 && code <= 97)
            pen->fg = (uint8_t)(code - 81);
        else if (code >= 100 && code <= 107)
            pen->bg = (uint8_t)(code - 91);
        else if (code == 38 || code == 48)
            i += i + 1 < terminal->param_count && terminal->params[i + 1] == 2 ? 4 : 2; /* unsupported colours */
    }
}

static void control_sequence(struct terminal *terminal, unsigned char final)
{
    if (terminal->private_mode) {
        if (param(terminal, 0, 0) == 25 && (final == 'l' || final == 'h'))
            terminal->cursor_hidden = final == 'l';
        return;
    }
    switch (final) {
    case 'm': select_graphic_rendition(terminal); break;
    case 'H': case 'f':
        terminal->row = SDL_clamp(param(terminal, 0, 1) - 1, 0, terminal->rows - 1);
        terminal->column = SDL_clamp(param(terminal, 1, 1) - 1, 0, terminal->columns - 1);
        break;
    case 'A': terminal->row = SDL_max(terminal->row - param(terminal, 0, 1), 0); break;
    case 'B': terminal->row = SDL_min(terminal->row + param(terminal, 0, 1), terminal->rows - 1); break;
    case 'C': terminal->column = SDL_min(terminal->column + param(terminal, 0, 1), terminal->columns - 1); break;
    case 'D': terminal->column = SDL_max(SDL_min(terminal->column, terminal->columns) - param(terminal, 0, 1), 0); break;
    case 'G': terminal->column = SDL_clamp(param(terminal, 0, 1) - 1, 0, terminal->columns - 1); break;
    case 'J': erase_display(terminal, terminal->param_count ? terminal->params[0] : 0); break;
    case 'K': {
        int mode = terminal->param_count ? terminal->params[0] : 0;
        size_t line = terminal->top + (size_t)terminal->row;
        if (mode == 0)
            clear_cells(terminal, line, terminal->column, COLUMNS);
        else if (mode == 1)
            clear_cells(terminal, line, 0, terminal->column + 1);
        else
            clear_cells(terminal, line, 0, COLUMNS);
        break;
    }
    default: break;
    }
}

static void put_codepoint(struct terminal *terminal, uint32_t code)
{
    switch (code) {
    case '\033': terminal->state = ESCAPE; break;
    case '\r': terminal->column = 0; break;
    case '\n': case '\v': case '\f': newline(terminal); break;
    case '\b': if (terminal->column > 0) terminal->column = SDL_min(terminal->column, terminal->columns) - 1; break;
    case '\t': {
        int spaces = 8 - terminal->column % 8;
        while (spaces-- > 0)
            put_cell(terminal, 0);
        break;
    }
    default:
        if (code >= 0x20 && code != 0x7f && !(code >= 0x80 && code < 0xa0))
            put_cell(terminal, glyph_index(code));
        break;
    }
}

static void put_byte(struct terminal *terminal, unsigned char byte)
{
    switch (terminal->state) {
    case ESCAPE:
        terminal->state = GROUND;
        if (byte == '[') {
            terminal->state = CSI;
            terminal->param_count = 0;
            terminal->param_started = false;
            terminal->private_mode = false;
        } else if (byte == ']') {
            terminal->state = OSC;
            terminal->osc_length = 0;
            terminal->osc_overflow = false;
        } else if (byte == '(' || byte == ')' || byte == '#' || byte == '%') {
            terminal->state = ESCAPE_SKIP;
        }
        return;
    case ESCAPE_SKIP:
        /* Panel mode: a program that stops after ESC ( must not swallow the ESC
         * of hands' next control string (a lost wipe=1 would not forget). */
        terminal->state = terminal->control && byte == '\033' ? ESCAPE : GROUND;
        return;
    case OSC:
        if (terminal->control) {
            /* 0x9c is no terminator here: it is a UTF-8 continuation byte in titles. */
            if (byte == '\a' || byte == '\033') {
                terminal->state = byte == '\033' ? ESCAPE : GROUND;
                if (!terminal->osc_overflow)
                    panel_osc(terminal->control, terminal->osc, terminal->osc_length);
                if (terminal->control->wipe_request) {
                    /* In stream order: what came before is gone, what follows stays. */
                    wipe_terminal(terminal);
                    terminal->control->wipe_request = false;
                    terminal->control->wiped = true;
                }
            } else if (terminal->osc_length < sizeof(terminal->osc)) {
                terminal->osc[terminal->osc_length++] = (char)byte;
            } else {
                terminal->osc_overflow = true;
            }
            return;
        }
        if (byte == '\a' || byte == '\033' || byte == 0x9c)
            terminal->state = byte == '\033' ? ESCAPE : GROUND;
        return;
    case CSI:
        if (byte >= '0' && byte <= '9') {
            if (!terminal->param_started && terminal->param_count < 16) {
                terminal->params[terminal->param_count++] = 0;
                terminal->param_started = true;
            }
            if (terminal->param_started) {
                int *value = &terminal->params[terminal->param_count - 1];
                *value = SDL_min(*value * 10 + (byte - '0'), 9999);
            }
        } else if (byte == ';') {
            if (!terminal->param_started && terminal->param_count < 16)
                terminal->params[terminal->param_count++] = 0;
            terminal->param_started = false;
        } else if (byte == '?' || byte == '>' || byte == '=') {
            terminal->private_mode = true;
        } else if (byte >= 0x40 && byte <= 0x7e) {
            terminal->state = GROUND;
            control_sequence(terminal, byte);
        } else if (byte < 0x20 && byte != '\033') {
            put_codepoint(terminal, byte);
        } else if (byte == '\033') {
            terminal->state = ESCAPE;
        }
        return;
    default:
        break;
    }
    if (terminal->utf8_tail > 0) {
        if ((byte & 0xc0) == 0x80) {
            terminal->codepoint = (terminal->codepoint << 6) | (byte & 0x3f);
            if (--terminal->utf8_tail == 0)
                put_codepoint(terminal, terminal->codepoint);
            return;
        }
        terminal->utf8_tail = 0;
        put_codepoint(terminal, '?');
    }
    if (byte < 0x80) {
        put_codepoint(terminal, byte);
    } else if (byte >= 0xc2 && byte <= 0xf4) {
        terminal->utf8_tail = byte >= 0xf0 ? 3 : byte >= 0xe0 ? 2 : 1;
        terminal->codepoint = byte & (byte >= 0xf0 ? 0x07 : byte >= 0xe0 ? 0x0f : 0x1f);
    } else {
        put_codepoint(terminal, '?');
    }
}

static bool read_output(const struct session *session, struct terminal *terminal)
{
    unsigned char bytes[8192];
    ssize_t count = read(session->master, bytes, sizeof(bytes));
    for (ssize_t index = 0; index < count; ++index)
        put_byte(terminal, bytes[index]);
    return count > 0;
}

static void enqueue(struct session *session, struct terminal *terminal, const char *bytes, size_t length)
{
    terminal->scroll = 0;
    if (length <= sizeof(session->input) - session->input_size) {
        memcpy(session->input + session->input_size, bytes, length);
        session->input_size += length;
    }
}

static bool enqueue_text(struct session *session, struct terminal *terminal, const char *text)
{
    size_t length = 0;
    for (; text[length]; ++length) {
        unsigned char byte = (unsigned char)text[length];
        if (byte < ' ' || byte == '\177')
            return false;
    }
    enqueue(session, terminal, text, length);
    return true;
}

static void send_input(struct session *session)
{
    if (!session->input_size)
        return;
    ssize_t sent = write(session->master, session->input, session->input_size);
    if (sent > 0) {
        session->input_size -= (size_t)sent;
        memmove(session->input, session->input + sent, session->input_size);
    } else if (sent < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
        session->input_size = 0;
    }
}

/* -- drawing --------------------------------------------------------------- */

static uint32_t next_random(struct eyes *eyes)
{
    eyes->seed ^= eyes->seed << 13;
    eyes->seed ^= eyes->seed >> 17;
    eyes->seed ^= eyes->seed << 5;
    return eyes->seed;
}

static Uint32 random_between(struct eyes *eyes, Uint32 low, Uint32 high)
{
    return low + next_random(eyes) % (high - low);
}

static void start_eyes(struct eyes *eyes, Uint32 now)
{
    *eyes = (struct eyes){ .seed = 0x9e3779b9u };
    eyes->next_blink = now + random_between(eyes, 2600, 4200);
    eyes->next_glance = now + random_between(eyes, 9000, 15000);
}

static float smooth(float t)
{
    t = SDL_clamp(t, 0.0f, 1.0f);
    return t * t * (3.0f - 2.0f * t);
}

/* Advances the eyes. Returns whether they are moving (so another frame is due). */
static bool animate_eyes(struct eyes *eyes, Uint32 now, float *open, float *look)
{
    bool moving = false;
    *open = 1.0f;
    if (!eyes->blink_start && (Sint32)(now - eyes->next_blink) >= 0) {
        eyes->blink_start = now;
        if (!eyes->blinks_left)
            eyes->blinks_left = next_random(eyes) % 6 == 0 ? 2 : 1;
    }
    if (eyes->blink_start) {
        float t = (float)(now - eyes->blink_start) / BLINK_MS;
        if (t >= 1.0f) {
            eyes->blink_start = 0;
            eyes->blinks_left--;
            eyes->next_blink = now + (eyes->blinks_left > 0 ? 90 : random_between(eyes, 3200, 7600));
        } else {
            /* Lids fall quickly, rest a moment, and lift more slowly. */
            *open = t < 0.32f ? 1.0f - smooth(t / 0.32f) : t < 0.45f ? 0.0f : smooth((t - 0.45f) / 0.55f);
            moving = true;
        }
    }
    *look = 0.0f;
    if (!eyes->glance_start && (Sint32)(now - eyes->next_glance) >= 0) {
        eyes->glance_start = now;
        eyes->glance = next_random(eyes) % 2 ? 1.0f : -1.0f;
    }
    if (eyes->glance_start) {
        Uint32 elapsed = now - eyes->glance_start;
        if (elapsed >= GLANCE_MS) {
            eyes->glance_start = 0;
            eyes->next_glance = now + random_between(eyes, 14000, 26000);
        } else {
            float in = smooth((float)elapsed / GLANCE_MOVE_MS);
            float out = smooth((float)(GLANCE_MS - elapsed) / GLANCE_MOVE_MS);
            *look = eyes->glance * SDL_min(in, out);
            moving = true;
        }
    }
    return moving;
}

/* The colours a cell is drawn in, after bold, dim and reverse. */
static void cell_colors(struct cell cell, struct rgb *fg, struct rgb *bg, bool *has_bg)
{
    int fg_index = cell.fg;
    if ((cell.attr & ATTR_BOLD) && fg_index >= 2 && fg_index <= 8) /* black stays black */
        fg_index += 8;
    *fg = palette_fg[fg_index];
    *has_bg = cell.bg != 0;
    *bg = palette_bg[cell.bg];
    if (cell.attr & ATTR_REVERSE) {
        struct rgb swapped = *has_bg ? *bg : canvas_ink;
        *bg = *fg;
        *fg = swapped;
        *has_bg = true;
    }
    /* Default text on a light panel turns dark so it stays readable. */
    if (*has_bg && !(cell.attr & ATTR_REVERSE) && cell.fg == 0 && bg->r + 2 * bg->g + bg->b > 520)
        *fg = canvas_ink;
    if (cell.attr & ATTR_DIM)
        *fg = mix(*fg, *has_bg ? *bg : canvas_mean, 0.45f);
}

static bool same_fill(struct terminal *terminal, size_t line, int column, struct rgb color)
{
    struct rgb fg, bg;
    bool has_bg;
    cell_colors(line_at(terminal, line)[column], &fg, &bg, &has_bg);
    return has_bg && bg.r == color.r && bg.g == color.g && bg.b == color.b;
}

static void draw_row(const struct canvas *canvas, struct terminal *terminal, const struct layout *geometry, int row,
                     size_t view_top)
{
    size_t line = view_top + (size_t)row;
    const struct cell *cells = line_at(terminal, line);
    int cw = geometry->cell_width, ch = geometry->cell_height;
    int y = geometry->y0 + row * ch;
    bool above = row > 0, below = row + 1 < geometry->rows;
    /* Runs of one background colour become one panel, rounded where nothing continues it. */
    for (int column = 0; column < geometry->columns;) {
        struct rgb fg, bg;
        bool has_bg;
        cell_colors(cells[column], &fg, &bg, &has_bg);
        if (!has_bg) {
            column++;
            continue;
        }
        int end = column + 1;
        while (end < geometry->columns && same_fill(terminal, line, end, bg))
            end++;
        float r = (float)ch / 5.0f;
        float radius[4] = {
            above && same_fill(terminal, line - 1, column, bg) ? 0 : r,
            above && same_fill(terminal, line - 1, end - 1, bg) ? 0 : r,
            below && same_fill(terminal, line + 1, end - 1, bg) ? 0 : r,
            below && same_fill(terminal, line + 1, column, bg) ? 0 : r,
        };
        fill_rounded(canvas, geometry->x0 + column * cw, y, (end - column) * cw, ch, radius, bg);
        column = end;
    }
    for (int column = 0; column < geometry->columns; ++column) {
        struct cell cell = cells[column];
        if (!cell.glyph)
            continue;
        struct rgb fg, bg;
        bool has_bg;
        cell_colors(cell, &fg, &bg, &has_bg);
        draw_glyph(canvas, geometry->face, cell.attr & ATTR_BOLD ? WEIGHT_BOLD : WEIGHT_REGULAR, cell.glyph,
                   geometry->x0 + column * cw, y, fg);
    }
}

struct art {
    uint8_t *backdrop;
    int width, height, face;
};

static bool prepare_backdrop(struct art *art, const struct layout *geometry, int pitch)
{
    if (art->backdrop && art->width == geometry->width && art->height == geometry->height && art->face == geometry->face)
        return true;
    free(art->backdrop);
    art->backdrop = malloc((size_t)pitch * (size_t)geometry->height);
    if (!art->backdrop)
        return false;
    art->width = geometry->width;
    art->height = geometry->height;
    art->face = geometry->face;
    struct canvas canvas = { art->backdrop, pitch, geometry->width, geometry->height };
    paint_backdrop(&canvas, geometry->face, geometry->header, geometry->footer, geometry->pad_x);
    return true;
}

/* Draws the whole screen, or with header_only just the band with the eyes. */
static bool draw_screen(struct display *display, struct art *art, struct terminal *terminal,
                        const struct layout *geometry, bool visible, bool panel_controls, float open, float look,
                        bool header_only)
{
    int first_row = 0, last_row = geometry->height;
    if (visible) {
        if (!resize_surface(display, geometry->width, geometry->height))
            return false;
        SDL_Surface *surface = display->surface;
        if (!prepare_backdrop(art, geometry, surface->pitch))
            return false;
        struct canvas canvas = { surface->pixels, surface->pitch, surface->w, surface->h };
        int rows = header_only ? SDL_min(geometry->header, geometry->height) : geometry->height;
        memcpy(surface->pixels, art->backdrop, (size_t)surface->pitch * (size_t)rows);
        draw_header_eyes(&canvas, geometry->header, geometry->pad_x, open, look);
        if (header_only) {
            last_row = rows;
        } else {
            int scroll = SDL_min(terminal->scroll, max_scroll(terminal));
            size_t view_top = terminal->top - (size_t)scroll;
            for (int row = 0; row < geometry->rows; ++row)
                draw_row(&canvas, terminal, geometry, row, view_top);
            if (!scroll && !terminal->cursor_hidden && terminal->row < geometry->rows) {
                int band = console_faces[geometry->face].cursor;
                int column = SDL_min(terminal->column, geometry->columns - 1);
                fill_rect(&canvas, geometry->x0 + column * geometry->cell_width + 1,
                          geometry->y0 + (terminal->row + 1) * geometry->cell_height - band,
                          geometry->cell_width - 2, band, accent);
            }
            draw_footer(&canvas, geometry->face, geometry->footer, geometry->pad_x, panel_controls, scroll > 0);
        }
    }
    present_frame(display, geometry->width, geometry->height, visible, first_row, last_row);
    return true;
}

static bool write_snapshot(const struct display *display, const char *path)
{
    const SDL_Surface *surface = display->surface;
    FILE *file = surface ? fopen(path, "wb") : NULL;
    if (!file)
        return false;
    bool written = fprintf(file, "P6\n%d %d\n255\n", surface->w, surface->h) > 0;
    for (int y = 0; y < surface->h; ++y) {
        const uint8_t *pixel = (const uint8_t *)surface->pixels + (size_t)y * (size_t)surface->pitch;
        for (int x = 0; x < surface->w; ++x, pixel += 4)
            written &= fwrite(pixel, 1, 3, file) == 3;
    }
    return (fclose(file) == 0) & written;
}

/* -- the screen gate and keys ---------------------------------------------- */

static bool show_sequence(int directory, unsigned long long *sequence)
{
    char text[32];
    int fd = openat(directory, "show", O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0)
        return false;
    ssize_t size = read(fd, text, sizeof(text) - 1);
    close(fd);
    if (size <= 0 || size >= (ssize_t)sizeof(text) - 1 || text[0] < '0' || text[0] > '9')
        return false;
    text[size] = '\0';
    char *end;
    errno = 0;
    unsigned long long value = strtoull(text, &end, 10);
    if (errno || (*end && strcmp(end, "\n")))
        return false;
    *sequence = value;
    return true;
}

static bool publish_frame(int directory, unsigned long long sequence)
{
    char text[32];
    int length = snprintf(text, sizeof(text), "%llu\n", sequence);
    int fd = openat(directory, "presented.tmp", O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0)
        return false;
    ssize_t count = write(fd, text, (size_t)length);
    bool closed = close(fd) == 0;
    return count == length && closed && renameat(directory, "presented.tmp", directory, "presented") == 0;
}

static void request_panel_action(int directory, const char *name)
{
    int fd = openat(directory, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd >= 0)
        close(fd);
}

static char key_character(SDL_Keysym key)
{
    SDL_Keycode code = key.sym;
    if (code == SDLK_RETURN || code == SDLK_KP_ENTER)
        return '\r';
    if (code == SDLK_BACKSPACE)
        return '\177';
    if (code >= SDLK_KP_1 && code <= SDLK_KP_9)
        return (char)('1' + code - SDLK_KP_1);
    if (code == SDLK_KP_0)
        return '0';
    if (code < ' ' || code > '~')
        return 0;
    bool shift = (key.mod & KMOD_SHIFT) != 0;
    if (code >= 'a' && code <= 'z')
        return (char)(shift != ((key.mod & KMOD_CAPS) != 0) ? code - 'a' + 'A' : code);
    const char plain[] = "`1234567890-=[]\\;',./";
    const char shifted[] = "~!@#$%^&*()_+{}|:\"<>?";
    const char *position = strchr(plain, (int)code);
    return shift && position ? shifted[position - plain] : (char)code;
}

static bool handle_key(SDL_KeyboardEvent event, struct session *session, struct terminal *terminal,
                       const struct layout *geometry, bool visible, bool kms, int gate)
{
    SDL_Keysym key = event.keysym;
    if (gate >= 0 && !event.repeat && (key.sym == SDLK_F9 || key.sym == SDLK_F10)) {
        request_panel_action(gate, key.sym == SDLK_F9 ? "recover" : "mute");
        return false;
    }
    if (!visible || (key.mod & (KMOD_CTRL | KMOD_ALT | KMOD_GUI)))
        return false;
    if (key.sym == SDLK_PAGEUP || key.sym == SDLK_PAGEDOWN) {
        int step = SDL_max(geometry->rows - 1, 1);
        int position = terminal->scroll + (key.sym == SDLK_PAGEUP ? step : -step);
        terminal->scroll = SDL_clamp(position, 0, max_scroll(terminal));
        return true;
    }
    if (kms || key.sym == SDLK_RETURN || key.sym == SDLK_KP_ENTER || key.sym == SDLK_BACKSPACE) {
        char character = key_character(key);
        if (character) {
            enqueue(session, terminal, &character, 1);
            return true;
        }
    }
    return false;
}

/* A brainstem that died after powering the panel down leaves it dark. Set every
 * backlight's bl_power to FB_BLANK_UNBLANK (the brightness is left alone); a
 * live brainstem re-asserts its own choice on its next tick. */
static void unblank_backlight(void)
{
#ifdef __linux__
    DIR *directory = opendir("/sys/class/backlight");
    struct dirent *entry;
    while (directory && (entry = readdir(directory))) {
        if (entry->d_name[0] == '.')
            continue;
        char path[300], value[8] = "";
        snprintf(path, sizeof(path), "/sys/class/backlight/%s/bl_power", entry->d_name);
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0)
            continue;
        ssize_t size = read(fd, value, sizeof(value) - 1);
        close(fd);
        if (size > 0 && value[0] == '0')
            continue;
        fd = open(path, O_WRONLY | O_CLOEXEC);
        bool done = fd >= 0 && write(fd, "0", 1) == 1;
        if (fd >= 0)
            close(fd);
        fprintf(stderr, "being-console: %s %s\n", done ? "powered the panel back up through" :
                "could not power the panel up through", path);
    }
    if (directory)
        closedir(directory);
#endif
}

static bool run_console(struct display *display, struct session *session, int gate, bool kms,
                        const struct options *options)
{
    struct terminal *terminal = &console_terminal;
    struct art art = {0};
    struct eyes eyes;
    Uint32 started = SDL_GetTicks();
    start_eyes(&eyes, started);
    struct layout geometry = measure(display);
    resize_terminal(terminal, geometry.rows, geometry.columns);
    size_pty(session->master, &geometry);
    bool quitting = false, dirty = true, was_visible = false, acknowledged = false, eyes_moving = false;
    bool fed = options->feed == NULL, ok = true;
    // The first KMS swap can change the EGL surface, discarding the drawn buffer.
    const int settle_frames = kms ? 3 : 1;
    int frames_left = settle_frames;
    unsigned long long sequence = 0;
    /* With no lid reading at all, ask for the panel and, if nobody even takes
     * the request (no brainstem behind mind/local_screen.py), draw anyway. */
    bool ever_shown = false, forced = false, was_shown = false;
    Uint32 asked_at = 0;
    while (session->running && !stopping && !quitting) {
        Uint32 now = SDL_GetTicks();
        if (options->exit_after > 0 && now - started >= (Uint32)(options->exit_after * 1000.0))
            break;
        unsigned long long next_sequence = 0;
        bool shown = gate >= 0 && show_sequence(gate, &next_sequence);
        if (shown) {
            ever_shown = true;
            forced = false;
        } else if (forced && faccessat(gate, "recover", F_OK, 0) != 0) {
            /* The request was taken after all (mind/local_screen.py started
             * late): a brainstem is there, so it decides. A closed lid now
             * keeps the screen dark; an unknown one is shown in a moment. */
            fprintf(stderr, "being-console: the brainstem is up; it decides the screen again\n");
            forced = false;
            asked_at = now ? now : 1;
        } else if (gate >= 0 && !ever_shown && !forced && now - started >= LID_GRACE_MS) {
            if (!asked_at) {
                request_panel_action(gate, "recover");
                asked_at = now ? now : 1;
            } else if (now - asked_at >= RECOVER_WAIT_MS) {
                if (faccessat(gate, "recover", F_OK, 0) == 0) {
                    fprintf(stderr, "being-console: no lid reading and no brainstem; drawing the screen anyway\n");
                    forced = true;
                    if (kms)
                        unblank_backlight();
                } else {
                    asked_at = 0; /* taken but unanswered: the lid is closed, or ask again */
                }
            }
        }
        bool visible = gate < 0 || shown || forced;
        if (visible != was_visible || next_sequence != sequence || shown != was_shown) {
            dirty = true;
            acknowledged = false;
            frames_left = settle_frames;
        }
        sequence = next_sequence;
        was_visible = visible;
        was_shown = shown;

        SDL_Event event;
        while (display->window && SDL_PollEvent(&event)) {
            if (event.type == SDL_QUIT) {
                quitting = true;
            } else if (event.type == SDL_WINDOWEVENT) {
                struct layout next = measure(display);
                if (next.columns != geometry.columns || next.rows != geometry.rows) {
                    resize_terminal(terminal, next.rows, next.columns);
                    size_pty(session->master, &next);
                }
                if (next.width != geometry.width || next.height != geometry.height)
                    frames_left = settle_frames;
                geometry = next;
                dirty = true;
            } else if (event.type == SDL_KEYDOWN) {
                dirty |= handle_key(event.key, session, terminal, &geometry, visible, kms, gate);
            } else if (event.type == SDL_TEXTINPUT && visible && !kms) {
                dirty |= enqueue_text(session, terminal, event.text.text);
            }
        }
        if (!fed && now - started >= 500) {
            enqueue(session, terminal, options->feed, strlen(options->feed));
            fed = true;
        }
        if (visible && !quitting)
            send_input(session);
        else
            session->input_size = 0;
        dirty |= read_output(session, terminal);
        float open = 1.0f, look = 0.0f;
        bool was_moving = eyes_moving;
        eyes_moving = visible && animate_eyes(&eyes, now, &open, &look);
        bool header_due = eyes_moving || was_moving;
        /* Only a frame drawn for a lid reading is reported as presented. */
        bool unacknowledged = gate >= 0 && shown && !acknowledged;
        if (geometry.width > 0 && geometry.height > 0 &&
            (dirty || frames_left > 0 || unacknowledged || header_due)) {
            bool full = dirty || frames_left > 0 || unacknowledged;
            if (!draw_screen(display, &art, terminal, &geometry, visible, gate >= 0, open, look, !full)) {
                SDL_LogError(SDL_LOG_CATEGORY_APPLICATION, "Console frame: %s", SDL_GetError());
                ok = false;
                break;
            }
            if (full) {
                dirty = false;
                if (frames_left > 0)
                    frames_left--;
                if (unacknowledged && frames_left == 0)
                    acknowledged = publish_frame(gate, sequence);
            }
        }
        reap(session);
        if (session->running && !quitting)
            SDL_Delay(eyes_moving ? 16 : 20);
    }
    if (ok && options->snapshot) {
        while (read_output(session, terminal))
            ;
        float open = 1.0f, look = 0.0f;
        animate_eyes(&eyes, SDL_GetTicks(), &open, &look);
        if (!draw_screen(display, &art, terminal, &geometry, true, gate >= 0, open, look, false) ||
            !write_snapshot(display, options->snapshot)) {
            fprintf(stderr, "being-console: could not write %s\n", options->snapshot);
            ok = false;
        }
    }
    free(art.backdrop);
    return ok;
}

/* -- panel mode -------------------------------------------------------------- */

/* `being-console --panel NAME`: renders one app's bytes, read from --io, into
 * the frame file the face composites (being-console-panel.h). No SDL video,
 * no pty of its own, no keyboard: hands holds the pty and decides who types.
 * The KMS console above is not used on this path at all. */
struct panel_options {
    const char *name, *directory, *io, *user, *title;
    int width, height;
    bool hidden;
};

struct panel {
    struct layout geometry;
    struct canvas canvas;
    uint8_t *backdrop;
    uint64_t hashes[MAX_ROWS], fills[MAX_ROWS];
    bool hashes_valid;
    char chrome[PANEL_TITLE + PANEL_STATUS + 16];
    struct panel_control control;
    struct panel_header header;
    struct panel_file file;
    const char *title;
};

static int panel_signals[2] = { -1, -1 };

static void panel_signal(int signal_number)
{
    int saved = errno;
    unsigned char byte = (unsigned char)signal_number;
    ssize_t ignored = write(panel_signals[1], &byte, 1);
    (void)ignored;
    errno = saved;
}

static uint64_t panel_now(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint64_t)now.tv_sec * 1000u + (uint64_t)now.tv_nsec / 1000000u;
}

static struct layout panel_layout(int width, int height)
{
    struct layout geometry = { .width = width, .height = height };
    geometry.face = width >= 1480 && height >= 900 ? FACE_LARGE : width >= 750 && height >= 400 ? FACE_MEDIUM : FACE_SMALL;
    geometry.cell_width = console_faces[geometry.face].width;
    geometry.cell_height = console_faces[geometry.face].height;
    geometry.header = PANEL_STATUS_BAR;
    geometry.footer = PANEL_FOOTER;
    /* 1280 wide in the medium face: 107 columns, as voice-apps.md 3.2 plans. */
    geometry.pad_x = geometry.face == FACE_SMALL ? 16 : geometry.face == FACE_MEDIUM ? 50 : 64;
    geometry.columns = SDL_clamp((width - 2 * geometry.pad_x) / geometry.cell_width, 1, COLUMNS);
    geometry.rows = SDL_clamp((height - geometry.header - geometry.footer - PANEL_PAD_TOP - PANEL_PAD_BOTTOM) /
                                  geometry.cell_height, 1, MAX_ROWS);
    geometry.x0 = geometry.pad_x;
    geometry.y0 = geometry.header + PANEL_PAD_TOP;
    return geometry;
}

static uint64_t panel_row_hash(struct terminal *terminal, const struct layout *geometry, size_t line, int cursor)
{
    const uint8_t *bytes = (const uint8_t *)line_at(terminal, line);
    uint64_t hash = 1469598103934665603u;
    for (size_t i = 0; i < (size_t)geometry->columns * sizeof(struct cell); ++i)
        hash = (hash ^ bytes[i]) * 1099511628211u;
    return (hash ^ (uint64_t)(cursor + 2)) * 1099511628211u;
}

/* The row's background runs only: what its neighbours' rounded corners depend on. */
static uint64_t panel_fill_hash(struct terminal *terminal, const struct layout *geometry, size_t line)
{
    const struct cell *cells = line_at(terminal, line);
    uint64_t hash = 1469598103934665603u;
    for (int column = 0; column < geometry->columns; ++column) {
        struct rgb fg, bg;
        bool has_bg;
        cell_colors(cells[column], &fg, &bg, &has_bg);
        uint32_t value = has_bg ? 0x1000000u | (uint32_t)bg.r << 16 | (uint32_t)bg.g << 8 | bg.b : 0;
        hash = (hash ^ value) * 1099511628211u;
    }
    return hash;
}

static int panel_scroll(struct terminal *terminal)
{
    return SDL_min(terminal->scroll, max_scroll(terminal));
}

/* Redraws what changed since the last frame; returns the pixel rows touched
 * as [*first, *last], or *last < *first when nothing changed. */
static void panel_draw(struct panel *panel, struct terminal *terminal, bool full, int *first, int *last)
{
    const struct layout *geometry = &panel->geometry;
    const struct canvas *canvas = &panel->canvas;
    size_t pitch = (size_t)canvas->pitch;
    int low = canvas->height, high = -1;
    int scroll = panel_scroll(terminal);
    const char *title = panel->control.title[0] ? panel->control.title : panel->title;
    struct rgb dot;
    char chrome[sizeof(panel->chrome)];
    snprintf(chrome, sizeof(chrome), "%s\n%s\n%d%d%d%d", title, panel_status(&panel->control, &dot),
             panel->control.pip_typing, panel->control.pip_paused, panel->control.job_busy, scroll > 0);
    if (full) {
        memcpy(canvas->pixels, panel->backdrop, pitch * (size_t)canvas->height);
        panel->hashes_valid = false;
        low = 0;
        high = canvas->height - 1;
    }
    if (full || strcmp(chrome, panel->chrome) != 0) {
        draw_panel_status(canvas, geometry->face, geometry->pad_x, title, &panel->control);
        draw_panel_footer(canvas, geometry->face, geometry->pad_x, scroll > 0);
        memcpy(panel->chrome, chrome, sizeof(chrome));
        low = 0;
        high = canvas->height - 1;
    }
    size_t view_top = terminal->top - (size_t)scroll;
    bool cursor = !scroll && !terminal->cursor_hidden && terminal->row < geometry->rows;
    int cursor_column = SDL_min(terminal->column, geometry->columns - 1);
    bool changed[MAX_ROWS] = { false }, refilled[MAX_ROWS + 2] = { false };
    for (int row = 0; row < geometry->rows; ++row) {
        size_t line = view_top + (size_t)row;
        uint64_t hash = panel_row_hash(terminal, geometry, line, cursor && row == terminal->row ? cursor_column : -1);
        uint64_t fill = panel_fill_hash(terminal, geometry, line);
        if (!panel->hashes_valid || hash != panel->hashes[row]) {
            changed[row] = true;
            panel->hashes[row] = hash;
        }
        if (!panel->hashes_valid || fill != panel->fills[row]) {
            refilled[row + 1] = true;
            panel->fills[row] = fill;
        }
    }
    panel->hashes_valid = true;
    int cw = geometry->cell_width, ch = geometry->cell_height;
    for (int row = 0; row < geometry->rows; ++row) {
        /* A neighbour's new background runs can round or square this row's corners. */
        if (!changed[row] && !refilled[row] && !refilled[row + 2])
            continue;
        int y = geometry->y0 + row * ch;
        if (y + ch > canvas->height - geometry->footer)
            break;
        memcpy(canvas->pixels + (size_t)y * pitch, panel->backdrop + (size_t)y * pitch, pitch * (size_t)ch);
        draw_row(canvas, terminal, geometry, row, view_top);
        if (cursor && row == terminal->row) {
            int band = console_faces[geometry->face].cursor;
            fill_rect(canvas, geometry->x0 + cursor_column * cw + 1, y + ch - band, cw - 2, band, accent);
        }
        low = SDL_min(low, y);
        high = SDL_max(high, y + ch - 1);
    }
    *first = low;
    *last = high;
}

static void panel_fill_header(struct panel *panel, struct terminal *terminal, bool visible)
{
    const struct layout *geometry = &panel->geometry;
    struct panel_header *header = &panel->header;
    int scroll = panel_scroll(terminal);
    header->flags = panel_flags(&panel->control, visible, scroll > 0);
    bool cursor = !scroll && !terminal->cursor_hidden && terminal->row < geometry->rows;
    header->cursor_x = cursor ? geometry->x0 + SDL_min(terminal->column, geometry->columns - 1) * geometry->cell_width
                              : -1;
    header->cursor_y = cursor ? geometry->y0 + terminal->row * geometry->cell_height : -1;
    header->newest_y = -1;
    for (int row = SDL_min(terminal->row, geometry->rows - 1); !scroll && row >= 0; --row)
        if (row_used(terminal, row)) {
            header->newest_y = geometry->y0 + row * geometry->cell_height;
            break;
        }
    const char *title = panel->control.title[0] ? panel->control.title : panel->title;
    memset(header->title, 0, sizeof(header->title));
    panel_copy_text(header->title, sizeof(header->title), title, strlen(title));
}

static bool panel_name_valid(const char *name)
{
    size_t length = strlen(name);
    if (length < 1 || length > 24 || name[0] < 'a' || name[0] > 'z')
        return false;
    for (size_t i = 1; i < length; ++i)
        if (!((name[i] >= 'a' && name[i] <= 'z') || (name[i] >= '0' && name[i] <= '9') || name[i] == '_' ||
              name[i] == '-'))
            return false;
    return true;
}

/* --io: a file descriptor number, "-" for stdin, a Unix stream socket to
 * connect to, or a FIFO to open. */
static int panel_open_io(const char *io)
{
    if (strcmp(io, "-") == 0)
        return STDIN_FILENO;
    char *end;
    errno = 0;
    long number = strtol(io, &end, 10);
    if (end != io && !*end && !errno && number >= 0 && number < 65536)
        return fcntl((int)number, F_GETFD) < 0 ? -1 : (int)number;
    struct stat status;
    if (stat(io, &status) == 0 && S_ISSOCK(status.st_mode)) {
        struct sockaddr_un address = { .sun_family = AF_UNIX };
        if (strlen(io) >= sizeof(address.sun_path))
            return -1;
        strcpy(address.sun_path, io);
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd >= 0 && connect(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
            close(fd);
            fd = -1;
        }
        return fd;
    }
    return open(io, O_RDONLY | O_NOCTTY | O_CLOEXEC);
}

/* The renderer parses bytes that caged programs wrote, so it never runs as
 * root: started as root, it becomes the panel user or refuses to run. */
static bool panel_drop_root(const char *user)
{
    if (geteuid() != 0 && getuid() != 0)
        return true;
    struct passwd *account = getpwnam(user);
    if (!account || account->pw_uid == 0) {
        fprintf(stderr, "being-console: no user %s; panel mode will not run as root\n", user);
        return false;
    }
    uid_t uid = account->pw_uid;
    gid_t gid = account->pw_gid;
    if (setgroups(0, NULL) != 0 || setgid(gid) != 0 || setuid(uid) != 0 || setuid(0) == 0 || geteuid() == 0) {
        fprintf(stderr, "being-console: could not become %s: %s\n", user, strerror(errno));
        return false;
    }
#ifdef __linux__
    prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
#endif
    return true;
}

static int run_panel(const struct panel_options *options)
{
    static struct panel panel;
    struct terminal *terminal = &console_terminal;
    terminal->control = &panel.control;
    panel.file.fd = -1;
    panel.file.directory = -1;
    panel.title = options->title;
    panel.geometry = panel_layout(options->width, options->height);
    const struct layout *geometry = &panel.geometry;
    resize_terminal(terminal, geometry->rows, geometry->columns);

    int result = 1;
    int input = panel_open_io(options->io);
    if (input < 0) {
        fprintf(stderr, "being-console: cannot read the panel input %s\n", options->io);
        return 1;
    }
    panel.file.directory = open(options->directory, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (panel.file.directory < 0) {
        fprintf(stderr, "being-console: panel directory %s: %s\n", options->directory, strerror(errno));
        return 1;
    }
    if (!panel_drop_root(options->user))
        return 1;
    snprintf(panel.file.name, sizeof(panel.file.name), "%s.frame", options->name);
    snprintf(panel.file.temporary, sizeof(panel.file.temporary), "%s.frame.tmp", options->name);
    fcntl(input, F_SETFL, fcntl(input, F_GETFL) | O_NONBLOCK);
    if (isatty(input)) {
        /* A pty master handed over directly (lab previews): size it here. */
        struct winsize size = { .ws_row = (unsigned short)geometry->rows, .ws_col = (unsigned short)geometry->columns };
        ioctl(input, TIOCSWINSZ, &size);
    }
    if (!load_glyphs()) {
        fprintf(stderr, "being-console: out of memory\n");
        return 1;
    }
    if (pipe(panel_signals) != 0) {
        perror("being-console signal pipe");
        return 1;
    }
    for (int i = 0; i < 2; ++i) {
        fcntl(panel_signals[i], F_SETFL, O_NONBLOCK);
        fcntl(panel_signals[i], F_SETFD, FD_CLOEXEC);
    }
    struct sigaction action = { .sa_handler = panel_signal };
    sigemptyset(&action.sa_mask);
    const int handled[] = { SIGUSR1, SIGUSR2, SIGTERM, SIGINT, SIGHUP };
    for (size_t i = 0; i < sizeof(handled) / sizeof(handled[0]); ++i)
        sigaction(handled[i], &action, NULL);
    signal(SIGPIPE, SIG_IGN);

    size_t pitch = (size_t)options->width * 4, bytes = pitch * (size_t)options->height;
    panel.canvas = (struct canvas){ malloc(bytes), (int)pitch, options->width, options->height };
    panel.backdrop = malloc(bytes);
    if (!panel.canvas.pixels || !panel.backdrop) {
        fprintf(stderr, "being-console: out of memory\n");
        goto done;
    }
    struct canvas backdrop = { panel.backdrop, (int)pitch, options->width, options->height };
    paint_panel_backdrop(&backdrop);

    struct panel_header *header = &panel.header;
    *header = (struct panel_header){
        .width = (uint32_t)options->width, .height = (uint32_t)options->height, .stride = (uint32_t)pitch,
        .columns = (uint32_t)geometry->columns, .rows = (uint32_t)geometry->rows,
        .cell_width = (uint16_t)geometry->cell_width, .cell_height = (uint16_t)geometry->cell_height,
        .pid = (uint32_t)getpid(),
    };
    bool visible = !options->hidden;
    int first, last;
    panel_draw(&panel, terminal, true, &first, &last);
    panel_fill_header(&panel, terminal, visible);
    if (!panel_create(&panel.file, header, panel.canvas.pixels)) {
        fprintf(stderr, "being-console: cannot write %s/%s: %s\n", options->directory, panel.file.name,
                strerror(errno));
        goto done;
    }
    fprintf(stderr, "being-console: panel %s %dx%d, %d columns by %d rows\n", options->name, options->width,
            options->height, geometry->columns, geometry->rows);

    uint64_t now = panel_now(), next_frame = now, next_beat = now + PANEL_BEAT_MS;
    uint32_t written_flags = header->flags;
    bool pending = false, full_due = false, header_due = false, quitting = false;
    result = 0;
    while (!quitting) {
        int timeout = -1; /* hidden: sleep until there is input or a signal */
        if (visible) {
            uint64_t wake = pending || full_due ? SDL_min(next_beat, next_frame) : next_beat;
            timeout = wake > now ? (int)(wake - now) : 0;
        }
        struct pollfd fds[2] = { { .fd = input, .events = POLLIN }, { .fd = panel_signals[0], .events = POLLIN } };
        int ready = poll(fds, 2, timeout);
        if (ready < 0 && errno != EINTR) {
            perror("being-console poll");
            result = 1;
            break;
        }
        now = panel_now();
        if (ready > 0 && fds[1].revents) {
            unsigned char signals[16];
            ssize_t count = read(panel_signals[0], signals, sizeof(signals));
            for (ssize_t i = 0; i < count; ++i) {
                if (signals[i] == SIGUSR1 && !visible) {
                    visible = true;
                    full_due = true;
                    next_beat = now; /* a fresh beat with the first frame back */
                } else if (signals[i] == SIGUSR2 && visible) {
                    visible = false;
                    header_due = true;
                } else if (signals[i] != SIGUSR1 && signals[i] != SIGUSR2) {
                    quitting = true;
                }
            }
        }
        if (ready > 0 && fds[0].revents && !quitting) {
            unsigned char chunk[16384];
            ssize_t count = read(input, chunk, sizeof(chunk));
            if (count > 0) {
                for (ssize_t i = 0; i < count; ++i)
                    put_byte(terminal, chunk[i]);
                pending = true;
            } else if (count == 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
                break; /* the session is over (EIO from a pty whose shell exited) */
            }
        }
        /* Forgetting does not wait for the frame cap or for the panel to be
         * shown: the old pixels leave the frame file at once, hidden or not. */
        bool wipe_now = panel.control.wiped;
        panel.control.wiped = false;
        if (panel.control.scroll_request) {
            int step = SDL_max(geometry->rows - 1, 1), request = panel.control.scroll_request;
            int position = request == -2 ? 0 : terminal->scroll + request * step;
            terminal->scroll = SDL_clamp(position, 0, max_scroll(terminal));
            panel.control.scroll_request = 0;
            pending = true;
        }
        if (panel.control.changed) {
            panel.control.changed = false;
            pending = true;
            if (!visible)
                header_due = true; /* a new title reaches the header while minimised */
        }
        if (panel_flags(&panel.control, visible, panel_scroll(terminal) > 0) != written_flags)
            header_due = true;
        bool frame_waiting = visible && (pending || full_due);
        bool beat_due = visible && now >= next_beat;
        bool wrote = false;
        if (wipe_now) {
            panel_draw(&panel, terminal, true, &first, &last);
            panel_fill_header(&panel, terminal, visible);
            wrote = panel_write_frame(&panel.file, header, panel.canvas.pixels, 0, header->height);
            /* While hidden the pixels are now the wiped screen; SIGUSR1 still
             * redraws in full when it shows the panel. */
            pending = full_due = header_due = false;
            if (visible)
                next_frame = now + PANEL_FRAME_MS;
        } else if (frame_waiting && now >= next_frame) {
            panel_draw(&panel, terminal, full_due, &first, &last);
            if (beat_due)
                header->beat++;
            panel_fill_header(&panel, terminal, visible);
            wrote = panel_write_frame(&panel.file, header, panel.canvas.pixels, last >= first ? (uint32_t)first : 0,
                                      last >= first ? (uint32_t)(last - first + 1) : 0);
            pending = full_due = header_due = false;
            next_frame = now + PANEL_FRAME_MS;
            if (beat_due)
                next_beat = now + PANEL_BEAT_MS;
        } else if (beat_due || (header_due && !frame_waiting)) {
            if (beat_due) {
                header->beat++;
                next_beat = now + PANEL_BEAT_MS;
            }
            panel_fill_header(&panel, terminal, visible);
            wrote = panel_write_frame(&panel.file, header, panel.canvas.pixels, 0, 0);
            header_due = false;
        } else {
            continue;
        }
        if (!wrote) {
            fprintf(stderr, "being-console: cannot write the panel frame: %s\n", strerror(errno));
            result = 1;
            break;
        }
        written_flags = header->flags;
    }

done:
    panel_remove(&panel.file);
    if (panel.file.directory >= 0)
        close(panel.file.directory);
    free(panel.canvas.pixels);
    free(panel.backdrop);
    terminal->control = NULL;
    return result;
}

static int panel_usage(const char *name)
{
    fprintf(stderr, "usage: %s --panel NAME [--size WIDTHxHEIGHT] [--io FD|-|PATH] [--dir DIRECTORY]\n"
            "       [--title TEXT] [--user USER] [--hidden]\n", name);
    return 2;
}

static int panel_main(int argc, char **argv)
{
    struct panel_options options = {
        .name = argv[2], .io = "-", .user = "being-panel", .width = 1280, .height = 640,
        .directory = getenv("BEING_PANEL_DIR") ? getenv("BEING_PANEL_DIR") : "/run/being-panel",
    };
    if (!panel_name_valid(options.name))
        return panel_usage(argv[0]);
    for (int i = 3; i < argc; ++i) {
        const char *option = argv[i];
        if (strcmp(option, "--hidden") == 0) {
            options.hidden = true;
            continue;
        }
        if (i + 1 >= argc)
            return panel_usage(argv[0]);
        const char *value = argv[++i];
        if (strcmp(option, "--size") == 0) {
            if (sscanf(value, "%dx%d", &options.width, &options.height) != 2 || options.width < 160 ||
                options.width > 4096 || options.height < 120 || options.height > 4096)
                return panel_usage(argv[0]);
        } else if (strcmp(option, "--io") == 0) {
            options.io = value;
        } else if (strcmp(option, "--dir") == 0) {
            options.directory = value;
        } else if (strcmp(option, "--title") == 0) {
            options.title = value;
        } else if (strcmp(option, "--user") == 0) {
            options.user = value;
        } else {
            return panel_usage(argv[0]);
        }
    }
    if (!options.title)
        options.title = strcmp(options.name, "terminal") == 0 ? "Terminal" :
                        strcmp(options.name, "monitor") == 0 ? "Body monitor" :
                        strcmp(options.name, "clock") == 0 ? "Clock" : options.name;
    return run_panel(&options);
}

#ifdef __linux__
#include <dirent.h>
#include <linux/input.h>

/* Returns -1 for a boot that is not verbose, 0 to wait for a key, else seconds. */
static int verbose_hold(const char *cmdline)
{
    char copy[4096];
    snprintf(copy, sizeof(copy), "%s", cmdline);
    bool verbose = false;
    int hold = VERBOSE_HOLD_S;
    char *saved = NULL;
    for (char *word = strtok_r(copy, " \t\n", &saved); word; word = strtok_r(NULL, " \t\n", &saved)) {
        if (strcmp(word, "being.verbose=1") == 0) {
            verbose = true;
        } else if (strncmp(word, "being.verbose=", 14) == 0) {
            verbose = false;
        } else if (strncmp(word, "being.verbose_hold=", 19) == 0) {
            char *end;
            long value = strtol(word + 19, &end, 10);
            if (end != word + 19 && !*end && value >= 0)
                hold = value > 3600 ? 3600 : (int)value;
        }
    }
    return verbose ? hold : -1;
}

/* Leave the boot messages up on a verbose boot until the hold is over or a key
 * (Esc, F2, Enter, Space) is pressed, once per boot. Reads the keyboards
 * directly: nothing has opened the display yet. */
static void hold_for_boot_messages(void)
{
    char cmdline[4096] = "";
    int fd = open("/proc/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return;
    ssize_t size = read(fd, cmdline, sizeof(cmdline) - 1);
    close(fd);
    if (size <= 0)
        return;
    cmdline[size] = '\0';
    int hold = verbose_hold(cmdline);
    if (hold < 0 || access(VERBOSE_RELEASED, F_OK) == 0)
        return;
    char note[200];
    if (hold > 0)
        snprintf(note, sizeof(note), "being-console: verbose boot: the screen stays on these messages for %d s;"
                 " press Esc or F2 to go on now", hold);
    else
        snprintf(note, sizeof(note), "being-console: verbose boot: the screen stays on these messages;"
                 " press Esc or F2 to go on");
    fprintf(stderr, "%s\n", note);
    int tty = open("/dev/tty0", O_WRONLY | O_NOCTTY | O_CLOEXEC);
    if (tty >= 0) {
        dprintf(tty, "\r\n%s\r\n", note);
        close(tty);
    }

    enum { MAX_DEVICES = 32 };
    int devices[MAX_DEVICES];
    char names[MAX_DEVICES][32];
    int count = 0;
    bool pressed = false;
    struct timespec start, now, last_scan = {0};
    clock_gettime(CLOCK_MONOTONIC, &start);
    while (!stopping && !pressed && access(VERBOSE_RELEASED, F_OK) != 0) {
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (hold > 0 && now.tv_sec - start.tv_sec >= hold)
            break;
        /* Keyboards can turn up late (USB), so look again every second. */
        if (now.tv_sec != last_scan.tv_sec) {
            last_scan = now;
            DIR *directory = opendir("/dev/input");
            struct dirent *entry;
            while (directory && (entry = readdir(directory)) && count < MAX_DEVICES) {
                if (strncmp(entry->d_name, "event", 5) != 0 || strlen(entry->d_name) >= sizeof(names[0]))
                    continue;
                bool known = false;
                for (int i = 0; i < count && !known; i++)
                    known = strcmp(names[i], entry->d_name) == 0;
                if (known)
                    continue;
                int device = openat(dirfd(directory), entry->d_name, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
                if (device >= 0) {
                    devices[count] = device;
                    snprintf(names[count], sizeof(names[count]), "%s", entry->d_name);
                    count++;
                }
            }
            if (directory)
                closedir(directory);
        }
        for (int i = 0; i < count && !pressed; i++) {
            struct input_event events[32];
            ssize_t got;
            while (!pressed && (got = read(devices[i], events, sizeof(events))) > 0) {
                for (size_t k = 0; k < (size_t)got / sizeof(events[0]); k++) {
                    unsigned code = events[k].code;
                    if (events[k].type == EV_KEY && events[k].value == 1 &&
                        (code == KEY_ESC || code == KEY_F2 || code == KEY_ENTER || code == KEY_SPACE ||
                         code == KEY_KPENTER))
                        pressed = true;
                }
            }
        }
        usleep(100000);
    }
    for (int i = 0; i < count; i++)
        close(devices[i]);
    fprintf(stderr, "being-console: %s\n", pressed ? "a key ended the verbose wait" : "verbose wait over");
    int marker = open(VERBOSE_RELEASED, O_WRONLY | O_CREAT | O_CLOEXEC, 0644);
    if (marker >= 0)
        close(marker);
}
#endif

static void usage(const char *name)
{
    fprintf(stderr, "usage: %s COMMAND [ARGS...]\n", name);
}

int main(int argc, char **argv)
{
    if (argc >= 3 && strcmp(argv[1], "--panel") == 0)
        return panel_main(argc, argv);
    /* Hidden options for lab/console_preview.py: a snapshot of the screen
     * after a while, drawn without a window if asked, with typed input. */
    struct options options = {0};
    int first = 1;
    while (first < argc && strncmp(argv[first], "--", 2) == 0) {
        const char *option = argv[first++];
        if (strcmp(option, "--") == 0)
            break;
        if (first >= argc) {
            usage(argv[0]);
            return 2;
        }
        const char *value = argv[first++];
        if (strcmp(option, "--snapshot") == 0)
            options.snapshot = value;
        else if (strcmp(option, "--exit-after") == 0)
            options.exit_after = atof(value);
        else if (strcmp(option, "--feed") == 0)
            options.feed = value;
        else if (strcmp(option, "--headless") == 0 &&
                 sscanf(value, "%dx%d", &options.headless_width, &options.headless_height) == 2 &&
                 options.headless_width > 0 && options.headless_height > 0)
            continue;
        else {
            usage(argv[0]);
            return 2;
        }
    }
    if (first >= argc) {
        usage(argv[0]);
        return 2;
    }

    const char *screen = getenv("BEING_SCREEN_DIR");
    int gate = -1;
    if (screen && (gate = open(screen, O_RDONLY | O_DIRECTORY | O_CLOEXEC)) < 0) {
        perror("being-console screen directory");
        return 1;
    }
    struct sigaction action = { .sa_handler = stop };
    sigemptyset(&action.sa_mask);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGHUP, &action, NULL);
#ifdef __linux__
    const char *video = getenv("SDL_VIDEODRIVER");
    if (gate >= 0 && !options.snapshot && options.headless_width == 0 && video && strcmp(video, "kmsdrm") == 0) {
        hold_for_boot_messages();
        if (stopping) {
            close(gate);
            return 1;
        }
    }
#endif
    setenv("TERM", "dumb", 1);
    /* Colour, box drawing and cursor moves are welcome here (see the top of this file). */
    setenv("BEING_CONSOLE_UI", "1", 1);
    if (!load_glyphs()) {
        fprintf(stderr, "being-console: out of memory\n");
        return 1;
    }

    struct session session = { .running = true, .status = -1 };
    struct winsize initial = { .ws_row = 24, .ws_col = 80 };
    session.pid = forkpty(&session.master, NULL, NULL, &initial);
    if (session.pid == 0) {
        execvp(argv[first], &argv[first]);
        perror(argv[first]);
        _exit(127);
    }
    if (session.pid < 0) {
        perror("being-console PTY");
        return 1;
    }

    int result = 1;
    if (fcntl(session.master, F_SETFL, O_NONBLOCK) < 0) {
        perror("being-console PTY nonblocking mode");
        goto cleanup;
    }
    struct termios attributes;
    if (tcgetattr(session.master, &attributes) == 0) {
        attributes.c_cc[VERASE] = '\177';
#ifdef IUTF8
        attributes.c_iflag |= IUTF8;
#endif
        tcsetattr(session.master, TCSANOW, &attributes);
    }
    SDL_SetMainReady();
    SDL_SetHint(SDL_HINT_NO_SIGNAL_HANDLERS, "1");
    bool headless = options.headless_width > 0;
    if (SDL_Init(headless ? SDL_INIT_TIMER : SDL_INIT_VIDEO) < 0) {
        SDL_LogError(SDL_LOG_CATEGORY_APPLICATION, "Console video: %s", SDL_GetError());
        goto cleanup;
    }

    const char *driver = headless ? NULL : SDL_GetCurrentVideoDriver();
    bool kms = driver && SDL_strcasecmp(driver, "kmsdrm") == 0;
    struct display display = { .headless_width = options.headless_width, .headless_height = options.headless_height };
    if (headless || open_display(&display, kms)) {
        if (!headless) {
            SDL_StartTextInput();
            if (kms)
                SDL_ShowCursor(SDL_DISABLE);
        }
        SDL_Log("Console opened: %s", argv[first]);
        bool finished = run_console(&display, &session, gate, kms, &options);
        if (!headless)
            SDL_StopTextInput();
        if (finished)
            result = WIFEXITED(session.status) ? WEXITSTATUS(session.status) : session.running && !stopping ? 0 : 1;
        SDL_FreeSurface(display.surface);
        if (display.context)
            SDL_GL_DeleteContext(display.context);
        if (display.window)
            SDL_DestroyWindow(display.window);
    } else {
        SDL_LogError(SDL_LOG_CATEGORY_APPLICATION, "Console renderer: %s", SDL_GetError());
    }
    SDL_Quit();

cleanup:
    finish_session(&session);
    if (gate >= 0)
        close(gate);
    return result;
}
