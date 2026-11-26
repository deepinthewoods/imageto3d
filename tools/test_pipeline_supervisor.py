"""pipeline_supervisor: screen parsing and pause/resume transitions, freeze candidates from a fake process table,
auto-advance gates 1 and 2 (temp root, subprocesses mocked), and the PAUSE check of view_batch's start loop.
Run: python3 -m pytest tools/test_pipeline_supervisor.py -q   (Windows: python -X utf8 -m pytest ...)

The screen-gate tests below drive the GNOME (gdbus) path through a fake run_cmd; on Windows an autouse fixture routes
Supervisor.user_away to that path so they still run, and the Windows gate has its own tests at the end."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_supervisor as ps  # noqa: E402
import view_batch  # noqa: E402

SET = 'ragged_outfit_01_test'
RACES = ['dwarf_male', 'elf_female']
VIEWS = ('front', 'front_three_quarter', 'back', 'back_three_quarter')


def png(path, size=(8, 8), mode='RGBA'):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, (200, 10, 10, 255) if mode == 'RGBA' else (200, 10, 10)).save(path)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding='utf-8')


def completed(stdout='', returncode=0, stderr=''):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Fake:
    """Stands in for Supervisor.run_cmd."""

    def __init__(self, root):
        self.root, self.calls, self.screen, self.drift = root, [], '(true,)\n', set()
        self.idle = ''   # GetIdletime reply; '' = unreadable, so the lock state alone decides
        self.fail_gdbus = False

    def __call__(self, command, timeout=900):
        command = [str(c) for c in command]
        self.calls.append(command)
        text = ' '.join(command)
        if 'GetActive' in text:
            if self.fail_gdbus:
                raise OSError('no gdbus')
            return completed(self.screen)
        if 'GetIdletime' in text:
            if self.fail_gdbus:
                raise OSError('no gdbus')
            return completed(self.idle)
        if 'check_concept_colours.py' in text:
            images = command[command.index('--json') + 1:]
            return completed(json.dumps([{'image': i, 'ok': Path(i).name not in self.drift,
                                          'problems': ['accent: hue 322'] if Path(i).name in self.drift else []}
                                         for i in images]), 1 if self.drift else 0)
        if text.endswith(f'claim {SET}') or ' claim ' in text:
            name = command[-1]
            job = self.root / 'processing_views' / f'{name}--abc123def456'
            job.parent.mkdir(exist_ok=True)
            (self.root / 'approved' / name).rename(job)
            return completed(json.dumps({'path': str(job), 'job': job.name}))
        return completed('{}')

    def of(self, word):
        return [c for c in self.calls if word in c]


@pytest.fixture(autouse=True)
def gnome_gate(request, monkeypatch):
    """Tests of the GNOME gate (fake gdbus replies); the Windows-gate tests are marked `wingate` and keep the real dispatch."""
    if ps.IS_WIN and not getattr(request.function, 'wingate', False):
        monkeypatch.setattr(ps.Supervisor, 'user_away', ps.Supervisor.away_gnome)


@pytest.fixture
def root(tmp_path):
    for race in RACES:
        png(tmp_path / 'base_views' / race / 'front.png')
    (tmp_path / '.codex_jobs' / '_batch' / 'tasks').mkdir(parents=True)
    (tmp_path / '.codex_jobs' / '_batch' / 'status').mkdir(parents=True)
    write(tmp_path / '.codex_jobs' / '_batch' / 'briefs' / 'rules.txt', 'rules')
    write(tmp_path / '.codex_jobs' / '_batch' / 'advance.json', {'sets': [
        {'pattern': 'ragged_outfit_*', 'extra_file': '.codex_jobs/_batch/briefs/rules.txt', 'views_priority': 2}]})
    return tmp_path


def make_sup(root):
    sup = ps.Supervisor(root)
    sup.run_cmd = Fake(root)
    return sup


def events(root):
    path = root / '.codex_jobs' / '_batch' / 'events.log'
    return path.read_text(encoding='utf-8').splitlines() if path.exists() else []


def add_task(root, name, outputs, state='done', checks=None):
    batch = root / '.codex_jobs' / '_batch'
    write(batch / 'tasks' / f'{name}.json', {'name': name, 'outputs': [str(o) for o in outputs], 'priority': 1})
    status = {'state': state, 'attempts': 1, 'jobs': []}
    if checks is not None:
        status['checks'] = checks
    write(batch / 'status' / f'{name}.json', status)


def make_front(root, set_id=SET, race='dwarf_male', module='outfit', flags=(), state='done', prompts='{"x": 1}',
               design='Design: a sack\n'):
    folder = root / 'concepts' / set_id / race
    out = folder / f'{module}_front.png'
    png(out)
    write(folder / f'{module}_prompts.json', prompts)
    write(folder / f'{module}_design_record.md', '# rec\n' + design)
    add_task(root, f'fronts--{set_id}--{race}', [out], state, {out.name: list(flags)})
    return out


# ---------------------------------------------------------------- screen and freezing
def test_parse_screen():
    assert ps.parse_screen('(true,)\n') is True
    assert ps.parse_screen('(false,)') is False
    assert ps.parse_screen('Error: no such object') is None
    assert ps.parse_screen('') is None


def table(root=None):
    """A fake process table; with `root` every row's working directory is that checkout (the supervisor only freezes the
    descendants of a convert_job.py whose resolved script path is under its own root)."""
    def row(pid, ppid, comm, *cmd, stopped=False):
        return {'pid': pid, 'ppid': ppid, 'comm': comm, 'cmdline': list(cmd), 'stopped': stopped, 'start': 1000.0 + pid,
                **({'cwd': str(root)} if root is not None else {})}
    return [
        row(1, 0, 'systemd'),
        row(100, 1, 'python3', 'python3', 'tools/convert_queue.py', '--worker', '1'),
        row(110, 100, 'python3', '/usr/bin/python3', 'tools/tripo_pipeline/convert_job.py', 'job--1'),
        row(111, 110, 'blender', '/usr/local/bin/blender', '--background'),
        row(112, 110, 'python3', 'python3', 'tools/tripo_pipeline/reduce_model.py', 'x'),
        row(113, 110, 'python3', 'python3', 'tools/hyper3d_pipeline/rodin_backend.py', 'collect'),
        row(114, 113, 'firefox', 'firefox'),
        row(115, 113, 'geckodriver', 'geckodriver'),
        row(116, 110, 'node', 'node', 'tools/tripo_pipeline/studio.mjs', 'collect'),
        row(117, 110, 'Xvfb', 'Xvfb', ':91'),
        row(118, 111, 'python3', 'python3', 'tools/tripo_pipeline/segment_first.py'),
        row(119, 110, 'bash', 'bash', 'rereduce.sh'),
        row(120, 119, 'blender', 'blender', '-b'),
        row(200, 1, 'codex', 'codex', 'exec'),
        row(300, 1, 'blender', 'blender'),   # not under convert_job (a user's Blender)
        row(301, 1, 'python3', 'python3', 'tools/tripo_pipeline/reduce_model.py'),   # not under convert_job
    ]


def test_heavy_selection():
    assert sorted(r['pid'] for r in ps.heavy_processes(table())) == [111, 112, 118, 120]


def test_heavy_ignores_already_stopped_in_freeze(root, monkeypatch):
    rows = table(root)
    rows[3]['stopped'] = True
    sent = []
    sup = make_sup(root)
    monkeypatch.setattr(sup, 'send', lambda pid, sig, start=None: sent.append((pid, sig)))
    frozen = sup.freeze(rows)
    assert sorted(r['pid'] for r in frozen) == [112, 118, 120]
    assert all(sig == ps.SIGSTOP for _, sig in sent)


def test_pause_resume_transitions(root, monkeypatch):
    rows, sent = table(root), []
    sup = make_sup(root)
    monkeypatch.setattr(ps, 'process_table', lambda: rows)

    def send(pid, sig, start=None):
        sent.append((pid, sig))
        for row in rows:
            if row['pid'] == pid:
                row['stopped'] = sig == ps.SIGSTOP
    monkeypatch.setattr(sup, 'send', send)
    sup.run_cmd.screen = '(false,)\n'   # screen on
    sup.tick_screen()
    assert sup.pause_file.exists() and json.loads(sup.pause_file.read_text())['reason'] == 'user active'
    assert json.loads(sup.pause_file.read_text())['scope'] == ['conversions', 'views']   # user rule 2026-10-29: views idle too
    assert sorted(p for p, s in sent if s == ps.SIGSTOP) == [111, 112, 118, 120]
    recorded = json.loads(sup.pids_file.read_text())
    assert sorted(r['pid'] for r in recorded) == [111, 112, 118, 120] and all('start' in r for r in recorded)
    sup.tick_screen()   # still paused: nothing new, nothing re-signalled
    assert len(sent) == 4
    rows.append({'pid': 130, 'ppid': 110, 'comm': 'blender', 'cmdline': ['blender'], 'stopped': False, 'start': 1130.0})
    sup.tick_screen()   # a new Blender step started by the module after collecting is frozen on the next tick
    assert (130, ps.SIGSTOP) in sent
    sent.clear()
    sup.run_cmd.screen = '(true,)\n'   # blanked
    sup.tick_screen()
    assert not sup.pause_file.exists() and not sup.pids_file.exists()
    assert sorted(p for p, s in sent if s == ps.SIGCONT) == [111, 112, 118, 120, 130]
    assert any('SUPERVISOR PAUSE' in line for line in events(root)) and any('SUPERVISOR RESUME' in line for line in events(root))


def test_parse_idle():
    assert ps.parse_idle('(uint64 301234,)\n') == 301.234
    assert ps.parse_idle('(5,)') == 0.005
    assert ps.parse_idle('Error: no such object') is None


def test_idle_user_counts_as_away_even_with_the_screen_unlocked(root, monkeypatch):
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    sup = make_sup(root)
    sup.run_cmd.screen = '(false,)\n'   # never locks (a video inhibits the screensaver)
    sup.run_cmd.idle = '(uint64 4000,)\n'   # moved the mouse 4 s ago
    sup.tick_screen()
    assert json.loads(sup.pause_file.read_text())['reason'] == 'user active'
    sup.run_cmd.idle = f'(uint64 {ps.AWAY_AFTER * 1000 + 1},)\n'   # no input for 5 min
    sup.tick_screen()
    assert not sup.pause_file.exists()
    sup.run_cmd.idle = '(uint64 10,)\n'   # back at the computer
    sup.tick_screen()
    assert sup.pause_file.exists()
    sup.run_cmd.screen, sup.run_cmd.idle = '(true,)\n', ''   # locked, idle unreadable
    sup.tick_screen()
    assert not sup.pause_file.exists()


def test_resume_skips_reused_pid(root, monkeypatch):
    sup = make_sup(root)
    sent = []
    monkeypatch.setattr(sup, 'send', lambda pid, sig, start=None: sent.append(pid))
    ps.atomic_write(sup.pids_file, [{'pid': 111, 'start': 5.0, 'comm': 'blender'}])   # started at 1111 now: another process
    rows = table()
    assert sup.resume(rows) == []
    assert sent == []


def test_gdbus_failure_keeps_state_and_logs_once(root, monkeypatch):
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    sup = make_sup(root)
    sup.run_cmd.screen = '(false,)\n'
    sup.tick_screen()
    assert sup.pause_file.exists()
    sup.run_cmd.fail_gdbus = True
    sup.tick_screen()
    sup.tick_screen()
    assert sup.pause_file.exists()   # kept
    assert sum('unreadable' in line for line in events(root)) == 1
    sup.run_cmd.fail_gdbus = False
    sup.run_cmd.screen = '(true,)\n'
    sup.tick_screen()
    assert not sup.pause_file.exists()


def test_manual_conversions_only_pause_keeps_views_running(root, monkeypatch, capsys):
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    sup = make_sup(root)
    sup.run_cmd.screen = '(true,)\n'
    ps.main(['--root', str(root), 'mode', 'pause-conversions'])
    sup.tick_screen()
    pause = json.loads(sup.pause_file.read_text())
    assert (pause['reason'], pause['scope']) == ('manual pause-conversions', ['conversions'])
    sup.status()
    assert 'mode: pause-conversions; user: away (run)' in capsys.readouterr().out


def test_mode_override(root, monkeypatch):
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    sup = make_sup(root)
    sup.run_cmd.screen = '(true,)\n'
    ps.main(['--root', str(root), 'mode', 'pause'])
    sup.tick_screen()
    assert sup.pause_file.exists() and json.loads(sup.pause_file.read_text())['reason'] == 'manual pause'
    assert json.loads(sup.pause_file.read_text())['scope'] == ['conversions', 'views']
    ps.main(['--root', str(root), 'mode', 'run'])
    sup.run_cmd.screen = '(false,)\n'
    sup.tick_screen()
    assert not sup.pause_file.exists()
    ps.main(['--root', str(root), 'mode', 'auto'])
    sup.tick_screen()
    assert sup.pause_file.exists()


def test_release_all_on_exit(root, monkeypatch):
    rows, sent = table(root), []
    monkeypatch.setattr(ps, 'process_table', lambda: rows)
    sup = make_sup(root)
    monkeypatch.setattr(sup, 'send', lambda pid, sig, start=None: sent.append((pid, sig)))
    sup.run_cmd.screen = '(false,)\n'
    sup.tick_screen()
    for row in rows:
        row['stopped'] = row['pid'] in (111, 112, 118, 120)
    sent.clear()
    sup.release_all()
    assert not sup.pause_file.exists() and not sup.pids_file.exists()
    assert sorted(p for p, s in sent if s == ps.SIGCONT) == [111, 112, 118, 120]


def test_pause_and_mode_writes_take_the_gate_transition_mutex(root, monkeypatch):
    """Last-check row 6: PAUSE, supervisor_pause.json and supervisor_mode.json change only under the paid-gate transition lock,
    so a paid click's final gate check and its dispatch (which hold it) never straddle a pause."""
    import threading
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    sup = make_sup(root)
    seen, real_write, real_unlink = [], ps.atomic_write, Path.unlink

    def holder():
        owner = ps.paid_gate.lock_owner(root)       # None when nobody holds the lock: the write would be unserialized
        return owner['what'] if owner else None

    def write(path, data):
        if path in (sup.pause_file, sup.mode_file):
            seen.append(('write', path.name, holder()))
        elif path == sup.own_pause_file:
            assert holder() is not None
        real_write(path, data)

    def unlink(self, missing_ok=False):
        if self == sup.pause_file:
            seen.append(('unlink', self.name, holder()))
        elif self == sup.own_pause_file:
            assert holder() is not None
        real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(ps, 'atomic_write', write)
    monkeypatch.setattr(Path, 'unlink', unlink)
    sup.run_cmd.screen = '(false,)\n'                       # the user is active: pause
    sup.tick_screen()
    sup.run_cmd.screen = '(true,)\n'                        # away: resume
    sup.tick_screen()
    ps.main(['--root', str(root), 'mode', 'pause'])
    sup.tick_screen()
    sup.release_all()
    assert seen == [('write', 'PAUSE', 'supervisor PAUSE (user active)'), ('unlink', 'PAUSE', 'supervisor PAUSE removed'),
                    ('write', 'supervisor_mode.json', 'supervisor mode pause'), ('write', 'PAUSE', 'supervisor PAUSE (manual pause)'),
                    ('unlink', 'PAUSE', 'supervisor exit: PAUSE removed')]
    assert not ps.paid_gate.lock_busy(root)                 # released every time
    assert not sup.own_pause_file.exists()
    # a click in progress (its check + dispatch hold the lock): the pause is written only after it
    ps.main(['--root', str(root), 'mode', 'auto'])
    sup.run_cmd.screen = '(false,)\n'
    with ps.paid_gate.transition_lock(root, what='paid click'):
        ticker = threading.Thread(target=sup.tick_screen)
        ticker.start()
        ticker.join(0.4)
        assert ticker.is_alive() and not sup.pause_file.exists()     # the writer waits for the click's critical section
    ticker.join(20)
    assert not ticker.is_alive() and sup.pause_file.exists()
    assert seen[-1] == ('write', 'PAUSE', 'supervisor PAUSE (user active)')


# ---------------------------------------------------------------- post-cut-over review (review_v2_post_cutover_20261126)
def pause_bytes(sup):
    return sup.pause_file.read_bytes() if sup.pause_file.exists() else None


def test_blocker1_the_supervisor_never_overwrites_or_removes_a_pause_it_did_not_write(root, monkeypatch):
    """The reviewer's case: the mode pauses conversions (the supervisor mirrors that into PAUSE), then a separate maintenance PAUSE
    is written. No tick, no mode change and no exit of the supervisor may overwrite or remove it."""
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    sup = make_sup(root)
    gate = ps.paid_gate
    ps.main(['--root', str(root), 'mode', 'pause-conversions'])
    sup.tick_screen()
    mirror = pause_bytes(sup)
    own = json.loads(sup.own_pause_file.read_text())
    assert json.loads(mirror) == {'since': own['since'], 'reason': 'manual pause-conversions', 'scope': ['conversions']}
    assert own['mirror'] == hashlib.sha256(mirror).hexdigest() and own['pid'] == ps.os.getpid()
    assert set(json.loads(mirror)) == {'since', 'reason', 'scope'}          # the mirror keeps the old shape for its readers
    maintenance = json.dumps({'reason': 'maintenance: disk swap', 'scope': ['conversions']}).encode()
    sup.pause_file.write_bytes(maintenance)                                  # a second, independent pause replaces the mirror
    for _ in range(3):
        sup.tick_screen()
        assert pause_bytes(sup) == maintenance                               # (the reviewer saw it overwritten by one tick)
    own = json.loads(sup.own_pause_file.read_text())
    assert (own['reason'], own['mirror']) == ('manual pause-conversions', None)   # our own pause is still on record, separately
    assert sum('PAUSE FOREIGN (maintenance: disk swap)' in line for line in events(root)) == 1
    ps.main(['--root', str(root), 'mode', 'run'])                            # Run: the supervisor's own pause ends ...
    sup.tick_screen()
    assert pause_bytes(sup) == maintenance and not sup.own_pause_file.exists()   # ... the maintenance pause does not
    assert 'maintenance: disk swap' in gate.file_reason(root)
    ps.main(['--root', str(root), 'mode', 'pause'])                          # and a new mode pause does not replace it either
    sup.tick_screen()
    assert pause_bytes(sup) == maintenance and json.loads(sup.own_pause_file.read_text())['reason'] == 'manual pause'
    sup.release_all()                                                        # nor does the supervisor's exit
    assert pause_bytes(sup) == maintenance and not sup.own_pause_file.exists()
    sup.pause_file.unlink()                                                  # maintenance over: the supervisor mirrors again
    sup.tick_screen()
    assert json.loads(pause_bytes(sup))['reason'] == 'manual pause'
    ps.main(['--root', str(root), 'mode', 'run'])
    sup.tick_screen()
    assert pause_bytes(sup) is None and not sup.own_pause_file.exists()


def test_blocker1_a_supervisor_shaped_pause_without_a_record_is_taken_over_any_other_is_not(root, monkeypatch):
    """Start-up: an older supervisor's PAUSE (or the one the app put back after a restart) has no supervisor_pause.json beside
    it. One of exactly the supervisor's shape is its own and follows the user gate; anything else is somebody else's."""
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    batch = root / '.codex_jobs' / '_batch'
    write(batch / 'PAUSE', {'since': '2026-11-26T08:00:00', 'reason': 'user active', 'scope': ['conversions', 'views']})
    sup = make_sup(root)
    assert sup.paused
    sup.run_cmd.screen = '(false,)\n'                       # still active: adopted (since kept), recorded as ours
    sup.tick_screen()
    assert json.loads(pause_bytes(sup))['since'] == '2026-11-26T08:00:00'
    assert json.loads(sup.own_pause_file.read_text())['mirror'] == hashlib.sha256(pause_bytes(sup)).hexdigest()
    sup.run_cmd.screen = '(true,)\n'                        # away: removed
    sup.tick_screen()
    assert pause_bytes(sup) is None
    for foreign in ({'since': 'x', 'reason': 'user active', 'scope': ['conversions'], 'by': 'niall'},     # an extra field
                    {'since': 'x', 'reason': 'maintenance', 'scope': ['conversions']},                    # not our reason
                    {'reason': 'manual pause'}):                                                          # no scope
        write(batch / 'PAUSE', foreign)
        before = pause_bytes(sup)
        fresh = make_sup(root)
        assert not fresh.paused
        fresh.tick_screen()                                 # away: nothing of ours to remove
        fresh.run_cmd.screen = '(false,)\n'
        fresh.tick_screen()                                 # active: our own pause is recorded beside it
        fresh.release_all()
        assert pause_bytes(sup) == before and not fresh.own_pause_file.exists()


def test_blocker2_a_stalled_lock_holder_keeps_the_pause_unwritten_and_the_tick_alive(root, monkeypatch):
    """The transition lock never expires. While a (stalled) click holds it the supervisor writes no PAUSE, says so once, still
    freezes the heavy steps and writes it on the first tick after the holder left."""
    rows, sent = table(root), []
    monkeypatch.setattr(ps, 'process_table', lambda: rows)
    monkeypatch.setattr(ps, 'GATE_LOCK_TRIES', 2)
    monkeypatch.setattr(ps, 'GATE_LOCK_WAIT', 0.05)
    sup = make_sup(root)
    monkeypatch.setattr(sup, 'send', lambda pid, sig, start=None: sent.append((pid, sig)))
    sup.run_cmd.screen = '(false,)\n'
    with ps.paid_gate.transition_lock(root, what='a stalled paid click'):
        assert sup.tick_screen() is True
        sup.tick_screen()
        assert not sup.pause_file.exists() and not sup.own_pause_file.exists() and not sup.paused
        assert sorted(p for p, s in sent if s == ps.SIGSTOP) == [111, 112, 118, 120]
    assert sum('ERROR pause-write' in line and 'not written yet' in line for line in events(root)) == 1
    sup.tick_screen()
    assert sup.pause_file.exists() and sup.paused
    sup.run_cmd.screen = '(true,)\n'
    with ps.paid_gate.transition_lock(root, what='a stalled paid click'):
        sup.tick_screen()                                   # the pause cannot be removed: nothing is resumed either
        assert sup.pause_file.exists() and sup.paused and not [s for s in sent if s[1] == ps.SIGCONT]
        assert ps.main(['--root', str(root), 'mode', 'pause']) == 1     # a mode write is not passed off as made
        assert not sup.mode_file.exists()
    sup.tick_screen()
    assert not sup.pause_file.exists() and not sup.paused


def test_blocker6_a_failed_resume_keeps_the_identity_and_is_retried_until_it_works(root, monkeypatch):
    """The reviewer's case: resume raises (a Windows error). The supervisor must not report the process as continued and must
    not delete its only record of the suspension; it tries again every tick."""
    rows, sent, broken = table(root), [], {111}
    monkeypatch.setattr(ps, 'process_table', lambda: rows)
    sup = make_sup(root)

    def send(pid, sig, start=None):
        if sig == ps.SIGCONT and pid in broken:
            raise OSError('[WinError 5] Access is denied')
        sent.append((pid, sig))
    monkeypatch.setattr(sup, 'send', send)
    sup.run_cmd.screen = '(false,)\n'
    sup.tick_screen()
    record = {r['pid']: r for r in json.loads(sup.pids_file.read_text())}[111]
    sup.run_cmd.screen = '(true,)\n'
    sup.tick_screen()
    assert json.loads(sup.pids_file.read_text()) == [record]                 # the failed identity is kept, the others are done
    assert sorted(p for p, s in sent if s == ps.SIGCONT) == [112, 118, 120]
    log = events(root)
    assert any('RESUME (user away)' in line and 'continued 3 process(es)' in line and '1 process(es) could NOT be resumed' in line
               for line in log)
    assert sum('RESUME FAILED 111:blender ([WinError 5] Access is denied)' in line for line in log) == 1
    assert not sup.pause_file.exists() and not sup.paused
    sup.tick_screen()                                                        # tried again on every tick, reported once
    sup.tick_screen()
    assert json.loads(sup.pids_file.read_text()) == [record]
    assert sum('RESUME FAILED' in line for line in events(root)) == 1
    assert not any('continued 4' in line for line in events(root))
    sup.release_all()                                                        # an exit keeps it for the next start as well
    assert json.loads(sup.pids_file.read_text()) == [record]
    assert any('RESUME on exit: continued 0 process(es); 1 process(es) could NOT be resumed' in line for line in events(root))
    later = make_sup(root)                                                   # the next instance resumes it at start
    monkeypatch.setattr(later, 'send', send)
    broken.clear()
    later.resume_stale()
    assert (111, ps.SIGCONT) in sent and not later.pids_file.exists()
    assert any('RESUME at start: continued 1 process(es)' in line for line in events(root))


def test_blocker6_a_record_is_dropped_only_when_the_process_is_confirmed_gone(root, monkeypatch):
    rows = table(root)
    by_pid = {row['pid']: row for row in rows}
    monkeypatch.setattr(ps, 'process_table', lambda: rows)
    sup = make_sup(root)
    records = [{'pid': pid, 'start': by_pid[pid]['start'], 'comm': 'blender'} for pid in (111, 112, 118, 120)]
    ps.atomic_write(sup.pids_file, records)

    def send(pid, sig, start=None):
        if pid == 111:                                       # it ends while it is being resumed
            rows.remove(by_pid[111])
            raise OSError('process no longer exists')
        if pid == 112:
            raise OSError('resume failed')
    monkeypatch.setattr(sup, 'send', send)
    by_pid[118]['start'] = by_pid[118]['start'] + 500        # the pid belongs to a later process now: gone, never signalled
    by_pid[120]['start'] = 0                                 # its identity cannot be read: not confirmed gone
    assert sup.resume() == []
    assert sorted(r['pid'] for r in json.loads(sup.pids_file.read_text())) == [112, 120]
    assert sorted(r['pid'] for r, _ in sup.resume_failures) == [112, 120]


def test_blocker6_a_paused_tick_keeps_the_record_of_a_pid_whose_start_time_is_unreadable(root, monkeypatch):
    """Final-live review, earlier blocker 6 (the reviewer's case): while paused, a tick on which the suspended pid's start time
    could not be read erased its record in freeze(), so the process was never resumed. freeze applies the confirmed-gone rule
    of resume: the record stays, the process is not suspended a second time, and it is resumed once the pause ends."""
    rows, sent = table(root), []
    by_pid = {row['pid']: row for row in rows}
    monkeypatch.setattr(ps, 'process_table', lambda: rows)
    sup = make_sup(root)
    monkeypatch.setattr(sup, 'send', lambda pid, sig, start=None: sent.append((pid, sig, start)))
    sup.run_cmd.screen = '(false,)\n'
    sup.tick_screen()                                                        # the pause: the four heavy processes are suspended
    records = json.loads(sup.pids_file.read_text())
    assert sorted(r['pid'] for r in records) == [111, 112, 118, 120]
    readable = by_pid[111]['start']
    by_pid[111]['start'] = 0                                                 # psutil could not read it on this tick
    by_pid[118]['start'] += 500                                              # a later process has this pid: the old one is gone
    rows.remove(by_pid[120])                                                 # ended: confirmed gone
    del sent[:]
    for _ in range(3):                                                       # paused ticks
        sup.tick_screen()
        assert sup.paused
    assert sup.freeze(rows) == []
    later = {'pid': 118, 'start': by_pid[118]['start'], 'comm': 'python3'}   # the new heavy process with pid 118 was frozen
    assert json.loads(sup.pids_file.read_text()) == [r for r in records if r['pid'] in (111, 112)] + [later]
    assert [s for s in sent if s[1] == ps.SIGSTOP] == [(118, ps.SIGSTOP, later['start'])]   # 111 / 112 never a second time
    assert not [s for s in sent if s[1] == ps.SIGCONT]
    by_pid[111]['start'] = readable
    sup.run_cmd.screen = '(true,)\n'
    sup.tick_screen()                                                        # the pause ends: every recorded process continues
    assert sorted(s[0] for s in sent if s[1] == ps.SIGCONT) == [111, 112, 118]
    assert (111, ps.SIGCONT, readable) in sent
    assert not sup.pids_file.exists()
    # a malformed entry is not a record of anything and never breaks a tick
    ps.atomic_write(sup.pids_file, [{'pid': 112}, 'x', {'pid': 112, 'start': by_pid[112]['start'], 'comm': 'python3'}])
    assert sorted(r['pid'] for r in sup.freeze(rows)) == [111, 118]          # 112 is on record
    assert sorted(r['pid'] for r in json.loads(sup.pids_file.read_text())) == [111, 112, 118]


# ---------------------------------------------------------------- gate 1
def gate1_ready(root, **kwargs):
    for race in RACES:
        make_front(root, race=race, **(kwargs if race == RACES[0] else {}))
    return make_sup(root)


def test_gate1_pass_claims_and_adds_views(root):
    sup = gate1_ready(root)
    sup.advance()
    job = root / 'processing_views' / f'{SET}--abc123def456'   # the fake claim moved approved/<set> here
    assert (job / 'dwarf_male' / 'outfit_front.png').exists() and (job / 'elf_female' / 'outfit_front.png').exists()
    assert not (root / 'concepts' / SET).exists() and not (root / 'approved' / SET).exists()
    assert sup.run_cmd.of('claim')
    add = sup.run_cmd.of('add-views')
    assert len(add) == 1
    assert '--priority' in add[0] and add[0][add[0].index('--priority') + 1] == '2'
    assert ps.norm(add[0][add[0].index('--extra-file') + 1]).endswith('briefs/rules.txt')
    assert any('SUPERVISOR AUTO-CLAIM ' + SET + ' -> ' + SET + '--abc123def456' in line for line in events(root))
    assert ps.read_json(sup.state_file)['sets'][SET]['views_added'] is True
    sup.advance()   # nothing left to do
    assert len(sup.run_cmd.of('add-views')) == 1


def test_gate1_waits_for_unfinished_fronts(root):
    sup = gate1_ready(root)
    add_task(root, f'fronts--{SET}--elf_female', [root / 'concepts' / SET / 'elf_female' / 'outfit_front.png'], 'running')
    sup.advance()
    assert not sup.run_cmd.of('claim') and not [l for l in events(root) if 'HOLD' in l]


def test_gate1_ignores_framing_flags_one_shot_fronts(root):
    # Concept fronts are one-shot and unchecked (user rule 2026-10-28): recorded framing flags no longer hold a set.
    sup = gate1_ready(root, flags=['- FRAMING: scale 1.20'])
    sup.advance()
    assert not [l for l in events(root) if 'HOLD' in l]
    assert sup.run_cmd.of('claim')


def test_gate1_ignores_pose_flags_for_any_module(root):
    # Same one-shot rule: pose flags on fronts (any module) do not hold.
    note = ['- POSE: body_red shifted (+64.0,+10.0) px']
    sup = gate1_ready(root, flags=note)
    sup.advance()
    assert not [l for l in events(root) if 'HOLD' in l]
    assert sup.run_cmd.of('claim')



def test_gate1_hold_on_empty_prompts_json(root):
    sup = gate1_ready(root, prompts='')
    sup.advance()
    holds = [l for l in events(root) if 'HOLD' in l]
    assert len(holds) == 1 and 'prompts.json' in holds[0] and not sup.run_cmd.of('claim')


def test_gate1_hold_on_missing_design_line_and_bad_image(root):
    sup = gate1_ready(root, design='just notes\n')
    png(root / 'concepts' / SET / 'elf_female' / 'outfit_front.png', size=(9, 9))
    sup.advance()
    text = ' '.join(events(root))
    assert 'Design:' in text and '9x9' in text and not sup.run_cmd.of('claim')


def test_gate1_ignores_colour_drift_but_holds_failed_task(root):
    # One-shot fronts (user rule 2026-10-28): colour drift no longer holds a set; a failed fronts task still does.
    sup = gate1_ready(root)
    sup.run_cmd.drift = {'outfit_front.png'}
    add_task(root, f'fronts--{SET}--elf_female', [root / 'concepts' / SET / 'elf_female' / 'outfit_front.png'], 'failed')
    sup.advance()
    assert 'fronts task failed: elf_female' in ' '.join(events(root))
    assert 'DRIFT' not in ' '.join(events(root)) and not sup.run_cmd.of('claim')


def test_gate1_accepted_drift_entries_are_now_inert(root):
    # accepted_drift was the reviewed-colour-drift waiver; drift no longer gates one-shot fronts, so entries are inert.
    sup = gate1_ready(root)
    sup.run_cmd.drift = {'outfit_front.png'}
    advance = json.loads((root / '.codex_jobs' / '_batch' / 'advance.json').read_text())
    advance['accepted_drift'] = [{'sha256': hashlib.sha256(b'not the image').hexdigest()}]
    write(root / '.codex_jobs' / '_batch' / 'advance.json', advance)
    sup.advance()
    assert not [l for l in events(root) if 'HOLD' in l] and sup.run_cmd.of('claim')


def test_gate1_ignores_sets_not_in_advance_json(root):
    make_front(root, set_id='other_set_01', race='dwarf_male')
    sup = make_sup(root)
    sup.advance()
    assert not sup.run_cmd.of('claim') and (root / 'concepts' / 'other_set_01' / 'dwarf_male').is_dir()
    assert not events(root)


def test_gate1_approved_exists_holds(root):
    (root / 'approved' / SET).mkdir(parents=True)
    sup = gate1_ready(root)
    sup.advance()
    assert 'approved/ already has this set' in ' '.join(events(root)) and not sup.run_cmd.of('claim')


def test_gate1_gnome_claimed_alone_after_other_races_moved_on(root):
    # the other races were claimed long ago (their done fronts tasks remain); a gnome front is added later
    old = root / 'processing_views' / f'{SET}--old000000000'
    for race in RACES:
        (old / race / 'outfit').mkdir(parents=True)
        add_task(root, f'fronts--{SET}--{race}', [root / 'concepts' / SET / race / 'outfit_front.png'], 'done', {})
    write(root / '.codex_jobs' / '_batch' / 'briefs' / 'gnome.txt', 'gnome rules')
    write(root / '.codex_jobs' / '_batch' / 'advance.json', {'sets': [
        {'pattern': 'ragged_outfit_*', 'extra_file': '.codex_jobs/_batch/briefs/rules.txt',
         'gnome_extra_file': '.codex_jobs/_batch/briefs/gnome.txt', 'views_priority': 2}]})
    png(root / 'base_views' / 'gnome_female' / 'front.png')
    make_front(root, race='gnome_female')
    sup = make_sup(root)
    sup.state['sets'][SET] = {'job': old.name, 'path': str(old), 'views_added': True}
    sup.advance()
    job = root / 'processing_views' / f'{SET}--abc123def456'
    assert (job / 'gnome_female' / 'outfit_front.png').exists() and not (job / 'dwarf_male').exists()
    add = sup.run_cmd.of('add-views')
    assert len(add) == 1 and ps.norm(add[0][add[0].index('--extra-file') + 1]).endswith('briefs/gnome.txt')
    state = ps.read_json(sup.state_file)['sets']
    assert state[f'{SET}#gnome']['views_added'] is True and state[SET]['job'] == old.name
    assert any(f'SUPERVISOR AUTO-CLAIM {SET}#gnome -> ' in line for line in events(root))
    assert not [l for l in events(root) if 'HOLD' in l]


def test_gate1_redone_gnome_front_is_claimed_despite_an_older_gnome_job(root):
    old = root / 'completed' / f'{SET}--old000000000'
    for race in RACES + ['gnome_female']:
        (old / race / 'outfit').mkdir(parents=True)
    png(root / 'base_views' / 'gnome_female' / 'front.png')
    make_front(root, race='gnome_female')   # the new front, queued after the old gnome model was converted
    sup = make_sup(root)
    sup.advance()
    assert (root / 'processing_views' / f'{SET}--abc123def456' / 'gnome_female').is_dir()
    assert f'{SET}#gnome' in ps.read_json(sup.state_file)['sets']


def test_gate1_gnome_does_not_wait_for_other_races(root):
    png(root / 'base_views' / 'gnome_female' / 'front.png')
    make_front(root, race='gnome_female')
    make_front(root, race='dwarf_male', state='running')
    sup = make_sup(root)
    sup.advance()
    job = root / 'processing_views' / f'{SET}--abc123def456'
    assert (job / 'gnome_female').is_dir() and (root / 'concepts' / SET / 'dwarf_male').is_dir()
    add = sup.run_cmd.of('add-views')
    assert len(add) == 1 and '--extra-file' not in add[0]   # the series rules (hems, left foot) never go to gnomes
    assert SET not in ps.read_json(sup.state_file)['sets']


# ---------------------------------------------------------------- gate 2
def make_job(root, omit_gnome=False):
    job = root / 'processing_views' / f'{SET}--abc123def456'
    write(job / '.tripo-job.json', {'state': 'generating_other_views', 'set': SET})
    for race in RACES:
        for view in VIEWS:
            png(job / '.base_views' / race / f'{view}.png')
        module = job / race / 'outfit'
        outputs, checks = [], {}
        for view in VIEWS:
            if omit_gnome and race == RACES[1] and view.endswith('three_quarter'):
                continue
            png(module / f'{view}.png')
            if view != 'front':
                outputs.append(module / f'{view}.png')
                checks[f'{view}.png'] = []
        if omit_gnome and race == RACES[1]:
            write(module / 'views_omitted.json', {'omitted': ['front_three_quarter', 'back_three_quarter'], 'reason': 'x', 'date': 'd'})
        add_task(root, f'views--{SET}--{race}--outfit', outputs, 'done', checks)
    return job


def test_gate2_pass_runs_ready(root):
    job = make_job(root)
    sup = make_sup(root)
    sup.advance()
    ready = sup.run_cmd.of('ready')
    assert ready and ready[0][-1] == job.name
    assert any('SUPERVISOR AUTO-READY ' + job.name in line for line in events(root))


def test_gate2_two_view_race_with_marker_passes(root):
    make_job(root, omit_gnome=True)
    sup = make_sup(root)
    sup.advance()
    assert sup.run_cmd.of('ready')


def test_gate2_hold_missing_view_flag_and_autoaccept_drift(root):
    job = make_job(root)
    (job / RACES[0] / 'outfit' / 'back.png').unlink()
    add_task(root, f'views--{SET}--{RACES[1]}--outfit', [job / RACES[1] / 'outfit' / 'back.png'], 'done',
             {'back.png': ['- POSE: left_foot_white shifted (+6.2,+12.3) px']})
    sup = make_sup(root)
    sup.run_cmd.drift = {'front_three_quarter.png'}
    sup.advance()
    # framing/missing reasons still hold; the colour drift is auto-waived hash-bound instead of holding
    holds = [l for l in events(root) if 'HOLD' in l]
    assert holds and 'back.png missing' in holds[-1] and 'left_foot_white' in holds[-1] and 'DRIFT' not in holds[-1]
    assert any('ACCEPTED colour drift' in l for l in events(root))
    assert not sup.run_cmd.of('ready')
    config = json.loads((root / '.codex_jobs' / '_batch' / 'advance.json').read_text())
    assert config['accepted_drift'] and all('auto-waived' in e['note'] for e in config['accepted_drift'])


def test_gate2_waits_while_tasks_run_and_holds_on_failure(root):
    job = make_job(root)
    add_task(root, f'views--{SET}--{RACES[0]}--outfit', [job / RACES[0] / 'outfit' / 'back.png'], 'running')
    sup = make_sup(root)
    sup.advance()
    assert not sup.run_cmd.of('ready') and not events(root)
    add_task(root, f'views--{SET}--{RACES[0]}--outfit', [job / RACES[0] / 'outfit' / 'back.png'], 'failed')
    sup.advance()
    assert 'views task failed' in ' '.join(events(root))


def test_gate2_ignores_sets_not_in_advance_json(root):
    job = make_job(root)
    write(root / '.codex_jobs' / '_batch' / 'advance.json', {'sets': [{'pattern': 'colonial_*'}]})
    sup = make_sup(root)
    sup.advance()
    assert not sup.run_cmd.of('ready') and not events(root) and job.is_dir()


def test_dry_run_changes_nothing(root):
    job = make_job(root)
    sup = ps.Supervisor(root, dry_run=True)
    sup.run_cmd = Fake(root)
    sup.advance()
    assert not sup.run_cmd.of('ready') and not events(root) and not sup.state_file.exists()


# ---------------------------------------------------------------- view_batch honours PAUSE
class Stop(Exception):
    pass


def run_view_batch(root, monkeypatch, loops):
    batch = root / '.codex_jobs' / '_batch'
    for name, value in dict(ROOT=root, BATCH=batch, TASKS=batch / 'tasks', STATUS=batch / 'status', EVENTS=batch / 'events.log',
                            PAUSE=batch / 'PAUSE').items():
        monkeypatch.setattr(view_batch, name, value)
    started, count = [], [0]

    def fake_sleep(_):
        count[0] += 1
        if count[0] >= loops:
            raise Stop
    monkeypatch.setattr(view_batch.time, 'sleep', fake_sleep)
    monkeypatch.setattr(view_batch.subprocess, 'Popen', lambda *a, **k: started.append(a) or SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(view_batch.shutil, 'disk_usage', lambda _: SimpleNamespace(free=100 * 2**30))
    with pytest.raises(Stop):
        view_batch.run(SimpleNamespace(parallel=2, exit_when_idle=False))
    return started


def test_view_batch_start_loop_honours_pause(root, monkeypatch):
    batch = root / '.codex_jobs' / '_batch'
    write(batch / 'tasks' / 't1.json', {'name': 't1', 'priority': 1, 'prompt': 'p', 'refs': [], 'outputs': [str(root / 'o.png')],
                                       'checks': [], 'max_attempts': 3})
    write(batch / 'PAUSE', {'since': 'now', 'reason': 'screen on'})
    assert run_view_batch(root, monkeypatch, loops=3) == []   # paused: nothing started, PAUSED reported once
    assert sum('PAUSED (screen on)' in line for line in events(root)) == 1
    (batch / 'PAUSE').unlink()
    assert len(run_view_batch(root, monkeypatch, loops=1)) == 1   # resumed: the pending task starts


def test_view_batch_runs_through_a_conversions_only_pause(root, monkeypatch):
    batch = root / '.codex_jobs' / '_batch'
    write(batch / 'tasks' / 't1.json', {'name': 't1', 'priority': 1, 'prompt': 'p', 'refs': [], 'outputs': [str(root / 'o.png')],
                                       'checks': [], 'max_attempts': 3})
    write(batch / 'PAUSE', {'since': 'now', 'reason': 'screen on', 'scope': ['conversions']})
    assert len(run_view_batch(root, monkeypatch, loops=1)) == 1
    write(batch / 'PAUSE', {'since': 'now', 'reason': 'manual pause', 'scope': ['conversions', 'views']})
    write(batch / 'status' / 't1.json', {'state': 'pending', 'attempts': 0, 'jobs': []})
    assert run_view_batch(root, monkeypatch, loops=2) == []


def test_accept_drift_cli_records_hashes_once(root):
    image = root / 'x.png'
    png(image)
    advance = root / '.codex_jobs' / '_batch' / 'advance.json'
    assert ps.main(['--root', str(root), 'accept-drift', '--note', 'ok', str(image)]) == 0
    assert ps.main(['--root', str(root), 'accept-drift', '--note', 'ok', str(image)]) == 0
    data = json.loads(advance.read_text())
    assert data['sets'] and [e['sha256'] for e in data['accepted_drift']] == [hashlib.sha256(image.read_bytes()).hexdigest()]


# ---------------------------------------------------------------- Windows port
windows_only = pytest.mark.skipif(not ps.IS_WIN, reason='needs the Windows implementation (msvcrt lock, real psutil suspend)')


def wingate(func):
    func.wingate = True   # keeps the real user_away dispatch (see the gnome_gate fixture)
    return func


def as_backslash(path):
    return str(path).replace('/', '\\')


def as_slash(path):
    return str(path).replace('\\', '/')


@pytest.mark.parametrize('style', [as_backslash, as_slash])
def test_gate2_matches_task_outputs_in_either_separator_style(root, style):
    # Task JSONs written on Windows carry backslash paths, older ones forward slashes: both must match the job and the view.
    job = make_job(root)
    for race in RACES:
        outputs = [style(job / race / 'outfit' / f'{v}.png') for v in VIEWS if v != 'front']
        batch = root / '.codex_jobs' / '_batch'
        write(batch / 'tasks' / f'views--{SET}--{race}--outfit.json', {'name': f'views--{SET}--{race}--outfit', 'outputs': outputs})
    sup = make_sup(root)
    sup.advance()
    assert sup.run_cmd.of('ready') and not [l for l in events(root) if 'HOLD' in l]


def test_view_reasons_finds_the_framing_record_through_backslashes(root):
    job = make_job(root)
    for race in RACES:
        outputs = [as_backslash(job / race / 'outfit' / f'{v}.png') for v in VIEWS if v != 'front']
        flags = {f'{v}.png': ['- FRAMING: scale 1.30'] for v in VIEWS if v != 'front'}
        write(root / '.codex_jobs' / '_batch' / 'tasks' / f'views--{SET}--{race}--outfit.json', {'outputs': outputs})
        write(root / '.codex_jobs' / '_batch' / 'status' / f'views--{SET}--{race}--outfit.json',
              {'state': 'done', 'checks': flags})
    sup = make_sup(root)
    reasons = sup.view_reasons(job, [sup.load_task(f'views--{SET}--{race}--outfit') for race in RACES])
    assert any('framing back.png: - FRAMING: scale 1.30' in r for r in reasons)
    assert not [r for r in reasons if 'no framing record' in r]


def test_front_reasons_accept_backslash_outputs(root):
    out = make_front(root)
    task = {'outputs': [as_backslash(out)]}
    sup = make_sup(root)
    assert sup.front_reasons(SET, 'dwarf_male', task, {}) == []


def test_read_json_reads_utf8_and_bom(tmp_path):
    path = tmp_path / 'a.json'
    path.write_bytes('﻿{"note": "café"}'.encode('utf-8'))
    assert ps.read_json(path) == {'note': 'café'}
    ps.atomic_write(path, {'note': 'café'})
    assert b'\r\n' not in path.read_bytes() and ps.read_json(path) == {'note': 'café'}


@wingate
def test_windows_gate_uses_idle_time(root, monkeypatch):
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    monkeypatch.setattr(ps.Supervisor, 'user_away', ps.Supervisor.away_windows)
    monkeypatch.setattr(ps, 'IS_WIN', True)
    idle = [4.0]
    monkeypatch.setattr(ps, 'win_idle_seconds', lambda: idle[0])
    sup = make_sup(root)
    assert sup.user_away() is False
    sup.tick_screen()
    assert json.loads(sup.pause_file.read_text())['reason'] == 'user active'
    idle[0] = ps.AWAY_AFTER - 1
    assert sup.user_away() is False
    idle[0] = ps.AWAY_AFTER
    sup.tick_screen()
    assert sup.user_away() is True and not sup.pause_file.exists()
    idle[0] = None   # GetLastInputInfo failed: keep the state, log once
    sup.tick_screen()
    sup.tick_screen()
    assert not sup.pause_file.exists() and sum('GetLastInputInfo failed' in l for l in events(root)) == 1
    idle[0] = 1.0
    sup.tick_screen()
    assert sup.pause_file.exists()


@wingate
@windows_only
def test_win_idle_seconds_reads_the_real_clock():
    value = ps.win_idle_seconds()
    assert value is not None and 0 <= value < 10 * 365 * 86400


def test_environment_on_windows_has_utf8_and_no_dbus(root, monkeypatch):
    monkeypatch.setattr(ps, 'IS_WIN', True)
    monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
    monkeypatch.delenv('PYTHONUTF8', raising=False)
    env = ps.Supervisor(root).environment()
    assert env['PYTHONUTF8'] == '1' and 'DBUS_SESSION_BUS_ADDRESS' not in env


@pytest.mark.skipif(ps.IS_WIN, reason='os.getuid and the DBus default are Linux only')
def test_environment_on_linux_keeps_the_dbus_default(root, monkeypatch):
    monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
    assert ps.Supervisor(root).environment()['DBUS_SESSION_BUS_ADDRESS'].startswith('unix:path=/run/user/')


class FakePsutil:
    class Error(Exception):
        pass

    class NoSuchProcess(Error):
        pass

    log = []
    missing = set()
    created = {}   # pid -> create time when it differs from table()'s 1000 + pid (a reused pid)

    class Process:
        def __init__(self, pid):
            if pid in FakePsutil.missing:
                raise FakePsutil.NoSuchProcess(pid)
            self.pid = pid

        def create_time(self):
            return FakePsutil.created.get(self.pid, 1000.0 + self.pid)

        def suspend(self):
            FakePsutil.log.append(('suspend', self.pid))

        def resume(self):
            FakePsutil.log.append(('resume', self.pid))


def test_windows_freeze_and_resume_use_psutil_suspend_resume(root, monkeypatch):
    FakePsutil.log, FakePsutil.missing = [], set()
    monkeypatch.setattr(ps, 'IS_WIN', True)
    monkeypatch.setattr(ps, 'psutil', FakePsutil)
    rows = table(root)
    sup = make_sup(root)
    frozen = sup.freeze(rows)
    assert sorted(r['pid'] for r in frozen) == [111, 112, 118, 120]
    assert sorted(p for a, p in FakePsutil.log if a == 'suspend') == [111, 112, 118, 120]
    recorded = json.loads(sup.pids_file.read_text())
    assert sorted(r['pid'] for r in recorded) == [111, 112, 118, 120] and all('start' in r for r in recorded)
    FakePsutil.log.clear()
    assert sup.resume(rows) == [111, 112, 118, 120]
    assert sorted(p for a, p in FakePsutil.log if a == 'resume') == [111, 112, 118, 120]
    assert not sup.pids_file.exists()


def test_windows_freeze_never_suspends_a_recorded_process_twice(root, monkeypatch):
    FakePsutil.log, FakePsutil.missing = [], set()
    monkeypatch.setattr(ps, 'IS_WIN', True)
    monkeypatch.setattr(ps, 'psutil', FakePsutil)
    sup = make_sup(root)
    sup.freeze(table(root))
    FakePsutil.log.clear()
    assert sup.freeze(table(root)) == []   # psutil still reports them as running; the record says they are frozen
    assert not [p for a, p in FakePsutil.log if a == 'suspend']
    assert sorted(r['pid'] for r in json.loads(sup.pids_file.read_text())) == [111, 112, 118, 120]


def test_windows_freeze_skips_a_process_that_vanished(root, monkeypatch):
    FakePsutil.log, FakePsutil.missing = [], {111}
    monkeypatch.setattr(ps, 'IS_WIN', True)
    monkeypatch.setattr(ps, 'psutil', FakePsutil)
    frozen = make_sup(root).freeze(table(root))
    assert sorted(r['pid'] for r in frozen) == [112, 118, 120]   # psutil.Error became OSError and was skipped


def test_heavy_processes_see_the_script_behind_windows_interpreter_options(monkeypatch):
    monkeypatch.setattr(ps, 'IS_WIN', True)
    rows = [
        {'pid': 1, 'ppid': 0, 'comm': 'python.exe', 'cmdline': ['python', '-X', 'utf8', '-u', 'tools/tripo_pipeline/convert_job.py', 'j'],
         'stopped': False, 'start': 1.0},
        {'pid': 2, 'ppid': 1, 'comm': 'blender.exe', 'cmdline': ['blender.exe', '-b'], 'stopped': False, 'start': 2.0},
        {'pid': 3, 'ppid': 1, 'comm': 'python.exe', 'cmdline': ['python', '-X', 'utf8', '-u', 'tools/tripo_pipeline/reduce_model.py'],
         'stopped': False, 'start': 3.0},
        {'pid': 4, 'ppid': 1, 'comm': 'node.exe', 'cmdline': ['node', 'tools/tripo_pipeline/studio.mjs'], 'stopped': False, 'start': 4.0},
    ]
    assert sorted(r['pid'] for r in ps.heavy_processes(rows)) == [2, 3]


@windows_only
def test_windows_suspend_is_reported_as_stopped_by_process_table():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        sup = ps.Supervisor(Path('.'))
        row = next(r for r in ps.process_table() if r['pid'] == child.pid)
        assert row['stopped'] is False
        sup.send(child.pid, ps.SIGSTOP)
        row = next(r for r in ps.process_table() if r['pid'] == child.pid)
        assert row['stopped'] is True and ps.same_process(row, {'pid': child.pid, 'start': row['start']})
        sup.send(child.pid, ps.SIGCONT)
        assert next(r for r in ps.process_table() if r['pid'] == child.pid)['stopped'] is False
        with pytest.raises(OSError):
            sup.send(2 ** 22 + 12345, ps.SIGSTOP)   # no such process: OSError like os.kill, so freeze() skips it
    finally:
        child.kill()
        child.wait()


@windows_only
def test_windows_lock_second_acquire_fails_and_status_reads_the_pid(root, capsys, monkeypatch):
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    monkeypatch.setattr(ps.Supervisor, 'user_away', lambda self: False)
    first, second = make_sup(root), make_sup(root)
    assert first.instance() == (False, '')   # nothing created yet
    handle = first.acquire()
    assert handle is not None
    try:
        assert second.acquire() is None
        assert first.instance() == (True, str(ps.os.getpid()))   # status does not break the lock and reads the pid
        assert second.acquire() is None
        second.status()
        assert f'supervisor: running (pid {ps.os.getpid()})' in capsys.readouterr().out
    finally:
        handle.close()
    assert second.instance()[0] is False
    again = second.acquire()
    assert again is not None
    again.close()


@windows_only
def test_windows_lock_held_by_another_process(root, monkeypatch):
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    code = ('import sys; sys.path.insert(0, r"%s"); import pipeline_supervisor as ps; '
            'h = ps.Supervisor(r"%s").acquire(); print(ps.os.getpid(), flush=True); sys.stdin.readline()'
            % (Path(ps.__file__).resolve().parent, root))
    child = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        pid = child.stdout.readline().strip()
        sup = make_sup(root)
        assert sup.acquire() is None
        assert sup.instance() == (True, pid)
    finally:
        child.communicate('\n', timeout=20)
    assert make_sup(root).instance()[0] is False


@windows_only
def test_stop_file_ends_the_loop_through_the_normal_exit_path(root, monkeypatch):
    import threading
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    monkeypatch.setattr(ps.Supervisor, 'user_away', lambda self: False)   # user active: PAUSE gets written
    sup = make_sup(root)
    seen = {}

    def request_stop():
        deadline = ps.time.time() + 15
        while not sup.pause_file.exists() and ps.time.time() < deadline:
            ps.time.sleep(0.1)
        seen['paused'] = sup.pause_file.exists()
        seen['code'] = ps.main(['--root', str(root), 'stop'])
    thread = threading.Thread(target=request_stop)
    thread.start()
    assert sup.run() == 0
    thread.join()
    assert seen['paused'] and seen['code'] == 0
    assert not sup.pause_file.exists() and not sup.stop_file.exists()   # released on exit
    assert any('SUPERVISOR STOP' in l for l in events(root))


@windows_only
def test_startup_resumes_processes_left_suspended_by_a_killed_instance(root, monkeypatch):
    import threading
    rows = table(root)
    monkeypatch.setattr(ps, 'process_table', lambda: rows)
    monkeypatch.setattr(ps.Supervisor, 'user_away', lambda self: True)
    sent = []
    monkeypatch.setattr(ps.Supervisor, 'send', lambda self, pid, sig, start=None: sent.append((pid, sig)))
    ps.atomic_write(root / '.codex_jobs' / '_batch' / 'paused_pids.json',
                    [{'pid': 111, 'start': rows[3]['start'], 'comm': 'blender'}])
    sup = make_sup(root)
    sup.stop_file.parent.mkdir(parents=True, exist_ok=True)
    sup.stop_file.write_text('stale')   # a stale stop request must not end the new instance immediately
    timer = threading.Timer(2.5, lambda: setattr(sup, 'stop', True))
    timer.start()
    assert sup.run() == 0
    timer.join()
    assert (111, ps.SIGCONT) in sent and any('RESUME at start' in l for l in events(root))


def test_stop_subcommand_when_nothing_runs(root, capsys):
    assert ps.main(['--root', str(root), 'stop']) == 0
    assert 'not running' in capsys.readouterr().out
    assert not (root / '.codex_jobs' / '_batch' / ps.STOP_FILE_NAME).exists()


def test_dry_run_does_not_write_auto_accepted_drift(root):
    make_job(root)
    before = (root / '.codex_jobs' / '_batch' / 'advance.json').read_bytes()
    sup = ps.Supervisor(root, dry_run=True)
    sup.run_cmd = Fake(root)
    sup.run_cmd.drift = {'front_three_quarter.png'}
    sup.advance()
    assert (root / '.codex_jobs' / '_batch' / 'advance.json').read_bytes() == before


# ---------------------------------------------------------------- review_v2_phase4 findings 7, 9 and the I.1 tick file
def other_checkout_rows(root, other):
    """This checkout's conversion (pids 1xx) beside another checkout's (pids 5xx, a stopped Blender among them)."""
    def row(pid, ppid, comm, *cmd, stopped=False, cwd=None):
        return {'pid': pid, 'ppid': ppid, 'comm': comm, 'cmdline': list(cmd), 'stopped': stopped, 'start': 1000.0 + pid,
                **({'cwd': str(cwd)} if cwd else {})}
    script = str(root / 'tools' / 'tripo_pipeline' / 'convert_job.py')
    other_script = str(other / 'tools' / 'tripo_pipeline' / 'convert_job.py')
    return [
        row(110, 1, 'python3', 'python3', script, 'job--1'),                                         # absolute, ours
        row(111, 110, 'blender', 'blender', '-b'),
        row(510, 1, 'python3', 'python3', other_script, 'job--2'),                                   # absolute, other
        row(511, 510, 'blender', 'blender', '-b', stopped=True),
        row(520, 1, 'python3', 'python3', 'tools/tripo_pipeline/convert_job.py', 'j', cwd=other),    # relative, other cwd
        row(521, 520, 'blender', 'blender', '-b'),
        row(530, 1, 'python3', 'python3', 'tools/tripo_pipeline/convert_job.py', 'j', cwd=root),     # relative, our cwd
        row(531, 530, 'python3', 'python3', 'tools/tripo_pipeline/reduce_model.py'),
        row(540, 1, 'python3', 'python3', 'tools/tripo_pipeline/convert_job.py', 'j'),               # relative, cwd unknown
        row(541, 540, 'blender', 'blender', '-b'),
    ]


def test_heavy_processes_only_match_this_checkouts_convert_job(root, tmp_path_factory, monkeypatch):
    other = tmp_path_factory.mktemp('other_checkout')
    monkeypatch.setattr(ps, 'psutil', None)          # a row without cwd cannot be resolved: it must not count
    monkeypatch.setattr(ps, 'IS_WIN', True)
    rows = other_checkout_rows(root, other)
    assert sorted(r['pid'] for r in ps.heavy_processes(rows, root)) == [111, 531]
    assert sorted(r['pid'] for r in ps.heavy_processes(rows)) == [111, 511, 521, 531, 541]   # no root: the old behaviour


def test_freeze_and_resume_never_touch_another_checkouts_processes(root, tmp_path_factory, monkeypatch):
    other = tmp_path_factory.mktemp('other_checkout')
    monkeypatch.setattr(ps, 'psutil', None)
    monkeypatch.setattr(ps, 'IS_WIN', True)
    rows, sent = other_checkout_rows(root, other), []
    sup = make_sup(root)
    monkeypatch.setattr(sup, 'send', lambda pid, sig, start=None: sent.append((pid, sig)))
    assert sorted(r['pid'] for r in sup.freeze(rows)) == [111, 531]
    assert sorted(p for p, s in sent if s == ps.SIGSTOP) == [111, 531]
    for row in rows:
        row['stopped'] = row['stopped'] or row['pid'] in (111, 531)
    rows.append({'pid': 532, 'ppid': 530, 'comm': 'blender', 'cmdline': ['blender'], 'stopped': True, 'start': 1532.0})
    sent.clear()
    assert sup.resume(rows) == [111, 531]            # only the recorded identities: not 511 (other checkout), not 532 (unrecorded)
    assert sorted(p for p, s in sent if s == ps.SIGCONT) == [111, 531]


def test_resume_at_start_continues_only_recorded_identities(root, monkeypatch):
    rows = table(root)
    for row in rows:
        row['stopped'] = row['pid'] in (111, 112, 118, 120)
    sent = []
    sup = make_sup(root)
    monkeypatch.setattr(sup, 'send', lambda pid, sig, start=None: sent.append((pid, sig)))
    ps.atomic_write(sup.pids_file, [{'pid': 111, 'start': 1111.0, 'comm': 'blender'},
                                    {'pid': 112, 'start': 5.0, 'comm': 'python3'}])   # 112: pid reused since
    assert sup.resume(rows) == [111]
    assert sent == [(111, ps.SIGCONT)]


def test_a_completed_tick_writes_the_tick_file(root, monkeypatch):
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    sup = make_sup(root)
    sup.run_cmd.screen = '(false,)\n'
    before = ps.time.time()
    assert sup.tick_screen() is True
    sup.write_tick()
    tick = json.loads(sup.tick_file.read_text())
    assert tick['pid'] == ps.os.getpid() and tick['paused'] is True and tick['mode'] == 'auto'
    assert before <= tick['time'] <= ps.time.time() and tick['at']
    if ps.psutil:
        assert abs(tick['create_time'] - ps.psutil.Process(ps.os.getpid()).create_time()) < 1
    dry = ps.Supervisor(root, dry_run=True)
    sup.tick_file.unlink()
    dry.write_tick()
    assert not sup.tick_file.exists()


def test_run_writes_the_tick_file_only_after_a_completed_tick(root, monkeypatch):
    import threading
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    monkeypatch.setattr(ps.Supervisor, 'user_away', lambda self: True)
    sup = make_sup(root)
    monkeypatch.setattr(sup, 'advance_async', lambda: None)
    timer = threading.Timer(1.5, lambda: setattr(sup, 'stop', True))
    timer.start()
    assert sup.run() == 0
    timer.join()
    assert json.loads(sup.tick_file.read_text())['pid'] == ps.os.getpid()
    broken = make_sup(root)
    sup.tick_file.unlink()
    monkeypatch.setattr(broken, 'tick_screen', lambda: 1 / 0)          # a failed tick writes no tick file
    monkeypatch.setattr(broken, 'advance_async', lambda: None)
    timer = threading.Timer(1.5, lambda: setattr(broken, 'stop', True))
    timer.start()
    assert broken.run() == 0
    timer.join()
    assert not sup.tick_file.exists()


def test_shutdown_waits_for_a_running_claim_child_before_releasing(root, monkeypatch):
    """Finding 9: a stop during gate 1's `workflow.py claim` (a child relying on this process's claim lock) must not release
    the claim lock, PAUSE or the single-instance lock until the claim returned."""
    from claim_lock import ClaimBusy, claim_lock
    monkeypatch.setattr(ps, 'process_table', lambda: [])
    monkeypatch.setattr(ps.Supervisor, 'user_away', lambda self: False)   # user active: PAUSE is written
    sup = gate1_ready(root)
    fake, seen = sup.run_cmd, {}

    def slow_claim(command, timeout=900):
        if ' claim ' in ' '.join(str(c) for c in command):
            sup.stop = True                     # the stop request arrives while the claim child runs
            ps.time.sleep(2.5)
            seen['instance_running'] = ps.Supervisor(root).instance()[0]
            seen['pause_present'] = sup.pause_file.exists()
            try:
                with claim_lock(root, SET, wait=0):
                    seen['claim_lock'] = 'free'
            except ClaimBusy:
                seen['claim_lock'] = 'held'
        return fake(command, timeout)
    sup.run_cmd = slow_claim
    assert sup.run() == 0
    assert seen == {'instance_running': True, 'pause_present': True, 'claim_lock': 'held'}
    assert (root / 'processing_views' / f'{SET}--abc123def456').is_dir()   # the claim completed
    lines = events(root)
    stop = next(i for i, l in enumerate(lines) if l.endswith('SUPERVISOR STOP'))
    assert any('STOPPING: waiting for the running advance pass' in l for l in lines[:stop])
    assert any('STOPPING: advance pass finished' in l for l in lines[:stop])
    assert not sup.pause_file.exists()
    assert ps.Supervisor(root).instance()[0] is False
    with claim_lock(root, SET, wait=0):
        pass


def test_drain_gives_up_on_a_pass_outside_a_claim_after_the_timeout(root):
    import threading
    sup = make_sup(root)
    release = threading.Event()
    sup.thread = threading.Thread(target=release.wait, args=(30,), daemon=True)
    sup.thread.start()
    try:
        assert sup.drain_advance(timeout=0.3, poll=0.1) is False
        sup.claim_active = 'some_set'                     # inside a claim the timeout does not apply
        threading.Timer(0.6, release.set).start()
        assert sup.drain_advance(timeout=0.1, poll=0.1) is True
    finally:
        release.set()
    assert any('outside any claim' in l for l in events(root))


def test_a_stop_ends_the_advance_pass_before_the_next_gate(root):
    sup = gate1_ready(root)
    sup.stop = True
    sup.advance()
    assert not sup.run_cmd.of('claim')


# ---------------------------------------------------------------- review_v2_phase4 finding 7 (re-check): entry point + signal identity
def test_script_of_is_the_script_the_interpreter_runs():
    def row(*cmd):
        return {'pid': 1, 'ppid': 0, 'comm': 'python.exe', 'cmdline': list(cmd), 'stopped': False, 'start': 1.0}
    assert ps.script_of(row('python', '-X', 'utf8', '-u', 'tools/tripo_pipeline/convert_job.py', 'j')) == 'tools/tripo_pipeline/convert_job.py'
    assert ps.script_of(row('python', '-Xutf8', '-uB', 'a.py')) == 'a.py'
    # an unrelated program given the checkout's script as DATA is not the conversion parent (the reviewer's case)
    assert ps.script_of(row('python', 'tools/other_tool.py', 'tools/tripo_pipeline/convert_job.py')) == 'tools/other_tool.py'
    assert ps.script_of(row('python', '-m', 'pytest', 'tools/tripo_pipeline/convert_job.py')) == ''
    assert ps.script_of(row('python', '-c', 'pass', 'tools/tripo_pipeline/convert_job.py')) == ''
    assert ps.script_of(row('python', '-', 'tools/tripo_pipeline/convert_job.py')) == ''
    assert ps.script_of(row('python')) == ''


def test_heavy_processes_ignore_convert_job_given_as_data(root):
    script = str(root / 'tools' / 'tripo_pipeline' / 'convert_job.py')

    def row(pid, ppid, comm, *cmd):
        return {'pid': pid, 'ppid': ppid, 'comm': comm, 'cmdline': list(cmd), 'stopped': False, 'start': 1000.0 + pid,
                'cwd': str(root)}
    rows = [row(10, 1, 'python3', 'python3', '-m', 'pytest', script),                   # -m: no script file runs
            row(11, 10, 'blender', 'blender', '-b'),
            row(20, 1, 'python3', 'python3', str(root / 'tools' / 'lint.py'), script),  # the script path is only an argument
            row(21, 20, 'blender', 'blender', '-b'),
            row(30, 1, 'python3', 'python3', '-X', 'utf8', script, 'job--1'),           # the real conversion parent
            row(31, 30, 'blender', 'blender', '-b')]
    assert [r['pid'] for r in ps.heavy_processes(rows, root)] == [31]


def test_freeze_verifies_the_identity_at_delivery(root, monkeypatch):
    FakePsutil.log, FakePsutil.missing, FakePsutil.created = [], set(), {112: 5.0}   # pid 112 now belongs to another process
    monkeypatch.setattr(ps, 'IS_WIN', True)
    monkeypatch.setattr(ps, 'psutil', FakePsutil)
    sup = make_sup(root)
    try:
        frozen = sup.freeze(table(root))
        assert sorted(r['pid'] for r in frozen) == [111, 118, 120]             # 112 not signalled, not recorded
        assert sorted(p for a, p in FakePsutil.log if a == 'suspend') == [111, 118, 120]
        assert sorted(r['pid'] for r in json.loads(sup.pids_file.read_text())) == [111, 118, 120]
        with pytest.raises(OSError):
            sup.send(112, ps.SIGSTOP, start=1112.0)
    finally:
        FakePsutil.created = {}


def test_resume_rechecks_identity_on_the_handle(root, monkeypatch):
    """The table still matched the recorded pid 111, but by delivery time the pid belongs to another process: not resumed."""
    FakePsutil.log, FakePsutil.missing, FakePsutil.created = [], set(), {}
    monkeypatch.setattr(ps, 'IS_WIN', True)
    monkeypatch.setattr(ps, 'psutil', FakePsutil)
    sup = make_sup(root)
    try:
        sup.freeze(table(root))
        FakePsutil.log.clear()
        FakePsutil.created = {111: 9.0}                                         # reused between the table read and the signal
        sup.resume(table(root))
        resumed = [p for a, p in FakePsutil.log if a == 'resume']
        assert 111 not in resumed and sorted(resumed) == [112, 118, 120]
    finally:
        FakePsutil.created = {}


def test_proc_cmdline_agrees_with_the_preview_apps_parser():
    import proc_cmdline
    sys.path.insert(0, str(Path(ps.__file__).resolve().parent / 'preview' / 'v2'))
    try:
        import services
    except Exception:   # noqa: BLE001  the preview app is optional here
        pytest.skip('tools/preview/v2/services.py not importable')
    finally:
        sys.path.pop(0)
    for cmd in (['python', 'a.py', 'x'], ['python', '-X', 'utf8', '-u', 'a.py'], ['python', '-Xutf8', 'a.py'], ['python', '-m', 'x'],
                ['python', '-c', 'x', 'a.py'], ['python', '-'], ['python'], ['python', '--help'], ['python', '-W', 'ignore', 'a.py'],
                ['python', '--check-hash-based-pycs', 'never', 'a.py'], ['python', '-uB', 'a.py'], ['python', '--', 'a.py']):
        assert proc_cmdline.python_script(cmd) == services.python_script(cmd), cmd
