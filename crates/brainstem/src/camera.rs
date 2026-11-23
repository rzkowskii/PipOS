//! The camera's grant and short-lived face position, independent of speech.

use crate::machine::State;
use bus::Event;
use serde_json::Value;

const PRESENCE_TIMEOUT: f64 = 1.5;

#[derive(Default)]
pub struct Camera {
    peer: Option<u64>,
    requested: bool,
    allowed: bool,
    seq: u64,
    presence_until: Option<f64>,
}

impl Camera {
    pub fn identified(&mut self, id: u64, name: &str, lid_seq: u64) -> Vec<Event> {
        if name == "eyes" {
            self.peer = Some(id);
            self.requested = false;
            self.set_allowed(false, lid_seq)
        } else {
            self.disconnected(id, lid_seq)
        }
    }

    pub fn disconnected(&mut self, id: u64, lid_seq: u64) -> Vec<Event> {
        if self.peer != Some(id) {
            return Vec::new();
        }
        self.peer = None;
        self.requested = false;
        self.set_allowed(false, lid_seq)
    }

    pub fn policy(&self) -> Event {
        Event::new("camera.policy")
            .with("allowed", self.allowed)
            .with("seq", self.seq)
    }

    pub fn request(&mut self, id: u64, ev: &Event) {
        if self.peer == Some(id) {
            self.requested = ev.bool("enabled") == Some(true);
        }
    }

    pub fn update(&mut self, state: State, lid_blocked: bool, lid_seq: u64, now: f64) -> Vec<Event> {
        let can_see = matches!(
            state,
            State::Idle | State::Attending | State::Listening | State::Thinking | State::Speaking
        );
        let mut out = self.set_allowed(self.requested && !lid_blocked && can_see, lid_seq);
        if self.presence_until.is_some_and(|until| now >= until) {
            out.extend(self.clear(lid_seq));
        }
        out
    }

    pub fn observe(&mut self, id: u64, ev: &Event, lid_seq: u64, now: f64) -> Vec<Event> {
        if self.peer != Some(id)
            || !self.allowed
            || ev.data.get("lid_seq").and_then(Value::as_u64) != Some(lid_seq)
            || ev.data.get("camera_seq").and_then(Value::as_u64) != Some(self.seq)
        {
            return Vec::new();
        }
        if ev.bool("present") == Some(false) {
            return self.clear(lid_seq);
        }
        let (Some(true), Some(x), Some(y)) = (ev.bool("present"), ev.f64("x"), ev.f64("y")) else {
            return Vec::new();
        };
        self.presence_until = Some(now + PRESENCE_TIMEOUT);
        vec![
            Event::new("face.gaze").with("x", x.clamp(-1.0, 1.0)).with("y", y.clamp(-1.0, 1.0)),
            presence(true, lid_seq),
        ]
    }

    fn set_allowed(&mut self, allowed: bool, lid_seq: u64) -> Vec<Event> {
        if self.allowed == allowed {
            return Vec::new();
        }
        self.allowed = allowed;
        self.seq += 1;
        let mut out = vec![self.policy()];
        if !allowed {
            out.extend(self.clear(lid_seq));
        }
        out
    }

    fn clear(&mut self, lid_seq: u64) -> Vec<Event> {
        if self.presence_until.take().is_some() {
            vec![Event::new("face.gaze").with("present", false), presence(false, lid_seq)]
        } else {
            Vec::new()
        }
    }
}

fn presence(present: bool, lid_seq: u64) -> Event {
    Event::new("body.presence").with("present", present).with("lid_seq", lid_seq)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capture_stops_in_every_resting_state_and_old_samples_expire() {
        let mut camera = Camera::default();
        camera.identified(1, "eyes", 0);
        camera.request(1, &Event::new("camera.request").with("enabled", true));
        assert!(camera.update(State::Waking, false, 0, 0.0).is_empty());
        assert_eq!(camera.update(State::Idle, false, 0, 1.5)[0].bool("allowed"), Some(true));
        let sample = Event::new("see.presence")
            .with("present", true)
            .with("x", 2.0)
            .with("y", -2.0)
            .with("camera_seq", 1u64)
            .with("lid_seq", 0u64);
        let detected = camera.observe(1, &sample, 0, 2.0);
        let gaze = &detected[0];
        assert_eq!((gaze.f64("x"), gaze.f64("y")), (Some(1.0), Some(-1.0)));
        assert_eq!(detected[1].to_line(), presence(true, 0).to_line());
        assert!(camera.update(State::Idle, false, 0, 3.0).is_empty());
        let cleared = camera.update(State::Idle, false, 0, 3.5);
        assert_eq!(cleared[0].bool("present"), Some(false));
        assert_eq!(cleared[1].to_line(), presence(false, 0).to_line());
        assert!(camera.update(State::Idle, false, 0, 4.0).is_empty());
        for rest in [State::Drowsy, State::Asleep, State::Waking] {
            assert_eq!(camera.update(rest, false, 0, 5.0)[0].bool("allowed"), Some(false));
            assert_eq!(camera.update(State::Idle, false, 0, 6.0)[0].bool("allowed"), Some(true));
        }
    }
}
