// One triangle that covers the screen. No vertex buffers.
uniform vec2 uRes;
uniform vec4 uE[8];
uniform float uGlowR;

flat out vec4 vShape[2]; // cos(tilt), sin(tilt), diagonal bulge, padding
flat out vec2 vBounds[2];
flat out vec4 vEllipse[2]; // inverse half-axes and ellipse evolute factors

void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
    for (int i = 0; i < 2; i++) {
        vec2 r = max(uE[i * 4].zw, vec2(1e-4));
        vec2 shape = uE[i * 4 + 1].xy;
        float cs = cos(shape.y), sn = sin(shape.y);
        float bulge = (pow(2.0, 0.5 - 1.0 / shape.x) - 1.0) * min(r.x, r.y);
        vShape[i] = vec4(cs, sn, bulge, 0.0);
        vEllipse[i] = vec4(1.0 / r, (r * r - r.yx * r.yx) / r);

        // A rotated ellipse fits inside these half extents. Lids cut away
        // from it, and diagonal bulge adds at most max(bulge, 0) to its reach.
        vec2 extent = vec2(length(r * vec2(cs, sn)), length(r * vec2(sn, cs)));
        // The bloom reaches exactly zero at uGlowR, so nothing lies beyond.
        vBounds[i] = extent + max(bulge, 0.0) + max(2.0 / uRes.y, uGlowR);
    }
}
