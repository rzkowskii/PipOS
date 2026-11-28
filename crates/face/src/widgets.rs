//! Optional desktop readings from the brainstem and the owner's weather request.

use crate::{
    chrome::{self, DIM, EDGE, GLOW, INK},
    look::Corner,
    panel::Rect,
    render, text,
};
use bus::Event;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Readings {
    clock: Option<(u8, u8)>,
    system: System,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
struct System {
    battery: Option<u8>,
    temperature: Option<i16>,
    on_ac: Option<bool>,
}

impl Readings {
    pub fn from_body(event: &Event) -> Self {
        let time = |key: &str, limit: u8| {
            let number = event.data.get(key)?.as_u64()?;
            u8::try_from(number).ok().filter(|&value| value < limit)
        };
        Self {
            clock: time("hour", 24).zip(time("minute", 60)),
            system: System {
                battery: event.f64("battery").filter(|v| (0.0..=100.0).contains(v)).map(|v| v.round() as u8),
                temperature: event.f64("temp").filter(|v| (-40.0..=125.0).contains(v)).map(|v| v.round() as i16),
                on_ac: event.bool("on_ac"),
            },
        }
    }
}

#[derive(Default)]
pub struct Tiles {
    clock: Option<(u8, u8)>,
    system: Option<System>,
    weather: Option<String>,
    textures: [Option<render::Texture>; 3],
}

const WIDTH: usize = 230;

fn card(font: &text::Font, label: &str, height: usize) -> text::Picture {
    let mut picture = chrome::picture(WIDTH, height);
    picture.glass(Rect { x: 0, y: 0, w: WIDTH as i32, h: height as i32 }, 16);
    font.draw(&mut picture, 16, 10, label, DIM, 1);
    picture
}

fn system_card(font: &text::Font, system: System) -> text::Picture {
    let height = 40 + 23 * (usize::from(system.battery.is_some()) + usize::from(system.temperature.is_some())
        + usize::from(system.on_ac == Some(true)));
    let mut picture = card(font, "SYSTEM", height);
    let mut y = 30;
    if let Some(battery) = system.battery {
        font.draw(&mut picture, 16, y, "Battery", DIM, 1);
        picture.fill(Rect { x: 81, y: y + 6, w: 86, h: 5 }, 2, EDGE);
        picture.fill(Rect { x: 81, y: y + 6, w: 86 * i32::from(battery) / 100, h: 5 }, 2, GLOW);
        font.draw(&mut picture, 180, y, &format!("{battery}%"), INK, 1);
        y += 23;
    }
    if let Some(temp) = system.temperature {
        font.draw(&mut picture, 16, y, "Heat", DIM, 1);
        font.draw(&mut picture, 156, y, &format!("{temp} °C"), INK, 1);
        y += 23;
    }
    if system.on_ac == Some(true) {
        font.draw(&mut picture, 16, y, "Plugged in", DIM, 1);
    }
    picture
}

impl Tiles {
    /// Only regenerate a texture when the text it shows changes.
    pub unsafe fn refresh(&mut self, gl: &glow::Context, renderer: &render::Renderer,
                          values: Readings, clock: bool, system: bool, weather: Option<&str>) -> Result<(), String> {
        let clock = values.clock.filter(|_| clock);
        let system = (system && (values.system.battery.is_some() || values.system.temperature.is_some()
            || values.system.on_ac == Some(true))).then_some(values.system);
        if clock == self.clock && system == self.system && weather == self.weather.as_deref() {
            return Ok(());
        }
        let Some(font) = text::font() else { return Ok(()) };
        if clock != self.clock {
            let picture = clock.map(|(hour, minute)| {
                let mut picture = card(font, "CLOCK", 79);
                font.draw(&mut picture, 16, 30, &format!("{hour:02}:{minute:02}"), INK, 2);
                picture
            });
            Self::replace(&mut self.textures[0], gl, renderer, picture)?;
            self.clock = clock;
        }
        if system != self.system {
            Self::replace(&mut self.textures[1], gl, renderer, system.map(|values| system_card(font, values)))?;
            self.system = system;
        }
        if weather != self.weather.as_deref() {
            let picture = weather.map(|line| {
                let lines = text::Font::wrap(line, (WIDTH - 32) / font.w, 0);
                let mut picture = card(font, "WEATHER", 43 + font.h * lines.len());
                for (index, line) in lines.iter().enumerate() {
                    font.draw(&mut picture, 16, 31 + (index * font.h) as i32, line, INK, 1);
                }
                picture
            });
            Self::replace(&mut self.textures[2], gl, renderer, picture)?;
            self.weather = weather.map(str::to_owned);
        }
        Ok(())
    }

    unsafe fn replace(texture: &mut Option<render::Texture>, gl: &glow::Context, renderer: &render::Renderer,
                      picture: Option<text::Picture>) -> Result<(), String> {
        if let Some(previous) = texture.take() {
            renderer.delete(gl, previous);
        }
        if let Some(picture) = picture {
            *texture = Some(renderer.texture(gl, picture.w as i32, picture.h as i32, &picture.rgba)?);
        }
        Ok(())
    }

    pub unsafe fn draw(&self, gl: &glow::Context, renderer: &render::Renderer, w: i32, h: i32,
                       corners: [Corner; 3]) {
        let sizes = self.textures.each_ref().map(|tile| tile.as_ref().map(|tile| (tile.w, tile.h)));
        for (tile, rect) in self.textures.iter().zip(layout(sizes, w, h, corners)) {
            if let (Some(tile), Some(rect)) = (tile, rect) {
                renderer.draw_texture(gl, tile, rect, w, h, 1.0);
            }
        }
    }
}

/// Where each tile goes, in GL pixels (y up), or None when it does not fit.
fn layout(sizes: [Option<(i32, i32)>; 3], w: i32, h: i32, corners: [Corner; 3]) -> [Option<Rect>; 3] {
    let zoom = (h as f32 / 800.0).clamp(0.6, 1.5);
    let zoom = zoom.min((w - 18).max(1) as f32 / (WIDTH * 2 + 32) as f32);
    let scaled = |pixels| ((pixels as f32 * zoom).round() as i32).max(1);
    let margin = scaled(24);
    // Leave the shader's top privacy marks unobstructed.
    let mut top = [h - (h * 6 + 99) / 100 - scaled(24); 2];
    let mut bottom = [scaled(74); 2];
    // And the covered-ears bars at mid-height on both sides (eye.frag:
    // fixedMarks, |y| <= 0.128 of the half height): stacks stop short of
    // 0.15 of it, whether or not the mark is showing, so tiles never move
    // when the microphone is muted.
    let ears = (h as f32 * 0.075).ceil() as i32;
    let (ears_low, ears_high) = (h / 2 - ears, h - h / 2 + ears);
    let gap = scaled(10);
    let mut placed = [None; 3];
    for ((size, corner), slot) in sizes.into_iter().zip(corners).zip(&mut placed) {
        let Some((tile_w, tile_h)) = size else { continue };
        let (width, height) = (scaled(tile_w), scaled(tile_h));
        let side = corner as usize % 2;
        let upper = matches!(corner, Corner::TopLeft | Corner::TopRight);
        let y = if upper { top[side] - height } else { bottom[side] };
        let fits = if upper { y >= bottom[side].max(ears_high) } else { y + height <= top[side].min(ears_low) };
        if !fits {
            continue;
        }
        if upper {
            top[side] = y - gap;
        } else {
            bottom[side] += height + gap;
        }
        let x = if side == 0 { margin } else { w - width - margin };
        if x >= 0 && y >= 0 {
            *slot = Some(Rect { x, y, w: width, h: height });
        }
    }
    placed
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uses_only_known_values() {
        let body = Readings::from_body(&Event::new("body.state").with("hour", 9u32).with("minute", 2u32)
                                      .with("battery", 84.6).with("temp", 53.2).with("on_ac", true));
        assert_eq!(body.clock, Some((9, 2)));
        assert_eq!(body.system, System { battery: Some(85), temperature: Some(53), on_ac: Some(true) });
        let unknown = Readings::from_body(&Event::new("body.state").with("hour", 24u32).with("minute", 9u32)
                                          .with("battery", 132.0).with("temp", -100.0).with("on_ac", false));
        assert_eq!(unknown, Readings { system: System { on_ac: Some(false), ..System::default() }, ..Readings::default() });
    }

    #[test]
    fn stacked_tiles_keep_clear_of_the_ears_marks() {
        // Tallest cards: clock, a three-row system card, four weather lines.
        let sizes = [Some((WIDTH as i32, 79)), Some((WIDTH as i32, 109)), Some((WIDTH as i32, 107))];
        for (w, h) in [(1280, 800), (1024, 600)] {
            // The ears bar in pixels: |y| <= 0.128 of the half height.
            let bar = (h as f32 / 2.0 - 0.128 * h as f32 / 2.0, h as f32 / 2.0 + 0.128 * h as f32 / 2.0);
            for corner in [Corner::TopLeft, Corner::TopRight, Corner::BottomLeft, Corner::BottomRight] {
                let placed = layout(sizes, w, h, [corner; 3]);
                assert!(placed[0].is_some() && placed[1].is_some(), "{w}x{h} {}: {placed:?}", corner as usize);
                for rect in placed.into_iter().flatten() {
                    let (y0, y1) = (rect.y as f32, (rect.y + rect.h) as f32);
                    assert!(y1 <= bar.0 || y0 >= bar.1, "{w}x{h} {}: {rect:?} covers the ears", corner as usize);
                    assert!(rect.y >= 0 && rect.y + rect.h <= h - h * 6 / 100, "{w}x{h} {}: {rect:?}", corner as usize);
                }
            }
        }
    }

    #[test]
    fn tiles_split_across_sides_all_fit() {
        let sizes = [Some((WIDTH as i32, 79)), Some((WIDTH as i32, 109)), Some((WIDTH as i32, 107))];
        let placed = layout(sizes, 1280, 800, [Corner::TopRight, Corner::BottomRight, Corner::TopLeft]);
        assert!(placed.iter().all(Option::is_some), "{placed:?}");
    }
}
