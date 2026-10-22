"""Tests for split_pieces.py on a synthetic labelled armour (and, when present, a real reduced plate mail).

Run: python3 -m unittest tools/tripo_pipeline/test_split_pieces.py   (from the parent project; needs numpy + Pillow)
Dump the synthetic reduced folders for inspection: python3 tools/tripo_pipeline/test_split_pieces.py --dump <dir>

The synthetic armour is ONE welded open tube (48 x 16 cells, 2 triangles each) with a bump on each shoulder block
(6 x 6 cells at the left/right side, rows 10..15). Rim style: the block's outer ring of cells is accent (magenta) and the
4 x 4 field inside is primary/secondary decor. Roles style: the block is tertiary/accent. A stray speck sits on the chest.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import split_pieces as sp  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
NU, NV, NBASE = 48, 16, 3
LEFT_COLS = [(-3 + k) % NU for k in range(6)]        # cells 45..47, 0..2 (anatomical left = +X)
RIGHT_COLS = [21 + k for k in range(6)]
BLOCK_ROWS = list(range(10, 16))
MATERIALS = [('base', (.9, .1, .1, 1)), ('primary', (.1, .82, .16, 1)), ('secondary', (.12, .25, .92, 1)),
             ('tertiary', (.96, .86, .08, 1)), ('accent', (.88, .1, .86, 1)), ('quinary', (.08, .86, .92, 1)), ('joint', (.08, .86, .92, 1))]
ROLE = {name: i for i, (name, _) in enumerate(MATERIALS)}
PART_NAMES = ['spine_01.x', 'shoulder.l', 'shoulder.r', 'arm_stretch.l', 'arm_stretch.r', 'thigh_stretch.l', 'root.x', 'thigh_stretch.r']


def cell_parts(i, j):
    """Body part name of a cell: shoulder blocks widened by two columns (room for stray specks), arms below them."""
    left = i <= 4 or i >= NU - 5
    right = 19 <= i <= 28
    if j >= 10:
        return 'shoulder.l' if left else 'shoulder.r' if right else 'spine_01.x'
    if 6 <= j <= 9:
        return 'arm_stretch.l' if left else 'arm_stretch.r' if right else 'spine_01.x'
    return 'spine_01.x' if j >= 0 else 'thigh_stretch.l'


def bands_left(i):
    return i <= 11 or i >= 36


def cell_parts_b(i, j):
    """Parts of the joint-band synthetic: thighs rows 0-1 (left half = .l), hip band row 2 and pelvis rows 3-4 = root, waist
    band row 5 and chest = spine, arms rows 7-9 under the pauldron blocks (rows 10+)."""
    left = i <= 4 or i >= NU - 5
    right = 19 <= i <= 28
    if j >= 10:
        return 'shoulder.l' if left else 'shoulder.r' if right else 'spine_01.x'
    if 7 <= j <= 9:
        return 'arm_stretch.l' if left else 'arm_stretch.r' if right else 'spine_01.x'
    if j >= 5:
        return 'spine_01.x'
    if j >= 2:
        return 'root.x'
    return 'thigh_stretch.l' if bands_left(i) else 'thigh_stretch.r'


PLATES_L = {(i, j) for i in LEFT_COLS for j in (8, 9)}
PLATES_R = {(i, j) for i in RIGHT_COLS for j in (8, 9)}


def build_cells_b(style='rim', no_hip_band=False, no_right_arm_band=False, no_cyan=False, **_):
    """Role per cell of the joint-band synthetic (see cell_parts_b); cyan role 'joint' on the bands."""
    role, truth = {}, {'L': set(), 'R': set()}
    for j in range(-NBASE, NV):
        for i in range(NU):
            role[(i, j)] = 'base' if j < 0 else 'primary'
    for side, cols in (('L', LEFT_COLS), ('R', RIGHT_COLS)):
        for ci, i in enumerate(cols):
            for rj, j in enumerate(BLOCK_ROWS):
                truth[side].add((i, j))
                edge = ci in (0, 5) or rj in (0, 5)
                role[(i, j)] = 'accent' if edge else ('secondary' if (ci + rj) % 2 else 'primary')
    band = 'primary' if no_cyan else 'joint'
    for i in range(NU):
        role[(i, 5)] = band                                   # waist band
        role[(i, 2)] = 'primary' if no_hip_band else band     # hip band
    for (i, j) in [(i, j) for j in range(7, 10) for i in range(NU)]:
        part = cell_parts_b(i, j)
        if not part.startswith('arm_stretch'):
            continue
        plate = (i, j) in PLATES_L or (i, j) in PLATES_R
        if part.endswith('.r') and no_right_arm_band:
            role[(i, j)] = 'primary'
        else:
            role[(i, j)] = 'primary' if plate else band
    return role, truth


def build_cells_r(style='rim', no_pelvis_colour=False, arm_role='secondary', deco=True, straddle=False, **_):
    """Per-piece base colours on the band layout (cell_parts_b): thighs rows 0-1 and chest green, pelvis rows 2-4 and the
    arm cells blue; pauldron blocks as in build_cells_b. deco: a 3x3 yellow patch inside the chest and a 2x2 inside the
    left arm; straddle: a yellow strip across the left arm / chest boundary."""
    role, truth = build_cells_b(style, no_cyan=True)
    for j in range(0, NV):
        for i in range(NU):
            if role[(i, j)] in ('accent',) or (i, j) in {c for s_ in ('L', 'R') for c in truth[s_]}:
                continue
            part = cell_parts_b(i, j)
            if 2 <= j <= 4:
                role[(i, j)] = 'primary' if no_pelvis_colour else 'secondary'
            elif part.startswith('arm_stretch'):
                role[(i, j)] = arm_role
            else:
                role[(i, j)] = 'primary'
    if deco:
        for i in (11, 12, 13):
            for j in (11, 12, 13):
                role[(i, j)] = 'tertiary'
        for c in ((0, 8), (1, 8), (0, 9), (1, 9)):
            role[c] = 'tertiary'
    if straddle:                                           # arm cells (5..) are blue; cells (2,6),(2,7) straddle the lower arm edge
        for c in ((2, 6), (2, 7)):
            role[c] = 'tertiary'
    return role, truth


def build_cells(style, gap=False, speck=True, island=False, hole=False, arm_colour=False, seam=False, cyan=False, hip_band=False):
    """role name per cell (i, j) of the whole grid (j = -3..15), plus the truth sets of block cells."""
    role = {}
    for j in range(-NBASE, NV):
        for i in range(NU):
            role[(i, j)] = 'base' if j < 0 else ('primary' if (j // 3) % 2 == 0 else 'secondary')
    truth = {'L': set(), 'R': set()}
    for side, cols in (('L', LEFT_COLS), ('R', RIGHT_COLS)):
        for ci, i in enumerate(cols):
            for rj, j in enumerate(BLOCK_ROWS):
                truth[side].add((i, j))
                edge = ci in (0, 5) or rj in (0, 5)
                if style == 'rim':
                    role[(i, j)] = 'accent' if edge else ('secondary' if (ci + rj) % 2 else 'primary')
                else:
                    role[(i, j)] = 'accent' if (ci + rj) % 2 else 'tertiary'
    if arm_colour:                                       # arm cells above row 6 are blue: a colour edge between rows 6 and 7
        for (i, j) in list(role):
            if 7 <= j <= 9 and cell_parts(i, j).startswith('arm_stretch'):
                role[(i, j)] = 'secondary'
            elif j == 6 and cell_parts(i, j).startswith('arm_stretch'):
                role[(i, j)] = 'primary'
    plates = {'L': {(i, j) for i in LEFT_COLS for j in (7, 8, 9)}, 'R': {(i, j) for i in RIGHT_COLS for j in (7, 8, 9)}}
    if seam:                                             # blue soft band round the arm plates (rows 7..9 under the pauldron blocks)
        for (i, j) in list(role):
            if cell_parts(i, j).startswith('arm_stretch'):
                role[(i, j)] = 'primary' if (i, j) in plates['L'] | plates['R'] else 'secondary'
            elif role[(i, j)] == 'secondary':            # no other blue on the armour (it would join the band)
                role[(i, j)] = 'primary'
    if hip_band:
        for i in range(NU):
            for j in (1, 2):
                role[(i, j)] = 'secondary'
    if cyan:                                             # cyan arm plates with a 2x2 decoration enclosed
        for (i, j) in ARM_CELLS:
            role[(i, j)] = 'quinary'
        for c in ((1, 8), (2, 8), (1, 7), (2, 7)):
            role[c] = 'primary'
    marker = 'accent' if style == 'rim' else 'tertiary'
    if cyan and speck:
        role[(12, 6)] = 'quinary'
        speck = False
    if speck:
        role[(12, 6)] = marker                           # chest speck, spine_01.x
    if island:
        role[(4, 12)] = marker                           # in the shoulder region but two columns from the block
    if hole and style == 'roles':
        role[(0, 12)] = 'primary'                        # 1-cell hole inside the left pauldron
    if gap:
        role[(LEFT_COLS[2], BLOCK_ROWS[0])] = 'primary'  # a missing rim cell on the left block's lower edge
        truth['gap'] = (LEFT_COLS[2], BLOCK_ROWS[0])
    return role, truth


def build_vertices():
    rows = NV + NBASE + 1
    v = np.zeros((NU * rows, 3))
    for jj in range(rows):
        j = jj - NBASE
        for i in range(NU):
            phi = 2 * np.pi * i / NU
            r = .17 + .004 * j
            bump = 0.
            for ci in (0., NU / 2):
                di = min(abs(i - ci), NU - abs(i - ci))
                bump += .05 * np.exp(-(di ** 2 / (2 * 2.2 ** 2) + (j - 13) ** 2 / (2 * 2.2 ** 2)))
            r += bump
            v[jj * NU + i] = (r * np.cos(phi), .40 + .03 * j, r * np.sin(phi) * .62)
    return v


def vid(i, j):
    return (j + NBASE) * NU + (i % NU)


def make_reduced(root, style='rim', parts=True, mismatch=False, wear_only_levels=('0p4',), bands=False, regions=False, **kw):
    """Write a synthetic reduce_model.py-like folder; returns (reduced_dir, truth, info)."""
    root = Path(root)
    reduced = root / 'plate' / 'human_male' / 'body_armor' / 'reduced'
    reduced.mkdir(parents=True)
    bands = bands or regions
    role, truth = (build_cells_r(style, **kw) if regions else build_cells_b(style, **kw) if bands else build_cells(style, **kw))
    parts_of = cell_parts_b if bands else cell_parts
    v = build_vertices()
    cells = [(i, j) for j in range(-NBASE, NV) for i in range(NU)]
    if bands:                                             # only a left leg: no thigh_r faces (hip_r is absent)
        cells = [(i, j) for (i, j) in cells if not (0 <= j <= 1 and not bands_left(i))]
    faces, mats, cell_of = [], [], []
    for (i, j) in cells:
        a, b, c, d = vid(i, j), vid(i + 1, j), vid(i + 1, j + 1), vid(i, j + 1)
        for tri in ((a, b, c), (a, c, d)):
            faces.append(tri)
            mats.append(ROLE[role[(i, j)]])
            cell_of.append((i, j))
    faces, mats = np.array(faces), np.array(mats)
    order = np.argsort(mats, kind='stable')               # the exporter groups faces by material slot, stable
    faces_o, mats_o = faces[order], mats[order]
    cell_o = [cell_of[k] for k in order]
    materials = [{'name': n, 'color': list(c)} for n, c in MATERIALS]
    names = ['0p4']
    manifest = {'module': 'body_armor', 'normalization_scale': 2. / 1.0, 'normalization_origin_rotated': [0, 0, 0],
                'rotation_to_review': np.eye(3).tolist(), 'levels': {}}
    for level in names:
        sp.write_glb(reduced / f'lod_error_{level}.glb', 'full', v, faces_o, mats_o, materials)
        keep = mats_o != 0
        wear_cells = [c for c, k in zip(cell_o, keep) if k]
        sp.write_glb(reduced / f'lod_error_{level}__wearable.glb', 'wear', v, faces_o[keep], mats_o[keep], materials)
        part_id = np.array([PART_NAMES.index(parts_of(*c)) if parts_of(*c) in PART_NAMES else 0 for c in cell_o])
        if parts:
            wear_part = part_id[keep]
            if mismatch:                                   # the known issue: npz wearable arrays have 2 faces more
                wear_part = np.r_[wear_part, wear_part[:2]]
            np.savez_compressed(reduced / f'lod_error_{level}_colors.npz', body_part_names=np.array(PART_NAMES),
                                body_part_glb_order=part_id, glb_face_order=np.arange(len(part_id)),
                                wearable_body_part_glb_order=wear_part, face_label_source=np.zeros(len(part_id), int),
                                label_source_names=np.array(['texture', 'symmetry', 'prior', 'neighbours', 'off_body', 'riser']), face_confidence=np.ones(len(part_id)))
        manifest['levels'][level] = {}
    if bands and not regions and not kw.get('no_cyan'):
        manifest['palette'] = {'garments': [{'role': n} for n in ('primary', 'secondary', 'tertiary', 'accent', 'joint')]}
    (reduced / 'manifest.json').write_text(json.dumps(manifest))
    return reduced, truth, {'role': role, 'cells': cell_o, 'wear_cells': wear_cells, 'faces': faces_o[keep],
                            'mats': mats_o[keep], 'vertices': v}


def _setup(testcase):
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    return Path(tmp.name)


def run(root, reduced, **kw):
    out = Path(root) / 'out'
    kw.setdefault('render', False)
    kw.setdefault('blend', False)
    return sp.run(reduced, output_dir=out, **kw), out


def piece_cells(result_out, info, level='0p4'):
    z = np.load(Path(result_out) / f'lod_{level}' / 'faces.npz', allow_pickle=True)
    piece = z['piece']
    cells = info['wear_cells']
    return {k: {cells[i] for i in np.flatnonzero(piece == lab)} for lab, k in ((0, 'rest'), (1, 'L'), (2, 'R'))}, piece


class GlbTest(unittest.TestCase):
    def test_roundtrip(self):
        root = _setup(self)
        v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1.]])
        f = np.array([[0, 1, 2], [0, 2, 3], [1, 3, 2]])
        mats = [{'name': 'a', 'color': [1, 0, 0, 1]}, {'name': 'b', 'color': [0, 1, 0, 1]}]
        sp.write_glb(root / 'x.glb', 'x', v, f, np.array([1, 0, 1]), mats)
        g = sp.read_glb(root / 'x.glb')
        self.assertEqual(len(g['f']), 3)
        self.assertEqual([m['name'] for m in g['materials']], ['a', 'b'])
        # faces are grouped by material (a first); positions are preserved exactly (float32)
        np.testing.assert_allclose(g['v'][g['f']][0], v[f[1]], atol=1e-7)
        self.assertEqual(g['face_material'].tolist(), [0, 1, 1])

    def test_weld_and_pairs(self):
        v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 0], [1, 0, 0], [1, 1, 0.]])
        f = np.array([[0, 1, 2], [3, 5, 4]])
        wid, wv = sp.weld(v, 1e-6)
        self.assertEqual(len(wv), 4)
        pairs, counts = sp.face_pairs(wid[f])
        self.assertEqual(pairs.tolist(), [[0, 1]])
        self.assertEqual(counts.tolist(), [1])


class SyntheticTest(unittest.TestCase):
    def test_roles_style_splits_left_right_rest(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'roles')
        res, out = run(root, reduced, style='roles')
        cells, piece = piece_cells(out, info)
        self.assertEqual({c for c in cells['L']}, truth['L'])
        self.assertEqual({c for c in cells['R']}, truth['R'])
        self.assertIn((12, 6), cells['rest'])                       # the chest speck is not a pauldron
        lv = res['levels']['0p4']
        self.assertEqual(len(lv['stray_seed_components']), 1)
        self.assertEqual(lv['stray_seed_components'][0]['parts'], {'spine_01.x': 2})
        self.assertEqual([lv['pieces'][k]['components'] for k in ('pauldron_l', 'pauldron_r', 'rest')], [1, 1, 1])
        self.assertEqual(lv['pieces']['pauldron_l']['faces'], 72)
        self.assertEqual(lv['pieces']['pauldron_r']['faces'], 72)
        self.assertEqual(sum(p['faces'] for p in lv['pieces'].values()), lv['wearable_faces'])
        self.assertEqual(lv['body_parts']['method'], 'full_glb_centroid')
        self.assertEqual(lv['body_parts']['unmatched'], 0)

    def test_symmetry_numbers(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'roles')
        res, out = run(root, reduced, style='roles')
        sym = res['levels']['0p4']['symmetry']
        self.assertAlmostEqual(sym['area_ratio_l_over_r'], 1.0, places=2)
        self.assertLess(sym['centroid_mirror_error_pct_height'], 1.0)
        self.assertEqual(sym['mirror_face_match_l_to_r'], 1.0)
        self.assertEqual(sym['mirror_face_match_r_to_l'], 1.0)
        self.assertLess(abs(res['levels']['0p4']['plane']['x']), 5e-3)

    def test_rim_style_takes_rim_and_enclosed_field(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim')
        res, out = run(root, reduced, style='rim')
        cells, _ = piece_cells(out, info)
        self.assertEqual(cells['L'], truth['L'])
        self.assertEqual(cells['R'], truth['R'])
        self.assertIn((12, 6), cells['rest'])
        lv = res['levels']['0p4']
        self.assertGreater(lv['rim_fields'][0]['faces'], 0)
        self.assertEqual([lv['pieces'][k]['components'] for k in ('pauldron_l', 'pauldron_r')], [1, 1])
        self.assertEqual(lv['warnings'], [])

    def test_rim_gap_leaks_without_closing_and_is_recovered_with_it(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim', gap=True)
        # no erosion pass: the field is connected to the body through the gap, so only the rim is a pauldron
        res0, out0 = run(root, reduced, style='rim', overrides={'close_rings': 0})
        rim_only = res0['levels']['0p4']['pieces']['pauldron_l']['faces']
        self.assertLess(rim_only, 60)
        self.assertTrue(any('no enclosed field' in w for w in res0['levels']['0p4']['warnings']))
        # with the erosion/flood pass every field cell (and the rim) comes back
        root2 = _setup(self)
        reduced2, truth2, info2 = make_reduced(root2, 'rim', gap=True)
        res1, out1 = run(root2, reduced2, style='rim')
        cells, _ = piece_cells(out1, info2)
        field = {(i, j) for (i, j) in truth2['L'] if i in LEFT_COLS[1:5] and j in BLOCK_ROWS[1:5]}
        self.assertTrue(field <= cells['L'])
        self.assertGreaterEqual(len(cells['L']), 34)       # field 16 cells + 19 rim cells (+ maybe the gap cell)
        self.assertEqual(res1['levels']['0p4']['pieces']['pauldron_l']['components'], 1)
        self.assertEqual(cells['R'], truth2['R'])

    def test_islands_absorbed_both_ways(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'roles', island=True, hole=True)
        res, out = run(root, reduced, style='roles')
        cells, _ = piece_cells(out, info)
        self.assertIn((4, 12), cells['rest'])              # a 2-face seed island in the region -> rest
        self.assertIn((0, 12), cells['L'])                 # a 2-face hole inside the pauldron -> pauldron
        islands = res['levels']['0p4']['absorbed_islands']
        pairs = sorted((x['from'], x['to'], x['faces']) for x in islands)
        self.assertIn(('L', 'rest', 2), pairs)
        self.assertIn(('rest', 'L', 2), pairs)

    def test_no_parts_uses_plane_for_sides(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'roles', parts=False, speck=False)
        res, out = run(root, reduced, style='roles')
        lv = res['levels']['0p4']
        self.assertEqual(lv['body_parts']['method'], 'none')
        self.assertTrue(any('no body parts' in w for w in lv['warnings']))
        self.assertEqual([lv['pieces'][k]['faces'] for k in ('pauldron_l', 'pauldron_r')], [72, 72])
        self.assertIn(lv['plane']['source'], ('parts_npz', 'wearable_bbox_centre'))
        self.assertLess(abs(lv['plane']['x']), 5e-3)

    def test_wearable_with_different_face_count_than_npz(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'roles', mismatch=True)
        res, out = run(root, reduced, style='roles')
        lv = res['levels']['0p4']
        self.assertEqual(lv['body_parts']['method'], 'full_glb_centroid')
        self.assertEqual(lv['body_parts']['wearable_array_length_mismatch'][0] - lv['body_parts']['wearable_array_length_mismatch'][1], 2)
        self.assertEqual(lv['pieces']['pauldron_l']['faces'], 72)

    def test_outputs_pieces_are_independent_and_complete(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim')
        res, out = run(root, reduced, style='rim', render=True)
        lod = out / 'lod_0p4'
        total = 0
        positions = []
        for name in ('pauldron_l', 'pauldron_r', 'rest'):
            g = sp.read_glb(lod / f'{name}.glb')
            total += len(g['f'])
            self.assertTrue(set(m['name'] for m in g['materials']) <= {'primary', 'secondary', 'accent', 'tertiary'})
            positions.append(g['v'])
        self.assertEqual(total, res['levels']['0p4']['wearable_faces'])
        self.assertTrue((lod / 'pieces_render.png').is_file())
        self.assertTrue((lod / 'faces.npz').is_file())
        pj = json.loads((out / 'pieces.json').read_text())
        for key in ('faces', 'area', 'components', 'bbox', 'centroid'):
            self.assertIn(key, pj['levels']['0p4']['pieces']['pauldron_l'])
        for key in ('area_ratio_l_over_r', 'centroid_mirror_error', 'centroid_mirror_error_pct_height'):
            self.assertIn(key, pj['levels']['0p4']['symmetry'])
        for key in ('absorbed_islands', 'ambiguous_faces'):
            self.assertIn(key, pj['levels']['0p4'])
        # each piece GLB has only its own vertices: the left pauldron lies on +X, the right on -X
        self.assertGreater(positions[0][:, 0].mean(), 0.05)
        self.assertLess(positions[1][:, 0].mean(), -0.05)

    def test_refuses_to_overwrite(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'roles')
        run(root, reduced, style='roles')
        with self.assertRaises(SystemExit):
            run(root, reduced, style='roles')

    def test_cli_overrides(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'roles')
        out = root / 'cli'
        sp.main([str(reduced), '--style', 'roles', '--roles', 'accent', '--output-dir', str(out), '--no-blend', '--no-render',
                 '--min-island', '3'])
        j = json.loads((out / 'pieces.json').read_text())
        self.assertEqual(j['levels']['0p4']['spec']['roles'], ['accent'])
        self.assertEqual(j['levels']['0p4']['spec']['min_island_faces'], 3)

    def test_blender_blend_smoke(self):
        blender = sp.find_blender()
        if not Path(blender).is_file():
            self.skipTest('no Blender')
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim')
        res, out = run(root, reduced, style='rim', blend=True)
        blend = out / 'lod_0p4' / 'pieces_0p4.blend'
        self.assertTrue(blend.is_file(), res['levels']['0p4']['outputs'])


class RealPlateMailTest(unittest.TestCase):
    """The legacy 2-colour plate mail has no pauldron colours: treat 'secondary' faces on the shoulder / upper-arm
    parts as the pauldron set through the CLI overrides. Skipped when the model is not on this machine."""
    REDUCED = ROOT / 'models/plain_plate_mail_01/cb0e04f84bfb/orc_male/body_armor/reduced'

    def test_orc_male_secondary_on_shoulder_parts(self):
        if not (self.REDUCED / 'manifest.json').is_file():
            self.skipTest('orc_male plate mail not present')
        root = _setup(self)
        res, out = run(root, self.REDUCED, style='roles',
                       overrides={'roles': ['secondary'], 'seed_in_region': True, 'region_parts': ['shoulder', 'arm_stretch']})
        for level, lv in res['levels'].items():
            self.assertEqual(sum(p['faces'] for p in lv['pieces'].values()), lv['wearable_faces'])
            self.assertEqual(lv['pieces']['pauldron_l']['components'], 1, level)
            self.assertEqual(lv['pieces']['pauldron_r']['components'], 1, level)
            self.assertEqual(lv['body_parts']['unmatched'], 0)
            self.assertGreater(lv['pieces']['pauldron_l']['centroid'][0], lv['plane']['x'])
            self.assertLess(lv['pieces']['pauldron_r']['centroid'][0], lv['plane']['x'])
            self.assertLess(lv['symmetry']['centroid_mirror_error_pct_height'], 5)


ARM_CELLS = {(i, j) for j in range(6, 10) for i in list(range(0, 5)) + list(range(NU - 5, NU)) + list(range(19, 29))}
ARM_L = {(i, j) for j in range(6, 10) for i in list(range(0, 5)) + list(range(NU - 5, NU))}
ARM_R = {(i, j) for j in range(6, 10) for i in range(19, 29)}
JOINTS = {'joint_l': [.16, .62, 0.], 'joint_r': [-.16, .62, 0.], 'joint_hip_l': [.14, .46, 0.], 'joint_hip_r': [-.14, .46, 0.],
          'joint_waist': [0., .55, 0.]}


def rigid_run(testcase, method, style='roles', overlap=False, pose=False, render=False, extra=None, **kw):
    root = _setup(testcase)
    reduced, truth, info = make_reduced(root, style, **kw)
    ov = {'rigid_method': method, 'overlap': True if overlap else None, 'pose_test': True if pose else None, **JOINTS, **(extra or {})}
    res, out = run(root, reduced, style=style, overrides=ov, render=render)
    return res, out, info, truth


class RigidSyntheticTest(unittest.TestCase):
    def test_zones_cut_arms_by_body_part(self):
        res, out, info, truth = rigid_run(self, 'zones')
        cells, piece = piece_cells3(out, info)
        self.assertEqual(cells['arm_l'], ARM_L)
        self.assertEqual(cells['arm_r'], ARM_R)
        self.assertEqual(cells['pauldron_l'], truth['L'])
        self.assertEqual(cells['pauldron_r'], truth['R'])
        self.assertIn((12, 6), cells['torso'])
        lv = res['levels']['0p4']
        self.assertEqual([lv['pieces'][k]['components'] for k in ('torso', 'pauldron_l', 'pauldron_r', 'arm_l', 'arm_r')], [1] * 5)
        self.assertEqual(sum(p['faces'] for p in lv['pieces'].values()), lv['wearable_faces'])
        pairs = {frozenset(c['pair']) for c in lv['rigid']['cuts']}
        self.assertEqual(pairs, {frozenset(x) for x in (('arm_l', 'torso'), ('arm_r', 'torso'), ('arm_l', 'pauldron_l'),
                                                         ('arm_r', 'pauldron_r'), ('pauldron_l', 'torso'), ('pauldron_r', 'torso'))})

    def test_snap_moves_the_cut_onto_a_colour_edge(self):
        res0, out0, info0, _ = rigid_run(self, 'zones', arm_colour=True)
        res1, out1, info1, _ = rigid_run(self, 'snap', arm_colour=True)
        c0, _ = piece_cells3(out0, info0)
        c1, _ = piece_cells3(out1, info1)
        # zone guess: whole arm zone; snap: row 6 (primary, like the torso) goes back to the torso, the cut sits on the colour edge
        self.assertTrue(ARM_L <= c0['arm_l'] | c0['torso'] and (3, 6) in c0['arm_l'])
        self.assertTrue(all((i, 6) in c1['torso'] for i in (0, 1, 2, 3)))
        self.assertTrue(all((i, 7) in c1['arm_l'] for i in (0, 1, 2, 3)))
        cut0 = [c for c in res0['levels']['0p4']['rigid']['cuts'] if set(c['pair']) == {'arm_l', 'torso'}][0]
        cut1 = [c for c in res1['levels']['0p4']['rigid']['cuts'] if set(c['pair']) == {'arm_l', 'torso'}][0]
        self.assertGreater(cut1['role_boundary_share'], cut0['role_boundary_share'])
        self.assertLess(cut1['snap_cost'], cut0['snap_cost'])
        self.assertEqual(res1['levels']['0p4']['pieces']['arm_l']['components'], 1)
        self.assertEqual(res1['levels']['0p4']['pieces']['torso']['components'], 1)

    def test_overlap_strips_are_flagged_extra_geometry(self):
        res, out, info, truth = rigid_run(self, 'zones', overlap=True,
                                          extra={'overlap_mode': 'parent', 'overlap_length': .03, 'pose_convention': 'arm'})
        lv = res['levels']['0p4']
        ov = lv['rigid']['overlap']
        self.assertTrue(ov)
        self.assertTrue(all(o['owner'] == 'torso' for o in ov if o['under'].startswith('arm')))
        z = np.load(out / 'lod_0p4' / 'faces.npz', allow_pickle=True)
        n_strip = sum(o['strip_faces'] for o in ov)
        total_flagged = sum(int(z[k].sum()) for k in z.files if k.endswith('__overlap'))
        self.assertEqual(total_flagged, n_strip)
        g = sp.read_glb(out / 'lod_0p4' / 'torso.glb')
        self.assertEqual(len(g['f']), lv['pieces']['torso']['faces'] + int(z['torso__overlap'].sum()))
        for o in ov:                                       # 3% of height long (<=), sunk 0.4% below the other surface
            self.assertLessEqual(o['max_extrusion_pct_height'], 3.2)
            self.assertGreater(o['strip_area'], 0)

    def test_strip_lies_under_the_covering_surface(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'roles')
        level = sp.load_level(reduced, '0p4', json.loads((reduced / 'manifest.json').read_text()))
        spec = sp.load_spec('body_armor', 'roles', overrides={'rigid_method': 'zones', 'overlap': True, **JOINTS})
        sec = sp.build_sections(level, spec, 'zones', True)
        strip, flag = sec.tris(0, True)
        strip = strip[flag]
        self.assertGreater(len(strip), 0)
        surface = level.raw_v[level.raw_f]                  # every face of the armour: the strip tucks under one of them
        _, _, dist = sp.closest_points(strip.reshape(-1, 3), surface)
        off = spec['rigid']['overlap']['offset'] * level.height
        self.assertLessEqual(dist.max(), off + 1e-9)        # cut vertices sit on the surface, the rest at most `offset` below it
        self.assertGreater(dist.max(), .5 * off)            # ... and some really are sunk

    def test_pose_overlap_reduces_gap(self):
        res, out, info, truth = rigid_run(self, 'zones', overlap=True, pose=True, render=True)
        pose = res['levels']['0p4']['rigid']['pose']
        v = pose['variants']
        for conv in ('arm', 'torso'):
            no, yes = v[f'{conv}/parent/arms_up/no_overlap'], v[f'{conv}/parent/arms_up/overlap']
            self.assertLessEqual(yes['gap_mean_pct_height'], no['gap_mean_pct_height'] + 1e-9)
        self.assertLess(v['arm/parent/arms_up/overlap']['gap_mean_pct_height'], v['arm/parent/arms_up/no_overlap']['gap_mean_pct_height'])
        self.assertEqual({k: pose['angles_deg'][k] for k in ('arms_down', 'arms_up')}, {'arms_down': 45, 'arms_up': -30})
        self.assertTrue((out / 'lod_0p4' / 'pose_render.png').is_file())

    def test_pose_rotation_directions(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'roles')
        level = sp.load_level(reduced, '0p4', json.loads((reduced / 'manifest.json').read_text()))
        spec = sp.load_spec('body_armor', 'roles', overrides={'rigid_method': 'zones', **JOINTS})
        sec = sp.build_sections(level, spec, 'zones', False)
        T = sp.pose_transforms(sec, 45, 'arm')
        tip_l = sp.apply_T(np.array([[.5, .62, 0.]]), T[3])[0]      # left arm tip (+X) goes down
        tip_r = sp.apply_T(np.array([[-.5, .62, 0.]]), T[4])[0]
        self.assertLess(tip_l[1], .62 - .2)
        self.assertLess(tip_r[1], .62 - .2)
        self.assertGreater(tip_l[0], 0)
        self.assertIn(1, sp.pose_transforms(sec, 45, 'arm'))
        self.assertNotIn(1, sp.pose_transforms(sec, 45, 'torso'))

    def test_default_output_unchanged_without_rigid(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'roles')
        res, out = run(root, reduced, style='roles')
        self.assertEqual(sorted(res['levels']['0p4']['pieces']), ['pauldron_l', 'pauldron_r', 'rest'])
        self.assertNotIn('rigid', res['levels']['0p4'])


class RigidMethodsTest(unittest.TestCase):
    PLATES_L = {(i, j) for i in LEFT_COLS for j in (7, 8, 9)}
    PLATES_R = {(i, j) for i in RIGHT_COLS for j in (7, 8, 9)}

    def test_seam_soft_piece(self):
        res, out, info, truth = rigid_run(self, 'seam', style='rim', seam=True, speck=False)
        cells, _ = piece_cells3(out, info)
        lv = res['levels']['0p4']
        self.assertEqual(cells['arm_l'], self.PLATES_L)
        self.assertEqual(cells['arm_r'], self.PLATES_R)
        self.assertEqual(cells['soft'], ARM_CELLS - self.PLATES_L - self.PLATES_R)
        self.assertEqual(lv['pieces']['soft']['components'], 2)
        self.assertEqual(sum(p['faces'] for p in lv['pieces'].values()), lv['wearable_faces'])
        self.assertEqual(lv['rigid']['seam']['seam_soft']['mode'], 'piece')

    def test_seam_nearest_has_no_soft_piece(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim', seam=True, speck=False)
        res, out = run(root, reduced, style='rim', overrides={'rigid_method': 'seam', 'soft_mode': 'nearest', **JOINTS})
        cells, _ = piece_cells3(out, info)
        lv = res['levels']['0p4']
        self.assertNotIn('soft', lv['pieces'])
        self.assertTrue(self.PLATES_L <= cells['arm_l'] and self.PLATES_R <= cells['arm_r'])
        self.assertEqual(sum(p['faces'] for p in lv['pieces'].values()), lv['wearable_faces'])
        self.assertEqual(lv['pieces']['arm_l']['components'], 1)
        self.assertEqual(lv['pieces']['torso']['components'], 1)

    def test_seam_blue_far_from_the_arm_stays_rigid(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim', seam=True, speck=False)
        # hip cloth: recolour torso rows 1..2 blue in the written wearable via a second synthetic with an extra band
        reduced2, truth2, info2 = make_reduced(_setup(self), 'rim', seam=True, speck=False, hip_band=True)
        res, out = run(root, reduced2, style='rim', overrides={'rigid_method': 'seam', **JOINTS})
        lv = res['levels']['0p4']
        self.assertEqual(len(lv['rigid']['seam']['seam_not_soft']), 1)           # the hip band: 0% on the arm parts
        cells, _ = piece_cells3(out, info2)
        self.assertTrue(all((12, j) in cells['torso'] for j in (1, 2)))
        self.assertEqual(lv['pieces']['soft']['components'], 2)

    def test_colour_method_takes_quinary_and_enclosed_decoration(self):
        res, out, info, truth = rigid_run(self, 'colour', style='rim', cyan=True)
        cells, _ = piece_cells3(out, info)
        lv = res['levels']['0p4']
        self.assertEqual(cells['arm_l'], ARM_L)
        self.assertEqual(cells['arm_r'], ARM_R)
        self.assertIn((12, 6), cells['torso'])                      # cyan speck on the chest is not an arm
        self.assertEqual(len(lv['rigid']['colour']['stray_components']), 1)
        self.assertGreater(lv['rigid']['colour']['field_faces'], 0)  # the 2x2 decoration is enclosed by the cyan plate
        self.assertEqual([lv['pieces'][k]['components'] for k in ('torso', 'arm_l', 'arm_r')], [1, 1, 1])

    def test_colour_method_needs_the_role(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim')
        with self.assertRaises(SystemExit):
            run(root, reduced, style='rim', overrides={'rigid_method': 'colour', **JOINTS})

    def test_half_convention_turns_pauldrons_half_the_arm_angle(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'roles')
        level = sp.load_level(reduced, '0p4', json.loads((reduced / 'manifest.json').read_text()))
        spec = sp.load_spec('body_armor', 'roles', overrides={'rigid_method': 'zones', **JOINTS})
        sec = sp.build_sections(level, spec, 'zones', False)
        T = sp.pose_transforms(sec, 40, 'half')
        ang = lambda R: np.degrees(np.arctan2(R[1, 0], R[0, 0]))
        self.assertAlmostEqual(ang(T[3][0]), -40, places=6)
        self.assertAlmostEqual(ang(T[1][0]), -20, places=6)
        self.assertAlmostEqual(ang(T[4][0]), 40, places=6)
        self.assertAlmostEqual(ang(T[2][0]), 20, places=6)

    def test_overlap_modes_and_gap(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'roles')
        level = sp.load_level(reduced, '0p4', json.loads((reduced / 'manifest.json').read_text()))
        spec = sp.load_spec('body_armor', 'roles', overrides={'rigid_method': 'zones', 'overlap': True, **JOINTS})
        sec = sp.build_sections(level, spec, 'zones', True)
        parent, rp = sp.get_strips(sec, 'parent', 'half')
        both, rb = sp.get_strips(sec, 'both', 'half')
        child, rc = sp.get_strips(sec, 'child', 'half')
        self.assertEqual({o['owner'] for o in rp}, {'torso', 'pauldron_l', 'pauldron_r'})   # the static / less-moving piece
        self.assertEqual(len(rb), 2 * len(rp))                                                # both sides own one
        self.assertTrue({'arm_l', 'arm_r'} <= {o['owner'] for o in rc})                       # mover-owned
        for ang in (45, -30):
            _, _, c_no = sp.pose_metrics(sec, ang, 'half', False, 'both')
            _, _, c_yes = sp.pose_metrics(sec, ang, 'half', True, 'both')
            self.assertLessEqual(np.mean([c['gap_mean_pct_height'] for c in c_yes]), np.mean([c['gap_mean_pct_height'] for c in c_no]) + 1e-9)
            _, _, c_par = sp.pose_metrics(sec, ang, 'half', True, 'parent')
            self.assertLessEqual(np.mean([c['gap_mean_pct_height'] for c in c_yes]), np.mean([c['gap_mean_pct_height'] for c in c_par]) + 1e-9)

    def test_soft_piece_is_skinned_continuously(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim', seam=True, speck=False)
        level = sp.load_level(reduced, '0p4', json.loads((reduced / 'manifest.json').read_text()))
        spec = sp.load_spec('body_armor', 'rim', overrides={'rigid_method': 'seam', 'overlap': True, **JOINTS})
        sec = sp.build_sections(level, spec, 'seam', True)
        self.assertNotIn('soft', [c['owner'] for c in sp.pose_metrics(sec, 45, 'arm', True, 'both')[2] if c['owner'] != 'soft'])
        posed, flags, cuts = sp.pose_metrics(sec, 45, 'arm', False, 'parent')
        stretch = [c['soft_stretch_max'] for c in cuts if 'soft_stretch_max' in c]
        self.assertEqual(len(stretch), 1)
        self.assertGreater(stretch[0], 1.0)
        self.assertLess(stretch[0], 4.0)
        # no rigid-rigid cut moves relative to the other in the arm convention: every cut goes through the soft band
        self.assertEqual([c for c in cuts if c['owner'] != 'soft' and c['separation_max_pct_height'] > 1e-6 and 'arm' in c['under'] and 'torso' in c['owner']], [])


class BandsTest(unittest.TestCase):
    def run_bands(self, method='bands', soft='piece', render=False, pose=False, overlap=False, **kw):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim', bands=True, **kw)
        ov = {'rigid_method': method, 'soft_mode': soft, 'pose_test': True if pose else None, 'overlap': True if overlap else None, **JOINTS}
        res, out = run(root, reduced, style='rim', overrides=ov, render=render)
        return res, out, info

    def test_bands_cut_chest_pelvis_thigh_arms_and_soft_pieces(self):
        res, out, info = self.run_bands()
        lv = res['levels']['0p4']
        cells, _ = piece_cells3(out, info)
        b = lv['rigid']['bands']
        self.assertEqual(b['fell_back'], [])
        self.assertEqual(b['absent'], ['hip_r'])                    # no right leg in the synthetic
        self.assertEqual(cells['arm_l'], PLATES_L)
        self.assertEqual(cells['arm_r'], PLATES_R)
        self.assertEqual(cells['pelvis'], {(i, j) for j in (3, 4) for i in range(NU)})
        self.assertEqual(cells['thigh_l'], {(i, j) for j in (0, 1) for i in range(NU) if bands_left(i)})
        self.assertEqual(cells['soft_waist'], {(i, 5) for i in range(NU)})
        self.assertEqual(cells['soft_hip_l'], {(i, 2) for i in range(NU)})
        self.assertEqual(len(cells['soft_arm_l']), 18)
        self.assertEqual(sum(p['faces'] for p in lv['pieces'].values()), lv['wearable_faces'])
        self.assertEqual(lv['pieces']['chest']['components'], 1)
        self.assertEqual(lv['rigid']['method_used'], 'bands')

    def test_bands_nearest_mode_has_no_soft_pieces(self):
        res, out, info = self.run_bands(soft='nearest')
        lv = res['levels']['0p4']
        self.assertEqual([k for k in lv['pieces'] if k.startswith('soft')], [])
        self.assertEqual(sum(p['faces'] for p in lv['pieces'].values()), lv['wearable_faces'])
        cells, _ = piece_cells3(out, info)
        self.assertTrue(PLATES_L <= cells['arm_l'])
        self.assertTrue(cells['pelvis'] >= {(i, j) for j in (3, 4) for i in range(NU)})
        self.assertEqual(lv['pieces']['arm_l']['components'], 1)
        self.assertEqual(lv['pieces']['chest']['components'], 1)

    def test_missing_bands_fall_back_to_snap_per_joint(self):
        res, out, info = self.run_bands(no_hip_band=True, no_right_arm_band=True)
        lv = res['levels']['0p4']
        b = lv['rigid']['bands']
        self.assertEqual(sorted(b['fell_back']), ['arm_r', 'hip_l'])
        self.assertEqual(b['joints']['arm_l']['status'], 'band')
        self.assertEqual(b['joints']['waist']['status'], 'band')
        self.assertTrue(set(lv['rigid']['snap']) >= {'arm_r', 'hip_l'})
        self.assertGreater(lv['pieces']['arm_r']['faces'], 40)       # cut by the zone guess + snap, not lost
        self.assertGreater(lv['pieces']['thigh_l']['faces'], 60)
        self.assertEqual(sum(p['faces'] for p in lv['pieces'].values()), lv['wearable_faces'])

    def test_auto_picks_bands_with_cyan_and_snap_without(self):
        res, out, info = self.run_bands(method='auto')
        self.assertEqual(res['levels']['0p4']['rigid']['method_used'], 'bands')
        res2, out2, info2 = self.run_bands(method='auto', no_cyan=True)
        lv2 = res2['levels']['0p4']['rigid']
        self.assertEqual(lv2['method_used'], 'snap')
        self.assertEqual(lv2['auto']['joint_role_faces'], 0)
        names = set(res2['levels']['0p4']['pieces'])
        self.assertTrue({'chest', 'pelvis', 'thigh_l', 'arm_l'} <= names)      # snap on all joints
        self.assertGreater(res2['levels']['0p4']['pieces']['pelvis']['faces'], 0)

    def test_separable_palette_makes_auto_the_default(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim', bands=True)
        res, out = run(root, reduced, style='rim', overrides=JOINTS)
        lv = res['levels']['0p4']
        self.assertEqual(lv['rigid']['method_used'], 'bands')
        self.assertEqual(res['default_rigid_reason'], 'separable palette (joint role)')
        self.assertFalse(res['overlap'])                                        # overlap strips are opt-in (--overlap)
        self.assertFalse(lv['rigid']['overlap'])
        root2 = _setup(self)
        reduced2, truth2, info2 = make_reduced(root2, 'rim', bands=True)
        res2, _ = run(root2, reduced2, style='rim', overrides={'rigid_method': 'none', **JOINTS})
        self.assertNotIn('rigid', res2['levels']['0p4'])                       # rim split of the pauldrons only

    def test_pose_scenarios_for_legs_and_spine(self):
        res, out, info = self.run_bands(pose=True, overlap=True)
        v = res['levels']['0p4']['rigid']['pose']['variants']
        for sc in ('thigh_flex', 'thigh_abd', 'spine'):
            yes = v[f'half/both/{sc}/overlap']
            self.assertIn('soft_stretch_max', yes, sc)                    # every joint is a skinned band here
            self.assertGreater(yes['soft_stretch_max'], 1.0, sc)
            self.assertEqual(yes['gap_max_pct_height'], 0., sc)           # no rigid-rigid cut moves relative to another
        # a joint that fell back to snap is a rigid cut: it opens when the thigh swings and the strips narrow it
        res2, out2, info2 = self.run_bands(pose=True, overlap=True, no_hip_band=True)
        v2 = res2['levels']['0p4']['rigid']['pose']['variants']
        no, yes = v2['half/both/thigh_flex/no_overlap'], v2['half/both/thigh_flex/overlap']
        self.assertGreater(no['gap_max_pct_height'], 1.0)
        self.assertLess(yes['gap_mean_pct_height'], no['gap_mean_pct_height'])
        self.assertTrue(any(c['owner'] == 'pelvis' and c['under'] == 'thigh_l' for c in yes['cuts']))
        # strips exist for rigid-rigid cuts only; soft pieces get none, and with all joints banded there are none
        # between chest/pelvis/thigh/arm (only the pauldron cuts)
        ov = res['levels']['0p4']['rigid']['overlap']
        self.assertTrue(all('soft' not in o['owner'] and 'soft' not in o['under'] for o in ov))

    def test_leg_and_spine_rotations(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim', bands=True)
        level = sp.load_level(reduced, '0p4', json.loads((reduced / 'manifest.json').read_text()))
        spec = sp.load_spec('body_armor', 'rim', overrides={'rigid_method': 'bands', **JOINTS})
        sec = sp.build_sections(level, spec, 'bands', False)
        T = sp.pose_transforms(sec, 45, 'half', 'thigh_flex')
        lab = [l for l, n in sec.names.items() if n == 'thigh_l'][0]
        foot = np.array([[.14, .0, 0.]])                                   # below the hip: swings forward (+Z)
        moved = sp.apply_T(foot, T[lab])[0]
        self.assertGreater(moved[2], .2)
        T2 = sp.pose_transforms(sec, 20, 'half', 'thigh_abd')
        self.assertGreater(sp.apply_T(foot, T2[lab])[0][0], foot[0][0])      # abduction: the left thigh moves out (+X)
        T3 = sp.pose_transforms(sec, 20, 'half', 'spine')
        top = np.array([[0., 1., 0.]])
        self.assertGreater(sp.apply_T(top, T3[0])[0][2], .1)                 # chest pitches forward
        self.assertNotIn([l for l, n in sec.names.items() if n == 'pelvis'][0], T3)


class RegionsTest(unittest.TestCase):
    def run_regions(self, method='regions', **kw):
        root = _setup(self)
        ov_kw = {k: kw.pop(k) for k in ('expect',) if k in kw}
        reduced, truth, info = make_reduced(root, 'rim', regions=True, **kw)
        res, out = run(root, reduced, style=None, overrides={'rigid_method': method, 'overlap': True, **ov_kw, **JOINTS})
        return res, out, info

    def test_sections_follow_the_colour_regions(self):
        res, out, info = self.run_regions()
        lv = res['levels']['0p4']
        cells, _ = piece_cells3(out, info)
        g = lv['rigid']['regions']
        self.assertEqual(lv['rigid']['pauldron_spec']['style'], 'rim')           # regions mode: rim pauldrons by default
        self.assertEqual(g['fell_back'], [])
        self.assertEqual(g['absent'], ['hip_r'])                                  # only a left leg in the synthetic
        self.assertEqual(cells['pelvis'], {(i, j) for j in (2, 3, 4) for i in range(NU)})
        self.assertEqual(cells['thigh_l'], {(i, j) for j in (0, 1) for i in range(NU) if bands_left(i)})
        arm_l = {(i, j) for j in (7, 8, 9) for i in range(NU) if cell_parts_b(i, j) == 'arm_stretch.l'}
        arm_r = {(i, j) for j in (7, 8, 9) for i in range(NU) if cell_parts_b(i, j) == 'arm_stretch.r'}
        self.assertEqual(cells['arm_l'], arm_l)
        self.assertEqual(cells['arm_r'], arm_r)
        self.assertTrue(all(g['joints'][k]['status'] == 'region' for k in ('arm_l', 'arm_r', 'waist', 'hip_l')))
        self.assertTrue(all(r['how'].startswith('whole') for r in g['regions'] if r['kind'] == 'region'))
        self.assertEqual(sum(p['faces'] for p in lv['pieces'].values()), lv['wearable_faces'])
        self.assertEqual(lv['rigid']['method_used'], 'regions')

    def test_enclosed_decoration_is_absorbed(self):
        res, out, info = self.run_regions()
        cells, _ = piece_cells3(out, info)
        g = res['levels']['0p4']['rigid']['regions']
        self.assertTrue({(i, j) for i in (11, 12, 13) for j in (11, 12, 13)} <= cells['chest'])
        self.assertTrue({(0, 8), (1, 8), (0, 9), (1, 9)} <= cells['arm_l'])
        enclosed = [c for c in g['other_components'] if c['how'] == 'enclosed']
        self.assertEqual(sorted(c['section'] for c in enclosed if c['roles'] == ['tertiary']), ['arm_l', 'chest'])

    def test_decoration_across_a_joint_is_split_and_reported(self):
        res, out, info = self.run_regions(straddle=True)
        g = res['levels']['0p4']['rigid']['regions']
        two = g['decoration_touching_two_sections']
        self.assertEqual(len(two), 1)
        self.assertEqual(two[0]['sections'], ['arm_l', 'chest'])

    def test_missing_pelvis_colour_splits_the_joined_region_with_snap(self):
        res, out, info = self.run_regions(no_pelvis_colour=True)
        lv = res['levels']['0p4']
        g = lv['rigid']['regions']
        self.assertEqual(sorted(g['fell_back']), ['hip_l', 'waist'])
        self.assertEqual(g['joints']['arm_l']['status'], 'region')
        spanning = [r for r in g['regions'] if r['how'].startswith('spanning')]
        self.assertEqual(len(spanning), 1)
        self.assertEqual(sorted(spanning[0]['assigned']), ['chest', 'pelvis', 'thigh_l'])
        self.assertTrue({'waist', 'hip_l'} <= set(lv['rigid']['snap']))
        cells, _ = piece_cells3(out, info)
        self.assertEqual(len(cells['pelvis']), 144)                      # the snap re-cut keeps the colourless boundary on the zone cut
        self.assertEqual(len(cells['chest']), 396)
        self.assertEqual(lv['pieces']['pelvis']['components'], 1)

    def test_expect_override_changes_which_colour_is_a_section(self):
        res, out, info = self.run_regions(arm_role='quinary', expect={'arm': 'quinary'})
        lv = res['levels']['0p4']
        cells, _ = piece_cells3(out, info)
        self.assertTrue(all(lv['rigid']['regions']['joints'][k]['status'] == 'region' for k in ('arm_l', 'arm_r')))
        self.assertEqual(len(cells['arm_l']), 3 * 10)
        res2, out2, info2 = self.run_regions(arm_role='quinary')                  # default table expects blue arms: not a section colour
        self.assertEqual(res2['levels']['0p4']['rigid']['regions']['joints']['arm_l']['status'], 'absent')

    def test_auto_chooses_regions_then_snap(self):
        res, out, info = self.run_regions(method='auto')
        lv = res['levels']['0p4']['rigid']
        self.assertEqual(lv['method_used'], 'regions')
        self.assertGreaterEqual(lv['auto']['large_regions'], 3)
        self.assertEqual(lv['auto']['chosen'], 'regions')
        root = _setup(self)                                                        # one colour everywhere: no regions
        reduced, truth, info2 = make_reduced(root, 'rim', bands=True, no_cyan=True)
        res2, out2 = run(root, reduced, overrides={'rigid_method': 'auto', **JOINTS})
        self.assertEqual(res2['levels']['0p4']['rigid']['method_used'], 'snap')
        self.assertLessEqual(res2['levels']['0p4']['rigid']['auto']['large_regions'], 1)

    def test_auto_keeps_cyan_designs_on_their_methods(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim', bands=True)
        res, out = run(root, reduced, overrides={'rigid_method': 'auto', **JOINTS})
        self.assertEqual(res['levels']['0p4']['rigid']['method_used'], 'bands')

    def test_adaptive_manifest_makes_auto_the_default(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim', regions=True)
        mpath = reduced / 'manifest.json'
        m = json.loads(mpath.read_text())
        m['hue_fit'] = {'active': True}
        mpath.write_text(json.dumps(m))
        res, out = run(root, reduced, overrides=JOINTS)
        self.assertEqual(res['levels']['0p4']['rigid']['method_used'], 'regions')
        self.assertEqual(res['default_rigid_reason'], 'separable reduction (adaptive hue fit on)')
        self.assertFalse(res['overlap'])
        root2 = _setup(self)
        reduced2, truth2, info2 = make_reduced(root2, 'rim', regions=True)
        m2 = json.loads((reduced2 / 'manifest.json').read_text())
        m2['hue_fit'] = {'active': True}
        (reduced2 / 'manifest.json').write_text(json.dumps(m2))
        res2, _ = run(root2, reduced2, overrides={'overlap': True, **JOINTS})                # --overlap still works
        self.assertTrue(res2['overlap'])
        self.assertTrue(res2['levels']['0p4']['rigid']['overlap'])

    def test_pose_test_runs_for_regions(self):
        root = _setup(self)
        reduced, truth, info = make_reduced(root, 'rim', regions=True)
        res, out = run(root, reduced, overrides={'rigid_method': 'regions', 'overlap': True, 'pose_test': True, **JOINTS})
        pose = res['levels']['0p4']['rigid']['pose']
        self.assertIn('thigh_flex', json.dumps(pose))


def ring_mesh(rings, labels, n=24):
    """Tube of n facets through (y, radius) rings, top to bottom; labels[k] = section label of the band between ring k and k+1."""
    v = [(r * np.cos(2 * np.pi * i / n), y, r * np.sin(2 * np.pi * i / n)) for y, r in rings for i in range(n)]
    f, lab = [], []
    for k in range(len(rings) - 1):
        for i in range(n):
            a, b = k * n + i, k * n + (i + 1) % n
            c, d = (k + 1) * n + (i + 1) % n, (k + 1) * n + i
            f += [(a, d, c), (a, c, b)]
            lab += [labels[k]] * 2
    return np.array(v), np.array(f), np.array(lab)


class CrossJointTest(unittest.TestCase):
    """reassign_cross_joint on a chest tube above the waist pivot (y = 0) with a pelvis tube below."""

    @staticmethod
    def run_case(rings, labels, enabled=True, mode='plane', **cfg):
        v, f, lab = ring_mesh(rings, labels)
        level = sp.Level('t', v, f, np.zeros(len(f), np.int64), [{'name': 'primary', 'color': [0, 1, 0, 1]}], 1.0)
        level.joints = {'waist': np.zeros(3)}
        spec = sp.load_spec('body_armor', None, overrides={'rigid_method': 'regions', 'cross_joint': enabled})
        spec['rigid']['cross_joint'].update(cfg, mode=mode)
        names = {0: 'chest', 5: 'pelvis'}
        out, rep = sp.reassign_cross_joint(level, lab, spec, names, sp.resolve_joints(level, {'joint_l': None}) or level.joints)
        return lab, out, rep

    # chest wall down to -0.04, a flared skirt (flange) from -0.04 to -0.10 and its underside, labelled chest; pelvis below
    FLARE = ([(.8, .2), (-.04, .2), (-.10, .28), (-.10, .2), (-.4, .2)], [0, 0, 0, 5])

    def test_flared_skirt_beyond_the_waist_goes_to_the_pelvis(self):
        rings, labels = self.FLARE
        lab, out, rep = self.run_case(rings, labels)
        moved = (out != lab)
        self.assertGreater(moved.sum(), 0)
        self.assertTrue((out[moved] == 5).all())
        self.assertEqual(lab[moved].min(), 0)
        rec = rep['reassigned'][0]
        self.assertEqual((rec['joint'], rec['from'], rec['to']), ('waist', 'chest', 'pelvis'))
        self.assertGreater(rec['mean_distance_past_plane_pct_height'], 2.5)
        self.assertGreater(rec['normal_to_axis_deg'], 10)
        self.assertGreater(rec['contact_share_with_target'], .1)
        self.assertEqual(rep['faces_moved'], int(moved.sum()))
        wall = np.zeros(len(lab), bool)
        wall[:48] = True                                       # the chest wall above the flange stays
        self.assertFalse(moved[wall].any())

    def test_overhang_just_past_the_plane_stays(self):
        rings = [(.8, .2), (-.004, .2), (-.02, .28), (-.02, .2), (-.4, .2)]
        lab, out, rep = self.run_case(rings, [0, 0, 0, 5])
        self.assertTrue((out == lab).all())                    # everything within 2% of the plane, under the 2.5% margin: a breastplate edge over the fauld
        self.assertEqual(rep['reassigned'], [])

    def test_smooth_continuation_is_not_a_fringe(self):
        rings = [(.8, .2), (-.04, .2), (-.12, .2), (-.4, .2)]
        lab, out, rep = self.run_case(rings, [0, 0, 5])        # chest tube simply continues below the plane: one planar patch
        self.assertTrue((out == lab).all())

    def test_switch_off_and_report(self):
        rings, _ = self.FLARE
        lab, out, rep = self.run_case(rings, [0, 0, 0, 5], enabled=False)
        self.assertTrue((out == lab).all())
        self.assertFalse(rep['enabled'])
        lab, out, rep = self.run_case(rings, [0, 0, 0, 5], margin=.2)       # beyond the margin: rejected, but reported
        self.assertTrue((out == lab).all())
        self.assertTrue(any('rejected_because' in r for r in rep['rejected']))

    def test_pelvis_strip_above_the_waist_goes_to_the_chest(self):
        rings = [(.8, .2), (.15, .2), (.06, .28), (.06, .2), (-.9, .2)]
        lab, out, rep = self.run_case(rings, [0, 5, 5, 5])      # a sash-like flange of pelvis-labelled faces up on the chest
        moved = out != lab
        self.assertTrue(moved.any())
        self.assertTrue((out[moved] == 0).all())
        self.assertEqual(rep['reassigned'][0]['from'], 'pelvis')


class ShelfTest(unittest.TestCase):
    """The shelf / cap rule (mode shelf, the default): an up-facing ledge at the bottom of the chest goes to the pelvis."""

    # chest wall down to y = -0.04 (just below the waist pivot at 0), a flat ledge flange out to r = .25 (up-facing, labelled
    # chest), the flange's outer rim and underside and the pelvis tube labelled pelvis
    LEDGE = ([(.8, .2), (-.04, .2), (-.04, .25), (-.10, .25), (-.10, .2), (-.4, .2)], [0, 0, 5, 5, 5])
    run_case = staticmethod(CrossJointTest.run_case)

    def case(self, rings, labels, **cfg):
        return self.run_case(rings, labels, mode='shelf', **cfg)

    def test_default_mode_is_shelf(self):
        spec = sp.load_spec('body_armor', None, overrides={'rigid_method': 'regions'})
        cfg = sp.cross_joint_settings(spec)
        self.assertEqual(cfg['mode'], 'shelf')
        self.assertEqual(cfg['shelf']['joints'], ['waist'])
        spec['rigid']['cross_joint'] = {'shelf': {'band': .05}}            # nested block merges over the defaults
        self.assertEqual(sp.cross_joint_settings(spec)['shelf']['axis_deg'], 35.)
        self.assertEqual(sp.cross_joint_settings(spec)['shelf']['band'], .05)

    def test_up_facing_ledge_goes_to_the_pelvis(self):
        rings, labels = self.LEDGE
        lab, out, rep = self.case(rings, labels)
        moved = out != lab
        ledge = np.zeros(len(lab), bool)
        ledge[48:96] = True                                    # ring 1 -> 2: 24 quads = 48 triangles, y = -0.04
        self.assertTrue((moved == ledge).all())                # exactly the ledge, not the chest wall above it
        self.assertTrue((out[moved] == 5).all())
        rec = rep['reassigned'][0]
        self.assertEqual((rec['rule'], rec['joint'], rec['from'], rec['to']), ('shelf', 'waist', 'chest', 'pelvis'))
        self.assertLess(rec['normal_to_axis_deg'], 5)
        self.assertGreater(rec['neck_mean_dihedral_deg'], 80)      # the fold at the ledge's inner edge
        self.assertAlmostEqual(rec['contact_share_with_target'], .25 / .45, delta=.02)      # outer rim length over the whole outline
        self.assertEqual(rep['faces_moved'], 48)

    def test_vertical_band_below_the_pivot_stays(self):
        rings = [(.8, .2), (-.2, .2), (-.4, .2)]               # an abdomen band: chest-labelled wall 20% below the pivot
        lab, out, rep = self.case(rings, [0, 5])
        self.assertTrue((out == lab).all())
        rings = [(.8, .2), (-.04, .2), (-.10, .28), (-.10, .2), (-.4, .2)]
        lab, out, rep = self.case(rings, [0, 0, 0, 5])         # the flared skirt the plane rule moves: not a shelf (normals 37 deg off up)
        self.assertTrue((out == lab).all())
        lab2, out2, _ = self.run_case(rings, [0, 0, 0, 5], mode='plane')
        self.assertTrue((out2 != lab2).any())                  # ... while mode plane (the old rule) still does
        lab3, out3, _ = self.run_case(rings, [0, 0, 0, 5], mode='both')
        self.assertTrue((out3 != lab3).any())

    def test_down_facing_lip_on_the_pelvis_goes_to_the_chest(self):
        # chest wall and an outward flange (top, rim) down to y = .04, then the flange's UNDERSIDE (faces down, into the pelvis'
        # side of the axis) labelled pelvis together with the pelvis tube: the underside is a cap of the chest
        rings = [(.8, .2), (.1, .2), (.1, .25), (.04, .25), (.04, .2), (-.9, .2)]
        lab, out, rep = self.case(rings, [0, 0, 0, 5, 5])
        moved = out != lab
        underside = np.zeros(len(lab), bool)
        underside[144:192] = True                              # ring 3 -> 4
        self.assertTrue((moved == underside).all())
        self.assertTrue((out[moved] == 0).all())
        self.assertEqual((rep['reassigned'][0]['from'], rep['reassigned'][0]['to']), ('pelvis', 'chest'))

    def test_guards_and_switches(self):
        rings, labels = self.LEDGE
        lab, out, rep = self.case(rings, labels, enabled=False)
        self.assertTrue((out == lab).all())
        lab, out, rep = self.case(rings, labels, shelf={'max_share': .01})
        self.assertTrue((out == lab).all())
        self.assertIn('area', rep['rejected'][0]['rejected_because'])
        lab, out, rep = self.case(rings, labels, shelf={'min_contact': .9})
        self.assertTrue((out == lab).all())
        self.assertIn('outline', rep['rejected'][0]['rejected_because'])
        lab, out, rep = self.case(rings, labels, shelf={'axis_deg': 0.5, 'joints': ['hip_l']})      # not a listed joint
        self.assertTrue((out == lab).all())

    def test_gently_sloped_neighbour_is_not_a_shelf(self):
        # the same ledge, but its inner edge meets a wall that slopes only 10 degrees: no fold, so it is part of the body
        rings = [(.8, .2), (-.04, .2), (-.04 - .02, .2 + .0), (-.4, .2)]
        lab, out, rep = self.case(rings, [0, 0, 5])
        self.assertTrue((out == lab).all())


class FlapTest(unittest.TestCase):
    """prune_flaps: a triangle hanging off the wearable by one edge and standing far out of it is dropped from the pieces."""

    @staticmethod
    def tube_with(tips):
        """An open 24-facet tube (y 0..-.4, radius .2) with one triangle per entry of `tips` = (column, height) stuck to its
        top rim edge and pointing up by `height`."""
        v, f, lab = ring_mesh([(0., .2), (-.4, .2)], [0])
        v, f = [tuple(x) for x in v], [tuple(x) for x in f]
        for col, h in tips:
            a, b = col, (col + 1) % 24
            mid = (np.array(v[a]) + np.array(v[b])) / 2
            v.append(tuple(mid + np.array([0, h, 0])))
            f.append((a, len(v) - 1, b))
        level = sp.Level('t', np.array(v), np.array(f), np.zeros(len(f), np.int64), [{'name': 'primary', 'color': [0, 1, 0, 1]}], 1.0)
        return level, np.zeros(level.n, np.int64)

    def spec(self, **kw):
        spec = sp.load_spec('body_armor', None, overrides={'rigid_method': 'regions', **kw})
        return spec

    def test_long_spike_is_dropped_and_short_one_stays(self):
        level, label = self.tube_with([(2, .12), (10, .02)])
        dropped, recs = sp.prune_flaps(level, label, self.spec(), {0: 'arm_l'})
        self.assertEqual(int(dropped.sum()), 1)
        self.assertTrue(dropped[48])                           # the first added face (the 12% spike)
        self.assertFalse(dropped[49])                          # a 2% tab is just an uneven rim
        self.assertEqual((recs[0]['piece'], recs[0]['attached_edges']), ('arm_l', 1))
        self.assertAlmostEqual(recs[0]['tip_height_pct_height'], 12, delta=.5)
        self.assertTrue((label == 0).all())                    # labels untouched: a flap has no second piece to go to

    def test_chain_is_peeled_leaf_by_leaf(self):
        level, label = self.tube_with([(2, .12)])
        v, f = level.raw_v.tolist(), level.raw_f.tolist()
        tip = len(v) - 1
        radial = np.array([v[tip][0], 0, v[tip][2]])
        v.append(tuple(np.array(v[tip]) + radial / np.linalg.norm(radial) * .1))      # a second spike triangle on the first one's free edge
        f.append((f[-1][0], tip, len(v) - 1))
        level = sp.Level('t', np.array(v), np.array(f), np.zeros(len(f), np.int64), [{'name': 'primary', 'color': [0, 1, 0, 1]}], 1.0)
        dropped, recs = sp.prune_flaps(level, np.zeros(level.n, np.int64), self.spec(), {0: 'arm_l'})
        self.assertGreaterEqual(int(dropped.sum()), 2)

    def test_switches(self):
        level, label = self.tube_with([(2, .12)])
        self.assertEqual(int(sp.prune_flaps(level, label, self.spec(flaps=False), {0: 'x'})[0].sum()), 0)
        spec = self.spec()
        spec['rigid']['flaps'] = {'min_height': .2}
        self.assertEqual(int(sp.prune_flaps(level, label, spec, {0: 'x'})[0].sum()), 0)

    def test_closed_tube_has_no_flaps(self):
        level, label = self.tube_with([])
        self.assertEqual(int(sp.prune_flaps(level, label, self.spec(), {0: 'x'})[0].sum()), 0)


class BandContinuityTest(unittest.TestCase):
    @staticmethod
    def ring_level(arcs, n=24, height=2.0):
        """A waist-height tube (radius .2) of 24 quads around Y; `arcs` = columns that are joint-role (cyan)."""
        v, f, mat = [], [], []
        for i in range(n + 1):
            a = 2 * np.pi * (i % n) / n
            v += [(.2 * np.cos(a), -.03, .2 * np.sin(a)), (.2 * np.cos(a), .03, .2 * np.sin(a))]
        for i in range(n):
            lo0, hi0, lo1, hi1 = 2 * i, 2 * i + 1, 2 * i + 2, 2 * i + 3
            for tri in ((lo0, lo1, hi1), (lo0, hi1, hi0)):
                f.append(tri)
                mat.append(1 if i in arcs else 0)
        materials = [{'name': 'primary', 'color': [0, 1, 0, 1]}, {'name': 'joint', 'color': [0, 1, 1, 1]}]
        level = sp.Level('0p4', np.array(v), np.array(f), np.array(mat), materials, height)
        level.joints = {'waist': np.zeros(3)}
        return level

    def test_full_ring_is_covered_and_one_piece(self):
        c = sp.band_continuity(self.ring_level(set(range(24))))['waist']
        self.assertAlmostEqual(c['coverage'], 1.0, places=2)
        self.assertEqual(c['components'], 1)
        self.assertLessEqual(c['largest_gap_deg'], 2.0)
        self.assertAlmostEqual(c['axial_width_pct_height'], 3.0, delta=.2)

    def test_partial_ring_reports_coverage_and_gap(self):
        c = sp.band_continuity(self.ring_level(set(range(0, 12))))['waist']        # half the circumference
        self.assertAlmostEqual(c['coverage'], .5, delta=.05)
        self.assertAlmostEqual(c['largest_gap_deg'], 180, delta=10)
        c2 = sp.band_continuity(self.ring_level({0, 1, 2, 12, 13, 14}))['waist']
        self.assertEqual(c2['components'], 2)
        self.assertAlmostEqual(c2['coverage'], .25, delta=.03)

    def test_no_joint_faces_or_no_joint_points(self):
        self.assertEqual(sp.band_continuity(self.ring_level(set()))['waist']['coverage'], 0.)
        level = self.ring_level({0})
        level.joints = None
        self.assertFalse(sp.band_continuity(level)['available'])


class GeometryHelpersTest(unittest.TestCase):
    def test_align_by_area_skips_dropped_faces(self):
        ref = np.array([1., 2., 3., 4., 5., 6., 7., 8.])
        cur = np.array([1., 2., 4., 5., 7., 8.]) * (1 + 1e-5)      # faces 3 and 6 were dropped by the exporter
        self.assertEqual(sp.align_by_area(ref, cur).tolist(), [0, 1, 3, 4, 6, 7])
        self.assertEqual(sp.align_by_area(ref, np.array([1., 2., 99., 3., 4.])).tolist(), [0, 1, -1, 2, 3])

    def test_closest_points_matches_dense_sampling(self):
        rng = np.random.default_rng(3)
        tri = rng.normal(size=(6, 3, 3))
        P = rng.normal(size=(40, 3)) * 1.5
        pts, idx, dist = sp.closest_points(P, tri)
        u, v = np.meshgrid(np.linspace(0, 1, 121), np.linspace(0, 1, 121))
        keep = u + v <= 1
        w = np.stack([1 - u[keep] - v[keep], u[keep], v[keep]], 1)          # (s,3) barycentric samples
        samples = np.einsum('sk,tkd->tsd', w, tri).reshape(-1, 3)
        brute = np.linalg.norm(P[:, None] - samples[None], axis=2).min(1)
        self.assertTrue(np.all(dist <= brute + 1e-9))
        self.assertTrue(np.all(brute - dist < 0.03))

    def test_min_cut_chain(self):
        # s - a - b - c - t with the cheapest link between b and c
        edges = [(0, 1, 5, 5), (1, 2, 4, 4), (2, 3, 1, 1), (3, 4, 6, 6)]
        side = sp.min_cut(5, edges, 0, 4)
        self.assertEqual(side.tolist(), [True, True, True, False, False])

    def test_real_base_joints_are_inside_the_armour(self):
        reduced = ROOT / 'experiments/pauldron_pipeline_20261022/S4_lion_crest/reduced'
        if not (reduced / 'manifest.json').is_file():
            self.skipTest('pauldron pipeline data not present')
        man = json.loads((reduced / 'manifest.json').read_text())
        j = sp.find_joints(man, reduced)
        g = sp.read_glb(reduced / 'lod_error_0p4__wearable.glb')
        lo, hi = g['v'].min(0), g['v'].max(0)
        for side, sgn in (('l', 1), ('r', -1)):
            sh, el = j[side]['shoulder'], j[side]['elbow']
            self.assertTrue(np.all(sh >= lo - .02) and np.all(sh <= hi + .02), (side, sh, lo, hi))
            self.assertGreater(sgn * sh[0], 0.05)
            self.assertGreater(sgn * el[0], sgn * sh[0] * 1.5)       # the elbow is further out than the shoulder


def piece_cells3(result_out, info, level='0p4'):
    z = np.load(Path(result_out) / f'lod_{level}' / 'faces.npz', allow_pickle=True)
    names = [str(n) for n in z['piece_names']]
    ids = [int(i) for i in z['piece_ids']] if 'piece_ids' in z.files else list(range(len(names)))
    cells = info['wear_cells']
    return {n: {cells[i] for i in np.flatnonzero(z['piece'] == k)} for k, n in zip(ids, names)}, z['piece']


def dump(dest):
    """Write the synthetic reduced folders (rim, rim with gap, roles) plus their splits under `dest` (scratch use)."""
    dest = Path(dest)
    for name, style, kw in (('synthetic_roles', 'roles', {'island': True, 'hole': True}), ('synthetic_rim', 'rim', {}),
                            ('synthetic_rim_gap', 'rim', {'gap': True})):
        root = dest / name
        reduced, truth, info = make_reduced(root, style, **kw)
        sp.run(reduced, style=style, output_dir=root / 'pieces')
        sp.run(reduced, style=style, output_dir=root / 'pieces_rigid_snap',
               overrides={'rigid_method': 'snap', 'overlap': True, 'pose_test': True, **JOINTS})


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--dump':
        dump(sys.argv[2])
    else:
        unittest.main()
