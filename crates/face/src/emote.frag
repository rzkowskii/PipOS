// Each instance draws a small signed-distance glyph in its own local frame.
// The eye shader is independent, so a face with effects off does no glyph work.
in vec2 vLocal;
in float vScreenX;
flat in vec4 vSprite;
out vec4 frag;

uniform vec2 uRes;
uniform vec3 uFill;
uniform vec3 uCore;
uniform vec3 uHalo;
uniform float uAccentEmotes;

const vec3 pink = vec3(255.0, 98.0, 150.0) / 255.0;
const vec3 gold = vec3(255.0, 214.0, 90.0) / 255.0;
const vec3 red = vec3(255.0, 70.0, 50.0) / 255.0;
const vec3 green = vec3(90.0, 255.0, 140.0) / 255.0;

float segment(vec2 p, vec2 a, vec2 b) {
    vec2 d = b - a;
    return length(p - a - d * clamp(dot(p - a, d) / dot(d, d), 0.0, 1.0));
}

float box(vec2 p, vec2 extent) {
    vec2 q = abs(p) - extent;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0);
}

float drop(vec2 p) {
    float base = length(vec2(p.x, p.y + 0.26)) - 0.59;
    float tip = max(abs(p.x) - 0.54 * (0.88 - p.y), max(p.y - 0.88, -p.y - 0.25));
    return min(base, tip);
}

float heart(vec2 p) {
    vec2 q = vec2(abs(p.x), p.y + 0.87) * 0.64;
    if (q.x + q.y > 1.0) return length(q - vec2(0.25, 0.75)) - 0.35355;
    return min(length(q - vec2(0.0, 1.0)), length(q - 0.5 * max(q.x + q.y, 0.0))) * sign(q.x - q.y);
}

float exclaim(vec2 p) {
    float stroke = segment(vec2(p.x * (1.1 - 0.24 * p.y), p.y),
        vec2(0.0, -0.22), vec2(0.0, 0.69)) - 0.15;
    return min(stroke, length(p + vec2(0.0, 0.62)) - 0.16);
}

float question(vec2 p) {
    vec2 q = p - vec2(0.0, 0.36);
    float arc = abs(length(q) - 0.40) - 0.106;
    arc = max(arc, min(-q.y - 0.04, -q.x - 0.05));
    float tip = segment(p, vec2(0.27, 0.06), vec2(0.0, -0.15));
    tip = min(tip, segment(p, vec2(0.0, -0.15), vec2(0.0, -0.29)));
    return min(min(arc, tip - 0.10), length(p + vec2(0.0, 0.63)) - 0.117);
}

float batteryCharge(vec2 p) {
    return box(p + vec2(0.52, 0.0), vec2(0.14, 0.43));
}

float glyph(int kind, vec2 p, float variant) {
    if (kind == 0) return drop(p);
    if (kind == 1) return segment(vec2(p.x * 0.58, p.y), vec2(0.0, -0.55), vec2(0.0, 0.55)) - 0.36;
    if (kind == 2) return heart(p);
    if (kind == 3) {
        vec2 q = abs(p);
        return 0.21 * (min(3.9 * q.x + q.y, q.x + 3.9 * q.y) - 0.90);
    }
    if (kind == 4) {
        float stroke = segment(p, vec2(-0.58, 0.65), vec2(0.58, 0.65));
        stroke = min(stroke, segment(p, vec2(0.58, 0.65), vec2(-0.58, -0.65)));
        return min(stroke, segment(p, vec2(-0.58, -0.65), vec2(0.58, -0.65))) - 0.13;
    }
    if (kind == 5) return question(p);
    if (kind == 6) return exclaim(p);
    if (kind == 7) {
        vec2 q = abs(p);
        float vertical = segment(q, vec2(0.25, 0.78), vec2(0.34, 0.34));
        float horizontal = segment(q, vec2(0.34, 0.34), vec2(0.78, 0.25));
        return min(vertical, horizontal) - 0.096;
    }
    if (kind == 8) return length(p) - 0.75;
    if (kind == 9) {
        float bead = length((p - vec2(-0.29, -0.57)) * vec2(1.0, 1.50)) - 0.39;
        float staff = segment(p, vec2(-0.02, -0.48), vec2(-0.02, 0.70));
        float flag = segment(p, vec2(-0.02, 0.70), vec2(0.48, 0.45));
        flag = min(flag, segment(p, vec2(0.48, 0.45), vec2(0.48, 0.25)));
        float d = min(bead, min(staff, flag) - 0.090);
        if (variant > 0.5) {
            d = min(d, length((p - vec2(0.38, -0.35)) * vec2(1.0, 1.50)) - 0.30);
            d = min(d, segment(p, vec2(0.57, -0.30), vec2(0.57, 0.49)) - 0.082);
        }
        return d;
    }
    if (kind == 10) {
        float lamp = length(p - vec2(0.0, 0.08)) - 0.42;
        lamp = min(lamp, box(p + vec2(0.0, 0.45), vec2(0.20, 0.12)));
        vec2 q = abs(p);
        float reach = 0.82 + 0.10 * variant;
        float ray = segment(q, vec2(0.62, 0.0), vec2(reach, 0.0));
        ray = min(ray, segment(q, vec2(0.48, 0.48), vec2(0.707 * reach)));
        ray = min(ray, segment(q, vec2(0.0, 0.65), vec2(0.0, reach)));
        return min(lamp, ray - 0.055);
    }
    if (kind == 11) return length(p) - 0.75;
    if (kind == 12) {
        float ice = segment(p, vec2(-0.80, 0.0), vec2(0.80, 0.0));
        ice = min(ice, segment(p, vec2(-0.40, -0.69), vec2(0.40, 0.69)));
        return min(ice, segment(p, vec2(-0.40, 0.69), vec2(0.40, -0.69))) - 0.08;
    }
    if (kind == 13) {
        float t = clamp((p.y + 0.83) / 1.76, 0.0, 1.0);
        float width = 0.87 * pow(1.0 - t, 0.72);
        float bend = 0.35 * variant * t * t;
        return max(abs(p.x - bend) - width, max(-p.y - 0.83, p.y - 0.94));
    }
    if (kind == 15) {
        float wall = abs(length(p) - 0.76) - 0.07;
        return min(wall, length(p - vec2(-0.31, 0.36)) - 0.12);
    }
    if (kind == 16) {
        float d = segment(p, vec2(-0.74, -0.09), vec2(-0.24, -0.61));
        return min(d, segment(p, vec2(-0.24, -0.61), vec2(0.73, 0.61))) - 0.145;
    }
    if (kind == 17) {
        float d = segment(p, vec2(-0.77, -0.62), vec2(0.78, -0.62));
        d = min(d, segment(p, vec2(0.78, -0.62), vec2(0.0, 0.83)));
        d = min(d, segment(p, vec2(0.0, 0.83), vec2(-0.77, -0.62)));
        return min(d - 0.095, 0.40 * exclaim((p + vec2(0.0, 0.10)) / 0.43));
    }
    if (kind == 18) {
        float outline = abs(box(p + vec2(0.11, 0.0), vec2(0.70, 0.62))) - 0.071;
        outline = min(outline, box(p - vec2(0.70, 0.0), vec2(0.14, 0.25)));
        return min(outline, batteryCharge(p));
    }
    if (kind == 19) return box(p, vec2(0.73, 0.96));
    return length(p) - 0.93;
}

void main() {
    // Keep the margin for physical/privacy marks, including on narrow windows.
    if (abs(vScreenX) > min(1.38, uRes.x / uRes.y - 0.16)) discard;
    int kind = int(vSprite.x);
    vec2 p = vLocal;
    float alpha;
    if (kind == 14) {
        float band = max(1.0 - abs(p.y), 0.0);
        alpha = 0.36 * band * band + 0.64 * clamp(1.0 - abs(p.y) / 0.13, 0.0, 1.0);
        alpha *= smoothstep(0.0, 0.10, 1.0 - abs(p.x));
    } else {
        float d = glyph(kind, p, vSprite.z);
        alpha = clamp(0.5 - d / vSprite.w, 0.0, 1.0);
    }
    if (kind == 1 && vSprite.z > 0.9) alpha *= smoothstep(-0.92, 0.76, p.y);
    if (kind == 8) alpha *= max(1.0 - dot(p, p) * 1.4, 0.0);
    if (kind == 13) alpha *= 1.0 - smoothstep(-0.70, 0.94, p.y);
    if (kind == 19) alpha *= 1.0 - smoothstep(-0.93, 0.93, p.y);

    vec3 fill = sqrt(uFill), core = sqrt(uCore);
    vec3 ordinary = mix(fill, core, 0.32);
    vec3 accent = ordinary;
    if (kind == 0 || kind == 19) {
        vec3 water = mix(vec3(170.0, 232.0, 255.0) / 255.0, fill, 0.25);
        float glint = kind == 0 ? 0.84 * (1.0 - smoothstep(0.15, 0.25, length(p - vec2(-0.24, 0.14)))) : 0.0;
        accent = mix(water, vec3(1.0), glint);
        ordinary = mix(ordinary, core, glint);
    } else if (kind == 2 || kind == 20 || (kind == 1 && vSprite.z < 0.25)) {
        accent = pink;
    } else if (kind == 3 || (kind == 1 && vSprite.z < 0.9)) {
        accent = mix(core, vec3(1.0), kind == 3 ? 0.5 : 0.4);
    } else if (kind == 1) {
        accent = sqrt(uHalo);
        ordinary = accent;
    } else if (kind == 4) {
        accent = fill;
    } else if (kind == 5 || kind == 9 || kind == 11 || kind == 14) {
        accent = core;
    } else if (kind == 6 || kind == 10 || kind == 17) {
        accent = gold;
        if (kind == 10) {
            float glint = 1.0 - smoothstep(0.08, 0.12, length(p - vec2(-0.12, 0.23)));
            accent = mix(accent, vec3(1.0), 0.80 * glint);
        }
    } else if (kind == 7) {
        accent = red;
    } else if (kind == 8) {
        accent = vec3(255.0, 170.0, 150.0) / 255.0;
    } else if (kind == 12) {
        accent = vec3(220.0, 245.0, 255.0) / 255.0;
    } else if (kind == 13) {
        accent = mix(vec3(255.0, 90.0, 20.0), vec3(255.0, 210.0, 60.0), smoothstep(-0.8, 0.9, p.y)) / 255.0;
    } else if (kind == 15) {
        float glint = 1.0 - smoothstep(0.10, 0.15, length(p - vec2(-0.31, 0.36)));
        accent = mix(core, vec3(1.0), 0.70 * glint);
    } else if (kind == 16) {
        accent = green;
    } else if (kind == 18) {
        float charge = clamp(0.5 - batteryCharge(p) / vSprite.w, 0.0, 1.0);
        accent = mix(fill, red, charge);
        alpha *= mix(1.0, vSprite.z, charge);
    }
    vec3 colour = mix(ordinary, accent, clamp(uAccentEmotes, 0.0, 1.0));
    alpha *= vSprite.y;
    frag = vec4(colour * alpha, alpha);
}
