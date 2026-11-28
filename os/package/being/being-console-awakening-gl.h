/* The installer draws the moving graph over the console's texture. Only the
 * measured install estimate lights new nodes. Rotation is illustrative. */
#include <stddef.h>

struct awakening_gl {
    bool attempted;
    GLuint program, array;
    GLint heading, progress, scale, points;
};

struct awakening_vertex {
    float position[3], color[3], threshold[3];
};

enum { AWAKENING_LINE_VERTICES = 2 * AWAKENING_NEIGHBORS * AWAKENING_NODES };

static struct awakening_vertex awakening_vertex(const struct awakening_node *node)
{
    const struct awakening_region *region = &awakening_regions[node->region];
    return (struct awakening_vertex){
        { node->x, node->y, node->z },
        { region->color.r / 255.0f, region->color.g / 255.0f, region->color.b / 255.0f },
        { node->rank, (float)region->begin, (float)region->end },
    };
}

static void prepare_awakening_gl(struct display *display, struct awakening_gl *graph)
{
    if (!display->window || graph->attempted)
        return;
    graph->attempted = true;
    const char *vertex =
        "layout(location=0) in vec3 position;\n"
        "layout(location=1) in vec3 color;\n"
        "layout(location=2) in vec3 threshold;\n"
        "uniform float heading, progress;\n"
        "uniform vec2 scale;\n"
        "out vec3 tint;\n"
        "out float energy;\n"
        "void main() {\n"
        "    float x = position.x*cos(heading) - position.z*sin(heading);\n"
        "    float z = position.x*sin(heading) + position.z*cos(heading);\n"
        "    float y = position.y*0.93 - z*0.37;\n"
        "    gl_Position = vec4(vec2(x, -y)*scale*(3.3/(3.3+z)), 0.0, 1.0);\n"
        "    float stage = clamp((progress-threshold.y)/(threshold.z-threshold.y), 0.0, 1.0);\n"
        "    energy = progress >= 100.0 ? 1.0 : 0.10 + 0.90*clamp((stage-threshold.x)*8.0, 0.0, 1.0);\n"
        "    tint = color;\n"
        "    gl_PointSize = mix(5.0, 10.0, energy);\n"
        "}\n";
    const char *fragment =
        "uniform bool points;\n"
        "in vec3 tint;\n"
        "in float energy;\n"
        "out vec4 color;\n"
        "void main() {\n"
        "    if (points) {\n"
        "        float radius = length(gl_PointCoord-vec2(0.5))*2.0;\n"
        "        float halo = 1.0 - smoothstep(0.06, 1.0, radius);\n"
        "        float core = 1.0 - smoothstep(0.20, 0.59, radius);\n"
        "        color = vec4(tint, energy*(0.22*halo + 0.71*core));\n"
        "    } else {\n"
        "        color = vec4(tint, energy*0.25);\n"
        "    }\n"
        "}\n";
    GLuint vs = compile_shader(display, GL_VERTEX_SHADER, display->shader_version, vertex);
    GLuint fs = compile_shader(display, GL_FRAGMENT_SHADER, display->shader_version, fragment);
    GLuint program = 0;
    GLint linked = 0;
    if (vs && fs) {
        program = display->CreateProgram();
        display->AttachShader(program, vs);
        display->AttachShader(program, fs);
        display->LinkProgram(program);
        display->GetProgramiv(program, GL_LINK_STATUS, &linked);
    }
    if (vs)
        display->DeleteShader(vs);
    if (fs)
        display->DeleteShader(fs);
    if (!linked) {
        if (program) {
            char log[512] = {0};
            display->GetProgramInfoLog(program, sizeof(log), NULL, log);
            SDL_LogError(SDL_LOG_CATEGORY_APPLICATION, "Awakening illustration: %s", log);
            display->DeleteProgram(program);
        }
        return;
    }

    size_t vertex_count = AWAKENING_LINE_VERTICES + AWAKENING_NODES;
    struct awakening_vertex *vertices = SDL_malloc(vertex_count * sizeof(*vertices));
    if (!vertices) {
        display->DeleteProgram(program);
        return;
    }
    const struct awakening_node *nodes = awakening_graph();
    int end = 0;
    for (int i = 0; i < AWAKENING_NODES; ++i) {
        struct awakening_vertex vertex = awakening_vertex(&nodes[i]);
        vertices[AWAKENING_LINE_VERTICES + i] = vertex;
        for (int j = 0; j < AWAKENING_NEIGHBORS; ++j) {
            vertices[end++] = vertex;
            vertices[end++] = awakening_vertex(&nodes[nodes[i].neighbor[j]]);
        }
    }
    GLuint buffer = 0;
    display->GenVertexArrays(1, &graph->array);
    display->BindVertexArray(graph->array);
    display->GenBuffers(1, &buffer);
    display->BindBuffer(GL_ARRAY_BUFFER, buffer);
    display->BufferData(GL_ARRAY_BUFFER, (GLsizeiptr)(vertex_count * sizeof(*vertices)), vertices, GL_STATIC_DRAW);
    SDL_free(vertices);
    display->VertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, sizeof(struct awakening_vertex),
                                 (const void *)offsetof(struct awakening_vertex, position));
    display->VertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, sizeof(struct awakening_vertex),
                                 (const void *)offsetof(struct awakening_vertex, color));
    display->VertexAttribPointer(2, 3, GL_FLOAT, GL_FALSE, sizeof(struct awakening_vertex),
                                 (const void *)offsetof(struct awakening_vertex, threshold));
    for (GLuint index = 0; index < 3; ++index)
        display->EnableVertexAttribArray(index);
    display->BindBuffer(GL_ARRAY_BUFFER, 0);
    display->BindVertexArray(display->geometry);
    graph->heading = display->GetUniformLocation(program, "heading");
    graph->progress = display->GetUniformLocation(program, "progress");
    graph->scale = display->GetUniformLocation(program, "scale");
    graph->points = display->GetUniformLocation(program, "points");
    graph->program = program;
}

static void draw_live_awakening(struct display *display, const struct awakening_gl *graph, SDL_Rect area,
                                int screen_height, int percent, float elapsed)
{
    int width = area.w - 24, height = area.h - 64;
    display->Viewport(area.x + 12, screen_height - (area.y + 34 + height), width, height);
    float scale = SDL_min(width * 0.29f, height * 0.44f);
    float rotation = percent < 100 ? 0.28f * SDL_sinf(elapsed * 0.72f) : 0.0f;
    display->UseProgram(graph->program);
    display->BindVertexArray(graph->array);
    display->Uniform1f(graph->heading, 0.22f + 0.0064f * percent + rotation);
    display->Uniform1f(graph->progress, (float)percent);
    display->Uniform2f(graph->scale, 2.0f * scale / width, 2.0f * scale / height);
    display->Enable(GL_BLEND);
    display->BlendFunc(GL_SRC_ALPHA, GL_ONE);
    display->Uniform1i(graph->points, 0);
    display->DrawArrays(GL_LINES, 0, AWAKENING_LINE_VERTICES);
    if (!display->es)
        display->Enable(GL_PROGRAM_POINT_SIZE);
    display->Uniform1i(graph->points, 1);
    display->DrawArrays(GL_POINTS, AWAKENING_LINE_VERTICES, AWAKENING_NODES);
    if (!display->es)
        display->Disable(GL_PROGRAM_POINT_SIZE);
    display->Disable(GL_BLEND);
    display->BindVertexArray(display->geometry);
    display->UseProgram(display->program);
}
