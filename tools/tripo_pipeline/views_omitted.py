"""Three-view modules: the `views_omitted.json` marker of a module folder.

A module folder (`<job>/<race>/<module>/`) normally holds the four views front, front_three_quarter, back and
back_three_quarter. When one non-front view cannot be generated (repeated safety-filter refusals), the folder may
carry a marker instead, and the module is converted from the three views that exist:

    {"omitted": ["back"], "reason": "...", "date": "2026-10-24"}

Two-view modules (user rule 2026-10-25, gnome bases): the marker may instead omit BOTH three-quarter views,
`{"omitted": ["front_three_quarter", "back_three_quarter"], ...}`, and the module is converted from front + back.

Rules (fail closed): `front` is always required and cannot be omitted; either exactly one of the other three views is
omitted, or exactly the two three-quarter views; an omitted view's PNG must not exist. Stdlib only, shared by workflow.py, convert_job.py and the
Hyper3D / formy3d backends (studio.mjs mirrors the same rules in JavaScript).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

MARKER = 'views_omitted.json'
VIEWS = ('front', 'front_three_quarter', 'back', 'back_three_quarter')
OPTIONAL = VIEWS[1:]
TWO_VIEW_OMITTED = ('front_three_quarter', 'back_three_quarter')   # two-view (front + back) modules


def parse(data, module_dir: Path | None = None) -> list[str]:
    """Validate marker `data`; returns the omitted view names (no .png). With `module_dir`, also rejects a marker
    that names a view whose PNG exists there."""
    if not isinstance(data, dict) or not isinstance(data.get('omitted'), list):
        raise ValueError(f'{MARKER} needs {{"omitted": ["<view>"], "reason": ..., "date": ...}}')
    names = [n[:-4] if isinstance(n, str) and n.endswith('.png') else n for n in data['omitted']]
    if any(not isinstance(n, str) for n in names):
        raise ValueError(f'{MARKER}: omitted entries must be view names')
    if 'front' in names:
        raise ValueError(f'{MARKER}: front is always required and cannot be omitted')
    unknown = [n for n in names if n not in VIEWS]
    if unknown:
        raise ValueError(f'{MARKER}: unknown view {unknown}; views are {list(OPTIONAL)}')
    if len(names) != 1 and sorted(names) != sorted(TWO_VIEW_OMITTED):
        raise ValueError(f'{MARKER}: omit exactly one non-front view, or exactly both three-quarter views; got {names}')
    for name in names:
        if module_dir is not None and (Path(module_dir) / (name + '.png')).exists():
            raise ValueError(f'{MARKER} omits {name} but {Path(module_dir) / (name + ".png")} exists')
    return names


def load(module_dir) -> dict | None:
    """The validated marker of `module_dir` as {omitted: [name], reason, date, sha256}, or None when there is none."""
    path = Path(module_dir) / MARKER
    if not path.is_file():
        return None
    if path.is_symlink():
        raise ValueError(f'{path} must not be a symlink')
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except ValueError as exc:
        raise ValueError(f'{path} is not valid JSON: {exc}') from exc
    omitted = parse(data, Path(module_dir))
    return {'omitted': omitted, 'reason': data.get('reason'), 'date': data.get('date'),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def omitted_views(module_dir) -> list[str]:
    """Omitted view names of `module_dir` ([] without a marker); raises ValueError for an invalid marker."""
    marker = load(module_dir)
    return list(marker['omitted']) if marker else []
