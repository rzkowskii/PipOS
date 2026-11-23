# Pico face detector

The vendored `picornt.c`, `picornt.h` and trained `facefinder` cascade are
unmodified files from commit `7d550c78b2c31a4e1dfc5bcdfe9da013297b5cc8`.
The included MIT licence is also from that commit.
[Source](https://github.com/nenadmarkus/pico/tree/7d550c78b2c31a4e1dfc5bcdfe9da013297b5cc8).

| Local file | Upstream path | SHA-256 |
| --- | --- | --- |
| `picornt.c` | `rnt/picornt.c` | `a3cd46d076304feb0acc38fc9e5ec78ca14c7d69b5ff2d1c8d05b26c9e25b72d` |
| `picornt.h` | `rnt/picornt.h` | `4a9e30da0876cb49d33495b1c7827857b131446a6308ccca548175bccd5f8ded` |
| `facefinder` | `rnt/cascades/facefinder` | `d8014993e7298c7b1865d1f8b855d6dbf4ec5c808bf879e2091ab6837abf90cd` |
| `PICO-LICENSE` | `LICENSE` | `3769f26981094bcd4e45134dc3101f9e2fbde28c2bfe15534557b4c089f2f521` |

The detector is described by N. Markus, M. Frljak, I. S. Pandzic,
J. Ahlberg and R. Forchheimer in *Object Detection with Pixel Intensity
Comparisons Organized in Decision Trees*. [Paper](https://arxiv.org/abs/1305.4537).
