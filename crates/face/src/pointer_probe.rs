//! Bounded, opt-in SDL pointer evidence for the physical KMS face.

#[derive(Default)]
pub struct Probe {
    motions: u64,
    mouse_clicks: u64,
    finger_downs: u64,
    min_x: i64,
    max_x: i64,
    min_y: i64,
    max_y: i64,
}

impl Probe {
    pub fn mouse_click(&mut self) {
        self.mouse_clicks = self.mouse_clicks.saturating_add(1);
    }

    pub fn finger_down(&mut self) {
        self.finger_downs = self.finger_downs.saturating_add(1);
    }

    pub fn motion(&mut self, x: i32, y: i32) {
        let (x, y) = (i64::from(x), i64::from(y));
        if self.motions == 0 {
            self.min_x = x;
            self.max_x = x;
            self.min_y = y;
            self.max_y = y;
        } else {
            self.min_x = self.min_x.min(x);
            self.max_x = self.max_x.max(x);
            self.min_y = self.min_y.min(y);
            self.max_y = self.max_y.max(y);
        }
        self.motions = self.motions.saturating_add(1);
    }

    pub fn summary(&self, driver: &str) -> String {
        let span = if self.motions == 0 {
            "x_span_px=none y_span_px=none".to_string()
        } else {
            format!("x_span_px={} y_span_px={}", self.max_x - self.min_x, self.max_y - self.min_y)
        };
        format!(
            "face: pointer probe driver={driver} mouse_motion={} {span} left_clicks={} finger_downs={}",
            self.motions, self.mouse_clicks, self.finger_downs,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::Probe;

    #[test]
    fn summary_reports_aggregate_only() {
        let mut probe = Probe::default();
        assert_eq!(probe.summary("kmsdrm"),
            "face: pointer probe driver=kmsdrm mouse_motion=0 x_span_px=none y_span_px=none left_clicks=0 finger_downs=0");

        probe.motion(i32::MIN, 1000);
        probe.motion(i32::MAX, 1020);
        probe.mouse_click();
        probe.finger_down();
        assert_eq!(probe.summary("kmsdrm"),
            "face: pointer probe driver=kmsdrm mouse_motion=2 x_span_px=4294967295 y_span_px=20 left_clicks=1 finger_downs=1");
    }
}
