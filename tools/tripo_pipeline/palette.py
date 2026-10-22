"""Colour palette of a marker base: body, bare-anchor markers and garment colours.

One `palette.json` sits beside the four views of every base (`base_views/<race>/`, its job snapshot
`.base_views/<race>/` and the archived sets). Two schemes exist:

* `white_anchor_v2` (active since 2026-10-22): red body, ONE shared achromatic anchor colour on the
  bare head / anatomical-left hand / anatomical-left foot, and up to four garment colours
  (primary green, secondary blue, tertiary yellow, accent magenta).
* `tricolor_v1` (legacy): red body, three hue anchors (yellow head, magenta left hand, cyan left
  foot) and two garment colours (primary green, secondary blue).

A base without `palette.json` (older frozen job snapshots, old `swatches.json` files) is legacy
`tricolor_v1` with the colours of `config.json` `marker_palette_srgb`; its classification is
bit-for-bit the one `face_labels.classify_samples` always had.

Class indices of `Palette.classify`: 0 = base (body AND all anchors, i.e. everything the wearable
cut removes), 1..K = garment colours in the order of `garments`, K+1 = no usable colour. Garment roles
become material names (`primary`, `secondary`, `tertiary`, `accent`); `uncertain` and `off_body` are
reserved for the pipeline.

Separable mode (2026-10-22): a palette.json may carry an optional `separable_garments` block (default: config.json
`separable_garments`, role `joint` = cyan #14DCEB). It is ignored unless the palette is built with `separable=True`
(reduce_model.py / convert_job.py `--separable`), which appends those garments after the normal ones (scheme gets the
suffix `_joint`); without it nothing changes. A palette that already lists the role (the hand-made experiment palettes)
is left as it is.

numpy only (no Blender), so tools/check_framing.py can use it too.
"""
import json
from pathlib import Path

import numpy as np

HUE_TOLERANCE = 20.0      # degrees; closest swatch hues (cyan/blue) are ~45 deg apart
SAT_RAMP = (.2, .45)      # sample weight 0 at/below first value, 1 at/above second
VAL_RAMP = (.08, .25)
# Achromatic anchor (white_anchor_v2): a sample is anchor when saturation < ANCHOR_SAT_MAX and value >
# ANCHOR_VAL_MIN; its weight fades towards those limits. Garment hues are all >= .8 saturated, so the
# window cannot take a garment pixel unless it is washed out to grey.
ANCHOR_SAT_MAX = .25
ANCHOR_VAL_MIN = .45
ANCHOR_SAT_RAMP = (.15, ANCHOR_SAT_MAX)   # weight 1 at/below the first value, 0 at the second
ANCHOR_VAL_RAMP = (ANCHOR_VAL_MIN, .6)    # weight 0 at the first value, 1 at/above the second

HUE_FIT_MAX_SHIFT = 30.   # degrees a colour's fitted hue may differ from the palette hue
HUE_FIT_DEAD = 8.         # shifts below this are ignored: a calibrated model is classified by the nominal palette
HUE_FIT_MARGIN = 6.       # a peak must be this much closer to a colour than to any other palette hue
HUE_FIT_MIN_SHARE = .003  # a peak needs this share of the total sample weight (within +-8 degrees)

SCHEME_LEGACY = 'tricolor_v1'
SCHEME_WHITE = 'white_anchor_v2'
RESERVED_ROLES = ('base', 'uncertain', 'off_body')
UNCERTAIN_COLOR = (1., .4, .03)
CONFIG = Path(__file__).resolve().parent / 'config.json'


def swatch_rgb(value):
    if not isinstance(value, str) or len(value) != 7 or not value.startswith('#'):
        raise ValueError('Swatches must be #RRGGBB strings')
    return np.array([int(value[i:i+2], 16) for i in (1, 3, 5)], float) / 255


def hsv(rgb):
    rgb = np.asarray(rgb, float)
    mx, mn = rgb.max(1), rgb.min(1)
    d = mx - mn
    r, g, b = rgb.T
    h = np.zeros(len(rgb))
    nz = d > 1e-9
    i = nz & (mx == r)
    h[i] = ((g - b)[i] / d[i]) % 6
    j = nz & (mx == g) & ~i
    h[j] = (b - r)[j] / d[j] + 2
    k = nz & ~i & ~j
    h[k] = (r - g)[k] / d[k] + 4
    return h * 60, np.where(mx > 0, d / np.maximum(mx, 1e-9), 0), mx


def ramp(x, lo, hi):
    return np.clip((x - lo) / (hi - lo), 0, 1)


class Palette:
    """A validated palette. Build with `load`, `for_dir`, `legacy_default`, `from_swatches` or `coerce`."""

    def __init__(self, data, source='memory', separable=False):
        self.source = str(source)
        self.separable = False
        sep = data.get('separable_garments')
        if sep is None:
            sep = json.loads(CONFIG.read_text()).get('separable_garments') if separable else None
        for g in sep or []:
            swatch_rgb(g['hex'])
        if separable:
            if not sep:
                raise ValueError('separable palette requested but no separable_garments are defined')
            have = {g['role'] for g in data.get('garments') or []}
            extra = [g for g in sep if g['role'] not in have]
            data = dict(data, garments=list(data.get('garments') or []) + extra)
            if extra and data.get('scheme'):
                data['scheme'] = data['scheme'] + '_joint'
            self.separable = True
        self.separable_garments = [(str(g['role']), g['hex']) for g in sep or []]
        self.hue_shift = {}      # name -> degrees added to that colour's nominal hue (fit_hues); empty = nominal palette
        self.scheme = str(data.get('scheme') or '')
        base = data.get('base') or {}
        self.base_name, self.base_hex = base.get('name', 'body_red'), base.get('hex')
        swatch_rgb(self.base_hex)
        anchors = data.get('anchors') or {}
        self.anchor_kind = anchors.get('kind')
        if self.anchor_kind == 'hue':
            markers = anchors.get('markers')
            if not isinstance(markers, list) or not markers:
                raise ValueError('hue anchors need a nonempty markers list')
            self.anchor_markers = [(str(m['part']), m['hex']) for m in markers]
            for _, hex_ in self.anchor_markers:
                swatch_rgb(hex_)
            self.anchor_hex, self.anchor_parts = None, [part for part, _ in self.anchor_markers]
        elif self.anchor_kind == 'achromatic':
            self.anchor_hex = anchors.get('hex')
            swatch_rgb(self.anchor_hex)
            self.anchor_parts = [str(p) for p in anchors.get('parts') or []]
            if not self.anchor_parts:
                raise ValueError('achromatic anchors need a nonempty parts list')
            self.anchor_markers = []
        else:
            raise ValueError(f'unknown anchor kind: {self.anchor_kind!r}')
        garments = data.get('garments')
        if not isinstance(garments, list) or not garments:
            raise ValueError('palette needs a nonempty garments list')
        self.garments = [(str(g['role']), g['hex']) for g in garments]
        roles = [r for r, _ in self.garments]
        if len(set(roles)) != len(roles) or set(roles) & set(RESERVED_ROLES):
            raise ValueError(f'garment roles must be unique and not {RESERVED_ROLES}: {roles}')
        for _, hex_ in self.garments:
            swatch_rgb(hex_)
        if not self.scheme:
            self.scheme = SCHEME_WHITE if self.anchor_kind == 'achromatic' else SCHEME_LEGACY

    # ------------------------------------------------------------------ construction

    @classmethod
    def load(cls, path, separable=False):
        path = Path(path)
        return cls(json.loads(path.read_text()), path, separable)

    @classmethod
    def for_dir(cls, base_dir, separable=False):
        """`palette.json` of a base folder; a folder without one is a legacy tricolor_v1 base."""
        if base_dir is not None and (Path(base_dir) / 'palette.json').is_file():
            return cls.load(Path(base_dir) / 'palette.json', separable)
        return cls.legacy_default(note='legacy default (no palette.json' +
                                       (f' in {base_dir})' if base_dir is not None else ')'), separable=separable)

    def hue_names(self):
        """Names of the hue-bearing colours in classify order: body, hue anchors (legacy), garments."""
        return ['body'] + ([p for p, _ in self.anchor_markers] if self.anchor_kind == 'hue' else []) + self.roles

    def nominal_hues(self):
        """Nominal hue (degrees) per hue_names() entry, from the palette hexes."""
        hexes = [self.base_hex] + ([h for _, h in self.anchor_markers] if self.anchor_kind == 'hue' else []) + [h for _, h in self.garments]
        return hsv(np.array([swatch_rgb(x) for x in hexes]))[0]

    def with_hue_shifts(self, shifts):
        """Copy of this palette whose classification uses the nominal hue + shift per colour name."""
        out = Palette(self.to_dict() | ({'separable_garments': [{'role': r, 'hex': h} for r, h in self.separable_garments]}
                                        if self.separable_garments else {}), self.source, False)
        out.separable = self.separable
        out.hue_shift = {k: float(v) for k, v in shifts.items() if v}
        return out

    def fit_hues(self, colors, max_shift=HUE_FIT_MAX_SHIFT, dead=HUE_FIT_DEAD, margin=HUE_FIT_MARGIN, min_share=HUE_FIT_MIN_SHARE):
        """Per-model hue centres from the texture itself: a weighted histogram (saturation x brightness ramps) of the
        samples, smoothed, with one peak per palette colour. A colour takes the strongest peak within +-`max_shift` of its
        nominal hue when that peak is clearly closer to it than to any other palette hue (by `margin` degrees), no two
        colours share a peak, and shifts under `dead` degrees are ignored (so a well-calibrated model is classified exactly
        as by the nominal palette). Returns (shifts {name: degrees}, report dict with per-colour peaks and review flags)."""
        colors = np.asarray(colors, float)
        h, sat, val = hsv(colors)
        w = ramp(sat, *SAT_RAMP) * ramp(val, *VAL_RAMP)
        bins = np.bincount(np.floor(h).astype(int) % 360, weights=w, minlength=360)
        total = float(bins.sum())
        k = np.exp(-0.5 * (np.arange(-9, 10) / 2.5) ** 2)
        k /= k.sum()
        smooth = sum(np.roll(bins, o) * k[o + 9] for o in range(-9, 10))
        names, nominal = self.hue_names(), self.nominal_hues()
        dist = lambda a, b: abs((a - b + 180) % 360 - 180)
        peaks = [i for i in range(360) if smooth[i] > 0 and smooth[i] >= smooth[(i - 1) % 360] and smooth[i] > smooth[(i + 1) % 360]]
        mass = {i: float(sum(bins[(i + o) % 360] for o in range(-8, 9))) for i in peaks}
        report = {'max_shift': max_shift, 'dead_zone': dead, 'margin': margin, 'samples_weight': total, 'colours': {}, 'review': []}
        strong = [i for i in peaks if total and mass[i] / total >= min_share]
        # one peak per colour, no two colours on one peak: greedy by increasing hue distance (a peak that is nearer to a
        # colour that already has its own peak, e.g. the body red, goes to the next colour instead)
        pairs = sorted((dist(i, hn), ci, i) for ci, hn in enumerate(nominal) for i in strong if dist(i, hn) <= max_shift)
        taken, claimed = {}, set()
        for d_, ci, i in pairs:
            if ci not in taken and i not in claimed:
                taken[ci] = (i, d_)
                claimed.add(i)
        shifts = {}
        for ci, (n, hn) in enumerate(zip(names, nominal)):
            rec = {'nominal': float(hn), 'shift': 0., 'applied': False}
            if ci not in taken:
                rec['note'] = 'no peak in the window'
                report['colours'][n] = rec
                continue
            pk, d_self = taken[ci]
            rec.update(peak=float(pk), peak_share=mass[pk] / total, distance=float(d_self))
            # a colour without a peak of its own that sits (nearly) as close to this peak makes the assignment ambiguous
            rivals = [no for cj, (no, ho) in enumerate(zip(names, nominal))
                      if cj != ci and cj not in taken and dist(pk, ho) < d_self + margin]
            if rivals:
                rec['note'] = f'ambiguous: peak is within {margin} deg of the hue of {rivals}, which have no peak of their own'
                report['review'].append(f'{n}: peak {pk} deg is not clearly closer to it than to {rivals}')
            elif d_self < dead:
                rec['note'] = 'within the dead zone'
            else:
                s_ = (pk - hn + 180) % 360 - 180
                rec.update(shift=float(s_), applied=True)
                shifts[n] = float(s_)
            report['colours'][n] = rec
        report['shifts'] = shifts
        return shifts, report

    def with_separable(self):
        """This palette plus the separable garments (itself when already separable / the role is already listed)."""
        if self.separable:
            return self
        data = self.to_dict()
        if self.separable_garments:
            data['separable_garments'] = [{'role': r, 'hex': h} for r, h in self.separable_garments]
        return Palette(data, self.source, True)

    @classmethod
    def legacy_default(cls, note='legacy default (config.json marker_palette_srgb)', separable=False):
        colours = json.loads(CONFIG.read_text())['marker_palette_srgb']
        return cls.from_swatches({
            'base': colours['body_red'],
            'base_markers': {'head': colours['head_yellow'], 'left_hand': colours['left_hand_magenta'],
                             'left_foot': colours['left_foot_cyan']},
            'primary': colours['primary_green'], 'secondary': colours['secondary_blue']}, note, separable)

    @classmethod
    def from_swatches(cls, swatches, source='swatches', separable=False):
        """The old `swatches.json` form {base, base_markers?, primary, secondary} (legacy tricolor_v1)."""
        if set(swatches) not in [{'base', 'primary', 'secondary'}, {'base', 'primary', 'secondary', 'base_markers'}]:
            raise ValueError('Swatches need base, primary, secondary and optional base_markers')
        markers = swatches.get('base_markers')
        if 'base_markers' in swatches and (not isinstance(markers, dict) or not markers):
            raise ValueError('base_markers must be a nonempty name-to-hex map')
        data = {'scheme': SCHEME_LEGACY, 'base': {'name': 'body_red', 'hex': swatches['base']},
                'anchors': {'kind': 'hue', 'markers': [{'part': k, 'hex': v} for k, v in (markers or {}).items()]},
                'garments': [{'role': 'primary', 'hex': swatches['primary']},
                             {'role': 'secondary', 'hex': swatches['secondary']}]}
        if not markers:
            # no markers recorded: base only (the old code accepted this); hue anchors need >= 1, so
            # represent it as a palette whose only base-like colour is the body.
            data['anchors'] = {'kind': 'hue', 'markers': [{'part': 'body', 'hex': swatches['base']}]}
        return cls(data, source, separable)

    @classmethod
    def coerce(cls, value, source='memory', separable=False):
        """Palette | palette dict | swatches dict | path of either JSON."""
        if isinstance(value, cls):
            return value.with_separable() if separable else value
        if isinstance(value, (str, Path)):
            value = Path(value)
            return cls.for_dir(value, separable) if value.is_dir() else cls.coerce(json.loads(value.read_text()), value, separable)
        if 'scheme' in value or 'garments' in value:
            return cls(value, source, separable)
        return cls.from_swatches(value, source, separable)

    # ------------------------------------------------------------------ description

    @property
    def n_garments(self):
        return len(self.garments)

    @property
    def roles(self):
        return [r for r, _ in self.garments]

    @property
    def is_legacy(self):
        return self.scheme == SCHEME_LEGACY

    @property
    def unusable(self):
        """Class index of samples with no usable colour."""
        return len(self.garments) + 1

    def material_names(self):
        """Material slots of a reduced mesh: class index -> name; the last one is 'uncertain'."""
        return ['base', *self.roles, 'uncertain']

    def material_srgb(self):
        """Display colour per material slot (base, garments..., orange uncertain)."""
        return np.vstack([swatch_rgb(self.base_hex), *[swatch_rgb(h) for _, h in self.garments],
                          np.array(UNCERTAIN_COLOR)])

    def to_dict(self):
        out = {'scheme': self.scheme, 'base': {'name': self.base_name, 'hex': self.base_hex}}
        if self.anchor_kind == 'achromatic':
            out['anchors'] = {'kind': 'achromatic', 'hex': self.anchor_hex, 'parts': list(self.anchor_parts)}
        else:
            out['anchors'] = {'kind': 'hue', 'markers': [{'part': p, 'hex': h} for p, h in self.anchor_markers]}
        out['garments'] = [{'role': r, 'hex': h} for r, h in self.garments]
        return out

    def swatches_dict(self):
        """Legacy `swatches.json` form (hue anchors, exactly two garments), else None."""
        if self.anchor_kind != 'hue' or self.roles != ['primary', 'secondary']:
            return None
        return {'base': self.base_hex, 'base_markers': dict(self.anchor_markers),
                'primary': self.garments[0][1], 'secondary': self.garments[1][1]}

    def describe(self):
        """Compact record for manifests."""
        return {**self.to_dict(), 'source': self.source, **({'separable': True} if self.separable else {}), **({'hue_shift': dict(self.hue_shift)} if self.hue_shift else {}), 'classes': ['base', *self.roles, 'unusable']}

    # ------------------------------------------------------------------ classification

    def classify(self, colors):
        """(class 0..K+1, weight 0..1) per colour sample (rows of sRGB 0..1)."""
        colors = np.asarray(colors, float)
        names = [self.base_hex] + ([h for _, h in self.anchor_markers] if self.anchor_kind == 'hue' else []) \
            + [h for _, h in self.garments]
        base_like = len(names) - len(self.garments)
        centre_h, _, _ = hsv(np.array([swatch_rgb(s) for s in names]))
        if self.hue_shift:
            centre_h = centre_h + np.array([self.hue_shift.get(n, 0.) for n in self.hue_names()])
        h, s, v = hsv(colors)
        dist = np.abs((h[:, None] - centre_h[None] + 180) % 360 - 180)
        nearest = dist.argmin(1)
        role = np.where(nearest < base_like, 0, nearest - base_like + 1)
        weight = ramp(s, *SAT_RAMP) * ramp(v, *VAL_RAMP) * (dist.min(1) <= HUE_TOLERANCE)
        if self.anchor_kind == 'achromatic':
            anchor = (s < ANCHOR_SAT_MAX) & (v > ANCHOR_VAL_MIN)
            anchor_weight = ramp(v, *ANCHOR_VAL_RAMP) * (1 - ramp(s, *ANCHOR_SAT_RAMP))
            role = np.where(anchor, 0, role)
            weight = np.where(anchor, anchor_weight, weight)
        return np.where(weight > 0, role, self.unusable), weight

    def anchor_mask(self, colors):
        """bool per colour sample: looks like one of this palette's bare-anchor colours (achromatic rule, or
        nearest anchor hue within HUE_TOLERANCE and enough saturation/brightness). Garment and body colours
        are False. Used to tell which base a concept was drawn on (workflow.py claim)."""
        colors = np.asarray(colors, float)
        h, s, v = hsv(colors)
        if self.anchor_kind == 'achromatic':
            return (s < ANCHOR_SAT_MAX) & (v > ANCHOR_VAL_MIN)
        names = [self.base_hex] + [hex_ for _, hex_ in self.anchor_markers] + [hex_ for _, hex_ in self.garments]
        centre_h, _, _ = hsv(np.array([swatch_rgb(x) for x in names]))
        dist = np.abs((h[:, None] - centre_h[None] + 180) % 360 - 180)
        nearest = dist.argmin(1)
        usable = ramp(s, *SAT_RAMP) * ramp(v, *VAL_RAMP) * (dist.min(1) <= HUE_TOLERANCE) > 0
        return usable & (nearest >= 1) & (nearest <= len(self.anchor_markers))
