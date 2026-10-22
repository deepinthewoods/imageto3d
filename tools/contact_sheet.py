#!/usr/bin/env python3
"""Labelled contact sheet of concept images (flat grey backdrop, transparent PNGs composited).

Usage: tools/contact_sheet.py <out.jpg> --title "Title" [--cols N] [--tile PX] [--crop x0,y0,x1,y1] <img> [<img> ...]
Labels are the file names without extension (leading 'NN_' kept). Only for viewing: never feed a sheet back
into the pipeline.
"""
import argparse, os
from PIL import Image, ImageDraw, ImageFont

def font(size):
    for p in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/TTF/DejaVuSans.ttf'):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

ap = argparse.ArgumentParser()
ap.add_argument('out'); ap.add_argument('images', nargs='+')
ap.add_argument('--title', default=''); ap.add_argument('--cols', type=int, default=5)
ap.add_argument('--tile', type=int, default=420); ap.add_argument('--crop', default='')
a = ap.parse_args()
crop = tuple(int(v) for v in a.crop.split(',')) if a.crop else None
lab_h, head_h, t = 34, (70 if a.title else 0), a.tile
rows = (len(a.images) + a.cols - 1) // a.cols
sheet = Image.new('RGB', (a.cols * t, head_h + rows * (t + lab_h)), (118, 118, 122))
d = ImageDraw.Draw(sheet)
if a.title:
    d.text((20, 18), a.title, fill=(250, 250, 250), font=font(34))
for i, p in enumerate(a.images):
    im = Image.open(p).convert('RGBA')
    if crop: im = im.crop(crop)
    im.thumbnail((t, t), Image.LANCZOS)
    bg = Image.new('RGBA', (t, t), (118, 118, 122, 255)); bg.alpha_composite(im, ((t - im.width) // 2, (t - im.height) // 2))
    x, y = (i % a.cols) * t, head_h + (i // a.cols) * (t + lab_h)
    sheet.paste(bg.convert('RGB'), (x, y))
    name = os.path.splitext(os.path.basename(p))[0]
    if name in ('outfit_front', 'front', 'body_armor') or name.endswith('_front'):
        name = os.path.basename(os.path.dirname(os.path.dirname(p))) if 'human_male' in p else name
    d.text((x + 8, y + t + 6), name.replace('__body_armor', '')[:44], fill=(245, 245, 245), font=font(18))
sheet.save(a.out, quality=88)
print(a.out, sheet.size)
