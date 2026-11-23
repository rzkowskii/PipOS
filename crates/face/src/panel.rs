//! The workshop panel (docs/design/voice-apps.md, section 3.2): one app's
//! picture, rendered off-screen by `being-console --panel` into a frame file,
//! composited under the eyes, which glide up into a strip at the top.
//!
//! # Frame file
//!
//! `/run/being-panel/NAME.frame`, written only by its renderer. The layout is
//! the renderer's own: `os/package/being/being-console-panel.h` (what
//! `being-console --panel` writes) and `parse_panel_header()` in
//! `lab/console_preview.py` (the lab's reader) say the same thing. A 128-byte
//! little-endian header:
//!
//! | offset | size | field |
//! |---|---|---|
//! | 0 | 4 | magic `BPN1` |
//! | 4 | 4 | width (pixels) |
//! | 8 | 4 | height (pixels) |
//! | 12 | 4 | stride (bytes per row, at least width x 4) |
//! | 16 | 8 | sequence: odd while the renderer writes, even when a frame is whole |
//! | 24 | 4 | first dirty row |
//! | 28 | 4 | dirty row count (0: the header alone changed) |
//! | 32 | 4 | flags (below) |
//! | 36 | 4 | cursor x: left of the cursor cell in pixels, -1 when hidden |
//! | 40 | 4 | cursor y: top of the cursor cell in pixels, -1 when hidden |
//! | 44 | 4 | beat: +1 every 500 ms while visible |
//! | 48 | 4 | text grid columns |
//! | 52 | 4 | text grid rows |
//! | 56 | 4 | newest_y: top of the newest output line in pixels, -1 none |
//! | 60 | 2 | cell width (pixels) |
//! | 62 | 2 | cell height (pixels) |
//! | 64 | 4 | pid of the renderer |
//! | 68-95 | | reserved, zero |
//! | 96 | 32 | title, UTF-8, NUL-padded |
//!
//! RGBA rows follow at offset 128, top row first, `height` rows of `stride`
//! bytes. Flags: 1 visible, 2 focus, 4 pip_typing, 8 pip_paused, 16 job_busy,
//! 32 human_typing, 64 scrolled.
//!
//! Seqlock: the renderer writes an odd sequence, then the rows and the rest of
//! the header, then the next even sequence. The face reads the header, then
//! the dirty rows (all rows if it missed a frame), then the sequence again,
//! and drops the frame on a mismatch.
//!
//! Liveness: a visible frame's beat must change within 2.5 s or the panel is
//! hidden, so a dead renderer is never left frozen on screen. The beat stands
//! still while the app is minimised, so a hidden frame's flags count only
//! while its renderer's pid is alive. The file appears by rename, so a path
//! whose inode changed is opened again.
//!
//! Switching apps: at most one app is shown, and hands shows the next one
//! before it hides the last (the terminal, the body monitor and the clock
//! each have a file). The moment the followed frame turns hidden, or its file
//! is removed (a closed app), the directory is looked at again in the same
//! poll, so the face goes straight from one panel to the next with no frame
//! of full face between them, and a closed app leaves the screen at once.
//! While the followed frame is hidden the directory is looked at four times a
//! second, so an app opened over a minimised terminal shows within 250 ms.
//!
//! The renderer is unprivileged and parses what caged programs print, so the
//! face opens only regular files, never through a symlink, never blocking.

use std::fs::File;
use std::os::unix::fs::{FileExt, MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub const DIR: &str = "/run/being-panel";
pub const KEYS: &str = "/run/being-hands/keys";
pub const HEADER: usize = 128;
pub const MAGIC: &[u8; 4] = b"BPN1";
/// The eye strip on the laptop's 800-row screen: 160 px (owner decision 10).
pub const STRIP_FRACTION: f32 = 160.0 / 800.0;
/// Opening and closing the panel.
pub const TRANSITION: f32 = 0.45;
const BEAT_TIMEOUT: Duration = Duration::from_millis(2500);
const RESCAN: Duration = Duration::from_secs(1);
/// How often to look for another app while the followed one is hidden.
const RESCAN_HIDDEN: Duration = Duration::from_millis(250);
/// Larger than any panel this screen shows; it bounds what a broken or
/// hostile renderer can make the face allocate (16 MB).
const MAX_SIDE: u32 = 2048;

#[allow(dead_code)] // the face reads only some; all are listed for the format
pub mod flag {
    pub const VISIBLE: u32 = 1;
    pub const FOCUS: u32 = 2;
    pub const PIP_TYPING: u32 = 4;
    pub const PIP_PAUSED: u32 = 8;
    pub const JOB_BUSY: u32 = 16;
    pub const HUMAN_TYPING: u32 = 32;
    pub const SCROLLED: u32 = 64;
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Header {
    pub width: u32,
    pub height: u32,
    pub stride: u32,
    pub seq: u64,
    pub dirty_first: u32,
    pub dirty_count: u32,
    pub flags: u32,
    /// The cursor cell's top-left corner, in pixels.
    pub cursor: Option<(u32, u32)>,
    pub beat: u32,
    pub columns: u32,
    pub rows: u32,
    /// The top of the newest output line, in pixels.
    pub newest_y: Option<u32>,
    /// Cell width and height, in pixels.
    pub cell: (u16, u16),
    pub pid: u32,
    pub title: String,
}

impl Header {
    pub fn has(&self, bit: u32) -> bool {
        self.flags & bit != 0
    }

    /// Where Pip is typing: the middle of the cursor cell.
    pub fn cursor_point(&self) -> Option<(u32, u32)> {
        let (cw, ch) = (u32::from(self.cell.0) / 2, u32::from(self.cell.1) / 2);
        self.cursor.map(|(x, y)| ((x + cw).min(self.width - 1), (y + ch).min(self.height - 1)))
    }

    /// Where a running job's newest output is, read from near the start of
    /// its line; the cursor when the renderer does not say.
    pub fn newest_point(&self) -> Option<(u32, u32)> {
        let ch = u32::from(self.cell.1) / 2;
        self.newest_y.map(|y| (self.width / 3, (y + ch).min(self.height - 1))).or_else(|| self.cursor_point())
    }
}

fn word(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
}

fn half(b: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([b[at], b[at + 1]])
}

fn long(b: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(b[at..at + 8].try_into().unwrap())
}

/// A signed pixel position that must land inside `limit`; -1 means none.
fn position(v: u32, limit: u32) -> Option<u32> {
    (v as i32 >= 0 && v < limit).then_some(v)
}

pub fn parse_header(b: &[u8]) -> Result<Header, &'static str> {
    if b.len() < HEADER {
        return Err("short header");
    }
    if &b[..4] != MAGIC {
        return Err("not a panel frame");
    }
    let (width, height, stride) = (word(b, 4), word(b, 8), word(b, 12));
    if width == 0 || height == 0 || width > MAX_SIDE || height > MAX_SIDE {
        return Err("panel size out of range");
    }
    if stride < width * 4 || stride > MAX_SIDE * 4 * 2 {
        return Err("bad stride");
    }
    let (dirty_first, dirty_count) = (word(b, 24), word(b, 28));
    if dirty_first.checked_add(dirty_count).is_none_or(|end| end > height) {
        return Err("dirty rows out of range");
    }
    let cursor = position(word(b, 36), width).zip(position(word(b, 40), height));
    let raw = &b[96..128];
    let end = raw.iter().position(|&c| c == 0).unwrap_or(raw.len());
    let title = String::from_utf8_lossy(&raw[..end]).chars().filter(|c| !c.is_control()).collect();
    Ok(Header {
        width,
        height,
        stride,
        seq: long(b, 16),
        dirty_first,
        dirty_count,
        flags: word(b, 32),
        cursor,
        beat: word(b, 44),
        columns: word(b, 48),
        rows: word(b, 52),
        newest_y: position(word(b, 56), height),
        cell: (half(b, 60), half(b, 62)),
        pid: word(b, 64),
        title,
    })
}

/// Build a header (the renderer's side; here for tests and tools).
#[cfg_attr(not(test), allow(dead_code))]
pub fn encode_header(h: &Header) -> [u8; HEADER] {
    let mut b = [0u8; HEADER];
    b[..4].copy_from_slice(MAGIC);
    let none = u32::MAX; // -1
    let (cx, cy) = h.cursor.unwrap_or((none, none));
    for (at, v) in [
        (4, h.width),
        (8, h.height),
        (12, h.stride),
        (24, h.dirty_first),
        (28, h.dirty_count),
        (32, h.flags),
        (36, cx),
        (40, cy),
        (44, h.beat),
        (48, h.columns),
        (52, h.rows),
        (56, h.newest_y.unwrap_or(none)),
        (64, h.pid),
    ] {
        b[at..at + 4].copy_from_slice(&v.to_le_bytes());
    }
    b[16..24].copy_from_slice(&h.seq.to_le_bytes());
    b[60..62].copy_from_slice(&h.cell.0.to_le_bytes());
    b[62..64].copy_from_slice(&h.cell.1.to_le_bytes());
    let title = h.title.as_bytes();
    let n = title.len().min(31);
    b[96..96 + n].copy_from_slice(&title[..n]);
    b
}

/// Whether a renderer's process still exists (signal 0 checks, sends nothing).
fn alive(pid: u32) -> bool {
    extern "C" {
        fn kill(pid: i32, sig: i32) -> i32;
    }
    const EPERM: i32 = 1;
    // 0 would ask about our own process group, and a negative number about
    // someone else's: neither is a renderer.
    if pid == 0 || pid > i32::MAX as u32 {
        return false;
    }
    // SAFETY: signal 0 delivers nothing; it only reports whether pid exists.
    let rc = unsafe { kill(pid as i32, 0) };
    rc == 0 || std::io::Error::last_os_error().raw_os_error() == Some(EPERM)
}

/// Open a frame file the way root must open what an unprivileged process
/// made: a regular file only, never through a symlink, and never blocking
/// (a FIFO named *.frame would otherwise freeze the face on open).
fn open_frame(path: &Path) -> Option<File> {
    #[cfg(target_os = "linux")]
    const FLAGS: i32 = 0o400000 | 0o4000; // O_NOFOLLOW | O_NONBLOCK
    #[cfg(not(target_os = "linux"))]
    const FLAGS: i32 = 0x0100 | 0x0004; // the same on the BSDs and macOS
    let file = std::fs::OpenOptions::new().read(true).custom_flags(FLAGS).open(path).ok()?;
    file.metadata().ok().filter(|m| m.is_file())?;
    Some(file)
}

/// The file a path names now (a renamed-in replacement is a new one).
fn file_id(path: &Path) -> Option<(u64, u64)> {
    std::fs::symlink_metadata(path).ok().map(|m| (m.dev(), m.ino()))
}

/// One frame file being followed.
struct Source {
    path: PathBuf,
    file: File,
    id: (u64, u64),
    header: Option<Header>,
    last_seq: Option<u64>,
    beat: Option<u32>,
    beat_seen: Option<Instant>,
    /// The renderer's pid, whether it lives, and when that was checked.
    alive: Option<(u32, bool, Instant)>,
}

impl Source {
    fn fresh(&self, now: Instant) -> bool {
        self.beat_seen.is_some_and(|at| now.saturating_duration_since(at) < BEAT_TIMEOUT)
    }

    fn renderer_alive(&self) -> bool {
        self.alive.is_some_and(|(_, alive, _)| alive)
    }
}

/// What changed in the picture since the last poll.
#[derive(Debug, PartialEq, Eq)]
pub enum Update {
    Nothing,
    /// These rows of `pixels` are new (first row, count).
    Rows(u32, u32),
    /// The size changed: upload everything.
    Resized,
}

/// The panel as the face sees it: the newest whole picture of the visible app.
pub struct Panel {
    dir: PathBuf,
    source: Option<Source>,
    scanned: Option<Instant>,
    /// Packed RGBA rows, `width * 4` bytes each.
    pub pixels: Vec<u8>,
    pub width: u32,
    pub height: u32,
    row: Vec<u8>,
    /// The followed frame just turned hidden or lost its file: look again now.
    rescan_now: bool,
}

impl Panel {
    pub fn new(dir: impl Into<PathBuf>) -> Panel {
        Panel {
            dir: dir.into(),
            source: None,
            scanned: None,
            pixels: Vec::new(),
            width: 0,
            height: 0,
            row: Vec::new(),
            rescan_now: false,
        }
    }

    /// The followed frame file's path, if any (for tests and the log).
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn following(&self) -> Option<&Path> {
        self.source.as_ref().map(|s| s.path.as_path())
    }

    /// The header of the followed frame, if it may be shown: valid, flagged
    /// visible, and with a beat that changed in the last 2.5 s.
    pub fn shown(&self, now: Instant) -> Option<&Header> {
        let source = self.source.as_ref()?;
        let header = source.header.as_ref()?;
        (header.has(flag::VISIBLE) && source.fresh(now) && !self.pixels.is_empty()).then_some(header)
    }

    /// The latest flags even while the panel is hidden or minimised: from a
    /// live beat, or from a hidden frame whose renderer is still running.
    pub fn flags(&self, now: Instant) -> u32 {
        let Some(source) = &self.source else { return 0 };
        let Some(header) = &source.header else { return 0 };
        let live = source.fresh(now) || (!header.has(flag::VISIBLE) && source.renderer_alive());
        if live {
            header.flags
        } else {
            0
        }
    }

    /// Drop the face's copy of the picture (zeroed first): a dark screen, a
    /// closed app or a wiped terminal leaves nothing of it behind in the face.
    /// The next poll reads the whole frame again.
    pub fn forget_picture(&mut self) {
        self.pixels.fill(0);
        std::hint::black_box(&self.pixels);
        self.row.fill(0);
        self.pixels = Vec::new();
        self.width = 0;
        self.height = 0;
        if let Some(source) = &mut self.source {
            source.last_seq = None;
        }
    }

    fn drop_source(&mut self) {
        if self.source.take().is_some() {
            self.forget_picture();
        }
    }

    /// Look for frame files at most once a second; with none, that is all
    /// the panel ever costs.
    fn discover(&mut self, now: Instant) {
        let hidden = self.source.as_ref().is_some_and(|s| s.header.as_ref().is_none_or(|h| !h.has(flag::VISIBLE)));
        let every = if hidden { RESCAN_HIDDEN } else { RESCAN };
        if self.scanned.is_some_and(|at| now.saturating_duration_since(at) < every) {
            return;
        }
        self.scanned = Some(now);
        let Ok(entries) = std::fs::read_dir(&self.dir) else {
            self.drop_source();
            return;
        };
        let mut frames: Vec<PathBuf> =
            entries.flatten().map(|e| e.path()).filter(|p| p.extension().is_some_and(|x| x == "frame")).collect();
        frames.sort();
        // Keep following the current file while it is still the one at its
        // path; otherwise take the one that says it is visible (the terminal
        // and at most one other app may exist at once), else the first.
        let current = self
            .source
            .as_ref()
            .filter(|s| frames.contains(&s.path) && file_id(&s.path) == Some(s.id))
            .map(|s| (s.path.clone(), s.header.as_ref().is_some_and(|h| h.has(flag::VISIBLE))));
        if current.as_ref().is_some_and(|(_, visible)| *visible) {
            return;
        }
        let mut best: Option<(bool, PathBuf)> = None;
        for path in frames {
            let mut b = [0u8; HEADER];
            let visible = open_frame(&path)
                .and_then(|f| f.read_exact_at(&mut b, 0).ok())
                .and_then(|()| parse_header(&b).ok())
                .is_some_and(|h| h.has(flag::VISIBLE));
            if best.as_ref().is_none_or(|(v, _)| visible && !v) {
                best = Some((visible, path));
            }
        }
        match best {
            Some((_, path)) if current.as_ref().is_some_and(|(p, _)| *p == path) => {}
            Some((_, path)) => {
                self.drop_source();
                self.source = open_frame(&path).and_then(|file| {
                    let meta = file.metadata().ok()?;
                    Some(Source {
                        path,
                        file,
                        id: (meta.dev(), meta.ino()),
                        header: None,
                        last_seq: None,
                        beat: None,
                        beat_seen: None,
                        alive: None,
                    })
                });
            }
            None => self.drop_source(),
        }
    }

    /// Read whatever is new. Never blocks on anything but a page-cache read.
    pub fn poll(&mut self, now: Instant) -> Update {
        self.discover(now);
        let update = self.read(now);
        if !std::mem::take(&mut self.rescan_now) {
            return update;
        }
        // The followed app was hidden or closed: whatever is shown now is
        // read in this same poll.
        let before = self.source.as_ref().map(|s| s.id);
        self.scanned = None;
        self.discover(now);
        if self.source.as_ref().map(|s| s.id) == before {
            return update;
        }
        match self.read(now) {
            Update::Nothing if update == Update::Resized => Update::Resized,
            next => next,
        }
    }

    fn read(&mut self, now: Instant) -> Update {
        let Some(source) = &mut self.source else { return Update::Nothing };
        let mut b = [0u8; HEADER];
        if source.file.read_exact_at(&mut b, 0).is_err() {
            self.drop_source();
            return Update::Nothing;
        }
        // A closed app's renderer removes its file; a restarted one renames a
        // new file over it. Either way this one is finished (one fstat).
        if source.file.metadata().is_ok_and(|m| m.nlink() == 0) {
            self.rescan_now = true;
            self.drop_source();
            return Update::Nothing;
        }
        let header = match parse_header(&b) {
            Ok(h) => h,
            Err(_) => {
                source.header = None;
                return Update::Nothing;
            }
        };
        let was_visible = source.header.as_ref().is_some_and(|h| h.has(flag::VISIBLE));
        if was_visible && !header.has(flag::VISIBLE) {
            self.rescan_now = true;
        }
        let stale = source.alive.is_none_or(|(pid, _, at)| pid != header.pid || now.saturating_duration_since(at) >= RESCAN);
        if stale {
            source.alive = Some((header.pid, alive(header.pid), now));
        }
        match source.beat {
            // A file found already beating counts only if its renderer runs:
            // one left by a renderer that died is never shown.
            None => {
                source.beat = Some(header.beat);
                if source.renderer_alive() {
                    source.beat_seen = Some(now);
                }
            }
            Some(beat) if beat != header.beat => {
                source.beat = Some(header.beat);
                source.beat_seen = Some(now);
            }
            Some(_) => {}
        }
        if header.seq % 2 == 1 || Some(header.seq) == source.last_seq {
            if header.seq % 2 == 0 {
                source.header = Some(header);
            }
            return Update::Nothing;
        }
        let resized = header.width != self.width || header.height != self.height;
        let missed = source.last_seq.is_none_or(|last| header.seq != last.wrapping_add(2));
        let (first, count) = if resized || missed {
            (0, header.height)
        } else {
            (header.dirty_first, header.dirty_count)
        };
        if resized {
            self.width = header.width;
            self.height = header.height;
            self.pixels = vec![0; (header.width * header.height * 4) as usize];
        }
        let packed = (header.width * 4) as usize;
        self.row.resize(header.stride as usize, 0);
        for y in first..first + count {
            let at = HEADER as u64 + u64::from(y) * u64::from(header.stride);
            if source.file.read_exact_at(&mut self.row, at).is_err() {
                source.last_seq = None;
                return if resized { Update::Resized } else { Update::Nothing };
            }
            let dst = y as usize * packed;
            self.pixels[dst..dst + packed].copy_from_slice(&self.row[..packed]);
        }
        let mut seq = [0u8; 8];
        let whole = source.file.read_exact_at(&mut seq, 16).is_ok() && u64::from_le_bytes(seq) == header.seq;
        if !whole {
            // Torn: what was read may be half old and half new. Read it all
            // again next time.
            source.last_seq = None;
            return if resized { Update::Resized } else { Update::Nothing };
        }
        source.last_seq = Some(header.seq);
        source.header = Some(header);
        if resized {
            Update::Resized
        } else if count > 0 {
            Update::Rows(first, count)
        } else {
            Update::Nothing
        }
    }
}


/// A rectangle in GL window coordinates (origin bottom left), in pixels.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

/// Where the eyes and the panel go, `amount` of the way (0 full face, 1
/// strip) through the 450 ms transition.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Layout {
    pub eyes: Rect,
    pub panel: Rect,
    /// How far the panel has faded up.
    pub panel_alpha: f32,
}

pub fn smoothstep(x: f32) -> f32 {
    let x = x.clamp(0.0, 1.0);
    x * x * (3.0 - 2.0 * x)
}

pub fn layout(w: i32, h: i32, amount: f32) -> Layout {
    let s = smoothstep(amount);
    let strip_h = ((h as f32) * STRIP_FRACTION).round().max(1.0);
    // The strip keeps the screen's shape, so the eye geometry does not change.
    let strip_w = (strip_h * w as f32 / h.max(1) as f32).round();
    let lerp = |a: f32, b: f32| (a + (b - a) * s).round() as i32;
    let eyes = Rect {
        x: lerp(0.0, (w as f32 - strip_w) / 2.0),
        y: lerp(0.0, h as f32 - strip_h),
        w: lerp(w as f32, strip_w),
        h: lerp(h as f32, strip_h),
    };
    let panel = Rect { x: 0, y: 0, w, h: h - strip_h as i32 };
    Layout { eyes, panel, panel_alpha: s }
}

/// Where the eyes look for a point on the panel: the panel lies below the
/// strip, so even its top row is well down.
pub fn gaze_at(x: u32, y: u32, width: u32, height: u32) -> [f32; 2] {
    let fx = x as f32 / width.max(1) as f32;
    let fy = y as f32 / height.max(1) as f32;
    [((fx * 2.0 - 1.0) * 0.9).clamp(-1.0, 1.0), (-0.55 - 0.4 * fy).clamp(-1.0, 1.0)]
}

/// The bytes a key sends to the shell, as being-console's `key_character()`
/// does (US layout), plus Ctrl+letter and the usual xterm sequences. Esc is
/// never here: it is `key.interrupt` and never reaches the shell.
pub fn key_bytes(key: sdl2::keyboard::Keycode, keymod: sdl2::keyboard::Mod) -> Option<Vec<u8>> {
    use sdl2::keyboard::{Keycode as K, Mod};
    let shift = keymod.intersects(Mod::LSHIFTMOD | Mod::RSHIFTMOD);
    let ctrl = keymod.intersects(Mod::LCTRLMOD | Mod::RCTRLMOD);
    let caps = keymod.contains(Mod::CAPSMOD);
    if keymod.intersects(Mod::LALTMOD | Mod::RALTMOD | Mod::LGUIMOD | Mod::RGUIMOD) {
        return None;
    }
    let seq = |s: &str| Some(s.as_bytes().to_vec());
    match key {
        K::Escape => return None,
        K::Return | K::KpEnter => return seq("\r"),
        K::Backspace => return seq("\x7f"),
        K::Tab => return seq("\t"),
        K::Up => return seq("\x1b[A"),
        K::Down => return seq("\x1b[B"),
        K::Right => return seq("\x1b[C"),
        K::Left => return seq("\x1b[D"),
        K::Home => return seq("\x1b[H"),
        K::End => return seq("\x1b[F"),
        K::Delete => return seq("\x1b[3~"),
        K::PageUp => return seq("\x1b[5~"),
        K::PageDown => return seq("\x1b[6~"),
        K::F3 => return seq("\x1bOR"),
        K::Kp0 => return seq("0"),
        _ => {}
    }
    let code = key.into_i32();
    if (K::Kp1.into_i32()..=K::Kp9.into_i32()).contains(&code) {
        return Some(vec![b'1' + (code - K::Kp1.into_i32()) as u8]);
    }
    if !(0x20..=0x7e).contains(&code) {
        return None;
    }
    let c = code as u8;
    if c.is_ascii_lowercase() {
        if ctrl {
            return Some(vec![c - b'a' + 1]);
        }
        return Some(vec![if shift != caps { c.to_ascii_uppercase() } else { c }]);
    }
    if ctrl {
        return None;
    }
    const PLAIN: &[u8] = b"`1234567890-=[]\\;',./";
    const SHIFTED: &[u8] = b"~!@#$%^&*()_+{}|:\"<>?";
    let out = match PLAIN.iter().position(|&p| p == c) {
        Some(i) if shift => SHIFTED[i],
        _ => c,
    };
    Some(vec![out])
}

/// Keys to hands: one datagram per key, never blocking; a full socket drops
/// the key.
pub struct KeySender {
    path: PathBuf,
    socket: Option<std::os::unix::net::UnixDatagram>,
}

impl KeySender {
    pub fn new(path: impl Into<PathBuf>) -> KeySender {
        KeySender { path: path.into(), socket: None }
    }

    pub fn send(&mut self, bytes: &[u8]) -> bool {
        if self.socket.is_none() {
            self.socket = std::os::unix::net::UnixDatagram::unbound().ok();
            if let Some(socket) = &self.socket {
                let _ = socket.set_nonblocking(true);
            }
        }
        self.socket.as_ref().is_some_and(|s| s.send_to(bytes, Path::new(&self.path)).is_ok())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sdl2::keyboard::{Keycode as K, Mod};
    use std::io::Write;

    fn header(seq: u64, flags: u32) -> Header {
        Header {
            width: 4,
            height: 3,
            stride: 20,
            seq,
            dirty_first: 0,
            dirty_count: 3,
            flags,
            cursor: Some((2, 1)),
            beat: seq as u32,
            columns: 1,
            rows: 1,
            newest_y: None,
            cell: (2, 2),
            pid: std::process::id(),
            title: "Terminal".to_string(),
        }
    }

    #[test]
    fn a_header_round_trips_and_bad_ones_are_refused() {
        let h = header(6, flag::VISIBLE | flag::PIP_TYPING);
        let b = encode_header(&h);
        assert_eq!(parse_header(&b), Ok(h.clone()));
        let mut bad = b;
        bad[0] = b'X';
        assert!(parse_header(&bad).is_err());
        let mut narrow = h.clone();
        narrow.stride = 8;
        assert!(parse_header(&encode_header(&narrow)).is_err());
        let mut rows = h.clone();
        rows.dirty_first = 2;
        rows.dirty_count = 2;
        assert!(parse_header(&encode_header(&rows)).is_err());
        let mut none = h.clone();
        none.cursor = None;
        assert_eq!(parse_header(&encode_header(&none)).unwrap().cursor, None);
        let mut huge = h;
        huge.width = 4096;
        huge.stride = 4096 * 4;
        assert!(parse_header(&encode_header(&huge)).is_err(), "bounded allocation");
        assert!(parse_header(&b[..64]).is_err());
    }

    #[test]
    fn the_layout_is_the_renderers_own() {
        // Field by field as os/package/being/being-console-panel.h
        // panel_encode() writes it (and lab/console_preview.py reads it).
        let mut b = [0u8; HEADER];
        b[..4].copy_from_slice(b"BPN1");
        let put = |b: &mut [u8; HEADER], at: usize, v: u32| b[at..at + 4].copy_from_slice(&v.to_le_bytes());
        put(&mut b, 4, 1280);
        put(&mut b, 8, 640);
        put(&mut b, 12, 1280 * 4);
        b[16..24].copy_from_slice(&0x1_0000_0006u64.to_le_bytes());
        put(&mut b, 24, 600);
        put(&mut b, 28, 24);
        put(&mut b, 32, flag::VISIBLE | flag::FOCUS | flag::JOB_BUSY);
        put(&mut b, 36, 33);
        put(&mut b, 40, 34);
        put(&mut b, 44, 77);
        put(&mut b, 48, 107);
        put(&mut b, 52, 23);
        put(&mut b, 56, 586);
        b[60..62].copy_from_slice(&11u16.to_le_bytes());
        b[62..64].copy_from_slice(&24u16.to_le_bytes());
        put(&mut b, 64, 4242);
        b[96..104].copy_from_slice(b"Terminal");
        let h = parse_header(&b).unwrap();
        assert_eq!((h.width, h.height, h.stride, h.seq), (1280, 640, 5120, 0x1_0000_0006));
        assert_eq!((h.dirty_first, h.dirty_count, h.beat, h.pid), (600, 24, 77, 4242));
        assert!(h.has(flag::FOCUS) && h.has(flag::JOB_BUSY) && !h.has(flag::PIP_TYPING));
        assert_eq!((h.cursor, h.newest_y, h.cell, h.columns, h.rows), (Some((33, 34)), Some(586), (11, 24), 107, 23));
        assert_eq!(h.title, "Terminal");
        assert_eq!(h.cursor_point(), Some((38, 46)), "the middle of the cursor cell");
        assert_eq!(h.newest_point(), Some((426, 598)));
        // -1: no cursor and no newest line.
        put(&mut b, 36, u32::MAX);
        put(&mut b, 40, u32::MAX);
        put(&mut b, 56, u32::MAX);
        let h = parse_header(&b).unwrap();
        assert_eq!((h.cursor, h.newest_y, h.newest_point()), (None, None, None));
        assert_eq!(encode_header(&h)[..96], b[..96]);
    }

    struct Fake {
        dir: PathBuf,
    }

    impl Fake {
        fn new() -> Fake {
            use std::sync::atomic::{AtomicU32, Ordering};
            static N: AtomicU32 = AtomicU32::new(0);
            let dir = std::env::temp_dir().join(format!("face-panel-{}-{}", std::process::id(), N.fetch_add(1, Ordering::Relaxed)));
            std::fs::create_dir_all(&dir).unwrap();
            Fake { dir }
        }

        /// Write a whole frame whose every pixel is `value`, in place.
        fn write(&self, h: &Header, value: u8) {
            self.write_app("terminal", h, value);
        }

        fn write_app(&self, app: &str, h: &Header, value: u8) {
            let path = self.dir.join(format!("{app}.frame"));
            let mut f = std::fs::OpenOptions::new().create(true).write(true).truncate(false).open(path).unwrap();
            f.write_all(&encode_header(h)).unwrap();
            f.write_all(&vec![value; (h.stride * h.height) as usize]).unwrap();
        }

        /// As the renderer creates its file: written aside, then renamed in.
        fn replace(&self, h: &Header, value: u8) {
            let aside = self.dir.join("terminal.frame.tmp");
            let mut f = std::fs::File::create(&aside).unwrap();
            f.write_all(&encode_header(h)).unwrap();
            f.write_all(&vec![value; (h.stride * h.height) as usize]).unwrap();
            std::fs::rename(&aside, self.dir.join("terminal.frame")).unwrap();
        }
    }

    impl Drop for Fake {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    #[test]
    fn frames_are_read_whole_then_by_dirty_rows_and_torn_ones_dropped() {
        let fake = Fake::new();
        let mut panel = Panel::new(&fake.dir);
        let t = Instant::now();
        assert_eq!(panel.poll(t), Update::Nothing);
        assert!(panel.shown(t).is_none());

        fake.write(&header(2, flag::VISIBLE), 7);
        let mut panel = Panel::new(&fake.dir);
        assert_eq!(panel.poll(t), Update::Resized);
        assert_eq!(panel.pixels.len(), 4 * 3 * 4);
        assert!(panel.pixels.iter().all(|&p| p == 7));
        assert_eq!(panel.shown(t).map(|h| h.title.as_str()), Some("Terminal"));
        assert_eq!(panel.poll(t), Update::Nothing, "same sequence: nothing new");

        // The next frame: only row 1 is dirty.
        let mut next = header(4, flag::VISIBLE);
        (next.dirty_first, next.dirty_count) = (1, 1);
        fake.write(&next, 9);
        assert_eq!(panel.poll(t), Update::Rows(1, 1));
        assert!(panel.pixels[..16].iter().all(|&p| p == 7) && panel.pixels[16..32].iter().all(|&p| p == 9));

        // A missed frame means a full read, dirty rows or not.
        let mut skipped = header(8, flag::VISIBLE);
        (skipped.dirty_first, skipped.dirty_count) = (0, 1);
        fake.write(&skipped, 5);
        assert_eq!(panel.poll(t), Update::Rows(0, 3));

        // Mid-write (odd): keep the old picture.
        fake.write(&header(9, flag::VISIBLE), 1);
        assert_eq!(panel.poll(t), Update::Nothing);
        assert!(panel.pixels.iter().all(|&p| p == 5));

        // Forgetting drops the face's copy; the next poll reads it all again.
        fake.write(&header(10, flag::VISIBLE), 6);
        panel.forget_picture();
        assert!(panel.pixels.is_empty() && panel.shown(t).is_none());
        assert_eq!(panel.poll(t), Update::Resized);
        assert!(panel.pixels.iter().all(|&p| p == 6));
    }

    #[test]
    fn a_renderer_whose_beat_stops_is_hidden_and_invisible_frames_never_show() {
        let fake = Fake::new();
        let mut h = header(2, flag::VISIBLE);
        fake.write(&h, 3);
        let mut panel = Panel::new(&fake.dir);
        let t = Instant::now();
        panel.poll(t);
        assert!(panel.shown(t).is_some());
        assert!(panel.shown(t + Duration::from_secs(3)).is_none(), "a renderer that stopped beating is hidden");
        // Minimised: no beat, but the renderer lives, so its flags count.
        h.seq = 4;
        h.flags = flag::PIP_TYPING;
        fake.write(&h, 3);
        let later = t + Duration::from_secs(10);
        panel.poll(later);
        assert!(panel.shown(later).is_none(), "not visible");
        assert_eq!(panel.flags(later), flag::PIP_TYPING, "minimised flags still arrive");
    }

    #[test]
    fn a_file_left_by_a_dead_renderer_never_shows() {
        let fake = Fake::new();
        let mut h = header(2, flag::VISIBLE | flag::PIP_TYPING);
        h.pid = 0x7fff_fff0; // no such process
        fake.write(&h, 3);
        let mut panel = Panel::new(&fake.dir);
        let t = Instant::now();
        panel.poll(t);
        assert!(panel.shown(t).is_none());
        assert_eq!(panel.flags(t), 0);
        h.flags = flag::PIP_TYPING;
        h.seq = 4;
        fake.write(&h, 3);
        panel.poll(t + Duration::from_secs(2));
        assert_eq!(panel.flags(t + Duration::from_secs(2)), 0, "hidden and dead: no flags");
        assert!(!alive(0) && alive(std::process::id()));
    }

    #[test]
    fn a_renamed_in_replacement_is_followed_and_the_old_picture_dropped() {
        let fake = Fake::new();
        fake.replace(&header(2, flag::VISIBLE), 3);
        let mut panel = Panel::new(&fake.dir);
        let t = Instant::now();
        assert_eq!(panel.poll(t), Update::Resized);
        // The renderer restarts: a new file at the same path.
        let mut again = header(2, flag::VISIBLE);
        again.beat = 500;
        fake.replace(&again, 8);
        let later = t + Duration::from_secs(2);
        assert_eq!(panel.poll(later), Update::Resized);
        assert!(panel.pixels.iter().all(|&p| p == 8), "the new renderer's picture");
        assert!(panel.shown(later).is_some());
        // The app closes: its file goes, and so does the face's copy.
        std::fs::remove_file(fake.dir.join("terminal.frame")).unwrap();
        panel.poll(t + Duration::from_secs(4));
        assert!(panel.pixels.is_empty() && panel.shown(t + Duration::from_secs(4)).is_none());
    }

    #[test]
    fn only_regular_files_are_opened() {
        let fake = Fake::new();
        let secret = fake.dir.join("secret");
        std::fs::write(&secret, [0u8; 300]).unwrap();
        std::os::unix::fs::symlink(&secret, fake.dir.join("link.frame")).unwrap();
        assert!(open_frame(&fake.dir.join("link.frame")).is_none(), "never through a symlink");
        let fifo = fake.dir.join("pipe.frame");
        let made = std::process::Command::new("mkfifo").arg(&fifo).status().is_ok_and(|s| s.success());
        if made {
            assert!(open_frame(&fifo).is_none(), "a FIFO neither blocks nor opens");
        }
        let mut panel = Panel::new(&fake.dir);
        assert_eq!(panel.poll(Instant::now()), Update::Nothing);
        assert!(open_frame(&secret).is_some());
    }

    fn app_header(seq: u64, flags: u32, title: &str) -> Header {
        Header { title: title.to_string(), ..header(seq, flags) }
    }

    #[test]
    fn opening_the_monitor_over_the_terminal_goes_straight_from_one_panel_to_the_other() {
        // hands shows the monitor first, then hides the terminal (SIGUSR2).
        let fake = Fake::new();
        fake.write_app("terminal", &app_header(2, flag::VISIBLE | flag::FOCUS, "Pip's terminal"), 3);
        let mut panel = Panel::new(&fake.dir);
        let t = Instant::now();
        assert_eq!(panel.poll(t), Update::Resized);
        assert!(panel.following().is_some_and(|p| p.ends_with("terminal.frame")));
        fake.write_app("monitor", &app_header(2, flag::VISIBLE, "How I'm feeling"), 7);
        let t1 = t + Duration::from_millis(1100);
        panel.poll(t1);
        assert!(panel.following().is_some_and(|p| p.ends_with("terminal.frame")), "the shown terminal is kept");
        assert_eq!(panel.shown(t1).map(|h| h.title.as_str()), Some("Pip's terminal"));
        // The terminal is hidden: the monitor is read in this same poll, well
        // inside the 1 s rescan, so the face never shows a frame of full face.
        fake.write_app("terminal", &app_header(4, flag::PIP_TYPING, "Pip's terminal"), 3);
        let t2 = t1 + Duration::from_millis(16);
        assert_eq!(panel.poll(t2), Update::Resized);
        assert_eq!(panel.shown(t2).map(|h| h.title.as_str()), Some("How I'm feeling"));
        assert!(!panel.shown(t2).unwrap().has(flag::FOCUS), "keys stay with the face for the small apps");
        assert!(panel.pixels.iter().all(|&p| p == 7));
        // The clock replaces the monitor the same way.
        fake.write_app("clock", &app_header(2, flag::VISIBLE, "Pip's clock"), 9);
        fake.write_app("monitor", &app_header(4, 0, "How I'm feeling"), 7);
        let t3 = t2 + Duration::from_millis(16);
        panel.poll(t3);
        assert_eq!(panel.shown(t3).map(|h| h.title.as_str()), Some("Pip's clock"));
        assert!(panel.pixels.iter().all(|&p| p == 9));
    }

    #[test]
    fn a_closed_app_leaves_the_screen_at_once() {
        let fake = Fake::new();
        fake.write_app("terminal", &app_header(2, flag::PIP_TYPING, "Pip's terminal"), 3); // minimised
        fake.write_app("monitor", &app_header(2, flag::VISIBLE, "How I'm feeling"), 7);
        let mut panel = Panel::new(&fake.dir);
        let t = Instant::now();
        panel.poll(t);
        assert_eq!(panel.shown(t).map(|h| h.title.as_str()), Some("How I'm feeling"));
        // "Show me your face": the monitor's renderer ends and removes its file.
        std::fs::remove_file(fake.dir.join("monitor.frame")).unwrap();
        let t1 = t + Duration::from_millis(16);
        panel.poll(t1);
        assert!(panel.shown(t1).is_none(), "gone at the next frame, not after a rescan or a stale beat");
        assert!(!panel.pixels.contains(&7), "and its picture with it");
        assert!(panel.following().is_some_and(|p| p.ends_with("terminal.frame")), "the minimised terminal's flags");
        assert_eq!(panel.flags(t1), flag::PIP_TYPING);
    }

    #[test]
    fn an_app_opened_over_a_minimised_terminal_shows_within_a_quarter_second() {
        let fake = Fake::new();
        fake.write_app("terminal", &app_header(2, 0, "Pip's terminal"), 3);
        let mut panel = Panel::new(&fake.dir);
        let t = Instant::now();
        panel.poll(t);
        assert!(panel.shown(t).is_none());
        fake.write_app("clock", &app_header(2, flag::VISIBLE, "Pip's clock"), 9);
        panel.poll(t + Duration::from_millis(100));
        assert!(panel.shown(t + Duration::from_millis(100)).is_none(), "not looked for yet");
        let t1 = t + Duration::from_millis(260);
        assert_eq!(panel.poll(t1), Update::Resized);
        assert_eq!(panel.shown(t1).map(|h| h.title.as_str()), Some("Pip's clock"));
    }

    #[test]
    fn the_small_apps_panel_fills_the_space_below_the_strip() {
        // hands renders every app at 1280x640 (PANEL_SIZE in mind/hands.py):
        // exactly the panel rectangle on the 1280x800 screen once the eyes
        // are in the strip, so the picture is never scaled.
        let strip = layout(1280, 800, 1.0);
        assert_eq!((strip.panel.w, strip.panel.h), (1280, 640));
        assert_eq!(strip.eyes.y, strip.panel.h, "the strip sits right on top of the panel");
        // The transition is monotonic and eased at both ends.
        let mut last = layout(1280, 800, 0.0).eyes.h;
        for step in 1..=45 {
            let h = layout(1280, 800, step as f32 / 45.0).eyes.h;
            assert!(h <= last);
            last = h;
        }
        assert!(smoothstep(0.05) < 0.05 && smoothstep(0.95) > 0.95);
        assert!((TRANSITION - 0.45).abs() < 1e-6);
    }

    #[test]
    fn the_strip_is_160_px_at_the_top_and_keeps_the_screen_shape() {
        let full = layout(1280, 800, 0.0);
        assert_eq!(full.eyes, Rect { x: 0, y: 0, w: 1280, h: 800 });
        assert_eq!(full.panel_alpha, 0.0);
        let strip = layout(1280, 800, 1.0);
        assert_eq!(strip.eyes, Rect { x: 512, y: 640, w: 256, h: 160 });
        assert_eq!(strip.panel, Rect { x: 0, y: 0, w: 1280, h: 640 });
        assert_eq!(strip.panel_alpha, 1.0);
        let half = layout(1280, 800, 0.5);
        assert!(half.eyes.w < 1280 && half.eyes.w > 256 && (half.panel_alpha - 0.5).abs() < 1e-6);
        // Past either end it holds.
        assert_eq!(layout(1280, 800, 2.0), strip);
    }

    #[test]
    fn eyes_look_down_at_the_cursor() {
        let [x, y] = gaze_at(0, 0, 1280, 640);
        assert!(x < -0.8 && y < -0.5);
        let [x, y] = gaze_at(1279, 639, 1280, 640);
        assert!(x > 0.8 && y < -0.9);
        assert!(gaze_at(640, 320, 1280, 640)[0].abs() < 0.01);
    }

    #[test]
    fn keys_become_the_bytes_a_terminal_expects() {
        let none = Mod::NOMOD;
        assert_eq!(key_bytes(K::A, none), Some(b"a".to_vec()));
        assert_eq!(key_bytes(K::A, Mod::LSHIFTMOD), Some(b"A".to_vec()));
        assert_eq!(key_bytes(K::A, Mod::CAPSMOD), Some(b"A".to_vec()));
        assert_eq!(key_bytes(K::A, Mod::CAPSMOD | Mod::RSHIFTMOD), Some(b"a".to_vec()));
        assert_eq!(key_bytes(K::C, Mod::LCTRLMOD), Some(vec![3]), "Ctrl-C reaches the shell");
        assert_eq!(key_bytes(K::Num2, Mod::LSHIFTMOD), Some(b"@".to_vec()));
        assert_eq!(key_bytes(K::Slash, Mod::LSHIFTMOD), Some(b"?".to_vec()));
        assert_eq!(key_bytes(K::Space, none), Some(b" ".to_vec()));
        assert_eq!(key_bytes(K::Return, none), Some(b"\r".to_vec()));
        assert_eq!(key_bytes(K::Backspace, none), Some(vec![0x7f]));
        assert_eq!(key_bytes(K::Up, none), Some(b"\x1b[A".to_vec()));
        assert_eq!(key_bytes(K::PageDown, none), Some(b"\x1b[6~".to_vec()));
        assert_eq!(key_bytes(K::Kp7, none), Some(b"7".to_vec()));
        assert_eq!(key_bytes(K::F3, none), Some(b"\x1bOR".to_vec()));
        assert_eq!(key_bytes(K::Escape, none), None, "Esc stays an interrupt");
        assert_eq!(key_bytes(K::F10, none), None, "F10 still mutes");
        assert_eq!(key_bytes(K::Q, Mod::LGUIMOD), None);
    }

    #[test]
    fn keys_are_datagrams_and_a_missing_listener_drops_them() {
        let fake = Fake::new();
        let path = fake.dir.join("keys");
        let mut sender = KeySender::new(&path);
        assert!(!sender.send(b"a"), "no hands: the key is dropped, nothing blocks");
        let listener = std::os::unix::net::UnixDatagram::bind(&path).unwrap();
        assert!(sender.send(b"\x1b[A"));
        let mut buf = [0u8; 16];
        let n = listener.recv(&mut buf).unwrap();
        assert_eq!(&buf[..n], b"\x1b[A");
    }
}
