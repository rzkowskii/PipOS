/* The Shell Lab's illustrative network, shared by the live installation
 * renderer and the still drawn for Mind or headless previews. */

enum { AWAKENING_NODES = 440, AWAKENING_NEIGHBORS = 3 };

static const struct awakening_region {
    const char *label;
    struct rgb color;
    int begin, end;
} awakening_regions[] = {
    {"Body", {159, 216, 255}, 0, 18},
    {"Senses", {90, 240, 176}, 18, 55},
    {"Language", {176, 146, 255}, 22, 62},
    {"Memory", {255, 207, 122}, 40, 62},
    {"Self", {226, 244, 255}, 97, 100},
};

struct awakening_node {
    float x, y, z, rank;
    int region;
    int neighbor[AWAKENING_NEIGHBORS];
};

static float awakening_random(uint32_t *seed)
{
    *seed = *seed * 1664525u + 1013904223u;
    return (float)(*seed >> 8) / 16777216.0f;
}

static const struct awakening_node *awakening_graph(void)
{
    static struct awakening_node nodes[AWAKENING_NODES];
    static bool ready;
    if (ready)
        return nodes;
    uint32_t seed = 0x6265696eu;
    for (int i = 0; i < AWAKENING_NODES; ++i) {
        struct awakening_node *node = &nodes[i];
        float x, y, z, radius;
        do {
            x = 2.0f * awakening_random(&seed) - 1.0f;
            y = 2.0f * awakening_random(&seed) - 1.0f;
            z = 2.0f * awakening_random(&seed) - 1.0f;
            radius = x * x + y * y + z * z;
        } while (radius > 1.0f);
        node->region = radius < 0.28f ? 3 : y > 0.42f ? 0 : z < -0.28f ? 1 : x < 0.0f ? 2 : 4;
        node->x = 1.28f * x + (x < 0.0f ? -0.06f : 0.06f);
        node->y = y > 0.55f ? 0.55f + (y - 0.55f) * 0.45f : y;
        node->z = z;
        node->rank = awakening_random(&seed);
    }
    for (int i = 0; i < AWAKENING_NODES; ++i) {
        struct awakening_node *node = &nodes[i];
        float closest[AWAKENING_NEIGHBORS] = {100.0f, 100.0f, 100.0f};
        for (int j = 0; j < AWAKENING_NODES; ++j) {
            if (i == j)
                continue;
            const struct awakening_node *other = &nodes[j];
            float dx = node->x - other->x, dy = node->y - other->y, dz = node->z - other->z;
            float distance = dx * dx + dy * dy + dz * dz;
            for (int k = 0; k < AWAKENING_NEIGHBORS; ++k) {
                if (distance >= closest[k])
                    continue;
                for (int next = AWAKENING_NEIGHBORS - 1; next > k; --next) {
                    closest[next] = closest[next - 1];
                    node->neighbor[next] = node->neighbor[next - 1];
                }
                closest[k] = distance;
                node->neighbor[k] = j;
                break;
            }
        }
    }
    ready = true;
    return nodes;
}

static void awakening_pixel(const struct canvas *canvas, int x, int y, struct rgb color, int alpha)
{
    if (0 <= x && x < canvas->width && 0 <= y && y < canvas->height)
        blend(pixel_at(canvas, x, y), color, alpha);
}

static void awakening_line(const struct canvas *canvas, int x, int y, int end_x, int end_y,
                           struct rgb color, int alpha)
{
    int dx = abs(end_x - x), dy = -abs(end_y - y);
    int step_x = end_x > x ? 1 : -1, step_y = end_y > y ? 1 : -1;
    int error = dx + dy;
    for (;;) {
        awakening_pixel(canvas, x, y, color, alpha);
        if (x == end_x && y == end_y)
            return;
        int twice = 2 * error;
        if (twice >= dy) { error += dy; x += step_x; }
        if (twice <= dx) { error += dx; y += step_y; }
    }
}

static void awakening_dot(const struct canvas *canvas, int x, int y, struct rgb color, int radius, int alpha)
{
    int glow = radius * 2;
    for (int dy = -glow; dy <= glow; ++dy)
        for (int dx = -glow; dx <= glow; ++dx) {
            float distance = SDL_sqrtf((float)(dx * dx + dy * dy));
            float halo = SDL_clamp(1.0f - distance / (float)glow, 0.0f, 1.0f) * 0.24f;
            float core = SDL_clamp((float)radius + 0.5f - distance, 0.0f, 1.0f);
            awakening_pixel(canvas, x + dx, y + dy, color, (int)((halo + core * 0.76f) * alpha));
        }
}

static void draw_awakening_card(const struct canvas *canvas, int left, int top, int width, int height, int percent)
{
    const float edge[4] = {12.0f, 12.0f, 12.0f, 12.0f};
    const float inner[4] = {11.0f, 11.0f, 11.0f, 11.0f};
    fill_rounded(canvas, left, top, width, height, edge, (struct rgb){27, 45, 57});
    fill_rounded(canvas, left + 1, top + 1, width - 2, height - 2, inner, (struct rgb){5, 11, 17});

    int face = width >= 380 && height >= 250 ? FACE_MEDIUM : FACE_SMALL;
    draw_text(canvas, face, WEIGHT_REGULAR, left + 12, top + 8, "Awakening illustration", hint_text);
    if (width >= 365) {
        int gap = (width - 24) / (int)SDL_arraysize(awakening_regions);
        int y = top + height - 27;
        for (size_t i = 0; i < SDL_arraysize(awakening_regions); ++i) {
            const struct awakening_region *region = &awakening_regions[i];
            float activation = SDL_clamp((float)(percent - region->begin) / (float)(region->end - region->begin),
                                         0.0f, 1.0f);
            int x = left + 12 + (int)i * gap;
            draw_text(canvas, FACE_SMALL, WEIGHT_REGULAR, x, y, region->label, region->color);
            fill_rect(canvas, x, y + 16, gap - 10, 2, footer_rule);
            fill_rect(canvas, x, y + 16, (int)((gap - 10) * activation), 2, region->color);
        }
    }
}

static void draw_awakening(const struct canvas *canvas, int left, int top, int width, int height, int percent)
{
    if (width < 184 || height < 116)
        return;
    draw_awakening_card(canvas, left, top, width, height, percent);

    const struct awakening_node *nodes = awakening_graph();
    struct projected_node { int x, y; float active; } projected[AWAKENING_NODES];
    float activation[SDL_arraysize(awakening_regions)];
    for (size_t i = 0; i < SDL_arraysize(awakening_regions); ++i) {
        const struct awakening_region *region = &awakening_regions[i];
        activation[i] = SDL_clamp((float)(percent - region->begin) / (float)(region->end - region->begin),
                                  0.0f, 1.0f);
    }
    float heading = 0.22f + 0.64f * (float)percent / 100.0f;
    float sin_heading = SDL_sinf(heading), cos_heading = SDL_cosf(heading);
    float scale = SDL_min(width * 0.29f, (height - 52) * 0.42f);
    int center_x = left + width / 2, center_y = top + height / 2 + 3;
    for (int i = 0; i < AWAKENING_NODES; ++i) {
        const struct awakening_node *node = &nodes[i];
        float x = node->x * cos_heading - node->z * sin_heading;
        float depth = node->x * sin_heading + node->z * cos_heading;
        float y = node->y * 0.93f - depth * 0.37f;
        float perspective = 3.3f / (3.3f + depth);
        projected[i] = (struct projected_node){
            center_x + (int)(x * scale * perspective), center_y + (int)(y * scale * perspective),
            0.10f + 0.90f * SDL_clamp((activation[node->region] - node->rank) * 8.0f, 0.0f, 1.0f),
        };
        if (percent == 100)
            projected[i].active = 1.0f;
    }
    for (int i = 0; i < AWAKENING_NODES; ++i) {
        const struct awakening_node *node = &nodes[i];
        for (int k = 0; k < AWAKENING_NEIGHBORS; ++k) {
            int other = node->neighbor[k];
            float activity = SDL_min(projected[i].active, projected[other].active);
            awakening_line(canvas, projected[i].x, projected[i].y, projected[other].x, projected[other].y,
                           awakening_regions[node->region].color, (int)(activity * 92.0f));
        }
    }
    for (int i = 0; i < AWAKENING_NODES; ++i) {
        int radius = 1 + (projected[i].active > 0.5f && width >= 300);
        awakening_dot(canvas, projected[i].x, projected[i].y, awakening_regions[nodes[i].region].color,
                      radius, (int)(projected[i].active * 218.0f));
    }
}
