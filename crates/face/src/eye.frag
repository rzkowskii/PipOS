// Two glowing eyes on black. Each eye is a signed-distance shape:
// an ellipse (optionally squared off a little) cut by an upper and a lower lid.
//
// Light is added up in linear terms and encoded for the screen once, at the
// end, so the bloom fades into black the way real light does rather than in
// visible steps. Colours arrive already linear (see render.rs: Look).
//
// Units: the screen is 2 high, centred on the origin, y up.
// The version header is prepended at load time (desktop GL or GLES).

flat in vec4 vShape[2];
flat in vec2 vBounds[2];
flat in vec4 vEllipse[2];
out vec4 frag;

uniform vec2 uRes;     // drawable size in pixels
uniform vec4 uE[8];    // two eyes, four vec4 each (see eye.rs: Pose::uniforms)
uniform vec3 uFill;    // colour at the rim of an eye, linear
uniform vec3 uCore;    // hotter colour at its centre, linear
uniform vec3 uHalo;    // bloom colour, linear
uniform float uGrid;   // LED-grid depth, 0..1
uniform float uPitch;  // LED-grid pitch in whole pixels
uniform float uGlowR;  // reach of the bloom in screen units; it is zero beyond
uniform vec2 uOrigin;  // where this viewport starts in the window, in pixels
// Privacy marks (render.rs: Marks): glint, guest ring, covered ears, network
// dot; then SSH mark and the fixed marks' level on screen.
uniform vec4 uMarks[2];

// Distance to a segment.
float sdSegment(vec2 p, vec2 a, vec2 b) {
    vec2 pa = p - a, ba = b - a;
    float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
    return length(pa - ba * h);
}

// The fixed marks, as a colour already encoded for the screen. They stay
// dim (render.rs: MARK_LEVEL), so the lab never counts them as eyes.
vec3 fixedMarks(vec2 p, float px) {
    float aspect = uRes.x / uRes.y;
    float level = uMarks[1].y;
    float m = 0.0;
    if (uMarks[0].z > 0.0) {
        // Covered ears: a short rounded bar at each side of the face.
        vec2 q = vec2(abs(p.x) - (aspect - 0.075), p.y);
        float d = length(vec2(q.x, max(abs(q.y) - 0.11, 0.0))) - 0.018;
        m = max(m, uMarks[0].z * clamp(0.5 - d / px, 0.0, 1.0));
    }
    if (uMarks[0].w > 0.0) {
        // Network: a dot in the top right corner.
        float d = length(p - vec2(aspect - 0.075, 0.915)) - 0.017;
        m = max(m, uMarks[0].w * clamp(0.5 - d / px, 0.0, 1.0));
    }
    if (uMarks[1].x > 0.0) {
        // SSH session: a small ">_" prompt in the top left corner.
        vec2 o = vec2(-aspect + 0.06, 0.915);
        float d = min(sdSegment(p, o + vec2(0.0, 0.022), o + vec2(0.022, 0.0)),
                      sdSegment(p, o + vec2(0.022, 0.0), o + vec2(0.0, -0.022)));
        d = min(d, sdSegment(p, o + vec2(0.036, -0.024), o + vec2(0.070, -0.024))) - 0.005;
        m = max(m, uMarks[1].x * clamp(0.5 - d / px, 0.0, 1.0));
    }
    return sqrt(uFill) * (level * m);
}

// Distance to an ellipse with half-axes ab. Three rounds of the closest-point
// iteration are accurate at any aspect ratio, which a one-step estimate is not:
// thin eyes need it for an even glow.
float sdEllipse(vec2 pa, vec2 ab, vec2 inv, vec2 ee, vec2 u) {
    vec2 t = vec2(0.70710678);
    for (int i = 0; i < 3; i++) {
        vec2 e = ee * t * t * t;
        vec2 rr = ab * t - e;
        vec2 qq = pa - e;
        t = clamp((qq * (length(rr) * inversesqrt(max(dot(qq, qq), 1e-12))) + e) * inv, 0.0, 1.0);
        t *= inversesqrt(max(dot(t, t), 1e-12));
    }
    float d = length(pa - ab * t);
    return dot(u, u) > 1.0 ? d : -d;
}

// Maximum with the crease rounded off over a width k.
float smax(float a, float b, float k) {
    float h = max(k - abs(a - b), 0.0) / k;
    return max(a, b) + h * h * k * 0.25;
}

// Also returns, in u, the point in the ellipse's unit-circle frame.
float sdEye(vec2 p, vec4 a, vec4 b, vec4 c, vec3 shape, vec4 ellipse, out vec2 u) {
    // a = (x, y, w, h)  b = (round, tilt, top, topSlant)
    // c = (topCurve, bot, botSlant, botCurve)
    vec2 q = p - a.xy;
    float cs = shape.x, sn = shape.y;
    q = vec2(cs * q.x + sn * q.y, -sn * q.x + cs * q.y);
    vec2 r = max(a.zw, vec2(1e-4));

    // "round" above 2 pushes the diagonals outward, toward a squircle.
    u = abs(q) * ellipse.xy;
    float dBase = sdEllipse(abs(q), r, ellipse.xy, ellipse.zw, u);
    float diag = 2.0 * u.x * u.y / max(dot(u, u), 1e-6);
    dBase -= shape.z * diag * diag;

    // Lids are parabolas in the eye's own frame, heights in units of h.
    float xn = q.x * ellipse.x;
    float bow = 1.0 - xn * xn;
    float yt = r.y * (b.z + b.w * xn + c.x * bow);
    float st = r.y * (b.w - 2.0 * c.x * xn) * ellipse.x;
    float dTop = (q.y - yt) / sqrt(1.0 + st * st);
    float yb = r.y * (c.y + c.z * xn + c.w * bow);
    float sb = r.y * (c.z - 2.0 * c.w * xn) * ellipse.x;
    float dBot = (yb - q.y) / sqrt(1.0 + sb * sb);

    float dLid = max(dTop, dBot);
    // Where a lid meets the outline the corner is rounded over 0.04: enough
    // to melt the knee where the resting egg's lids pinch it, and to soften
    // concern's and tired's ends, while joy's tips still come to points.
    float d = smax(dBase, dLid, 0.04);
    if (dBase > 0.0 && dLid > 0.0) {
        d = max(d, length(vec2(dBase, dLid)));
    }
    return d;
}

void main() {
    vec2 p = (gl_FragCoord.xy - uOrigin - 0.5 * uRes) / (0.5 * uRes.y);
    float px = 1.0 / (0.5 * uRes.y);

    vec3 lit = vec3(0.0); // the eyes themselves
    float cover = 0.0;    // how much of this pixel an eye covers
    float halo = 0.0;     // bloom from both eyes
    for (int i = 0; i < 2; i++) {
        vec4 eye = uE[i * 4];
        if (any(greaterThan(abs(p - eye.xy), vBounds[i]))) {
            continue;
        }
        vec2 u;
        float d = sdEye(p, eye, uE[i * 4 + 1], uE[i * 4 + 2], vShape[i].xyz, vEllipse[i], u);
        // Glow is a brightness on screen; squared, it scales linear light.
        float g = uE[i * 4 + 3].x;
        float g2 = g * g;

        // A one-pixel ramp: crisp, but no stair steps.
        float c = clamp(0.5 - d / px, 0.0, 1.0);
        // Hotter toward the middle of the ellipse, so a crescent cut from its
        // top stays the rim colour, and easing off just inside every edge.
        // (Depth alone would show creases along the shape's midlines.)
        float rmin = 1.0 / max(vEllipse[i].x, vEllipse[i].y);
        float t = max(1.0 - dot(u, u), 0.0) * smoothstep(0.0, 0.45 * rmin, -d);
        // A dimmed eye loses its hot centre first, so it stays a deep cyan
        // instead of fading to grey.
        t *= clamp(2.5 * g - 1.5, 0.0, 1.0);
        // Further down, the rim itself deepens toward the bloom's blue, the
        // way a dim light looks bluer, rather than greying into teal.
        vec3 rim = mix(uFill, uHalo, 0.5 * clamp(1.5 - 1.5 * g, 0.0, 1.0));
        vec3 colour = mix(rim, uCore, t);
        if (uMarks[0].x > 0.0) {
            // The camera may capture: a small glint up and to the left in
            // each eye, in the eye's own hottest colour (never white).
            vec2 at = eye.xy + vec2(-0.34 * eye.z, 0.36 * eye.w);
            float r = 0.16 * min(eye.z, eye.w);
            float glint = clamp(1.0 - length(p - at) / r, 0.0, 1.0);
            colour = mix(colour, uCore * 1.35, uMarks[0].x * smoothstep(0.0, 0.45, glint));
        }
        lit = max(lit, colour * (c * g2));
        cover = max(cover, c);
        if (uMarks[0].y > 0.0) {
            // Guest mode: a soft ring just outside each eye.
            float ring = 1.0 - smoothstep(0.0, 0.012, abs(d - 0.05));
            halo += uMarks[0].y * 0.22 * g2 * ring;
        }

        // Bloom: a bright skin hugging the edge, then a broad soft falloff
        // that reaches exactly zero at uGlowR, with no edge of its own.
        float o = max(d, 0.0);
        float w = 1.0 - min(o / uGlowR, 1.0);
        w *= w;
        halo += g2 * (0.34 * exp(-o / (0.055 * uGlowR)) + 0.10 * w * w);
    }

    vec3 marks = vec3(0.0);
    if (uMarks[0].z + uMarks[0].w + uMarks[1].x > 0.0) {
        marks = fixedMarks(p, px);
    }
    if (cover == 0.0 && halo == 0.0) {
        frag = vec4(marks, 1.0);
        return;
    }

    // A fine screen-door of dark lines between LED cells, aligned to pixels.
    if (cover > 0.0 && uGrid > 0.0) {
        ivec2 m = ivec2(gl_FragCoord.xy) % int(uPitch);
        if (m.x == 0 || m.y == 0) {
            lit *= 1.0 - uGrid;
        }
    }

    vec3 col = lit + uHalo * (halo * (1.0 - cover));
    // Encode for the screen (gamma 2), then roll highlights off softly so a
    // bright eye turns cyan-white without a hard clip.
    col = sqrt(col);
    vec3 over = max(col - 0.92, 0.0);
    col = min(col, 0.92) + 0.08 * (1.0 - exp(-over / 0.08));
    // A little ordered noise below one 8-bit step hides banding in the bloom.
    float n = fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(0.06711056, 0.00583715))));
    col += (n - 0.5) / 255.0;
    frag = vec4(max(col, marks), 1.0);
}
