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
#ifdef FACE_DECOR
// Two vec4s per brow, four for the mouth, two for optional eye details.
uniform vec4 uDecor[10];
uniform vec4 uGlitch; // three signed offsets and a presence flag
#endif

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

#ifdef FACE_DECOR
float stroke(float d, float px) {
    return clamp(0.5 - d / px, 0.0, 1.0);
}

float decorCoverage(vec2 p, float px) {
    if (abs(p.x) > min(1.38, uRes.x / uRes.y - 0.12) || abs(p.y) > 0.86) return 0.0;
    float result = 0.0;
    for (int i = 0; i < 2; i++) {
        vec4 shape = uDecor[2 * i];
        vec3 ink = uDecor[2 * i + 1].xyz; // curve, thickness, alpha
        if (ink.z <= 0.0) continue;
        vec2 eye = uE[i * 4].xy;
        float width = max(0.05, shape.z);
        float slope = 0.60 * shape.w;
        float arc = 0.10 * ink.x;
        float radius = 0.5 * max(0.02, ink.y);
        float inward = i == 0 ? 1.0 : -1.0;
        vec2 center = vec2(eye.x + inward * shape.y, eye.y + shape.x);
        center.y = min(center.y, 0.85 - width * abs(slope) - max(arc, 0.0) - radius);
        vec2 q = p - center;
        if (abs(q.x) > width + radius + px || abs(q.y) > width * abs(slope) + abs(arc) + radius + px) continue;
        float along = clamp(inward * q.x / width, -1.0, 1.0);
        float y = slope * width * along + arc * (1.0 - along * along);
        float d = length(vec2(q.x - inward * along * width, q.y - y)) - radius;
        result = max(result, ink.z * stroke(d, px));
    }

    vec4 m = uDecor[4]; // x, half-width, y, corner height
    vec4 curve = uDecor[5]; // skew, arc, opening, roundness
    vec4 detail = uDecor[6]; // cat curve, wave, zigzag, tongue
    vec3 ink = uDecor[7].xyz; // lower lip, alpha, wave phase
    vec2 eyeCenter = 0.5 * (uE[0].xy + uE[4].xy);
    float gap = min(0.26 * curve.z, 0.22);
    float center = max(m.z + m.w + 0.20 * eyeCenter.y, -0.80 + gap + ink.x + max(curve.y, 0.0));
    // The window's top follows the raised centre, so a wide-open mouth such
    // as horror keeps its upper lip: skew, an upturned arc, wave, zigzag, the
    // stroke's cap and a pixel are the most it can rise above the centre.
    float reach = 0.5 * abs(curve.x) + max(-curve.y, 0.0) + 0.022 * abs(detail.y) + 0.014 * abs(detail.z) + 0.017 + px;
    if (ink.y > 0.0 && abs(p.x - m.x) < m.y + 0.15 && p.y > -0.86 && p.y < max(center + reach, -0.32)) {
        float width = max(0.025, m.y);
        float roundness = clamp(max(curve.w, gap / (2.0 * width) * 1.1 - 0.35), 0.0, 1.0);
        float x = p.x - m.x - 0.20 * eyeCenter.x;
        float along = clamp(x / width, -1.0, 1.0);
        float profile = max(1.0 - along * along, 0.0);
        float cat = 2.0 * abs(along) - 1.0;
        float catDip = 0.07 * clamp(width / 0.2, 0.4, 1.4) * (1.0 - cat * cat);
        float wave = detail.y * 0.022 * sin(along * 8.79645943 + ink.z) * pow(profile, 0.35);
        float zig = detail.z * 0.014 * (2.0 * abs(mod(along * 4.0 + 1.0, 2.0) - 1.0) - 1.0) * pow(profile, 0.2);
        float upper = center + 0.5 * curve.x * along - mix(curve.y * profile, catDip, detail.x) + wave + zig;
        float lower = upper - gap * pow(profile, mix(0.90, 0.50, roundness)) - ink.x * profile + 0.6 * wave - 2.0 * zig;
        float end = x - along * width;
        float d = length(vec2(end, p.y - upper));
        if (gap + ink.x > 0.014) {
            d = min(d, length(vec2(end, p.y - lower)));
            if (abs(along) < 1.0 && p.y < upper && p.y > lower) {
                result = max(result, 0.17 * ink.y);
                if (detail.w > 0.03) {
                    float height = max(0.015, gap * 0.44 * detail.w);
                    float tongue = length(vec2(along / 0.53, (p.y - lower - 0.2 * height) / height));
                    result = max(result, ink.y * 0.75 * stroke((tongue - 1.0) * height, px));
                }
            }
        }
        result = max(result, ink.y * stroke(d - 0.017, px));
    }
    return result;
}

float sdHeart(vec2 p) {
    p.x = abs(p.x);
    if (p.x + p.y > 1.0) return length(p - vec2(0.25, 0.75)) - 0.35355339;
    vec2 a = p - vec2(0.0, 1.0);
    vec2 b = p - 0.5 * max(p.x + p.y, 0.0);
    return sqrt(min(dot(a, a), dot(b, b))) * sign(p.x - p.y);
}

float sdStar(vec2 p) {
    // Fold onto the wedge from an outer tip to its nearest inner corner.
    const float sector = 1.25663706;
    float a = abs(mod(atan(p.x, p.y + 1e-6) + 0.5 * sector, sector) - 0.5 * sector);
    p = length(p) * vec2(sin(a), cos(a));
    vec2 tip = vec2(0.0, 1.0), valley = vec2(0.26450, 0.36406);
    vec2 edge = valley - tip, q = p - tip;
    return sdSegment(p, tip, valley) * sign(edge.x * q.y - edge.y * q.x);
}

float specialEye(vec2 q, float radius, vec4 shape, float phase) {
    float d = 2.0;
    float strength = max(max(shape.x, shape.y), max(shape.z, shape.w));
    if (shape.x > 0.0) {
        float size = radius * (1.0 + 0.05 * sin(phase));
        d = min(d, sdHeart(q * (0.63 / size) + vec2(0.0, 0.49)) * size / 0.63 + 0.22 * (strength - shape.x));
    }
    if (shape.y > 0.0) {
        vec2 p = abs(q);
        d = min(d, sdSegment(p, vec2(0.0), vec2(0.63 * radius)) - 0.041 + 0.22 * (strength - shape.y));
    }
    if (shape.z > 0.0) {
        float angle = atan(q.y, q.x + 1e-6) - phase;
        float radial = length(q);
        float turn = radius / 2.6;
        float space = (radial / turn - angle / 6.28318531);
        float spiral = abs(space - round(space)) * turn;
        d = min(d, max(spiral - 0.025, radial - radius) + 0.22 * (strength - shape.z));
    }
    if (shape.w > 0.0) {
        float angle = 0.18 * sin(phase);
        vec2 p = mat2(cos(angle), -sin(angle), sin(angle), cos(angle)) * q;
        float size = 1.07 * radius * (1.0 + 0.04 * sin(phase));
        d = min(d, sdStar(p / size) * size + 0.22 * (strength - shape.w));
    }
    return d;
}

#endif

void main() {
    vec2 p = (gl_FragCoord.xy - uOrigin - 0.5 * uRes) / (0.5 * uRes.y);
    float px = 1.0 / (0.5 * uRes.y);
    vec2 eyePoint = p;
#ifdef FACE_DECOR
    if (uGlitch.w > 0.0 && uMarks[0].x + uMarks[0].y == 0.0) {
        vec3 centers = vec3(-0.16, 0.015, 0.19);
        vec3 widths = vec3(0.025, 0.041, 0.030);
        vec3 bands = 1.0 - smoothstep(widths, widths + 0.004, abs(p.y - centers));
        eyePoint.x += dot(uGlitch.xyz, bands);
    }
#endif

    vec3 lit = vec3(0.0); // the eyes themselves
    float cover = 0.0;    // how much of this pixel an eye covers
    float halo = 0.0;     // bloom from both eyes
#ifdef FACE_DECOR
    vec4 special = uDecor[8];
    float morph = clamp(max(max(special.x, special.y), max(special.z, special.w)), 0.0, 1.0);
#endif
    for (int i = 0; i < 2; i++) {
        vec4 eye = uE[i * 4];
        vec2 bounds = vBounds[i];
#ifdef FACE_DECOR
        float radius = min(eye.z, 1.12 * eye.w);
        if (morph > 0.0) bounds = max(bounds, vec2(1.20 * radius + uGlowR));
#endif
        if (any(greaterThan(abs(eyePoint - eye.xy), bounds))) {
            continue;
        }
        vec2 u;
        vec4 lid = uE[i * 4 + 1], crease = uE[i * 4 + 2];
        float d = sdEye(eyePoint, eye, lid, crease, vShape[i].xyz, vEllipse[i], u);
#ifdef FACE_DECOR
        vec4 details = uDecor[9]; // pupil, shine, wet lower edge, phase
        vec2 q = eyePoint - eye.xy;
        float aperture = lid.z + crease.x - crease.y - crease.w;
        if (morph > 0.0) {
            vec2 rotation = vShape[i].xy;
            vec2 local = vec2(dot(q, rotation), dot(q, vec2(-rotation.y, rotation.x)));
            float phase = (i == 0 ? details.w : -details.w);
            float change = morph * smoothstep(0.10, 0.75, aperture);
            d = mix(d, specialEye(local, radius, special, phase), change);
        }
#endif
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
#ifdef FACE_DECOR
        if (c > 0.0 && details.x + details.y + details.z > 0.0) {
            if (details.x > 0.0 && aperture > 0.40) {
                float pupil = stroke(length(q - vec2(0.04 * eye.x, 0.02)) - details.x * rmin, px);
                colour *= 1.0 - 0.94 * pupil * smoothstep(0.40, 0.95, aperture);
            }
            if (details.y + details.z > 0.0) {
                float shine = 0.0;
                if (uMarks[0].x <= 0.0) {
                    // Both highlights sit on the right, well clear of the capture
                    // glint's spot up and to the left, so a shine never reads as
                    // the camera being on. While it is, they go out entirely.
                    shine = stroke(length(q - eye.zw * vec2(0.30, 0.30)) - 0.14 * rmin * details.y, px);
                    shine = max(shine, 0.7 * stroke(length(q - eye.zw * vec2(0.30, -0.28)) - 0.08 * rmin * details.y, px));
                }
                float wet = 1.0 - smoothstep(-0.82, -0.20, q.y / eye.w);
                colour = mix(colour, uCore, clamp(0.9 * shine * details.y + 0.7 * wet * details.z, 0.0, 1.0));
            }
        }
#endif
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
    // Only the eyes receive the LED grid. The thin strokes keep their outline.
    float decor = 0.0;
#ifdef FACE_DECOR
    if (uDecor[1].z + uDecor[3].z + uDecor[7].y > 0.0) {
        decor = decorCoverage(p, px) * (1.0 - cover);
    }
#endif
    if (cover == 0.0 && halo == 0.0 && decor == 0.0) {
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

#ifdef FACE_DECOR
    if (decor > 0.0) {
        // Strokes follow the eyes' glow, so they never outshine dimmed or
        // blinking eyes, and their green stays below 0.59 linear (about 196
        // on screen): the lab reads G >= 200 as an eye's core.
        vec3 ink = mix(uFill, uCore, 0.26) * 0.82;
        ink *= min(1.0, 0.59 / max(ink.g, 1e-4));
        // Speech and bounce lift eye glow above 1; the cap must still hold.
        float glow = min(1.0, 0.5 * (uE[3].x * uE[3].x + uE[7].x * uE[7].x));
        lit = max(lit, ink * (glow * decor * decor));
    }
#endif
    vec3 col = lit + uHalo * (halo * (1.0 - max(cover, decor)));
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
