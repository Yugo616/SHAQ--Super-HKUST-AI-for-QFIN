import unittest

from native_geometry import resize_css_width


class NativeGeometryTests(unittest.TestCase):
    def exercise(self, dpr, frame, initial, target, clamp=None):
        class Window:
            native_width = initial * dpr + frame
            native_height = 800
            def resize(self, width, height):
                self.native_width = min(width, clamp) if clamp else width
                self.native_height = height
        window = Window()
        def capture(_window):
            return {'inner_width': (window.native_width-frame)/dpr, 'inner_height': 740,
                    'dpr': dpr, 'resize_scale': dpr, 'native_width': window.native_width,
                    'native_height': window.native_height, 'work_area': {'width': clamp or 2560}}
        snapshots = []
        self.snapshots = snapshots
        result = resize_css_width(window, target, snapshots, capture=capture, pause=lambda _: None)
        return result, snapshots

    def test_actual_css_targets_with_dpi_and_native_frame(self):
        for dpr, frame in ((1, 16), (1.25, 20), (1.5, 24), (2, 32)):
            for target in (1320, 980):
                with self.subTest(dpr=dpr, target=target):
                    result, snapshots = self.exercise(dpr, frame, 1100, target)
                    self.assertAlmostEqual(result['inner_width'], target)
                    self.assertEqual(snapshots[0]['geometry']['inner_width'], 1100)
                    self.assertEqual(snapshots[0]['target_css_width'], target)

    def test_display_clamp_fails_bounded_with_actual_geometry(self):
        with self.assertRaisesRegex(AssertionError, '1320.*1008.*work_area'):
            self.exercise(1, 16, 980, 1320, clamp=1024)
        self.assertEqual(len(self.snapshots), 7, 'clamped host must fail after bounded attempts')
        self.assertEqual(self.snapshots[0]['geometry']['inner_width'], 980)

    def test_macos_point_coordinates_do_not_double_apply_retina_dpr(self):
        class Window:
            width = 1320
            def resize(self, width, height): self.width = width
        window = Window()
        snapshots = []
        result = resize_css_width(window, 980, snapshots, capture=lambda w: {
            'inner_width': w.width, 'inner_height': 760, 'native_width': w.width,
            'native_height': 760, 'dpr': 2, 'resize_scale': 1}, pause=lambda _: None)
        self.assertEqual(result['inner_width'], 980)
