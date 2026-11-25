"""Click-time gate for paid provider actions (phase 5 review finding 2; DESIGN_PHASE4.md section I.8).

A caller that spawns a paid-capable child (convert_job.py -> rodin_backend.py / studio.mjs, the v2 monster lane -> the monster
Hyper3D wrappers) checks its gate before spawning, but the child can wait minutes for the browser profile or a provider stage before
it clicks. So the CHILD re-checks the gate immediately before every credit-capable intent and click:

    import paid_gate
    reason = paid_gate.closed_reason()      # None = open (or no gate configured: a manual run)
    if reason:
        raise paid_gate.GateClosed(reason)  # record left unchanged; exit EXIT_GATE_CLOSED; the caller waits/requeues and resumes

The caller describes the gate in the environment variable IMAGETO3D_PAID_GATE (JSON; `env_for` builds it):
    {"root": "<imageto3d root>",          # whose .codex_jobs/_batch/{CONVERT_HOLD,PAUSE} apply
     "override_pause": false,             # true only for a user-confirmed Run now while paused: PAUSE is ignored, CONVERT_HOLD never
     "cmd": ["python", "...", "..."]}     # optional extra check (the monster lane's mode/idle gate): exit 0 = open, else closed and
                                          # its first stdout line is the reason (a command that fails to run counts as closed)
No IMAGETO3D_PAID_GATE (someone running a wrapper by hand with --allow-paid) = no gate: the explicit flag is the authority.
A malformed IMAGETO3D_PAID_GATE counts as closed.

Non-python children (tools/tripo_pipeline/studio.mjs before its Generate click) ask through the command line:
    python tools/paid_gate.py check      exit 0 = open (prints "open"), exit EXIT_GATE_CLOSED = closed (first stdout line: the reason)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ENV = 'IMAGETO3D_PAID_GATE'
EXIT_GATE_CLOSED = 4          # exit code of a paid-capable child that stopped at a closed gate (resumable, nothing clicked)
CMD_TIMEOUT = 60


class GateClosed(Exception):
    """A paid click was not made because the gate closed; the record is unchanged and the step can be resumed."""


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        raise
    except Exception:   # noqa: BLE001  unreadable = treat as present with unknown content
        return None


def file_reason(root, override_pause: bool = False) -> str | None:
    """Why the CONVERT_HOLD / CONVERT_DRAIN / PAUSE files of `root` close the gate, else None (hold and drain are never overridable).
    A PAUSE without a readable scope covers conversions."""
    batch = Path(root) / '.codex_jobs' / '_batch'
    hold = batch / 'CONVERT_HOLD'
    if hold.exists():
        try:
            data = _read_json(hold)
        except FileNotFoundError:
            data = {}
        reason = data.get('reason') if isinstance(data, dict) else None
        return f'conversions are held (CONVERT_HOLD: {reason or "no reason"})'
    if (batch / 'CONVERT_DRAIN').exists():   # a drain closes the gate by itself, never overridable (final-verify review, new 1)
        return 'conversions are being drained (CONVERT_DRAIN)'
    if not override_pause:
        pause = batch / 'PAUSE'
        if pause.exists():
            try:
                data = _read_json(pause)
            except FileNotFoundError:
                return None
            scope = data.get('scope') if isinstance(data, dict) else None
            if not isinstance(scope, list) or 'conversions' in scope:
                reason = data.get('reason') if isinstance(data, dict) else None
                return f'conversions are paused (PAUSE: {reason or "no reason"})'
    return None


def spec_from_env(environ=None) -> dict | None:
    """The gate description of this process, None when no gate is configured; {'malformed': text} when it cannot be used."""
    raw = (environ if environ is not None else os.environ).get(ENV)
    if not raw:
        return None
    try:
        spec = json.loads(raw)
    except ValueError:
        return {'malformed': f'{ENV} is not JSON'}
    if not isinstance(spec, dict) or not isinstance(spec.get('root'), str) or not spec['root']:
        return {'malformed': f'{ENV} has no root'}
    cmd = spec.get('cmd')
    if cmd is not None and (not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd)):
        return {'malformed': f'{ENV} cmd must be a list of strings'}
    return spec


def closed_reason(environ=None) -> str | None:
    """None when a paid click may happen now (or no gate is configured), else the reason it may not."""
    spec = spec_from_env(environ)
    if spec is None:
        return None
    if 'malformed' in spec:
        return spec['malformed']
    reason = file_reason(spec['root'], bool(spec.get('override_pause')))
    if reason:
        return reason
    cmd = spec.get('cmd')
    if cmd:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CMD_TIMEOUT, cwd=spec['root'])
        except Exception as exc:   # noqa: BLE001  a gate that cannot be asked is closed
            return f'gate check could not run ({type(exc).__name__}: {exc})'
        if proc.returncode != 0:
            first = (proc.stdout or proc.stderr or '').strip().splitlines()
            return first[0] if first else f'gate check said closed (exit {proc.returncode})'
    return None


def check(environ=None) -> None:
    """Raise GateClosed when the gate is closed."""
    reason = closed_reason(environ)
    if reason:
        raise GateClosed(reason)


def env_for(root, override_pause: bool = False, cmd: list[str] | None = None) -> dict:
    """{ENV: json} to add to a child's environment."""
    spec = {'root': str(root), 'override_pause': bool(override_pause)}
    if cmd:
        spec['cmd'] = [str(x) for x in cmd]
    return {ENV: json.dumps(spec)}


def main(argv=None) -> int:
    """`check`: exit 0 when a paid click may happen now, EXIT_GATE_CLOSED (reason on the first stdout line) when not."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ['check']:
        print('usage: paid_gate.py check', file=sys.stderr)
        return 2
    reason = closed_reason()
    print(reason or 'open', flush=True)
    return EXIT_GATE_CLOSED if reason else 0


if __name__ == '__main__':
    raise SystemExit(main())
