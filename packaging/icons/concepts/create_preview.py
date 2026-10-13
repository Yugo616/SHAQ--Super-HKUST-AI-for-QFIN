"""Render the two review-only SVG concepts without changing shipping assets."""
from pathlib import Path
import importlib.util

from PIL import Image, ImageDraw, ImageFont

DIRECTORY = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('icon_renderer', DIRECTORY.parent / 'generate_icons.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def main():
    preview = Image.new('RGB', (1200, 830), '#F4F4F3')
    draw = ImageDraw.Draw(preview)
    heading = ImageFont.load_default(size=30)
    label = ImageFont.load_default(size=17)
    small = ImageFont.load_default(size=13)
    draw.line((600, 28, 600, 800), fill='#D8DADC', width=1)
    for origin, name, title, detail in (
            (0, 's-price', 'A  /  S price signal', 'The price path forms the S; two analysis nodes.'),
            (600, 'q-signal', 'B  /  Q quant signal', 'A Q with an integrated market signal and two nodes.')):
        master = renderer.render(DIRECTORY / (name + '.svg'), 2048)
        image = master.resize((1024, 1024), Image.Resampling.LANCZOS)
        image.save(DIRECTORY / (name + '.png'))
        draw.text((origin + 56, 36), title, fill='#24262B', font=heading)
        main_icon = master.resize((320, 320), Image.Resampling.LANCZOS)
        preview.paste(main_icon, (origin + 140, 105), main_icon)
        draw.text((origin + 56, 465), detail, fill='#60646C', font=label)
        draw.text((origin + 56, 510), 'ACTUAL PIXEL SIZES', fill='#60646C', font=small)
        draw.rounded_rectangle((origin + 36, 650, origin + 564, 792), radius=18, fill='#17181B')
        for index, size in enumerate((16, 24, 32, 48, 64)):
            icon = master.resize((size, size), Image.Resampling.LANCZOS)
            x = origin + 68 + index * 96
            for y, color in ((550, '#60646C'), (692, '#C9CDCF')):
                preview.paste(icon, (x, y), icon)
                draw.text((x, y + 74), str(size) + 'px', fill=color, font=small)
    preview.save(DIRECTORY / 'comparison.png')


if __name__ == '__main__':
    main()
