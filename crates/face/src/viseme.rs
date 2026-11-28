//! The Face Lab's text-driven mouth preview on the same clock as exported frames.

const CHAR_SECS: f64 = 0.085;
const RELEASE_SECS: f64 = 0.25;
const RESPONSE: f64 = 28.0;

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Sample {
    pub open: f32,
    /// The relative change in the mouth's width.
    pub width: f32,
    pub round: f32,
}

impl Sample {
    fn approach(self, target: Self, seconds: f64) -> Self {
        let amount = (-(-seconds * RESPONSE).exp_m1()) as f32;
        Self {
            open: self.open + (target.open - self.open) * amount,
            width: self.width + (target.width - self.width) * amount,
            round: self.round + (target.round - self.round) * amount,
        }
    }
}

struct Segment {
    start: f64,
    at_start: Sample,
    target: Sample,
}

pub struct Visemes {
    segments: Vec<Segment>,
    duration: f64,
}

impl Visemes {
    pub fn new(text: &str) -> Self {
        let mut segments = Vec::new();
        let mut start = 0.0;
        let mut at_start = Sample::default();
        for letter in text.chars().flat_map(char::to_lowercase) {
            let (target, seconds) = target(letter);
            segments.push(Segment {
                start,
                at_start,
                target,
            });
            at_start = at_start.approach(target, seconds);
            start += seconds;
        }
        if !segments.is_empty() {
            segments.push(Segment {
                start,
                at_start,
                target: Sample::default(),
            });
            start += RELEASE_SECS;
        }
        Self {
            segments,
            duration: start,
        }
    }

    pub fn duration(&self) -> f64 {
        self.duration
    }

    pub fn sample(&self, seconds: f64) -> Sample {
        if !(0.0..self.duration).contains(&seconds) {
            return Sample::default();
        }
        let index = self
            .segments
            .partition_point(|segment| segment.start <= seconds)
            - 1;
        let segment = &self.segments[index];
        segment
            .at_start
            .approach(segment.target, seconds - segment.start)
    }
}

fn target(letter: char) -> (Sample, f64) {
    let (open, width, round, seconds) = match letter {
        'a' => (0.85, 0.12, 0.0, CHAR_SECS),
        'e' => (0.5, 0.28, 0.0, CHAR_SECS),
        'i' => (0.25, 0.34, 0.0, CHAR_SECS),
        'o' => (0.7, -0.38, 1.0, CHAR_SECS),
        'u' => (0.4, -0.46, 1.0, CHAR_SECS),
        'm' | 'b' | 'p' => (0.0, -0.05, 0.0, CHAR_SECS),
        'f' | 'v' => (0.1, 0.12, 0.0, CHAR_SECS),
        'a'..='z' => (0.3, 0.0, 0.0, 0.06),
        ',' | '.' | '!' | '?' | ';' | ':' => (0.0, 0.0, 0.0, 0.28),
        _ => (0.0, 0.0, 0.0, 0.10),
    };
    (Sample { open, width, round }, seconds)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn authored_vowels_move_and_release_the_mouth() {
        let speech = Visemes::new("Ao");
        assert_eq!(speech.sample(0.0), Sample::default());
        let first = speech.sample(0.08);
        assert!(first.open > 0.7 && first.width > 0.1);
        assert_eq!(first.round, 0.0);
        let second = speech.sample(0.16);
        assert!(second.open > 0.65 && second.width < -0.3 && second.round > 0.8);
        assert!(speech.sample(speech.duration() - 0.001).open < 0.001);
        assert_eq!(speech.sample(speech.duration()), Sample::default());
    }

    #[test]
    fn sampling_is_repeatable_and_punctuation_holds_the_pause() {
        let speech = Visemes::new("a!p u");
        let first = speech.sample(0.05);
        assert!(speech.sample(0.32).open < 0.002);
        assert_eq!(speech.sample(0.05), first);
        assert_eq!(speech.sample(f64::NAN), Sample::default());
        assert_eq!(Visemes::new("").duration(), 0.0);
    }
}
