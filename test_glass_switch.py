import time
import unittest

import glass_switch
import ui_motion

W, H, PAD = 88, 48, 10            # a 44 x 24 switch on a 2x screen


def centre(at, press=0.0):
    x0, y0, x1, y1 = glass_switch.thumb_box(W, H, PAD, at, press)
    return round((x0 + x1) / 2), round((y0 + y1) / 2)


class GlassSwitchTests(unittest.TestCase):
    def test_size_and_ground(self):
        image = glass_switch.render(W, H, 0, ground="#2a2925", pad=PAD)
        self.assertEqual((image.size, image.mode), ((W + 2 * PAD, H + 2 * PAD), "RGB"))
        self.assertEqual(image.getpixel((1, 1)), (42, 41, 37))

    def test_the_track_turns_green_when_on(self):
        off = glass_switch.render(W, H, 0, pad=PAD).getpixel((PAD + W - 14, PAD + H // 2))
        on = glass_switch.render(W, H, 1, pad=PAD).getpixel((PAD + 14, PAD + H // 2))
        self.assertGreater(on[1] - on[0], 50)                     # green
        self.assertLess(abs(off[1] - off[0]), 10)                 # grey

    def test_the_resting_thumb_is_white_and_the_held_one_is_glass(self):
        resting = glass_switch.render(W, H, 1, pad=PAD).getpixel(centre(1))
        held = glass_switch.render(W, H, 1, press=1, pad=PAD).getpixel(centre(1, 1))
        self.assertGreater(min(resting), 225)
        self.assertGreater(held[1] - held[0], 25)                 # the green track shows through

    def test_the_held_thumb_swells_past_the_track(self):
        x0, y0, x1, y1 = glass_switch.thumb_box(W, H, PAD, 0, press=1)
        self.assertLess(y0, PAD)
        self.assertGreater(y1, PAD + H)
        rest = glass_switch.thumb_box(W, H, PAD, 0)
        self.assertGreater(x1 - x0, rest[2] - rest[0])

    def test_no_dot_or_seam_in_the_middle_of_the_thumb(self):
        for at in (0, 1):
            for press in (0, 1):
                image = glass_switch.render(W, H, at, press=press, pad=PAD)
                cx, cy = centre(at, press)
                patch = [image.getpixel((cx + dx, cy + dy)) for dx in range(-4, 5) for dy in range(-4, 5)]
                spread = max(max(p[c] for p in patch) - min(p[c] for p in patch) for c in range(3))
                self.assertLessEqual(spread, 8, (at, press))
                column = [image.getpixel((cx, y))[1] for y in range(cy - 8, cy + 9)]
                self.assertLessEqual(max(abs(a - b) for a, b in zip(column, column[1:])), 3, (at, press))

    def test_a_frame_is_cheap_enough_to_animate(self):
        started = time.perf_counter()
        for step in range(10):
            glass_switch.render(W, H, step / 9, press=1, stretch=0.2, pad=PAD)
        self.assertLess((time.perf_counter() - started) / 10, 0.05)

    def test_the_spring_overshoots_and_settles(self):
        values = [ui_motion.ease_out_back(step / 20) for step in range(21)]
        self.assertEqual((round(values[0], 6), round(values[-1], 6)), (0, 1))
        self.assertGreater(max(values), 1.0)


if __name__ == "__main__":
    unittest.main()
