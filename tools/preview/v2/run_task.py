#!/usr/bin/env python3
"""Detached wrapper of one task ATTEMPT (DESIGN_PHASE3.md sections A and E).

    python tools/preview/v2/run_task.py <task json path> [--token <attempt token>]

The task file is `{"id", "attempt", "token", "argv": [...], "env": {...}, "log": path, "err": path, "result": path, "claim": path,
"cwd": path?, "task": {"key", "lane", "set", "unit", "args", "max_attempts"}}` (the paths are attempt-specific: `<id>-a<n>.log`, ...). The token is
also on the command line, so the orchestrator can find this process by scanning command lines after a crash.

1. FIRST the wrapper writes the claim file `{token, attempt, task, wrapper_pid, wrapper_create, builder_pid: null, ...}` (atomic): from then on the
   orchestrator knows this attempt is alive even if it crashed before recording the process.
2. It starts the command: stdout is appended to `log` (and scanned for the verdict), stderr goes to the separate `err` log (never parsed).
   Right after the start the claim gets `builder_pid` / `builder_create` (for Kill & retry).
3. On exit it writes `result` ATOMICALLY as `{token, attempt, exit, last_json, finished}` (`last_json` = the last STDOUT line that parses as a
   JSON object, `null` when there is none). The result is written on every path, also when the command could not even start (`exit` -1 and a
   `start_error`).
Heartbeat: the orchestrator watches the two logs grow; a build whose logs stop growing for `build_stall_minutes` is shown as stalled.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

try:
    import psutil
except ImportError:      # the claim then has no create time; the orchestrator falls back to its conservative checks
    psutil = None


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data), encoding='utf-8')
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def _json_object(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith('{'):
        return None
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def create_time(pid: int) -> float | None:
    if psutil is None:
        return None
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def _drain(proc, result: dict) -> None:
    """Read the builder's stdout to its end without the log (after a log write failed): the builder never blocks on a full pipe, and a verdict
    printed after the failure is still seen."""
    try:
        for raw in proc.stdout:
            found = _json_object(raw.decode('utf-8', 'replace'))
            if found is not None:
                result['last_json'] = found
    except (OSError, ValueError):
        pass


def run(spec: dict) -> dict:
    """Run the command of `spec`; returns the result record (main() writes it to spec['result']). Once the builder started, the result is only
    produced after the builder EXITED (phase 4 review F12): `builder_pid` / `builder_create` name it and `builder_exited` is true only after its
    wait() returned, so the orchestrator never frees a worker slot while the builder still runs."""
    result: dict = {'token': spec.get('token'), 'attempt': spec.get('attempt'), 'exit': -1, 'last_json': None, 'finished': None,
                    'builder_pid': None, 'builder_create': None, 'builder_exited': None}
    log_path = Path(spec['log'])
    err_path = Path(spec.get('err') or str(log_path)[:-4] + '.err.log')
    claim_path = Path(spec['claim']) if spec.get('claim') else None
    claim = {'token': spec.get('token'), 'attempt': spec.get('attempt'), 'task_id': spec.get('id'), 'task': spec.get('task'),
             'wrapper_pid': os.getpid(), 'wrapper_create': create_time(os.getpid()), 'builder_pid': None, 'builder_create': None,
             'log': str(log_path), 'err': str(err_path), 'result': spec.get('result'), 'argv': [str(a) for a in spec.get('argv') or []],
             'claimed': time.time()}
    proc = None
    try:
        if claim_path is not None:
            _write_atomic(claim_path, claim)                    # the first thing: this attempt is alive
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (spec.get('env') or {}).items()})
        env.setdefault('PYTHONUNBUFFERED', '1')
        with open(log_path, 'ab') as log, open(err_path, 'ab') as err:
            log.write(f'--- {time.strftime("%Y-%m-%d %H:%M:%S")} task {spec.get("id")} attempt {spec.get("attempt")}: '
                      f'{" ".join(map(str, spec["argv"]))}\n'.encode('utf-8', 'replace'))
            log.flush()
            # this wrapper has no console (it is started detached): without CREATE_NO_WINDOW Windows would open one per console child
            proc = subprocess.Popen([str(a) for a in spec['argv']], stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL,
                                    cwd=spec.get('cwd') or None, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result.update(builder_pid=proc.pid, builder_create=create_time(proc.pid))
            if claim_path is not None:
                for attempt in range(5):                        # the builder identity must reach the claim (Kill & retry, slot keeping)
                    try:
                        _write_atomic(claim_path, claim | {'builder_pid': proc.pid, 'builder_create': result['builder_create']})
                        break
                    except OSError as exc:
                        if attempt == 4:
                            result['claim_error'] = f'{type(exc).__name__}: {exc}'
                        time.sleep(0.2 * (attempt + 1))
            try:
                for raw in proc.stdout:
                    log.write(raw)
                    log.flush()
                    found = _json_object(raw.decode('utf-8', 'replace'))
                    if found is not None:
                        result['last_json'] = found
            except Exception as exc:  # noqa: BLE001  (a log write failed: keep the builder running to its end, never orphan it)
                result['log_error'] = f'{type(exc).__name__}: {exc}'
                _drain(proc, result)
            proc.stdout.close()
            result['exit'] = proc.wait()
            result['builder_exited'] = True
    except BaseException as exc:  # noqa: BLE001  (the result file is written on every path)
        result['start_error'] = f'{type(exc).__name__}: {exc}'
        if proc is not None and not result.get('builder_exited'):
            try:                                                # the builder started: the result waits for its exit
                _drain(proc, result)
                result['exit'] = proc.wait()
                result['builder_exited'] = True
            except BaseException:  # noqa: BLE001
                pass
        try:
            with open(log_path, 'ab') as log:
                log.write(f'wrapper error: {result["start_error"]}\n'.encode('utf-8', 'replace'))
        except OSError:
            pass
    if proc is None:
        result['builder_exited'] = True                         # no builder was ever started
    result['finished'] = time.time()
    return result


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 3 and argv[1] == '--token':
        argv = argv[:1]
    if len(argv) != 1:
        print('usage: run_task.py <task json> [--token <token>]', file=sys.stderr)
        return 2
    spec = json.loads(Path(argv[0]).read_text(encoding='utf-8-sig'))
    result = run(spec)
    _write_atomic(Path(spec['result']), result)
    return 0


if __name__ == '__main__':
    sys.exit(main())
