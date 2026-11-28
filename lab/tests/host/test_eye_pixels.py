import unittest

from lab.eye_pixels import palette_eyes
from lab.vm import eyes


WARM = ((215, 70, 20), (245, 112, 42), (150, 27, 9))
CYAN = ((8, 219, 255), (77, 250, 255), (0, 122, 255))


class FacePixels(unittest.TestCase):
    def setUp(self):
        self.width, self.height = 96, 72
        self.rgb = bytearray(self.width * self.height * 3)

    def paint(self, bounds, colour):
        x0, y0, x1, y1 = bounds
        for y in range(y0, y1):
            start = (y * self.width + x0) * 3
            self.rgb[start:start + (x1 - x0) * 3] = bytes(colour) * (x1 - x0)

    def frame(self):
        return self.width, self.height, self.rgb

    def test_eye_region_is_palette_aware_and_ignores_text_and_panel(self):
        self.paint((16, 12, 36, 32), WARM[0])
        self.paint((64, 12, 84, 32), WARM[0])
        self.paint((24, 16, 32, 24), WARM[1])
        self.paint((68, 16, 76, 24), WARM[1])
        self.paint((0, 0, 96, 8), (230, 230, 230))
        self.paint((0, 40, 44, 72), WARM[1])
        result = palette_eyes(self.frame(), WARM, region=(0, 0, 96, 36))
        self.assertEqual([eye.bounds for eye in result], [(16, 12, 32, 28), (64, 12, 80, 28)])
        self.assertEqual([eye.peak for eye in result], [WARM[1], WARM[1]])
        self.assertEqual(palette_eyes(self.frame(), CYAN), (None, None))

    def test_larger_dim_decoration_does_not_displace_a_narrow_eye(self):
        dim_fill = tuple(channel // 3 for channel in CYAN[0])
        self.paint((0, 32, 40, 60), dim_fill)
        self.paint((12, 12, 36, 20), CYAN[0])
        self.paint((56, 12, 84, 20), CYAN[1])
        self.paint((40, 24, 56, 32), CYAN[1])
        left, right = palette_eyes(self.frame(), CYAN)
        self.assertEqual(left.bounds, (12, 12, 32, 16))
        self.assertEqual(right.bounds, (56, 12, 80, 16))

    def test_bright_text_rejects_the_face_only_cyan_gate_as_before(self):
        self.paint((12, 12, 36, 20), CYAN[0])
        self.paint((56, 12, 84, 20), CYAN[1])
        self.paint((0, 40, 96, 56), (250, 250, 250))
        self.assertTrue(all(palette_eyes(self.frame(), CYAN)))
        self.assertLess(eyes(*self.frame())[2], 0.95)
