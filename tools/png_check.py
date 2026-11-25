"""One technical validator for generated view images and their provenance records (recheck-before-cutover finding 15).

The view runner (tools/view_batch.py, task `validate: "rgba_png"` / `provenance`) and the preview app (tools/preview/v2/monsters_v2.py:
convert / freeze / retry) must agree on what counts as a usable view, so both call this module:

    png_check.png_problem(path)          -> None | reason     decodable PNG, mode RGBA, >= MIN_SHARE fully transparent AND solid pixels
    png_check.provenance_problem(path)   -> None | reason     exists and parses as a JSON OBJECT
    png_check.view_problems(png, provenance=None) -> [(path, reason)]
    png_check.provenance_only(problems, provenance_paths) -> bool   only the record is wrong (see the rule below)

Terminal-state rule (both sides apply it; `provenance_only(problems)` is the test):
- an invalid IMAGE is never usable: it is set aside (`<name>.invalid-<time>`) and generated again;
- a VALID image whose provenance is missing or not a JSON object is NOT regenerated. Only the unusable provenance file is set
  aside, the image stays, and the attempt ends `failed` with reason `provenance` (view_batch status `reason: "provenance"`). The
  record is then repaired on its own (the app's 'repair provenance' writes a JSON object); once `view_problems` is empty again the
  view is usable (the runner's next scan marks the task `done`).

These are TECHNICAL checks only (no framing, pose or colour judgement: monster views are never redrawn for those, user 2026-11-25).
Pillow is required; without it every image fails closed ("cannot validate").
"""
from __future__ import annotations

import json
from pathlib import Path

MIN_SHARE = 0.01
SOLID_ALPHA = 250   # 'solid' = alpha >= 250: real Codex views draw their subject at alpha ~250-254, rarely exactly 255
                    # (measured 2026-11-25 on 70 real monster images: >= 17% solid, >= 39% fully transparent each)


def png_problem(path) -> str | None:
    """None when `path` is a decodable RGBA PNG with at least MIN_SHARE fully transparent (alpha 0) and MIN_SHARE solid
    (alpha >= SOLID_ALPHA) pixels: an invisible or background-only image fails, a normal cut-out passes."""
    try:
        from PIL import Image
    except ImportError:   # fail closed: an image that cannot be checked does not count as usable
        return "cannot validate: Pillow is not installed for this python"
    try:
        with Image.open(path) as image:
            image.load()
            fmt, mode, size = image.format, image.mode, image.size
            histogram = image.getchannel('A').histogram() if mode == 'RGBA' else None
    except FileNotFoundError:
        return 'missing'
    except Exception as error:   # noqa: BLE001  truncated / not an image / decoder error
        return f'does not decode ({type(error).__name__}: {str(error)[:120]})'
    if fmt != 'PNG':
        return f'not a PNG ({fmt})'
    if mode != 'RGBA':
        return f'mode {mode}, not RGBA'
    total = size[0] * size[1]
    if not total:
        return 'empty image'
    clear, opaque = histogram[0] / total, sum(histogram[SOLID_ALPHA:]) / total
    if clear < MIN_SHARE:
        return f'only {clear:.2%} fully transparent pixels (at least {MIN_SHARE:.0%} needed)'
    if opaque < MIN_SHARE:
        return f'only {opaque:.2%} solid pixels (alpha >= {SOLID_ALPHA}; at least {MIN_SHARE:.0%} needed)'
    return None


def provenance_problem(path) -> str | None:
    """None when `path` exists and holds a JSON object."""
    path = Path(path)
    if not path.is_file():
        return 'provenance missing'
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        return f'provenance is not readable JSON ({type(error).__name__})'
    if not isinstance(data, dict):
        return 'provenance is not a JSON object'
    return None


def provenance_only(problems, provenance_paths) -> bool:
    """True when `problems` ([(path, reason)]) is non-empty and every entry is one of `provenance_paths`: the images are usable
    and only the record needs repair (the image is not regenerated)."""
    records = {Path(p) for p in provenance_paths}
    return bool(problems) and all(path is not None and Path(path) in records for path, _ in problems)


def view_problems(png, provenance=None) -> list[tuple[Path, str]]:
    """[(path, reason)] for one view image and (optionally) its provenance record; [] = usable."""
    out = []
    reason = png_problem(png)
    if reason:
        out.append((Path(png), reason))
    if provenance is not None:
        reason = provenance_problem(provenance)
        if reason:
            out.append((Path(provenance), reason))
    return out
