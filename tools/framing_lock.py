#!/usr/bin/env python3
"""Print a framing-lock sentence for an imagegen prompt, measured from a base view.

Usage: tools/framing_lock.py <base.png> [<base.png> ...]

The built-in imagegen tool has no size/fidelity/mask controls and often returns
the figure enlarged 1.08-1.25x (head pushed up, hands out, feet down). Codex's
advice (2026-10-22): state the spatial constraint FIRST in the prompt, with pixel
coordinates measured from the base. This prints that sentence; paste it at the very
top of each view's prompt. It does not replace tools/check_framing.py.

The palette beside the base (palette.json; none = legacy tricolor_v1) decides how the bare anchors are
described: white_anchor_v2 -> "white head / white anatomical-left hand / white anatomical-left foot"
(found as achromatic blobs, top-most = head); tricolor_v1 -> the yellow head, magenta hand, cyan foot.
"""
import os
import sys
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tripo_pipeline'))
from check_framing import anchor_blobs, anchor_names, masks  # noqa: E402
from palette import Palette  # noqa: E402


def measure(path):
    img = Image.open(path).convert('RGBA')
    a = np.asarray(img)[..., 3] > 25
    ys, xs = np.nonzero(a)
    out = dict(size=img.size, bbox=(int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())),
               palette=Palette.for_dir(os.path.dirname(os.path.abspath(path))))
    if out['palette'].anchor_kind == 'achromatic':
        blobs = anchor_blobs(img)
        for name, blob in zip(anchor_names(blobs), blobs):
            my, mx = np.nonzero(blob['mask'])
            out[name + '_white'] = (int(mx.min()), int(my.min()), int(mx.max()), int(my.max()))
        for name, m in masks(img, ('body_red',))[0].items():
            if m.any():
                my, mx = np.nonzero(m)
                out[name] = (int(mx.min()), int(my.min()), int(mx.max()), int(my.max()))
        return out
    for name, m in masks(img)[0].items():
        if m is None or not m.any():
            continue
        my, mx = np.nonzero(m)
        out[name] = (int(mx.min()), int(my.min()), int(mx.max()), int(my.max()))
    return out


def sentence(path):
    m = measure(path)
    W, H = m['size']
    x0, y0, x1, y1 = m['bbox']
    parts = [f"Edit Image 1 in place. Treat its {W}x{H} transparent canvas and camera as fixed. "
             f"The figure's full bounding box is x={x0}..{x1}, y={y0}..{y1} px; keep the same "
             f"empty transparent margins (left {x0}, right {W - 1 - x1}, top {y0}, bottom {H - 1 - y1} px)."]
    if m['palette'].anchor_kind == 'achromatic':
        # a view where the three white anchors are separate blobs: head, left hand, left foot (top to bottom)
        if 'head_white' in m:
            hx0, hy0, hx1, hy1 = m['head_white']
            parts.append(f"Keep the white head at x={hx0}..{hx1}, top of head at y={hy0}.")
        if 'left_hand_white' in m:
            px0, py0, px1, py1 = m['left_hand_white']
            parts.append(f"Keep the white anatomical-left hand at x={px0}..{px1}, y={py0}..{py1}.")
        if 'left_foot_white' in m:
            fx0, fy0, fx1, fy1 = m['left_foot_white']
            parts.append(f"Keep the white anatomical-left foot at x={fx0}..{fx1}, sole at y={fy1}.")
        generic = [k for k in m if k.startswith('anchor') and k.endswith('_white')]
        for k in generic:   # fewer/more than three blobs (touching anchors, hat-head): positions only
            gx0, gy0, gx1, gy1 = m[k]
            parts.append(f"Keep the white bare anchor area (head, hand or foot, top to bottom blob {k[6:-6]}) at "
                         f"x={gx0}..{gx1}, y={gy0}..{gy1}.")
    else:
        if 'head_yellow' in m:
            hx0, hy0, hx1, hy1 = m['head_yellow']
            parts.append(f"Keep the yellow head at x={hx0}..{hx1}, top of head at y={hy0}.")
        if 'hand_magenta' in m:
            px0, py0, px1, py1 = m['hand_magenta']
            parts.append(f"Keep the magenta anatomical-left hand at x={px0}..{px1}, y={py0}..{py1}.")
        if 'foot_cyan' in m:
            fx0, fy0, fx1, fy1 = m['foot_cyan']
            parts.append(f"Keep the cyan anatomical-left foot at x={fx0}..{fx1}, sole at y={fy1}.")
    parts.append("Preserve T-pose, limb lengths and scale. Do not zoom, recenter, crop or extend the figure; "
                 "the outfit is added on top of the unchanged figure.")
    return ' '.join(parts)


if __name__ == '__main__':
    for p in sys.argv[1:]:
        print(f"# {p}\n{sentence(p)}\n")
