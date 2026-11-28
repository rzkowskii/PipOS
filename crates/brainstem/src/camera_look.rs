//! A single deliberate look during an addressed turn. Neither images nor
//! captions are broadcast by the camera route.

use bus::Event;
use serde_json::Value;

const DEADLINE: f64 = 32.0;
const MAX_DESCRIPTION_BYTES: usize = 240;

pub type Delivery = (u64, Event);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Grant {
    pub eyes: u64,
    pub lid_seq: u64,
    pub camera_seq: u64,
}

struct Request {
    id: String,
    mind: u64,
    grant: Grant,
    deadline: f64,
}

impl Request {
    fn cancel(&self) -> Delivery {
        (self.grant.eyes, Event::new("camera.look_cancel").with("id", self.id.as_str()))
    }

    fn result(&self, status: &str) -> Delivery {
        result(self.mind, &self.id, self.grant.lid_seq, status)
    }
}

#[derive(Default)]
pub struct Look {
    request: Option<Request>,
    used: Option<(u64, String)>,
}

impl Look {
    /// The caller supplies the current Mind peer and a confirmed-open lid
    /// during an unmuted thinking turn.
    pub fn begin(&mut self, mind: u64, ev: &Event, lid_seq: u64, grant: Option<Grant>, now: f64) -> Vec<Delivery> {
        let Some(id) = nonce(ev) else { return vec![] };
        if ev.data.len() != 2 || seq(ev, "lid_seq") != Some(lid_seq) {
            return vec![];
        }
        if let Some((used_mind, used_id)) = &self.used {
            return if (*used_mind, used_id.as_str()) == (mind, id) {
                vec![]
            } else {
                vec![result(mind, id, lid_seq, "unavailable")]
            };
        }
        self.used = Some((mind, id.to_owned()));
        let Some(grant) = grant else { return vec![result(mind, id, lid_seq, "disabled")] };
        self.request = Some(Request { id: id.to_owned(), mind, grant, deadline: now + DEADLINE });
        vec![(grant.eyes, Event::new("camera.look").with("id", id)
            .with("lid_seq", grant.lid_seq).with("camera_seq", grant.camera_seq))]
    }

    pub fn complete(&mut self, eyes: u64, ev: &Event, grant: Option<Grant>, now: f64) -> Option<Delivery> {
        let request = self.request.as_ref()?;
        if grant != Some(request.grant) || eyes != request.grant.eyes || now >= request.deadline
            || nonce(ev) != Some(request.id.as_str()) || seq(ev, "lid_seq") != Some(request.grant.lid_seq)
            || seq(ev, "camera_seq") != Some(request.grant.camera_seq)
        {
            return None;
        }
        let delivery = match ev.str("status")? {
            "ok" if ev.data.len() == 5 => {
                let description = ev.str("description").filter(|text| valid_description(text))?;
                let (peer, event) = request.result("ok");
                (peer, event.with("description", description))
            }
            "unavailable" if ev.data.len() == 4 => request.result("unavailable"),
            _ => return None,
        };
        self.request = None;
        Some(delivery)
    }

    pub fn cancel(&mut self, mind: u64, ev: &Event) -> Option<Delivery> {
        let request = self.request.as_ref()?;
        if mind != request.mind || ev.data.len() != 2 || nonce(ev) != Some(request.id.as_str())
            || seq(ev, "lid_seq") != Some(request.grant.lid_seq)
        {
            return None;
        }
        self.request.take().map(|request| request.cancel())
    }

    pub fn reconcile(&mut self, thinking: bool, grant: Option<Grant>, now: f64) -> Vec<Delivery> {
        if !thinking {
            self.used = None;
        }
        if self.request.as_ref().is_some_and(|request| {
            !thinking || grant != Some(request.grant) || now >= request.deadline
        }) {
            self.abort(true)
        } else {
            vec![]
        }
    }

    pub fn disconnected(&mut self, peer: u64) -> Vec<Delivery> {
        match self.request.as_ref() {
            Some(request) if request.mind == peer => self.abort(false),
            Some(request) if request.grant.eyes == peer => self.abort(true),
            _ => vec![],
        }
    }

    fn abort(&mut self, notify_mind: bool) -> Vec<Delivery> {
        let Some(request) = self.request.take() else { return vec![] };
        let mut deliveries = vec![request.cancel()];
        if notify_mind {
            deliveries.push(request.result("unavailable"));
        }
        deliveries
    }
}

fn nonce(ev: &Event) -> Option<&str> {
    ev.str("id").filter(|id| {
        id.len() == 32 && id.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    })
}

fn seq(ev: &Event, key: &str) -> Option<u64> {
    ev.data.get(key).and_then(Value::as_u64)
}

fn result(mind: u64, id: &str, lid_seq: u64, status: &str) -> Delivery {
    (mind, Event::new("camera.look_result").with("id", id)
        .with("lid_seq", lid_seq).with("status", status))
}

fn valid_description(text: &str) -> bool {
    text.len() <= MAX_DESCRIPTION_BYTES && text.trim() == text
        && text.chars().any(char::is_alphanumeric)
        && text.chars().all(|ch| ch.is_alphanumeric() || matches!(ch, ' ' | '’' | '‘' | '“' | '”' | '–' | '—' | '…')
            || (ch.is_ascii_punctuation() && !matches!(ch, '[' | ']' | '<' | '>')))
}

#[cfg(test)]
mod tests {
    use super::*;

    const ID: &str = "0123456789abcdef0123456789abcdef";
    const OTHER_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const GRANT: Grant = Grant { eyes: 42, lid_seq: 2, camera_seq: 5 };

    fn ask(id: &str) -> Event {
        Event::new("camera.look").with("id", id).with("lid_seq", GRANT.lid_seq)
    }

    fn response(description: &str) -> Event {
        Event::new("camera.look_result").with("id", ID)
            .with("lid_seq", GRANT.lid_seq).with("camera_seq", GRANT.camera_seq)
            .with("status", "ok").with("description", description)
    }

    #[test]
    fn one_request_per_turn_with_a_private_sanitized_result() {
        let mut look = Look::default();
        assert!(look.begin(1, &ask("short"), 2, Some(GRANT), 0.0).is_empty());
        assert!(look.begin(1, &ask(ID), 3, Some(GRANT), 0.0).is_empty());
        let accepted = look.begin(1, &ask(ID), 2, Some(GRANT), 0.0);
        assert_eq!(accepted, vec![(42, ask(ID).with("camera_seq", 5u64))]);
        assert!(look.begin(1, &ask(ID), 2, Some(GRANT), 0.1).is_empty());
        assert_eq!(look.begin(1, &ask(OTHER_ID), 2, Some(GRANT), 0.1),
            vec![result(1, OTHER_ID, 2, "unavailable")]);
        let valid = response("A blue mug.");
        assert!(look.complete(1, &valid, Some(GRANT), 0.2).is_none());
        assert!(look.complete(42, &valid.clone().with("frame", "pixel data"), Some(GRANT), 0.2).is_none());
        assert!(look.complete(42, &valid.clone().with("lid_seq", 1u64), Some(GRANT), 0.2).is_none());
        assert!(look.complete(42, &valid.clone().with("camera_seq", 4u64), Some(GRANT), 0.2).is_none());
        let mut expected = result(1, ID, 2, "ok");
        expected.1 = expected.1.with("description", "A blue mug.");
        assert_eq!(look.complete(42, &valid, Some(GRANT), 0.3), Some(expected));
        assert!(look.complete(42, &valid, Some(GRANT), 0.4).is_none());
        assert!(look.begin(1, &ask(ID), 2, Some(GRANT), 0.5).is_empty());
        look.reconcile(false, None, 1.0);
        assert_eq!(look.begin(1, &ask(OTHER_ID), 2, Some(GRANT), 1.1).len(), 1);
    }

    #[test]
    fn safety_rejects_markup_controls_and_oversized_captions() {
        let mut look = Look::default();
        look.begin(1, &ask(ID), 2, Some(GRANT), 0.0);
        for invalid in [" [joy] A mug.", "line\nbreak", "A\u{202e}cup", "<command>", " ", "!?"] {
            assert!(look.complete(42, &response(invalid), Some(GRANT), 0.1).is_none(), "{invalid:?}");
        }
        assert!(look.complete(42, &response(&"á".repeat(121)), Some(GRANT), 0.1).is_none());
        assert!(look.complete(42, &response("Un bol — bleu, peut-être."), Some(GRANT), 0.1).is_some());
    }

    #[test]
    fn stale_results_close_on_policy_lid_state_timeout_and_peer_changes() {
        let no_grant = [(true, None, 1.0), (false, Some(GRANT), 1.0), (true, Some(GRANT), DEADLINE),
            (true, Some(Grant { camera_seq: 6, ..GRANT }), 1.0),
            (true, Some(Grant { lid_seq: 3, ..GRANT }), 1.0),
            (true, Some(Grant { eyes: 43, ..GRANT }), 1.0)];
        for (thinking, grant, now) in no_grant {
            let mut look = Look::default();
            look.begin(1, &ask(ID), 2, Some(GRANT), 0.0);
            let delivery = look.reconcile(thinking, grant, now);
            assert_eq!(delivery, vec![(42, Event::new("camera.look_cancel").with("id", ID)),
                result(1, ID, 2, "unavailable")]);
            assert!(look.complete(42, &response("A mug."), Some(GRANT), now).is_none());
        }
    }

    #[test]
    fn deny_without_grant_and_cancel_only_the_matching_connection() {
        let mut look = Look::default();
        assert_eq!(look.begin(1, &ask(ID), 2, None, 0.0), vec![result(1, ID, 2, "disabled")]);
        look.reconcile(false, None, 0.1);
        look.begin(1, &ask(ID), 2, Some(GRANT), 0.2);
        let cancel = ask(ID);
        assert!(look.cancel(9, &cancel).is_none());
        assert!(look.cancel(1, &ask(OTHER_ID)).is_none());
        assert_eq!(look.cancel(1, &cancel), Some((42, Event::new("camera.look_cancel").with("id", ID))));
        assert!(look.complete(42, &response("A mug."), Some(GRANT), 1.0).is_none());
        look.reconcile(false, None, 1.1);
        look.begin(1, &ask(ID), 2, Some(GRANT), 1.2);
        assert_eq!(look.disconnected(1).len(), 1);
        assert!(look.complete(42, &response("A mug."), Some(GRANT), 1.3).is_none());
        look.reconcile(false, None, 1.4);
        look.begin(1, &ask(ID), 2, Some(GRANT), 1.5);
        assert_eq!(look.disconnected(42).len(), 2);
        assert!(look.complete(42, &response("A mug."), Some(GRANT), 1.6).is_none());
    }
}
