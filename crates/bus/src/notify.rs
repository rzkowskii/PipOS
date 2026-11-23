//! The systemd service manager's notify socket: readiness, the watchdog and a
//! status line. Every organ uses this one helper, so a frozen loop anywhere
//! (the face's frame loop included) is caught by `WatchdogSec=`.
//!
//! Everything here is a single non-blocking datagram, so calling it from a
//! render or hub loop can never stall that loop. Outside systemd (a
//! development Mac, a test) it is a no-op.

use std::os::unix::net::{SocketAddr, UnixDatagram};
use std::time::{Duration, Instant};

/// A connection to `$NOTIFY_SOCKET`, or nothing when there is none.
pub struct Notifier {
    socket: Option<UnixDatagram>,
    /// The watchdog interval systemd asked for, if it asked for one for us.
    interval: Option<Duration>,
    last_kick: Option<Instant>,
}

/// The watchdog interval in `WATCHDOG_USEC`, if it is meant for `pid`.
/// systemd sets `WATCHDOG_PID` so a child that inherits the environment
/// does not pretend to be the service.
pub fn watchdog_interval(usec: Option<&str>, for_pid: Option<&str>, pid: u32) -> Option<Duration> {
    if let Some(owner) = for_pid {
        if owner.trim().parse::<u32>().ok() != Some(pid) {
            return None;
        }
    }
    let usec: u64 = usec?.trim().parse().ok().filter(|&n| n > 0)?;
    Some(Duration::from_micros(usec))
}

fn connect(target: &std::ffi::OsStr) -> Option<UnixDatagram> {
    #[cfg(target_os = "linux")]
    let address = {
        use std::os::linux::net::SocketAddrExt;
        use std::os::unix::ffi::OsStrExt;
        match target.as_bytes().strip_prefix(b"@") {
            Some(name) => SocketAddr::from_abstract_name(name),
            None => SocketAddr::from_pathname(target),
        }
    };
    #[cfg(not(target_os = "linux"))]
    let address = SocketAddr::from_pathname(target);

    let socket = UnixDatagram::unbound().ok()?;
    socket.connect_addr(&address.ok()?).ok()?;
    socket.set_nonblocking(true).ok()?;
    Some(socket)
}

impl Notifier {
    /// From the environment systemd gives a `Type=notify` or watchdog service.
    pub fn from_env() -> Notifier {
        let interval = watchdog_interval(
            std::env::var("WATCHDOG_USEC").ok().as_deref(),
            std::env::var("WATCHDOG_PID").ok().as_deref(),
            std::process::id(),
        );
        let socket = std::env::var_os("NOTIFY_SOCKET").and_then(|target| connect(&target));
        Notifier { socket, interval, last_kick: None }
    }

    /// A notifier that sends to `path`; for tests.
    pub fn to_path(path: &std::path::Path, interval: Option<Duration>) -> Notifier {
        Notifier { socket: connect(path.as_os_str()), interval, last_kick: None }
    }

    /// No socket: nothing will ever be sent.
    pub fn none() -> Notifier {
        Notifier { socket: None, interval: None, last_kick: None }
    }

    /// True if systemd watches this process.
    pub fn watched(&self) -> bool {
        self.socket.is_some() && self.interval.is_some()
    }

    pub fn interval(&self) -> Option<Duration> {
        self.interval
    }

    fn send(&self, message: &str) {
        if let Some(socket) = &self.socket {
            let _ = socket.send(message.as_bytes());
        }
    }

    /// Startup is finished (`Type=notify`).
    pub fn ready(&self) {
        self.send("READY=1");
    }

    /// Shutdown has begun.
    pub fn stopping(&self) {
        self.send("STOPPING=1");
    }

    /// One line for `systemctl status`.
    pub fn status(&self, line: &str) {
        let line: String = line.chars().filter(|c| *c != '\n').take(200).collect();
        self.send(&format!("STATUS={line}"));
    }

    /// "Still alive." Call it as often as you like (once per frame is fine):
    /// it sends at most four times per watchdog interval, and never more
    /// than twice a second.
    pub fn kick(&mut self) {
        self.kick_at(Instant::now());
    }

    pub fn kick_at(&mut self, now: Instant) {
        let Some(interval) = self.interval else { return };
        let every = (interval / 4).min(Duration::from_millis(500));
        if self.last_kick.is_some_and(|at| now.saturating_duration_since(at) < every) {
            return;
        }
        self.last_kick = Some(now);
        self.send("WATCHDOG=1");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_watchdog_interval_belongs_to_one_process() {
        assert_eq!(watchdog_interval(Some("5000000"), None, 7), Some(Duration::from_secs(5)));
        assert_eq!(watchdog_interval(Some("5000000"), Some("7"), 7), Some(Duration::from_secs(5)));
        assert_eq!(watchdog_interval(Some("5000000"), Some("8"), 7), None, "inherited by a child");
        assert_eq!(watchdog_interval(Some("0"), None, 7), None);
        assert_eq!(watchdog_interval(Some("soon"), None, 7), None);
        assert_eq!(watchdog_interval(None, None, 7), None);
    }

    #[test]
    fn kicks_are_rate_limited_and_messages_arrive() {
        let dir = std::env::temp_dir().join(format!("bus-notify-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("n");
        let _ = std::fs::remove_file(&path);
        let server = UnixDatagram::bind(&path).unwrap();
        server.set_read_timeout(Some(Duration::from_millis(200))).unwrap();
        let mut n = Notifier::to_path(&path, Some(Duration::from_secs(5)));
        assert!(n.watched());
        n.ready();
        let start = Instant::now();
        for i in 0..100 {
            n.kick_at(start + Duration::from_millis(i)); // a frame loop
        }
        n.kick_at(start + Duration::from_millis(600));
        n.status("all\nwell");
        let mut got = Vec::new();
        let mut buf = [0u8; 256];
        while let Ok(len) = server.recv(&mut buf) {
            got.push(String::from_utf8_lossy(&buf[..len]).into_owned());
        }
        assert_eq!(got, ["READY=1", "WATCHDOG=1", "WATCHDOG=1", "STATUS=allwell"]);
        let _ = std::fs::remove_dir_all(&dir);

        let mut quiet = Notifier::none();
        quiet.kick();
        quiet.ready();
        assert!(!quiet.watched());
    }
}
