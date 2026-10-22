"""Compare the segment-first prototype with the current pipeline (pieces_auto) on the same concepts.

Usage (venv python with numpy/scipy/Pillow):
  python tools/tripo_pipeline/compare_segment_first.py <out.jpg> <name>=<current_concept_dir>:<segment_first_dir> ...
  e.g. P_29=experiments/piece_colours_20261022/P_29_hawk_helmless_champion:experiments/segment_first_20261022/P_29_hawk_helmless_champion
<current_concept_dir> holds reduced/ + pieces_auto/ (the current pipeline), <segment_first_dir> holds prep/, pieces/,
levels/, highpoly_labels.npz (segment_first.py). Writes <out.jpg> (per concept: current vs segment-first, assembled front,
exploded, spine-pitch side view; 6 panels of 380 px) and <out.jpg>.json with, per concept and level:
  mixed_area_share  - area share of the wearable's triangles whose top garment role covers < 75% of the high-poly wear area
                      under them (the high-poly roles are segment_first's cleaned classes for BOTH pipelines);
  texture_vote_mixed_share - the current pipeline's own number (texture-sample vote share < 75%, from its colours npz);
  cut_alignment     - distance of every cut edge between pieces (3 points per edge) to the nearest high-poly colour boundary
                      (edges between different primary/secondary/accent classes), mean / p95 / max in % of body height;
  pieces            - faces / connected components per piece, wearable triangles, pose gaps, runtime.
"""
import json
import sys
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compare_rigid as cr  # noqa: E402
import segment_first as sf  # noqa: E402
import split_pieces as sp  # noqa: E402

PW = 380
SECTION_CLASSES = (0, 1, 3)        # primary, secondary, accent (garment indices of the standard palette; tertiary = decoration)


def hp_reference(sfdir):
    z = np.load(Path(sfdir) / 'prep' / 'prep.npz')
    meta = json.loads((Path(sfdir) / 'prep' / 'prep.json').read_text())
    hl = np.load(Path(sfdir) / 'highpoly_labels.npz')
    wear = hl['wear']
    v, f = z['v'].astype(np.float64), z['f'].astype(np.int64)
    fw = f[wear]
    role = hl['label'][wear].astype(np.int64) - 1
    piece = hl['piece'].astype(np.int64)
    vg = sf.to_glb(v, meta)
    cent = vg[fw].mean(1)
    area = sp.triangle_geometry(vg, fw)[1]
    pairs = sf.edge_pairs(fw)
    diff = role[pairs[:, 0]] != role[pairs[:, 1]]
    both = np.isin(role[pairs[:, 0]], SECTION_CLASSES) & np.isin(role[pairs[:, 1]], SECTION_CLASSES)
    sel = pairs[diff & both]
    boundary = (cent[sel[:, 0]] + cent[sel[:, 1]]) / 2
    return {'cent': cent, 'area': area, 'role': role, 'piece': piece, 'boundary': boundary, 'tree': cKDTree(boundary), 'height': 2 / meta['normalization_scale'],
            'meta': meta}


def cut_alignment(level, label, ref):
    """Distances (% of height) from points on every cut edge to the high-poly colour boundary."""
    e = sp.edge_geometry(level)
    p = level.graph.pairs
    cut = label[p[:, 0]] != label[p[:, 1]]
    v0, v1 = level.v[e['verts'][cut, 0]], level.v[e['verts'][cut, 1]]
    L = np.linalg.norm(v1 - v0, axis=1)
    pts = np.concatenate([v0, (v0 + v1) / 2, v1])
    d = ref['tree'].query(pts)[0] / ref['height'] * 100
    w = np.tile(L, 3)
    return {'cut_edges': int(cut.sum()), 'mean_pct': float(d.mean()), 'length_weighted_mean_pct': float((d * w).sum() / w.sum()),
            'p95_pct': float(np.percentile(d, 95)), 'max_pct': float(d.max())}


def mixed_stats(level, ref):
    tris = level.raw_v[level.raw_f]
    votes, _ = sf.cover_votes(tris, ref['cent'], ref['area'], ref['role'], 4)
    out = sf.top_share_stats(votes, level.area)
    pv, _ = sf.cover_votes(tris, ref['cent'], ref['area'], ref['piece'], 8)       # the same with the section the area belongs to
    out['piece_mixed_area_share'] = sf.top_share_stats(pv, level.area)['mixed_area_share']
    return out


def current_level(cdir, lv):
    manifest = json.loads((Path(cdir) / 'reduced' / 'manifest.json').read_text())
    level = sp.load_level(Path(cdir) / 'reduced', lv, manifest)
    z = np.load(Path(cdir) / 'pieces_auto' / f'lod_{lv}' / 'faces.npz', allow_pickle=True)
    return level, z['piece'].astype(np.int64), manifest


def sf_level(sfdir, lv, meta, mats):
    z = np.load(Path(sfdir) / 'levels' / f'{lv}.npz')
    level = sp.Level(lv, z['raw_v'], z['raw_f'], z['role'], mats, 2 / meta['normalization_scale'])
    return level, z['piece'].astype(np.int64)


def sections(level, label, joints_from):
    level.joints = joints_from
    level.plane = {'x': 0., 'source': 'zero', 'candidates': {}}
    spec = sp.load_spec('body_armor', None, overrides={'rigid_method': 'given', 'overlap': True, 'pose_test': True, 'joints': 'all', 'given_label': label})
    sec = sp.build_sections(level, spec, 'given', True)
    sec.joints = sp.resolve_joints(level, spec)
    return sec


def fit_bounds(b, pw, ph, head=12):
    """Widen (xmin, xmax, ymin, ymax) so that draw_view's width-based scale also fits the panel height."""
    xmin, xmax, ymin, ymax = b
    need_w = (ymax - ymin) * (pw - 24) / max(ph - head - 12, 1)
    if need_w > xmax - xmin:
        c = (xmin + xmax) / 2
        xmin, xmax = c - need_w / 2, c + need_w / 2
    return xmin, xmax, ymin, ymax


def side_items(sec):
    posed, flags, _ = sp.pose_metrics(sec, 20, 'half', True, 'both', 'spine')
    items = []
    for lab, name in sec.names.items():
        t = posed[lab][:, :, [2, 1, 0]]               # horizontal = z (front), depth = x
        c = np.tile(sp.RIGID_COLORS[name], (len(t), 1))
        c[flags[lab]] = sp.STRIP_COLOR
        items.append((t, c))
    return items


def pose_row(info, level_key='0p4'):
    pz = info['levels'][level_key]['rigid']['pose']['variants']
    out = {}
    for sc in ('arms_down', 'arms_up', 'thigh_flex', 'thigh_abd', 'spine'):
        for ov in ('no_overlap', 'overlap'):
            x = pz.get(f'half/both/{sc}/{ov}')
            out[f'{sc}/{ov}'] = None if not x else {'max': x['gap_max_pct_height'], 'mean': x['gap_mean_pct_height']}
    return out


def main(argv):
    out_path = Path(argv[0])
    from PIL import Image, ImageDraw
    font, small = sp._fonts()
    rows, report = [], {}
    for entry in argv[1:]:
        name, rest = entry.split('=', 1)
        cdir, sdir = rest.split(':', 1)
        ref = hp_reference(sdir)
        meta = ref['meta']
        cur_json = json.loads((Path(cdir) / 'pieces_auto' / 'pieces.json').read_text())
        sf_json = json.loads((Path(sdir) / 'pieces' / 'pieces.json').read_text())
        sf_seg = json.loads((Path(sdir) / 'segment_first.json').read_text())
        cur_manifest = json.loads((Path(cdir) / 'reduced' / 'manifest.json').read_text())
        rep = {'levels': {}}
        secs = {}
        for lv in sf_json['levels']:
            lc, labc, _ = current_level(cdir, lv)
            mats = lc.materials
            ls, labs = sf_level(sdir, lv, meta, mats)
            ls.joints = lc.joints
            r = {}
            for key, level, label in (('current', lc, labc), ('segment_first', ls, labs)):
                r[key] = {'wearable_triangles': int(level.n), 'mixed': mixed_stats(level, ref), 'cut_alignment': cut_alignment(level, label, ref)}
            z = np.load(Path(cdir) / 'reduced' / f'lod_error_{lv}_colors.npz', allow_pickle=True)
            if 'face_role_vote_share' in z.files:
                mid, vs, fa = z['material_id'], z['face_role_vote_share'], z['face_area']
                vs = vs.max(1) if vs.ndim == 2 else vs
                g = mid > 0
                r['current']['texture_vote_mixed_share'] = float(fa[g & (vs < .75)].sum() / fa[g].sum())
            r['current']['pieces'] = {k: {'faces': p['faces'], 'components': p['components']} for k, p in cur_json['levels'][lv]['pieces'].items()}
            r['segment_first']['pieces'] = {k: {'faces': p['faces'], 'components': p['components']} for k, p in sf_json['levels'][lv]['pieces'].items()}
            r['current']['pose'] = pose_row(cur_json, lv)
            r['segment_first']['pose'] = pose_row(sf_json, lv)
            r['current']['joints'] = {k: x['status'] for k, x in ((cur_json['levels'][lv]['rigid'].get('regions') or {}).get('joints') or {}).items()}
            r['segment_first']['reduction'] = sf_seg['levels'][lv]
            rep['levels'][lv] = r
            if lv == '0p4':
                secs['current'] = sections(lc, labc, lc.joints)
                secs['segment_first'] = sections(ls, labs, lc.joints)
        # runtime
        cur_s = sum(x.get('total_level_seconds', 0) for x in cur_manifest['levels'].values())
        rep['runtime_s'] = {'current_reduce_model_levels': cur_s, 'segment_first': sf_seg['timing_s'], 'prep_blender': meta['timing'],
                            'prep_total': meta.get('total_s')}
        rep['unseen_dominated'] = sf_seg['reflood_unseen']
        report[name] = rep
        # sheet row: assembled, exploded, spine side; current then segment-first
        items = {}
        for key in ('current', 'segment_first'):
            items[key] = {'asm': cr.static_items(secs[key], False), 'exp': cr.static_items(secs[key], True), 'side': side_items(secs[key])}
        bf = cr.bounds_of([i for k in items for i in items[k]['asm']])
        be = cr.bounds_of([i for k in items for i in items[k]['exp']])
        bs = cr.bounds_of([i for k in items for i in items[k]['side']])
        ph = max(sp.panel_height(bf, PW), sp.panel_height(be, PW))
        bs = fit_bounds(bs, PW, ph)
        row = Image.new('RGB', (PW * 6, ph + 4), (246, 246, 244))
        d = ImageDraw.Draw(row)
        for c, (key, view) in enumerate([(k, v) for k in ('current', 'segment_first') for v in ('asm', 'exp', 'side')]):
            b = {'asm': bf, 'exp': be, 'side': bs}[view]
            row.paste(sp.draw_view(items[key][view], 1, b, PW, ph), (c * PW, 4))
            lab = {'asm': 'assembled', 'exp': 'exploded', 'side': 'spine 20 deg, side'}[view]
            d.text((c * PW + 6, 6), f'{name} {"current" if key == "current" else "segment-first"} {lab}', fill=(30, 30, 30), font=small)
        r4 = rep['levels']['0p4']
        head = Image.new('RGB', (PW * 6, 26), (230, 232, 236))
        txt = (f'{name} 0p4  triangles {r4["current"]["wearable_triangles"]} -> {r4["segment_first"]["wearable_triangles"]}   mixed-face area '
               f'{r4["current"]["mixed"]["mixed_area_share"]:.1%} -> {r4["segment_first"]["mixed"]["mixed_area_share"]:.1%}   cut-to-colour-edge mean '
               f'{r4["current"]["cut_alignment"]["mean_pct"]:.2f}% -> {r4["segment_first"]["cut_alignment"]["mean_pct"]:.2f}% H')
        ImageDraw.Draw(head).text((6, 6), txt, fill=(20, 20, 20), font=small)
        rows += [head, row]
    sheet = Image.new('RGB', (PW * 6, sum(r.height for r in rows)), (246, 246, 244))
    y = 0
    for r in rows:
        sheet.paste(r, (0, y))
        y += r.height
    if max(sheet.size) > 2400:
        f_ = 2400 / max(sheet.size)
        sheet = sheet.resize((int(sheet.width * f_), int(sheet.height * f_)), Image.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, quality=88)
    print('SHEET', out_path, sheet.size)
    Path(str(out_path) + '.json').write_text(json.dumps(report, indent=2, default=float))


if __name__ == '__main__':
    main(sys.argv[1:])
