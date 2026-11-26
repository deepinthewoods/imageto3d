"""Click-time gate for paid provider actions (phase 5 review finding 2; DESIGN_PHASE4.md section I.8).

A caller that spawns a paid-capable child (convert_job.py -> rodin_backend.py / studio.mjs, the v2 monster lane -> the monster
Hyper3D wrappers) checks its gate before spawning, but the child can wait minutes for the browser profile or a provider stage before
it clicks. So the CHILD re-checks the gate immediately before every credit-capable intent and click:

    import paid_gate
    reason = paid_gate.closed_reason()      # None = open (or no gate configured: a manual run)
    if reason:
        raise paid_gate.GateClosed(reason)  # record left unchanged; exit EXIT_GATE_CLOSED; the caller waits/requeues and resumes

The caller describes the gate in the environment variable IMAGETO3D_PAID_GATE (JSON; `env_for` builds it):
    {"root": "<imageto3d root>",          # whose .codex_jobs/_batch/{CONVERT_HOLD,CONVERT_DRAIN,PAUSE,supervisor_pause.json,
                                          # supervisor_mode.json} apply
     "override_pause": false,             # true only for a user-confirmed Run now while paused: the pauses (PAUSE, the supervisor's
                                          # own pause, a manual pause mode) are ignored, CONVERT_HOLD / CONVERT_DRAIN never
     "cmd": ["python", "...", "..."]}     # optional extra check (the monster lane's mode/idle gate): exit 0 = open, else closed and
                                          # its first stdout line is the reason (a command that fails to run counts as closed)
No IMAGETO3D_PAID_GATE (someone running a wrapper by hand with --allow-paid) = no gate: the explicit flag is the authority.
A malformed IMAGETO3D_PAID_GATE counts as closed.

Non-python children (tools/tripo_pipeline/studio.mjs before its Generate click) ask through the command line:
    python tools/paid_gate.py check      exit 0 = open (prints "open"), exit EXIT_GATE_CLOSED = closed (first stdout line: the reason)
    python tools/paid_gate.py hold --root R [--wait S] [--what TEXT]
                                         take the gate transition LOCK of R for the caller: prints "held" once it has it and keeps
                                         it until its stdin closes (the caller is done, or died), then exits 0; exit EXIT_LOCK_BUSY
                                         (prints "busy") when it could not be taken within S seconds. node holds the lock this way
                                         (tripo_pipeline/paid_click.mjs takeGateMutex), so it never depends on a lock expiring.
                                         While it holds, each stdin line "ping <nonce>" is answered on stdout with "held <nonce>"
                                         after the helper RE-CHECKED its own lock handle (transition_lock.verify), or with
                                         "lost <nonce>" (it then releases and exits 1). node asks this immediately before its
                                         dispatch, after the intent write (final-live review, earlier blocker 2).

The pauses (post-cut-over review, blockers 1 and 3; DESIGN_PHASE4.md I.11). Three INDEPENDENT things pause conversions, each closes
the gate by itself and none is overridden by anything but `override_pause`:
    PAUSE                  the manual / maintenance pause file (and, for older readers, the supervisor's mirror of its own pause,
                           marked `"written_by": "supervisor"`; the supervisor never overwrites or removes a PAUSE it did not write)
    supervisor_pause.json  the supervisor's OWN pause (the idle rule, or its reflection of the mode), whoever owns PAUSE
    supervisor_mode.json   mode `pause` / `pause-conversions`: read HERE, at click time and under the dispatch lock, so a Queue
                           Pause stops payments as soon as the mode file is written, not at the supervisor's next tick
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ENV = 'IMAGETO3D_PAID_GATE'
EXIT_GATE_CLOSED = 4          # exit code of a paid-capable child that stopped at a closed gate (resumable, nothing clicked)
EXIT_LOCK_BUSY = 3            # `hold`: the gate transition lock stayed busy
PAUSE_MODES = ('pause', 'pause-conversions')   # supervisor_mode.json modes that pause conversions by hand (= v2 modes.PAUSE_MODES)
SUPERVISOR_PAUSE = 'supervisor_pause.json'     # the supervisor's own pause (pipeline_supervisor.py writes and removes it)
MODE_FILE = 'supervisor_mode.json'
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


def _pause_file_reason(path: Path, label: str) -> str | None:
    """Why the pause file `path` ({reason, scope}) pauses conversions, else None. One without a readable scope covers conversions."""
    if not path.exists():
        return None
    try:
        data = _read_json(path)
    except FileNotFoundError:
        return None
    scope = data.get('scope') if isinstance(data, dict) else None
    if not isinstance(scope, list) or 'conversions' in scope:
        reason = data.get('reason') if isinstance(data, dict) else None
        return f'conversions are paused ({label}: {reason or "no reason"})'
    return None


def mode_reason(batch) -> str | None:
    """Why the manual mode of `batch`/supervisor_mode.json pauses conversions (mode `pause` / `pause-conversions`), else None
    (post-cut-over review, blocker 3: the mode file itself closes the gate, the supervisor's PAUSE reflection is not waited for).
    A missing file is the default mode (not a pause); one that exists but cannot be read is treated as a pause: fail closed."""
    path = Path(batch) / MODE_FILE
    try:
        data = _read_json(path)
    except FileNotFoundError:
        return None
    if data is None:
        return f'conversions are paused ({MODE_FILE} cannot be read)'
    mode = data.get('mode') if isinstance(data, dict) else None
    if mode in PAUSE_MODES:
        return f'conversions are paused (mode {mode})'
    return None


def supervisor_pause_reason(batch) -> str | None:
    """Why the supervisor's OWN pause (supervisor_pause.json: the idle rule or its reflection of the mode) pauses conversions, else
    None. It exists beside PAUSE because the supervisor leaves a PAUSE it did not write alone (blocker 1)."""
    return _pause_file_reason(Path(batch) / SUPERVISOR_PAUSE, 'supervisor')


def file_reason(root, override_pause: bool = False) -> str | None:
    """Why the CONVERT_HOLD / CONVERT_DRAIN / PAUSE / supervisor_pause.json / supervisor_mode.json files of `root` close the gate,
    else None (hold and drain are never overridable). A pause file without a readable scope covers conversions."""
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
        return (_pause_file_reason(batch / 'PAUSE', 'PAUSE') or supervisor_pause_reason(batch) or mode_reason(batch))
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
# through. So the final check + dispatch of every paid click, and every WRITE of CONVERT_HOLD, CONVERT_DRAIN, PAUSE,
# supervisor_pause.json and supervisor_mode.json, run under one cross-process lock of the root.
#
# Post-cut-over review, blocker 2: that lock used to be a directory (`gate_mutex/`, mkdir) that was reclaimed after 30 s, so a
# second holder got in while a stalled first one was still inside its critical section. It is now an OS BYTE LOCK on
# `.codex_jobs/_batch/gate.lock` (msvcrt.locking on Windows, fcntl.flock elsewhere; same convention as tripo_pipeline/claim_lock.py):
#   - it NEVER expires: while its owner lives, nobody else gets it, however long the owner stalls;
#   - the kernel releases it when the owner exits or dies (the file itself stays and is never deleted);
#   - two handles exclude each other also inside one process (two threads).
# A holder that cannot get it within `wait` gets GateBusy: a clicker treats that as a closed gate (nothing clicked), a writer
# retries and, if it still cannot, reports that it did NOT write (a transition that is not serialized with a click in flight is
# never acknowledged). node cannot take a byte lock, so it keeps a `paid_gate.py hold` helper process alive for its critical
# section (hold_main below). `gate.lock.owner.json` beside it names the current holder; it is information only (`lock_owner`).
LOCK_FILE = 'gate.lock'
LOCK_OWNER_FILE = 'gate.lock.owner.json'
LOCK_OFFSET = 1 << 20          # the locked byte (= claim_lock.LOCK_OFFSET)
LOCK_POLL = 0.01

if os.name == 'nt':
    import msvcrt
else:
    import fcntl


class GateBusy(Exception):
    """The gate transition lock could not be taken in time."""


def _lock_path(root) -> Path:
    return Path(root) / '.codex_jobs' / '_batch' / LOCK_FILE


def _try_lock(handle) -> bool:
    if os.name == 'nt':
        handle.seek(LOCK_OFFSET)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        finally:
            handle.seek(0)
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle) -> None:
    if os.name == 'nt':
        handle.seek(LOCK_OFFSET)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.seek(0)
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)


def lock_busy(root) -> bool:
    """Somebody (any process, or another handle of this one) holds the gate transition lock of `root` right now. A probe: it takes
    and releases the lock when it is free."""
    path = _lock_path(root)
    if not path.exists():
        return False
    with open(path, 'a+b') as handle:
        if not _try_lock(handle):
            return True
        _unlock(handle)
    return False


def lock_owner(root) -> dict | None:
    """{pid, time, what} of the current holder of the gate transition lock of `root`, None when nobody holds it (information for
    messages and tests; {} when it is held but the holder's note cannot be read)."""
    if not lock_busy(root):
        return None
    try:
        owner = json.loads(_lock_path(root).with_name(LOCK_OWNER_FILE).read_text(encoding='utf-8'))
    except Exception:   # noqa: BLE001
        return {}
    return owner if isinstance(owner, dict) else {}


class transition_lock:
    """`with paid_gate.transition_lock(root, what='...'):` hold the gate transition lock of `root` (see above): an OS byte lock that
    excludes every other holder until this one leaves the block or its process ends; GateBusy when it stays busy for `wait` s."""

    def __init__(self, root, wait: float = 10.0, what: str = ''):
        self.path, self.wait, self.what, self.held = _lock_path(root), wait, what, False
        self.handle = None

    def owned(self) -> bool:
        """This holder has the lock. (An OS lock cannot be taken away from a live holder; kept for the callers of the old mutex.)"""
        return self.held

    def verify(self) -> bool:
        """Re-check the lock itself, not this object's memory of it (final-live review, earlier blocker 2; the `hold` helper
        answers node's ping with it): this holder's handle is still open AND the lock byte is still locked, i.e. a second handle
        cannot take it (handles exclude each other inside one process too). False when anything about that cannot be shown."""
        handle = self.handle
        if not self.held or handle is None or handle.closed:
            return False
        try:
            os.fstat(handle.fileno())
            with open(self.path, 'a+b') as probe:
                if _try_lock(probe):          # free: this holder does not have it (any more)
                    _unlock(probe)
                    return False
        except (OSError, ValueError):
            return False
        return True

    def __enter__(self):
        import time
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, 'a+b')
        try:
            deadline = time.monotonic() + self.wait
            while not _try_lock(handle):
                if time.monotonic() >= deadline:
                    raise GateBusy(f'{self.path}: the paid-gate transition lock is busy')
                time.sleep(LOCK_POLL)
        except BaseException:
            handle.close()
            raise
        self.handle, self.held = handle, True
        try:   # information only: who holds it (written by the holder alone, so never torn by two writers)
            self.path.with_name(LOCK_OWNER_FILE).write_text(
                json.dumps({'pid': os.getpid(), 'time': time.time(), 'what': self.what}), encoding='utf-8')
        except OSError:
            pass
        return self

    def __exit__(self, *exc):
        handle, self.handle, self.held = self.handle, None, False
        if handle is None:
            return
        try:
            _unlock(handle)
        except (OSError, ValueError):
            pass          # closing the handle releases it as well (ValueError: it was closed already)
        finally:
            handle.close()


def checked_dispatch(dispatch, environ=None, root=None, wait: float = 2.0):
    """Run `dispatch()` (the paid click) only while the gate is open, with the final check and the dispatch under the transition
    lock, which is held until `dispatch()` has returned however long that takes (it never expires; blocker 2). Raises GateClosed
    (closed, or the lock was busy) without calling it. No gate configured and no root = no lock."""
    spec = spec_from_env(environ)
    lock_root = root or (spec.get('root') if isinstance(spec, dict) and 'malformed' not in spec else None)
    if lock_root is None:
        check(environ)
        return dispatch()
    try:
        with transition_lock(lock_root, wait=wait, what='paid click') as lock:
            check(environ)
            if not lock.owned():   # cannot happen with the OS lock; kept as the last look before the click
                raise GateClosed('the paid-gate transition lock is not held; nothing was clicked')
            return dispatch()
    except GateBusy as exc:
        raise GateClosed(f'gate transition in progress ({exc})') from exc


def hold_main(args) -> int:
    """`hold --root R [--wait S] [--what TEXT]`: take the gate transition lock for the calling process (node), say "held", keep it
    until stdin closes (the caller finished its critical section, or died), release it and exit 0. "busy" + EXIT_LOCK_BUSY when it
    could not be taken. If THIS process dies the kernel releases the lock; the caller notices that the helper is gone (`owned()` in
    paid_click.mjs) before it dispatches. A stdin line `ping <nonce>` is answered with `held <nonce>` when the lock handle was
    re-checked and is still held (transition_lock.verify), else with `lost <nonce>` and exit 1: node's last look before its
    dispatch (`verify()` in paid_click.mjs). Any other line is ignored."""
    import argparse
    parser = argparse.ArgumentParser(prog='paid_gate.py hold')
    parser.add_argument('--root')
    parser.add_argument('--wait', type=float, default=2.0)
    parser.add_argument('--what', default='paid click')
    opts = parser.parse_args(args)
    root = opts.root
    if not root:
        spec = spec_from_env()
        root = spec.get('root') if isinstance(spec, dict) and 'malformed' not in spec else None
    if not root:
        print('no root', flush=True)
        return 2
    try:
        with transition_lock(root, wait=opts.wait, what=opts.what) as lock:
            print('held', flush=True)
            try:
                while True:
                    line = sys.stdin.buffer.readline()   # until EOF: the caller closed its end, or is gone
                    if not line:
                        break
                    words = line.split()
                    if words[:1] != [b'ping']:
                        continue
                    nonce = words[1].decode('ascii', 'replace') if len(words) > 1 else ''
                    held = lock.verify()
                    print(f"{'held' if held else 'lost'} {nonce}".rstrip(), flush=True)
                    if not held:
                        return 1
            except (OSError, ValueError):
                pass
    except GateBusy:
        print('busy', flush=True)
        return EXIT_LOCK_BUSY
    return 0


def main(argv=None) -> int:
    """`check`: exit 0 when a paid click may happen now, EXIT_GATE_CLOSED (reason on the first stdout line) when not.
    `hold ...`: see hold_main."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ['hold']:
        return hold_main(args[1:])
    if args != ['check']:
        print('usage: paid_gate.py check | hold --root R [--wait S] [--what TEXT]', file=sys.stderr)
        return 2
    reason = closed_reason()
    print(reason or 'open', flush=True)
    return EXIT_GATE_CLOSED if reason else 0


if __name__ == '__main__':
    raise SystemExit(main())
