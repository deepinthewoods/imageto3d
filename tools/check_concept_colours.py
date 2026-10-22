#!/usr/bin/env python3
"""Measure how far each palette colour in a concept image has drifted from the palette hue.

Usage: tools/check_concept_colours.py <image.png> [image.png ...] [--palette palette.json|dir]
           [--separable] [--max-drift 12] [--json]

Image generators sometimes repaint a marker colour in a slightly different hue (the magenta accent
#E119DC, hue ~304 deg, has come out as hot pink ~322 deg). The Tripo label step can only separate roles
while their hues stay apart, so this flags it at concept time. For each palette colour (body, hue
anchors, garments; the separable `joint` colour with --separable) the weighted hue histogram of the
opaque, saturated pixels (same saturation/brightness ramps as palette.py) is searched for a peak within
+-45 deg of the nominal hue (never past the midpoint to another palette hue); the peak's hue is the
measured hue. Reported per colour: nominal hue, measured hue, signed drift, share of the coloured
pixels. DRIFT when |drift| > --max-drift (default 12 deg); a colour with no peak is just "absent"
(modules do not use every colour). Exit 1 if any image has a DRIFT. This is a flag, not proof: it only
looks at hue, not at whether the colour sits on the right body part. Tool only; nothing calls it.
"""
import argparse, json, os, sys
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tripo_pipeline'))
from palette import SAT_RAMP, VAL_RAMP, Palette, hsv, ramp  # noqa: E402

WINDOW = 45.        # search +-this around the nominal hue
MIN_SHARE = .002    # a peak needs this share of the coloured pixel weight (within +-8 deg)


def circ(a, b):
    return (a - b + 180) % 360 - 180


def measure(path, palette, max_drift=12.):
    """Per-colour hue drift for one image. `palette` is a Palette."""
    img = np.asarray(Image.open(path).convert('RGBA'), float) / 255
    px = img[img[..., 3] > .5][:, :3]
    h, s, v = hsv(px)
    weight = ramp(s, *SAT_RAMP) * ramp(v, *VAL_RAMP)
    bins = np.bincount(np.floor(h).astype(int) % 360, weights=weight, minlength=360)
    total = float(bins.sum())
    kernel = np.exp(-0.5 * (np.arange(-9, 10) / 2.5) ** 2)
    kernel /= kernel.sum()
    smooth = sum(np.roll(bins, o) * kernel[o + 9] for o in range(-9, 10))
    names, nominal = palette.hue_names(), palette.nominal_hues()
    out, problems = {}, []
    for n, hn in zip(names, nominal):
        others = [ho for no, ho in zip(names, nominal) if no != n]
        # keep to this colour's side of every other palette hue
        reach = WINDOW
        for ho in others:
            d = circ(ho, hn)
            if abs(d) / 2 < reach:
                reach = abs(d) / 2
        cand = [(smooth[i % 360], i % 360) for i in range(int(round(hn - reach)), int(round(hn + reach)) + 1)
                if smooth[i % 360] > 0 and smooth[i % 360] >= smooth[(i - 1) % 360] and smooth[i % 360] > smooth[(i + 1) % 360]]
        rec = {'nominal': round(float(hn), 1), 'measured': None, 'drift': None, 'share': 0., 'status': 'absent'}
        if cand and total:
            _, pk = max(cand)
            offs = np.arange(-8, 9)
            wts = np.array([bins[(pk + o) % 360] for o in offs])
            share = float(wts.sum() / total)
            if share >= MIN_SHARE:
                measured = (pk + float((offs * wts).sum() / wts.sum())) % 360
                drift = float(circ(measured, hn))
                rec.update(measured=round(measured, 1), drift=round(drift, 1), share=round(share, 4),
                           status='DRIFT' if abs(drift) > max_drift else 'ok')
                if rec['status'] == 'DRIFT':
                    problems.append(f'{n}: hue {measured:.0f} deg vs palette {hn:.0f} deg ({drift:+.0f} deg)')
        out[n] = rec
    return {'image': str(path), 'colours': out, 'problems': problems, 'ok': not problems, 'max_drift': max_drift,
            'palette_scheme': palette.scheme}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('images', nargs='+')
    ap.add_argument('--palette', help='palette.json or a base folder holding one (default: legacy palette)')
    ap.add_argument('--separable', action='store_true', help='include the separable joint colour')
    ap.add_argument('--max-drift', type=float, default=12.)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    pal = Palette.coerce(a.palette) if a.palette else Palette.legacy_default()
    if a.separable:
        pal = pal.with_separable()
    results = [measure(p, pal, a.max_drift) for p in a.images]
    if a.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(f"{r['image']}  [{r['palette_scheme']}]  {'OK' if r['ok'] else 'DRIFT'}")
            for n, c in r['colours'].items():
                if c['status'] == 'absent':
                    print(f"   {n:10s} nominal {c['nominal']:5.1f}  absent")
                else:
                    print(f"   {n:10s} nominal {c['nominal']:5.1f}  measured {c['measured']:5.1f}  drift {c['drift']:+5.1f}  share {c['share']:.1%}  {c['status']}")
    return 0 if all(r['ok'] for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
