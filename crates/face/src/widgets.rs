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
        let zoom = (h as f32 / 800.0).clamp(0.6, 1.5);
        let zoom = zoom.min((w - 18).max(1) as f32 / (WIDTH * 2 + 32) as f32);
        let scaled = |pixels| ((pixels as f32 * zoom).round() as i32).max(1);
        let margin = scaled(24);
        // Leave the shader's top privacy marks unobstructed.
        let mut top = [h - (h * 6 + 99) / 100 - scaled(24); 2];
        let mut bottom = [scaled(74); 2];
        let gap = scaled(10);
        for (tile, corner) in self.textures.iter().zip(corners) {
            if let Some(tile) = tile {
                let (width, height) = (scaled(tile.w), scaled(tile.h));
                let side = corner as usize % 2;
                let upper = matches!(corner, Corner::TopLeft | Corner::TopRight);
                if height > top[side] - bottom[side] {
                    continue;
                }
                let x = if side == 0 { margin } else { w - width - margin };
                let y = if upper {
                    let y = top[side] - height;
                    top[side] = y - gap;
                    y
                } else {
                    let y = bottom[side];
                    bottom[side] += height + gap;
                    y
                };
                if x >= 0 && y >= 0 {
                    renderer.draw_texture(gl, tile, Rect { x, y, w: width, h: height }, w, h, 1.0);
                }
            }
        }
    }
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
}
