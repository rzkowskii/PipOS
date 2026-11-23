//! Linux evdev lid state and the panel's sysfs power control. Battery,
//! temperature and light are read by `body::sysfs` on a thread of their own.
//!
//! Everything here runs on the hub thread, so it only makes non-blocking
//! evdev reads, small ioctls and single sysfs writes, each at most once a
//! second while something is missing.

use crate::body::sysfs;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

const EV_SYN: u16 = 0;
const SYN_REPORT: u16 = 0;
const SYN_DROPPED: u16 = 3;
const EV_SW: u16 = 5;
const SW_LID: u16 = 0;
const INPUT_EVENT_SIZE: usize = std::mem::size_of::<libc::timeval>() + 8;
const POLL_INTERVAL: Duration = Duration::from_secs(1);
/// EVIOCGBIT(EV_SW): which switches a device implements.
const EVIOCGBIT_SW: u8 = 0x20 + EV_SW as u8;
/// EVIOCGSW: the switches' current state.
const EVIOCGSW: u8 = 0x1b;
/// EVIOCGNAME: the device's name.
const EVIOCGNAME: u8 = 0x06;
/// Say so once if no lid switch has appeared by then.
const LID_GRACE: Duration = Duration::from_secs(15);
/// Lid device losses logged before going quiet about a flapping device.
const MAX_LOGGED_LOSSES: u32 = 5;

/// An evdev read ioctl: _IOC_READ, type 'E', the buffer's size, `number`.
fn evdev_read(file: &File, number: u8, buffer: &mut [u8]) -> io::Result<usize> {
    let request = (2usize << 30)
        | ((buffer.len() & 0x3fff) << 16)
        | (usize::from(b'E') << 8)
        | usize::from(number);
    // SAFETY: the request encodes the exact size of this writable buffer and
    // the File owns the input device descriptor throughout the call.
    let status = unsafe { libc::ioctl(file.as_raw_fd(), request as _, buffer.as_mut_ptr()) };
    usize::try_from(status).map_err(|_| io::Error::last_os_error())
}

/// Read an evdev switch bitmap. SW_LID is always the first bit.
fn lid_bit(file: &File, number: u8) -> io::Result<bool> {
    let mut mask = [0u8; 4];
    evdev_read(file, number, &mut mask)?;
    Ok(mask[0] & 1 != 0)
}

fn device_name(file: &File) -> String {
    let mut name = [0u8; 128];
    match evdev_read(file, EVIOCGNAME, &mut name) {
        Ok(_) => {
            let end = name.iter().position(|&b| b == 0).unwrap_or(name.len());
            String::from_utf8_lossy(&name[..end]).into_owned()
        }
        Err(_) => "(no name)".into(),
    }
}

fn publish(changes: &mut Vec<Option<bool>>, lid: &mut Option<bool>, next: Option<bool>) {
    if *lid != next {
        *lid = next;
        changes.push(next);
    }
}

#[derive(PartialEq)]
enum Sync {
    Ready,
    Dropped,
    Query,
}

struct Switch {
    input: File,
    path: PathBuf,
    name: String,
    sync: Sync,
    pending: Option<bool>,
    next_check: Instant,
}

impl Switch {
    fn discover(now: Instant) -> Option<Self> {
        let mut devices: Vec<_> = fs::read_dir("/dev/input")
            .ok()?
            .flatten()
            .take(256)
            .filter(|entry| {
                entry.file_name().to_str().is_some_and(|name| {
                    name.strip_prefix("event")
                        .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
                })
            })
            .map(|entry| entry.path())
            .collect();
        devices.sort();
        for device in devices {
            let Ok(input) = OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_NONBLOCK | libc::O_CLOEXEC)
                .open(&device)
            else {
                continue;
            };
            if matches!(lid_bit(&input, EVIOCGBIT_SW), Ok(true)) {
                let name = device_name(&input);
                return Some(Self {
                    input,
                    path: device,
                    name,
                    sync: Sync::Query,
                    pending: None,
                    next_check: now,
                });
            }
        }
        None
    }

    fn event(
        &mut self,
        kind: u16,
        code: u16,
        value: i32,
        lid: &mut Option<bool>,
        changes: &mut Vec<Option<bool>>,
    ) {
        match (kind, code, &self.sync) {
            (EV_SYN, SYN_DROPPED, _) => {
                self.pending = None;
                self.sync = Sync::Dropped;
                publish(changes, lid, None);
            }
            (EV_SYN, SYN_REPORT, Sync::Dropped) => self.sync = Sync::Query,
            (EV_SW, SW_LID, Sync::Ready) if value == 0 || value == 1 => {
                self.pending = Some(value == 1)
            }
            (EV_SYN, SYN_REPORT, Sync::Ready) => {
                if let Some(next) = self.pending.take() {
                    publish(changes, lid, Some(next));
                }
            }
            _ => {}
        }
    }

    fn read(
        &mut self,
        now: Instant,
        lid: &mut Option<bool>,
        changes: &mut Vec<Option<bool>>,
    ) -> io::Result<()> {
        let mut buffer = [0u8; INPUT_EVENT_SIZE * 32];
        // Bound the work of a noisy input source per brainstem tick.
        for _ in 0..16 {
            let count = match self.input.read(&mut buffer) {
                Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
                Ok(n) if n % INPUT_EVENT_SIZE == 0 => n,
                Ok(_) => return Err(io::ErrorKind::InvalidData.into()),
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    // Drain packets queued before the snapshot. Once recovery
                    // starts, a queued lid value cannot supersede EVIOCGSW.
                    if self.sync == Sync::Query
                        || (self.sync == Sync::Ready && now >= self.next_check)
                    {
                        let current = lid_bit(&self.input, EVIOCGSW)?;
                        publish(changes, lid, Some(current));
                        self.sync = Sync::Ready;
                        self.next_check = now + POLL_INTERVAL;
                    }
                    return Ok(());
                }
                Err(e) => return Err(e),
            };
            for raw in buffer[..count].chunks_exact(INPUT_EVENT_SIZE) {
                let Some(&[kind_a, kind_b, code_a, code_b, a, b, c, d]) =
                    raw.get(INPUT_EVENT_SIZE - 8..).and_then(|t| <&[u8; 8]>::try_from(t).ok())
                else {
                    return Err(io::ErrorKind::InvalidData.into());
                };
                self.event(
                    u16::from_ne_bytes([kind_a, kind_b]),
                    u16::from_ne_bytes([code_a, code_b]),
                    i32::from_ne_bytes([a, b, c, d]),
                    lid,
                    changes,
                );
            }
        }
        Ok(())
    }
}

/// An input source reports unknown before discovery and after a dropped
/// packet or device loss. Neither unknown nor absence counts as lid open.
pub struct Lid {
    device: Option<Switch>,
    closed: Option<bool>,
    next_scan: Instant,
    suspended: Option<Duration>,
    started: Instant,
    /// When the current device was opened, to back off from one that fails
    /// as soon as it is opened.
    found_at: Option<Instant>,
    said_missing: bool,
    said: Option<(PathBuf, Option<bool>)>,
    losses: u32,
    /// `being.lid=open` on the kernel command line: the owner says the lid
    /// is open, for a machine whose lid switch never appears. Used only
    /// while no lid device is present; a real switch always wins.
    assume_open: bool,
}

/// True if the kernel command line asks to treat a missing lid as open.
fn lid_open_requested(cmdline: &str) -> bool {
    cmdline.split_whitespace().any(|word| word == "being.lid=open")
}

impl Lid {
    pub fn new() -> Self {
        let now = Instant::now();
        Self {
            device: None,
            closed: None,
            next_scan: now,
            suspended: suspended_time(),
            started: now,
            found_at: None,
            said_missing: false,
            said: None,
            losses: 0,
            assume_open: sysfs::read_text(Path::new("/proc/cmdline")).is_some_and(|line| lid_open_requested(&line)),
        }
    }

    pub fn poll(&mut self) -> Vec<Option<bool>> {
        let now = Instant::now();
        let mut changes = Vec::new();
        if let Some(current) = suspended_time() {
            let resumed = self.suspended.replace(current).is_some_and(|previous| {
                current.saturating_sub(previous) > Duration::from_millis(20)
            });
            if resumed {
                // On resume the kernel may not send a fresh switch event.
                publish(&mut changes, &mut self.closed, None);
                if let Some(device) = &mut self.device {
                    device.pending = None;
                    device.sync = Sync::Query;
                }
                self.next_scan = now;
            }
        }
        if self.device.is_none() && now >= self.next_scan {
            self.device = Switch::discover(now);
            self.next_scan = now + POLL_INTERVAL;
            if let Some(device) = &self.device {
                self.found_at = Some(now);
                if self.losses <= MAX_LOGGED_LOSSES && self.said.as_ref().map(|(path, _)| path) != Some(&device.path) {
                    eprintln!("brainstem: lid: found \"{}\" at {}", device.name, device.path.display());
                    self.said = Some((device.path.clone(), None));
                }
            } else if !self.said_missing && now.saturating_duration_since(self.started) >= LID_GRACE {
                self.said_missing = true;
                eprintln!(
                    "brainstem: lid: no input device reports a lid switch after {} s; \
                     the lid stays unknown and the panel waits for one (or a panel-recover key)",
                    LID_GRACE.as_secs()
                );
                if self.assume_open {
                    eprintln!("brainstem: lid: being.lid=open is set, so the lid is taken as open");
                }
            }
        }
        // After the grace period, and again after a resume or a lost device,
        // the owner's word stands in for a switch that is not there.
        if self.assume_open && self.said_missing && self.device.is_none() {
            publish(&mut changes, &mut self.closed, Some(false));
        }
        if let Some(device) = self.device.as_mut() {
            if let Err(error) = device.read(now, &mut self.closed, &mut changes) {
                self.losses = self.losses.saturating_add(1);
                if self.losses <= MAX_LOGGED_LOSSES {
                    eprintln!("brainstem: lid: lost \"{}\" at {}: {error}", device.name, device.path.display());
                } else if self.losses == MAX_LOGGED_LOSSES + 1 {
                    eprintln!("brainstem: lid: the lid switch keeps failing; further losses are not logged");
                }
                publish(&mut changes, &mut self.closed, None);
                self.device = None;
                self.said = None;
                // Reopen at once after a one-off loss, but not in a tight
                // loop for a device that fails as soon as it is opened.
                let flapping = self.found_at.is_some_and(|at| now.saturating_duration_since(at) < POLL_INTERVAL * 5);
                self.next_scan = if flapping { now + POLL_INTERVAL } else { now };
            }
        }
        // One line the first time each device gives a definite state.
        if let (Some(device), Some((path, said))) = (&self.device, &mut self.said) {
            if *path == device.path && said.is_none() && self.closed.is_some() {
                *said = self.closed;
                eprintln!(
                    "brainstem: lid: reads {}",
                    if self.closed == Some(true) { "closed" } else { "open" }
                );
            }
        }
        changes
    }
}

#[cfg(target_os = "linux")]
fn suspended_time() -> Option<Duration> {
    fn clock(id: libc::clockid_t) -> Option<Duration> {
        let mut value = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        // SAFETY: Linux fills the writable timespec for the selected clock.
        if unsafe { libc::clock_gettime(id, &mut value) } != 0 {
            return None;
        }
        Some(Duration::new(
            value.tv_sec.try_into().ok()?,
            value.tv_nsec.try_into().ok()?,
        ))
    }

    let before = clock(libc::CLOCK_MONOTONIC)?;
    let boot = clock(libc::CLOCK_BOOTTIME)?;
    let after = clock(libc::CLOCK_MONOTONIC)?;
    // Discard a slow sample rather than treating a scheduling delay between
    // the clock calls as time spent suspended.
    if after.saturating_sub(before) > Duration::from_millis(1) {
        return None;
    }
    Some(boot.saturating_sub(after))
}

#[cfg(not(target_os = "linux"))]
fn suspended_time() -> Option<Duration> {
    None
}

/// The kernel power attribute does not change the user's brightness setting.
pub struct Backlight {
    control: Option<PathBuf>,
    last_request: Option<(bool, Instant)>,
    reported: Option<PathBuf>,
}

impl Backlight {
    pub fn new() -> Self {
        Self {
            control: None,
            last_request: None,
            reported: None,
        }
    }

    pub fn set_blank(&mut self, blank: bool) -> io::Result<()> {
        let now = Instant::now();
        if self
            .last_request
            .is_some_and(|(last, retry)| last == blank && now < retry)
        {
            return Ok(());
        }
        self.last_request = Some((blank, now + POLL_INTERVAL));
        // FB_BLANK_POWERDOWN and FB_BLANK_UNBLANK, respectively.
        let value = if blank { "4" } else { "0" };
        let mut last_error = None;
        if let Some(control) = self.control.take() {
            match set_panel_power(&control, value) {
                Ok(()) => {
                    self.control = Some(control);
                    return Ok(());
                }
                Err(e) => last_error = Some(e),
            }
        }
        // Prefer the native GPU control when firmware exposes a duplicate.
        for (name, kind, control) in sysfs::backlight_controls(Path::new("/")) {
            match set_panel_power(&control, value) {
                Ok(()) => {
                    if self.reported.as_ref() != Some(&control) {
                        eprintln!("brainstem: backlight: panel power through {name} ({kind})");
                        self.reported = Some(control.clone());
                    }
                    self.control = Some(control);
                    return Ok(());
                }
                Err(e) => last_error = Some(io::Error::new(e.kind(), format!("{name}: {e}"))),
            }
        }
        last_error.map_or(Ok(()), Err)
    }
}

fn set_panel_power(control: &Path, value: &str) -> io::Result<()> {
    if sysfs::read_text(control).as_deref() == Some(value) {
        return Ok(());
    }
    fs::write(control, value)
}

/// The lid as the evdev device itself reports it, for "Check hardware".
pub fn probe_lid() -> String {
    match Switch::discover(Instant::now()) {
        None => "lid: no input device reports a lid switch".into(),
        Some(switch) => match lid_bit(&switch.input, EVIOCGSW) {
            Ok(closed) => format!(
                "lid: \"{}\" at {} says {}",
                switch.name,
                switch.path.display(),
                if closed { "closed" } else { "open" }
            ),
            Err(e) => format!("lid: \"{}\" at {} cannot be read: {e}", switch.name, switch.path.display()),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source() -> Switch {
        Switch {
            input: File::open("/dev/null").unwrap(),
            path: PathBuf::from("/dev/null"),
            name: "test".into(),
            sync: Sync::Ready,
            pending: None,
            next_check: Instant::now(),
        }
    }

    #[test]
    fn lid_edges_are_ordered_and_duplicates_are_ignored() {
        let mut switch = source();
        let mut lid = Some(false);
        let mut changes = Vec::new();
        for value in [1, 1, 0] {
            switch.event(EV_SW, SW_LID, value, &mut lid, &mut changes);
            switch.event(EV_SYN, SYN_REPORT, 0, &mut lid, &mut changes);
        }
        assert_eq!(changes, [Some(true), Some(false)]);
    }

    #[test]
    fn only_an_exact_boot_flag_takes_a_missing_lid_as_open() {
        assert!(lid_open_requested("BOOT_IMAGE=/bzImage being.try=1 being.lid=open quiet"));
        assert!(lid_open_requested("being.lid=open"));
        for line in ["", "being.lid=closed", "being.lid=opened", "xbeing.lid=open", "being.lid = open"] {
            assert!(!lid_open_requested(line), "{line:?}");
        }
    }

    #[test]
    fn dropped_sync_marks_lid_unknown_and_ignores_queued_edges() {
        let mut switch = source();
        let mut lid = Some(false);
        let mut changes = Vec::new();
        switch.event(EV_SYN, SYN_DROPPED, 0, &mut lid, &mut changes);
        switch.event(EV_SW, SW_LID, 1, &mut lid, &mut changes);
        switch.event(EV_SYN, SYN_REPORT, 0, &mut lid, &mut changes);
        switch.event(EV_SW, SW_LID, 0, &mut lid, &mut changes);
        switch.event(EV_SYN, SYN_REPORT, 0, &mut lid, &mut changes);
        assert!(switch.sync == Sync::Query);
        assert_eq!(lid, None);
        assert_eq!(changes, [None]);
    }
}
