#!/usr/bin/env python3
"""Flag framing drift (shift / scale) of a generated view against its base view.

Usage: tools/check_framing.py <base.png> <image.png> [image.png ...] [--json]

The bare marker regions of the semantic-marker base (yellow head, magenta
anatomical-left hand, cyan anatomical-left foot, red neck/body) are found by hue
in both images, so lighting changes do not matter. The head is always bare, so it
is the primary anchor; hands/feet/body markers are used only while enough of
them stays visible (robes and armor may cover them). A shifted/scaled head or a global scale error is reported as FRAMING (whole figure
moved/resized); a limb marker (hand/foot) that moved while the head did not is
POSE (arm/leg proportions or pose changed). Per marker we compare
centroid and bbox size; a similarity transform (scale + shift) is estimated from
the trusted markers. Also reports stray alpha islands. Exit 1 if any image fails.
This is a flag, not proof: rejected images still need a look; passes can still
hide drift of the garment itself (garment-only silhouettes are not constrained).
"""
import colorsys, json, sys
import numpy as np
from PIL import Image


def label(mask):
    """4-connected component labelling (numpy + stack flood fill; no scipy needed)."""
    lab = np.zeros(mask.shape, dtype=np.int32)
    H, W = mask.shape
    n = 0
    for y, x in zip(*np.nonzero(mask)):
        if lab[y, x]:
            continue
        n += 1
        lab[y, x] = n
        stack = [(y, x)]
        while stack:
            cy, cx = stack.pop()
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not lab[ny, nx]:
                    lab[ny, nx] = n
                    stack.append((ny, nx))
    return lab, n


def sizes_of(lab, n):
    return np.bincount(lab.ravel(), minlength=n + 1)[1:]

# hue (0-1) windows + min saturation/value; shading only moves V, so hue is stable.
MARKERS = {
    'head_yellow':   dict(h=(0.12, 0.19), s=0.55, v=0.35),
    'hand_magenta':  dict(h=(0.80, 0.90), s=0.45, v=0.30),
    'foot_cyan':     dict(h=(0.47, 0.53), s=0.45, v=0.35),
    'body_red':      dict(h=(0.97, 0.03), s=0.55, v=0.35),  # wraps around 0
}
ANCHORS = ('head_yellow',)            # always bare per the pipeline contract
MIN_VISIBLE = 0.5                     # other markers count if >=50% of base area remains
SCALE_TOL = 0.04                      # 4% marker/head scale error
SHIFT_TOL = 0.006                     # of image width (~7 px at 1254)
STRAY_ALPHA_MAX = 30                  # px in tiny disconnected alpha islands


def hsv(img):
    a = np.asarray(img.convert('RGBA'), dtype=np.float32) / 255
    rgb, alpha = a[..., :3], a[..., 3]
    mx, mn = rgb.max(-1), rgb.min(-1)
    d = mx - mn
    h = np.zeros_like(mx)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    m = d > 1e-6
    h = np.where(m & (mx == r), ((g - b) / np.where(m, d, 1)) % 6, h)
    h = np.where(m & (mx == g), (b - r) / np.where(m, d, 1) + 2, h)
    h = np.where(m & (mx == b), (r - g) / np.where(m, d, 1) + 4, h)
    return h / 6, np.where(mx > 0, d / np.where(mx > 0, mx, 1), 0), mx, alpha


def masks(img):
    h, s, v, a = hsv(img)
    out = {}
    for name, c in MARKERS.items():
        lo, hi = c['h']
        hm = (h >= lo) | (h <= hi) if lo > hi else (h >= lo) & (h <= hi)
        m = hm & (s >= c['s']) & (v >= c['v']) & (a > 0.5)
        lab, n = label(m)
        if n:  # keep only the biggest connected blob, drops speckle/lookalike trim
            m = lab == (1 + int(np.argmax(sizes_of(lab, n))))
        out[name] = m
    return out, a


def stats(m):
    ys, xs = np.nonzero(m)
    if len(xs) == 0:
        return None
    return dict(area=len(xs), cx=xs.mean(), cy=ys.mean(), w=np.ptp(xs) + 1, h=np.ptp(ys) + 1)


def check(base_path, img_path):
    base, img = Image.open(base_path), Image.open(img_path)
    res = dict(image=str(img_path), problems=[], markers={})
    if img.size != base.size or img.mode != 'RGBA':
        res['problems'].append(f'size/mode {img.size} {img.mode} vs base {base.size} RGBA')
        return res
    W = base.size[0]
    bm, _ = masks(base)
    im, alpha = masks(img)
    scales, shifts = [], []
    for name in MARKERS:
        sb, si = stats(bm[name]), stats(im[name])
        if sb is None:
            continue
        if si is None or si['area'] < MIN_VISIBLE * sb['area']:
            if name in ANCHORS:
                res['problems'].append(f'{name}: anchor missing/covered/recoloured')
            res['markers'][name] = 'hidden'
            continue
        sc = float(np.sqrt(si['area'] / sb['area']))
        dx, dy = si['cx'] - sb['cx'], si['cy'] - sb['cy']
        res['markers'][name] = dict(scale=round(sc, 3), dx=round(float(dx), 1), dy=round(float(dy), 1))
        # a partly covered marker (area <~90%) is judged on shift only loosely
        full = si['area'] >= 0.9 * sb['area']
        if full:
            scales.append(sc)
        shifts.append((name, dx, dy, full))
    if scales:
        med = float(np.median(scales))
        res['scale'] = round(med, 3)
        if abs(med - 1) > SCALE_TOL:
            res['problems'].append(f'FRAMING: scale {med:.3f} (figure {"enlarged" if med > 1 else "shrunk"})')
    for name, dx, dy, full in shifts:
        tol = SHIFT_TOL * W * (1 if full else 3)
        if max(abs(dx), abs(dy)) > tol:
            kind = 'FRAMING' if name in ANCHORS else 'POSE'
            res['problems'].append(f'{kind}: {name} shifted ({dx:+.1f},{dy:+.1f}) px')
    # stray alpha islands
    solid = alpha > 0.1  # near-invisible fringe (alpha < ~0.1) is reported only as info
    lab, n = label(solid)
    if n > 1:
        sizes = sizes_of(lab, n)
        stray = int(sizes[sizes < sizes.max()].sum())
        res['stray_alpha_px'] = stray
        if stray > STRAY_ALPHA_MAX:
            res['problems'].append(f'{stray}px in detached alpha islands')
    ys, xs = np.nonzero(alpha > 0.5)
    bb = lambda a: [int(x) for x in (np.nonzero(a.any(0))[0][[0, -1]].tolist() + np.nonzero(a.any(1))[0][[0, -1]].tolist())]
    res['alpha_bbox_xxyy'] = bb(alpha > 0.5)
    res['base_alpha_bbox_xxyy'] = bb(np.asarray(base.getchannel('A')) > 127)
    res['ok'] = not res['problems']
    return res


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if a != '--json']
    if len(args) < 2:
        sys.exit(__doc__)
    results = [check(args[0], p) for p in args[1:]]
    if '--json' in sys.argv:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            print(('OK   ' if r.get('ok') else 'FLAG ') + r['image'])
            print('     markers:', r['markers'], 'scale:', r.get('scale'))
            for p in r['problems']:
                print('     -', p)
    sys.exit(0 if all(r.get('ok') for r in results) else 1)
