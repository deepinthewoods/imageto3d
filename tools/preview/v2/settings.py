"""App settings of the v2 preview server: `.codex_jobs/_app/settings.json` (DESIGN_PHASE4.md section A).

    settings.load(root) -> dict      every key present; a missing file or a missing key takes its default (read failures: see below)
    settings.health(root) -> dict    {error, problems, source, hold_starts}: what the last load could not use as it is
    settings.save(root, patch) -> dict   validate a partial update, merge it onto the file, write atomically, return load(root)
    settings.validate(patch) -> dict     the cleaned patch; raises SettingsError listing every problem

Read failures never change the policy (phase 4 review F16): a MISSING file means the first-install defaults, but an existing file that cannot
be read or does not parse as a JSON object keeps the LAST VALID policy this process loaded for the root; with none yet, `load` answers the
defaults for display and `health()["hold_starts"]` is true, so the service manager starts nothing until the file is readable. A single invalid
value takes its default, except a `services.<name>` flag: it falls back to the last valid flag or to False, so an invalid value never starts a
service. Each fallback is listed in `health()["problems"]`.

`load` is the ONLY reader of the file (DESIGN_PHASE4.md H): `allowed_hosts` for server.py, `layer_workers_present` / `layer_workers_away` /
`build_stall_minutes` for orchestrator.py (`read_settings`) and builds.py (`stall_minutes`), the rest for services.py / credits.py. Keys this
module does not know are kept in the file by `save` but are not returned by `load` and are refused in a patch.
"""
from __future__ import annotations

import copy
import json
import os
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
_LOCK = threading.RLock()


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


_LAST: dict[str, dict] = {}          # root -> the last valid policy this process loaded (phase 4 review F16)
_HEALTH: dict[str, dict] = {}


def _key(root) -> str:
    return os.path.normcase(str(Path(root).resolve()))


def _read(root) -> tuple[dict | None, str | None]:
    """(raw object, None); (None, None) when the file is missing; (None, error) when it exists but cannot be used."""
    try:
        text = path(root).read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f'settings.json cannot be read ({type(exc).__name__}: {exc})'
    try:
        raw = json.loads(text)
    except ValueError as exc:
        return None, f'settings.json is not valid JSON ({exc})'
    if not isinstance(raw, dict):
        return None, 'settings.json does not hold a JSON object'
    return raw, None


def _read_raw(root) -> dict:
    raw, _error = _read(root)
    return raw or {}


def _short(value) -> str:
    text = repr(value) if isinstance(value, str) else json.dumps(value)
    return text if len(text) <= 60 else text[:57] + '...'


def normalise(raw, fallback: dict | None = None, problems: list | None = None) -> dict:
    """`raw` (whatever the file held) on top of the defaults. An invalid value takes its default, except an invalid `services.<name>` flag:
    it falls back to `fallback`'s flag (the last valid policy) or to False, so it never starts a service. Every fallback is in `problems`."""
    out = defaults()
    problems = problems if problems is not None else []
    if not isinstance(raw, dict):
        return out
    for key in DEFAULTS:
        if key not in raw:
            continue
        value = raw[key]
        if key == 'services':
            flags = value if isinstance(value, dict) else {name: value if not isinstance(value, bool) else None for name in SERVICE_NAMES}
            for name in SERVICE_NAMES:
                if name not in flags:
                    continue
                if isinstance(flags[name], bool):
                    out['services'][name] = flags[name]
                else:
                    out['services'][name] = bool(fallback['services'][name]) if fallback else False
                    problems.append(f'services.{name} = {_short(flags[name])} is not true / false: '
                                    f'{"the last valid value" if fallback else "off"} is used')
        elif _problem(key, value) is None:
            out[key] = copy.deepcopy(value)
        else:
            problems.append(f'{key} = {_short(value)} is not valid: the default is used')
    return out


def load(root) -> dict:
    """The settings of `root` with every default filled in (never raises). A missing file = the defaults; an existing file that cannot be read
    or parsed = the last valid policy of this process (or, with none yet, the defaults for display while health() holds every service start)."""
    key = _key(root)
    raw, error = _read(root)
    with _LOCK:
        last = _LAST.get(key)
        if error is None:
            problems: list[str] = []
            loaded = normalise(raw if raw is not None else {}, last, problems)
            _LAST[key] = copy.deepcopy(loaded)
            _HEALTH[key] = {'error': None, 'problems': problems, 'source': 'file' if raw is not None else 'defaults', 'hold_starts': False}
            return loaded
        if last is not None:
            _HEALTH[key] = {'error': error, 'problems': [], 'source': 'last_valid', 'hold_starts': False}
            return copy.deepcopy(last)
        _HEALTH[key] = {'error': error, 'problems': [], 'source': 'none', 'hold_starts': True}
        return defaults()


def health(root, reload: bool = True) -> dict:
    """{error, problems, source: file | defaults | last_valid | none, hold_starts} of the settings file; `hold_starts` = no valid policy is
    known (an unreadable file and nothing loaded before): the service manager starts nothing. Reloads the file unless `reload` is False."""
    if reload:
        load(root)
    with _LOCK:
        return copy.deepcopy(_HEALTH.get(_key(root)) or {'error': None, 'problems': [], 'source': 'defaults', 'hold_starts': False})


def save(root, patch) -> dict:
    """Validate `patch`, merge it into the file (unknown keys of the file are kept), write atomically; returns `load(root)`. An existing file that
    cannot be read is rebuilt from the last valid policy (its unknown keys are lost then); with no valid policy known the patch is refused."""
    clean = validate(patch)
    with _LOCK:
        raw, error = _read(root)
        if error is not None:
            last = _LAST.get(_key(root))
            if last is None:
                raise SettingsError([f'{error}; fix or delete .codex_jobs/_app/settings.json first (nothing was written)'])
            raw = copy.deepcopy(last)
        raw = raw if raw is not None else {}
        for key, value in clean.items():
            if key == 'services':
                merged = dict(raw.get('services') if isinstance(raw.get('services'), dict) else {})
                merged.update(value)
                raw['services'] = merged
            else:
                raw[key] = value
        guard.write_atomic(path(root), (json.dumps(raw, indent=2) + '\n').encode('utf-8'))
    return load(root)
