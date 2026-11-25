"""part_cmp.py <out.png> <title> <cx,cy,cz> <win_mm> <row label>=<glb>[+<glb>...] ... : close-ups (front, top, from the hand end, back) of
several renders, one row each; meshes in a row are drawn together, each in its own tint (first = grey, then orange, blue)."""
import sys

import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, 'F:/godot/blender/characters/imageto3d/tools/layers')
import visibility as vis  # noqa: E402

out, title = sys.argv[1], sys.argv[2]
pt = np.array([float(x) for x in sys.argv[3].split(',')])
win = float(sys.argv[4]) / 1000
rows = [a.split('=', 1) for a in sys.argv[5:]]
font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 17)
TINT = [(.78, .78, .78), (1., .62, .25), (.35, .55, 1.)]


def load_glb(p, tint):
    s = trimesh.load(p, process=False)
    ms = list(s.geometry.values()) if isinstance(s, trimesh.Scene) else [s]
    Vs, Fs, Cs, off = [], [], [], 0
    for m in ms:
        V = np.asarray(m.vertices)
        Vs.append(np.c_[V[:, 0], -V[:, 2], V[:, 1]])
        F = np.asarray(m.faces)
        Fs.append(F + off)
        try:
            C = np.asarray(m.visual.vertex_colors)[:, :3][F].mean(1) / 255.
            if np.ptp(C) < 1e-3:
                raise ValueError
        except Exception:  # noqa: BLE001
            C = np.tile(np.array([tint]), (len(F), 1))
        Cs.append(C)
        off += len(V)
    return np.concatenate(Vs), np.concatenate(Fs), np.concatenate(Cs)


def load(p, tint):
    if not p.endswith('.npz'):
        return load_glb(p, tint)
    z = np.load(p)
    return z['gV'].astype(np.float64), z['gF'].astype(np.int64), z['gC'].astype(np.float64)


S = 330
views = ((0, 5, 'front'), (0, 89, 'top'), (270, 5, 'from the hand end'), (180, 5, 'back'))
sheet = Image.new('RGB', (len(views) * S + 150, len(rows) * S + 30), 'white')
dr0 = ImageDraw.Draw(sheet)
dr0.text((6, 6), title, fill=(0, 0, 0), font=font)
for ri, (label, files) in enumerate(rows):
    parts = [load(p, TINT[k % 3]) for k, p in enumerate(files.split('+'))]
    off = 0
    V, F, C = [], [], []
    for v, f, c in parts:
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
        H, W = img.shape
        rgb = np.full((H, W, 3), 248.)
        fg = img >= 0
        f = img[fg]
        nf = N[f] @ dd
        rgb[fg] = np.where((nf > 0)[:, None], np.array([[1., 0, 0]]), C[f]) * (0.4 + 0.6 * np.abs(nf))[:, None] * 255
        im = Image.fromarray(np.clip(rgb[::-1], 0, 255).astype(np.uint8))
        ImageDraw.Draw(im).text((4, 4), name, fill=(0, 0, 0), font=font)
        sheet.paste(im, (150 + vi * S, 30 + ri * S))
sheet.save(out)
print('saved', out)
