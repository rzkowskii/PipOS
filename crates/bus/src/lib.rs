//! The being's nervous system: one JSON object per line over a Unix socket.
//! See docs/PROTOCOL.md for the topics.

pub mod body_drives;
pub mod body_map;
pub mod notify;

use serde_json::{Map, Value};
use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::sync::Arc;
use std::time::{Duration, Instant};

/// Longest line either end of the bus accepts. Longer lines are dropped whole.
pub const MAX_LINE: usize = 64 * 1024;

/// Same-boot timestamp for events exchanged between Linux processes.
#[cfg(target_os = "linux")]
pub fn monotonic_millis() -> Option<u64> {
    let mut value = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    // SAFETY: the writable timespec remains valid for this clock call.
    if unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut value) } != 0 {
        return None;
    }
    let seconds = u64::try_from(value.tv_sec).ok()?;
    let millis = u64::try_from(value.tv_nsec).ok()? / 1_000_000;
    seconds.checked_mul(1_000)?.checked_add(millis)
}

#[cfg(not(target_os = "linux"))]
pub fn monotonic_millis() -> Option<u64> {
    None
}

/// What one capped read found.
#[derive(Debug, PartialEq, Eq)]
pub enum LineRead {
    /// The other end closed.
    Eof,
    /// One line (or the last bytes before the end), in the buffer.
    Line,
    /// A line longer than the cap. It was read and thrown away, so memory
    /// never grows past the cap however long the line is.
    TooLong,
}

/// Read one line of at most `max` bytes (newline included) into `buf`,
/// which is cleared first.
pub fn read_line_capped<R: BufRead>(reader: &mut R, buf: &mut Vec<u8>, max: usize) -> io::Result<LineRead> {
    buf.clear();
    let limit = u64::try_from(max).unwrap_or(u64::MAX).saturating_add(1);
    let n = io::Read::take(&mut *reader, limit).read_until(b'\n', buf)?;
    if n == 0 {
        return Ok(LineRead::Eof);
    }
    if buf.last() == Some(&b'\n') && buf.len() <= max {
        return Ok(LineRead::Line);
    }
    if buf.len() <= max {
        // The stream ended in the middle of a line: keep what came.
        return Ok(LineRead::Line);
    }
    // Too long: skip to the end of this line without keeping it.
    buf.clear();
    loop {
        let (done, used) = {
            let chunk = reader.fill_buf()?;
            if chunk.is_empty() {
                return Ok(LineRead::TooLong);
            }
            match chunk.iter().position(|&b| b == b'\n') {
                Some(i) => (true, i + 1),
                None => (false, chunk.len()),
            }
        };
        reader.consume(used);
        if done {
            return Ok(LineRead::TooLong);
        }
    }
}

/// One message on the bus: a topic plus free-form fields.
#[derive(Debug, Clone, PartialEq)]
pub struct Event {
    pub topic: String,
    pub data: Map<String, Value>,
}

impl Event {
    pub fn new(topic: impl Into<String>) -> Self {
        Event { topic: topic.into(), data: Map::new() }
    }

    pub fn with(mut self, key: &str, value: impl Into<Value>) -> Self {
        self.data.insert(key.to_string(), value.into());
        self
    }

    pub fn str(&self, key: &str) -> Option<&str> {
        self.data.get(key).and_then(Value::as_str)
    }

    pub fn f64(&self, key: &str) -> Option<f64> {
        self.data.get(key).and_then(Value::as_f64)
    }

    pub fn bool(&self, key: &str) -> Option<bool> {
        self.data.get(key).and_then(Value::as_bool)
    }

    /// Serialise as one line, newline included.
    pub fn to_line(&self) -> String {
        let mut obj = Map::new();
        obj.insert("t".to_string(), Value::String(self.topic.clone()));
        for (k, v) in &self.data {
            if k != "t" {
                obj.insert(k.clone(), v.clone());
            }
        }
        let mut line = Value::Object(obj).to_string();
        line.push('\n');
        line
    }

    /// Parse one line. Anything that is not an object with a string `t` is dropped.
    pub fn parse(line: &str) -> Option<Event> {
        let line = line.trim();
        if line.is_empty() {
            return None;
        }
        let Value::Object(mut obj) = serde_json::from_str::<Value>(line).ok()? else {
            return None;
        };
        let Value::String(topic) = obj.remove("t")? else {
            return None;
        };
        Some(Event { topic, data: obj })
    }
}

/// Where the bus socket lives. Same rule as the Python side.
pub fn socket_path() -> PathBuf {
    if let Some(p) = std::env::var_os("BEING_BUS") {
        return PathBuf::from(p);
    }
    if let Some(dir) = std::env::var_os("XDG_RUNTIME_DIR") {
        return PathBuf::from(dir).join("being").join("bus.sock");
    }
    let user = std::env::var("USER").unwrap_or_else(|_| "being".to_string());
    PathBuf::from(format!("/tmp/being-{user}/bus.sock"))
}

/// A subscription is an exact topic, a prefix ending in `.*`, or `*`.
pub fn topic_matches(pattern: &str, topic: &str) -> bool {
    if pattern == "*" {
        return true;
    }
    if let Some(prefix) = pattern.strip_suffix(".*") {
        return topic.len() > prefix.len()
            && topic.starts_with(prefix)
            && topic.as_bytes()[prefix.len()] == b'.';
    }
    pattern == topic
}

/// The other end went away.
#[derive(Debug)]
pub struct Closed;

/// An organ's connection to the brainstem.
pub struct Client {
    stream: UnixStream,
    rx: Receiver<Event>,
    joined: Arc<AtomicBool>,
}

impl Client {
    /// Connect and say hello. This never waits for the hub's answer, so an
    /// organ's own loop (the face's frame loop) cannot stall on a busy hub;
    /// use `joined` or `wait_joined` to know when the subscription is live.
    pub fn connect(path: &Path, name: &str, subs: &[&str]) -> io::Result<Client> {
        let stream = UnixStream::connect(path)?;
        // A stalled hub must never freeze an organ's own loop.
        stream.set_write_timeout(Some(Duration::from_millis(100)))?;
        let reader = stream.try_clone()?;
        let (tx, rx) = mpsc::channel();
        let joined = Arc::new(AtomicBool::new(false));
        let seen = joined.clone();
        std::thread::spawn(move || {
            let mut reader = BufReader::new(reader);
            let mut buf = Vec::new();
            loop {
                match read_line_capped(&mut reader, &mut buf, MAX_LINE) {
                    Ok(LineRead::Line) => {}
                    Ok(LineRead::TooLong) => continue,
                    Ok(LineRead::Eof) | Err(_) => break,
                }
                let Ok(line) = std::str::from_utf8(&buf) else { continue };
                if let Some(ev) = Event::parse(line) {
                    // The hub's receipt for our hello is for this client, not the organ.
                    if ev.topic == "hello.ok" {
                        seen.store(true, Ordering::SeqCst);
                        continue;
                    }
                    if tx.send(ev).is_err() {
                        break;
                    }
                }
            }
        });
        let mut client = Client { stream, rx, joined };
        let subs: Vec<Value> = subs.iter().map(|s| Value::String(s.to_string())).collect();
        client.send(&Event::new("hello").with("name", name).with("sub", Value::Array(subs)).with("ack", true))?;
        Ok(client)
    }

    /// Connect and wait up to `timeout` for the hub to confirm the hello, so
    /// nothing sent to us after this returns can be missed. A hub too old to
    /// answer is accepted after the timeout.
    pub fn connect_joined(path: &Path, name: &str, subs: &[&str], timeout: Duration) -> io::Result<Client> {
        let client = Client::connect(path, name, subs)?;
        client.wait_joined(timeout);
        Ok(client)
    }

    /// True once the hub has confirmed our hello (`hello.ok`).
    pub fn joined(&self) -> bool {
        self.joined.load(Ordering::SeqCst)
    }

    /// Wait up to `timeout` for `joined`. Returns whether it came.
    pub fn wait_joined(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while !self.joined() {
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(2));
        }
        true
    }

    pub fn send(&mut self, ev: &Event) -> io::Result<()> {
        self.stream.write_all(ev.to_line().as_bytes())
    }

    /// Next waiting event, `None` if there is none, `Closed` if the hub is gone.
    pub fn try_recv(&self) -> Result<Option<Event>, Closed> {
        match self.rx.try_recv() {
            Ok(ev) => Ok(Some(ev)),
            Err(TryRecvError::Empty) => Ok(None),
            Err(TryRecvError::Disconnected) => Err(Closed),
        }
    }

    /// Wait up to `timeout` for an event.
    pub fn recv_timeout(&self, timeout: Duration) -> Result<Option<Event>, Closed> {
        match self.rx.recv_timeout(timeout) {
            Ok(ev) => Ok(Some(ev)),
            Err(mpsc::RecvTimeoutError::Timeout) => Ok(None),
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(Closed),
        }
    }
}

impl Drop for Client {
    /// Close both directions, so the hub sees the organ leave and the reader
    /// thread (which holds its own handle to the socket) ends.
    fn drop(&mut self) {
        let _ = self.stream.shutdown(std::net::Shutdown::Both);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip() {
        let ev = Event::new("hear.final").with("text", "hello there").with("n", 3);
        let line = ev.to_line();
        assert!(line.ends_with('\n'));
        let back = Event::parse(&line).unwrap();
        assert_eq!(back.topic, "hear.final");
        assert_eq!(back.str("text"), Some("hello there"));
        assert_eq!(back.f64("n"), Some(3.0));
    }

    #[test]
    fn rejects_junk() {
        assert!(Event::parse("").is_none());
        assert!(Event::parse("not json").is_none());
        assert!(Event::parse("[1,2]").is_none());
        assert!(Event::parse("{\"x\":1}").is_none());
        assert!(Event::parse("{\"t\":5}").is_none());
    }

    #[test]
    fn matching() {
        assert!(topic_matches("*", "anything.at.all"));
        assert!(topic_matches("face.*", "face.express"));
        assert!(!topic_matches("face.*", "face"));
        assert!(!topic_matches("face.*", "facet.x"));
        assert!(topic_matches("say.level", "say.level"));
        assert!(!topic_matches("say.level", "say.levels"));
    }

    #[test]
    fn capped_reads_drop_long_lines_whole_and_keep_the_next() {
        let long = "x".repeat(100);
        let input = format!("short\n{long}\nafter\ntail");
        let mut reader = io::BufReader::with_capacity(8, input.as_bytes());
        let mut buf = Vec::new();
        assert_eq!(read_line_capped(&mut reader, &mut buf, 16).unwrap(), LineRead::Line);
        assert_eq!(buf, b"short\n");
        assert_eq!(read_line_capped(&mut reader, &mut buf, 16).unwrap(), LineRead::TooLong);
        assert!(buf.is_empty());
        assert_eq!(read_line_capped(&mut reader, &mut buf, 16).unwrap(), LineRead::Line);
        assert_eq!(buf, b"after\n");
        assert_eq!(read_line_capped(&mut reader, &mut buf, 16).unwrap(), LineRead::Line);
        assert_eq!(buf, b"tail");
        assert_eq!(read_line_capped(&mut reader, &mut buf, 16).unwrap(), LineRead::Eof);
        // Exactly at the cap is still a line; one more byte is not.
        let mut reader = io::BufReader::new("abc\nabcd\n".as_bytes());
        assert_eq!(read_line_capped(&mut reader, &mut buf, 4).unwrap(), LineRead::Line);
        assert_eq!(read_line_capped(&mut reader, &mut buf, 4).unwrap(), LineRead::TooLong);
        // A long line that never ends is dropped without growing the buffer.
        let endless = "y".repeat(1000);
        let mut reader = io::BufReader::with_capacity(8, endless.as_bytes());
        assert_eq!(read_line_capped(&mut reader, &mut buf, 16).unwrap(), LineRead::TooLong);
        assert!(buf.capacity() < 1000);
    }

    #[test]
    fn a_client_swallows_the_hello_receipt_and_reports_joined() {
        use std::os::unix::net::UnixListener;
        let dir = std::env::temp_dir().join(format!("bus-ack-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("s");
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path).unwrap();
        let hub = std::thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            let hello = Event::parse(&line).unwrap();
            assert_eq!(hello.topic, "hello");
            assert_eq!(hello.bool("ack"), Some(true));
            let mut w = stream;
            w.write_all(Event::new("hello.ok").to_line().as_bytes()).unwrap();
            w.write_all(Event::new("state.changed").with("state", "idle").to_line().as_bytes()).unwrap();
            std::thread::sleep(Duration::from_millis(200));
        });
        let client = Client::connect_joined(&path, "face", &["state.changed"], Duration::from_secs(2)).unwrap();
        assert!(client.joined());
        let ev = client.recv_timeout(Duration::from_secs(2)).unwrap().unwrap();
        assert_eq!(ev.topic, "state.changed", "the receipt is not passed on");
        hub.join().unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }
}
