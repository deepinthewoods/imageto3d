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
