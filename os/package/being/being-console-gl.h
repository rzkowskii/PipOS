/* The console's single-texture presentation path for SDL OpenGL contexts. */
#include <SDL_opengl.h>

#define CONSOLE_GL_FUNCTIONS(X) \
    X(GLuint, CreateShader, (GLenum type)) \
    X(void, ShaderSource, (GLuint shader, GLsizei n, const GLchar *const *source, const GLint *length)) \
    X(void, CompileShader, (GLuint shader)) \
    X(void, GetShaderiv, (GLuint shader, GLenum option, GLint *value)) \
    X(void, GetShaderInfoLog, (GLuint shader, GLsizei capacity, GLsizei *length, GLchar *log)) \
    X(void, DeleteShader, (GLuint shader)) \
    X(GLuint, CreateProgram, (void)) \
    X(void, AttachShader, (GLuint program, GLuint shader)) \
    X(void, LinkProgram, (GLuint program)) \
    X(void, GetProgramiv, (GLuint program, GLenum option, GLint *value)) \
    X(void, GetProgramInfoLog, (GLuint program, GLsizei capacity, GLsizei *length, GLchar *log)) \
    X(void, DeleteProgram, (GLuint program)) \
    X(void, UseProgram, (GLuint program)) \
    X(void, GenVertexArrays, (GLsizei n, GLuint *arrays)) \
    X(void, BindVertexArray, (GLuint array)) \
    X(void, GenBuffers, (GLsizei n, GLuint *buffers)) \
    X(void, BindBuffer, (GLenum target, GLuint buffer)) \
    X(void, BufferData, (GLenum target, GLsizeiptr size, const void *data, GLenum usage)) \
    X(void, VertexAttribPointer, (GLuint index, GLint size, GLenum type, GLboolean normalized, GLsizei stride, const void *pointer)) \
    X(void, EnableVertexAttribArray, (GLuint index)) \
    X(GLint, GetUniformLocation, (GLuint program, const GLchar *name)) \
    X(void, Uniform1f, (GLint location, GLfloat x)) \
    X(void, Uniform1i, (GLint location, GLint x)) \
    X(void, Uniform2f, (GLint location, GLfloat x, GLfloat y)) \
    X(void, Enable, (GLenum capability)) \
    X(void, Disable, (GLenum capability)) \
    X(void, BlendFunc, (GLenum source, GLenum destination)) \
    X(void, GenTextures, (GLsizei count, GLuint *textures)) \
    X(void, BindTexture, (GLenum target, GLuint texture)) \
    X(void, TexParameteri, (GLenum target, GLenum option, GLint value)) \
    X(void, TexImage2D, (GLenum target, GLint level, GLint format, GLsizei width, GLsizei height, GLint border, GLenum external_format, GLenum type, const void *pixels)) \
    X(void, TexSubImage2D, (GLenum target, GLint level, GLint x, GLint y, GLsizei width, GLsizei height, GLenum format, GLenum type, const void *pixels)) \
    X(void, Viewport, (GLint x, GLint y, GLsizei width, GLsizei height)) \
    X(void, ClearColor, (GLfloat r, GLfloat g, GLfloat b, GLfloat a)) \
    X(void, Clear, (GLbitfield mask)) \
    X(void, DrawArrays, (GLenum mode, GLint first, GLsizei count)) \
    X(const GLubyte *, GetString, (GLenum name))

struct display {
    SDL_Window *window;
    SDL_GLContext context;
    SDL_Surface *surface;
    int texture_width, texture_height;
    int headless_width, headless_height; /* no window: draw into the surface only (previews) */
    const char *shader_version;
    bool es;
    GLuint program, geometry;
#define DECLARE_GL(result, name, args) result (APIENTRYP name) args;
    CONSOLE_GL_FUNCTIONS(DECLARE_GL)
#undef DECLARE_GL
};

static GLuint compile_shader(struct display *display, GLenum type, const char *version, const char *body)
{
    GLuint shader = display->CreateShader(type);
    const char *sources[] = { version, body };
    display->ShaderSource(shader, 2, sources, NULL);
    display->CompileShader(shader);
    GLint success = 0;
    display->GetShaderiv(shader, GL_COMPILE_STATUS, &success);
    if (success)
        return shader;
    char error[512] = {0};
    display->GetShaderInfoLog(shader, sizeof(error), NULL, error);
    SDL_LogError(SDL_LOG_CATEGORY_APPLICATION, "Console shader: %s", error);
    display->DeleteShader(shader);
    return 0;
}

static bool initialize_gl(struct display *display, bool es)
{
#define LOAD_GL(result, name, args) \
    display->name = (result (APIENTRYP) args)SDL_GL_GetProcAddress("gl" #name); \
    if (!display->name) { \
        SDL_LogError(SDL_LOG_CATEGORY_APPLICATION, "Console renderer missing gl%s", #name); \
        return false; \
    }
    CONSOLE_GL_FUNCTIONS(LOAD_GL)
#undef LOAD_GL

    const char *version = es ? "#version 300 es\nprecision highp float;\n" : "#version 330 core\n";
    const char *vertex =
        "out vec2 uv;\n"
        "void main() {\n"
        "    const vec2 points[3] = vec2[3](vec2(-1.0, -1.0), vec2(3.0, -1.0), vec2(-1.0, 3.0));\n"
        "    vec2 p = points[gl_VertexID];\n"
        "    uv = vec2(p.x + 1.0, 1.0 - p.y) * 0.5;\n"
        "    gl_Position = vec4(p, 0.0, 1.0);\n"
        "}\n";
    const char *fragment =
        "in vec2 uv;\n"
        "out vec4 color;\n"
        "uniform sampler2D screen_texture;\n"
        "void main() { color = texture(screen_texture, uv); }\n";
    GLuint vertex_shader = compile_shader(display, GL_VERTEX_SHADER, version, vertex);
    GLuint fragment_shader = compile_shader(display, GL_FRAGMENT_SHADER, version, fragment);
    if (!vertex_shader || !fragment_shader)
        return false;
    GLuint program = display->CreateProgram();
    display->AttachShader(program, vertex_shader);
    display->AttachShader(program, fragment_shader);
    display->LinkProgram(program);
    display->DeleteShader(vertex_shader);
    display->DeleteShader(fragment_shader);
    GLint success = 0;
    display->GetProgramiv(program, GL_LINK_STATUS, &success);
    if (!success) {
        char error[512] = {0};
        display->GetProgramInfoLog(program, sizeof(error), NULL, error);
        SDL_LogError(SDL_LOG_CATEGORY_APPLICATION, "Console renderer program: %s", error);
        return false;
    }

    display->es = es;
    display->shader_version = version;
    display->program = program;
    display->UseProgram(display->program);
    // No vertex data is needed, but core OpenGL still requires a bound VAO.
    GLuint texture = 0;
    display->GenVertexArrays(1, &display->geometry);
    display->BindVertexArray(display->geometry);
    display->GenTextures(1, &texture);
    display->BindTexture(GL_TEXTURE_2D, texture);
    display->TexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    display->TexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    display->TexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    display->TexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    SDL_GL_SetSwapInterval(1);
    const char *renderer = (const char *)display->GetString(GL_RENDERER);
    SDL_Log("Console renderer selected: %s (%s)", es ? "GLES 3.0" : "GL 3.3 core", renderer ? renderer : "unknown GPU");
    return true;
}

static bool open_display(struct display *display, bool kms)
{
    Uint32 window_flags = SDL_WINDOW_OPENGL | SDL_WINDOW_ALLOW_HIGHDPI;
    window_flags |= kms ? SDL_WINDOW_FULLSCREEN_DESKTOP : SDL_WINDOW_RESIZABLE;
    int width = kms ? 1280 : 960, height = kms ? 800 : 660;
    for (int attempt = 0; attempt < 2; ++attempt) {
        bool es = attempt == 1;
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, es ? SDL_GL_CONTEXT_PROFILE_ES : SDL_GL_CONTEXT_PROFILE_CORE);
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 3);
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, es ? 0 : 3);
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_FLAGS, es ? 0 : SDL_GL_CONTEXT_FORWARD_COMPATIBLE_FLAG);
        display->window = SDL_CreateWindow("PipOS", SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED, width, height,
                                           window_flags);
        if (!display->window)
            continue;
        display->context = SDL_GL_CreateContext(display->window);
        if (display->context && initialize_gl(display, es))
            return true;
        if (display->context)
            SDL_GL_DeleteContext(display->context);
        SDL_DestroyWindow(display->window);
        display->window = NULL;
        display->context = NULL;
    }
    return false;
}

static bool resize_surface(struct display *display, int width, int height)
{
    if (display->surface && display->surface->w == width && display->surface->h == height)
        return true;
    SDL_FreeSurface(display->surface);
    display->surface = SDL_CreateRGBSurfaceWithFormat(0, width, height, 32, SDL_PIXELFORMAT_RGBA32);
    return display->surface != NULL;
}

/* Uploads rows [first_row, last_row) of the surface (all of it after a resize)
 * and draws the base frame. A headless display only keeps the surface. */
static void draw_frame(struct display *display, int width, int height, bool visible, int first_row, int last_row)
{
    if (!display->window)
        return;
    display->Viewport(0, 0, width, height);
    if (visible) {
        const Uint8 *pixels = display->surface->pixels;
        if (display->texture_width != width || display->texture_height != height) {
            display->TexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, width, height, 0, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
            display->texture_width = width;
            display->texture_height = height;
        } else {
            first_row = SDL_clamp(first_row, 0, height);
            last_row = SDL_clamp(last_row, first_row, height);
            if (last_row > first_row)
                display->TexSubImage2D(GL_TEXTURE_2D, 0, 0, first_row, width, last_row - first_row, GL_RGBA,
                                       GL_UNSIGNED_BYTE, pixels + (size_t)first_row * (size_t)display->surface->pitch);
        }
        display->DrawArrays(GL_TRIANGLES, 0, 3);
    } else {
        display->ClearColor(0, 0, 0, 1);
        display->Clear(GL_COLOR_BUFFER_BIT);
    }
}
