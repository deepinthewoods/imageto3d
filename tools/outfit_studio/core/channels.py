"""Colour channels with role aliases (SPEC section 3, Channels). Pure numpy.

A channel is {id, label, role, material}; `role` is the alias the engine tints with, `material` the optional default
game material id ('' = engine default). Faces carry an index into the item's channel list (`os_channel`)."""
import numpy as np

ROLES = ('primary', 'secondary', 'tertiary', 'accent', 'joint')
DECORATION = {'body_armor': 'tertiary', 'gauntlets': 'tertiary', 'boots': 'tertiary'}
EXTRA_ROLE_SHARE = .15          # a second role covering this share of a piece gets its own channel


def _label(text):
    return text.replace('_', ' ').capitalize()


def _channel(cid, role, label=None):
    return {'id': cid, 'label': label or _label(cid), 'role': role, 'material': ''}


def role_areas(role, area):
    """{role: area} in descending order."""
    out = {}
    for r in sorted(set(role.tolist())):
        out[r] = float(area[role == r].sum())
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def build_channels(item_type, piece, role, area, piece_order=None, share=EXTRA_ROLE_SHARE):
    """(channels, channel_of_face): `piece`/`role` are per-face string arrays, `area` the face areas.

    * outfit: one channel per role present (id = role).
    * armour (body_armor, gauntlets, boots): `decoration` (the type's decoration role, if any face has it), then per
      piece a channel `<piece>` aliased to its majority role; further non-decoration roles covering >= `share` of the
      piece get `<piece>_<role>`, smaller ones fold into `<piece>`."""
    piece, role, area = np.asarray(piece), np.asarray(role), np.asarray(area, np.float64)
    ch_of = np.zeros(len(piece), np.int32)
    channels = []
    if item_type == 'outfit':
        present = [r for r in ROLES if (role == r).any()] + sorted(set(role.tolist()) - set(ROLES))
        for r in present:
            ch_of[role == r] = len(channels)
            channels.append(_channel(r, r))
        return channels, ch_of
    deco = DECORATION.get(item_type)
    deco_mask = (role == deco) if deco else np.zeros(len(piece), bool)
    if deco_mask.any():
        ch_of[deco_mask] = len(channels)
        channels.append(_channel('decoration', deco))
    names = list(piece_order or []) + sorted(set(piece.tolist()) - set(piece_order or []))
    for name in names:
        sel = (piece == name) & ~deco_mask
        if not sel.any():
            continue
        areas = role_areas(role[sel], area[sel])
        total = sum(areas.values()) or 1.
        main = next(iter(areas))
        ch_of[sel] = len(channels)
        channels.append(_channel(name, main))
        for r, a in list(areas.items())[1:]:
            if a / total >= share:
                ch_of[sel & (role == r)] = len(channels)
                channels.append(_channel(f'{name}_{r}', r, f'{_label(name)} ({r})'))
    return channels, ch_of


def merge_channels(new, old):
    """Keep the role alias and material of `old` channels whose id still exists in `new`. -> merged list."""
    by_id = {c['id']: c for c in old or []}
    out = []
    for c in new:
        o = by_id.get(c['id'])
        out.append({**c, 'role': o['role'], 'material': o.get('material', ''), 'label': o.get('label', c['label'])}
                   if o else dict(c))
    return out
