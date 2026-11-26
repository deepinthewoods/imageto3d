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


def test_transition_lock_excludes_and_reclaims_a_dead_owner(tmp_path):
    """Last-check row 6: the transition mutex serialises gate writes with the final check + dispatch."""
    import json as _json
    import paid_gate as g
    import pytest
    with g.transition_lock(tmp_path, what='writer'):
        with pytest.raises(g.GateBusy):
            with g.transition_lock(tmp_path, wait=0.05):
                pass
    mutex = tmp_path / '.codex_jobs' / '_batch' / g.MUTEX_DIR
    mutex.mkdir()
    (mutex / 'owner.json').write_text(_json.dumps({'pid': 999999999}))   # a dead owner
    with g.transition_lock(tmp_path, wait=1.0):
        pass
    assert not mutex.exists()


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


def test_a_holder_that_lost_the_mutex_neither_dispatches_nor_removes_the_new_holders(tmp_path):
    """The mutex is reclaimed as stale (a gate check that outlasted MUTEX_STALE) and taken by another process: the first holder
    does not click, and its release leaves the other holder's mutex alone."""
    import json as _json
    import sys as _sys
    import paid_gate as g
    import pytest
    mutex = tmp_path / '.codex_jobs' / '_batch' / g.MUTEX_DIR
    other = _json.dumps({'pid': 999999999, 'token': 'someone else'})   # (a dead pid: reclaimable below)
    # the gate's extra command stands in for the slow check during which the mutex changes hands
    steal = f'import pathlib; pathlib.Path({str(mutex / "owner.json")!r}).write_text({other!r})'
    env = g.env_for(tmp_path, cmd=[_sys.executable, '-c', steal])
    calls = []
    with pytest.raises(g.GateClosed, match='mutex was lost'):
        g.checked_dispatch(lambda: calls.append(1), env)
    assert calls == []
    assert _json.loads((mutex / 'owner.json').read_text())['token'] == 'someone else'   # not removed by the loser's release
    lock = g.transition_lock(tmp_path, wait=0.05)
    with lock:
        assert lock.owned()
        owner = _json.loads((mutex / 'owner.json').read_text())
        assert owner['pid'] == __import__('os').getpid() and owner['token'] == lock.token
    assert not lock.owned() and not mutex.exists()
