"""prov_view.py <out.png> <title> <cx,cy,cz> <win_mm> <label>=<layer glb>[@fill][+<other glb>[@fill]]...: close-ups like npz_cmp.py, faces coloured
by provenance: end caps magenta, closed caps cyan, body strips orange, other garment by role tint, skin tan; back faces red. '@fill' = Amount 100."""
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, 'F:/godot/blender/characters/imageto3d/experiments/shell_slots_20261122')
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
import glbread2  # noqa: E402
import visibility as vis  # noqa: E402

out, title = sys.argv[1], sys.argv[2]
pt = np.array([float(x) for x in sys.argv[3].split(',')])
win = float(sys.argv[4]) / 1000
rows = [a.split('=', 1) for a in sys.argv[5:]]
font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 17)
ROLE = {'skin': (.85, .66, .52), 'primary': (.2, .75, .25), 'secondary': (.2, .35, .9), 'tertiary': (.9, .8, .2), 'accent': (.8, .3, .8)}
PROV = {'endcap': (1., 0., 1.), 'cap': (0., .9, .9), 'seamband': (1., .55, .1)}


def load(spec):
    p, _, mo = spec.partition('@')
    V, F, mat = glbread2.read(p, morph=mo or None)
    C = np.array([PROV.get(m.split(':')[1] if ':' in m else '', ROLE.get(m.split(':')[0], (.6, .6, .6))) for m in mat])
    return V, F, C


S = 360
views = ((0, 5, 'front'), (90, 5, 'from the left'), (270, 5, 'from the right'), (180, 5, 'back'), (0, -60, 'from below'))
sheet = Image.new('RGB', (len(views) * S + 150, len(rows) * S + 30), 'white')
dr0 = ImageDraw.Draw(sheet)
dr0.text((6, 6), title, fill=(0, 0, 0), font=font)
for ri, (label, files) in enumerate(rows):
    V, F, C, off = [], [], [], 0
    for spec in files.split('+'):
        v, f, c = load(spec)
        V.append(v)
        F.append(f + off)
        C.append(c)
        off += len(v)
    V, F, C = np.concatenate(V), np.concatenate(F), np.concatenate(C)
    N = vis.face_normals(V, F)
    dr0.text((6, 30 + ri * S + S // 2), label, fill=(0, 0, 0), font=font)
    for vi, (az, el, name) in enumerate(views):
        a, e = np.radians(az), np.radians(el)
        d = -np.array([np.sin(a) * np.cos(e), -np.cos(a) * np.cos(e), np.sin(e)])
        dd, r, u = vis._frame(d)
        c2 = np.array([pt @ r, pt @ u])
        img, lo, _ = vis.zbuffer(V, F, d, win / S, lo=c2 - win / 2, hi=c2 + win / 2)
        rgb = np.full(img.shape + (3,), 248.)
        fg = img >= 0
        f = img[fg]
        nf = N[f] @ dd
        rgb[fg] = np.where((nf > 0)[:, None], np.array([[1., 0, 0]]), C[f]) * (0.4 + 0.6 * np.abs(nf))[:, None] * 255
        im = Image.fromarray(np.clip(rgb[::-1], 0, 255).astype(np.uint8))
        ImageDraw.Draw(im).text((4, 4), name, fill=(0, 0, 0), font=font)
        sheet.paste(im, (150 + vi * S, 30 + ri * S))
sheet.save(out)
print('saved', out)
