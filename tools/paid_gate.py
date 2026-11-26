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


# ---------------------------------------------------------------------------------------------------- transition lock
# Last-check review row 6: a hold / drain / pause written between a click's final gate check and its dispatch still let one click
# through. So the final check + dispatch of every paid click, and every WRITE of CONVERT_HOLD, CONVERT_DRAIN, PAUSE and
# supervisor_mode.json by current code, run under one short mutex: the directory `.codex_jobs/_batch/gate_mutex/` (mkdir is atomic
# on every filesystem, so python and node take it the same way; `owner.json` inside names the holder). Critical sections are
# milliseconds; a mutex whose owner process is gone, or older than MUTEX_STALE seconds, is reclaimed (renamed aside, never deleted
# while it might be live). A holder that cannot get it within `wait` gets GateBusy: a clicker treats that as a closed gate, a
# writer retries. Older code (the pre-cut-over supervisor) writes PAUSE without it; that is the documented pre-cut-over residual.
# `owner.json` carries a random token: a holder releases only a mutex that is still its own (one reclaimed from it as stale and
# taken by someone else is left alone), and a clicker looks that it still owns the mutex right before it dispatches (`owned()`;
# a gate check that outlasted MUTEX_STALE therefore ends as a closed gate, never as an unserialized click).
MUTEX_DIR = 'gate_mutex'
MUTEX_STALE = 30.0
OWNERLESS_STALE = 2.0


class GateBusy(Exception):
    """The gate transition mutex could not be taken in time."""


def _mutex_path(root) -> Path:
    return Path(root) / '.codex_jobs' / '_batch' / MUTEX_DIR


def _pid_alive(pid) -> bool:
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:   # noqa: BLE001  no psutil / bad pid: assume alive (only the age rule then reclaims)
        return True


def _reclaim_if_stale(path: Path) -> bool:
    """Rename a stale mutex aside (owner gone, or older than MUTEX_STALE); True when it was reclaimed."""
    import time
    try:
        owner = json.loads((path / 'owner.json').read_text(encoding='utf-8'))
    except Exception:   # noqa: BLE001  owner not written yet / unreadable: judge by the directory's age only
        owner = {}
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return True
    gone = isinstance(owner, dict) and owner.get('pid') is not None and not _pid_alive(owner['pid'])
    ownerless = not (isinstance(owner, dict) and owner.get('pid') is not None)
    # owner.json is written right after the mkdir, so a mutex still without an owner after OWNERLESS_STALE seconds was abandoned
    # (a holder whose release could not remove the folder, or one that died between mkdir and the owner write)
    if not gone and age < (OWNERLESS_STALE if ownerless else MUTEX_STALE):
        return False
    try:
        path.rename(path.with_name(f'{MUTEX_DIR}.stale-{int(time.time() * 1000)}-{os.getpid()}'))
    except OSError:
        return False
    return True


class transition_lock:
    """`with paid_gate.transition_lock(root, what='...'):` hold the gate transition mutex of `root` (see above)."""

    def __init__(self, root, wait: float = 10.0, what: str = ''):
        self.path, self.wait, self.what, self.held = _mutex_path(root), wait, what, False
        self.token, self.wrote = os.urandom(8).hex(), False

    def owned(self) -> bool:
        """This holder still has the mutex: it was not reclaimed as stale (and possibly taken by another process) meanwhile."""
        if not self.held:
            return False
        try:
            owner = json.loads((self.path / 'owner.json').read_text(encoding='utf-8'))
        except Exception:   # noqa: BLE001  gone / unreadable: ours only when we never managed to write it and the folder is there
            return not self.wrote and self.path.is_dir()
        return isinstance(owner, dict) and owner.get('token') == self.token

    def __enter__(self):
        import time
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.wait
        spins = 0
        while True:
            try:
                os.mkdir(self.path)
                break
            except (FileExistsError, PermissionError):   # PermissionError: Windows, while the folder is being removed or renamed
                if _reclaim_if_stale(self.path) and spins < 3:   # out of the way: try at once (bounded, then the timed path)
                    spins += 1
                    continue
                if time.monotonic() >= deadline:
                    raise GateBusy(f'{self.path}: the paid-gate transition mutex is busy')
                time.sleep(0.01)
        self.held = True
        try:
            (self.path / 'owner.json').write_text(json.dumps({'pid': os.getpid(), 'time': time.time(), 'what': self.what,
                                                              'token': self.token}), encoding='utf-8')
            self.wrote = True
        except OSError:
            pass
        return self

    def __exit__(self, *exc):
        if not self.held:
            return
        mine = self.owned()
        self.held = False
        if not mine:          # reclaimed as stale meanwhile: whatever is at the path now belongs to another holder
            return
        # Release = rename the folder aside first (one atomic step; Windows can refuse a remove for a moment while an indexer or a
        # contender's mkdir touches it), then tidy the renamed copy. Retried briefly; whatever is left over is ownerless and is
        # reclaimed after OWNERLESS_STALE seconds by the next contender, never held for MUTEX_STALE.
        import time
        aside = self.path.with_name(f'{MUTEX_DIR}.released-{self.token}')
        for attempt in range(20):
            try:
                os.rename(self.path, aside)
                break
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.01 * (attempt + 1))
        else:
            aside = self.path
        for attempt in range(20):
            try:
                (aside / 'owner.json').unlink(missing_ok=True)
                os.rmdir(aside)
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.01 * (attempt + 1))


def checked_dispatch(dispatch, environ=None, root=None, wait: float = 2.0):
    """Run `dispatch()` (the paid click) only while the gate is open, with the final check and the dispatch under the transition
    mutex. Raises GateClosed (closed, or the mutex was busy) without calling it. No gate configured and no root = no mutex."""
    spec = spec_from_env(environ)
    lock_root = root or (spec.get('root') if isinstance(spec, dict) and 'malformed' not in spec else None)
    if lock_root is None:
        check(environ)
        return dispatch()
    try:
        with transition_lock(lock_root, wait=wait, what='paid click') as lock:
            check(environ)
            if not lock.owned():   # the check outlasted MUTEX_STALE and the mutex was reclaimed: no longer serialized
                raise GateClosed('the paid-gate transition mutex was lost during the gate check; nothing was clicked')
            return dispatch()
    except GateBusy as exc:
        raise GateClosed(f'gate transition in progress ({exc})') from exc


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
