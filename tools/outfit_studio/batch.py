"""Batch entry points of Outfit Studio (SPEC section 8).

The module top is bpy-free, so `run.py` (plain python3) and the operators share `resolve_batch`, `command` and `run`.
`main()` is the in-Blender entry, one command per Blender process:

    blender -b --factory-startup --python-expr "import addon_utils; addon_utils.enable('outfit_studio', default_set=False); \
        import outfit_studio.batch as b; b.main()" -- <command> --base <b> [options]

Commands: import | validate | export-base | export-anims | export-items. Each returns a result dict, written to
`--result` as JSON, and the process exits 0 when `ok`, else 1."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths

COMMANDS = ('import', 'validate', 'export-base', 'export-anims', 'export-items')
BOOT = ("import addon_utils; addon_utils.enable('outfit_studio', default_set=False); "
        "import outfit_studio.batch as b; b.main()")


# ------------------------------------------------------------------------------------------------- shared (no bpy)

def resolve_batch(name=None, resume=False):
    """Batch folder: an absolute path / path with a slash as given, a bare name under EXPORT, or `YYYYMMDD-vN`
    (the next free N, or with `resume` the newest existing one of today)."""
    if name:
        return Path(name) if os.sep in str(name) or Path(name).is_absolute() else paths.export_dir(str(name))
    day = time.strftime('%Y%m%d')
    versions = sorted(int(p.name.rsplit('-v', 1)[1]) for p in paths.EXPORT.glob(f'{day}-v*') if p.name.rsplit('-v', 1)[1].isdigit())
    n = (versions[-1] if resume else versions[-1] + 1) if versions else 1
    return paths.export_dir(f'{day}-v{n}')


def blender_exe():
    exe = os.environ.get('BLENDER') or shutil.which('blender')
    if not exe:
        try:
            import bpy
            exe = bpy.app.binary_path
        except ImportError:
            pass
    if not exe:
        raise FileNotFoundError('blender executable not found (set $BLENDER)')
    return exe


def command(cmd, argv, exe=None):
    """argv of the Blender child process for `cmd`."""
    return [exe or blender_exe(), '-b', '--factory-startup', '-noaudio', '--python-exit-code', '1', '--python-expr', BOOT,
            '--', cmd, *map(str, argv)]


def spawn(cmd, argv, log, exe=None):
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(command(cmd, argv, exe), stdout=open(log, 'ab'), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)


def run(cmd, argv, log, exe=None):
    """Run a command to completion; returns the exit code."""
    p = spawn(cmd, argv, log, exe)
    return p.wait()


def project_file(base, project_dir=None):
    return Path(project_dir or paths.OUTFIT_PROJECTS) / f'{base}_outfits.blend'


# ------------------------------------------------------------------------------------------------- commands (in Blender)

def _open(path):
    import bpy
    from .blender import arp_export
    if not (bpy.data.filepath and Path(bpy.data.filepath).resolve() == Path(path).resolve()):
        bpy.ops.wm.open_mainfile(filepath=str(path))
    arp_export.enable_arp()


def cmd_import(a):
    """Create the outfit project when missing and import every new item of the pipeline for the base; edited items are
    never overwritten (importer puts a new import in `__reimport`). Saves the project."""
    import bpy
    from .blender import importer, items, project
    from .core import pipeline_source as ps
    entries = [e for e in ps.latest(ps.discover()) if e['race'] == a.base and (a.set == 'all' or a.set in (e['set_id'], e.get('source_set')))]
    out = {'base': a.base, 'imported': [], 'skipped': [], 'failed': [], 'created': False}
    if not entries:
        out['note'] = 'no converted pipeline output for this base / set'
        return out
    path = project_file(a.base, a.project_dir)
    if path.exists():
        _open(path)
    else:
        project.create_outfit_project(a.base, path)
        out['created'] = True
    ctx = bpy.context
    have = {items.item_data(c).get('item_id') for c in items.list_items(ctx.scene)}
    for e in entries:
        want = {t for t in ps.MODULE_ITEMS[e['module']] if ps.item_id(e['set_id'], t) not in have}
        if not want and not a.reimport:
            out['skipped'] += [ps.item_id(e['set_id'], t) for t in ps.MODULE_ITEMS[e['module']]]
            continue
        lod = a.lod if a.lod in e['levels'] else None
        try:
            if lod is None:
                raise ValueError(f"LOD {a.lod} not converted for {e['set_id']}/{e['module']} (has {e['levels']})")
            t0 = time.time()
            for r in importer.import_module(ctx, e, lod, only=(None if a.reimport else want)):
                out['imported'].append({**{k: r[k] for k in ('item_id', 'mode', 'tris', 'fit_median_pct', 'warnings')},
                                        'seconds': round(time.time() - t0, 2)})
        except Exception as ex:
            import traceback
            traceback.print_exc()
            out['failed'].append({'set_id': e['set_id'], 'module': e['module'], 'error': str(ex)})
    bpy.ops.wm.save_mainfile()
    out['file'] = str(path)
    return out


def _filter(ctx, a):
    """Item ids selected by --items / --set (None = no restriction)."""
    from .blender import items
    from .core import pipeline_source as ps
    ids = [x for x in (a.items or '').split(',') if x] or None
    if a.set != 'all':
        pool = ids or [items.item_data(c).get('item_id') for c in items.list_items(ctx.scene)]
        by_id = {items.item_data(c).get('item_id'): items.item_data(c) for c in items.list_items(ctx.scene)}
        ids = [i for i in pool if by_id.get(i, {}).get('set_id') == ps.canonical_set(a.set)]
    return ids


def cmd_validate(a):
    import bpy
    from .blender import validate
    path = project_file(a.base, a.project_dir)
    if not path.exists():
        raise FileNotFoundError(f'{path} does not exist (run import first)')
    _open(path)
    results = validate.validate(bpy.context, _filter(bpy.context, a), sibling_dir=Path(path).parent)
    s = validate.summary(results)
    for r in results:
        print(f"[validate] {r['level']:7} {r['item_id']}/{r['piece']}: {r['code']}: {r['message']}")
    return {'base': a.base, 'ok': s['errors'] == 0, **s, 'results': results}


def cmd_export_items(a):
    from .blender import export_items
    path = project_file(a.base, a.project_dir)
    if not path.exists():
        raise FileNotFoundError(f'{path} does not exist (run import first)')
    _open(path)
    import bpy
    ids = _filter(bpy.context, a)
    r = export_items.export_items(path, a.batch, ids, a.all_status, a.resume)
    r['ok'] = not r['failed']
    return r


def cmd_export_base(a):
    from .blender import export_base
    out = Path(a.batch) / 'bases' / a.base
    if (out / 'base.json').exists() and (out / 'base.glb').exists():
        if not a.resume:
            raise FileExistsError(f'{out} already has a base export (use --resume to skip it)')
        return {'base': a.base, 'status': 'skipped', 'reason': 'exists'}
    export_base.open_base(paths.base_project(a.base))
    return {'base': a.base, 'status': 'exported', **export_base.export_base(a.base, out)}


def cmd_export_anims(a):
    from .blender import anims
    r = anims.export_anims(a.base, a.batch, only=[x for x in (a.only or '').split(',') if x] or None, limit=a.limit)
    r['ok'] = not r.get('failed')
    return r


HANDLERS = {'import': cmd_import, 'validate': cmd_validate, 'export-base': cmd_export_base,
            'export-anims': cmd_export_anims, 'export-items': cmd_export_items}


def parser():
    p = argparse.ArgumentParser(prog='outfit_studio.batch')
    p.add_argument('command', choices=COMMANDS)
    p.add_argument('--base', required=True)
    p.add_argument('--set', default='all')
    p.add_argument('--lod', default='0p4')
    p.add_argument('--batch', default=None)
    p.add_argument('--resume', action='store_true')
    p.add_argument('--all-status', action='store_true')
    p.add_argument('--reimport', action='store_true', help='import: also re-run items that already exist (edited ones go to __reimport)')
    p.add_argument('--project-dir', default=None)
    p.add_argument('--items', default='')
    p.add_argument('--only', default='', help='export-anims: comma separated clip names')
    p.add_argument('--limit', type=int, default=None, help='export-anims: first N clips only')
    p.add_argument('--result', default=None)
    return p


def main(argv=None):
    """In-Blender entry: parse the arguments after `--`, run the command, write the result JSON, exit 0 (ok) / 1."""
    argv = list(argv) if argv is not None else sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:]
    a = parser().parse_args(argv)
    t0 = time.time()
    try:
        r = HANDLERS[a.command](a)
        r.setdefault('ok', not r.get('failed'))
    except Exception as e:
        import traceback
        traceback.print_exc()
        r = {'base': a.base, 'ok': False, 'failed': [{'error': f'{type(e).__name__}: {e}'}]}
    r.update(command=a.command, seconds=round(time.time() - t0, 2))
    if a.result:
        Path(a.result).parent.mkdir(parents=True, exist_ok=True)
        Path(a.result).write_text(json.dumps(r, indent=2, default=str) + '\n')
    print('BATCH_RESULT', json.dumps({k: v for k, v in r.items() if k != 'results'}, default=str), flush=True)
    sys.stdout.flush()
    sys.exit(0 if r['ok'] else 1)
