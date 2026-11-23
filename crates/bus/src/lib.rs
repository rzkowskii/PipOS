//! The being's nervous system: one JSON object per line over a Unix socket.
//! See docs/PROTOCOL.md for the topics.

use serde_json::{Map, Value};
use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::time::Duration;

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
}

impl Client {
    pub fn connect(path: &Path, name: &str, subs: &[&str]) -> io::Result<Client> {
        let stream = UnixStream::connect(path)?;
        // A stalled hub must never freeze an organ's own loop.
        stream.set_write_timeout(Some(Duration::from_millis(100)))?;
        let reader = stream.try_clone()?;
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(reader).lines() {
                let Ok(line) = line else { break };
                if let Some(ev) = Event::parse(&line) {
                    if tx.send(ev).is_err() {
                        break;
                    }
                }
            }
        });
        let mut client = Client { stream, rx };
        let subs: Vec<Value> = subs.iter().map(|s| Value::String(s.to_string())).collect();
        client.send(&Event::new("hello").with("name", name).with("sub", Value::Array(subs)))?;
        Ok(client)
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
}
