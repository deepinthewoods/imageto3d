"""What a python command line actually runs (stdlib only; phase-4 review finding 7).

`python_entry(cmd)` parses argv the way the interpreter does: interpreter options (`-u`, `-B`, `-X utf8`, `-Xutf8`, `-W x`, combined
`-uB`, `--check-hash-based-pycs x`) are skipped; the first non-option is the script and everything after it belongs to the script.
`-c` / `-m` / `-` (stdin) / an unknown long option mean no script is run. Only argv[1:] is parsed (argv[0] is the interpreter).

    python_entry(['python', '-X', 'utf8', 'tools/x.py', 'a'])  -> ('script', 'tools/x.py', ['a'])
    python_entry(['python', '-m', 'pkg.mod', 'a'])             -> ('module', 'pkg.mod', ['a'])
    python_entry(['python', '-c', 'code', 'tools/x.py'])       -> ('command', 'code', ['tools/x.py'])   # x.py is only data

`python_script(cmd)` is the (script, args) form, with the same results as tools/preview/v2/services.python_script (the preview app's
service identification), which can import it from here.
"""
from __future__ import annotations

PY_ARG_OPTIONS = ('-X', '-W', '--check-hash-based-pycs')


def python_entry(cmd) -> tuple[str | None, str | None, list[str]]:
    """(kind, value, args): kind 'script' (value = the script path as written), 'module' (-m NAME), 'command' (-c CODE), 'stdin'
    (`-` or no operand), or None for an unparseable / help-like line."""
    cmd = list(cmd or [])
    i, n = 1, len(cmd)
    while i < n:
        arg = cmd[i]
        if arg == '--':
            i += 1
            break
        if not arg.startswith('-') or arg == '-':
            break
        if arg.startswith('--'):
            if arg in PY_ARG_OPTIONS:
                i += 2
                continue
            if arg.startswith('--check-hash-based-pycs='):
                i += 1
                continue
            return None, None, []
        body = arg[1:]
        for j, ch in enumerate(body):
            if ch in 'cm':
                rest = body[j + 1:]
                if rest:                                   # -mpkg / -cCODE
                    return ('module' if ch == 'm' else 'command'), rest, cmd[i + 1:]
                if i + 1 >= n:
                    return None, None, []
                return ('module' if ch == 'm' else 'command'), cmd[i + 1], cmd[i + 2:]
            if ch in 'XW':
                if j == len(body) - 1:                     # -X utf8: the value is the next argument
                    i += 1
                break
        i += 1
    if i >= n or cmd[i] == '-':
        return 'stdin', None, cmd[i + 1:] if i < n else []
    return 'script', cmd[i], cmd[i + 1:]


def python_script(cmd) -> tuple[str | None, list[str]]:
    """(script operand, its arguments) of a python command line; (None, []) when no script file is run."""
    kind, value, args = python_entry(cmd)
    return (value, args) if kind == 'script' else (None, [])
