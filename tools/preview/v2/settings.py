"""App settings of the v2 preview server: `.codex_jobs/_app/settings.json` (DESIGN_PHASE4.md section A).

    settings.load(root) -> dict      every key present; a missing file, a missing key or an invalid value takes its default
    settings.save(root, patch) -> dict   validate a partial update, merge it onto the file, write atomically, return load(root)
    settings.validate(patch) -> dict     the cleaned patch; raises SettingsError listing every problem

`load` is the ONLY reader of the file (DESIGN_PHASE4.md H): `allowed_hosts` for server.py, `layer_workers_present` / `layer_workers_away` /
`build_stall_minutes` for orchestrator.py (`read_settings`) and builds.py (`stall_minutes`), the rest for services.py / credits.py. Keys this
module does not know are kept in the file by `save` but are not returned by `load` and are refused in a patch.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sys
import threading

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import guard  # noqa: E402

SERVICE_NAMES = ('supervisor', 'views', 'convert')
DEFAULTS = {
    'services': {'supervisor': True, 'views': True, 'convert': True},
    'view_parallel': 8,
    'tripo': 'off',
    'layer_workers_present': 1,
    'layer_workers_away': 4,
    'build_stall_minutes': 30,
    'allowed_hosts': [],
    'paid_budget_hyper3d': 0,
}
INT_RANGES = {'view_parallel': (1, 16), 'layer_workers_present': (0, 16), 'layer_workers_away': (0, 16),
              'build_stall_minutes': (1, 1440), 'paid_budget_hyper3d': (0, 1000)}
TRIPO_MODES = ('off', 'auto')
HOST_RE = re.compile(r'^[A-Za-z0-9._:\[\]-]{1,253}$')
MAX_HOSTS = 32
_LOCK = threading.Lock()


class SettingsError(ValueError):
    """A settings patch that is not valid; `.problems` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__('; '.join(problems))
        self.problems = problems


def path(root) -> Path:
    return Path(root) / guard.APP / 'settings.json'


def defaults() -> dict:
    return copy.deepcopy(DEFAULTS)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _problem(key: str, value) -> str | None:
    """Why `value` is not acceptable for the top-level `key` (None = fine)."""
    if key == 'services':
        if not isinstance(value, dict):
            return 'services must be an object {supervisor, views, convert: true|false}'
        unknown = sorted(set(value) - set(SERVICE_NAMES))
        if unknown:
            return f'services: unknown service(s) {unknown}'
        bad = sorted(k for k, v in value.items() if not isinstance(v, bool))
        return f'services.{", ".join(bad)} must be true or false' if bad else None
    if key in INT_RANGES:
        low, high = INT_RANGES[key]
        if not _is_int(value) or not low <= value <= high:
            return f'{key} must be an integer from {low} to {high}'
        return None
    if key == 'tripo':
        return None if value in TRIPO_MODES else f'tripo must be one of {list(TRIPO_MODES)}'
    if key == 'allowed_hosts':
        if not isinstance(value, list) or len(value) > MAX_HOSTS:
            return f'allowed_hosts must be a list of at most {MAX_HOSTS} host names'
        bad = [h for h in value if not isinstance(h, str) or not HOST_RE.match(h)]
        return f'allowed_hosts: invalid host name(s) {bad[:3]}' if bad else None
    return f'unknown setting {key!r}'


def validate(patch) -> dict:
    """The cleaned partial update (`services` may name only some services); SettingsError lists every problem."""
    if not isinstance(patch, dict):
        raise SettingsError(['settings must be a JSON object'])
    problems = [p for key, value in patch.items() if (p := _problem(key, value))]
    if problems:
        raise SettingsError(problems)
    clean = copy.deepcopy(patch)
    if 'allowed_hosts' in clean:
        seen: list[str] = []
        for host in clean['allowed_hosts']:
            if host not in seen:
                seen.append(host)
        clean['allowed_hosts'] = seen
    return clean


def _read_raw(root) -> dict:
    try:
        raw = json.loads(path(root).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def normalise(raw) -> dict:
    """`raw` (whatever the file held) on top of the defaults; every invalid value falls back to its default."""
    out = defaults()
    if not isinstance(raw, dict):
        return out
    for key in DEFAULTS:
        if key not in raw:
            continue
        value = raw[key]
        if key == 'services':
            if isinstance(value, dict):
                for name in SERVICE_NAMES:
                    if isinstance(value.get(name), bool):
                        out['services'][name] = value[name]
        elif _problem(key, value) is None:
            out[key] = copy.deepcopy(value)
    return out


def load(root) -> dict:
    """The settings of `root` with every default filled in (never raises)."""
    return normalise(_read_raw(root))


def save(root, patch) -> dict:
    """Validate `patch`, merge it into the file (unknown keys of the file are kept), write atomically; returns `load(root)`."""
    clean = validate(patch)
    with _LOCK:
        raw = _read_raw(root)
        for key, value in clean.items():
            if key == 'services':
                merged = dict(raw.get('services') if isinstance(raw.get('services'), dict) else {})
                merged.update(value)
                raw['services'] = merged
            else:
                raw[key] = value
        guard.write_atomic(path(root), (json.dumps(raw, indent=2) + '\n').encode('utf-8'))
    return load(root)
