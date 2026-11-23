#!/usr/bin/env python3
"""Move a finished set's high-poly and heavy working files from models/ to the archive drive.

    python3 tools/tripo_pipeline/offload_models.py [--root DIR] [--dest DIR] (--completed | --job NAME ... | --dir PATH ...) [--apply]

Rule (user, 2026-11-18): the SSD keeps a module's high-poly files only while its set is still being processed. Once a
set is complete (finish_sets.py moved its jobs to completed/), the conversion worker calls this for those jobs and the
files below move to `<dest>/<same path relative to models/>` (config.json `offload.dest`, the Toshiba drive):

  - working files: review.blend, prep.npz, source_geometry.npz, source_import.blend, highpoly*.blend, obj_*.zip
  - high-poly GLBs: source.glb and the backend downloads beside it (glb_*.glb, base_basic_pbr.glb)

Each file is copied, compared by SHA-256 and only then removed; names that are hard links to one file stay hard links
on the destination. A symlink is left at source.glb so the tools that read it keep working while the drive is plugged
in. Low-poly, wearable and piece GLBs, manifests and label arrays stay on the SSD. The default is a dry run. A missing
destination drive is reported and nothing is moved; the caller never fails because of it.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = Path(__file__).resolve().parent / 'config.json'
NAMES = ('review.blend', 'prep.npz', 'source_geometry.npz', 'source_import.blend', 'highpoly*.blend', 'obj_*.zip',
         'source.glb', 'base_basic_pbr.glb', 'glb_*.glb')
LINKED = ('source.glb',)   # a symlink to the moved file is left at these names
DERIVED = {'reduced', 'pieces', 'aligned'}   # their GLBs are the low-poly outputs; only exact working-file names move from them
WORKING = ('review.blend', 'source_import.blend', 'highpoly*.blend')


def settings(root=ROOT):
    """(enabled, dest) from config.json `offload`; disabled when the block is absent."""
    try:
        block = json.loads(CONFIG.read_text(encoding='utf-8-sig')).get('offload') or {}
    except (OSError, ValueError):
        block = {}
    return bool(block.get('enabled')), Path(block['dest']) if block.get('dest') else None


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(8 << 20), b''):
            h.update(block)
    return h.hexdigest()


def wanted(folder, name, top):
    inside = set(Path(folder).relative_to(top).parts)
    patterns = WORKING if inside & DERIVED else NAMES
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def groups_of(tops):
    """{(dev, inode): [paths]} of the real (non-symlink) files to move under the folders `tops`."""
    groups = {}
    for top in tops:
        for folder, _, files in os.walk(top):
            for name in files:
                path = Path(folder) / name
                if not wanted(folder, name, top) or path.is_symlink():
                    continue
                st = path.stat()
                groups.setdefault((st.st_dev, st.st_ino), []).append(path)
    return groups


def move_group(paths, models, dest):
    """Copy one file (all its hard-link names) to `dest`, verify, remove the originals, leave the source.glb symlink."""
    paths = sorted(paths, key=lambda p: (p.name not in LINKED, str(p)))
    first = paths[0]
    target = dest / first.relative_to(models)
    target.parent.mkdir(parents=True, exist_ok=True)
    want = digest(first)
    if not (target.is_file() and digest(target) == want):
        shutil.copy2(first, target)
        if digest(target) != want:
            raise OSError('sha256 mismatch after copy')
    targets = [target]
    for other in paths[1:]:
        twin = dest / other.relative_to(models)
        twin.parent.mkdir(parents=True, exist_ok=True)
        if twin.exists():
            if not os.path.samefile(twin, target) and digest(twin) != want:
                raise OSError(f'{twin} exists with other content')
        else:
            try:
                os.link(target, twin)
            except OSError:
                shutil.copy2(target, twin)
        targets.append(twin)
    for path, moved in zip(paths, targets):
        path.unlink()
        if path.name in LINKED:
            try:
                os.symlink(moved, path)
            except OSError:   # no symlink privilege: the file is still safe on the destination
                pass


def offload(tops, root=ROOT, dest=None, apply=False):
    """Move the heavy files under the model folders `tops`. Returns {'files', 'bytes', 'errors', 'skipped'}."""
    models = Path(root) / 'models'
    result = {'files': 0, 'bytes': 0, 'errors': [], 'skipped': None}
    if dest is None:
        dest = settings(root)[1]
    if dest is None:
        result['skipped'] = 'no offload.dest in config.json'
        return result
    dest = Path(dest)
    if not Path(dest.anchor).exists():
        result['skipped'] = f'destination drive {dest.anchor} is not available'
        return result
    tops = [Path(t) for t in tops if Path(t).is_dir()]
    for paths in sorted(groups_of(tops).values(), key=lambda p: str(p[0])):
        size = paths[0].stat().st_size
        if apply:
            try:
                move_group(paths, models, dest)
            except OSError as error:
                result['errors'].append(f'{paths[0]}: {error}')
                continue
        result['files'] += 1
        result['bytes'] += size
    return result


def job_dirs(root, names):
    """models/<set>/<job id> folders of the completed/ (or processing/) jobs `names`."""
    found = []
    for name in names:
        set_dir, _, job_dir = name.partition('--')
        record = {}
        for stage in ('completed', 'processing'):
            try:
                record = json.loads((Path(root) / stage / name / '.tripo-job.json').read_text(encoding='utf-8-sig'))
                break
            except (OSError, ValueError):
                continue
        found.append(Path(root) / 'models' / (record.get('set') or set_dir) / (record.get('job_id') or job_dir))
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--dest', type=Path, help='default: config.json offload.dest')
    parser.add_argument('--completed', action='store_true', help='every job in completed/')
    parser.add_argument('--job', action='append', default=[], help='a <set>--<job-id> job (repeatable)')
    parser.add_argument('--dir', action='append', default=[], type=Path, help='a folder under models/ (repeatable)')
    parser.add_argument('--apply', action='store_true', help='move the files (default: dry run)')
    args = parser.parse_args(argv)
    names = list(args.job)
    if args.completed:
        names += sorted(p.name for p in (args.root / 'completed').glob('*--*') if p.is_dir())
    tops = job_dirs(args.root, names) + list(args.dir)
    if not tops:
        parser.error('give --completed, --job or --dir')
    result = offload(tops, args.root, args.dest, args.apply)
    if result['skipped']:
        print(f"skipped: {result['skipped']}")
        return 1
    for error in result['errors']:
        print(f'ERROR {error}')
    print(f"{result['files']} file(s), {result['bytes'] / 1e9:.2f} GB {'moved' if args.apply else 'would move (dry run, use --apply)'}"
          f"; {len(result['errors'])} error(s)")
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
