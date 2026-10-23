"""Base export: skeleton + body + head GLB and base.json for one base project (SPEC §7).

`export_base()` runs inside a Blender process that has the base project open and works on the
in-memory session only (weights repaired, head split, ARP export mutate it), so it never saves.
The `ostudio.export_base` operator therefore runs it in a background `blender -b` subprocess on
the SAVED base file, and the same entry point works headless:
`blender -b projects/<base>.blend --python export_base.py -- --out <dir> [--base <name>]`.
"""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import time

import bpy

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]                                      # .../imageto3d
PROJECTS = ROOT / 'projects'
EXPORT = ROOT.parents[1] / 'export' / 'outfit_studio'
SPACING_DEFAULT = dict(arm_deg=8.0, leg_cm=3.0)


def _arp():
    """The arp_export helper, whether this file runs as a package module or as a --python script."""
    try:
        from . import arp_export
    except ImportError:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import arp_export
    return arp_export


def _sibling(name):
    try:
        return __import__(__package__ + '.' + name, fromlist=['x']) if __package__ else __import__(name)
    except ImportError:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        return __import__(name)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def dump_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, default=lambda v: float(v) if hasattr(v, '__float__') else list(v)) + '\n')
    os.replace(tmp, path)


def reset_pose(rig):
    """Rest-pose, action-free session, as native_worker.load_source leaves it."""
    scene = bpy.context.scene
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    if rig.animation_data:
        rig.animation_data.action = None
        for track in list(rig.animation_data.nla_tracks):
            rig.animation_data.nla_tracks.remove(track)
    for pb in rig.pose.bones:
        pb.matrix_basis.identity()
    rig.data.pose_position = 'POSE'
    scene.frame_set(1)
    bpy.context.view_layer.update()


def open_base(path=None):
    """Enable ARP, (re)open a base project in this process and return (body, rig) at rest."""
    _arp().enable_arp()
    if path:
        bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False)
    body = bpy.data.objects['base']
    rig = body.find_armature()
    if rig is None or rig.get('ce_rig_workflow') != 'standard_arp_v1':
        raise ValueError('Expected a native ARP base project (rig ce_rig_workflow=standard_arp_v1)')
    reset_pose(rig)
    return body, rig


def rig_fingerprint(rig):
    """sha256 of sorted (bone, parent, rounded head/tail/roll-axis) for deform bones and sockets."""
    try:
        return _sibling('project').rig_fingerprint(rig)
    except Exception:
        pass
    sockets = _arp().ce('native_contract').SOCKETS
    rows = []
    for b in rig.data.bones:
        if b.use_deform or b.name in sockets:
            rows.append([b.name, b.parent.name if b.parent else None,
                         [round(v, 5) for v in b.head_local], [round(v, 5) for v in b.tail_local],
                         [round(v, 5) for v in b.matrix_local.to_3x3().col[2]]])
    return hashlib.sha256(json.dumps(sorted(rows), separators=(',', ':')).encode()).hexdigest()


def read_spacing(scene):
    try:
        return _sibling('spacing').read_settings(scene)
    except Exception:
        return dict(SPACING_DEFAULT)


def ensure_morphs(body, rig, ce):
    """Head (and, if the project has none, bulk) shape keys; returns the non-Basis key names."""
    keys = lambda: [k.name for k in body.data.shape_keys.key_blocks[1:]] if body.data.shape_keys else []
    if not any(k.startswith('head_') for k in keys()):
        ce('head_morphs').install(body)
    if not any(k.startswith('bulk_') for k in keys()):
        ce('native_morphs').body_bulk_keys(body, rig)
    return keys()


def attachment_points(body, rig, ce):
    """AP surface bindings (+ morph offsets); re-fits the authored contract if the weight repair moved a dominant bone."""
    restore = ce('native_attachment_restore')
    refit = None
    try:
        points = restore.bindings(body, rig)
    except ValueError as e:
        refit = str(e)
        restore.refit_authored(body, rig)
        points = restore.bindings(body, rig)
    ce('native_morphs').attachment_offsets(body, points)
    return points, refit


def prepare_session(body, rig, morphs=True):
    """In-memory export prep shared by base and clip exports: weight repair, morphs, AP bindings.
    Returns (repaired {name: [vertex ids]}, morph names, attachment bindings, refit message|None)."""
    arp = _arp()
    fixed = arp.fix_weights([body], rig)                           # top-4 + normalise, in memory only
    names = ensure_morphs(body, rig, arp.ce) if morphs else []
    points, refit = attachment_points(body, rig, arp.ce)
    return fixed, names, points, refit


def export_base(base_name, out_dir, project=None):
    """Write out_dir/base.glb + base.json from the open session (never saves the .blend); returns a summary dict."""
    arp = _arp()
    ce = arp.ce
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    body = bpy.data.objects['base']
    rig = body.find_armature()
    reset_pose(rig)
    spacing = read_spacing(bpy.context.scene)
    fixed, morphs, points, refit = prepare_session(body, rig)
    parts = ce('skin').split_head(body)
    glb = out / 'base.glb'
    audit = arp.export_glb(glb, rig, parts)
    morph_audit = ce('native_morphs').audit_glb(glb, morphs)
    doc, _ = ce('gltf').read(glb)
    joints = [doc['nodes'][i]['name'] for i in doc['skins'][0]['joints']]
    socket_map = ce('native_contract').SOCKETS
    missing = [s for s in socket_map if s not in joints]
    if missing:
        raise ValueError('Exported skeleton lacks sockets: ' + str(missing))
    project = Path(project or bpy.data.filepath)
    record = dict(version=1, base=base_name, skeleton_sha256=audit['skeleton_sha256'], bones=audit['bones'],
                  joints=joints, sockets=[dict(name=n, parent=p) for n, p in socket_map.items()],
                  attachments=points, morphs=morphs, base_body='body', default_head='head',
                  legless=bool(audit['legless']), spacing=dict(arm_deg=spacing['arm_deg'], leg_cm=spacing['leg_cm'], overrides=spacing.get('overrides', {})),
                  source=dict(project=str(project.relative_to(ROOT)) if ROOT in project.parents else str(project),
                              sha256=sha256(project) if project.is_file() else None,
                              rig_fingerprint=rig_fingerprint(rig)),
                  weights_repaired={k: len(v) for k, v in fixed.items()}, attachments_refit=refit,
                  morph_max_displacement={m['mesh']: m['max_displacement'] for m in morph_audit['meshes']})
    dump_json(out / 'base.json', record)
    return dict(base=base_name, glb=str(glb), json=str(out / 'base.json'), skeleton_sha256=record['skeleton_sha256'],
                bones=record['bones'], aps=len(points), sockets=len(record['sockets']), morphs=len(morphs),
                legless=record['legless'], repaired_vertices=len(fixed.get('base', [])), refit=bool(refit))


def spawn(blend, script, argv, log, extra=()):
    """Start `blender -b blend --python script -- argv` detached from this session; returns the Popen."""
    exe = bpy.app.binary_path if bpy.app.binary_path else 'blender'
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    stream = open(log, 'ab')
    return subprocess.Popen([exe, '-b', '-noaudio', str(blend), *extra, '--python-exit-code', '3', '--python', str(script), '--', *map(str, argv)],
                            stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)


def default_out(base_name, batch=None):
    return EXPORT / (batch or time.strftime('%Y%m%d') + '-v1') / 'bases' / base_name


def main(argv=None):
    """Headless entry: `-- --out <dir> [--base <name>]` on the already opened base file."""
    import argparse
    args = sys.argv[sys.argv.index('--') + 1:] if argv is None and '--' in sys.argv else (argv or [])
    p = argparse.ArgumentParser()
    p.add_argument('--base')
    p.add_argument('--out', required=True)
    a = p.parse_args(args)
    base = a.base or Path(bpy.data.filepath).stem
    open_base()
    result = export_base(base, a.out)
    dump_json(Path(a.out) / '.export_base.done', result)
    print('EXPORT_BASE', json.dumps(result), flush=True)
    return result


# --- operator -----------------------------------------------------------------------------------

class OSTUDIO_OT_export_base(bpy.types.Operator):
    bl_idname = 'ostudio.export_base'
    bl_label = 'Export base'
    bl_description = ('Export base.glb + base.json from the saved base project in a background Blender '
                      '(this session is not modified; unsaved changes are not included)')
    batch: bpy.props.StringProperty(name='Batch', description='Export batch folder (empty = today, v1)')
    overwrite: bpy.props.BoolProperty(name='Overwrite', default=False)
    _proc = None
    _timer = None
    _out = None

    @classmethod
    def poll(cls, context):
        return bool(bpy.data.filepath) and 'base' in bpy.data.objects

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        base = Path(bpy.data.filepath).stem
        out = default_out(base, self.batch or None)
        if (out / 'base.json').exists() and not self.overwrite:
            self.report({'WARNING'}, f'{out} already has a base export (tick Overwrite)')
            return {'CANCELLED'}
        if bpy.data.is_dirty:
            self.report({'WARNING'}, 'File has unsaved changes; exporting the saved version')
        (out / '.export_base.done').unlink(missing_ok=True)
        self._out = out
        self._proc = spawn(bpy.data.filepath, Path(__file__).resolve(), ['--base', base, '--out', out], out / 'export_base.log')
        self._timer = context.window_manager.event_timer_add(1.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        self.report({'INFO'}, f'Exporting {base} to {out} ...')
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        code = self._proc.poll()
        if code is None:
            return {'PASS_THROUGH'}
        context.window_manager.event_timer_remove(self._timer)
        done = self._out / '.export_base.done'
        if code == 0 and done.exists():
            r = json.loads(done.read_text())
            self.report({'INFO'}, f"Base exported: {r['bones']} bones, {r['aps']} AP, sha {r['skeleton_sha256'][:10]}")
            return {'FINISHED'}
        self.report({'ERROR'}, f'Base export failed (exit {code}); see {self._out / "export_base.log"}')
        return {'CANCELLED'}


CLASSES = (OSTUDIO_OT_export_base,)


def draw(layout, context):
    box = layout.box()
    box.label(text='Export base')
    row = box.row()
    row.enabled = bool(bpy.data.filepath)
    row.operator('ostudio.export_base', icon='EXPORT')
    if not bpy.data.filepath or bpy.data.is_dirty:
        box.label(text='Save the file first: the saved copy is exported.', icon='INFO')


if __name__ == '__main__':
    main()
