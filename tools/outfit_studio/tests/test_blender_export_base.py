"""Headless tests for blender/export_base.py, anims.py and spacing.py.

Run `python3 tests/test_blender_export_base.py [step ...]` (steps: base, spacing, override, clips;
default all). The orchestrator drives `blender -b` subprocesses; the same file is the in-Blender
worker (`blender -b <project> --python this.py -- <step> <out.json>`). Everything is written under
$OSTUDIO_TEST_OUT (default: <tmp>/ostudio_test_export_base); nothing under projects/ or models/ is saved.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
BLENDER_DIR = HERE.parent / 'blender'
ROOT = HERE.parents[2]
REPO = ROOT.parents[1]
PROJECTS = ROOT / 'projects'
OUT = Path(os.environ.get('OSTUDIO_TEST_OUT', Path(tempfile.gettempdir()) / 'ostudio_test_export_base'))
EXE = os.environ.get('BLENDER', 'blender')
MIXAMO = ['Idle (5)', 'Warrior Idle (1)', 'Wide Arm Spell Casting']
RPG = ['RPG-Character@Dagger-Attack-L1', 'RPG-Character@Unarmed-Idle', 'RPG-Character@Unarmed-Idle-Injured']

try:
    import bpy
except ImportError:
    bpy = None


# =====================================================================================================
# in-Blender workers
# =====================================================================================================

def _w_mods():
    sys.path.insert(0, str(BLENDER_DIR))
    import anims, arp_export, export_base, spacing
    return anims, arp_export, export_base, spacing


def _sample(rig, spacing, frames):
    """Per frame: hips-relative outward hand offset/distance and lateral foot positions (m), world space."""
    sc = bpy.context.scene
    rows = {}
    for f in frames:
        sc.frame_set(f)
        ev = rig.evaluated_get(bpy.context.evaluated_depsgraph_get()).pose.bones
        w = rig.matrix_world
        hips = w @ ev['c_root_master.x'].head
        lat = spacing._lateral(rig)
        r = dict(abd_l=spacing.abduction_deg(rig, 'c_arm_fk.l'), abd_r=spacing.abduction_deg(rig, 'c_arm_fk.r'))
        for s, sign in (('l', 1.0), ('r', -1.0)):
            hand = w @ ev['c_hand_fk.' + s].head
            r['hand_out_' + s] = float((hand - hips).dot(lat) * sign)
            r['hand_dist_' + s] = float((hand - hips).length)
            r['hand_' + s] = list(hand)
            r['foot_lat_' + s] = float((w @ ev['foot.' + s].head).dot(lat))
            r['ik_lat_' + s] = float((w @ ev['c_foot_ik.' + s].head).dot(lat))
        rows[f] = r
    return rows


def w_spacing(out):
    anims, arp, eb, spacing = _w_mods()
    result = {}
    body, rig = eb.open_base(PROJECTS / 'human_male.blend')
    for clip in ('Idle (5)', 'Wide Arm Spell Casting'):
        action = anims.retarget(rig, anims.ANIMS / (clip + '.fbx'), clip)
        f0, f1 = map(int, action.frame_range)
        frames = list(range(f0, f1 + 1, 4 if 'Wide' in clip else max(1, (f1 - f0) // 8)))
        before = _sample(rig, spacing, frames)
        t = time.time()
        spaced = action.copy()
        spaced.name = clip + '__spaced'
        stats = spacing.apply_spacing(rig, spaced, 8.0, 3.0)
        seconds = time.time() - t
        after = _sample(rig, spacing, frames)
        zero = action.copy()
        spacing.apply_spacing(rig, zero, 0.0, 0.0)                     # a no-op: must not touch the keys
        spacing._assign(rig, zero)
        same = _sample(rig, spacing, frames)
        result[clip] = dict(frames=frames, before=before, after=after, zero=same, stats=stats, seconds=seconds)
        spacing._assign(rig, action)
    # legless: surface feet must be re-baked
    body, rig = eb.open_base(PROJECTS / 'gnome_female.blend')
    action = anims.retarget(rig, anims.ANIMS / 'Idle (5).fbx', 'Idle (5)')
    sc = bpy.context.scene
    def surface():
        sc.frame_set(20)
        ev = rig.evaluated_get(bpy.context.evaluated_depsgraph_get()).pose.bones
        return {s: list(ev['c_surface_foot.' + s].head) for s in 'lr'}, {s: list(ev['c_foot_ik.' + s].head) for s in 'lr'}
    s0, ik0 = surface()
    stats = spacing.apply_spacing(rig, action, 8.0, 3.0)
    s1, ik1 = surface()
    result['gnome'] = dict(stats=stats, surface_before=s0, surface_after=s1, ik_before=ik0, ik_after=ik1, legless=bool(rig.get('ce_legless')))
    # settings round trip keeps other keys
    sc['outfit_studio'] = json.dumps(dict(base='x', ap=dict(a=1)))
    spacing.write_settings(sc, arm_deg=5.0)
    spacing.write_settings(sc, leg_cm=2.0, overrides={'Idle (5)': dict(arm_deg=0.0)})
    result['settings'] = dict(raw=json.loads(sc['outfit_studio']), idle=spacing.read_settings(sc, 'Idle (5)_remap'), other=spacing.read_settings(sc, 'Run'))
    Path(out).write_text(json.dumps(result))


def w_inspect(out):
    anims, arp, eb, spacing = _w_mods()
    result = {}
    for base in ('human_male', 'gnome_female', 'dwarf_male'):
        body, rig = eb.open_base(PROJECTS / (base + '.blend'))
        result[base] = dict(
            aps=sum(1 for b in rig.data.bones if b.name.startswith('AP_')),
            morphs=[k.name for k in body.data.shape_keys.key_blocks[1:]],
            legless=bool(rig.get('ce_legless')),
            fingerprint=eb.rig_fingerprint(rig))
    Path(out).write_text(json.dumps(result))


def w_override(out):
    """Library-override rig: does ARP retarget work directly, and does the load_clip fallback?"""
    anims, arp, eb, spacing = _w_mods()
    result = {}
    arp.enable_arp()
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    sc = bpy.context.scene
    path = str(PROJECTS / 'human_male.blend')
    with bpy.data.libraries.load(path, link=True) as (src, dst):
        dst.collections = ['Character']
    coll = dst.collections[0]
    sc.collection.children.link(coll)
    root = coll.override_hierarchy_create(sc, bpy.context.view_layer)
    rig = next(o for o in bpy.data.objects if o.type == 'ARMATURE' and o.override_library)
    result['override'] = bool(rig.override_library)
    sc['outfit_studio'] = json.dumps(dict(base='human_male'))
    try:
        bpy.context.view_layer.objects.active = rig
        anims.retarget(rig, anims.ANIMS / 'Idle (5).fbx', 'Idle (5)')
        result['direct'] = 'ok'
    except Exception as e:
        result['direct'] = f'{type(e).__name__}: {str(e)[:200]}'
    t = time.time()
    try:
        action = anims.load_clip(bpy.context, anims.ANIMS / 'Idle (5).fbx', rig=rig)
        result['fallback'] = dict(action=action.name, frames=list(action.frame_range), assigned=rig.animation_data.action == action,
                                  seconds=time.time() - t)
        for key, frame in (('hand_l', 30), ('hand_l_f1', 1)):
            bpy.context.scene.frame_set(frame)
            ev = rig.evaluated_get(bpy.context.evaluated_depsgraph_get()).pose.bones
            result['fallback'][key] = list(rig.matrix_world @ ev['c_hand_fk.l'].head)
    except Exception as e:
        import traceback
        result['fallback'] = f'{type(e).__name__}: {e}\n' + traceback.format_exc()[-800:]
    Path(out).write_text(json.dumps(result))


def blender_main():
    step, out = sys.argv[sys.argv.index('--') + 1:][:2]
    {'spacing': w_spacing, 'inspect': w_inspect, 'override': w_override}[step](out)


# =====================================================================================================
# orchestrator (plain python)
# =====================================================================================================

FAILS = []


def check(cond, msg):
    print(('  ok   ' if cond else '  FAIL ') + msg, flush=True)
    if not cond:
        FAILS.append(msg)


def blender(args, log, blend=None):
    cmd = [EXE, '-b', '-noaudio'] + ([str(blend)] if blend else []) + ['--python-exit-code', '3', *args]
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    with open(log, 'ab') as stream:
        return subprocess.run(cmd, stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL).returncode


def worker(step, blend=None):
    out = OUT / f'{step}.json'
    out.unlink(missing_ok=True)
    code = blender(['--python', str(Path(__file__).resolve()), '--', step, str(out)], OUT / f'{step}.log', blend)
    check(code == 0 and out.is_file(), f'worker {step} exit {code}')
    return json.loads(out.read_text()) if out.is_file() else {}


def export_base_proc(base, out):
    t = time.time()
    code = blender(['--python', str(BLENDER_DIR / 'export_base.py'), '--', '--out', str(out)], OUT / 'logs' / f'base_{base}.log', PROJECTS / (base + '.blend'))
    return code, time.time() - t


def glb(path):
    sys.path.insert(0, str(REPO / 'scripts'))
    from character_export.gltf import read
    return read(path)


def test_base():
    print('== base export')
    info = worker('inspect')
    hashes = {}
    for base, tag in (('human_male', 'a'), ('human_male', 'b'), ('gnome_female', 'a'), ('dwarf_male', 'a')):
        out = OUT / 'bases' / f'{base}_{tag}'
        shutil.rmtree(out, ignore_errors=True)
        code, seconds = export_base_proc(base, out)
        check(code == 0 and (out / 'base.glb').is_file() and (out / 'base.json').is_file(), f'{base}/{tag} exported in {seconds:.1f}s (exit {code})')
        if code:
            continue
        rec = json.loads((out / 'base.json').read_text())
        doc, _ = glb(out / 'base.glb')
        joints = [doc['nodes'][i]['name'] for i in doc['skins'][0]['joints']]
        hashes[(base, tag)] = rec['skeleton_sha256']
        want = info[base]
        check(rec['version'] == 1 and rec['base'] == base, f'{base}: version/base fields')
        check(len(rec['skeleton_sha256']) == 64 and rec['bones'] == len(joints), f"{base}: {rec['bones']} bones == {len(joints)} joints")
        check(all(s['name'] in joints and s['parent'] in joints for s in rec['sockets']) and len(rec['sockets']) == 10, f"{base}: {len(rec['sockets'])} sockets are joints")
        aps = [j for j in joints if j.startswith('AP_')]
        check(len(rec['attachments']) == len(aps) == want['aps'], f"{base}: {len(rec['attachments'])} attachments == {len(aps)} AP joints == {want['aps']} rig AP bones")
        one = next(iter(rec['attachments'].values()))
        check({'triangle', 'vertices', 'barycentric', 'weights', 'bone', 'morph_offsets'} <= set(one), f'{base}: attachment record shape')
        check(rec['morphs'] == want['morphs'], f"{base}: morphs {len(rec['morphs'])} == project shape keys")
        names = {n for m in doc['meshes'] for n in m.get('extras', {}).get('targetNames', [])}
        check(set(rec['morphs']) <= names, f'{base}: morphs present as glTF targets')
        check(rec['legless'] == want['legless'], f"{base}: legless {rec['legless']}")
        check(rec['base_body'] == 'body' and rec['default_head'] == 'head' and {'arm_deg', 'leg_cm'} <= set(rec['spacing']), f'{base}: body/head/spacing fields')
        check(rec['source']['sha256'] == hashlib.sha256((PROJECTS / (base + '.blend')).read_bytes()).hexdigest() and rec['source']['rig_fingerprint'] == want['fingerprint'], f'{base}: source sha + rig fingerprint')
        meshes = {n['name'] for n in doc['nodes'] if 'mesh' in n}
        check({'body', 'head'} <= meshes, f'{base}: body + head meshes')
        print(f"       {base}: sha {rec['skeleton_sha256'][:12]} bones {rec['bones']} aps {len(aps)} morphs {len(rec['morphs'])} repaired {rec['weights_repaired']} refit {rec['attachments_refit']}")
    check(hashes.get(('human_male', 'a')) == hashes.get(('human_male', 'b')) is not None, 'human_male skeleton hash identical across two runs')
    check(len({hashes.get(('human_male', 'a')), hashes.get(('dwarf_male', 'a')), hashes.get(('gnome_female', 'a'))}) == 3, 'bases have distinct skeleton hashes')


def test_spacing():
    print('== spacing')
    r = worker('spacing')
    if not r:
        return
    idle = r['Idle (5)']
    ok = lambda k, rows: [rows[str(f)][k] for f in idle['frames']]
    d_out = [min(a - b for a, b in zip(ok('hand_out_' + s, idle['after']), ok('hand_out_' + s, idle['before']))) for s in 'lr']
    d_dist = [min(a - b for a, b in zip(ok('hand_dist_' + s, idle['after']), ok('hand_dist_' + s, idle['before']))) for s in 'lr']
    check(all(d > 0.004 for d in d_out), f'idle: hands move outward on every sampled frame (min gain l/r {d_out[0]*100:.1f}/{d_out[1]*100:.1f} cm)')
    mean_dist = [sum(a - b for a, b in zip(ok('hand_dist_' + s, idle['after']), ok('hand_dist_' + s, idle['before']))) / len(idle['frames']) for s in 'lr']
    check(all(d > 0.0 for d in mean_dist), f'idle: mean hand-to-hips distance increases (l/r {mean_dist[0]*100:.1f}/{mean_dist[1]*100:.1f} cm; per-frame min {d_dist[0]*100:.1f}/{d_dist[1]*100:.1f})')
    for s, want in (('l', 0.03), ('r', -0.03)):
        for key in ('ik_lat_', 'foot_lat_'):
            deltas = [a - b for a, b in zip(ok(key + s, idle['after']), ok(key + s, idle['before']))]
            check(all(abs(d - want) < 0.003 for d in deltas), f'idle: {key}{s} moves {min(deltas)*100:.2f}..{max(deltas)*100:.2f} cm (want {want*100:+.0f})')
    sep = [(a['foot_lat_l'] - a['foot_lat_r']) - (b['foot_lat_l'] - b['foot_lat_r']) for a, b in zip((idle['after'][str(f)] for f in idle['frames']), (idle['before'][str(f)] for f in idle['frames']))]
    check(all(abs(x - 0.06) < 0.006 for x in sep), f'idle: foot separation grows by {sum(sep)/len(sep)*100:.1f} cm (2 x leg_cm)')
    check(all(a == b for f in idle['frames'] for a, b in zip(idle['zero'][str(f)]['hand_l'], idle['before'][str(f)]['hand_l'])), '0/0 spacing leaves the pose untouched')
    # raised arms
    cast = r['Wide Arm Spell Casting']
    raised = [f for f in cast['frames'] if min(cast['before'][str(f)]['abd_l'], cast['before'][str(f)]['abd_r']) >= 90]
    low = [f for f in cast['frames'] if max(cast['before'][str(f)]['abd_l'], cast['before'][str(f)]['abd_r']) < 45]
    check(bool(raised) and bool(low), f'cast clip has raised frames {raised} and lowered frames {low}')
    for f in raised:
        for s in 'lr':
            moved = max(abs(a - b) for a, b in zip(cast['after'][str(f)]['hand_' + s], cast['before'][str(f)]['hand_' + s]))
            check(moved < 1e-4, f'raised arms frame {f} hand_{s} unchanged ({moved*1000:.3f} mm)')
    for f in low:
        gain = [cast['after'][str(f)]['hand_out_' + s] - cast['before'][str(f)]['hand_out_' + s] for s in 'lr']
        check(min(gain) > 0.004, f'lowered arms frame {f} spread (+{gain[0]*100:.1f}/{gain[1]*100:.1f} cm)')
    g = r['gnome']
    surface_moved = max(abs(a - b) for s in 'lr' for a, b in zip(g['surface_after'][s], g['surface_before'][s]))
    check(g['legless'] and g['stats']['legs'] and g['stats'].get('legless_rebaked'), 'legless rig: leg spacing applied and surface feet re-baked')
    check(surface_moved > 0.01, f'legless surface feet follow the spread ({surface_moved*100:.1f} cm)')
    s = r['settings']
    check(s['raw']['base'] == 'x' and s['raw']['ap'] == {'a': 1} and s['raw']['spacing']['arm_deg'] == 5.0 and s['raw']['spacing']['leg_cm'] == 2.0, 'settings merge keeps other keys')
    check(s['idle']['arm_deg'] == 0.0 and s['idle']['leg_cm'] == 2.0 and s['other']['arm_deg'] == 5.0, 'per-clip override resolves')
    print(f"       apply_spacing: {idle['seconds']:.2f}s for {idle['stats']['frames']} frames")


def test_override():
    print('== library-override rig')
    r = worker('override')
    print('       ', json.dumps(r)[:900])
    check(r.get('override') is True, 'test rig is a library override')
    check('non-editable override' in r.get('direct', '') or 'Cannot edit' in r.get('direct', '') or r.get('direct', '').startswith('RuntimeError'), f"direct ARP retarget on the override rig is refused (so load_clip falls back): {r.get('direct', '')[:60]!r}")
    fb = r.get('fallback')
    check(isinstance(fb, dict) and fb.get('assigned'), 'load_clip assigns a retargeted action on the override rig')
    if isinstance(fb, dict):
        check(fb['hand_l'] != fb['hand_l_f1'], 'override rig pose changes over the clip')


def test_clips():
    print('== clip export')
    from importlib import util
    spec = util.spec_from_file_location('anims_plain', BLENDER_DIR / 'anims.py')
    anims = util.module_from_spec(spec)
    spec.loader.exec_module(anims)
    names = MIXAMO + RPG
    rows = anims.clip_rows(names)
    check(len(rows) == 6 and sum(r['set'] == 'rpg' for r in rows) == 3, f"6 clips selected ({[r['source_name'] for r in rows]})")
    annotations = anims.read_json(REPO / 'scripts' / 'character_export' / 'cast_annotations.json')['clips']
    cast = next(r for r in anims.ce('batch').clips(REPO) if r['casting'])
    check(anims.castable(cast, annotations) and not anims.castable(cast, {}) and anims.castable(rows[0], {}), 'castable(): unreviewed casting clips are skipped, others pass')
    runs = {}
    for tag, extra in (('spaced', ['--arm', '8', '--leg', '3']), ('flat', ['--arm', '0', '--leg', '0'])):
        batch = OUT / 'clips' / tag
        shutil.rmtree(batch, ignore_errors=True)
        t = time.time()
        res = subprocess.run([sys.executable, str(BLENDER_DIR / 'anims.py'), 'drive', '--base', 'human_male', '--batch-dir', str(batch), '--chunk', '3',
                              '--only', ','.join(names), '--blender', EXE, *extra], capture_output=True, text=True)
        wall = time.time() - t
        line = next((l for l in res.stdout.splitlines() if l.startswith('EXPORT_ANIMS')), None)
        check(line is not None and res.returncode == 0, f'{tag}: driver finished in {wall:.0f}s (exit {res.returncode})')
        if not line:
            print(res.stdout[-1500:], res.stderr[-1500:])
            continue
        summary = json.loads(line.split(' ', 1)[1])
        runs[tag] = (batch, summary, wall)
        out = batch / 'bases' / 'human_male'
        check(summary['exported'] == 6 and not summary['failed'], f"{tag}: exported {summary['exported']}/6, failed {summary['failed']}")
        base = json.loads((out / 'base.json').read_text())
        clips = json.loads((out / 'clips.json').read_text())
        check(summary['merged']['clips'] == 6 and summary['merged']['animations'] == 6, f'{tag}: anims.glb merges 6 animations')
        check(len(clips['clips']) == 6 and all({'start', 'end', 'fps', 'loop', 'source', 'set'} <= set(c) for c in clips['clips'].values()), f'{tag}: clips.json rows')
        check(clips['skeleton_sha256'] == base['skeleton_sha256'], f'{tag}: clips share the base skeleton hash')
        check(clips['clips']['Idle (5)_remap']['loop'] and clips['clips']['Idle (5)_remap']['set'] == 'mixamo', f'{tag}: loop/set flags')
        sets = {c['set'] for c in clips['clips'].values()}
        check(sets == {'mixamo', 'rpg'}, f'{tag}: both clip sets present')
        doc, _ = glb(out / 'anims.glb')
        check(sorted(a['name'] for a in doc['animations']) == sorted(clips['clips']), f'{tag}: animation names match clips.json')
    if len(runs) == 2:
        sys.path.insert(0, str(REPO / 'scripts'))
        from character_export.native_audit import accessor
        def rotations(batch, clip):
            doc, binary = glb(batch / 'bases' / 'human_male' / 'anims.glb')
            anim = next(a for a in doc['animations'] if a['name'] == clip)
            nodes = {i: n['name'] for i, n in enumerate(doc['nodes'])}
            return {nodes[c['target']['node']]: accessor(doc, binary, anim['samplers'][c['sampler']]['output']).copy()
                    for c in anim['channels'] if c['target']['path'] == 'rotation' and nodes[c['target']['node']] in ('LeftUpperArm', 'RightUpperArm', 'LeftUpperLeg')}
        a, b = rotations(runs['spaced'][0], 'Idle (5)_remap'), rotations(runs['flat'][0], 'Idle (5)_remap')
        import numpy as np
        diff = {k: float(np.minimum(np.abs(a[k] - b[k]), np.abs(a[k] + b[k])).max()) for k in a}     # q and -q are one rotation
        check(diff.get('LeftUpperArm', 0) > 0.01 and diff.get('RightUpperArm', 0) > 0.01, f'exported arm rotations differ with spacing ({diff})')
    if 'spaced' in runs:
        batch, summary, wall = runs['spaced']
        out = batch / 'bases' / 'human_male'
        # resume: nothing to do, then one deleted clip is redone
        t = time.time()
        res = subprocess.run([sys.executable, str(BLENDER_DIR / 'anims.py'), 'drive', '--base', 'human_male', '--batch-dir', str(batch), '--only', ','.join(names), '--blender', EXE, '--arm', '8', '--leg', '3'], capture_output=True, text=True)
        again = json.loads(next(l for l in res.stdout.splitlines() if l.startswith('EXPORT_ANIMS')).split(' ', 1)[1])
        check(again['exported'] == 0 and again['up_to_date'] == 6, f"resume: {again['up_to_date']} up to date, {again['exported']} exported ({time.time()-t:.1f}s)")
        victim = next((out / 'work').glob('RPG-Character_Unarmed-Idle-Injured*'))
        shutil.rmtree(victim)
        res = subprocess.run([sys.executable, str(BLENDER_DIR / 'anims.py'), 'drive', '--base', 'human_male', '--batch-dir', str(batch), '--only', ','.join(names), '--blender', EXE, '--arm', '8', '--leg', '3'], capture_output=True, text=True)
        again = json.loads(next(l for l in res.stdout.splitlines() if l.startswith('EXPORT_ANIMS')).split(' ', 1)[1])
        check(again['exported'] == 1 and again['up_to_date'] == 5, f"resume: deleted clip redone ({again['up_to_date']} kept, {again['exported']} exported)")
        # a settings change invalidates the checkpoints
        res = subprocess.run([sys.executable, str(BLENDER_DIR / 'anims.py'), 'drive', '--base', 'human_male', '--batch-dir', str(batch), '--only', 'Idle (5)', '--no-merge', '--blender', EXE, '--arm', '4', '--leg', '3'], capture_output=True, text=True)
        again = json.loads(next(l for l in res.stdout.splitlines() if l.startswith('EXPORT_ANIMS')).split(' ', 1)[1])
        check(again['exported'] == 1, 'changed arm_deg re-exports the clip')
        # timing
        metas = [json.loads(p.read_text()) for p in (out / 'work').glob('*/clip.json')]
        per = [m['timing']['retarget'] + m['timing']['export'] for m in metas]
        n = len([r for r in anims.clip_rows() if anims.castable(r, annotations)])
        mean = sum(per) / len(per)
        print(f"       per clip: retarget {sum(m['timing']['retarget'] for m in metas)/len(metas):.1f}s + spacing/export {sum(m['timing']['export'] for m in metas)/len(metas):.1f}s = {mean:.1f}s; "
              f"driver wall {wall/6:.1f}s/clip incl. base export and merge -> ~{n} clips x {mean:.1f}s = {n*mean/3600:.1f} h per base (+ chunk startup), {11*n*mean/3600:.0f} h for 11 bases serial")


STEPS = dict(base=test_base, spacing=test_spacing, override=test_override, clips=test_clips)

if __name__ == '__main__':
    if bpy:
        blender_main()
    else:
        OUT.mkdir(parents=True, exist_ok=True)
        for name in (sys.argv[1:] or STEPS):
            STEPS[name]()
        print('\nFAILED: ' + '; '.join(FAILS) if FAILS else '\nall passed')
        sys.exit(1 if FAILS else 0)
