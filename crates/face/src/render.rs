//! Drawing: one shader, one triangle, no textures.

use glow::HasContext;

const VERT: &str = include_str!("eye.vert");
const FRAG: &str = include_str!("eye.frag");

/// Colours and texture of the eyes. Colours are as they appear on screen;
/// the shader works in linear light.
#[derive(Clone, Copy)]
pub struct Look {
    /// The eye at its rim: a saturated cyan.
    pub fill: [f32; 3],
    /// The eye at its centre: hotter, toward cyan-white but never white
    /// (the lab finds the eyes by their blue lead over red).
    pub core: [f32; 3],
    /// The bloom around the eye: a deeper, bluer cyan.
    pub halo: [f32; 3],
    /// How far the LED grid's lines darken the eye, 0 to 1.
    pub grid: f32,
    /// How far the bloom reaches, in screen units (the screen is 2 high).
    pub glow_r: f32,
}

impl Default for Look {
    fn default() -> Look {
        Look {
            fill: [0.03, 0.86, 1.0],
            core: [0.30, 0.98, 1.0],
            halo: [0.0, 0.48, 1.0],
            grid: 0.16,
            glow_r: 0.22,
        }
    }
}

/// Screen colour to the shader's linear light (gamma 2, matching its output).
fn linear(c: [f32; 3]) -> [f32; 3] {
    c.map(|v| v * v)
}

/// The LED grid's pitch in whole pixels, and its depth at that pitch:
/// about 266 cells down the screen (3 pixels on the laptop's 800 rows),
/// lighter at 2 pixels and gone below that, where it would only be noise.
fn grid(h: i32, depth: f32) -> (f32, f32) {
    let cells = h as f32 / 266.0;
    let pitch = cells.round().max(2.0);
    let depth = if cells < 1.5 {
        0.0
    } else if pitch < 3.0 {
        depth * 0.5
    } else {
        depth
    };
    (pitch, depth)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grid_is_three_pixels_on_the_laptop_and_absent_when_tiny() {
        assert_eq!(grid(800, 0.16), (3.0, 0.16));
        assert_eq!(grid(400, 0.16), (2.0, 0.08));
        assert_eq!(grid(200, 0.16).1, 0.0);
    }

    #[test]
    fn colours_keep_blue_ahead_of_red_for_the_lab() {
        // lab/vm.py counts lit pixels as the eyes only when b > r + 30 and g > r.
        let look = Look::default();
        for c in [look.fill, look.core, look.halo] {
            let [r, g, b] = c.map(|v| v * 255.0);
            assert!(b > r + 30.0 && g > r, "{c:?}");
        }
    }
}

/// A small off-screen canvas, for machines that draw in software.
struct Canvas {
    fb: glow::Framebuffer,
    rb: glow::Renderbuffer,
    w: i32,
    h: i32,
}

/// The name of whatever is drawing, and whether it is a software renderer.
///
/// # Safety
/// Needs a current GL context.
pub unsafe fn describe(gl: &glow::Context) -> (String, bool) {
    let name = gl.get_parameter_string(glow::RENDERER);
    let low = name.to_lowercase();
    let software = ["llvmpipe", "softpipe", "swrast", "software", "swiftshader"].iter().any(|s| low.contains(s));
    (name, software)
}

/// Clear the displayed framebuffer without running the eye shader.
///
/// # Safety
/// Needs a current GL context.
pub unsafe fn blank(gl: &glow::Context) {
    gl.clear_color(0.0, 0.0, 0.0, 1.0);
    gl.clear(glow::COLOR_BUFFER_BIT);
}

pub struct Renderer {
    canvas: Option<Canvas>,
    program: glow::Program,
    vao: glow::VertexArray,
    u_res: Option<glow::UniformLocation>,
    u_e: Option<glow::UniformLocation>,
    u_fill: Option<glow::UniformLocation>,
    u_core: Option<glow::UniformLocation>,
    u_halo: Option<glow::UniformLocation>,
    u_grid: Option<glow::UniformLocation>,
    u_pitch: Option<glow::UniformLocation>,
    u_glow_r: Option<glow::UniformLocation>,
}

impl Renderer {
    /// # Safety
    /// Needs a current GL context of at least OpenGL 3.3 or GLES 3.0.
    pub unsafe fn new(gl: &glow::Context) -> Result<Renderer, String> {
        let header = if gl.version().is_embedded {
            "#version 300 es\nprecision highp float;\nprecision highp int;\n"
        } else {
            "#version 330 core\n"
        };
        let program = gl.create_program()?;
        let mut shaders = Vec::new();
        for (kind, src) in [(glow::VERTEX_SHADER, VERT), (glow::FRAGMENT_SHADER, FRAG)] {
            let shader = gl.create_shader(kind)?;
            gl.shader_source(shader, &format!("{header}{src}"));
            gl.compile_shader(shader);
            if !gl.get_shader_compile_status(shader) {
                return Err(format!("shader compile failed: {}", gl.get_shader_info_log(shader)));
            }
            gl.attach_shader(program, shader);
            shaders.push(shader);
        }
        gl.link_program(program);
        if !gl.get_program_link_status(program) {
            return Err(format!("shader link failed: {}", gl.get_program_info_log(program)));
        }
        for shader in shaders {
            gl.detach_shader(program, shader);
            gl.delete_shader(shader);
        }
        let vao = gl.create_vertex_array()?;
        let loc = |name: &str| gl.get_uniform_location(program, name);
        Ok(Renderer {
            canvas: None,
            program,
            vao,
            u_res: loc("uRes"),
            u_e: loc("uE"),
            u_fill: loc("uFill"),
            u_core: loc("uCore"),
            u_halo: loc("uHalo"),
            u_grid: loc("uGrid"),
            u_pitch: loc("uPitch"),
            u_glow_r: loc("uGlowR"),
        })
    }

    /// Draw into whatever framebuffer is bound, `w` by `h` pixels.
    ///
    /// # Safety
    /// Needs the GL context this renderer was made with to be current.
    pub unsafe fn draw(&self, gl: &glow::Context, w: i32, h: i32, eyes: &[f32; 32], look: &Look) {
        gl.viewport(0, 0, w, h);
        gl.use_program(Some(self.program));
        gl.bind_vertex_array(Some(self.vao));
        gl.uniform_2_f32(self.u_res.as_ref(), w as f32, h as f32);
        gl.uniform_4_f32_slice(self.u_e.as_ref(), eyes);
        for (at, c) in [(&self.u_fill, look.fill), (&self.u_core, look.core), (&self.u_halo, look.halo)] {
            let [r, g, b] = linear(c);
            gl.uniform_3_f32(at.as_ref(), r, g, b);
        }
        let (pitch, depth) = grid(h, look.grid);
        gl.uniform_1_f32(self.u_grid.as_ref(), depth);
        gl.uniform_1_f32(self.u_pitch.as_ref(), pitch);
        gl.uniform_1_f32(self.u_glow_r.as_ref(), look.glow_r);
        gl.draw_arrays(glow::TRIANGLES, 0, 3);
    }

    /// Draw at 1/`scale` of the screen's size and stretch the result to fit.
    /// The eyes are soft shapes, so this costs little in looks and saves a
    /// software renderer most of its work.
    ///
    /// # Safety
    /// Needs the GL context this renderer was made with to be current.
    pub unsafe fn draw_scaled(
        &mut self,
        gl: &glow::Context,
        w: i32,
        h: i32,
        scale: i32,
        eyes: &[f32; 32],
        look: &Look,
    ) -> Result<(), String> {
        if scale <= 1 {
            self.draw(gl, w, h, eyes, look);
            return Ok(());
        }
        let (cw, ch) = ((w / scale).max(16), (h / scale).max(16));
        if self.canvas.as_ref().is_none_or(|c| c.w != cw || c.h != ch) {
            if let Some(old) = self.canvas.take() {
                gl.delete_framebuffer(old.fb);
                gl.delete_renderbuffer(old.rb);
            }
            let fb = gl.create_framebuffer()?;
            let rb = gl.create_renderbuffer()?;
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fb));
            gl.bind_renderbuffer(glow::RENDERBUFFER, Some(rb));
            gl.renderbuffer_storage(glow::RENDERBUFFER, glow::RGBA8, cw, ch);
            gl.framebuffer_renderbuffer(glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::RENDERBUFFER, Some(rb));
            if gl.check_framebuffer_status(glow::FRAMEBUFFER) != glow::FRAMEBUFFER_COMPLETE {
                return Err("low-resolution canvas is incomplete".to_string());
            }
            self.canvas = Some(Canvas { fb, rb, w: cw, h: ch });
        }
        let fb = self.canvas.as_ref().map(|c| c.fb);
        gl.bind_framebuffer(glow::FRAMEBUFFER, fb);
        self.draw(gl, cw, ch, eyes, look);
        gl.bind_framebuffer(glow::READ_FRAMEBUFFER, fb);
        gl.bind_framebuffer(glow::DRAW_FRAMEBUFFER, None);
        gl.blit_framebuffer(0, 0, cw, ch, 0, 0, w, h, glow::COLOR_BUFFER_BIT, glow::LINEAR);
        gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        Ok(())
    }

    /// Render off-screen and return RGBA rows, top row first.
    ///
    /// # Safety
    /// Needs the GL context this renderer was made with to be current.
    pub unsafe fn snapshot(
        &self,
        gl: &glow::Context,
        w: u32,
        h: u32,
        eyes: &[f32; 32],
        look: &Look,
    ) -> Result<Vec<u8>, String> {
        let fb = gl.create_framebuffer()?;
        let rb = gl.create_renderbuffer()?;
        gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fb));
        gl.bind_renderbuffer(glow::RENDERBUFFER, Some(rb));
        gl.renderbuffer_storage(glow::RENDERBUFFER, glow::RGBA8, w as i32, h as i32);
        gl.framebuffer_renderbuffer(glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::RENDERBUFFER, Some(rb));
        if gl.check_framebuffer_status(glow::FRAMEBUFFER) != glow::FRAMEBUFFER_COMPLETE {
            return Err("off-screen framebuffer is incomplete".to_string());
        }
        self.draw(gl, w as i32, h as i32, eyes, look);
        gl.finish();
        let mut raw = vec![0u8; (w * h * 4) as usize];
        gl.pixel_store_i32(glow::PACK_ALIGNMENT, 1);
        gl.read_pixels(
            0,
            0,
            w as i32,
            h as i32,
            glow::RGBA,
            glow::UNSIGNED_BYTE,
            glow::PixelPackData::Slice(Some(&mut raw)),
        );
        gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        gl.delete_framebuffer(fb);
        gl.delete_renderbuffer(rb);

        // GL rows run bottom-up; images run top-down.
        let stride = (w * 4) as usize;
        let mut out = vec![0u8; raw.len()];
        for row in 0..h as usize {
            let src = (h as usize - 1 - row) * stride;
            out[row * stride..(row + 1) * stride].copy_from_slice(&raw[src..src + stride]);
        }
        Ok(out)
    }
}
