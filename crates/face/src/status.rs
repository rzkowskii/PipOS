//! Privacy readings from the host: guest mode, network, SSH, ALSA capture,
//! and the internal camera's USB authorisation. A background
//! thread looks once a second, so no file or /proc read ever lands on a frame.
//!
//! Each reading is honest about its source:
//! - guest: `/run/being/guest` exists (the mind keeps it while guest mode is on);
//! - network: some interface other than loopback reports `operstate` up;
//! - ssh: an sshd session process (not the listener) is running.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::Arc;
use std::time::Duration;

pub const GUEST_FILE: &str = "/run/being/guest";
const NETWORK_MODE: &str = "/run/being-network/mode";

const GUEST: u8 = 1;
const NETWORK: u8 = 2;
const SSH: u8 = 4;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
#[repr(u8)]
pub enum Reading {
    #[default]
    Unknown = 0,
    Closed = 1,
    Open = 2,
}

impl Reading {
    fn decode(bits: u8) -> Self {
        match bits & 3 {
            1 => Self::Closed,
            2 => Self::Open,
            _ => Self::Unknown,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Privacy {
    pub guest: bool,
    pub network: bool,
    pub ssh: bool,
    pub mic: Reading,
    pub mic_latched: bool,
    pub camera_usb: Reading,
}

/// The latest reading, shared with the poller thread.
#[derive(Clone)]
pub struct Watch {
    bits: Arc<AtomicU8>,
}

impl Watch {
    /// Start looking. `roots` lets tests point it at a fake tree; the
    /// appliance passes "/".
    pub fn start(root: PathBuf) -> Watch {
        let bits = Arc::new(AtomicU8::new(0));
        let shared = Arc::clone(&bits);
        let spawned = std::thread::Builder::new().name("face-status".to_string()).spawn(move || loop {
            shared.store(encode(read(&root)), Ordering::Relaxed);
            std::thread::sleep(Duration::from_secs(1));
        });
        if let Err(e) = spawned {
            eprintln!("face: could not start the privacy watch: {e}");
        }
        Watch { bits }
    }

    /// No watching at all (a development window, a recording).
    pub fn none() -> Watch {
        Watch { bits: Arc::new(AtomicU8::new(0)) }
    }

    pub fn get(&self) -> Privacy {
        decode(self.bits.load(Ordering::Relaxed))
    }
}

fn encode(p: Privacy) -> u8 {
    (if p.guest { GUEST } else { 0 }) | (if p.network { NETWORK } else { 0 }) | (if p.ssh { SSH } else { 0 })
        | ((p.mic as u8) << 3) | ((p.camera_usb as u8) << 5) | (u8::from(p.mic_latched) << 7)
}

fn decode(bits: u8) -> Privacy {
    Privacy {
        guest: bits & GUEST != 0,
        network: bits & NETWORK != 0,
        ssh: bits & SSH != 0,
        mic: Reading::decode(bits >> 3),
        mic_latched: bits & (1 << 7) != 0,
        camera_usb: Reading::decode(bits >> 5),
    }
}

fn under(root: &Path, path: &str) -> PathBuf {
    root.join(path.trim_start_matches('/'))
}

pub fn read(root: &Path) -> Privacy {
    Privacy {
        guest: under(root, GUEST_FILE).exists(),
        network: network_up(root),
        ssh: ssh_session(root),
        mic: mic_stream(root),
        mic_latched: under(root, "/run/being/mic-policy-breach").is_file(),
        camera_usb: camera_usb(root),
    }
}

fn numbered(name: &std::ffi::OsStr, prefix: &str, suffix: &str) -> bool {
    name.to_str()
        .and_then(|name| name.strip_prefix(prefix))
        .and_then(|rest| rest.strip_suffix(suffix))
        .is_some_and(|digits| !digits.is_empty() && digits.bytes().all(|c| c.is_ascii_digit()))
}

fn mic_stream(root: &Path) -> Reading {
    let Ok(cards) = std::fs::read_dir(under(root, "/proc/asound")) else { return Reading::Unknown };
    let mut closed = false;
    let mut incomplete = false;
    for card in cards.flatten().take(64).filter(|entry| numbered(&entry.file_name(), "card", "")) {
        let card = card.path();
        let Ok(id) = std::fs::read_to_string(card.join("id")) else {
            incomplete = true;
            continue;
        };
        if id.trim() == "Loopback" {
            continue;
        }
        let Ok(pcms) = std::fs::read_dir(card) else {
            incomplete = true;
            continue;
        };
        for pcm in pcms.flatten().take(64).filter(|entry| numbered(&entry.file_name(), "pcm", "c")) {
            let Ok(streams) = std::fs::read_dir(pcm.path()) else {
                incomplete = true;
                continue;
            };
            for stream in streams.flatten().take(64).filter(|entry| numbered(&entry.file_name(), "sub", "")) {
                let value = std::fs::read_to_string(stream.path().join("status"));
                match value.as_deref().ok().and_then(|text| text.lines().next()).map(str::trim) {
                    Some("closed") => closed = true,
                    Some(state) if state.starts_with("state:") => return Reading::Open,
                    _ => incomplete = true,
                }
            }
        }
    }
    if closed && !incomplete { Reading::Closed } else { Reading::Unknown }
}

fn camera_usb(root: &Path) -> Reading {
    let Some((vendor, product)) = bus::body_map::at(root).camera else { return Reading::Unknown };
    let Ok(devices) = std::fs::read_dir(under(root, "/sys/bus/usb/devices")) else {
        return Reading::Unknown;
    };
    let mut blocked = false;
    let mut incomplete = false;
    for device in devices.flatten().take(128) {
        let path = device.path();
        let matches = |name: &str, value| std::fs::read_to_string(path.join(name))
            .is_ok_and(|contents| contents.trim() == value);
        if matches("idVendor", vendor) && matches("idProduct", product) {
            match std::fs::read_to_string(path.join("authorized")).as_deref().map(str::trim) {
                Ok("1") => return Reading::Open,
                Ok("0") => blocked = true,
                _ => incomplete = true,
            }
        }
    }
    if blocked && !incomplete { Reading::Closed } else { Reading::Unknown }
}

/// Any interface but loopback that the kernel calls up.
fn network_up(root: &Path) -> bool {
    let Ok(entries) = std::fs::read_dir(under(root, "/sys/class/net")) else { return false };
    entries.flatten().any(|entry| {
        entry.file_name() != "lo"
            && std::fs::read_to_string(entry.path().join("operstate")).is_ok_and(|state| state.trim() == "up")
    })
}

/// The network mode the mind last enforced, for the health overlay.
pub fn network_mode(root: &Path) -> Option<String> {
    let mode = std::fs::read_to_string(under(root, NETWORK_MODE)).ok()?;
    let mode = mode.trim();
    (!mode.is_empty() && mode.len() < 32 && mode.bytes().all(|b| b.is_ascii_graphic())).then(|| mode.to_string())
}

/// An sshd process serving a login: "sshd: root@pts/0", "sshd: root [priv]",
/// "sshd-session: root@notty". The listener ("sshd: /usr/sbin/sshd -D
/// [listener] ...") does not count.
pub fn is_ssh_session(cmdline: &[u8]) -> bool {
    let line: Vec<u8> = cmdline.iter().map(|&b| if b == 0 { b' ' } else { b }).collect();
    let line = String::from_utf8_lossy(&line);
    let line = line.trim();
    let session = line.starts_with("sshd-session") || line.starts_with("sshd:");
    session && !line.contains("[listener]") && (line.contains('@') || line.contains("[priv]") || line.contains("[net]"))
}

fn ssh_session(root: &Path) -> bool {
    let Ok(entries) = std::fs::read_dir(under(root, "/proc")) else { return false };
    entries.flatten().any(|entry| {
        let numeric = entry.file_name().to_str().is_some_and(|n| n.bytes().all(|b| b.is_ascii_digit()));
        numeric && std::fs::read(entry.path().join("cmdline")).is_ok_and(|c| is_ssh_session(&c))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tree() -> tempdir::Dir {
        tempdir::Dir::new()
    }

    mod tempdir {
        pub struct Dir(pub std::path::PathBuf);
        impl Dir {
            pub fn new() -> Dir {
                use std::sync::atomic::{AtomicU32, Ordering};
                static N: AtomicU32 = AtomicU32::new(0);
                let path = std::env::temp_dir().join(format!(
                    "face-status-{}-{}",
                    std::process::id(),
                    N.fetch_add(1, Ordering::Relaxed)
                ));
                std::fs::create_dir_all(&path).unwrap();
                Dir(path)
            }
        }
        impl Drop for Dir {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
    }

    fn write(root: &Path, path: &str, text: &[u8]) {
        let path = under(root, path);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }

    #[test]
    fn readings_come_from_their_own_sources() {
        let dir = tree();
        let root = &dir.0;
        assert_eq!(read(root), Privacy::default());
        write(root, "/sys/class/net/lo/operstate", b"unknown\n");
        write(root, "/sys/class/net/eth0/operstate", b"down\n");
        write(root, "/proc/1/cmdline", b"/sbin/init\0");
        write(root, "/proc/200/cmdline", b"sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups\0");
        assert_eq!(read(root), Privacy::default(), "a listener and a down link are not activity");
        write(root, "/sys/class/net/eth0/operstate", b"up\n");
        write(root, "/proc/201/cmdline", b"sshd-session: root@notty\0");
        write(root, "/run/being/guest", b"");
        assert_eq!(read(root), Privacy { guest: true, network: true, ssh: true, ..Privacy::default() });
        write(root, "/run/being/mic-policy-breach", b"latched\n");
        let latched = read(root);
        assert!(latched.mic_latched);
        assert_eq!(decode(encode(latched)), latched);
    }

    #[test]
    fn loopback_alone_is_not_a_network() {
        let dir = tree();
        write(&dir.0, "/sys/class/net/lo/operstate", b"up\n");
        assert!(!read(&dir.0).network);
    }

    #[test]
    fn ssh_sessions_are_told_from_the_listener() {
        assert!(is_ssh_session(b"sshd: root@pts/0"));
        assert!(is_ssh_session(b"sshd: root [priv]\0\0"));
        assert!(is_ssh_session(b"sshd-session: root@notty"));
        assert!(!is_ssh_session(b"sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups"));
        assert!(!is_ssh_session(b"/usr/sbin/sshd\0-D\0-f\0/run/being-lab-dev/sshd_config\0"));
        assert!(!is_ssh_session(b"python3\0-m\0mind\0"));
    }

    #[test]
    fn the_network_mode_is_one_short_word() {
        let dir = tree();
        assert_eq!(network_mode(&dir.0), None);
        write(&dir.0, NETWORK_MODE, b"local-only\n");
        assert_eq!(network_mode(&dir.0).as_deref(), Some("local-only"));
        write(&dir.0, NETWORK_MODE, b"two words\n");
        assert_eq!(network_mode(&dir.0), None);
    }

    #[test]
    fn bits_round_trip() {
        for bits in 0..8 {
            assert_eq!(encode(decode(bits)), bits);
        }
    }
}
