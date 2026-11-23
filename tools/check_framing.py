#!/usr/bin/env python3
"""Flag framing drift (shift / scale) of a generated view against its base view.

Usage: tools/check_framing.py <base.png> <image.png> [image.png ...] [--json] [--palette palette.json|dir]

The palette of the base decides which markers exist (tools/tripo_pipeline/palette.py; the
`palette.json` beside the base image, none = legacy tricolor_v1; --palette overrides):

* tricolor_v1 (legacy): the bare marker regions (yellow head, magenta anatomical-left hand,
  cyan anatomical-left foot, red neck/body) are found by hue in both images, so lighting changes
  do not matter.
* white_anchor_v2: head, anatomical-left hand and foot share ONE achromatic white anchor colour.
  Achromatic blobs (saturation < .25, value > .45, area >= ~250 px at 1254) are found in the base
  (top-most = head, then hand, then foot) and in the image; image blobs are paired with the base
  blobs by the transform fitted on the head (scale from its area, shift from its centroid), gated
  by distance and size so white garment highlights and specks never count. Red body: by hue as before.

The head is always bare, so it
is the primary anchor; hands/feet/body markers are used only while enough of
them stays visible (robes and armor may cover them). A shifted/scaled head or a global scale error is reported as FRAMING (whole figure
moved/resized); a limb marker (hand/foot) that moved while the head did not is
POSE (arm/leg proportions or pose changed). Per marker we compare
centroid and bbox size; a similarity transform (scale + shift) is estimated from
the trusted markers. Also reports stray alpha islands. Exit 1 if any image fails.

White anchors in place (white_anchor_v2): a garment covering part of a hand/foot/head, or a base whose red bounce
light tints part of a white anchor pink, moves the blob centroid and changes its area although nothing moved. So
each matched anchor is also judged on its outline against the background: the base anchor (grown through adjacent
tinted pixels) is compared with the image anchor at a 2 px tolerance. An anchor whose outline neither grew into the
background (<= 2% of it) nor lost base pixels to background or red body (<= 3% each) is `aligned`: no shift error,
and it counts as scale 1.0; the rest of it is covered (garment) and reported as `covered`. White painted over the red
body next to an anchor changes no outline and is ignored. The head is FRAMING when its outline grew into the
background by > 5% or lost > 8% to it. Anchors that are not aligned get the centroid/area checks above. A hand/foot
that is hidden in place (< 50% visible) while an unmatched white blob of >= half its size lies 2-12% of the width away
is POSE (drawn elsewhere, e.g. a white foot painted below a long hem). An unmatched white blob of >= half an anchor,
>= 70% over bare red body and > 12% of the width from every anchor is SIDE (the anatomical right hand/foot drawn
white; found in 11 accepted views by the 2026-10-26 audit).
This is a flag, not proof: rejected images still need a look; passes can still
hide drift of the garment itself (garment-only silhouettes are not constrained).
"""
import colorsys, json, os, sys
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tripo_pipeline'))
from palette import ANCHOR_SAT_MAX, ANCHOR_VAL_MIN, Palette  # noqa: E402


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
ANCHOR_MIN_AREA = 0.00016             # achromatic blob area (share of image area) below which it is a speck (~250 px at 1254)
ANCHOR_HEAD_GATE = 0.15               # head blob must lie within this many image widths of the base head
ANCHOR_MATCH_GATE = 0.06              # other anchors: max distance (image widths) from the position predicted by the head fit
ANCHOR_AREA_RANGE = (0.15, 3.0)       # image/base blob area ratio accepted as the same anchor (before hidden/partial rules)
ANCHOR_PARTS = ('head', 'left_hand', 'left_foot')   # top-to-bottom order of the three white anchors
# white anchor "in place" test (white_anchor_v2), calibrated on ~2800 accepted/rejected views (2026-10-25)
TINT_SAT_MAX = 0.6                    # base anchor grows through adjacent pixels below this saturation (red bounce light
                                      # tints parts of the white anchors pink, sat .26-.42; the red body is >= .7)
ALIGN_DILATE = 0.0016                 # outline tolerance, share of image width (2 px at 1254)
ALIGN_OUT_MAX = 0.02                  # image anchor pixels beyond the base anchor where the base is background
ALIGN_LOST_MAX = 0.03                 # base anchor pixels that became background in the image
ALIGN_RED_MAX = 0.03                  # base anchor pixels that became red body in the image
HEAD_OUT_MAX, HEAD_LOST_MAX = 0.05, 0.08   # head outline grown into / lost to the background: FRAMING
DISPLACED_GATE = (0.02, 0.12)         # hidden hand/foot + an unmatched white blob this far away (image widths) ...
DISPLACED_MIN_AREA = 0.5              # ... of at least this share of its base area = the anchor was drawn elsewhere (POSE)
STRAY_WHITE_MIN_AREA = 0.5            # unmatched white blob >= this share of the smallest base anchor ...
STRAY_WHITE_ON_RED = 0.7              # ... lying mostly where the base is bare red body ...
STRAY_WHITE_MIN_DIST = 0.12           # ... farther than this (image widths) from every base anchor = SIDE (right hand/foot white)


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


def masks(img, only=None):
    h, s, v, a = hsv(img)
    out = {}
    for name, c in MARKERS.items():
        if only is not None and name not in only:
            continue
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


def anchor_blobs(img):
    """Achromatic anchor blobs (white_anchor_v2) of an image, top-most first: dicts with the
    stats() fields plus `mask`."""
    h, s, v, a = hsv(img)
    m = (a > 0.5) & (s < ANCHOR_SAT_MAX) & (v > ANCHOR_VAL_MIN)
    lab, n = label(m)
    out = []
    if n:
        sizes = sizes_of(lab, n)
        for i in np.nonzero(sizes >= ANCHOR_MIN_AREA * m.size)[0]:
            blob = lab == i + 1
            st = stats(blob)
            st['mask'] = blob
            out.append(st)
    return sorted(out, key=lambda b: b['cy'])


def anchor_names(blobs):
    """Part names of base anchor blobs (top to bottom): head, left_hand, left_foot when all three exist."""
    return list(ANCHOR_PARTS) if len(blobs) == len(ANCHOR_PARTS) else [f'anchor{i + 1}' for i in range(len(blobs))]


def merge_blobs(blobs):
    """One blob (stats dict) from several: area, area-weighted centroid, bounding-box size (and the union mask)."""
    if len(blobs) == 1:
        return blobs[0]
    area = sum(b['area'] for b in blobs)
    x0 = min(b['cx'] - b['w'] / 2 for b in blobs); x1 = max(b['cx'] + b['w'] / 2 for b in blobs)
    y0 = min(b['cy'] - b['h'] / 2 for b in blobs); y1 = max(b['cy'] + b['h'] / 2 for b in blobs)
    out = dict(area=area, cx=sum(b['cx'] * b['area'] for b in blobs) / area,
               cy=sum(b['cy'] * b['area'] for b in blobs) / area, w=x1 - x0, h=y1 - y0)
    if all('mask' in b for b in blobs):
        out['mask'] = np.logical_or.reduce([b['mask'] for b in blobs])
    return out


def match_anchors(base_blobs, img_blobs, width):
    """Pair image blobs with base blobs. The head (top-most base blob) is the blob nearest to it within
    ANCHOR_HEAD_GATE; scale (sqrt of its area ratio) and shift of that pair predict the other positions,
    and a blob must be within ANCHOR_MATCH_GATE of its prediction with a plausible area. Returns a list
    parallel to base_blobs of image blobs (None = no match); each image blob is used once."""
    used, out = set(), [None] * len(base_blobs)
    if not base_blobs:
        return out
    head = base_blobs[0]
    cand = [(np.hypot(b['cx'] - head['cx'], b['cy'] - head['cy']), i) for i, b in enumerate(img_blobs)
            if ANCHOR_AREA_RANGE[0] <= b['area'] / head['area'] <= ANCHOR_AREA_RANGE[1]]
    cand = [c for c in cand if c[0] <= ANCHOR_HEAD_GATE * width]
    if not cand:
        return out
    hi = min(cand)[1]
    used.add(hi)
    out[0] = img_blobs[hi]
    sc = float(np.sqrt(img_blobs[hi]['area'] / head['area']))
    for k in range(1, len(base_blobs)):
        b = base_blobs[k]
        px = img_blobs[hi]['cx'] + sc * (b['cx'] - head['cx'])
        py = img_blobs[hi]['cy'] + sc * (b['cy'] - head['cy'])
        gate = ANCHOR_MATCH_GATE * width
        free = [i for i in range(len(img_blobs)) if i not in used]
        singles = [(np.hypot(img_blobs[i]['cx'] - px, img_blobs[i]['cy'] - py), (i,)) for i in free
                   if ANCHOR_AREA_RANGE[0] <= img_blobs[i]['area'] / b['area'] <= ANCHOR_AREA_RANGE[1]]
        options = [o for o in singles if o[0] <= gate]
        # a base blob that is two anchors touching (A-posed hand + foot) may be one blob in the base only,
        # or the reverse: try the union of the free blobs near the prediction as well
        near = tuple(i for i in free if np.hypot(img_blobs[i]['cx'] - px, img_blobs[i]['cy'] - py)
                     <= gate + sc * max(b['w'], b['h']) / 2)
        if len(near) > 1:
            u = merge_blobs([img_blobs[i] for i in near])
            if ANCHOR_AREA_RANGE[0] <= u['area'] / b['area'] <= ANCHOR_AREA_RANGE[1]:
                d = np.hypot(u['cx'] - px, u['cy'] - py)
                if d <= gate:
                    options.append((d, near))
        if options:
            # nearest in position; a union only wins over a single blob when it is closer in area
            best = min(options, key=lambda o: (abs(np.log(merge_blobs([img_blobs[i] for i in o[1]])['area'] / b['area'])), o[0]))
            used.update(best[1])
            out[k] = merge_blobs([img_blobs[i] for i in best[1]])
    return out


def dilate(m, r):
    """Binary dilation by a disk of radius r (numpy only)."""
    H, W = m.shape
    out = m.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if (dx or dy) and dx * dx + dy * dy <= r * r:
                out[max(dy, 0):H + min(dy, 0), max(dx, 0):W + min(dx, 0)] |= \
                    m[max(-dy, 0):H + min(-dy, 0), max(-dx, 0):W + min(-dx, 0)]
    return out


def anchor_fit(B, I, base_hsv, img_hsv, width):
    """How the image anchor mask I sits on the base anchor mask B, judged on the outline against the background.
    The base anchor is first grown through adjacent tinted pixels (TINT_SAT_MAX). Returns shares:
    out = image anchor beyond that region where the base is background (anchor grew or moved), lost / red = base
    anchor that became background / red body, covered = base anchor under garment (allowed). White painted over the
    red body next to an anchor (a lower collar showing more neck) changes no outline and is not counted."""
    ys, xs = np.nonzero(B | I)
    pad = int(0.03 * width) + 3
    win = (slice(max(ys.min() - pad, 0), ys.max() + pad + 1), slice(max(xs.min() - pad, 0), xs.max() + pad + 1))
    b, i = B[win], I[win]
    h, s, v, a = (x[win] for x in base_hsv)
    lab, _ = label(((a > 0.5) & (s < TINT_SAT_MAX) & (v > 0.35)) | b)
    ids = np.unique(lab[b])
    grown = np.isin(lab, ids[ids > 0])
    r = max(1, round(ALIGN_DILATE * width))
    ih, isat, iv, ia = (x[win] for x in img_hsv)
    c = MARKERS['body_red']
    red = ((ih >= c['h'][0]) | (ih <= c['h'][1])) & (isat >= c['s']) & (iv >= c['v']) & (ia > 0.5)
    lost = b & ~dilate(i, r)
    return dict(out=float((i & ~dilate(grown, r) & (a <= 0.5)).sum() / i.sum()),
                lost=float((lost & (ia <= 0.5)).sum() / b.sum()), red=float((lost & red).sum() / b.sum()),
                covered=float((lost & (ia > 0.5) & ~red).sum() / b.sum()))


def marker_stats(base, img, pal):
    """Per marker: (base stats, image stats or None, is_anchor, extra info). Legacy palettes use the
    hue masks of MARKERS; achromatic palettes pair white anchor blobs and add the hue-based body_red."""
    if pal.anchor_kind != 'achromatic':
        bm, _ = masks(base)
        im, alpha = masks(img)
        return [(n, stats(bm[n]), stats(im[n]), n in ANCHORS) for n in MARKERS], alpha, {}
    bm, _ = masks(base, ('body_red',))
    im, alpha = masks(img, ('body_red',))
    base_blobs = anchor_blobs(base)
    img_blobs = anchor_blobs(img)
    matched = match_anchors(base_blobs, img_blobs, base.size[0])
    base_hsv, img_hsv = hsv(base), hsv(img)
    for b, m in zip(base_blobs, matched):
        if m is not None and 'mask' in m:
            m['fit'] = anchor_fit(b['mask'], m['mask'], base_hsv, img_hsv, base.size[0])
    names = anchor_names(base_blobs)
    rows = [(f'{n}_white', {k: v for k, v in b.items() if k != 'mask'}, m and {k: v for k, v in m.items() if k != 'mask'},
             k == 0) for k, (n, b, m) in enumerate(zip(names, base_blobs, matched))]
    rows.append(('body_red', stats(bm['body_red']), stats(im['body_red']), False))
    extra = dict(base_anchor_blobs=len(base_blobs), image_white_blobs=len(img_blobs),
                 unmatched_white_blobs=len(img_blobs) - sum(m is not None for m in matched))
    # a hand/foot whose base place is covered (hidden) while a white blob of its size sits nearby: drawn elsewhere
    # (e.g. a long gown over the base foot and the white foot painted below the hem)
    used = {i for i, b in enumerate(img_blobs) for m in matched
            if m is not None and (m is b or ('mask' in m and (m['mask'] & b['mask']).any()))}
    W = base.size[0]
    for k, (n, b, m) in enumerate(zip(names, base_blobs, matched)):
        if k == 0 or (m is not None and m['area'] >= MIN_VISIBLE * b['area']):
            continue
        near = [(float(np.hypot(c['cx'] - b['cx'], c['cy'] - b['cy'])), c['area'] / b['area'])
                for i, c in enumerate(img_blobs) if i not in used and c['area'] >= DISPLACED_MIN_AREA * b['area']]
        near = [x for x in near if DISPLACED_GATE[0] * W < x[0] <= DISPLACED_GATE[1] * W]
        if near:
            d, ratio = min(near)
            extra.setdefault('displaced_anchors', {})[f'{n}_white'] = dict(dist_px=round(d, 1), area_ratio=round(ratio, 2))
    # anchor white painted on bare red body far from every anchor: the anatomical RIGHT hand/foot drawn white
    h, s, v, a = base_hsv
    c = MARKERS['body_red']
    base_red = ((h >= c['h'][0]) | (h <= c['h'][1])) & (s >= c['s']) & (v >= c['v']) & (a > 0.5)
    min_anchor = min((b['area'] for b in base_blobs), default=0)
    for i, blob in enumerate(img_blobs):
        if i in used or not min_anchor or blob['area'] < STRAY_WHITE_MIN_AREA * min_anchor:
            continue
        far = min(np.hypot(blob['cx'] - b['cx'], blob['cy'] - b['cy']) for b in base_blobs) > STRAY_WHITE_MIN_DIST * W
        on_red = (blob['mask'] & base_red).sum() / blob['area']
        if far and on_red >= STRAY_WHITE_ON_RED:
            extra.setdefault('stray_white', []).append(dict(cx=round(blob['cx']), cy=round(blob['cy']),
                                                            anchor_share=round(blob['area'] / min_anchor, 2)))
    return rows, alpha, extra


def check(base_path, img_path, palette=None):
    base, img = Image.open(base_path), Image.open(img_path)
    res = dict(image=str(img_path), problems=[], markers={})
    if img.size != base.size or img.mode != 'RGBA':
        res['problems'].append(f'size/mode {img.size} {img.mode} vs base {base.size} RGBA')
        return res
    W = base.size[0]
    pal = Palette.coerce(palette) if palette is not None else Palette.for_dir(os.path.dirname(os.path.abspath(base_path)))
    res['palette_scheme'] = pal.scheme
    rows, alpha, extra = marker_stats(base, img, pal)
    res.update(extra)
    for name, d in extra.get('displaced_anchors', {}).items():
        res['problems'].append(f'POSE: {name} covered in place but drawn {d["dist_px"]:.0f} px away '
                               f'({d["area_ratio"]:.0%} of its size)')
    for w in extra.get('stray_white', []):
        res['problems'].append(f'SIDE: anchor white painted on the bare red body at ({w["cx"]},{w["cy"]}) '
                               f'({w["anchor_share"]:.0%} of an anchor) - the anatomical RIGHT hand/foot must stay red')
    scales, shifts = [], []
    anchors = {n for n, _, _, is_anchor in rows if is_anchor}
    for name, sb, si, _ in rows:
        if sb is None:
            continue
        if si is None or si['area'] < MIN_VISIBLE * sb['area']:
            if name in anchors:
                res['problems'].append(f'{name}: anchor missing/covered/recoloured')
            res['markers'][name] = 'hidden'
            continue
        sc = float(np.sqrt(si['area'] / sb['area']))
        dx, dy = si['cx'] - sb['cx'], si['cy'] - sb['cy']
        res['markers'][name] = dict(scale=round(sc, 3), dx=round(float(dx), 1), dy=round(float(dy), 1))
        fit = si.get('fit')
        if fit and name in anchors and (fit['out'] > HEAD_OUT_MAX or fit['lost'] > HEAD_LOST_MAX):
            res['problems'].append(f'FRAMING: {name} outline moved/resized ({fit["out"]:.0%} grown into the '
                                   f'background, {fit["lost"]:.0%} lost to it)')
        if fit and fit['out'] <= ALIGN_OUT_MAX and fit['lost'] <= ALIGN_LOST_MAX and fit['red'] <= ALIGN_RED_MAX:
            # in place: whatever is missing is under the garment (or was pink-tinted in the base), so the
            # centroid/area change is not a shift or scale error
            res['markers'][name].update(aligned=True, covered=round(fit['covered'], 3))
            scales.append(1.0)
            continue
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
            kind = 'FRAMING' if name in anchors else 'POSE'
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
    argv = sys.argv[1:]
    palette_arg = None
    if '--palette' in argv:
        i = argv.index('--palette')
        palette_arg = Palette.coerce(__import__('pathlib').Path(argv[i + 1]))
        del argv[i:i + 2]
    args = [a for a in argv if a != '--json']
    if len(args) < 2:
        sys.exit(__doc__)
    results = [check(args[0], p, palette_arg) for p in args[1:]]
    if '--json' in sys.argv:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            print(('OK   ' if r.get('ok') else 'FLAG ') + r['image'])
            print('     markers:', r['markers'], 'scale:', r.get('scale'), '(' + r.get('palette_scheme', '?') + ')')
            for p in r['problems']:
                print('     -', p)
    sys.exit(0 if all(r.get('ok') for r in results) else 1)
