//! How often the face draws, and a once-a-minute note of what drawing costs.
//!
//! The face must never take the model's cores. It draws at 60 Hz only while
//! something on screen moves quickly (speech, attention, a blink, a gesture,
//! a glance), at 30 Hz while it idles or thinks, slower while it sleeps, and
//! 5 Hz while the panel is dark (it still reads the lid and the keys then).

use std::time::{Duration, Instant};

/// What the face is doing, for the frame rate.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Activity {
    /// The panel is dark.
    Blank,
    /// Asleep and still: only a slow breath moves.
    Asleep,
    /// Idle, thinking or drowsy, with nothing fast under way.
    Calm,
    /// Speaking, attending, listening, or a blink, glance or gesture is moving.
    Active,
}

/// Frames a second for an activity. A software renderer never goes above 20.
pub fn hz(activity: Activity, software: bool) -> u32 {
    let hz = match activity {
        Activity::Blank => 5,
        Activity::Asleep => 15,
        Activity::Calm => 30,
        Activity::Active => 60,
    };
    if software {
        hz.min(20)
    } else {
        hz
    }
}

pub const REPORT_EVERY: Duration = Duration::from_secs(60);

/// Frame-time bookkeeping for the once-a-minute log line. Only timings and
/// rates are kept: never anything that was heard or said.
pub struct Stats {
    since: Instant,
    frames: u32,
    drawn: u32,
    work: Duration,
    worst: Duration,
    /// Of `work`, the part before the swap (with vsync, the swap itself
    /// mostly waits for the screen, so `work` alone hides the drawing cost).
    drawing: Duration,
    /// Frames spent at each rate: 5, 15, 20, 30 and 60 Hz.
    at: [u32; 5],
    gpu: Option<Rapl>,
}

const RATES: [u32; 5] = [5, 15, 20, 30, 60];

impl Stats {
    pub fn new(now: Instant) -> Stats {
        Stats {
            since: now,
            frames: 0,
            drawn: 0,
            work: Duration::ZERO,
            worst: Duration::ZERO,
            drawing: Duration::ZERO,
            at: [0; 5],
            gpu: Rapl::open(),
        }
    }

    /// One pass of the loop: `work` is the time from its start to the end of
    /// the swap (without the pacing sleep), `drawing` the part of it before
    /// the swap; `drew` is false for a dark frame that was not presented again.
    pub fn frame(&mut self, work: Duration, drawing: Duration, drew: bool, rate: u32) {
        self.frames += 1;
        if drew {
            self.drawn += 1;
            self.work += work;
            self.worst = self.worst.max(work);
            self.drawing += drawing.min(work);
        }
        if let Some(i) = RATES.iter().position(|&r| r == rate) {
            self.at[i] += 1;
        }
    }

    /// The log line, once a minute.
    pub fn report(&mut self, now: Instant) -> Option<String> {
        let span = now.saturating_duration_since(self.since);
        if span < REPORT_EVERY {
            return None;
        }
        let watts = self.gpu.as_mut().and_then(|gpu| gpu.watts());
        let line = describe(span, self.frames, self.drawn, [self.work, self.worst, self.drawing], &self.at, watts);
        let gpu = self.gpu.take();
        *self = Stats { gpu, ..Stats::new(now) };
        Some(line)
    }
}

fn describe(
    span: Duration,
    frames: u32,
    drawn: u32,
    [work, worst, drawing]: [Duration; 3],
    at: &[u32; 5],
    watts: Option<f64>,
) -> String {
    let ms = |d: Duration| d.as_secs_f64() * 1000.0;
    let per_frame = |d: Duration| if drawn > 0 { ms(d) / f64::from(drawn) } else { 0.0 };
    let (average, before_swap) = (per_frame(work), per_frame(drawing));
    let mut rates: Vec<String> = RATES
        .iter()
        .zip(at)
        .filter(|(_, &n)| n > 0)
        .map(|(rate, &n)| format!("{rate} Hz {}%", (u64::from(n) * 100).div_ceil(u64::from(frames.max(1)))))
        .collect();
    if rates.is_empty() {
        rates.push("none".to_string());
    }
    let mut line = format!(
        "face: last {:.0} s: {frames} frames, {drawn} drawn, frame work {average:.1} ms average, {:.1} ms worst, \
         {before_swap:.1} ms of it before the swap; rates {}",
        span.as_secs_f64(),
        ms(worst),
        rates.join(", ")
    );
    if let Some(w) = watts {
        line.push_str(&format!("; graphics power (RAPL PP1) {w:.2} W"));
    }
    line
}

/// The graphics power plane (RAPL PP1, "uncore" on client Intel CPUs), read
/// once a minute. Absent anywhere else.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub struct Rapl {
    path: std::path::PathBuf,
    range: u64,
    last: Option<(u64, Instant)>,
}

impl Rapl {
    #[cfg(target_os = "linux")]
    fn open() -> Option<Rapl> {
        let base = std::path::Path::new("/sys/class/powercap");
        for entry in std::fs::read_dir(base).ok()?.flatten() {
            let dir = entry.path();
            let name = dir.file_name()?.to_str()?.to_string();
            // intel-rapl:0:N is a subzone of package 0.
            if !name.starts_with("intel-rapl:0:") {
                continue;
            }
            if std::fs::read_to_string(dir.join("name")).is_ok_and(|n| n.trim() == "uncore") {
                let range = std::fs::read_to_string(dir.join("max_energy_range_uj")).ok()?.trim().parse().ok()?;
                let mut rapl = Rapl { path: dir.join("energy_uj"), range, last: None };
                rapl.watts();
                return Some(rapl);
            }
        }
        None
    }

    #[cfg(not(target_os = "linux"))]
    fn open() -> Option<Rapl> {
        None
    }

    #[cfg(target_os = "linux")]
    fn read(&self) -> Option<u64> {
        std::fs::read_to_string(&self.path).ok()?.trim().parse().ok()
    }

    #[cfg(not(target_os = "linux"))]
    fn read(&self) -> Option<u64> {
        None
    }

    fn watts(&mut self) -> Option<f64> {
        let now = self.read()?;
        let at = Instant::now();
        let previous = self.last.replace((now, at));
        let (then, when) = previous?;
        energy_watts(then, now, self.range, at.saturating_duration_since(when))
    }
}

/// Average power between two energy counter readings (microjoules), allowing
/// for one wrap of the counter.
fn energy_watts(then: u64, now: u64, range: u64, span: Duration) -> Option<f64> {
    if span.is_zero() {
        return None;
    }
    let used = if now >= then { now - then } else { range.checked_sub(then)? + now };
    Some(used as f64 / 1e6 / span.as_secs_f64())
}

/// Sleep until the next frame is due at `hz`. An absolute deadline avoids
/// rounding every frame up to the next vblank; a deadline that has slipped
/// by more than a period starts again from now.
pub fn wait(deadline: Option<Instant>, frame_start: Instant, hz: u32) -> Instant {
    let period = Duration::from_secs_f64(1.0 / f64::from(hz.max(1)));
    let base = deadline.filter(|&next| frame_start.saturating_duration_since(next) < period).unwrap_or(frame_start);
    let next = base + period;
    if let Some(wait) = next.checked_duration_since(Instant::now()) {
        std::thread::sleep(wait);
    }
    next
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rates_follow_the_plan_and_software_stays_at_twenty_or_less() {
        assert_eq!(hz(Activity::Active, false), 60);
        assert_eq!(hz(Activity::Calm, false), 30);
        assert_eq!(hz(Activity::Asleep, false), 15);
        assert_eq!(hz(Activity::Blank, false), 5);
        assert_eq!(hz(Activity::Active, true), 20);
        assert_eq!(hz(Activity::Calm, true), 20);
        assert_eq!(hz(Activity::Blank, true), 5);
    }

    #[test]
    fn the_minute_line_names_rates_and_frame_times() {
        let line = describe(
            Duration::from_secs(60),
            1800,
            1790,
            [Duration::from_millis(3580), Duration::from_millis(9), Duration::from_millis(1790)],
            &[0, 0, 0, 1500, 300],
            Some(0.4321),
        );
        assert!(line.starts_with("face: last 60 s: 1800 frames, 1790 drawn"), "{line}");
        assert!(line.contains("2.0 ms average, 9.0 ms worst, 1.0 ms of it before the swap"), "{line}");
        assert!(line.contains("30 Hz 84%, 60 Hz 17%"), "{line}");
        assert!(line.ends_with("graphics power (RAPL PP1) 0.43 W"), "{line}");
        let idle = describe(Duration::from_secs(60), 0, 0, [Duration::ZERO; 3], &[0; 5], None);
        assert!(idle.contains("rates none") && !idle.contains("RAPL"), "{idle}");
    }

    #[test]
    fn stats_report_once_a_minute_and_start_again() {
        let start = Instant::now();
        let mut stats = Stats { gpu: None, ..Stats::new(start) };
        stats.frame(Duration::from_millis(2), Duration::from_millis(1), true, 30);
        stats.frame(Duration::from_millis(50), Duration::ZERO, false, 5);
        assert!(stats.report(start + Duration::from_secs(30)).is_none());
        let line = stats.report(start + Duration::from_secs(61)).unwrap();
        assert!(line.contains("2 frames, 1 drawn") && line.contains("2.0 ms worst"), "{line}");
        assert_eq!(stats.frames, 0);
    }

    #[test]
    fn energy_counter_wraps_once() {
        let minute = Duration::from_secs(60);
        assert_eq!(energy_watts(1_000_000, 61_000_000, 1 << 32, minute), Some(1.0));
        let wrapped = energy_watts(4_000_000_000, 55_032_704, 4_294_967_296, minute).unwrap();
        assert!((wrapped - 5.833).abs() < 0.01, "{wrapped}");
        assert_eq!(energy_watts(5, 6, 10, Duration::ZERO), None);
    }
}
