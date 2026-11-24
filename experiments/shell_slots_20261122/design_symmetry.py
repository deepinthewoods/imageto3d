"""Is a generated module's DESIGN left/right symmetric? Measured on its own front and back view images (2026-11-23).

User: "we can't just flip it because some of the outfits are asymmetrical intentionally". The views are edits of symmetric base renders, so a
symmetric design paints mirror pixels the same colour class. Per view: pixels are classed by the palette (garment roles, red body, white anchors,
background); the image is mirrored about the base's own symmetry column; a garment pixel counts as matched when its mirrored pixel, or one within
TOL px of it, has the same class. Asymmetry = unmatched garment pixels / garment pixels (front and back views; the larger).
The anatomical-left hand (and foot) are white anchors on purpose and the gauntlet/boot is one-sided: only outfits / body armour are judged, and the
anchor class is ignored.
"""
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[2]
TOL = 6
CLASSES = {'bg': None, 'body': (230, 25, 35), 'anchor': (237, 237, 237), 'primary': (25, 210, 40), 'secondary': (30, 65, 235),
           'tertiary': (245, 220, 20), 'accent': (225, 25, 220)}


def classify(img):
    a = np.asarray(img.convert('RGBA')).astype(np.float64)
    rgb, alpha = a[..., :3], a[..., 3]
    names = [k for k in CLASSES if k != 'bg']
    P = np.array([CLASSES[k] for k in names], np.float64)
    # hue-ish: nearest palette colour after normalising brightness a little (shading)
    x = rgb / np.maximum(rgb.max(-1, keepdims=True), 1) * 255
    Pn = P / P.max(-1, keepdims=True) * 255
    d = ((x[..., None, :] - Pn[None, None]) ** 2).sum(-1)
    lab = d.argmin(-1) + 1                      # 1.. = names index + 1
    sat = (rgb.max(-1) - rgb.min(-1)) / np.maximum(rgb.max(-1), 1)
    lab = np.where((sat < 0.25) & (rgb.max(-1) > 115), names.index('anchor') + 1, lab)
    lab = np.where(alpha < 128, 0, lab)
    return lab, ['bg'] + names


_AXIS = {}


def axis_column(base_png):
    """The column the base render is mirror-symmetric about (its alpha): coarse from the column profile, refined on the full mask (cached)."""
    key = str(base_png)
    if key in _AXIS:
        return _AXIS[key]
    a = np.asarray(Image.open(base_png).convert('RGBA'))[..., 3] > 128
    prof = a.sum(0).astype(float)
    W = len(prof)
    best, bc = -1, W / 2
    for c2 in range(int(W * 0.6), int(W * 1.4)):
        c = c2 / 2
        xs = np.arange(W)
        mx = np.round(2 * c - xs).astype(int)
        ok = (mx >= 0) & (mx < W)
        sc = -np.abs(prof[ok] - prof[mx[ok]]).sum() - prof[~ok].sum()
        if sc > best or best == -1:
            best, bc = sc, c
    fine, fc = -1, bc
    for c in np.arange(bc - 2, bc + 2.01, 0.5):
        xs = np.arange(W)
        mx = np.round(2 * c - xs).astype(int)
        ok = (mx >= 0) & (mx < W)
        sc = (a[:, ok] == a[:, mx[ok]]).mean()
        if sc > fine:
            fine, fc = sc, c
    _AXIS[key] = fc
    return fc


def view_asymmetry(png, axis):
    lab, names = classify(Image.open(png))
    garm = np.isin(lab, [names.index(k) for k in ('primary', 'secondary', 'tertiary', 'accent')])
    H, W = lab.shape
    xs = np.arange(W)
    mx = np.round(2 * axis - xs).astype(int)
    valid = (mx >= 0) & (mx < W)
    mir = np.zeros_like(lab)
    mir[:, valid] = lab[:, mx[valid]]
    matched = np.zeros_like(garm)
    for k in ('primary', 'secondary', 'tertiary', 'accent'):
        ki = names.index(k)
        near = ndimage.binary_dilation(mir == ki, iterations=TOL)
        matched |= (lab == ki) & near
    g = garm & valid[None, :]
    return float(1 - matched[g].sum() / max(g.sum(), 1)), int(g.sum())


def module_views(mod):
    """The view folder of a model module path models/<set>/<job>/<race>/<module>."""
    mod = Path(mod)
    s, j, race, name = mod.parts[-4], mod.parts[-3], mod.parts[-2], mod.parts[-1]
    for stage in ('completed', 'processing', 'multiview_ready', 'processing_views', 'held_conversions'):
        for d in (ROOT / stage).glob(f'{s}*--{j}'):
            p = d / race / name
            if (p / 'front.png').is_file():
                return p, d
    return None, None


def design_asymmetry(mod, race):
    vdir, job = module_views(mod)
    if vdir is None:
        return None
    base = job / '.base_views' / race
    if not (base / 'front.png').is_file():
        base = ROOT / 'base_views' / race
    out = {}
    for v in ('front', 'back'):
        if (vdir / f'{v}.png').is_file() and (base / f'{v}.png').is_file():
            out[v] = view_asymmetry(vdir / f'{v}.png', axis_column(base / f'{v}.png'))[0]
    if not out:
        return None
    return {'asymmetry': round(max(out.values()), 3), **{k: round(v, 3) for k, v in out.items()}, 'views': str(vdir.relative_to(ROOT)).replace('\\', '/')}


if __name__ == '__main__':
    import sys
    race = sys.argv[1]
    mods = sorted(p for p in ROOT.glob(f'models/*/*/{race}/*') if p.name in ('outfit', 'body_armor'))
    rows = []
    for m in mods:
        r = design_asymmetry(m, race)
        rows.append({'module': m.relative_to(ROOT).as_posix(), **(r or {'asymmetry': None})})
        print(rows[-1]['module'].split('/')[1][:46], rows[-1]['module'].split('/')[2][:6], rows[-1]['module'].split('/')[-1], r and r['asymmetry'], r and (r.get('front'), r.get('back')), flush=True)
    Path(sys.argv[2] if len(sys.argv) > 2 else f'design_symmetry_{race}.json').write_text(json.dumps(rows, indent=1))
