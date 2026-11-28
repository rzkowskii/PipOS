//! Apply capture policy to the mapped internal USB camera without blocking the hub.

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread::{self, Thread};
use std::time::Duration;

const NODE_BITS: u32 = 12;
const NODE_MASK: u64 = (1 << NODE_BITS) - 1;

fn cameras(root: &Path, ids: (&str, &str)) -> io::Result<Vec<PathBuf>> {
    let devices = fs::read_dir(root.join("sys/bus/usb/devices"))?;
    Ok(devices.flatten().filter_map(|entry| {
        let path = entry.path();
        let matches = |name, expected| {
            fs::read_to_string(path.join(name)).is_ok_and(|actual| actual.trim() == expected)
        };
        (matches("idVendor", ids.0) && matches("idProduct", ids.1)).then(|| path.canonicalize().ok()).flatten()
    }).collect())
}

fn authorise(camera: &Path, allowed: bool) -> io::Result<()> {
    let path = camera.join("authorized");
    let wanted = if allowed { "1" } else { "0" };
    if fs::read_to_string(&path)?.trim() == wanted {
        return Ok(());
    }
    OpenOptions::new().write(true).open(&path)?.write_all(wanted.as_bytes())?;
    if fs::read_to_string(path)?.trim() != wanted {
        return Err(io::Error::other("kernel camera authorization did not change"));
    }
    Ok(())
}

fn video_node(root: &Path, camera: &Path) -> Option<u64> {
    fs::read_dir(root.join("sys/class/video4linux")).ok()?.flatten().filter_map(|entry| {
        let number = entry.file_name().to_str()?.strip_prefix("video")?.parse::<u64>().ok()?;
        let path = entry.path();
        (number < NODE_MASK && path.join("device").canonicalize().ok()?.starts_with(camera)
            && fs::read_to_string(path.join("index")).is_ok_and(|index| index.trim() == "0")).then_some(number)
    }).min()
}

pub fn close_mapped(root: &Path) -> io::Result<()> {
    if let Some(ids) = bus::body_map::at(root).camera {
        let devices = match cameras(root, ids) {
            Ok(devices) => devices,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error),
        };
        let mut first_error = None;
        for camera in devices {
            if let Err(error) = authorise(&camera, false) {
                if error.kind() != io::ErrorKind::NotFound && first_error.is_none() {
                    first_error = Some(error);
                }
            }
        }
        if let Some(error) = first_error {
            return Err(error);
        }
    }
    Ok(())
}

#[derive(Default)]
struct Mailbox {
    // Low bit is the desired authorization. Upper bits identify this request.
    desired: AtomicU64,
    // Request encoded above a nonzero video-node index.
    ready: AtomicU64,
    stop: AtomicBool,
}

pub struct Gate {
    mailbox: Arc<Mailbox>,
    worker: Thread,
    request: u64,
}

impl Gate {
    pub fn start(root: PathBuf, ids: (&'static str, &'static str)) -> io::Result<Self> {
        let mailbox = Arc::new(Mailbox::default());
        let shared = Arc::clone(&mailbox);
        let worker = thread::Builder::new().name("camera-usb".into()).spawn(move || watch(root, ids, shared))?;
        Ok(Self { mailbox, worker: worker.thread().clone(), request: 0 })
    }

    pub fn set(&mut self, allowed: bool) -> Option<u64> {
        let desired = u64::from(allowed);
        if self.request & 1 != desired {
            self.request = ((self.request & !1) + 2) | desired;
            self.mailbox.desired.store(self.request, Ordering::Release);
            self.worker.unpark();
        }
        let ready = self.mailbox.ready.load(Ordering::Acquire);
        (allowed && ready >> NODE_BITS == self.request).then_some(ready & NODE_MASK)
            .and_then(|node| node.checked_sub(1))
    }
}

impl Drop for Gate {
    fn drop(&mut self) {
        self.mailbox.stop.store(true, Ordering::Release);
        self.worker.unpark();
    }
}

fn watch(root: PathBuf, ids: (&str, &str), mailbox: Arc<Mailbox>) {
    let mut previous = 0;
    let mut reported = false;
    loop {
        let request = mailbox.desired.load(Ordering::Acquire);
        let stopping = mailbox.stop.load(Ordering::Acquire);
        let on = !stopping && request & 1 == 1;
        // If closing and reopening were coalesced, enforce the intermediate
        // revocation before granting a new device epoch.
        let reset = previous & 1 == 1 && previous != request;
        let result = (|| -> io::Result<Option<u64>> {
            let devices = cameras(&root, ids)?;
            if reset || !on || devices.len() != 1 {
                let mut failed = None;
                for camera in &devices {
                    if let Err(error) = authorise(camera, false) {
                        failed = Some(error);
                    }
                }
                if let Some(error) = failed {
                    return Err(error);
                }
            }
            if on && devices.len() == 1
                && mailbox.desired.load(Ordering::Acquire) == request
                && !mailbox.stop.load(Ordering::Acquire)
            {
                let camera = &devices[0];
                authorise(camera, true)?;
                let node = video_node(&root, camera);
                // A lid or consent change can arrive while the kernel is
                // bringing the USB interface up. Undo that stale grant.
                if mailbox.desired.load(Ordering::Acquire) != request || mailbox.stop.load(Ordering::Acquire) {
                    authorise(camera, false)?;
                    return Ok(None);
                }
                return Ok(node);
            }
            Ok(None)
        })();
        let ready = match result {
            Ok(node) => {
                reported = false;
                previous = request;
                node.map_or(0, |node| (request << NODE_BITS) | (node + 1))
            }
            Err(error) => {
                if !reported && error.kind() != io::ErrorKind::NotFound {
                    eprintln!("brainstem: could not apply the internal camera policy: {error}");
                    reported = true;
                }
                0
            }
        };
        mailbox.ready.store(ready, Ordering::Release);
        if stopping {
            return;
        }
        let interval = match (on, ready) {
            (true, 0) => 250,
            (true, _) => 750,
            (false, _) => 2_000,
        };
        thread::park_timeout(Duration::from_millis(interval));
    }
}
