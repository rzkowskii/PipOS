//! Shared shaping for the brainstem's published drives and the face.

pub fn tiredness(energy: Option<f32>, comfort: Option<f32>) -> f32 {
    let flat = energy.map_or(0.0, |e| ((0.6 - e) / 0.5).clamp(0.0, 1.0));
    let hot = comfort.map_or(0.0, |c| ((0.6 - c) / 0.6).clamp(0.0, 1.0));
    (0.20 * flat + 0.08 * hot).min(0.26)
}

pub fn eagerness(social: Option<f32>) -> f32 {
    social.map_or(0.0, |s| ((0.6 - s) / 0.6).clamp(0.0, 1.0))
}
