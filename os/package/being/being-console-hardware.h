/* A laptop diagram for the installer's hardware report. Each mark follows
 * the corresponding observation in mind/installer.py. */
static void draw_hardware_map(const struct canvas *canvas, SDL_Rect area, unsigned int detected)
{
    const struct rgb edge = {33, 62, 77}, case_fill = {13, 24, 33}, glass = {5, 10, 15};
    const struct rgb found = {90, 240, 176}, check = {255, 180, 84};
    int width = SDL_min(area.w - 24, (area.h - 44) * 4 / 3), height = 3 * width / 4;
    if (width < 200)
        return;
    int x = area.x + (area.w - width) / 2, y = area.y + 20;
    int bezel = width * 7 / 100;
    int frame_x = x + width * 17 / 100, frame_y = y + height * 7 / 100;
    int frame_w = width * 66 / 100, frame_h = height * 53 / 100;
    outline_rounded(canvas, frame_x, frame_y, frame_w, frame_h, 13.0f, case_fill, edge);
    outline_rounded(canvas, frame_x + bezel / 2, frame_y + bezel / 2, frame_w - bezel, frame_h - bezel,
                    7.0f, glass, edge);

    const float eye_round[4] = {8.0f, 8.0f, 8.0f, 8.0f};
    int eye_w = width * 8 / 100, eye_h = height * 15 / 100;
    int eye_y = frame_y + (frame_h - eye_h) / 2;
    for (int side = -1; side <= 1; side += 2) {
        int eye_x = x + width / 2 + side * (eye_w * 3 / 4) - eye_w / 2;
        fill_rounded(canvas, eye_x - 2, eye_y - 2, eye_w + 4, eye_h + 4, eye_round, (struct rgb){7, 46, 61});
        fill_rounded(canvas, eye_x, eye_y, eye_w, eye_h, eye_round, eye_fill);
    }

    int deck = y + height * 63 / 100, deck_h = height * 28 / 100;
    for (int row = 0; row < deck_h; ++row) {
        int taper = (deck_h - row) * width / (14 * deck_h);
        fill_rect(canvas, x + taper, deck + row, width - 2 * taper, 1, edge);
        if (row > 0 && row < deck_h - 1)
            fill_rect(canvas, x + taper + 1, deck + row, width - 2 * taper - 2, 1, case_fill);
    }
    for (int row = 0; row < 4; ++row)
        for (int column = 0; column < 13; ++column)
            fill_rect(canvas, x + width * 16 / 100 + column * (width * 68 / 100 / 13),
                      deck + (row + 1) * deck_h / 9, width * 4 / 100, deck_h / 14,
                      (struct rgb){20, 35, 45});
    outline_rounded(canvas, x + width * 34 / 100, deck + deck_h * 6 / 10, width * 32 / 100,
                    deck_h * 3 / 10, 4.0f, glass, edge);

    /* Lid, memory, battery, microphone, speakers, keyboard, trackpad. */
    static const unsigned int points[][2] = {{50, 8}, {57, 72}, {22, 85}, {16, 69},
                                             {85, 69}, {43, 72}, {50, 83}};
    for (size_t i = 0; i < SDL_arraysize(points); ++i)
        awakening_dot(canvas, x + width * (int)points[i][0] / 100, y + height * (int)points[i][1] / 100,
                      detected & (1u << i) ? found : check, SDL_max(3, width / 75), 255);

    draw_text(canvas, FACE_SMALL, WEIGHT_BOLD, x, area.y, "Laptop body", wordmark_text);
    int legend_y = y + height * 93 / 100;
    draw_text(canvas, FACE_SMALL, WEIGHT_REGULAR, x, legend_y, "● Detected", found);
    draw_text(canvas, FACE_SMALL, WEIGHT_REGULAR, x + 88, legend_y, "● Check in Try", check);
}
