"""Native icon assets and the build routes that put them on the desktop."""
from pathlib import Path
import hashlib
import importlib.util
import json
import plistlib
import struct
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / 'packaging/icons'


def load_builder():
    spec = importlib.util.spec_from_file_location('icon_build', ROOT / 'packaging/build_desktop.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png_size(data):
    if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        raise AssertionError('Icon frame is not a PNG image')
    return struct.unpack('>II', data[16:24])


class DesktopIconTests(unittest.TestCase):
    def test_vector_can_render_without_fonts_external_assets_or_effects(self):
        svg = ET.parse(ROOT / 'src/shaq_daily_oracle/desktop/brand.svg').getroot()
        left, top, width, height = map(float, svg.attrib['viewBox'].split())
        self.assertEqual((left, top), (0, 0))
        self.assertEqual(width, height)
        self.assertGreater(width, 0)
        elements = list(svg)
        self.assertTrue(elements)
        # Export supports standalone, filled geometry. A browser-only effect or
        # font would silently diverge from the Finder/Explorer icon otherwise.
        attributes = {'rect': {'x', 'y', 'width', 'height', 'rx', 'fill'},
                      'polygon': {'points', 'fill'}}
        for node in elements:
            tag = node.tag.rsplit('}', 1)[-1]
            with self.subTest(element=tag):
                self.assertIn(tag, attributes)
                self.assertEqual(set(node.attrib), attributes[tag])
                self.assertFalse(list(node))
                self.assertRegex(node.attrib['fill'], r'^#[0-9A-Fa-f]{6}$')

    def test_generated_assets_are_current_and_have_native_sizes(self):
        manifest = json.loads((ICONS / 'manifest.json').read_text())
        source = ROOT / 'src/shaq_daily_oracle/desktop/brand.svg'
        self.assertEqual(manifest['source_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        for name in ('shaq.png', 'shaq.ico', 'shaq.icns'):
            self.assertEqual(manifest['assets'][name], hashlib.sha256((ICONS / name).read_bytes()).hexdigest())
        self.assertEqual(png_size((ICONS / 'shaq.png').read_bytes()), (1024, 1024))

        ico = (ICONS / 'shaq.ico').read_bytes()
        reserved, kind, count = struct.unpack('<HHH', ico[:6])
        self.assertEqual((reserved, kind), (0, 1))
        frames = {}
        for index in range(count):
            width, height, colors, reserved, planes, bits, size, offset = struct.unpack_from('<BBBBHHII', ico, 6 + 16 * index)
            dimensions = (width or 256, height or 256)
            self.assertEqual(bits, 32)
            frames[dimensions] = ico[offset:offset + size]
            self.assertEqual(png_size(frames[dimensions]), dimensions)
        self.assertEqual(set(frames), {(size, size) for size in (16, 24, 32, 48, 64, 128, 256)})

        icns = (ICONS / 'shaq.icns').read_bytes()
        magic, length = struct.unpack('>4sI', icns[:8])
        self.assertEqual((magic, length), (b'icns', len(icns)))
        offset = 8
        mac_frames = {}
        while offset < len(icns):
            kind, length = struct.unpack_from('>4sI', icns, offset)
            self.assertGreater(length, 8)
            mac_frames[kind] = icns[offset + 8:offset + length]
            offset += length
        self.assertEqual(offset, len(icns))
        for kind, size in {b'ic07': 128, b'ic08': 256, b'ic09': 512, b'ic10': 1024,
                           b'ic11': 32, b'ic12': 64, b'ic13': 256, b'ic14': 512}.items():
            self.assertEqual(png_size(mac_frames[kind]), (size, size))
        # Small PNG icp* entries can be misread by native IconServices. Keep
        # legacy RGB planes and explicit alpha masks for the 1x small sizes.
        for size, rgb_type, mask_type in ((16, b'is32', b's8mk'), (32, b'il32', b'l8mk'),
                                          (48, b'ih32', b'h8mk')):
            mask = mac_frames[mask_type]
            self.assertEqual(len(mask), size * size)
            self.assertEqual(mask[0], 0)
            self.assertEqual(max(mask), 255)
            data = mac_frames[rgb_type]
            offset = 0
            for channel in range(3):
                pixels = 0
                while pixels < size * size:
                    control = data[offset]
                    offset += 1
                    count = control - 125 if control >= 128 else control + 1
                    length = 1 if control >= 128 else count
                    self.assertEqual(len(data[offset:offset + length]), length)
                    offset += length
                    pixels += count
                self.assertEqual(pixels, size * size)
            self.assertEqual(offset, len(data))
        self.assertFalse({b'icp4', b'icp5', b'icp6'} & set(mac_frames))
        self.assertEqual(mac_frames[b'ic08'], frames[(256, 256)])
        self.assertEqual(mac_frames[b'ic10'], (ICONS / 'shaq.png').read_bytes())

    def test_pyinstaller_embeds_platform_icon_and_runtime_assets(self):
        build = load_builder()
        for platform, suffix in [('darwin', 'icns'), ('win32', 'ico')]:
            with self.subTest(platform=platform), patch.object(build.sys, 'platform', platform):
                args = build.pyinstaller_args(ROOT, Path('output'), 'Example')
                self.assertIn('--icon', args)
                self.assertEqual(Path(args[args.index('--icon') + 1]), ICONS / f'shaq.{suffix}')
                for runtime_suffix in ('png', 'ico', 'icns'):
                    self.assertIn(f'{ICONS / ("shaq." + runtime_suffix)}:packaging/icons', args)

    def test_macos_metadata_points_to_real_bundle_icon(self):
        build = load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / 'Example.app'
            (app / 'Contents').mkdir(parents=True)
            metadata = app / 'Contents/Info.plist'
            metadata.write_bytes(plistlib.dumps({'CFBundleIconFile': 'Python.icns'}))
            build.set_macos_bundle_identity(ROOT, app, '1.2.3')
            icon = plistlib.loads(metadata.read_bytes())['CFBundleIconFile']
            self.assertEqual(icon, 'shaq.icns')
            self.assertEqual((app / 'Contents/Resources' / icon).read_bytes(), (ICONS / icon).read_bytes())

    def test_velopack_installer_uses_same_icon_as_application(self):
        build = load_builder()
        for system, machine, suffix in [('Windows', 'AMD64', 'ico'), ('Darwin', 'arm64', 'icns'),
                                        ('Darwin', 'x86_64', 'icns')]:
            with self.subTest(system=system, machine=machine):
                args = build.managed_pack_args(ROOT, Path('Example.app' if system == 'Darwin' else 'Example'),
                                               Path('output'), '1.2.3', system, machine)
                self.assertIn('--icon', args)
                self.assertEqual(Path(args[args.index('--icon') + 1]), ICONS / f'shaq.{suffix}')

    def test_missing_native_icon_stops_before_default_python_icon_can_ship(self):
        build = load_builder()
        with tempfile.TemporaryDirectory() as temporary, patch.object(build.sys, 'platform', 'win32'):
            with self.assertRaisesRegex(FileNotFoundError, 'shaq.ico'):
                build.pyinstaller_args(Path(temporary), Path('output'), 'Example')
