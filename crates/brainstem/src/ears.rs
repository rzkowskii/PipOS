//! The microphone grant.  Capture is a reflex decision, not a voice decision.

use crate::machine::State;
use bus::Event;
use std::path::{Path, PathBuf};

const SAMPLE_SECONDS: f64 = 0.25;
const OPEN_GRACE_SECONDS: f64 = 1.0;
const DIAGNOSTIC_MARKER: &str = "run/being-diag/voice-stopped";
const BOOT_MEASUREMENT_MARKER: &str = "run/being-audio/mic-test-active";
pub const BREACH_MARKER: &str = "/run/being/mic-policy-breach";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Capture {
    Closed,
    Open,
    Unknown,
}

#[derive(Default)]
pub struct Ears {
    allowed: bool,
    seq: u64,
    root: PathBuf,
    target: Option<(String, u32)>,
    next_sample: f64,
    open_since: Option<f64>,
    tripped: bool,
}

impl Ears {
    pub fn for_body(hardware: bool, root: &Path) -> Self {
        let target = hardware.then(|| bus::body_map::at(root).mic).flatten()
            .map(|(card, device)| (card.to_string(), device));
        let marker = root.join(BREACH_MARKER.trim_start_matches('/'));
        Self { root: root.to_path_buf(), target, tripped: marker.exists(), ..Self::default() }
    }

    pub fn policy(&self) -> Event {
        Event::new("ears.policy").with("allowed", self.allowed).with("seq", self.seq)
    }

    pub fn latch_event(&self) -> Option<Event> {
        self.tripped.then(|| Event::new("body.mic_policy").with("breach", true).with("action", "safety_latched"))
    }

    pub fn update(&mut self, state: State, muted: bool, lid_blocked: bool) -> Vec<Event> {
        let hearing = matches!(
            state,
            State::Idle | State::Drowsy | State::Asleep | State::Attending
                | State::Listening | State::Thinking
        );
        let allowed = hearing && !muted && !lid_blocked && !self.tripped;
        if allowed == self.allowed {
            return Vec::new();
        }
        self.allowed = allowed;
        self.open_since = None;
        self.seq = self.seq.wrapping_add(1);
        vec![self.policy()]
    }

    /// Cross-check the kernel's real PCM state against a denied grant. This is
    /// sampled four times a second and trips only after a continuous second;
    /// missing or malformed procfs is unknown and never guessed open.
    pub fn tick(&mut self, now: f64) -> Option<Event> {
        if now < self.next_sample || self.target.is_none() || self.tripped {
            return None;
        }
        self.next_sample = now + SAMPLE_SECONDS;
        if self.allowed || self.root.join(DIAGNOSTIC_MARKER).exists()
            || self.root.join(BOOT_MEASUREMENT_MARKER).exists()
        {
            self.open_since = None;
            return None;
        }
        let reading = capture(&self.root, self.target.as_ref().unwrap());
        if reading != Capture::Open {
            self.open_since = None;
            return None;
        }
        let since = *self.open_since.get_or_insert(now);
        if now - since <= OPEN_GRACE_SECONDS {
            return None;
        }
        self.tripped = true;
        let marker = self.root.join(BREACH_MARKER.trim_start_matches('/'));
        if let Err(error) = std::fs::write(&marker, b"kernel capture stayed open after ears.policy denied it\n") {
            eprintln!("brainstem: could not persist microphone safety latch: {error}");
        }
        self.latch_event()
    }

    pub fn tripped(&self) -> bool { self.tripped }
}

fn numbered(name: &std::ffi::OsStr, prefix: &str) -> bool {
    name.to_str().and_then(|name| name.strip_prefix(prefix))
        .is_some_and(|digits| !digits.is_empty() && digits.bytes().all(|c| c.is_ascii_digit()))
}

fn capture(root: &Path, target: &(String, u32)) -> Capture {
    let Ok(cards) = std::fs::read_dir(root.join("proc/asound")) else { return Capture::Unknown };
    let mut saw_closed = false;
    let mut incomplete = false;
    let mut matched = false;
    for card in cards.flatten().take(64).filter(|e| numbered(&e.file_name(), "card")) {
        let path = card.path();
        let Ok(id) = std::fs::read_to_string(path.join("id")) else { incomplete = true; continue };
        if id.trim() != target.0 { continue; }
        matched = true;
        let pcm = path.join(format!("pcm{}c", target.1));
        let Ok(streams) = std::fs::read_dir(pcm) else { incomplete = true; continue };
        for stream in streams.flatten().take(64).filter(|e| numbered(&e.file_name(), "sub")) {
            match std::fs::read_to_string(stream.path().join("status")).ok()
                .and_then(|text| text.lines().next().map(str::trim).map(str::to_string)).as_deref()
            {
                Some("closed") => saw_closed = true,
                Some(state) if state.starts_with("state:") => return Capture::Open,
                _ => incomplete = true,
            }
        }
    }
    if matched && saw_closed && !incomplete { Capture::Closed } else { Capture::Unknown }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn root() -> PathBuf {
        let nonce = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let root = std::env::temp_dir().join(format!("being-ears-{nonce}"));
        std::fs::create_dir_all(root.join("proc/asound/card0/pcm0c/sub0")).unwrap();
        std::fs::create_dir_all(root.join("proc")).unwrap();
        std::fs::create_dir_all(root.join("sys/class/dmi/id")).unwrap();
        std::fs::create_dir_all(root.join("run/being")).unwrap();
        std::fs::write(root.join("proc/asound/card0/id"), "PCH\n").unwrap();
        std::fs::write(root.join("sys/class/dmi/id/product_name"), "MacBookPro9,2\n").unwrap();
        std::fs::write(root.join("proc/cmdline"), "quiet\n").unwrap();
        root
    }

    #[test]
    fn only_the_mapped_capture_counts_and_unknown_fails_inert() {
        let root = root();
        let target = ("PCH".to_string(), 0);
        std::fs::write(root.join("proc/asound/card0/pcm0c/sub0/status"), "closed\n").unwrap();
        assert_eq!(capture(&root, &target), Capture::Closed);
        std::fs::write(root.join("proc/asound/card0/pcm0c/sub0/status"), "state: RUNNING\n").unwrap();
        assert_eq!(capture(&root, &target), Capture::Open);
        std::fs::rename(root.join("proc/asound/card0"), root.join("proc/asound/card1")).unwrap();
        std::fs::create_dir(root.join("proc/asound/card0")).unwrap();
        std::fs::write(root.join("proc/asound/card0/id"), "Loopback\n").unwrap();
        assert_eq!(capture(&root, &target), Capture::Open);
        std::fs::remove_file(root.join("proc/asound/card1/pcm0c/sub0/status")).unwrap();
        assert_eq!(capture(&root, &target), Capture::Unknown);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn a_brief_grant_breaks_the_denied_capture_interval() {
        let root = root();
        std::fs::write(root.join("proc/asound/card0/pcm0c/sub0/status"), "state: RUNNING\n").unwrap();
        let mut ears = Ears::for_body(true, &root);
        assert!(ears.tick(0.0).is_none());
        ears.update(State::Idle, false, false);
        ears.update(State::Idle, true, false);
        assert!(ears.tick(1.25).is_none());
        assert!(ears.tick(2.5).is_some());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn denied_capture_needs_a_continuous_second_and_diagnostics_are_exempt() {
        let root = root();
        std::fs::write(root.join("proc/asound/card0/pcm0c/sub0/status"), "state: RUNNING\n").unwrap();
        let mut ears = Ears::for_body(true, &root);
        assert!(ears.tick(0.0).is_none());
        assert!(ears.tick(1.0).is_none());
        let breach = ears.tick(1.25).expect("continuous capture trips the latch");
        assert_eq!(breach.topic, "body.mic_policy");
        assert_eq!(breach.bool("breach"), Some(true));
        assert!(ears.tick(2.5).is_none(), "the live breach is reported only once");

        let mut restarted = Ears::for_body(true, &root);
        assert!(restarted.tripped());
        assert!(restarted.update(State::Idle, false, false).is_empty());
        assert_eq!(restarted.policy().bool("allowed"), Some(false));
        let snapshot = restarted.latch_event().expect("reconnected clients need the latched state");
        assert_eq!(snapshot.topic, breach.topic);
        assert_eq!(snapshot.data, breach.data);
        std::fs::remove_file(root.join(BREACH_MARKER.trim_start_matches('/'))).unwrap();

        std::fs::create_dir_all(root.join("run/being-diag")).unwrap();
        std::fs::write(root.join(DIAGNOSTIC_MARKER), "{}\n").unwrap();
        let mut diagnostics = Ears::for_body(true, &root);
        assert!(diagnostics.latch_event().is_none());
        assert!(diagnostics.tick(0.0).is_none());
        assert!(diagnostics.tick(2.0).is_none());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn boot_measurement_exemption_ends_when_measurement_finishes() {
        let root = root();
        assert!(Ears::for_body(false, &root).target.is_none());
        std::fs::write(root.join("proc/cmdline"), "being.mic_test=1\n").unwrap();
        assert!(Ears::for_body(true, &root).target.is_some());
        std::fs::write(root.join("proc/cmdline"), "being.try=1 being.mic_test=1\n").unwrap();
        std::fs::write(root.join("proc/asound/card0/pcm0c/sub0/status"), "state: RUNNING\n").unwrap();
        let mut ears = Ears::for_body(true, &root);
        let measuring = root.join(BOOT_MEASUREMENT_MARKER);
        std::fs::create_dir_all(measuring.parent().unwrap()).unwrap();
        std::fs::write(&measuring, "").unwrap();
        assert!(ears.tick(0.0).is_none());
        assert!(ears.tick(2.0).is_none());
        std::fs::remove_file(measuring).unwrap();
        assert!(ears.tick(2.25).is_none());
        assert!(ears.tick(3.5).is_some());
        std::fs::remove_dir_all(root).unwrap();
    }
}

