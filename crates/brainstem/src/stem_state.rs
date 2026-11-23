//! The brainstem's own short-term memory: `/run/being/stem-state`.
//!
//! A restarted brainstem (a crash, `kill -9`, an update of the binary) must
//! not reopen a muted mic, fake a nap, or reuse lid numbers organs still
//! hold. This small file, replaced whole on every change, carries that across
//! a restart. It lives in /run, so a reboot forgets it, and it names the boot
//! it was written in, so a stale copy from another boot is never believed.

use crate::machine::{Coarse, Resume};
use serde_json::{json, Value};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

pub const FILE: &str = "stem-state";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Saved {
    pub resume: Resume,
    /// The last lid reading number handed out.
    pub lid_seq: u64,
}

/// Where the file goes by default: next to the bus socket (on the appliance
/// that is /run/being, the brainstem's RuntimeDirectory).
/// A second brainstem on another socket in the same directory (the lab
/// does this) keeps a file of its own.
pub fn default_path(socket: &Path) -> PathBuf {
    let dir = socket.parent().unwrap_or(Path::new("."));
    match socket.file_name().and_then(|n| n.to_str()) {
        Some("bus.sock") | None => dir.join(FILE),
        Some(name) => dir.join(format!("{name}.{FILE}")),
    }
}

pub fn encode(saved: &Saved, boot: &str) -> String {
    let r = &saved.resume;
    json!({
        "boot": boot,
        "muted": r.muted,
        "state": r.coarse.name(),
        "natural_sleep": r.natural_sleep,
        "woke": r.woke,
        "lid_blocked": r.lid_blocked,
        "lid_seq": saved.lid_seq,
    })
    .to_string()
}

/// The saved state, if it is whole and from this boot.
pub fn decode(text: &str, boot: &str) -> Option<Saved> {
    let v: Value = serde_json::from_str(text).ok()?;
    if v.get("boot")?.as_str()? != boot {
        return None;
    }
    Some(Saved {
        resume: Resume {
            muted: v.get("muted")?.as_bool()?,
            coarse: Coarse::from_name(v.get("state")?.as_str()?)?,
            natural_sleep: v.get("natural_sleep")?.as_bool()?,
            woke: v.get("woke")?.as_bool()?,
            lid_blocked: v.get("lid_blocked")?.as_bool()?,
        },
        lid_seq: v.get("lid_seq")?.as_u64()?,
    })
}

pub fn load(path: &Path, boot: &str) -> Option<Saved> {
    decode(&fs::read_to_string(path).ok()?, boot)
}

/// Replace the file whole: written aside, then renamed over the old one.
/// /run is memory, so no sync is needed; a reboot discards it anyway.
pub fn save(path: &Path, saved: &Saved, boot: &str) -> io::Result<()> {
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    fs::write(&tmp, encode(saved, boot))?;
    fs::rename(&tmp, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn saved() -> Saved {
        Saved {
            resume: Resume { muted: true, coarse: Coarse::Asleep, natural_sleep: true, woke: true, lid_blocked: true },
            lid_seq: 41,
        }
    }

    #[test]
    fn round_trip_only_within_the_same_boot() {
        let dir = std::env::temp_dir().join(format!("being-stem-state-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = default_path(&dir.join("bus.sock"));
        assert_eq!(path, dir.join(FILE));
        assert_eq!(default_path(Path::new("/run/l5-native.sock")), Path::new("/run/l5-native.sock.stem-state"));
        assert_eq!(load(&path, "boot-1"), None, "nothing written yet");
        save(&path, &saved(), "boot-1").unwrap();
        assert_eq!(load(&path, "boot-1"), Some(saved()));
        assert_eq!(load(&path, "boot-2"), None, "another boot's state is not believed");
        fs::write(&path, "{\"boot\":\"boot-1\",\"muted\":true}").unwrap();
        assert_eq!(load(&path, "boot-1"), None, "a partial record is not believed");
        fs::write(&path, "half").unwrap();
        assert_eq!(load(&path, "boot-1"), None);
        let _ = fs::remove_dir_all(&dir);
    }
}
