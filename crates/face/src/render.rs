//! Drawing: the eye shader on one triangle, and a textured quad for the
//! workshop panel and the text overlays.

use crate::panel::Rect;
use glow::HasContext;

const VERT: &str = include_str!("eye.vert");
const FRAG: &str = include_str!("eye.frag");

const QUAD_VERT: &str = "
uniform vec4 uRect; // x0, y0, x1, y1 in clip space
out vec2 vUv;
void main() {
    vec2 p = vec2(float(gl_VertexID & 1), float((gl_VertexID >> 1) & 1));
    gl_Position = vec4(mix(uRect.xy, uRect.zw, p), 0.0, 1.0);
    vUv = vec2(p.x, 1.0 - p.y); // pictures are stored top row first
}
";

const QUAD_FRAG: &str = "
in vec2 vUv;
out vec4 frag;
uniform sampler2D uTex;
uniform float uAlpha;
void main() {
    frag = texture(uTex, vUv) * uAlpha; // premultiplied
}
";

/// Privacy marks drawn with the eyes (task 7.5), each 0 (off) to 1 (on).
/// The fixed marks stay dim enough (r+g+b at most about 110) that the lab
/// never counts them as lit, and every mark keeps blue ahead of red.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Marks {
    /// A glint in each eye while the camera may capture.
    pub glint: f32,
    /// A soft ring around each eye in guest mode.
    pub ring: f32,
    /// Covered ears at the screen's edges while the microphone is muted.
    pub ears: f32,
    /// A dot in the top right corner while a network link is up.
    pub network: f32,
    /// A small prompt mark in the top left corner during an SSH session.
    pub ssh: f32,
}

/// The fixed marks' brightness on screen, as a fraction of the eye colours.
pub const MARK_LEVEL: f32 = 0.22;

impl Marks {
    fn uniforms(self) -> [f32; 8] {
        [self.glint, self.ring, self.ears, self.network, self.ssh, MARK_LEVEL, 0.0, 0.0]
    }

}

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
    pub marks: Marks,
}

impl Default for Look {
    fn default() -> Look {
        Look {
            fill: [0.03, 0.86, 1.0],
            core: [0.30, 0.98, 1.0],
            halo: [0.0, 0.48, 1.0],
            grid: 0.16,
            glow_r: 0.22,
            marks: Marks::default(),
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
    fn fixed_marks_are_too_dim_for_the_lab_to_count_and_stay_blue() {
        // lab/vm.py counts r+g+b > 120 as lit; the marks sit at the screen's
        // edges, where a lit pixel would stretch the eyes' bounding boxes.
        let look = Look::default();
        for c in [look.fill, look.halo] {
            let [r, g, b] = c.map(|v| v * MARK_LEVEL * 255.0);
            assert!(r + g + b + 1.0 <= 112.0, "{c:?}");
            assert!(b > r + 30.0 && g > r, "{c:?}");
        }
    }

    #[test]
    fn rectangles_map_to_clip_space() {
        assert_eq!(clip(Rect { x: 0, y: 0, w: 1280, h: 800 }, 1280, 800), [-1.0, -1.0, 1.0, 1.0]);
        let strip = clip(Rect { x: 512, y: 640, w: 256, h: 160 }, 1280, 800);
        for (got, want) in strip.into_iter().zip([-0.2, 0.6, 0.2, 1.0]) {
            assert!((got - want).abs() < 1e-6, "{strip:?}");
        }
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

/// Run `draw` into an off-screen `w` by `h` framebuffer and return its RGBA
/// rows, top row first.
///
/// # Safety
/// Needs a current GL context; `draw` must draw into the bound framebuffer.
pub unsafe fn capture(
    gl: &glow::Context,
    w: u32,
    h: u32,
    draw: impl FnOnce() -> Result<(), String>,
) -> Result<Vec<u8>, String> {
    let fb = gl.create_framebuffer()?;
    let rb = gl.create_renderbuffer()?;
    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fb));
    gl.bind_renderbuffer(glow::RENDERBUFFER, Some(rb));
    gl.renderbuffer_storage(glow::RENDERBUFFER, glow::RGBA8, w as i32, h as i32);
    gl.framebuffer_renderbuffer(glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::RENDERBUFFER, Some(rb));
    let result = if gl.check_framebuffer_status(glow::FRAMEBUFFER) != glow::FRAMEBUFFER_COMPLETE {
        Err("off-screen framebuffer is incomplete".to_string())
    } else {
        draw()
    };
    let mut raw = vec![0u8; (w * h * 4) as usize];
    if result.is_ok() {
        gl.finish();
        gl.pixel_store_i32(glow::PACK_ALIGNMENT, 1);
        gl.read_pixels(0, 0, w as i32, h as i32, glow::RGBA, glow::UNSIGNED_BYTE, glow::PixelPackData::Slice(Some(&mut raw)));
    }
    gl.bind_framebuffer(glow::FRAMEBUFFER, None);
    gl.delete_framebuffer(fb);
    gl.delete_renderbuffer(rb);
    result?;
    // GL rows run bottom-up; images run top-down.
    let stride = (w * 4) as usize;
    let mut out = vec![0u8; raw.len()];
    for row in 0..h as usize {
        let src = (h as usize - 1 - row) * stride;
        out[row * stride..(row + 1) * stride].copy_from_slice(&raw[src..src + stride]);
    }
    Ok(out)
}

pub struct Renderer {
    canvas: Option<Canvas>,
    program: glow::Program,
    vao: glow::VertexArray,
    u_res: Option<glow::UniformLocation>,
    u_origin: Option<glow::UniformLocation>,
    u_e: Option<glow::UniformLocation>,
    u_fill: Option<glow::UniformLocation>,
    u_core: Option<glow::UniformLocation>,
    u_halo: Option<glow::UniformLocation>,
    u_grid: Option<glow::UniformLocation>,
    u_pitch: Option<glow::UniformLocation>,
    u_glow_r: Option<glow::UniformLocation>,
    u_marks: Option<glow::UniformLocation>,
    quad: glow::Program,
    u_rect: Option<glow::UniformLocation>,
    u_tex: Option<glow::UniformLocation>,
    u_alpha: Option<glow::UniformLocation>,
}

/// A picture on the GPU: the panel, or a block of overlay text.
pub struct Texture {
    tex: glow::Texture,
    pub w: i32,
    pub h: i32,
}

unsafe fn program(gl: &glow::Context, header: &str, vert: &str, frag: &str) -> Result<glow::Program, String> {
    let program = gl.create_program()?;
    let mut shaders = Vec::new();
    for (kind, src) in [(glow::VERTEX_SHADER, vert), (glow::FRAGMENT_SHADER, frag)] {
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
    Ok(program)
}

/// A pixel rectangle as clip-space corners on a `w` by `h` screen.
pub fn clip(rect: Rect, w: i32, h: i32) -> [f32; 4] {
    let (w, h) = (w.max(1) as f32, h.max(1) as f32);
    [
        rect.x as f32 / w * 2.0 - 1.0,
        rect.y as f32 / h * 2.0 - 1.0,
        (rect.x + rect.w) as f32 / w * 2.0 - 1.0,
        (rect.y + rect.h) as f32 / h * 2.0 - 1.0,
    ]
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
        let eyes = program(gl, header, VERT, FRAG)?;
        let quad = program(gl, header, QUAD_VERT, QUAD_FRAG)?;
        let vao = gl.create_vertex_array()?;
        let loc = |program, name: &str| gl.get_uniform_location(program, name);
        Ok(Renderer {
            canvas: None,
            program: eyes,
            vao,
            u_res: loc(eyes, "uRes"),
            u_origin: loc(eyes, "uOrigin"),
            u_e: loc(eyes, "uE"),
            u_fill: loc(eyes, "uFill"),
            u_core: loc(eyes, "uCore"),
            u_halo: loc(eyes, "uHalo"),
            u_grid: loc(eyes, "uGrid"),
            u_pitch: loc(eyes, "uPitch"),
            u_glow_r: loc(eyes, "uGlowR"),
            u_marks: loc(eyes, "uMarks"),
            quad,
            u_rect: loc(quad, "uRect"),
            u_tex: loc(quad, "uTex"),
            u_alpha: loc(quad, "uAlpha"),
        })
    }

    /// Draw the eyes into `rect` of whatever framebuffer is bound.
    ///
    /// # Safety
    /// Needs the GL context this renderer was made with to be current.
    pub unsafe fn draw_in(&self, gl: &glow::Context, rect: Rect, eyes: &[f32; 32], look: &Look) {
        let Rect { x, y, w, h } = rect;
        gl.viewport(x, y, w, h);
        gl.use_program(Some(self.program));
        gl.bind_vertex_array(Some(self.vao));
        gl.uniform_2_f32(self.u_res.as_ref(), w as f32, h as f32);
        gl.uniform_2_f32(self.u_origin.as_ref(), x as f32, y as f32);
        gl.uniform_4_f32_slice(self.u_e.as_ref(), eyes);
        for (at, c) in [(&self.u_fill, look.fill), (&self.u_core, look.core), (&self.u_halo, look.halo)] {
            let [r, g, b] = linear(c);
            gl.uniform_3_f32(at.as_ref(), r, g, b);
        }
        let (pitch, depth) = grid(h, look.grid);
        gl.uniform_1_f32(self.u_grid.as_ref(), depth);
        gl.uniform_1_f32(self.u_pitch.as_ref(), pitch);
        gl.uniform_1_f32(self.u_glow_r.as_ref(), look.glow_r);
        gl.uniform_4_f32_slice(self.u_marks.as_ref(), &look.marks.uniforms());
        gl.draw_arrays(glow::TRIANGLES, 0, 3);
    }

    /// Draw into whatever framebuffer is bound, `w` by `h` pixels.
    ///
    /// # Safety
    /// Needs the GL context this renderer was made with to be current.
    pub unsafe fn draw(&self, gl: &glow::Context, w: i32, h: i32, eyes: &[f32; 32], look: &Look) {
        self.draw_in(gl, Rect { x: 0, y: 0, w, h }, eyes, look);
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
        self.draw_scaled_in(gl, Rect { x: 0, y: 0, w, h }, scale, eyes, look)
    }

    /// As `draw_scaled`, into `rect` of the window (the eye strip).
    ///
    /// # Safety
    /// Needs the GL context this renderer was made with to be current.
    pub unsafe fn draw_scaled_in(
        &mut self,
        gl: &glow::Context,
        rect: Rect,
        scale: i32,
        eyes: &[f32; 32],
        look: &Look,
    ) -> Result<(), String> {
        if scale <= 1 {
            self.draw_in(gl, rect, eyes, look);
            return Ok(());
        }
        let (cw, ch) = ((rect.w / scale).max(16), (rect.h / scale).max(16));
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
        let Rect { x, y, w, h } = rect;
        gl.blit_framebuffer(0, 0, cw, ch, x, y, x + w, y + h, glow::COLOR_BUFFER_BIT, glow::LINEAR);
        gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        Ok(())
    }

    /// A texture for premultiplied RGBA rows, top row first.
    ///
    /// # Safety
    /// Needs the GL context this renderer was made with to be current.
    pub unsafe fn texture(&self, gl: &glow::Context, w: i32, h: i32, rgba: &[u8]) -> Result<Texture, String> {
        let tex = gl.create_texture()?;
        let texture = Texture { tex, w, h };
        self.replace(gl, &texture, rgba);
        Ok(texture)
    }

    /// Replace a whole texture of the same size.
    ///
    /// # Safety
    /// Needs the GL context this renderer was made with to be current.
    pub unsafe fn replace(&self, gl: &glow::Context, texture: &Texture, rgba: &[u8]) {
        gl.bind_texture(glow::TEXTURE_2D, Some(texture.tex));
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
        for (parameter, value) in [
            (glow::TEXTURE_MIN_FILTER, glow::LINEAR),
            (glow::TEXTURE_MAG_FILTER, glow::LINEAR),
            (glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE),
            (glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE),
        ] {
            gl.tex_parameter_i32(glow::TEXTURE_2D, parameter, value as i32);
        }
        gl.tex_image_2d(
            glow::TEXTURE_2D,
            0,
            glow::RGBA8 as i32,
            texture.w,
            texture.h,
            0,
            glow::RGBA,
            glow::UNSIGNED_BYTE,
            glow::PixelUnpackData::Slice(Some(rgba)),
        );
    }

    /// Upload rows `first..first+count` of a picture whose rows are packed
    /// in `rgba` (all of it, top row first).
    ///
    /// # Safety
    /// Needs the GL context this renderer was made with to be current.
    pub unsafe fn upload_rows(&self, gl: &glow::Context, texture: &Texture, first: i32, count: i32, rgba: &[u8]) {
        let stride = (texture.w * 4) as usize;
        let rows = &rgba[first as usize * stride..(first + count) as usize * stride];
        gl.bind_texture(glow::TEXTURE_2D, Some(texture.tex));
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
        gl.tex_sub_image_2d(
            glow::TEXTURE_2D,
            0,
            0,
            first,
            texture.w,
            count,
            glow::RGBA,
            glow::UNSIGNED_BYTE,
            glow::PixelUnpackData::Slice(Some(rows)),
        );
    }

    /// # Safety
    /// Needs the GL context this renderer was made with to be current.
    pub unsafe fn delete(&self, gl: &glow::Context, texture: Texture) {
        gl.delete_texture(texture.tex);
    }

    /// Draw a texture into `rect` of a `w` by `h` window, faded by `alpha`.
    ///
    /// # Safety
    /// Needs the GL context this renderer was made with to be current.
    pub unsafe fn draw_texture(&self, gl: &glow::Context, texture: &Texture, rect: Rect, w: i32, h: i32, alpha: f32) {
        gl.viewport(0, 0, w, h);
        gl.use_program(Some(self.quad));
        gl.bind_vertex_array(Some(self.vao));
        gl.active_texture(glow::TEXTURE0);
        gl.bind_texture(glow::TEXTURE_2D, Some(texture.tex));
        gl.uniform_1_i32(self.u_tex.as_ref(), 0);
        gl.uniform_1_f32(self.u_alpha.as_ref(), alpha.clamp(0.0, 1.0));
        let [x0, y0, x1, y1] = clip(rect, w, h);
        gl.uniform_4_f32(self.u_rect.as_ref(), x0, y0, x1, y1);
        gl.enable(glow::BLEND);
        gl.blend_func(glow::ONE, glow::ONE_MINUS_SRC_ALPHA);
        gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
        gl.disable(glow::BLEND);
    }

    /// Render the eyes alone off-screen and return RGBA rows, top row first.
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
        capture(gl, w, h, || {
            self.draw(gl, w as i32, h as i32, eyes, look);
            Ok(())
        })
    }
}
