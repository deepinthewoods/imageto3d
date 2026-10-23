"""Animation export for one base: chunked, resumable clip GLBs merged into anims.glb (SPEC §7).

Layout (inside `<batch>/bases/<base>/`): `work/<clip>/{clip.glb,clip.json}` checkpoints, then
`anims.glb`, `clips.json`, `cast.json`. `export_anims()` is a plain-python driver: it spawns one
`blender -b projects/<base>.blend` process per chunk of clips (each process reopens the base every
few clips to bound memory), skips clips whose `clip.json` still matches the source sha, spacing
settings and skeleton, and merges the lot with `native_glb.animations`. Casting clips without a
reviewed annotation are skipped with a warning. Runs headless too:
`python3 anims.py drive --base human_male --batch-dir <dir> [--chunk 40 --limit N --only a,b]`.
Also here: `load_clip()` retargets one FBX onto the open rig for the outfit-mode pose test.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import bpy
except ImportError:                                         # the driver and merge also run in plain python
    bpy = None

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]                                      # .../imageto3d
REPOSITORY = ROOT.parents[1]                                # .../godot/blender
SCRIPTS = REPOSITORY / 'scripts'
ANIMS = REPOSITORY / 'anims'
PROJECTS = ROOT / 'projects'
RPG_PREFIX = 'RPG Animations GLB-0.1.0/'
SPACING_DEFAULT = dict(arm_deg=8.0, leg_cm=3.0, overrides={})
REOPEN_EVERY = 10
VERSION = 1


# --- plain-python helpers ------------------------------------------------------------------------

def ce(name):
    """character_export submodule, importable without Blender for the pure modules."""
    import importlib
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module('character_export.' + name)


def _sibling(name):
    import importlib
    if __package__:
        return importlib.import_module('.' + name, __package__)
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    return importlib.import_module(name)


def dump_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, default=lambda v: float(v) if hasattr(v, '__float__') else list(v)) + '\n')
    os.replace(tmp, path)


def read_json(path):
    return json.loads(Path(path).read_text())


def profile_of(source):
    """'rpg' for FBX under `RPG Animations GLB-0.1.0/` (relative or absolute path), else 'mixamo'."""
    return 'rpg' if source.startswith(RPG_PREFIX) or '/' + RPG_PREFIX in str(source).replace('\\', '/') else 'mixamo'


def clip_rows(only=None, limit=None):
    """The clip catalogue (`batch.clips`, 1255 FBX) with a `set` (mixamo|rpg), optionally filtered by name/id/source/stem."""
    rows = ce('batch').clips(REPOSITORY)
    for r in rows:
        r['set'] = profile_of(r['source'])
    if only:
        want = set(only)
        rows = [r for r in rows if want & {r['id'], r['source'], r['source_name'], r['source_name'].removesuffix('_remap'), Path(r['source']).stem}]
        if not rows:
            raise ValueError('No clip matches ' + str(sorted(want)))
    return rows[:limit] if limit else rows


def folder_name(row):
    return re.sub(r'[^\w.-]+', '_', row['source_name'].removesuffix('_remap')).strip('_') + '__' + row['id'][:6]


def spacing_for(settings, row):
    """Effective {arm_deg, leg_cm} for one clip (per-clip override by source_name / stem)."""
    s = dict(settings)
    names = {row['source_name'], row['source_name'].removesuffix('_remap'), row['source'], Path(row['source']).stem}
    for key, over in settings.get('overrides', {}).items():
        if key in names or key.removesuffix('_remap') in names:
            s.update({k: over[k] for k in ('arm_deg', 'leg_cm') if k in over})
    return dict(arm_deg=round(float(s['arm_deg']), 4), leg_cm=round(float(s['leg_cm']), 4))


def _algo():
    """spacing.ALGO read from its source, so plain python (no bpy) agrees with the Blender side."""
    return int(re.search(r'^ALGO = (\d+)', (HERE / 'spacing.py').read_text(), re.M).group(1))


def settings_hash(settings, row):
    return ce('common').identity(dict(spacing=spacing_for(settings, row), profile=profile_of(row['source']), algo=_algo(), v=VERSION))


def up_to_date(folder, row, settings, skeleton=None):
    meta = Path(folder) / 'clip.json'
    if not meta.is_file() or not (Path(folder) / 'clip.glb').is_file():
        return False
    try:
        m = read_json(meta)
    except ValueError:
        return False
    return (m.get('sha256') == row['sha256'] and m.get('settings_hash') == settings_hash(settings, row)
            and (skeleton is None or m.get('skeleton') == skeleton))


def castable(row, annotations):
    """False for a casting clip with no reviewed, source-matching annotation (skipped with a warning)."""
    if not row.get('casting'):
        return True
    a = annotations.get(row['source_name'])
    return bool(a and a.get('status') == 'reviewed' and (not a.get('source_sha256') or a['source_sha256'] == row['sha256']))


def blender_exe(blender=None):
    exe = blender or os.environ.get('BLENDER') or (bpy.app.binary_path if bpy else None) or shutil.which('blender')
    if not exe:
        raise FileNotFoundError('blender executable not found (pass --blender)')
    return exe


def run_blender(exe, blend, script, argv, log):
    """Run `blender -b [blend] --python script -- argv` to completion, appending output to log; returns the exit code."""
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, '-b', '-noaudio'] + ([str(blend)] if blend else []) + ['--python-exit-code', '3', '--python', str(script), '--', *map(str, argv)]
    with open(log, 'ab') as stream:
        return subprocess.run(cmd, stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL).returncode


# --- driver --------------------------------------------------------------------------------------

def base_dir(base, batch_dir):
    return Path(batch_dir) / 'bases' / base


def progress(batch_dir, base):
    """{total, done, failed, state, current, updated} read from the checkpoint folder (cheap; for the panel)."""
    work = base_dir(base, batch_dir) / 'work'
    status = {}
    if (work / '_status.json').is_file():
        try:
            status = read_json(work / '_status.json')
        except ValueError:
            pass
    done = sum(1 for _ in work.glob('*/clip.json')) if work.is_dir() else 0
    failed = sum(1 for _ in work.glob('*/error.json')) if work.is_dir() else 0
    return dict(total=status.get('total', 0), done=done, failed=failed, state=status.get('state', 'idle'),
                current=status.get('current', ''), updated=status.get('updated', 0), merged=(base_dir(base, batch_dir) / 'anims.glb').is_file())


def resolve_settings(out, arm_deg=None, leg_cm=None):
    """Explicit values > base.json (the settings the base was exported with) > defaults."""
    s = dict(SPACING_DEFAULT)
    if (out / 'base.json').is_file():
        s.update({k: v for k, v in read_json(out / 'base.json').get('spacing', {}).items() if v is not None})
    if arm_deg is not None:
        s['arm_deg'] = arm_deg
    if leg_cm is not None:
        s['leg_cm'] = leg_cm
    return s


def export_anims(base, batch_dir, chunk=40, limit=None, only=None, resume=True, merge_after=True,
                 arm_deg=None, leg_cm=None, blender=None, reopen_every=REOPEN_EVERY, log=print):
    """Export (and merge) the clip library for one base into `batch_dir/bases/<base>/`; returns a summary dict."""
    t0 = time.time()
    out = base_dir(base, batch_dir)
    work = out / 'work'
    project = PROJECTS / (base + '.blend')
    if not project.is_file():
        raise FileNotFoundError(project)
    exe = blender_exe(blender)
    if not (out / 'base.glb').is_file() or not (out / 'base.json').is_file():
        log(f'[{base}] exporting base first')
        code = run_blender(exe, project, HERE / 'export_base.py', ['--base', base, '--out', out], work / '_logs' / 'base.log')
        if code or not (out / 'base.json').is_file():
            raise RuntimeError(f'base export failed (exit {code}); see {work / "_logs" / "base.log"}')
    skeleton = read_json(out / 'base.json')['skeleton_sha256']
    settings = resolve_settings(out, arm_deg, leg_cm)
    annotations = read_json(SCRIPTS / 'character_export' / 'cast_annotations.json')['clips']
    rows = clip_rows(only, limit)
    skipped_cast = [r['source_name'] for r in rows if not castable(r, annotations)]
    for name in skipped_cast:
        log(f'[{base}] WARNING skipping casting clip without reviewed annotation: {name}')
    rows = [r for r in rows if castable(r, annotations)]
    if not resume:
        for r in rows:
            shutil.rmtree(work / folder_name(r), ignore_errors=True)
    for r in rows:                                           # a stale failure record must not hide a retry
        (work / folder_name(r) / 'error.json').unlink(missing_ok=True)
    pending = [r for r in rows if not up_to_date(work / folder_name(r), r, settings, skeleton)]
    summary = dict(base=base, total=len(rows), up_to_date=len(rows) - len(pending), exported=0, failed={},
                   skipped_cast=skipped_cast, settings=settings, skeleton_sha256=skeleton)
    failed = set()
    index = 0

    def status(state, current=''):
        dump_json(work / '_status.json', dict(total=len(rows), state=state, current=current, updated=time.time(), failed=sorted(failed)))
    status('running')
    while pending:
        batch, pending = pending[:chunk], pending[chunk:]
        job = dict(base=base, project=str(project), work=str(work), rows=batch, settings=settings, skeleton=skeleton, reopen_every=reopen_every)
        dump_json(work / '_jobs' / f'chunk_{index:04d}.json', job)
        status('running', batch[0]['source_name'])
        log(f'[{base}] chunk {index}: {len(batch)} clips ({batch[0]["source_name"]} ...)')
        code = run_blender(exe, project, HERE / 'anims.py', ['chunk', work / '_jobs' / f'chunk_{index:04d}.json'], work / '_logs' / f'chunk_{index:04d}.log')
        index += 1
        left = [r for r in batch if not up_to_date(work / folder_name(r), r, settings, skeleton)]
        errored = [r for r in left if (work / folder_name(r) / 'error.json').is_file()]
        summary['exported'] += len(batch) - len(left)
        for r in errored:
            failed.add(r['source_name'])
            summary['failed'][r['source_name']] = read_json(work / folder_name(r) / 'error.json').get('error', '')
        crashed = [r for r in left if r not in errored]
        if crashed:                                          # worker died (exit != 0): quarantine the clip it was on, retry the rest
            bad = crashed[0]
            msg = f'worker exited {code} on this clip'
            dump_json(work / folder_name(bad) / 'error.json', dict(error=msg, source=bad['source']))
            failed.add(bad['source_name'])
            summary['failed'][bad['source_name']] = msg
            pending = crashed[1:] + pending
    status('merging')
    if merge_after:
        summary['merged'] = merge(base, batch_dir, rows=rows, log=log)
    status('done')
    summary['seconds'] = round(time.time() - t0, 2)
    return summary


def merge(base, batch_dir, rows=None, log=print):
    """Merge the finished clip GLBs into anims.glb and write clips.json + cast.json; returns a summary dict."""
    out = base_dir(base, batch_dir)
    work = out / 'work'
    skeleton = read_json(out / 'base.json')['skeleton_sha256']
    annotations = read_json(SCRIPTS / 'character_export' / 'cast_annotations.json')['clips']
    wanted = None if rows is None else {folder_name(r) for r in rows}
    items = []
    for folder in sorted(work.glob('*/clip.json')):
        if wanted is not None and folder.parent.name not in wanted:
            continue
        meta = read_json(folder)
        if meta.get('skeleton') != skeleton or not (folder.parent / 'clip.glb').is_file():
            log(f'[{base}] WARNING dropping {folder.parent.name}: skeleton differs from base.json')
            continue
        items.append((meta['source_name'], folder.parent / 'clip.glb', meta))
    items.sort(key=lambda t: t[0].lower())
    casting = ce('casting')
    kept, cast_rows = [], []
    for name, glb, meta in items:
        if meta.get('casting'):
            try:
                casting.manifest([meta], annotations)
            except ValueError as e:
                log(f'[{base}] WARNING casting clip left out of anims.glb: {e}')
                continue
            cast_rows.append(meta)
        kept.append((name, glb, meta))
    if not kept:
        return dict(clips=0)
    audit = ce('native_glb').animations(out / 'base.glb', [(n, g) for n, g, _ in kept], out / 'anims.glb')
    clips = {n: dict(start=m['time_min'], end=m['time_max'], fps=m['fps'], loop=m['loop'], source=m['source'], set=m['set'],
                     start_frame=m['start_frame'], end_frame=m['end_frame'], frames=m['frames'], casting=m['casting'])
             for n, _, m in kept}
    dump_json(out / 'clips.json', dict(version=1, base=base, skeleton_sha256=skeleton, clips=clips))
    dump_json(out / 'cast.json', casting.manifest(cast_rows, annotations))
    return dict(clips=len(kept), animations=audit['animations'], bytes=(out / 'anims.glb').stat().st_size, cast=len(cast_rows))


# --- Blender side: chunk worker ------------------------------------------------------------------

def _viewport():
    area = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
    return dict(area=area, region=next(r for r in area.regions if r.type == 'WINDOW'))


def retarget(rig, path, source_name):
    """Retarget one FBX onto the rig with ARP (root motion stays on the pelvis); returns the new action."""
    with bpy.context.temp_override(**_viewport()):
        return ce('standard_retarget').retarget_fbx(rig, Path(path), profile_of(str(path)), source_name.removesuffix('_remap'),
                                                    extract_root_motion=False)


def glb_times(doc):
    """(min, max) seconds over the animation input accessors of a clip GLB."""
    lo, hi = [], []
    for animation in doc['animations']:
        for sampler in animation['samplers']:
            a = doc['accessors'][sampler['input']]
            lo.append(a['min'][0])
            hi.append(a['max'][0])
    return min(lo), max(hi)


def export_clip(body, rig, row, folder, settings, skeleton=None):
    """Retarget, space, export `folder/clip.glb` and write `folder/clip.json` last (the completion marker)."""
    eb, spacing, arp = _sibling('export_base'), _sibling('spacing'), _sibling('arp_export')
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for stale in ('clip.json', 'error.json'):
        (folder / stale).unlink(missing_ok=True)
    mix = spacing_for(settings, row)
    t = time.time()
    action = retarget(rig, ANIMS / row['source'], row['source_name'])
    t_retarget = time.time() - t
    first, last = map(float, action.frame_range)
    fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
    stats = spacing.apply_spacing(rig, action, mix['arm_deg'], mix['leg_cm'])
    t = time.time()
    parts = ce('skin').split_head(body)
    try:
        audit = arp.export_glb(folder / 'clip.glb', rig, parts, animations=True)
    finally:
        for obj in parts:
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        body.hide_set(False)
        body.hide_render = False
    doc, _ = ce('gltf').read(folder / 'clip.glb')
    lo, hi = glb_times(doc)
    meta = {**row, 'fps': fps, 'start_frame': first, 'end_frame': last, 'frames': int(last - first) + 1, 'duration': (last - first) / fps,
            'time_min': lo, 'time_max': hi, 'skeleton': audit['skeleton_sha256'], 'legless': audit['legless'],
            'settings_hash': settings_hash(settings, row), 'spacing': mix, 'spacing_stats': stats,
            'timing': dict(retarget=round(t_retarget, 2), export=round(time.time() - t, 2)),
            'foot_surface_revision': action.get('ce_foot_surface_bake')}
    if skeleton and meta['skeleton'] != skeleton:
        raise ValueError(f"clip skeleton {meta['skeleton'][:12]} differs from base {skeleton[:12]}")
    dump_json(folder / 'clip.json', meta)
    # leave the session reusable
    rig.animation_data.action = None
    for a in [a for a in bpy.data.actions if a.name.startswith(row['source_name'].removesuffix('_remap'))]:
        bpy.data.actions.remove(a)
    eb.reset_pose(rig)
    return meta


def run_chunk(job_path):
    """Process one job file inside Blender: every clip in the chunk, reopening the base every `reopen_every` clips."""
    import traceback
    eb = _sibling('export_base')
    job = read_json(job_path)
    work, project, settings, skeleton = Path(job['work']), job['project'], job['settings'], job.get('skeleton')

    def fresh():
        body, rig = eb.open_base(project)
        eb.prepare_session(body, rig, morphs=False)
        return body, rig
    body, rig = fresh()
    since = 0
    for row in job['rows']:
        folder = work / folder_name(row)
        if up_to_date(folder, row, settings, skeleton):
            continue
        try:
            meta = export_clip(body, rig, row, folder, settings, skeleton)
            print('CLIP_DONE', row['source_name'], json.dumps(meta['timing']), flush=True)
        except Exception as e:
            traceback.print_exc()
            dump_json(folder / 'error.json', dict(error=f'{type(e).__name__}: {e}', source=row['source']))
            since = job.get('reopen_every', REOPEN_EVERY)
        since += 1
        if since >= job.get('reopen_every', REOPEN_EVERY):
            body, rig = fresh()
            since = 0


# --- pose-test helper ----------------------------------------------------------------------------

def load_clip(context, path, rig=None, arm_deg=None, leg_cm=None):
    """Retarget one FBX onto the open rig (with spacing) and assign it; returns the action.

    ARP's retarget rewrites rest poses in edit mode, which a library-override rig refuses, so for
    an override rig the clip is retargeted on the base project in a background Blender
    (`anims.py clip`), cached as a one-action .blend and appended here. A local rig is retargeted directly.
    """
    spacing = _sibling('spacing')
    path = Path(path)
    rig = rig or next((o for o in context.scene.objects if o.type == 'ARMATURE' and o.name.split('.')[0] == 'rig'), None) or context.object
    s = spacing.read_settings(context.scene, path.stem)
    arm_deg = s['arm_deg'] if arm_deg is None else arm_deg
    leg_cm = s['leg_cm'] if leg_cm is None else leg_cm
    name = path.stem
    if rig.override_library is None and rig.library is None:
        action = retarget(rig, path, name)
        spacing.apply_spacing(rig, action, arm_deg, leg_cm)
        return action
    root = spacing._root(context.scene)
    base = root.get('base')
    if not base or not (PROJECTS / (base + '.blend')).is_file():
        raise ValueError('Outfit project has no base name in scene["outfit_studio"]')
    key = hashlib.sha256(f'{path.resolve()}|{path.stat().st_mtime_ns}|{arm_deg}|{leg_cm}|{_algo()}'.encode()).hexdigest()[:16]
    cache = Path(tempfile.gettempdir()) / 'ostudio_clips' / base
    target = cache / (key + '.blend')
    if not target.is_file():
        code = run_blender(blender_exe(), PROJECTS / (base + '.blend'), HERE / 'anims.py',
                           ['clip', '--src', path, '--out', target, '--arm', arm_deg, '--leg', leg_cm], cache / (key + '.log'))
        if code or not target.is_file():
            raise RuntimeError(f'clip retarget failed (exit {code}); see {cache / (key + ".log")}')
    with bpy.data.libraries.load(str(target)) as (src, dst):
        dst.actions = list(src.actions)
    action = dst.actions[0]
    action.name = name + '_remap'
    action.use_fake_user = True
    if rig.data.pose_position != 'POSE':                    # linked override rigs report REST until switched
        rig.data.pose_position = 'POSE'
    ad = rig.animation_data or rig.animation_data_create()
    ad.action = action
    if getattr(ad, 'action_slot', None) is None and action.slots:
        ad.action_slot = action.slots[0]
    context.scene.frame_start, context.scene.frame_end = int(action.frame_range[0]), int(action.frame_range[1])
    return action


def run_single(argv):
    """`clip --src <fbx> --out <x.blend> [--arm d --leg cm]` inside Blender on a base project: retarget, space, save the action only."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--src', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--arm', type=float, default=None)
    p.add_argument('--leg', type=float, default=None)
    a = p.parse_args(argv)
    eb, spacing = _sibling('export_base'), _sibling('spacing')
    body, rig = eb.open_base()
    s = spacing.read_settings(bpy.context.scene, Path(a.src).stem)
    action = retarget(rig, a.src, Path(a.src).stem)
    spacing.apply_spacing(rig, action, s['arm_deg'] if a.arm is None else a.arm, s['leg_cm'] if a.leg is None else a.leg)
    action.use_fake_user = True
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    bpy.data.libraries.write(str(a.out), {action}, fake_user=True, compress=True)


# --- operator ------------------------------------------------------------------------------------

_STATE = dict(batch_dir=None, base=None)


if bpy:
    class OSTUDIO_OT_export_anims(bpy.types.Operator):
        bl_idname = 'ostudio.export_anims'
        bl_label = 'Export animations'
        bl_description = ('Retarget the clip library onto this base, apply spacing and merge anims.glb (background Blender processes, '
                          'resumable; the saved base file is used and this session is not modified)')
        batch: bpy.props.StringProperty(name='Batch', description='Export batch folder (empty = today, v1)')
        chunk: bpy.props.IntProperty(name='Clips per process', default=40, min=1, max=500)
        limit: bpy.props.IntProperty(name='Limit (0 = all)', default=0, min=0)
        only: bpy.props.StringProperty(name='Only clips', description='Comma-separated clip names (empty = all)')
        _proc = None
        _timer = None

        @classmethod
        def poll(cls, context):
            return bool(bpy.data.filepath) and 'base' in bpy.data.objects

        def invoke(self, context, event):
            return context.window_manager.invoke_props_dialog(self)

        def execute(self, context):
            base = Path(bpy.data.filepath).stem
            batch_dir = _sibling('export_base').default_out(base, self.batch or None).parents[1]
            argv = ['drive', '--base', base, '--batch-dir', batch_dir, '--chunk', self.chunk, '--blender', bpy.app.binary_path]
            if self.limit:
                argv += ['--limit', self.limit]
            if self.only.strip():
                argv += ['--only', self.only]
            _STATE.update(batch_dir=str(batch_dir), base=base)
            Path(batch_dir).mkdir(parents=True, exist_ok=True)
            cmd = [bpy.app.binary_path, '-b', '-noaudio', '--python-exit-code', '3', '--python', str(Path(__file__).resolve()), '--', *map(str, argv)]
            self._proc = subprocess.Popen(cmd, stdout=open(Path(batch_dir) / f'export_anims_{base}.log', 'ab'), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            self._timer = context.window_manager.event_timer_add(2.0, window=context.window)
            context.window_manager.modal_handler_add(self)
            self.report({'INFO'}, f'Animation export for {base} started in {batch_dir}')
            return {'RUNNING_MODAL'}

        def modal(self, context, event):
            if event.type != 'TIMER':
                return {'PASS_THROUGH'}
            if context.area:
                context.area.tag_redraw()
            code = self._proc.poll()
            if code is None:
                return {'PASS_THROUGH'}
            context.window_manager.event_timer_remove(self._timer)
            p = progress(_STATE['batch_dir'], _STATE['base'])
            self.report({'INFO' if code == 0 else 'ERROR'}, f"Animation export {'finished' if code == 0 else 'failed'}: {p['done']}/{p['total']} clips, {p['failed']} failed")
            return {'FINISHED' if code == 0 else 'CANCELLED'}

    CLASSES = (OSTUDIO_OT_export_anims,)

    def draw(layout, context):
        box = layout.box()
        box.label(text='Export animations')
        box.operator('ostudio.export_anims', icon='ACTION')
        if _STATE['batch_dir']:
            p = progress(_STATE['batch_dir'], _STATE['base'])
            box.label(text=f"{p['state']}: {p['done']}/{p['total']} clips, {p['failed']} failed")
            if p['current']:
                box.label(text=p['current'])
            if p['merged']:
                box.label(text='anims.glb written')


# --- entry point ---------------------------------------------------------------------------------

def main(argv=None):
    import argparse
    argv = list(sys.argv[sys.argv.index('--') + 1:] if argv is None and '--' in sys.argv else (argv if argv is not None else sys.argv[1:]))
    cmd, argv = (argv[0], argv[1:]) if argv else ('', [])
    if cmd == 'chunk':
        return run_chunk(argv[0])
    if cmd == 'clip':
        return run_single(argv)
    if cmd != 'drive':
        print('usage: anims.py drive --base B --batch-dir D [--chunk 40 --limit N --only a,b --arm deg --leg cm --blender exe --no-resume --no-merge]')
        return 2
    p = argparse.ArgumentParser()
    p.add_argument('--base', required=True)
    p.add_argument('--batch-dir', required=True)
    p.add_argument('--chunk', type=int, default=40)
    p.add_argument('--limit', type=int, default=None)
    p.add_argument('--only', default='')
    p.add_argument('--arm', type=float, default=None)
    p.add_argument('--leg', type=float, default=None)
    p.add_argument('--blender', default=None)
    p.add_argument('--reopen-every', type=int, default=REOPEN_EVERY)
    p.add_argument('--no-resume', action='store_true')
    p.add_argument('--no-merge', action='store_true')
    a = p.parse_args(argv)
    result = export_anims(a.base, a.batch_dir, a.chunk, a.limit, [x.strip() for x in a.only.split(',') if x.strip()] or None,
                          resume=not a.no_resume, merge_after=not a.no_merge, arm_deg=a.arm, leg_cm=a.leg, blender=a.blender,
                          reopen_every=a.reopen_every)
    print('EXPORT_ANIMS', json.dumps(result, default=str), flush=True)
    return 1 if result['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
