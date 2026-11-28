//! Notice USB storage without opening a device or reading a filesystem.

use bus::Event;
use std::collections::BTreeSet;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, TrySendError};
use std::time::Duration;

const BODY_POINTS: &[&str] = &["/", "/models", "/mind", "/bootstate"];

fn body_disks(root: &Path) -> io::Result<BTreeSet<PathBuf>> {
    let mut disks = BTreeSet::new();
    for line in fs::read_to_string(root.join("proc/self/mountinfo"))?.lines() {
        let mut fields = line.split_whitespace();
        let (Some(number), Some(point)) = (fields.nth(2), fields.nth(1)) else { continue };
        if !BODY_POINTS.contains(&point) {
            continue;
        }
        if let Ok(path) = root.join("sys/dev/block").join(number).canonicalize() {
            if path.join("partition").is_file() {
                if let Some(disk) = path.parent() {
                    disks.insert(disk.to_owned());
                }
            } else {
                disks.insert(path);
            }
        }
    }
    Ok(disks)
}

fn storage(root: &Path) -> io::Result<BTreeSet<PathBuf>> {
    let protected = body_disks(root)?;
    let disks = fs::read_dir(root.join("sys/class/block"))?;
    Ok(disks.flatten().take(256).filter_map(|disk| disk.path().canonicalize().ok()).filter(|path| {
        !protected.contains(path) && !path.join("partition").is_file() && path.components().any(|part| {
            part.as_os_str().to_str().and_then(|name| name.strip_prefix("usb")).is_some_and(|number| {
                !number.is_empty() && number.bytes().all(|byte| byte.is_ascii_digit())
            })
        })
    }).collect())
}

pub fn watch(root: PathBuf) -> io::Result<Receiver<Event>> {
    let (tx, rx) = mpsc::sync_channel(12);
    std::thread::Builder::new().name("brainstem-storage".into()).spawn(move || {
        let place = bus::body_map::at(&root).usb_storage_place;
        let mut previous: Option<BTreeSet<PathBuf>> = None;
        loop {
            if let Ok(current) = storage(&root) {
                if let Some(old) = &previous {
                    for attached in old.difference(&current).map(|_| false)
                        .chain(current.difference(old).map(|_| true))
                    {
                        let event = Event::new("body.plug").with("kind", "storage")
                            .with("place", place).with("attached", attached);
                        if matches!(tx.try_send(event), Err(TrySendError::Disconnected(_))) {
                            return;
                        }
                    }
                }
                previous = Some(current);
            }
            std::thread::sleep(Duration::from_secs(2));
        }
    })?;
    Ok(rx)
}
