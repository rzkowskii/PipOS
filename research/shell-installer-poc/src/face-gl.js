// PipGL: draws Pip's eyes with the real shader (crates/face/src/eye.vert and
// eye.frag, inlined verbatim by build.py) the way render.rs draw_in() does.
// Rects are in GL window pixels, origin bottom-left, like panel::Rect.
(function () {
  'use strict';
  function compile(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || 'shader failed');
    return s;
  }
  // render.rs: fn linear(c) = c * c (gamma 2, matching the shader's output).
  const linear = (c) => c.map((v) => v * v);
  // render.rs: fn grid(h, depth): about 266 cells down the screen.
  function grid(h, depth) {
    const cells = h / 266.0;
    const pitch = Math.max(2, Math.round(cells));
    const d = cells < 1.5 ? 0 : pitch < 3 ? depth * 0.5 : depth;
    return [pitch, d];
  }
  function create(canvas, src) {
    const gl = canvas.getContext('webgl2', { antialias: false, alpha: false, premultipliedAlpha: false, preserveDrawingBuffer: true });
    if (!gl) throw new Error('WebGL2 is not available');
    const prog = gl.createProgram();
    gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, src.header + src.vert));
    gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, src.header + src.frag));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog) || 'link failed');
    const vao = gl.createVertexArray();
    const L = (n) => gl.getUniformLocation(prog, n);
    const u = { res: L('uRes'), origin: L('uOrigin'), e: L('uE'), fill: L('uFill'), core: L('uCore'), halo: L('uHalo'), grid: L('uGrid'), pitch: L('uPitch'), glowR: L('uGlowR'), marks: L('uMarks') };
    return {
      gl,
      clear() {
        gl.viewport(0, 0, canvas.width, canvas.height);
        gl.clearColor(0, 0, 0, 1);
        gl.clear(gl.COLOR_BUFFER_BIT);
      },
      // draw_in(rect, eyes[32], look): look = {fill, core, halo, grid, glow_r, marks:{glint,ring,ears,network,ssh}, markLevel}
      draw(rect, eyes, look) {
        gl.viewport(rect.x, rect.y, rect.w, rect.h);
        gl.useProgram(prog);
        gl.bindVertexArray(vao);
        gl.uniform2f(u.res, rect.w, rect.h);
        gl.uniform2f(u.origin, rect.x, rect.y);
        gl.uniform4fv(u.e, eyes);
        gl.uniform3fv(u.fill, linear(look.fill));
        gl.uniform3fv(u.core, linear(look.core));
        gl.uniform3fv(u.halo, linear(look.halo));
        const [pitch, depth] = grid(rect.h, look.grid);
        gl.uniform1f(u.grid, depth);
        gl.uniform1f(u.pitch, pitch);
        gl.uniform1f(u.glowR, look.glow_r);
        const m = look.marks || {};
        gl.uniform4fv(u.marks, [m.glint || 0, m.ring || 0, m.ears || 0, m.network || 0, m.ssh || 0, look.markLevel, 0, 0]);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
      },
      // lab/vm.py eyes(): lit = r+g+b > 120, cyan = b > r+30 and g > r, split at the centre.
      labCheck(step = 2) {
        const w = canvas.width, h = canvas.height, px = new Uint8Array(w * h * 4);
        gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
        let lit = 0, cyan = 0;
        const boxes = [null, null];
        for (let y = 0; y < h; y += step) for (let x = 0; x < w; x += step) {
          const i = (y * w + x) * 4, r = px[i], g = px[i + 1], b = px[i + 2];
          if (r + g + b <= 120) continue;
          lit++;
          if (b > r + 30 && g > r) cyan++;
          const yy = h - 1 - y, s = x < w / 2 ? 0 : 1, bx = boxes[s];
          boxes[s] = bx ? [Math.min(bx[0], x), Math.min(bx[1], yy), Math.max(bx[2], x), Math.max(bx[3], yy)] : [x, yy, x, yy];
        }
        return { lit, cyan: lit ? cyan / lit : 0, boxes };
      },
    };
  }
  // panel.rs layout(): eyes glide into a strip at the top centre as amount goes 0 to 1.
  function smoothstep(x) { x = Math.min(1, Math.max(0, x)); return x * x * (3 - 2 * x); }
  function layout(w, h, amount, stripFraction) {
    const s = smoothstep(amount);
    const stripH = Math.max(1, Math.round(h * stripFraction));
    const stripW = Math.round(stripH * w / Math.max(1, h));
    const lerp = (a, b) => Math.round(a + (b - a) * s);
    return {
      eyes: { x: lerp(0, (w - stripW) / 2), y: lerp(0, h - stripH), w: lerp(w, stripW), h: lerp(h, stripH) },
      panel: { x: 0, y: 0, w, h: h - stripH },
      panelAlpha: s,
    };
  }
  // panel.rs advance_transition(): hold either endpoint until the surface changes.
  function advanceTransition(amount, open, dt, transition) {
    const step = Math.min(dt, 0.1) / transition;
    return open ? Math.min(1, amount + step) : Math.max(0, amount - step);
  }
  // panel.rs gaze_at(): where the eyes look for a point on the panel.
  function gazeAt(x, y, width, height) {
    const fx = x / Math.max(1, width), fy = y / Math.max(1, height);
    return [Math.max(-1, Math.min(1, (fx * 2 - 1) * 0.9)), Math.max(-1, Math.min(1, -0.55 - 0.4 * fy))];
  }
  const api = { create, layout, gazeAt, smoothstep, grid, advanceTransition };
  if (typeof window !== 'undefined') window.PipGL = api;
  if (typeof module !== 'undefined') module.exports = api;
})();
