//! Small cached pictures composited by the face's existing texture renderer.

use crate::{panel::Rect, text::Picture};

pub type Color = [u8; 3];
pub const INK: Color = [217, 240, 247];
pub const DIM: Color = [133, 160, 175];
pub const FAINT: Color = [107, 134, 150];
pub const EDGE: Color = [26, 42, 53];
pub const GLOW: Color = [45, 239, 255];
pub const WARNING: Color = [255, 180, 84];

pub fn picture(w: usize, h: usize) -> Picture {
    Picture { w, h, rgba: vec![0; w * h * 4] }
}

impl Picture {
    pub fn blend(&mut self, x: i32, y: i32, color: Color, alpha: u8) {
        if x < 0 || y < 0 || x >= self.w as i32 || y >= self.h as i32 || alpha == 0 {
            return;
        }
        let offset = (y as usize * self.w + x as usize) * 4;
        let pixel = &mut self.rgba[offset..offset + 4];
        let alpha = u16::from(alpha);
        let remaining = 255 - alpha;
        for (out, value) in pixel[..3].iter_mut().zip(color) {
            *out = ((u16::from(value) * alpha + u16::from(*out) * remaining + 127) / 255) as u8;
        }
        pixel[3] = (alpha + (u16::from(pixel[3]) * remaining + 127) / 255) as u8;
    }

    pub fn glass(&mut self, rect: Rect, radius: i32) {
        self.rounded(rect, radius, EDGE, EDGE, 255);
        self.rounded(
            Rect { x: rect.x + 1, y: rect.y + 1, w: rect.w - 2, h: rect.h - 2 },
            radius - 1, [18, 30, 40], [8, 15, 21], 255,
        );
    }

    pub fn fill(&mut self, rect: Rect, radius: i32, color: Color) {
        self.rounded(rect, radius, color, color, 255);
    }

    fn rounded(&mut self, rect: Rect, radius: i32, top: Color, bottom: Color, alpha: u8) {
        let range = rect.h.saturating_sub(1).max(1);
        for y in 0..rect.h {
            let color = std::array::from_fn(|i| {
                ((i32::from(top[i]) * (range - y).max(0) + i32::from(bottom[i]) * y) / range) as u8
            });
            let dy = (radius - y.min(rect.h - 1 - y)).max(0) as f32 - 0.5;
            for x in 0..rect.w {
                let dx = (radius - x.min(rect.w - 1 - x)).max(0) as f32 - 0.5;
                let coverage = if dx > 0.0 && dy > 0.0 {
                    (radius as f32 + 0.5 - dx.hypot(dy)).clamp(0.0, 1.0)
                } else {
                    1.0
                };
                self.blend(rect.x + x, rect.y + y, color, (coverage * f32::from(alpha)) as u8);
            }
        }
    }

    pub fn path(&mut self, x: i32, y: i32, points: &[(f32, f32)], color: Color) {
        for line in points.windows(2) {
            let [(ax, ay), (bx, by)] = [line[0], line[1]];
            let (dx, dy) = (bx - ax, by - ay);
            let length = (dx * dx + dy * dy).max(f32::EPSILON);
            for py in (ay.min(by) as i32 - 1)..=(ay.max(by) as i32 + 1) {
                for px in (ax.min(bx) as i32 - 1)..=(ax.max(bx) as i32 + 1) {
                    let (ex, ey) = (px as f32 + 0.5 - ax, py as f32 + 0.5 - ay);
                    let t = ((ex * dx + ey * dy) / length).clamp(0.0, 1.0);
                    let coverage = (1.35 - (ex - t * dx).hypot(ey - t * dy)).clamp(0.0, 1.0);
                    self.blend(x + px, y + py, color, (255.0 * coverage) as u8);
                }
            }
        }
    }
}
