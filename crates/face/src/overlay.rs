//! Owner captions and the separate health overlay, both off by default.
//!
//! It shows the last thing heard, the reply being spoken, and how the organs
//! are, for bringing up the hardware with no shell. Nothing here is ever
//! logged or written anywhere: words heard are kept only while the overlay
//! is open, only the latest of each, and are dropped when it closes, when
//! the panel goes dark, or when the brainstem goes away.

use crate::{
    chrome::{self, DIM, GLOW, INK, WARNING},
    panel::Rect,
    text::{Font, Picture},
};
use bus::Event;
use std::collections::BTreeMap;
use std::time::{Duration, Instant};

/// Longest caption kept, in characters (the newest part is kept).
const KEEP: usize = 360;
const CAPTION_HOLD: Duration = Duration::from_secs(16);

#[derive(Default)]
pub struct Captions {
    pub on: bool,
    heard: String,
    hearing: String,
    said: String,
    via: Option<&'static str>,
    guest: bool,
    /// The reply being spoken has ended; the next clause starts a new one.
    said_done: bool,
    state: String,
    muted: bool,
    unwell: bool,
    organs: BTreeMap<String, bool>,
    changed: bool,
    captions_changed: bool,
    last_caption: Option<Instant>,
}

fn keep_tail(text: &str) -> String {
    let clean: String = text.chars().map(|c| if c.is_control() { ' ' } else { c }).collect();
    let count = clean.chars().count();
    if count <= KEEP {
        clean
    } else {
        let tail: String = clean.chars().skip(count - KEEP + 1).collect();
        format!("…{tail}")
    }
}

impl Captions {
    pub fn toggle(&mut self) -> bool {
        self.set_enabled(!self.on);
        self.on
    }

    pub fn set_enabled(&mut self, enabled: bool) {
        if enabled != self.on {
            self.on = enabled;
            self.forget();
        }
    }

    /// Drop every word heard or said.
    pub fn forget(&mut self) {
        self.heard.clear();
        self.hearing.clear();
        self.said.clear();
        self.via = None;
        self.last_caption = None;
        self.said_done = true;
        self.changed = true;
        self.captions_changed = true;
    }

    /// Whether the picture needs drawing again, clearing the mark.
    pub fn take_changed(&mut self) -> bool {
        std::mem::take(&mut self.changed)
    }

    pub fn take_captions_changed(&mut self) -> bool {
        std::mem::take(&mut self.captions_changed)
    }

    pub fn displaying(&self, now: Instant) -> bool {
        self.on && !self.guest
            && self.last_caption.is_some_and(|at| now.saturating_duration_since(at) < CAPTION_HOLD)
    }

    fn changed_caption(&mut self) {
        self.last_caption = Some(Instant::now());
        self.changed = true;
        self.captions_changed = true;
    }

    pub fn event(&mut self, ev: &Event) {
        match ev.topic.as_str() {
            "state.changed" => {
                self.state = ev.str("state").unwrap_or("").to_string();
                self.muted = ev.bool("muted").unwrap_or(false);
                self.unwell = ev.bool("unwell").unwrap_or(false);
                self.changed = true;
            }
            "organ.up" | "organ.down" => {
                if let Some(name) = ev.str("name") {
                    self.organs.insert(name.to_string(), ev.topic == "organ.up");
                    self.changed = true;
                }
            }
            "app.privacy" => {
                if let Some(guest) = ev.bool("guest") {
                    self.guest = guest;
                    self.forget();
                }
            }
            // Words and routing indicators stay inside the opt-in owner overlay.
            _ if !self.on || self.guest => {}
            "mind.route" => {
                self.via = match ev.str("via") {
                    Some("code") => Some("Code"),
                    Some("model") => Some("Model"),
                    Some("waiting") => Some("Model waiting"),
                    Some("unavailable") => Some("Model unavailable"),
                    _ => None,
                };
                self.changed_caption();
            }
            "hear.partial" => {
                self.hearing = keep_tail(ev.str("text").unwrap_or(""));
                self.changed_caption();
            }
            "hear.final" => {
                self.via = None;
                self.heard = keep_tail(ev.str("text").unwrap_or(""));
                self.hearing.clear();
                self.changed_caption();
            }
            "hear.cancel" => {
                self.hearing.clear();
                self.changed_caption();
            }
            "say.request" => {
                let text = ev.str("text").unwrap_or("").trim();
                if !text.is_empty() {
                    if self.said_done {
                        self.said.clear();
                        self.said_done = false;
                    }
                    let joined = if self.said.is_empty() { text.to_string() } else { format!("{} {text}", self.said) };
                    self.said = keep_tail(&joined);
                }
                if ev.bool("last") == Some(true) {
                    self.said_done = true;
                }
                self.changed_caption();
            }
            _ => {}
        }
    }

    pub fn picture(&self, font: &Font, columns: usize, show_heard: bool, show_cost: bool) -> Option<Picture> {
        if !self.on || self.guest {
            return None;
        }
        let heard = if !show_heard { "" } else if self.hearing.is_empty() { self.heard.as_str() } else { self.hearing.as_str() };
        let route = self.via.filter(|_| show_cost).map(|via| if via == "Code" { "Code · 0 tokens" } else { via });
        if heard.is_empty() && self.said.is_empty() && route.is_none() {
            return None;
        }
        let heard = (!heard.is_empty()).then(|| {
            let label = if self.hearing.is_empty() { "Heard" } else { "Hearing" };
            let limit = columns.saturating_sub(label.len() + 3);
            if heard.chars().count() > limit {
                let start = heard.chars().count() - limit + 1;
                format!("{label} · …{}", heard.chars().skip(start).collect::<String>())
            } else {
                format!("{label} · {heard}")
            }
        });
        let said = if self.said.is_empty() { Vec::new() } else { Font::wrap(&self.said, columns, 0) };
        let inline = said.last().zip(route).and_then(|(line, badge)| {
            let length = line.chars().count() + badge.chars().count() + 4;
            (length <= columns).then_some(length)
        });
        let content = heard.iter().map(|line| line.chars().count())
            .chain(said.iter().map(|line| line.chars().count()))
            .chain(route.iter().map(|badge| badge.chars().count() + 2))
            .chain(inline)
            .max().unwrap_or(0);
        let w = (content * font.w + 36).max(172);
        let own_row = route.is_some() && inline.is_none();
        let h = 20 + 21 * (usize::from(heard.is_some()) + said.len()) + 25 * usize::from(own_row);
        let mut picture = chrome::picture(w, h);
        let rect = Rect { x: 0, y: 0, w: w as i32, h: h as i32 };
        picture.glass(rect, 14);
        let center = |line: &str| ((w - line.chars().count() * font.w) / 2) as i32;
        let mut y = 9;
        if let Some(line) = heard {
            font.draw(&mut picture, center(&line), y, &line, DIM, 1);
            y += 21;
        }
        for (index, line) in said.iter().enumerate() {
            if let Some((length, badge)) = inline.zip(route).filter(|_| index + 1 == said.len()) {
                let x = ((w - length * font.w) / 2) as i32;
                font.draw(&mut picture, x, y, line, INK, 1);
                draw_route(&mut picture, font, x + ((line.chars().count() + 3) * font.w) as i32, y, badge);
            } else {
                font.draw(&mut picture, center(line), y, line, INK, 1);
            }
            y += 21;
        }
        if let Some(badge) = route.filter(|_| own_row) {
            draw_route(&mut picture, font, center(badge), y + 3, badge);
        }
        Some(picture)
    }

    /// The brainstem is gone: whatever it said about organs no longer holds,
    /// and the words it carried are dropped too.
    pub fn link_lost(&mut self) {
        if !self.organs.is_empty() || !self.state.is_empty() {
            self.organs.clear();
            self.state.clear();
            self.changed = true;
        }
        if !self.heard.is_empty() || !self.hearing.is_empty() || !self.said.is_empty() || self.via.is_some() {
            self.forget();
        }
    }

    pub fn health_lines(&self, health: &Health, columns: usize) -> Vec<String> {
        self.format_lines(health, columns, false)
    }

    #[cfg(test)]
    fn lines(&self, health: &Health, columns: usize) -> Vec<String> {
        self.format_lines(health, columns, true)
    }

    fn format_lines(&self, health: &Health, columns: usize, speech: bool) -> Vec<String> {
        let wrap = |label: &str, text: &str| -> Vec<String> {
            let body = crate::text::Font::wrap(text, columns.saturating_sub(8), 0);
            body.into_iter()
                .enumerate()
                .map(|(i, line)| format!("{:<8}{line}", if i == 0 { label } else { "" }))
                .collect()
        };
        let header = self.via.filter(|_| speech).map_or_else(
            || "Health · Shift+F1 hides".to_string(),
            |via| format!("[{via}] · Health · Shift+F1 hides"),
        );
        let mut lines = vec![header];
        if speech {
            lines.extend(wrap("heard", if self.heard.is_empty() { "(nothing yet)" } else { &self.heard }));
            if !self.hearing.is_empty() {
                lines.extend(wrap("hearing", &self.hearing));
            }
            lines.extend(wrap("said", if self.said.is_empty() { "(nothing yet)" } else { &self.said }));
        }
        let state = if self.state.is_empty() { "unknown" } else { &self.state };
        let mut body = format!("{state} · mic {}", if self.muted { "muted" } else { "open" });
        if self.unwell {
            body.push_str(" · unwell");
        }
        lines.extend(wrap("state", &body));
        let mut organs = vec![match health.beat_age {
            Some(age) if age < 3.0 => format!("brainstem ok (beat {age:.1} s ago)"),
            Some(age) => format!("brainstem quiet for {age:.0} s"),
            None => "brainstem not connected".to_string(),
        }];
        organs.extend(self.organs.iter().filter(|(n, _)| n.as_str() != "face").map(|(name, up)| {
            format!("{name} {}", if *up { "up" } else { "down" })
        }));
        lines.extend(wrap("organs", &organs.join(" · ")));
        let p = health.privacy;
        let mut privacy = vec![
            if health.camera { "camera capture permitted" } else { "camera capture denied" }.to_string(),
            match &health.network_mode {
                Some(mode) => format!("network {} ({mode})", if p.network { "up" } else { "down" }),
                None => format!("network {}", if p.network { "up" } else { "down" }),
            },
            format!("guest {}", if p.guest { "on" } else { "off" }),
        ];
        if p.ssh {
            privacy.push("ssh session open".to_string());
        }
        use crate::status::Reading;
        privacy.push(match p.mic {
            Reading::Open => "mic capture open",
            Reading::Closed => "mic capture closed",
            Reading::Unknown => "mic capture unknown",
        }.to_string());
        if p.mic_latched {
            privacy.push("mic safety latch active".to_string());
        }
        privacy.push(match p.camera_usb {
            Reading::Open => "camera USB authorised",
            Reading::Closed => "camera USB blocked",
            Reading::Unknown => "camera USB unknown",
        }.to_string());
        lines.extend(wrap("privacy", &privacy.join(" · ")));
        let mut face = vec![format!("{}{}", health.renderer, if health.software { " (software)" } else { "" })];
        if health.scale > 1 {
            face.push(format!("1/{} size", health.scale));
        }
        face.push(format!("{} Hz", health.hz));
        face.push(format!("{:.1} ms a frame", health.frame_ms));
        if health.dpms_off {
            face.push("panel off".to_string());
        }
        lines.extend(wrap("face", &face.join(" · ")));
        lines
    }
}

fn draw_route(picture: &mut Picture, font: &Font, x: i32, y: i32, badge: &str) {
    let (ink, fill) = match badge {
        "Model" => ([208, 184, 255], [41, 33, 59]),
        "Model unavailable" | "Model waiting" => (WARNING, [56, 40, 21]),
        _ => (GLOW, [17, 46, 57]),
    };
    let width = (badge.chars().count() * font.w) as i32 + 14;
    picture.fill(Rect { x: x - 7, y: y - 3, w: width, h: 22 }, 10, fill);
    font.draw(picture, x, y, badge, ink, 1);
}

/// Everything else the overlay reports, gathered by the main loop.
pub struct Health {
    pub renderer: String,
    pub software: bool,
    pub scale: i32,
    pub hz: u32,
    pub frame_ms: f64,
    pub beat_age: Option<f64>,
    pub camera: bool,
    pub privacy: crate::status::Privacy,
    pub network_mode: Option<String>,
    pub dpms_off: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn health() -> Health {
        Health {
            renderer: "llvmpipe (LLVM 19.1, 256 bits)".to_string(),
            software: true,
            scale: 4,
            hz: 20,
            frame_ms: 2.24,
            beat_age: Some(0.4),
            camera: false,
            privacy: crate::status::Privacy { network: true, ..Default::default() },
            network_mode: Some("local-only".to_string()),
            dpms_off: false,
        }
    }

    #[test]
    fn words_are_kept_only_while_the_overlay_is_open() {
        let mut c = Captions::default();
        c.event(&Event::new("hear.final").with("text", "what time is it"));
        c.event(&Event::new("say.request").with("text", "It's four.").with("seq", 1).with("last", true));
        let shut = c.lines(&health(), 100).join("\n");
        assert!(!shut.contains("what time") && !shut.contains("four"), "{shut}");
        assert!(c.toggle());
        c.event(&Event::new("hear.partial").with("text", "what ti"));
        assert!(c.lines(&health(), 100).iter().any(|l| l.starts_with("hearing what ti")));
        c.event(&Event::new("hear.final").with("text", "what time is it"));
        c.event(&Event::new("say.request").with("text", "It's ten").with("seq", 1).with("last", false));
        c.event(&Event::new("say.request").with("text", "past four.").with("seq", 2).with("last", false));
        c.event(&Event::new("say.request").with("text", "").with("seq", 3).with("last", true));
        let open = c.lines(&health(), 100);
        assert!(open.iter().any(|l| l == "heard   what time is it"), "{open:?}");
        assert!(open.iter().any(|l| l == "said    It's ten past four."), "{open:?}");
        assert!(!open.iter().any(|l| l.starts_with("hearing")));
        // The next reply replaces the last one.
        c.event(&Event::new("say.request").with("text", "Hello.").with("seq", 1).with("last", true));
        assert!(c.lines(&health(), 100).iter().any(|l| l == "said    Hello."));
        assert!(!c.toggle());
        let closed = c.lines(&health(), 100).join("\n");
        assert!(!closed.contains("Hello") && !closed.contains("what time"), "closing forgets: {closed}");
    }

    #[test]
    fn health_names_organs_privacy_and_the_renderer() {
        let mut c = Captions::default();
        c.event(&Event::new("state.changed").with("state", "idle").with("muted", true).with("unwell", false));
        c.event(&Event::new("organ.up").with("name", "mind"));
        c.event(&Event::new("organ.up").with("name", "face"));
        c.event(&Event::new("organ.up").with("name", "voice"));
        c.event(&Event::new("organ.down").with("name", "voice"));
        let text = c.lines(&health(), 120).join("\n");
        assert!(text.contains("state   idle · mic muted"), "{text}");
        assert!(text.contains("brainstem ok (beat 0.4 s ago) · mind up · voice down"), "{text}");
        assert!(text.contains("camera capture denied · network up (local-only) · guest off"), "{text}");
        assert!(text.contains("camera USB unknown"), "{text}");
        assert!(text.contains("llvmpipe (LLVM 19.1, 256 bits) (software) · 1/4 size · 20 Hz · 2.2 ms a frame"), "{text}");
        c.toggle();
        c.event(&Event::new("hear.final").with("text", "are you there"));
        c.event(&Event::new("say.request").with("text", "I am.").with("seq", 1).with("last", true));
        c.link_lost();
        let lost = c.lines(&Health { beat_age: None, ..health() }, 120).join("\n");
        assert!(lost.contains("brainstem not connected") && !lost.contains("mind up"), "{lost}");
        assert!(!lost.contains("are you there") && !lost.contains("I am."), "a lost link drops the words: {lost}");
        assert!(c.take_changed());
        c.link_lost();
        assert!(!c.take_changed(), "nothing to redraw while it stays lost");
    }

    #[test]
    fn long_captions_keep_their_newest_words_and_no_control_characters() {
        let mut c = Captions::default();
        c.toggle();
        let long = format!("{}\u{1b}[2J end", "word ".repeat(200));
        c.event(&Event::new("hear.final").with("text", long.as_str()));
        assert!(c.heard.chars().count() <= KEEP && c.heard.ends_with(" end") && c.heard.starts_with('…'));
        assert!(!c.heard.contains('\u{1b}'));
        let lines = c.lines(&health(), 60);
        assert!(lines.iter().all(|l| l.chars().count() <= 60), "{lines:?}");
    }

    #[test]
    fn route_chip_only_appears_in_open_owner_captions() {
        let font = crate::text::font().unwrap();
        let route = Event::new("mind.route").with("via", "code");
        let mut captions = Captions::default();
        captions.event(&route);
        assert!(!captions.lines(&health(), 100)[0].contains("[Code]"));
        assert!(captions.picture(font, 100, true, true).is_none());
        captions.toggle();
        captions.event(&route);
        assert!(captions.lines(&health(), 100)[0].contains("[Code]"));
        assert!(captions.picture(font, 100, true, true).is_some());
        assert!(captions.picture(font, 100, true, false).is_none());
        captions.event(&Event::new("app.privacy").with("guest", true));
        captions.event(&Event::new("hear.final").with("text", "guest words"));
        captions.event(&route);
        let lines = captions.lines(&health(), 100).join("\n");
        assert!(!lines.contains("[Code]"));
        assert!(!lines.contains("guest words"));
        assert!(captions.picture(font, 100, true, true).is_none());
        captions.event(&Event::new("app.privacy").with("guest", false));
        captions.event(&Event::new("mind.route").with("via", "model"));
        assert!(captions.lines(&health(), 100)[0].contains("[Model]"));
    }
}
