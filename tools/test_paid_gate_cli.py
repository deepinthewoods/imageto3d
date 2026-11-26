"""tools/paid_gate.py: the click-time paid gate (DESIGN_PHASE4 I.8, phase 5 review finding 2). Temp roots only.

    python -X utf8 -m pytest tools/test_paid_gate_cli.py -q
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paid_gate  # noqa: E402

TOOL = Path(paid_gate.__file__).resolve()


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.batch = self.root / '.codex_jobs' / '_batch'
        self.batch.mkdir(parents=True)

    def env(self, **kw):
        return paid_gate.env_for(self.root, **kw)

    def test_no_variable_is_a_manual_run(self):
        self.assertIsNone(paid_gate.closed_reason({}))
        (self.batch / 'CONVERT_HOLD').write_text('{}')
        self.assertIsNone(paid_gate.closed_reason({}))
        paid_gate.check({})

    def test_hold_and_pause(self):
        env = self.env()
        self.assertIsNone(paid_gate.closed_reason(env))
        (self.batch / 'PAUSE').write_text(json.dumps({'reason': 'user active', 'scope': ['views']}))
        self.assertIsNone(paid_gate.closed_reason(env))                       # a views-only pause
        (self.batch / 'PAUSE').write_text(json.dumps({'reason': 'user active', 'scope': ['conversions', 'views']}))
        self.assertEqual(paid_gate.closed_reason(env), 'conversions are paused (PAUSE: user active)')
        self.assertIsNone(paid_gate.closed_reason(self.env(override_pause=True)))   # Run now while paused
        (self.batch / 'PAUSE').write_text('{broken')
        self.assertIn('paused', paid_gate.closed_reason(env))                 # unreadable PAUSE: closed
        (self.batch / 'CONVERT_HOLD').write_text('')
        for spec in (env, self.env(override_pause=True)):                     # the hold is never overridable
            self.assertEqual(paid_gate.closed_reason(spec), 'conversions are held (CONVERT_HOLD: no reason)')
            with self.assertRaises(paid_gate.GateClosed):
                paid_gate.check(spec)

    def test_malformed_variable_is_closed(self):
        for raw in ('not json', '[]', '{"root": ""}', json.dumps({'root': str(self.root), 'cmd': 'x'}),
                    json.dumps({'root': str(self.root), 'cmd': []})):
            self.assertIsNotNone(paid_gate.closed_reason({paid_gate.ENV: raw}), raw)

    def test_the_extra_command(self):
        open_cmd = [sys.executable, '-c', 'print("fine")']
        closed_cmd = [sys.executable, '-c', 'print("mode is pause (monster lane)"); raise SystemExit(1)']
        self.assertIsNone(paid_gate.closed_reason(paid_gate.env_for(self.root, cmd=open_cmd)))
        self.assertEqual(paid_gate.closed_reason(paid_gate.env_for(self.root, cmd=closed_cmd)), 'mode is pause (monster lane)')
        reason = paid_gate.closed_reason(paid_gate.env_for(self.root, cmd=[str(self.root / 'missing.exe')]))
        self.assertIn('could not run', reason)                                # a gate that cannot be asked is closed
        (self.batch / 'CONVERT_HOLD').write_text('{}')
        self.assertIn('held', paid_gate.closed_reason(paid_gate.env_for(self.root, cmd=open_cmd)))   # files first

    def test_cli_check(self):
        def check(extra):
            return subprocess.run([sys.executable, str(TOOL), 'check'], capture_output=True, text=True, timeout=60,
                                  env={**os.environ, **extra})
        run = check(self.env())
        self.assertEqual((run.returncode, run.stdout.strip()), (0, 'open'))
        (self.batch / 'CONVERT_HOLD').write_text(json.dumps({'reason': 'supervisor restart'}))
        run = check(self.env())
        self.assertEqual((run.returncode, run.stdout.strip()),
                         (paid_gate.EXIT_GATE_CLOSED, 'conversions are held (CONVERT_HOLD: supervisor restart)'))
        run = subprocess.run([sys.executable, str(TOOL)], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 2)

    def test_env_for_round_trip(self):
        spec = paid_gate.spec_from_env(paid_gate.env_for(self.root, override_pause=True, cmd=['a', 1]))
        self.assertEqual(spec, {'root': str(self.root), 'override_pause': True, 'cmd': ['a', '1']})


if __name__ == '__main__':
    unittest.main()


def test_a_drain_alone_closes_the_gate_even_with_a_pause_override(tmp_path):
    """Final-verify review, new finding 1: CONVERT_DRAIN without CONVERT_HOLD still closes the click-time gate."""
    import paid_gate as g
    batch = tmp_path / '.codex_jobs' / '_batch'
    batch.mkdir(parents=True)
    (batch / 'CONVERT_DRAIN').write_text('{}')
    env = g.env_for(tmp_path, override_pause=True)
    assert 'CONVERT_DRAIN' in (g.closed_reason(env) or '')
    (batch / 'CONVERT_DRAIN').unlink()
    assert g.closed_reason(env) is None


def _holder(root, *extra):
    """A separate process that takes the gate transition lock of `root` (`paid_gate.py hold`) and keeps it until its stdin closes."""
    proc = subprocess.Popen([sys.executable, '-X', 'utf8', str(TOOL), 'hold', '--root', str(root), *extra],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    return proc, proc.stdout.readline().strip()


def _end(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(10)
    proc.stdin.close()
    proc.stdout.close()


def test_transition_lock_excludes_and_is_released_when_its_owner_exits(tmp_path):
    """Last-check row 6 + post-cut-over blocker 2: the transition lock serialises gate writes with the final check + dispatch; it
    is an OS lock, so a holder that exits (cleanly or killed) leaves nothing behind that has to be reclaimed."""
    import paid_gate as g
    import pytest
    with g.transition_lock(tmp_path, what='writer') as lock:
        assert lock.owned() and g.lock_busy(tmp_path) and g.lock_owner(tmp_path)['what'] == 'writer'
        with pytest.raises(g.GateBusy):
            with g.transition_lock(tmp_path, wait=0.05):         # another handle of the same process is excluded as well
                pass
    assert not lock.owned() and not g.lock_busy(tmp_path) and g.lock_owner(tmp_path) is None
    assert (tmp_path / '.codex_jobs' / '_batch' / g.LOCK_FILE).exists()          # the lock file stays; only the byte lock goes
    assert not (tmp_path / '.codex_jobs' / '_batch' / 'gate_mutex').exists()     # the old expiring mkdir mutex is not used
    holder, said = _holder(tmp_path, '--what', 'a paid click')
    try:
        assert said == 'held'
        assert g.lock_owner(tmp_path) == {'pid': holder.pid, 'time': g.lock_owner(tmp_path)['time'], 'what': 'a paid click'}
        with pytest.raises(g.GateBusy):
            with g.transition_lock(tmp_path, wait=0.2):
                pass
        holder.kill()                                             # the owner dies inside its critical section
        holder.wait(10)
        with g.transition_lock(tmp_path, wait=5.0):               # released by the OS: no stale age to wait out
            pass
    finally:
        _end(holder)


def test_the_reviewers_case_a_stalled_holder_past_30_seconds_still_excludes_everyone(tmp_path, monkeypatch):
    """Post-cut-over review, blocker 2: "a second holder acquired the mutex after 30 seconds while the first was still inside".
    The holder is alive and stalled; everything an age rule could look at says the lock is an hour old (the lock file's and the
    owner note's times, and the contender's clocks). Nobody else gets it, and no click is dispatched beside it."""
    import time
    import paid_gate as g
    import pytest
    holder, said = _holder(tmp_path, '--what', 'stalled click')
    try:
        assert said == 'held'
        batch = tmp_path / '.codex_jobs' / '_batch'
        old = time.time() - 3600
        for name in (g.LOCK_FILE, g.LOCK_OWNER_FILE):
            os.utime(batch / name, (old, old))
        real_time, real_mono = time.time, time.monotonic
        monkeypatch.setattr(time, 'time', lambda: real_time() + 3600)
        monkeypatch.setattr(time, 'monotonic', lambda: real_mono() + 3600)
        for _ in range(3):
            with pytest.raises(g.GateBusy):
                with g.transition_lock(tmp_path, wait=0.2, what='second holder'):
                    pass
        calls = []
        with pytest.raises(g.GateClosed, match='gate transition in progress'):
            g.checked_dispatch(lambda: calls.append('click'), g.env_for(tmp_path), wait=0.2)
        assert calls == [] and holder.poll() is None
        second, said = _holder(tmp_path, '--wait', '0.3')        # another process: refused too
        try:
            assert (said, second.wait(10)) == ('busy', g.EXIT_LOCK_BUSY)
        finally:
            _end(second)
        assert g.lock_owner(tmp_path)['pid'] == holder.pid       # still the first holder's
        holder.stdin.close()                                      # it finishes: its stdin closes, it releases and exits 0
        assert holder.wait(10) == 0
        assert g.checked_dispatch(lambda: calls.append('click') or 'ok', g.env_for(tmp_path), wait=2.0) == 'ok'
    finally:
        _end(holder)


def test_checked_dispatch_holds_the_lock_until_the_dispatch_returned_however_long_it_takes(tmp_path):
    """The stalls the reviewer named (Hyper3D looks its control up after the check; Tripo awaits its intent write) are inside the
    lock: a hold writer that arrives during the dispatch waits for it and lands after the click, never between check and click."""
    import threading
    import time
    import paid_gate as g
    hold = tmp_path / '.codex_jobs' / '_batch' / 'CONVERT_HOLD'
    order = []

    def writer():
        with g.transition_lock(tmp_path, wait=20.0, what='hold writer'):
            hold.write_text('{}')
            order.append('hold written')
    thread = threading.Thread(target=writer)

    def dispatch():
        assert g.lock_owner(tmp_path)['what'] == 'paid click'
        thread.start()
        time.sleep(0.5)                                           # the stalled part of the dispatch
        assert thread.is_alive() and not hold.exists()
        order.append('click')
    g.checked_dispatch(dispatch, g.env_for(tmp_path))
    thread.join(20)
    assert order == ['click', 'hold written'] and not g.lock_busy(tmp_path)


def test_blocker3_a_manual_pause_mode_closes_the_gate_at_once_without_any_pause_file(tmp_path):
    """Post-cut-over review, blocker 3: Queue Pause writes supervisor_mode.json; the supervisor mirrors it into PAUSE only at its
    next tick (30 s). The paid gate reads the mode itself, under the dispatch lock."""
    import paid_gate as g
    import pytest
    batch = tmp_path / '.codex_jobs' / '_batch'
    batch.mkdir(parents=True)
    env, mode = g.env_for(tmp_path), batch / g.MODE_FILE
    calls = []
    for value, closed in (('auto', False), ('views-run', False), ('run', False), ('pause', True), ('pause-conversions', True)):
        mode.write_text(json.dumps({'mode': value, 'since': 'now'}))
        assert not (batch / 'PAUSE').exists()
        assert (g.closed_reason(env) is not None) == closed, value
        if closed:
            assert g.closed_reason(env) == f'conversions are paused (mode {value})'
            with pytest.raises(g.GateClosed, match=f'mode {value}'):
                g.checked_dispatch(lambda: calls.append(value), env)
            assert g.closed_reason(g.env_for(tmp_path, override_pause=True)) is None      # a confirmed Run now (monster lane)
        else:
            g.checked_dispatch(lambda: calls.append(value), env)
    assert calls == ['auto', 'views-run', 'run']
    mode.write_text('{broken')                                    # a mode that cannot be read is not assumed to be "run"
    assert 'cannot be read' in g.closed_reason(env)
    mode.unlink()
    assert g.closed_reason(env) is None                           # no mode file: the supervisor's default, not a pause


def test_blocker3_a_mode_pause_written_under_the_lock_lands_before_the_check_or_after_the_click(tmp_path):
    import threading
    import time
    import paid_gate as g
    import pytest
    batch = tmp_path / '.codex_jobs' / '_batch'
    env, mode, order = g.env_for(tmp_path), batch / g.MODE_FILE, []

    def pause():                                                  # what modes.write_mode does: the write under transition_lock
        with g.transition_lock(tmp_path, wait=20.0, what='mode pause'):
            mode.write_text(json.dumps({'mode': 'pause'}))
            order.append('paused')
    thread = threading.Thread(target=pause)

    def dispatch():
        thread.start()
        time.sleep(0.3)
        assert not mode.exists()
        order.append('click')
    g.checked_dispatch(dispatch, env)
    thread.join(20)
    assert order == ['click', 'paused']
    with pytest.raises(g.GateClosed):                             # acknowledged = no later click
        g.checked_dispatch(lambda: order.append('second click'), env)
    assert order == ['click', 'paused']


def test_blocker1_the_supervisors_own_pause_closes_the_gate_beside_a_foreign_pause(tmp_path):
    """The supervisor leaves a PAUSE it did not write alone and keeps its own pause in supervisor_pause.json; both count."""
    import paid_gate as g
    batch = tmp_path / '.codex_jobs' / '_batch'
    batch.mkdir(parents=True)
    env = g.env_for(tmp_path)
    (batch / 'PAUSE').write_text(json.dumps({'reason': 'maintenance', 'scope': ['views']}))     # a views-only manual pause
    assert g.closed_reason(env) is None
    own = batch / g.SUPERVISOR_PAUSE
    own.write_text(json.dumps({'reason': 'user active (views keep running)', 'scope': ['conversions'], 'mirror': None}))
    assert g.closed_reason(env) == 'conversions are paused (supervisor: user active (views keep running))'
    assert g.closed_reason(g.env_for(tmp_path, override_pause=True)) is None
    own.write_text(json.dumps({'reason': 'x', 'scope': ['views']}))
    assert g.closed_reason(env) is None
    own.write_text('{broken')
    assert 'supervisor' in g.closed_reason(env)                   # unreadable: closed
    own.unlink()
    assert g.closed_reason(env) is None


def test_hold_command_protocol(tmp_path):
    """`paid_gate.py hold`: "held" then the lock until stdin closes; exit 0. The lock of a root that does not exist yet is made."""
    import paid_gate as g
    root = tmp_path / 'fresh'
    holder, said = _holder(root)
    try:
        assert said == 'held' and g.lock_busy(root)
        holder.stdin.close()
        assert holder.wait(10) == 0 and not g.lock_busy(root)
    finally:
        _end(holder)
    run = subprocess.run([sys.executable, str(TOOL), 'hold'], capture_output=True, text=True, timeout=60,
                         env={k: v for k, v in os.environ.items() if k != g.ENV}, stdin=subprocess.DEVNULL)
    assert (run.returncode, run.stdout.strip()) == (2, 'no root')
    run = subprocess.run([sys.executable, str(TOOL), 'hold'], capture_output=True, text=True, timeout=60,
                         env={**os.environ, **g.env_for(root)}, stdin=subprocess.DEVNULL)       # the root of the gate variable
    assert (run.returncode, run.stdout.strip()) == (0, 'held')


def test_checked_dispatch_never_dispatches_through_a_closed_or_busy_gate(tmp_path):
    import paid_gate as g
    import pytest
    env = g.env_for(tmp_path)
    calls = []
    assert g.checked_dispatch(lambda: calls.append(1) or 'ok', env) == 'ok'
    batch = tmp_path / '.codex_jobs' / '_batch'
    (batch / 'CONVERT_HOLD').write_text('{}')
    with pytest.raises(g.GateClosed):
        g.checked_dispatch(lambda: calls.append(2), env)
    (batch / 'CONVERT_HOLD').unlink()
    with g.transition_lock(tmp_path):
        with pytest.raises(g.GateClosed):
            g.checked_dispatch(lambda: calls.append(3), env, wait=0.05)
    assert calls == [1]
