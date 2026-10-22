"""Comparison sheet of the rigid-section methods (M1 zones vs M2 snap) on reduced plate-mail armours, plus the guess
perturbation experiment for the snap method.

Usage: python3 tools/tripo_pipeline/compare_rigid.py <out.jpg> <name>=<reduced_dir>:<roles|rim> [...] [--level 0p4]
  e.g.  compare_rigid.py experiments/rigid_sections_20261022/compare_m1_m2.jpg \\
          S6=experiments/pauldron_pipeline_20261022/S6_crystal_heart/reduced:roles \\
          S4=experiments/pauldron_pipeline_20261022/S4_lion_crest/reduced:rim

Per armour two rows (6 panels of 400 px, 2400 px wide in all): front / back / exploded for M1 and M2, then posed front
views (pauldron follows the arm, strips owned by the static piece): arms down 45 deg without / with overlap, arms up 30
deg without / with overlap, and the 'pauldron follows the torso' convention (arms down / up, with overlap). Yellow =
overlap strips. Metrics are printed under every row and written next to the sheet as <out>.json.
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import split_pieces as sp  # noqa: E402

PW = 400


def static_items(sec, explode):
    H = sec.level.height
    items = []
    for lab, name in sec.names.items():
        sh = np.array([*sp.explode_shift(name, H, True), 0.]) if explode else 0.
        t = sec.level.raw_v[sec.level.raw_f[sec.label == lab]] + sh
        items.append((t, np.tile(sp.RIGID_COLORS[name], (len(t), 1))))
        if lab in sec.ext and len(sec.ext[lab]['f']):
            st = sec.tris(lab)[0][int((sec.label == lab).sum()):] + sh
            items.append((st, np.tile(sp.STRIP_COLOR, (len(st), 1))))
    return items


def bounds_of(items):
    p = np.concatenate([t.reshape(-1, 3) for t, _ in items])
    return p[:, 0].min(), p[:, 0].max(), p[:, 1].min(), p[:, 1].max()


def grow(level, label, lab, rings, into=0):
    """Guess perturbation: label `lab` grows by `rings` rings over faces labelled `into`."""
    out = label.copy()
    g = level.graph
    for _ in range(rings):
        take = []
        for i in np.flatnonzero(out == lab):
            for j in g.neighbours(i)[0]:
                if out[j] == into:
                    take.append(j)
        out[np.array(take, np.int64)] = lab
    return out


def perturb_experiment(level, spec, sec_zones):
    """Perturb the M1 zone guess (arms grown / shrunk by 1 and 2 rings), re-cut with M2, and report how many arm faces
    differ from the unperturbed M1 guess before and after the snap, and the snap cost of the cut."""
    out = {}
    base = sec_zones.label
    for how, rings in (('grow', 1), ('grow', 2), ('shrink', 1), ('shrink', 2)):
        label = base.copy()
        for lab in (3, 4):
            if how == 'grow':
                label = grow(level, label, lab, rings)
            else:
                for _ in range(rings):                      # peel `rings` rings off the torso side of the arm
                    peel = [i for i in np.flatnonzero(label == lab) if any(label[j] == 0 for j in level.graph.neighbours(i)[0])]
                    label[np.array(peel, np.int64)] = 0
        rep = {}
        snapped = sp.sections_snap(level, label, spec, rep)
        arm = np.isin(base, (3, 4))
        before = int(((np.isin(label, (3, 4))) ^ arm).sum())
        after = int(((np.isin(snapped, (3, 4))) ^ arm).sum())
        names = sp.piece_names(spec, True)
        cost = lambda lbl: sum(c.get('snap_cost', 0) for c in sp.cut_report(level, lbl, names, spec) if 'arm' in c['pair'][1] and c['pair'][0] == 'torso')
        out[f'{how}{rings}'] = {'arm_faces_off_guess_before_snap': before, 'after_snap': after,
                                'torso_arm_cut_cost_before': cost(label), 'after': cost(snapped), 'guess_cost': cost(base)}
    return out


def build_pair(reduced, style, level_name, methods=('zones', 'snap'), overrides=None):
    reduced = Path(reduced)
    manifest = json.loads((reduced / 'manifest.json').read_text())
    level = sp.load_level(reduced, level_name, manifest)
    spec = sp.load_spec('body_armor', style, overrides={'rigid_method': methods[-1], 'overlap': True, 'pose_test': True,
                                                         **(overrides or {})})
    secs = {m: sp.build_sections(level, spec, m, True) for m in methods}
    for s in secs.values():
        s.joints = sp.resolve_joints(level, spec)
    return level, spec, secs


def main(argv):
    """compare_rigid.py <out.jpg> <name>=<reduced>:<style>[:<methodA>:<methodB>] ... [--level 0p4] [--split]
    [--convention arm|torso|half] [--mode parent|child|both|cover] [--length F]   (default methods zones, snap)"""
    opts = {'level': '0p4', 'convention': None, 'mode': None, 'length': None}
    split = False
    argv = list(argv)
    for flag in ('--level', '--convention', '--mode', '--length'):
        if flag in argv:
            k = argv.index(flag)
            opts[flag[2:]] = argv[k + 1]
            del argv[k:k + 2]
    if '--split' in argv:
        split = True
        argv.remove('--split')
    level_name = opts['level']
    out_path, entries = Path(argv[0]), argv[1:]
    from PIL import Image, ImageDraw
    font, small = sp._fonts()
    static_rows, pose_rows, report = [], [], {}
    for entry in entries:
        name, rest = entry.split('=', 1)
        parts = rest.rsplit(':', 3) if rest.count(':') >= 3 else rest.rsplit(':', 1)
        reduced, style = parts[0], parts[1]
        methods = tuple(parts[2:4]) if len(parts) == 4 else ('zones', 'snap')
        ov = {'pose_convention': opts['convention'], 'overlap_mode': opts['mode'],
              'overlap_length': float(opts['length']) if opts['length'] else None}
        level, spec, secs = build_pair(reduced, style, level_name, methods, ov)
        mA, mB = methods
        sA, sB = secs[mA], secs[mB]
        items = {(m, ex): static_items(s, ex) for m, s in ((mA, sA), (mB, sB)) for ex in (False, True)}
        bounds_front = bounds_of([i for k, v in items.items() if not k[1] for i in v])
        bounds_exp = bounds_of([i for k, v in items.items() if k[1] for i in v])
        ph = max(sp.panel_height(bounds_front, PW), sp.panel_height(bounds_exp, PW))
        row1 = Image.new('RGB', (PW * 6, ph + 4), (246, 246, 244))
        d1 = ImageDraw.Draw(row1)
        for c, (m, sign, ex, label) in enumerate(((mA, 1, False, 'front'), (mB, 1, False, 'front'), (mA, -1, False, 'back'),
                                                  (mB, -1, False, 'back'), (mA, 1, True, 'exploded'), (mB, 1, True, 'exploded'))):
            row1.paste(sp.draw_view(items[(m, ex)], sign, bounds_exp if ex else bounds_front, PW, ph), (c * PW, 4))
            d1.text((c * PW + 8, 6), f'{name[:12]} {m} {label}', fill=(30, 30, 30), font=small)
        pose = spec['rigid']['pose']
        conv, mode = pose['convention'], spec['rigid']['overlap'].get('mode', 'parent')
        variants = [(sB, pose['down_deg'], False), (sB, pose['down_deg'], True), (sB, -pose['up_deg'], False),
                    (sB, -pose['up_deg'], True), (sA, pose['down_deg'], True), (sA, -pose['up_deg'], True)]
        posed = [sp.pose_metrics(sec, ang, conv, ov_, mode) for sec, ang, ov_ in variants]
        pb = sp.pose_bounds(sB, [p[0] for p in posed])
        pph = sp.panel_height(pb, PW)
        row2 = Image.new('RGB', (PW * 6, pph + 4), (246, 246, 244))
        d2 = ImageDraw.Draw(row2)
        pose_report = {}
        for c, ((sec, ang, ov_), (pz, fl, cuts)) in enumerate(zip(variants, posed)):
            row2.paste(sp.draw_posed(sec, pz, fl, pb, PW, pph), (c * PW, 4))
            rig = [x for x in cuts if x['owner'] != 'soft']
            gmax = max((x['gap_max_pct_height'] for x in rig), default=0.)
            gmean = float(np.mean([x['gap_mean_pct_height'] for x in rig])) if rig else 0.
            stretch = max((x.get('soft_stretch_max', 0) for x in cuts), default=0)
            sm = [x for x in cuts if x['owner'] == 'soft']
            cap = f'{name[:8]} {sec.report["rigid_method"]} {"down" if ang > 0 else "up"} {abs(ang):g}, {"overlap" if ov_ else "no ovl"}'
            d2.text((c * PW + 8, 6), cap, fill=(30, 30, 30), font=small)
            d2.text((c * PW + 8, pph - 12), f'gap max {gmax:.1f}% / mean {gmean:.2f}% H' + (f'  soft stretch x{stretch:.2f}' if sm else ''),
                    fill=(150, 30, 30), font=small)
            pose_report[f'{sec.report["rigid_method"]}/{"down" if ang > 0 else "up"}/{"overlap" if ov_ else "no_overlap"}'] = {
                'gap_max': gmax, 'gap_mean': gmean, 'soft_stretch_max': stretch if sm else None, 'rigid_cuts': len(rig)}
        info = {}
        for m, s in ((mA, sA), (mB, sB)):
            ps = {s.names[l]: one_stats(level, s.label, l) for l in s.names}
            cuts = s.report['cuts']
            tot = sum(c['length'] for c in cuts if 'torso' in c['pair'] and any(x.startswith('arm') or x == 'soft' for x in c['pair']))
            on = sum(c['length'] * c['role_boundary_share'] for c in cuts if 'torso' in c['pair'] and any(x.startswith('arm') or x == 'soft' for x in c['pair']))
            info[m] = {'pieces': ps, 'torso_arm_cut_on_colour_edge': (on / tot) if tot else None,
                       'torso_arm_cut_pct_height': tot / level.height * 100, 'cuts': cuts,
                       'seam': {k: s.report.get(k) for k in ('seam_soft', 'seam_not_soft')} if 'seam_soft' in s.report else None,
                       'colour': {k: v for k, v in (s.report.get('colour') or {}).items() if k != 'fields'} or None}
        if 'zones' in methods or 'snap' in methods:
            info['perturb'] = perturb_experiment(level, spec, secs['zones']) if 'zones' in secs else None
        info['pose'] = pose_report
        info['strip_faces'] = {m: sum(len(e['f']) for e in s.ext.values()) for m, s in secs.items()}
        report[name] = info
        head = Image.new('RGB', (PW * 6, 26), (230, 232, 236))
        txt = ''
        for m in (mA, mB):
            i = info[m]
            fc = ' '.join(f'{k}:{v["faces"]}/{v["components"]}c' for k, v in i['pieces'].items())
            ce = i['torso_arm_cut_on_colour_edge']
            txt += f'  [{m}] {fc}; torso|arm(soft) cut {i["torso_arm_cut_pct_height"]:.0f}% H on colour edge {ce:.0%}' if ce is not None else f'  [{m}] {fc}'
        ImageDraw.Draw(head).text((6, 4), f'{name} ({style}, {level_name}){txt}', fill=(20, 20, 20), font=small)
        static_rows += [head, row1]
        pose_rows += [head.copy(), row2]
    W = PW * 6

    def stack(rows, path):
        sheet = Image.new('RGB', (W, sum(r.height for r in rows)), (246, 246, 244))
        y = 0
        for r in rows:
            sheet.paste(r, (0, y))
            y += r.height
        path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(path, quality=88)
        print('SHEET', path, sheet.size)

    if split:
        stack(static_rows, out_path)
        stack(pose_rows, out_path.with_name(out_path.stem + '_pose.jpg'))
    else:
        rows = []
        for a_, b_ in zip(static_rows[::2], static_rows[1::2]):
            rows += [a_, b_]
        for a_, b_ in zip(pose_rows[1::2], pose_rows[1::2]):
            rows += [b_]
        stack(rows, out_path)
    Path(str(out_path) + '.json').write_text(json.dumps(report, indent=2, default=float))


def continuity_main(argv):
    """compare_rigid.py --continuity <out.json> <name>=<reduced_dir> ...: per joint and LOD, the fraction of the joint ring
    covered by joint-role (cyan) faces (split_pieces.band_continuity), table on stdout and numbers in <out.json>."""
    argv = [a for a in argv if a != '--continuity']
    out_path, entries = Path(argv[0]), argv[1:]
    result = {}
    for entry in entries:
        name, reduced = entry.split('=', 1)
        manifest = json.loads((Path(reduced) / 'manifest.json').read_text())
        result[name] = {}
        for lv in sp.levels_in(reduced)[::-1]:
            level = sp.load_level(Path(reduced), lv, manifest)
            result[name][lv] = sp.band_continuity(level)
            print(f'{name} {lv}: wearable {level.n} faces, joint faces {int(level.role_mask(sp.JOINT_ROLES).sum())}', flush=True)
            for k, v in result[name][lv].items():
                if isinstance(v, dict):
                    w = v['axial_width_pct_height']
                    print(f'    {k:7s} faces {v["faces"]:3d} in {v.get("components", 0)} piece(s)  ring coverage {v["coverage"]:.0%}  largest gap {v["largest_gap_deg"]:3.0f} deg  '
                          f'band width {"-" if w is None else f"{w:.1f}% H"}', flush=True)
    out_path.write_text(json.dumps(result, indent=2))
    return result


def bands_main(argv):
    """compare_rigid.py --bands <out.jpg> <name>=<reduced_dir> ...: snap (all joints) vs bands (soft pieces) vs bands
    (nearest) per concept: <out>.jpg = front / back / exploded views, <out>_pose.jpg = the pose scenarios with overlap
    (and two without), <out>.jpg.json = numbers. Pauldron convention half, overlap both, 6% (config defaults)."""
    argv = [a for a in argv if a != '--bands']
    out_path, entries = Path(argv[0]), argv[1:]
    from PIL import Image, ImageDraw
    font, small = sp._fonts()
    pw = 340
    static_rows, pose_rows, report = [], [], {}
    scen = [('arms_down', 'arms', 45), ('arms_up', 'arms', -30), ('thigh_flex', 'thigh_flex', 45), ('thigh_abd', 'thigh_abd', 20),
            ('spine', 'spine', 20)]
    for entry in entries:
        name, reduced = entry.split('=', 1)
        manifest = json.loads((Path(reduced) / 'manifest.json').read_text())
        level = sp.load_level(Path(reduced), '0p4', manifest)
        base = {'overlap': True, 'pose_test': True}
        secs = {}
        for key, method, soft in (('snap', 'snap', 'piece'), ('bands', 'bands', 'piece'), ('bands_nearest', 'bands', 'nearest')):
            spec = sp.load_spec('body_armor', 'rim', overrides={**base, 'rigid_method': method, 'soft_mode': soft, 'joints': 'all'})
            secs[key] = sp.build_sections(level, spec, method, True)
            secs[key].joints = sp.resolve_joints(level, spec)
        spec = sp.load_spec('body_armor', 'rim', overrides={'rigid_method': 'bands'})
        info = {}
        for key, sec in secs.items():
            pieces = {sec.names[l]: one_stats(level, sec.label, l) for l in sec.names}
            colour = None
            tc = [c for c in sec.report['cuts'] if any(x in c['pair'] for x in ('chest', 'pelvis')) and not any(x.startswith('soft') for x in c['pair'])
                  and not any(x.startswith('pauldron') for x in c['pair'])]
            if tc:
                tot = sum(c['length'] for c in tc)
                colour = sum(c['length'] * c['role_boundary_share'] for c in tc) / tot if tot else None
            info[key] = {'pieces': pieces, 'rigid_joint_cut_pct_height': sum(c['length'] for c in tc) / level.height * 100,
                         'rigid_joint_cut_on_colour_edge': colour, 'fell_back': (sec.report.get('bands') or {}).get('fell_back'),
                         'absent': (sec.report.get('bands') or {}).get('absent'),
                         'joint_status': {k: v['status'] for k, v in (sec.report.get('bands') or {}).get('joints', {}).items()} or None,
                         'strip_faces': sum(len(e['f']) for e in sec.ext.values()), 'wearable_faces': level.n}
        keys = ('snap', 'bands', 'bands_nearest')
        items = {(k, ex): static_items(secs[k], ex) for k in keys for ex in (False, True)}
        bf = bounds_of([i for (k, ex), v in items.items() if not ex for i in v])
        be = bounds_of([i for (k, ex), v in items.items() if ex for i in v])
        ph = max(sp.panel_height(bf, pw), sp.panel_height(be, pw))
        row = Image.new('RGB', (pw * 7, ph + 4), (246, 246, 244))
        d = ImageDraw.Draw(row)
        for c, (k, sign, ex, lab) in enumerate((('snap', 1, False, 'front'), ('bands', 1, False, 'front'), ('bands_nearest', 1, False, 'front'),
                                                ('snap', -1, False, 'back'), ('bands', -1, False, 'back'),
                                                ('snap', 1, True, 'exploded'), ('bands', 1, True, 'exploded'))):
            row.paste(sp.draw_view(items[(k, ex)], sign, be if ex else bf, pw, ph), (c * pw, 4))
            d.text((c * pw + 6, 6), f'{name[:10]} {k} {lab}', fill=(30, 30, 30), font=small)
        head = Image.new('RGB', (pw * 7, 26), (230, 232, 236))
        txt = f'{name}  '
        for k in keys:
            fc = ' '.join(f'{n}:{v["faces"]}/{v["components"]}' for n, v in info[k]['pieces'].items() if v['faces'])
            fb = info[k]['fell_back']
            txt += f'[{k}] {fc}' + (f' fallback={fb}' if fb else '') + '   '
        ImageDraw.Draw(head).text((6, 6), txt[:330], fill=(20, 20, 20), font=small)
        static_rows += [head, row]
        # pose
        conv, mode = 'half', 'both'
        panels = []
        for k in ('bands', 'snap'):
            for sc, scn, ang in scen:
                if sp.scenario_ready(secs[k], scn):
                    panels.append((k, sc, scn, ang, True))
            for sc, scn, ang in (scen[2], scen[4]):
                if sp.scenario_ready(secs[k], scn):
                    panels.append((k, sc, scn, ang, False))
        pose = {}
        posed = [(p_, sp.pose_metrics(secs[p_[0]], p_[3], conv, p_[4], mode, p_[2])) for p_ in panels]
        for (k, sc, scn, ang, ov), (pz, fl, cuts) in posed:
            rig = [c for c in cuts if 'soft_stretch_max' not in c]
            st = [c['soft_stretch_max'] for c in cuts if 'soft_stretch_max' in c]
            pose[f'{k}/{sc}/{"overlap" if ov else "no_overlap"}'] = {
                'gap_max': max((c['gap_max_pct_height'] for c in rig), default=0.),
                'gap_mean': float(np.mean([c['gap_mean_pct_height'] for c in rig])) if rig else 0.,
                'rigid_cuts': [f'{c["owner"]}|{c["under"]}' for c in rig], 'soft_stretch_max': max(st) if st else None,
                'per_cut': [{'cut': f'{c["owner"]}|{c["under"]}', 'max': c['gap_max_pct_height'], 'mean': c['gap_mean_pct_height']} for c in rig]}
        info['pose'] = pose
        report[name] = info
        allp = [pz for _, (pz, fl, cuts) in posed]
        pb = sp.pose_bounds(secs['bands'], allp)
        pph = sp.panel_height(pb, pw)
        for half in (('bands',), ('snap',)):
            sub = [(p_, r_) for p_, r_ in posed if p_[0] == half[0]]
            prow = Image.new('RGB', (pw * 7, pph + 4), (246, 246, 244))
            dd = ImageDraw.Draw(prow)
            for c, ((k, sc, scn, ang, ov), (pz, fl, cuts)) in enumerate(sub[:7]):
                prow.paste(sp.draw_posed(secs[k], pz, fl, pb, pw, pph), (c * pw, 4))
                m = pose[f'{k}/{sc}/{"overlap" if ov else "no_overlap"}']
                dd.text((c * pw + 6, 6), f'{name[:8]} {k} {sc} {"ovl" if ov else "no ovl"}', fill=(30, 30, 30), font=small)
                dd.text((c * pw + 6, pph - 12), f'gap {m["gap_max"]:.1f}/{m["gap_mean"]:.2f}% H' + (f' soft x{m["soft_stretch_max"]:.2f}' if m['soft_stretch_max'] else ''),
                        fill=(150, 30, 30), font=small)
            pose_rows += [head.copy() if half == ('bands',) else Image.new('RGB', (pw * 7, 2), (230, 232, 236)), prow]
    def stack(rows, path):
        sheet = Image.new('RGB', (pw * 7, sum(r.height for r in rows)), (246, 246, 244))
        y = 0
        for r in rows:
            sheet.paste(r, (0, y))
            y += r.height
        sheet.save(path, quality=88)
        print('SHEET', path, sheet.size)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stack(static_rows, out_path)
    stack(pose_rows, out_path.with_name(out_path.stem + '_pose.jpg'))
    Path(str(out_path) + '.json').write_text(json.dumps(report, indent=2, default=float))


def role_items(level):
    """Wearable triangles in their own garment colours (what the concept colours became after reduction)."""
    cols = np.array([m['color'][:3] for m in level.materials], float)
    cols = (np.clip(cols, 0, 1) * 255).astype(np.uint8) if cols.max() <= 1.0 else cols.astype(np.uint8)
    t = level.raw_v[level.raw_f]
    return [(t, cols[level.face_material])]


def regions_main(argv):
    """compare_rigid.py --regions <out.jpg> <name>=<reduced_dir>[@<concept front.png>] ...: per concept the per-piece-colour
    split (`--rigid regions`, pauldron rim style) against the snap split of the same wearable. <out>.jpg = concept front,
    reduced colours, regions front / back / exploded, snap front / exploded; <out>_pose.jpg = the pose scenarios (arms
    down 45 / up 30, thigh flex, thigh abduction, spine, and two without overlap) per method; <out>.jpg.json = numbers.
    Defaults: pauldron convention half, overlap both, 6%. Level 0p4 (--level 0p2 etc. via LEVEL env)."""
    import os
    argv = [a for a in argv if a != '--regions']
    out_path, entries = Path(argv[0]), argv[1:]
    from PIL import Image, ImageDraw
    font, small = sp._fonts()
    pw = 340
    lvl = os.environ.get('LEVEL', '0p4')
    static_rows, pose_rows, report = [], [], {}
    scen = [('arms_down', 'arms', 45), ('arms_up', 'arms', -30), ('thigh_flex', 'thigh_flex', 45), ('thigh_abd', 'thigh_abd', 20),
            ('spine', 'spine', 20)]
    for entry in entries:
        entry, _, concept = entry.partition('@')
        name, reduced = entry.split('=', 1)
        manifest = json.loads((Path(reduced) / 'manifest.json').read_text())
        level = sp.load_level(Path(reduced), lvl, manifest)
        base = {'overlap': True, 'pose_test': True}
        secs = {}
        for key, method in (('regions', 'regions'), ('snap', 'snap')):
            spec = sp.load_spec('body_armor', None, overrides={**base, 'rigid_method': method, 'joints': 'all'})
            secs[key] = sp.build_sections(level, spec, method, True)
            secs[key].joints = sp.resolve_joints(level, spec)
        info = {}
        for key, sec in secs.items():
            pieces = {sec.names[l]: one_stats(level, sec.label, l) for l in sec.names}
            tc = [c for c in sec.report['cuts'] if any(x in c['pair'] for x in ('chest', 'pelvis')) and not any(x.startswith('pauldron') for x in c['pair'])]
            tot = sum(c['length'] for c in tc)
            colour = sum(c['length'] * c['role_boundary_share'] for c in tc) / tot if tot else None
            reg = sec.report.get('regions') or {}
            info[key] = {'pieces': pieces, 'rigid_joint_cut_pct_height': tot / level.height * 100, 'rigid_joint_cut_on_colour_edge': colour,
                         'joint_status': {k: v['status'] for k, v in reg.get('joints', {}).items()} or None,
                         'fell_back': reg.get('fell_back'), 'absent': reg.get('absent'),
                         'decoration_touching_two': len(reg.get('decoration_touching_two_sections', [])) if reg else None,
                         'strip_faces': sum(len(e['f']) for e in sec.ext.values()), 'wearable_faces': level.n}
        items = {(k, ex): static_items(secs[k], ex) for k in secs for ex in (False, True)}
        bf = bounds_of([i for (k, ex), v in items.items() if not ex for i in v])
        be = bounds_of([i for (k, ex), v in items.items() if ex for i in v])
        ph = max(sp.panel_height(bf, pw), sp.panel_height(be, pw))
        row = Image.new('RGB', (pw * 7, ph + 4), (246, 246, 244))
        d = ImageDraw.Draw(row)
        if concept and Path(concept).is_file():
            im = Image.open(concept).convert('RGBA')
            bg = Image.new('RGBA', im.size, (246, 246, 244, 255))
            bg.alpha_composite(im)
            im = bg.convert('RGB')
            im.thumbnail((pw, ph))
            row.paste(im, (0, 4))
        row.paste(sp.draw_view(role_items(level), 1, bf, pw, ph), (pw, 4))
        d.text((6, 6), f'{name[:10]} concept', fill=(30, 30, 30), font=small)
        d.text((pw + 6, 6), f'{name[:10]} reduced colours', fill=(30, 30, 30), font=small)
        for c, (k, sign, ex, lab) in enumerate((('regions', 1, False, 'front'), ('regions', -1, False, 'back'), ('regions', 1, True, 'exploded'),
                                                ('snap', 1, False, 'front'), ('snap', 1, True, 'exploded')), 2):
            row.paste(sp.draw_view(items[(k, ex)], sign, be if ex else bf, pw, ph), (c * pw, 4))
            d.text((c * pw + 6, 6), f'{name[:10]} {k} {lab}', fill=(30, 30, 30), font=small)
        head = Image.new('RGB', (pw * 7, 26), (230, 232, 236))
        txt = f'{name}  [{secs["regions"].report.get("rigid_method")}] '
        fc = ' '.join(f'{n}:{v["faces"]}/{v["components"]}' for n, v in info['regions']['pieces'].items() if v['faces'])
        txt += fc + (f' region-joints={ {k: v for k, v in (info["regions"]["joint_status"] or {}).items()} }' if info['regions']['joint_status'] else '')
        ImageDraw.Draw(head).text((6, 6), txt[:330], fill=(20, 20, 20), font=small)
        static_rows += [head, row]
        conv, mode = 'half', 'both'
        panels = []
        for k in ('regions', 'snap'):
            for sc, scn, ang in scen:
                if sp.scenario_ready(secs[k], scn):
                    panels.append((k, sc, scn, ang, True))
            for sc, scn, ang in (scen[2], scen[4]):
                if sp.scenario_ready(secs[k], scn):
                    panels.append((k, sc, scn, ang, False))
        pose = {}
        posed = [(p_, sp.pose_metrics(secs[p_[0]], p_[3], conv, p_[4], mode, p_[2])) for p_ in panels]
        for (k, sc, scn, ang, ov), (pz, fl, cuts) in posed:
            rig = [c for c in cuts if 'soft_stretch_max' not in c]
            pose[f'{k}/{sc}/{"overlap" if ov else "no_overlap"}'] = {
                'gap_max': max((c['gap_max_pct_height'] for c in rig), default=0.),
                'gap_mean': float(np.mean([c['gap_mean_pct_height'] for c in rig])) if rig else 0.,
                'per_cut': [{'cut': f'{c["owner"]}|{c["under"]}', 'max': c['gap_max_pct_height'], 'mean': c['gap_mean_pct_height']} for c in rig]}
        info['pose'] = pose
        report[name] = info
        allp = [pz for _, (pz, fl, cuts) in posed]
        pb = sp.pose_bounds(secs['regions'], allp)
        pph = sp.panel_height(pb, pw)
        for half in ('regions', 'snap'):
            sub = [(p_, r_) for p_, r_ in posed if p_[0] == half]
            prow = Image.new('RGB', (pw * 7, pph + 4), (246, 246, 244))
            dd = ImageDraw.Draw(prow)
            for c, ((k, sc, scn, ang, ov), (pz, fl, cuts)) in enumerate(sub[:7]):
                prow.paste(sp.draw_posed(secs[k], pz, fl, pb, pw, pph), (c * pw, 4))
                m = pose[f'{k}/{sc}/{"overlap" if ov else "no_overlap"}']
                dd.text((c * pw + 6, 6), f'{name[:8]} {k} {sc} {"ovl" if ov else "no ovl"}', fill=(30, 30, 30), font=small)
                dd.text((c * pw + 6, pph - 12), f'gap {m["gap_max"]:.1f}/{m["gap_mean"]:.2f}% H', fill=(150, 30, 30), font=small)
            pose_rows += [head.copy() if half == 'regions' else Image.new('RGB', (pw * 7, 2), (230, 232, 236)), prow]

    def stack(rows, path):
        sheet = Image.new('RGB', (pw * 7, sum(r.height for r in rows)), (246, 246, 244))
        y = 0
        for r in rows:
            sheet.paste(r, (0, y))
            y += r.height
        if max(sheet.size) > 2400:                       # sheets stay within 2400 px both ways
            f_ = 2400 / max(sheet.size)
            sheet = sheet.resize((int(sheet.width * f_), int(sheet.height * f_)), Image.LANCZOS)
        sheet.save(path, quality=88)
        print('SHEET', path, sheet.size)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stack(static_rows, out_path)
    stack(pose_rows, out_path.with_name(out_path.stem + '_pose.jpg'))
    Path(str(out_path) + '.json').write_text(json.dumps(report, indent=2, default=float))


def one_stats(level, label, lab):
    st = sp.one_piece_stats(level, label, lab)
    return {'faces': st['faces'], 'components': st['components']}


if __name__ == '__main__':
    if '--continuity' in sys.argv:
        continuity_main(sys.argv[1:])
    elif '--regions' in sys.argv:
        regions_main(sys.argv[1:])
    elif '--bands' in sys.argv:
        bands_main(sys.argv[1:])
    else:
        main(sys.argv[1:])
