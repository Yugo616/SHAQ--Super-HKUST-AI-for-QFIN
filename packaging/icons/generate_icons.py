"""Regenerate checked-in native icons from the desktop's flat SVG.

Run locally with Pillow installed; release builds consume the generated files
and do not import Pillow. Only the SVG's rect/polygon vocabulary is supported.
"""
from pathlib import Path
import argparse
import hashlib
import io
import json
import struct
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw

SOURCE = Path(__file__).resolve().parents[2] / 'src/shaq_daily_oracle/desktop/brand.svg'
OUTPUT = Path(__file__).resolve().parent
WINDOWS_SIZES = (16, 24, 32, 48, 64, 128, 256)
MAC_FRAMES = {'ic07': 128, 'ic08': 256, 'ic09': 512, 'ic10': 1024,
              'ic11': 32, 'ic12': 64, 'ic13': 256, 'ic14': 512}


def render(source, size):
    svg = ET.parse(source).getroot()
    left, top, width, height = map(float, svg.attrib['viewBox'].split())
    if left or top or width != height:
        raise ValueError('The icon needs a square SVG viewBox starting at zero')
    scale = size / width
    image = Image.new('RGBA', (size, size))
    draw = ImageDraw.Draw(image)
    for element in svg:
        tag = element.tag.rsplit('}', 1)[-1]
        color = element.attrib['fill']
        if tag == 'rect':
            x, y, width, height, radius = (float(element.attrib[key]) * scale
                                         for key in ('x', 'y', 'width', 'height', 'rx'))
            draw.rounded_rectangle((x, y, x + width, y + height), radius=radius, fill=color)
        elif tag == 'polygon':
            points = [tuple(float(value) * scale for value in point.split(','))
                      for point in element.attrib['points'].split()]
            draw.polygon(points, fill=color)
        else:
            raise ValueError(f'Unsupported SVG icon element: {tag}')
    return image


def png(image):
    output = io.BytesIO()
    image.save(output, format='PNG', compress_level=9)
    return output.getvalue()


def generate(source, output):
    output.mkdir(parents=True, exist_ok=True)
    largest = max(MAC_FRAMES.values())
    master = render(source, largest * 4)
    sizes = sorted(set(WINDOWS_SIZES) | set(MAC_FRAMES.values()))
    images = {size: master.resize((size, size), Image.Resampling.LANCZOS) for size in sizes}
    frames = {size: png(image) for size, image in images.items()}
    (output / 'shaq.png').write_bytes(frames[largest])

    # PNG-compressed, 32-bit frames retain alpha and include Windows' small sizes.
    offset = 6 + 16 * len(WINDOWS_SIZES)
    directory = []
    for size in WINDOWS_SIZES:
        directory.append(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0,
                                     1, 32, len(frames[size]), offset))
        offset += len(frames[size])
    ico = struct.pack('<HHH', 0, 1, len(directory)) + b''.join(directory)
    (output / 'shaq.ico').write_bytes(ico + b''.join(frames[size] for size in WINDOWS_SIZES))

    entries = [(kind.encode('ascii'), frames[size]) for kind, size in MAC_FRAMES.items()]
    # Native IconServices can misread small icp* PNG chunks. Its classic 1x
    # entries use planar RGB with PackBits-style literal runs and an alpha mask.
    for size, rgb_type, mask_type in ((16, b'is32', b's8mk'), (32, b'il32', b'l8mk'),
                                      (48, b'ih32', b'h8mk')):
        bands = images[size].split()
        rgb = bytearray()
        for band in bands[:3]:
            raw = band.tobytes()
            for offset in range(0, len(raw), 128):
                block = raw[offset:offset + 128]
                rgb.append(len(block) - 1)
                rgb.extend(block)
        entries.extend(((rgb_type, bytes(rgb)), (mask_type, bands[3].tobytes())))
    chunks = b''.join(struct.pack('>4sI', kind, len(content) + 8) + content
                      for kind, content in entries)
    (output / 'shaq.icns').write_bytes(struct.pack('>4sI', b'icns', len(chunks) + 8) + chunks)
    manifest = {'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'assets': {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                           for name in ('shaq.png', 'shaq.ico', 'shaq.icns')}}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')

    # Review-only contact sheet; not included in release payloads.
    preview = Image.new('RGB', (800, 480), '#F4F4F3')
    draw = ImageDraw.Draw(preview)
    draw.rectangle((400, 0, 799, 479), fill='#17181B')
    for x in (72, 472):
        preview.paste(images[256], (x, 32), images[256])
    for origin, color in ((24, '#24262B'), (424, '#FFFFFF')):
        x = origin
        for size in (16, 24, 32, 48, 64):
            preview.paste(images[size], (x, 340), images[size])
            draw.text((x, 416), str(size) + 'px', fill=color)
            x += 76
    preview.save(output / 'preview.png')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SOURCE)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    options = parser.parse_args()
    generate(options.source, options.output)
