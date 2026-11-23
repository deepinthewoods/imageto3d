"""Tests of wearcut.py / wearcut_rules.py on synthetic meshes (plain python: numpy + scipy; the Blender side has its own smoke test, see README).

    characters/.venv/bin/python -m unittest tools/tripo_pipeline/test_wearcut.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import face_labels as fl  # noqa: E402
import segment_first as sf  # noqa: E402
import symmetry as sym  # noqa: E402
import wearcut as wc  # noqa: E402
import wearcut_rules as R  # noqa: E402
from palette import Palette  # noqa: E402


def grid(nx, ny, x0=0., x1=1., y0=0., y1=1.):
    xs, ys = np.linspace(x0, x1, nx + 1), np.linspace(y0, y1, ny + 1)
    v = np.array([(x, y, 0.) for y in ys for x in xs])
    idx = lambda i, j: j * (nx + 1) + i
    f = []
    for j in range(ny):
        for i in range(nx):
            a, b, c, d = idx(i, j), idx(i + 1, j), idx(i + 1, j + 1), idx(i, j + 1)
            f += [(a, b, c), (a, c, d)]
    return v, np.array(f, np.int64)


def tube(nu=48, nz=72, radius=.4, height=2.):
    """Open cylinder (normals outward), z in [0, height]."""
    th = np.linspace(0, 2 * np.pi, nu, endpoint=False)
    zs = np.linspace(0, height, nz + 1)
    v = np.array([(radius * np.cos(t), radius * np.sin(t), z) for z in zs for t in th])
    f = []
    for j in range(nz):
        for i in range(nu):
            a, b = j * nu + i, j * nu + (i + 1) % nu
            c, d = (j + 1) * nu + (i + 1) % nu, (j + 1) * nu + i
            f += [(a, b, c), (a, c, d)]
    return v, np.array(f, np.int64)


class Rules(unittest.TestCase):
    NAMES = ['head.x', 'spine_01.x', 'hand.l', 'hand.r', 'forearm_stretch.r', 'forearm_stretch.l', 'leg_stretch.r', 'leg_stretch.l',
             'foot.r', 'foot.l', 'thigh_stretch.r', 'thigh_stretch.l', 'arm_stretch.r']

    def test_garment_may_be_where_the_module_prior_allows(self):
        n = {k: i for i, k in enumerate(self.NAMES)}
        a = R.part_tables(self.NAMES, 'body_armor')['allowed']
        self.assertTrue(a[n['spine_01.x']] and a[n['thigh_stretch.l']] and a[n['arm_stretch.r']])
        self.assertFalse(a[n['head.x']] or a[n['hand.r']] or a[n['forearm_stretch.l']] or a[n['foot.l']] or a[n['leg_stretch.r']])
        g = R.part_tables(self.NAMES, 'right_gauntlet_boot')['allowed']
        self.assertTrue(g[n['hand.r']] and g[n['foot.r']] and g[n['thigh_stretch.r']])
        self.assertFalse(g[n['hand.l']] or g[n['spine_01.x']] or g[n['thigh_stretch.l']])
        o = R.part_tables(self.NAMES, 'outfit')['allowed']
        self.assertTrue(o[n['foot.r']] and o[n['spine_01.x']] and o[n['leg_stretch.l']])
        self.assertFalse(o[n['foot.l']] or o[n['hand.l']] or o[n['head.x']])

    def test_expected_asymmetric_and_mirror_ids(self):
        t = R.part_tables(self.NAMES, 'outfit')
        self.assertTrue(t['expected'][self.NAMES.index('foot.r')] and not t['expected'][self.NAMES.index('leg_stretch.l')])
        m = R.mirror_part_ids(self.NAMES)
        self.assertEqual(self.NAMES[m[self.NAMES.index('hand.r')]], 'hand.l')
        self.assertEqual(self.NAMES[m[self.NAMES.index('hand.l')]], 'hand.r')
        self.assertEqual(m[self.NAMES.index('spine_01.x')], self.NAMES.index('spine_01.x'))
        self.assertEqual(m[self.NAMES.index('arm_stretch.r')], self.NAMES.index('arm_stretch.r'))   # no left counterpart in the list


class KneeCops(unittest.TestCase):
    """Plate armour with knee cops: the knee band of each leg becomes the part knee_cop.l/.r (face_labels.knee_cop_parts / BaseParts.load)."""
    NAMES = ['foot.l', 'foot.r', 'leg_stretch.l', 'leg_stretch.r', 'thigh_stretch.l', 'thigh_stretch.r', 'root.x']
    KNEE, ANKLE = .6, .2

    def legs(self):
        """Two vertical tubes (x = +-.3), foot z .0-.2, lower leg .2-.6, thigh .6-1.0; rings at the part borders."""
        vs, fs, ps = [], [], []
        for side, x in (('l', .3), ('r', -.3)):
            v, f = tube(nu=12, nz=10, radius=.1, height=1.)
            v = v + np.array([x, 0., 0.])
            z = v[f].mean(1)[:, 2]
            name = np.where(z < .2, f'foot.{side}', np.where(z < .6, f'leg_stretch.{side}', f'thigh_stretch.{side}'))
            ps += [self.NAMES.index(n) for n in name]
            fs.append(f + sum(len(a) for a in vs))
            vs.append(v)
        return np.concatenate(vs), np.concatenate(fs), np.array(ps)

    def test_the_knee_band_below_and_above_the_knee_ring_becomes_knee_cop(self):
        v, f, part = self.legs()
        new, names, info = fl.knee_cop_parts(v, f, part, self.NAMES, .33, .15)
        z = v[f].mean(1)[:, 2]
        low, high = self.KNEE - .33 * (self.KNEE - self.ANKLE), self.KNEE + .15 * (self.KNEE - self.ANKLE)
        self.assertEqual(names[:len(self.NAMES)], self.NAMES)
        self.assertEqual(names[len(self.NAMES):], ['knee_cop.l', 'knee_cop.r'])                  # appended: existing part ids unchanged
        for side in 'lr':
            leg, thigh = self.NAMES.index(f'leg_stretch.{side}'), self.NAMES.index(f'thigh_stretch.{side}')
            cop = names.index(f'knee_cop.{side}')
            from_leg, from_thigh = (part == leg) & (new == cop), (part == thigh) & (new == cop)
            self.assertTrue(from_leg.any() and from_thigh.any())
            self.assertTrue(np.all(z[from_leg] > low) and np.all(z[from_thigh] < high))              # only the band
            self.assertTrue(np.all(new[(part == leg) & (z <= low)] == leg))                          # the rest of the shin stays leg_stretch
            self.assertTrue(np.all(new[(part == thigh) & (z >= high)] == thigh))                     # the rest of the thigh stays thigh
            self.assertEqual(info['sides'][side]['faces_moved'], int(from_leg.sum() + from_thigh.sum()))
            self.assertEqual(info['sides'][side]['status'], 'applied')
        keep = ~np.isin(part, [2, 3, 4, 5])
        self.assertTrue(np.array_equal(new[keep], part[keep]))                                       # feet, root untouched
        self.assertEqual(int((new == len(self.NAMES)).sum()), int((new == len(self.NAMES) + 1).sum()))  # both legs alike
        self.assertAlmostEqual(info['band_fraction'], .33)
        self.assertAlmostEqual(info['above_fraction'], .15)

    def test_band_fractions_default_from_config_can_be_overridden_and_replayed_from_a_manifest(self):
        cfg = json.loads((HERE / 'config.json').read_text())['knee_cops']
        self.assertEqual(fl.knee_cop_band(), cfg['band_fraction'])
        self.assertEqual(fl.knee_cop_above(True), cfg['above_fraction'])
        self.assertEqual(fl.knee_cop_band(.5), .5)
        self.assertEqual(fl.knee_cop_band({'band_fraction': .4}), .4)
        v, f, part = self.legs()
        small = int((fl.knee_cop_parts(v, f, part, self.NAMES, .2)[0] != part).sum())
        large = int((fl.knee_cop_parts(v, f, part, self.NAMES, .8)[0] != part).sum())
        self.assertLess(small, large)
        record = fl.knee_cop_parts(v, f, part, self.NAMES, .5, .3)[2]
        replay = fl.knee_cop_parts(v, f, part, self.NAMES, record)[0]                               # a manifest record replays its fractions
        self.assertTrue(np.array_equal(replay, fl.knee_cop_parts(v, f, part, self.NAMES, .5, .3)[0]))

    def test_a_leg_without_knee_or_ankle_ring_is_left_alone(self):
        v, f, part = self.legs()
        no_foot = np.where(np.isin(part, [0, 1]), part + 2, part)                 # feet merged into the lower leg: no ankle ring
        new, names, info = fl.knee_cop_parts(v, f, no_foot, self.NAMES, .33)
        self.assertTrue(np.array_equal(new, no_foot))
        self.assertEqual(names, self.NAMES)
        self.assertTrue(all(i['status'] == 'unchanged' for i in info['sides'].values()))
        new, names, info = fl.knee_cop_parts(v, f, part, ['head.x', 'root.x'], .33)       # parts missing altogether
        self.assertTrue(np.array_equal(new, part))

    def base_file(self, tmp):
        v, f, part = self.legs()
        path = Path(tmp) / 'parts.npz'
        np.savez_compressed(path, v=v.astype(np.float32), f=f.astype(np.int32), part=part.astype(np.int16), part_names=np.array(self.NAMES),
                            source_sha256='0' * 64)
        return path

    def test_base_parts_load_flag_off_is_the_file_and_flag_on_relabels_samples_too(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:   # ignore_cleanup_errors: Windows, tests keep np.load handles open
            path = self.base_file(tmp)
            off = fl.BaseParts.load(path)
            on = fl.BaseParts.load(path, knee_cops=True)
            raw = np.load(path)['part']
            self.assertTrue(np.array_equal(off.part, raw))
            self.assertEqual(off.names, self.NAMES)
            self.assertIsNone(off.knee_cops)
            self.assertFalse(np.array_equal(on.part, raw))
            self.assertEqual(on.names[-2:], ['knee_cop.l', 'knee_cop.r'])
            self.assertTrue(np.array_equal(on.v, off.v) and np.array_equal(on.samples[0], off.samples[0]))   # same geometry and samples
            self.assertEqual(on.knee_cops['sides']['l']['status'], 'applied')
            band = (off.samples[1] == 2) & (on.samples[1] == len(self.NAMES))                           # leg_stretch.l samples now knee_cop.l
            self.assertTrue(band.any())
            self.assertTrue(np.all(off.samples[0][band][:, 2] > .45))
            moved = on.moved(1.1, np.array([0., 0., .01]))
            self.assertIsNotNone(moved.knee_cops)                                                        # kept by the ICP move
            self.assertEqual(moved.names, on.names)

    def test_prior_on_the_band_goes_from_bare_to_allowed_and_the_shin_stays_bare(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = self.base_file(tmp)
            off, on = fl.BaseParts.load(path), fl.BaseParts.load(path, knee_cops=True)
            pts = np.array([[.4, 0., .56], [.4, 0., .50], [.4, 0., .25], [.4, 0., .8], [.4, 0., .64]])  # knee band, band edge, lower shin, thigh, above the knee
            prior = lambda base: fl.wear_prior(pts, 'body_armor', None, fl.part_zones(base.membership(pts), base.names))
            p_off, p_on = prior(off), prior(on)
            self.assertLess(p_off[0], .2)                         # until now bare
            self.assertGreater(p_on[0], .4)                       # with knee cops: garment allowed (>= OFF_BODY_MIN_PRIOR)
            self.assertGreater(p_on[1], p_off[1])
            self.assertLess(p_on[2], .1)                          # the rest of the shin stays bare
            self.assertAlmostEqual(p_off[2], p_on[2], places=6)
            self.assertAlmostEqual(p_off[3], .5)
            self.assertAlmostEqual(p_on[3], .5)
            self.assertGreater(p_on[4], p_off[4])                  # the soft membership no longer leaks the shin's bare prior above the knee
            self.assertGreater(p_on[4], .49)

    def test_part_tables_give_the_knee_cop_no_bare_cost(self):
        names = self.NAMES + ['knee_cop.l', 'knee_cop.r']
        tab = R.part_tables(names, 'body_armor')
        self.assertEqual(tab['cost'][names.index('knee_cop.l')], 0.)
        self.assertTrue(tab['allowed'][names.index('knee_cop.r')])
        self.assertEqual(tab['cost'][names.index('leg_stretch.l')], R.BARE_COST)
        self.assertEqual(R.part_family('knee_cop.l'), 'knee_cop')
        self.assertEqual(R.part_side('knee_cop.r'), 2)
        self.assertFalse(tab['expected'][names.index('knee_cop.l')])

    def test_landmark_fallback_moves_the_lower_leg_start_down_only_with_the_flag(self):
        regions = {'landmarks': {'shoulder_x': .5, 'arm_band_z': [1.5, 1.7], 'elbow_x': .8, 'wrist_x': 1.1, 'neck_z': 1.7,
                                 'knee_z': .6, 'crotch_z': .9, 'ankle_z': .2}}
        pts = np.array([[.3, 0., .55], [.3, 0., .3], [.3, 0., .8]])
        base = fl.wear_prior(pts, 'body_armor', regions)
        self.assertTrue(np.array_equal(base, fl.wear_prior(pts, 'body_armor', regions, None, None)))   # flag off = as before
        cops = fl.wear_prior(pts, 'body_armor', regions, None, .33)
        self.assertLess(base[0], .3)
        self.assertGreater(cops[0], .4)
        self.assertLess(cops[1], .3)                           # lower shin still bare
        self.assertTrue(base[2] > .45 and cops[2] > .45)       # the thigh stays garment-allowed


class RiserAndFlaps(unittest.TestCase):
    def test_riser_flips_only_candidates_next_to_garment_and_only_a_few_rings(self):
        v, f = grid(20, 2)
        n = len(f)
        pairs = sf.edge_pairs(f)
        cent = v[f].mean(1)
        lab = np.where(cent[:, 0] < .25, 1, 0)
        cand = cent[:, 0] < .8                                  # a base strip faces along the limb up to x = 0.8
        out, flipped = wc.hem_risers_hp(lab, cand, pairs, rings=3, classes=3)
        self.assertTrue(np.all(out[cent[:, 0] < .25] == 1))
        self.assertTrue(flipped.any())
        far = cent[:, 0] > .25 + 4 * .05 + .1                   # farther than 3 rings from the garment: untouched
        self.assertFalse(flipped[far].any())
        out2, fl2 = wc.hem_risers_hp(lab, np.zeros(n, bool), pairs, rings=3, classes=3)
        self.assertFalse(fl2.any())                             # no candidate (it does not face along the limb): nothing flips
        out3, fl3 = wc.hem_risers_hp(np.zeros(n, int), cand, pairs, rings=3, classes=3)
        self.assertFalse(fl3.any())                             # no garment anywhere: nothing to spread

    def test_riser_takes_the_neighbours_role(self):
        v, f = grid(10, 2)
        pairs = sf.edge_pairs(f)
        cent = v[f].mean(1)
        lab = np.where(cent[:, 0] < .3, 2, 0)
        out, flipped = wc.hem_risers_hp(lab, np.ones(len(f), bool), pairs, rings=1, classes=3)
        self.assertTrue(np.all(out[flipped] == 2))

    def test_flaps_are_peeled_by_height(self):
        v, f = grid(4, 4, 0., .4, 0., .4)
        # a spike triangle on the bottom edge y = 0, tip 0.5 below (> 0.12) and one 0.05 below (kept)
        k = len(v)
        v = np.vstack([v, [[.1, -.5, 0.], [.3, -.05, 0.]]])
        f = np.vstack([f, [[0, 1, k]], [[2, 3, k + 1]]])
        dropped, passes = wc.prune_flaps(v, f, min_height=.12, passes=4)
        self.assertEqual(int(dropped.sum()), 1)
        self.assertTrue(dropped[len(f) - 2] and not dropped[len(f) - 1])
        self.assertEqual(passes, [1])
        # a chain: the second triangle hangs off the first one
        v2 = np.vstack([v, [[.6, -1.0, 0.]]])
        f2 = np.vstack([f, [[0, k, k + 2]]])
        d2, p2 = wc.prune_flaps(v2, f2, min_height=.12, passes=4)
        self.assertGreaterEqual(int(d2.sum()), 2)


def closed_tube(nu=40, nz=40):
    """Capped tube (closed, outward normals)."""
    v, f = tube(nu, nz, .4, 2.)
    n0 = len(v)
    v = np.vstack([v, [[0, 0, 0.], [0, 0, 2.]]])
    f = f.tolist()
    for i in range(nu):
        f.append((n0, (i + 1) % nu, i))                              # bottom cap, normal -Z
        f.append((n0 + 1, nz * nu + i, nz * nu + (i + 1) % nu))      # top cap, normal +Z
    return v, np.array(f, np.int64)


class Orientation(unittest.TestCase):
    def volume(self, v, f):
        t = v[f]
        return float(np.einsum('ij,ij->i', t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6)

    def test_consistent_mesh_is_left_alone(self):
        v, f = closed_tube()
        g, info = R.orient_consistent(v, f)
        self.assertTrue(np.array_equal(g, f))
        self.assertEqual(info['inconsistent_edges_before'], 0)
        self.assertGreater(self.volume(v, f), 0)

    def test_inside_out_shell_is_turned_outward(self):
        v, f = closed_tube()
        g, info = R.orient_consistent(v, f[:, ::-1].copy())
        self.assertGreater(self.volume(v, g), 0)
        self.assertEqual(info['inconsistent_edges_after'], 0)

    def test_flipped_patch_is_turned_back_and_isolated_fold_is_kept(self):
        v, f = closed_tube(40, 40)
        cent = v[f].mean(1)
        patch = (np.abs(np.arctan2(cent[:, 1], cent[:, 0])) < .5) & (cent[:, 2] > .5) & (cent[:, 2] < 1.5) & (cent[:, 0] > 0)
        bad = f.copy()
        bad[patch] = bad[patch][:, ::-1]
        g, info = R.orient_consistent(v, bad)
        self.assertGreater(info['inconsistent_edges_before'], 10)
        self.assertEqual(info['inconsistent_edges_after'], 0)
        self.assertTrue(np.array_equal(np.sort(g, axis=1), np.sort(f, axis=1)))
        n_ok = ((g == f).all(1) | (g == f[:, [1, 2, 0]]).all(1) | (g == f[:, [2, 0, 1]]).all(1)).mean()
        self.assertEqual(float(n_ok), 1.0)                      # the patch ended up as in the original, not the rest flipped
        # one single face flipped: it is a fold, not a patch -> it is flipped back (one face disagrees with all three neighbours)
        one = f.copy()
        one[100] = one[100][::-1]
        g1, i1 = R.orient_consistent(v, one)
        self.assertEqual(i1['inconsistent_edges_after'], 0)


class HueShiftGuard(unittest.TestCase):
    def colours(self, hue_deg, n=400):
        import colorsys
        rgb = colorsys.hsv_to_rgb(hue_deg / 360., .85, .8)
        return np.tile(np.array(rgb, np.float32), (n, 3, 1))

    def test_a_shift_that_loses_the_garment_colour_is_dropped(self):
        pal = Palette.legacy_default()
        shifted = pal.with_hue_shifts({'primary': 18.})
        colours = self.colours(106.)                 # green 19 degrees under the nominal 125: inside the nominal window, outside the shifted one
        used, rec = wc.check_hue_shift(shifted, colours, np.ones(len(colours), bool))
        self.assertFalse(used.hue_shift)
        self.assertGreater(rec['unusable_share_with_shift'], rec['unusable_share_nominal'] + .5)
        self.assertIn('nominal', rec['used'])

    def test_a_shift_that_helps_is_kept(self):
        pal = Palette.legacy_default()
        shifted = pal.with_hue_shifts({'primary': 18.})
        colours = self.colours(143.)                 # green 18 degrees above the nominal 125: only the shifted window covers it
        used, rec = wc.check_hue_shift(shifted, colours, np.ones(len(colours), bool))
        self.assertEqual(used.hue_shift, {'primary': 18.})
        self.assertEqual(rec['used'], "the reduction's hue shift")

    def test_no_shift_means_nominal(self):
        pal = Palette.legacy_default()
        used, rec = wc.check_hue_shift(pal, self.colours(125.), np.ones(400, bool))
        self.assertFalse(used.hue_shift)


class PottsBase(unittest.TestCase):
    def test_off_body_cost_pushes_base_evidence_to_garment(self):
        v, f = grid(16, 16)
        n = len(f)
        pairs = sf.edge_pairs(f)
        cent = v[f].mean(1)
        ev = np.zeros((n, 2))
        ev[:, 0] = 1.                                           # everything looks like base
        ev[cent[:, 0] < .3, 1] = 1.
        ev[cent[:, 0] < .3, 0] = 0.                             # except a garment strip
        bare = np.zeros(n)
        plain = wc.potts_cost(ev, pairs, bare)
        self.assertTrue(np.all(plain[cent[:, 0] > .6] == 0))
        off = np.where((cent[:, 0] > .3) & (cent[:, 0] < .45), wc.P['off_penalty'], 0.)
        pushed = wc.potts_cost(ev, pairs, bare, off)
        self.assertGreater(float((pushed[(cent[:, 0] > .3) & (cent[:, 0] < .45)] > 0).mean()), .9)
        self.assertTrue(np.all(pushed[cent[:, 0] > .6] == 0))

    def test_bare_zone_keeps_base(self):
        v, f = grid(10, 10)
        n = len(f)
        pairs = sf.edge_pairs(f)
        ev = np.zeros((n, 2))
        ev[:, 1] = .6
        lab = wc.potts_cost(ev, pairs, np.full(n, R.BARE_COST))
        self.assertTrue(np.all(lab == 0))


class CutAndPair(unittest.TestCase):
    def test_iso_cut_is_exact_and_clean(self):
        v, f = grid(10, 10)
        u = v[:, 0] + .013 * v[:, 1]
        v2, f2, parent, cut = wc.cut_mesh(v, f, u, .5)
        vc, fc = wc.compact(v2, f2)
        self.assertTrue(np.all(wc.tri_area(vc, fc) > 0))
        self.assertAlmostEqual(float(wc.tri_area(vc, fc).sum()), .5 + .013 * .5, places=3)

    def test_mirror_copy_and_assemble_keep_attributes(self):
        v, f = grid(6, 6, -1., -.1, 0., 1.)               # a right patch (x < 0) touching nothing
        plane = sym.Plane.from_x(0.)
        vm, fm = wc.mirror_copy(v, f, plane)
        n0, n1 = fl.face_normals(v, f)[0], fl.face_normals(vm, fm)[0]
        self.assertTrue(np.allclose(n1, plane.reflect_vectors(n0), atol=1e-9))
        a = {'role': np.ones(len(f), int), 'part': np.full(len(f), 3), 'src': np.zeros(len(f), int), 'mirrored': np.zeros(len(f), int)}
        b = {'role': np.ones(len(f), int), 'part': np.full(len(f), 4), 'src': np.full(len(f), 6), 'mirrored': np.ones(len(f), int)}
        vv, ff, at = wc.assemble([(v, f, a), (vm, fm, b)])
        self.assertEqual(len(ff), 2 * len(f))
        self.assertEqual(int(at['mirrored'].sum()), len(f))
        self.assertEqual(set(at['part'].tolist()), {3, 4})
        # exact mirror: boundary of the pair maps onto itself under the reflection
        _, dense = wc.boundary_metrics(vv, ff, min_len_pct=1.)
        self.assertLess(wc.mirror_mismatch(dense, plane)['max'], 1e-6)


class OutfitAnkleCut(unittest.TestCase):
    """Outfit whose right shoe is joined to the leg garment: the foot part is cut off at the ankle (foot.r zone boundary) and reflected."""
    NAMES = ['spine_01.x', 'foot.r', 'foot.l', 'leg_stretch.r', 'leg_stretch.l', 'thigh_stretch.r', 'thigh_stretch.l']
    ANKLE, KNEE = .2, .6

    def make_prep(self, directory, right_shod=True, left_shod=False):
        """Two vertical tubes (right x = -.3, left x = +.3), z 0..1; foot zone z < .2, lower leg above. Garment (green) on the whole right
        tube (hose + shoe in one piece; the thigh above the knee keeps it from being a footwear component), on the left tube above the
        ankle; the left foot is bare unless `left_shod`."""
        vs, fs, ps, cs = [], [], [], []
        for side, x in (('r', -.3), ('l', .3)):
            v, f = tube(nu=24, nz=50, radius=.1, height=1.)
            v = v + np.array([x, 0., 0.])
            z = v[f].mean(1)[:, 2]
            part = np.where(z < self.ANKLE, self.NAMES.index(f'foot.{side}'),
                            np.where(z < self.KNEE, self.NAMES.index(f'leg_stretch.{side}'), self.NAMES.index(f'thigh_stretch.{side}')))
            shod = right_shod if side == 'r' else left_shod
            green = (z >= self.ANKLE) | shod
            col = np.zeros((len(f), 3, 3), np.float32)
            col[:] = (.90, .10, .14)
            col[green] = (.10, .82, .16)
            fs.append(f + sum(len(a) for a in vs))
            vs.append(v)
            ps.append(part)
            cs.append(col)
        np.savez_compressed(Path(directory) / 'prep.npz', v=np.concatenate(vs).astype(np.float32), f=np.concatenate(fs).astype(np.int32),
                            colors=np.concatenate(cs), part=np.concatenate(ps).astype(np.int16), part_names=np.array(self.NAMES),
                            cos=np.full(len(np.concatenate(ps)), .9, np.float32))
        (Path(directory) / 'prep.json').write_text(json.dumps({}))

    def run_module(self, **kw):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        prep, out = Path(tmp.name) / 'prep', Path(tmp.name) / 'out'
        prep.mkdir()
        self.make_prep(prep, **kw)
        res = wc.run(prep, out, 'outfit', Palette.legacy_default().describe(), sym.Plane.from_x(0.), levels=('0p4',))
        return res, np.load(out / 'wearcut_lod_0p4.npz')

    def test_joined_shoe_is_cut_at_the_ankle_and_reflected(self):
        res, z = self.run_module()
        self.assertIsNone(res['fallback'])
        self.assertEqual(res['footwear'], 'ankle_cut')
        self.assertEqual(res['pair']['status'], 'ankle_cut')
        self.assertEqual(res['pair']['ankle']['piece_open_loops'], 2)             # the ankle loop and the open sole of the synthetic tube
        v, f = z['v'].astype(float), z['f']
        m = z['mirrored'] > 0
        self.assertTrue(m.any() and (~m).any())
        zc, xc = v[f].mean(1)[:, 2], v[f].mean(1)[:, 0]
        self.assertTrue(np.all(xc[m] > 0))                                        # the copy is on the left (+X)
        # the copy is the foot only: it ends at the ankle (clean iso-line, a little smoothing tolerance)
        self.assertLess(float(v[f[m]][:, :, 2].max()), self.ANKLE + .04)
        self.assertGreater(float(v[f[m]][:, :, 2].max()), self.ANKLE - .04)
        self.assertLess(float(v[f[m]][:, :, 2].min()), .02)
        # the right side is one continuous garment from the sole to the top (not split at the ankle), nothing real on the left foot
        right = v[f[~m & (xc < 0)]]
        self.assertGreater(float(right[:, :, 2].max()), .95)
        self.assertLess(float(right[:, :, 2].min()), .02)
        self.assertEqual(int(((~m) & (xc > 0) & (zc < self.ANKLE - .05)).sum()), 0)
        # exact reflection: part foot.r -> foot.l, source mirrored, the pair check
        self.assertTrue(set(z['part'][m].tolist()) <= {self.NAMES.index('foot.l'), self.NAMES.index('leg_stretch.l')})
        self.assertTrue(np.all(z['src'][m] == fl.SOURCES.index('mirrored')))
        pc = res['levels']['0p4']['pair_check']
        self.assertEqual(pc['duplicate_faces'], 0)
        self.assertLess(pc['vertex_reflection_max_error'], 1e-9)
        back = v[np.unique(f[m])] * np.array([-1, 1, 1])                           # reflected back: on the right tube, inside the foot range
        self.assertLess(float(np.abs(np.hypot(back[:, 0] + .3, back[:, 1]) - .1).max()), .01)
        self.assertTrue(np.all(back[:, 0] < 1e-9))

    def test_barefoot_design_adds_nothing_and_does_not_fall_back(self):
        res, z = self.run_module(right_shod=False)
        self.assertIsNone(res['fallback'])
        self.assertEqual(res['footwear'], 'none')
        self.assertIn('barefoot', res['pair']['note'])
        self.assertEqual(int(z['mirrored'].sum()), 0)
        self.assertGreater(len(z['f']), 20)

    def test_left_foot_already_covered_adds_nothing(self):
        # the outfit prior keeps foot.l bare in the labels, so the covered-left rule is tested on the cut garment directly
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            self.make_prep(tmp, left_shod=True)
            z = np.load(Path(tmp) / 'prep.npz')
            v, f, part = z['v'].astype(float), z['f'].astype(np.int64), z['part'].astype(np.int64)
            area = wc.tri_area(v, f)
            M = {'part_names': self.NAMES, 'plane': sym.Plane.from_x(0.), 'part': part}
            args = (M, v, f, np.arange(len(f)), area, area, np.ones(len(f), np.int64), part, np.zeros(len(f), np.int64))
            piece, info = wc.ankle_foot_piece(*args)
            self.assertIsNone(piece)
            self.assertEqual(info['status'], 'none')
            self.assertIn('already covered', info['reason'])
            keep = part != self.NAMES.index('foot.l')                                      # left foot bare again: the same garment now cuts
            piece, info = wc.ankle_foot_piece(M, v, f[keep], np.arange(keep.sum()), area[keep], area, np.ones(keep.sum(), np.int64), part[keep],
                                              np.zeros(keep.sum(), np.int64))
            self.assertEqual(info['status'], 'ankle_cut')
            self.assertGreater(len(piece['f']), 20)

    def test_an_implausible_cut_falls_back_with_a_reason(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            prep, out = Path(tmp) / 'prep', Path(tmp) / 'out'
            prep.mkdir()
            self.make_prep(prep)
            z = dict(np.load(prep / 'prep.npz'))
            z['part'] = np.where(z['part'] == 1, 3, z['part']).astype(np.int16)          # no face on foot.r any more ...
            z['part_names'] = np.array([n if n != 'foot.r' else 'foot.rr' for n in self.NAMES])   # ... and no foot.r part at all
            np.savez_compressed(prep / 'prep.npz', **z)
            res = wc.run(prep, out, 'outfit', Palette.legacy_default().describe(), sym.Plane.from_x(0.), levels=('0p4',))
            self.assertIn('footwear', res['fallback'])
            self.assertIn('foot.r', res['fallback'])

    def test_a_separate_footwear_component_keeps_the_component_path(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            prep, out = Path(tmp) / 'prep', Path(tmp) / 'out'
            prep.mkdir()
            v1, f1 = tube(nu=24, nz=20, radius=.1, height=.2)                              # a shoe tube on the right foot ...
            v2, f2 = tube(nu=24, nz=30, radius=.1, height=.6)                              # ... and a leg tube higher up, not touching
            v1 = v1 + np.array([-.3, 0., 0.])
            v2 = v2 + np.array([-.3, 0., .5])
            v, f = np.vstack([v1, v2]), np.vstack([f1, f2 + len(v1)])
            z = v[f].mean(1)[:, 2]
            part = np.where(z < .2, 1, 3).astype(np.int16)
            col = np.zeros((len(f), 3, 3), np.float32)
            col[:] = (.10, .82, .16)
            np.savez_compressed(prep / 'prep.npz', v=v.astype(np.float32), f=f.astype(np.int32), colors=col, part=part,
                                part_names=np.array(self.NAMES), cos=np.full(len(f), .9, np.float32))
            (prep / 'prep.json').write_text(json.dumps({}))
            res = wc.run(prep, out, 'outfit', Palette.legacy_default().describe(), sym.Plane.from_x(0.), levels=('0p4',))
            self.assertIsNone(res['fallback'])
            self.assertEqual(res['footwear'], 'component')
            self.assertGreater(int(np.load(out / 'wearcut_lod_0p4.npz')['mirrored'].sum()), 0)


class WholeModule(unittest.TestCase):
    """A symmetric tube: green garment above z = 1.0, red base below; one body part. The cut must keep the upper part."""

    def make_prep(self, directory, off=False):
        v, f = tube()
        cent = v[f].mean(1)
        colors = np.zeros((len(f), 3, 3), np.float32)
        garment = cent[:, 2] > 1.0
        colors[:] = (.90, .10, .14)
        colors[garment] = (.10, .82, .16)
        extra = {}
        if off:
            extra = {'off_dist': np.where(cent[:, 2] < .5, .2, -np.inf).astype(np.float32), 'off_thr': np.full(len(f), .03, np.float32),
                     'base_agree': np.ones(len(f), np.float32)}
        np.savez_compressed(Path(directory) / 'prep.npz', v=v.astype(np.float32), f=f.astype(np.int32), colors=colors, part=np.zeros(len(f), np.int16),
                            part_names=np.array(['spine_01.x']), cos=np.full(len(f), .9, np.float32), **extra)
        (Path(directory) / 'prep.json').write_text(json.dumps({'normalization_scale': 1., 'normalization_origin_rotated': [0, 0, 0],
                                                               'rotation_to_review': np.eye(3).tolist()}))
        return garment, cent

    def test_body_armour_tube(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            prep, out = Path(tmp) / 'prep', Path(tmp) / 'out'
            prep.mkdir()
            garment, cent = self.make_prep(prep)
            pal = Palette.legacy_default().describe()
            res = wc.run(prep, out, 'body_armor', pal, sym.Plane.from_x(0.), levels=('0p4',), params={'pool_mode': 'split'})
            self.assertIsNone(res['fallback'])
            z = np.load(out / 'wearcut_lod_0p4.npz')
            v, f, role = z['v'].astype(float), z['f'], z['role']
            self.assertTrue(len(f) > 20)
            self.assertTrue(np.all(role == 1))                                   # legacy palette: green = primary
            self.assertGreater(float(v[:, 2].min()), .95)                        # the base part is gone, the cut is near z = 1
            self.assertLess(float(v[:, 2].min()), 1.06)
            self.assertEqual(int(z['mirrored'].sum()), 0)
            self.assertTrue(np.array_equal(z['part'], np.zeros(len(f))))
            self.assertTrue((out / 'labels.npz').exists() and (out / 'wearcut.json').exists())
            lab = np.load(out / 'labels.npz')['label']
            self.assertGreater(float((lab > 0)[garment].mean()), .97)
            self.assertLess(float((lab > 0)[~garment].mean()), .03)

    def test_label_hook_is_opt_in_scoped_to_the_run_and_cannot_add_garment(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            prep, base_out, hook_out = Path(tmp) / 'prep', Path(tmp) / 'base', Path(tmp) / 'hook'
            prep.mkdir()
            garment, cent = self.make_prep(prep)
            pal = Palette.legacy_default().describe()
            plane = sym.Plane.from_x(0.)
            plain = wc.run(prep, base_out, 'body_armor', pal, plane, levels=('0p4',), params={'pool_mode': 'split'})
            self.assertIsNone(wc.P['label_hook'])
            self.assertNotIn('feature_labels', plain)
            self.assertFalse((base_out / 'feature_labels.npz').exists())
            seen = {}

            def hook(ctx):
                seen['keys'] = sorted(ctx)
                lab = ctx['lab'].copy()
                stripe = (lab > 0) & (ctx['v'][ctx['f']].mean(1)[:, 0] > .2)
                lab[stripe] = 2                                              # a second garment role on one side
                return {'lab': lab, 'info': {'n': int(stripe.sum())}, 'summary': {'n': int(stripe.sum())}, 'arrays': {'extra': np.arange(3)}, 'params': {'sigma_pct': .3}}
            hook.spec = 'test'
            res = wc.run(prep, hook_out, 'body_armor', pal, plane, levels=('0p4',), params={'pool_mode': 'split', 'label_hook': hook})
            self.assertIsNone(wc.P['label_hook'])                            # never left behind
            self.assertEqual(res['feature_labels']['spec'], 'test')
            self.assertEqual(res['field']['sigma_pct'], .3)
            self.assertTrue({'v', 'f', 'lab', 'area', 'part', 'part_names', 'ev', 'pal', 'colors', 'cos', 'module'} <= set(seen['keys']))
            z = np.load(hook_out / 'feature_labels.npz')
            self.assertEqual(sorted(z.files), ['extra', 'label_new', 'label_old'])
            self.assertTrue((np.load(hook_out / 'labels.npz')['label'] == z['label_new']).all())
            self.assertTrue((z['label_old'] != z['label_new']).any())
            z.close()                                                        # Windows: an open handle blocks the re-run below deleting the file
            self.assertTrue((np.load(hook_out / 'wearcut_lod_0p4.npz')['role'] == 2).any())
            # the sources stay those of the unmodified labels
            self.assertTrue((np.load(base_out / 'labels.npz')['source'] == np.load(hook_out / 'labels.npz')['source']).all())
            # same garment/base split: the cut did not move
            self.assertEqual(plain['cut']['faces_labelled_garment'], res['cut']['faces_labelled_garment'])

            def bad(ctx):
                lab = ctx['lab'].copy()
                lab[lab == 0] = 1
                return {'lab': lab}
            with self.assertRaises(ValueError):
                wc.run(prep, Path(tmp) / 'bad', 'body_armor', pal, plane, levels=('0p4',), params={'label_hook': bad})
            # a plain re-run into the same folder removes the pass's files
            wc.run(prep, hook_out, 'body_armor', pal, plane, levels=('0p4',), params={'pool_mode': 'split'})
            self.assertFalse((hook_out / 'feature_labels.npz').exists())

    def test_outfit_without_footwear_component_asks_for_the_fallback(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            prep, out = Path(tmp) / 'prep', Path(tmp) / 'out'
            prep.mkdir()
            self.make_prep(prep)
            res = wc.run(prep, out, 'outfit', Palette.legacy_default().describe(), sym.Plane.from_x(0.), levels=('0p4',))
            self.assertIn('footwear', res['fallback'])
            self.assertEqual(res['pair']['status'], 'no_pair_found')

    def test_gauntlet_module_discards_left_and_reflects_right(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            prep, out = Path(tmp) / 'prep', Path(tmp) / 'out'
            prep.mkdir()
            v, f = tube()
            cent = v[f].mean(1)
            colors = np.zeros((len(f), 3, 3), np.float32)
            colors[:] = (.90, .10, .14)
            colors[cent[:, 2] > 1.0] = (.10, .82, .16)
            names = ['hand.r', 'hand.l']
            part = (cent[:, 0] > 0).astype(np.int16)                           # right = -X = part 0
            np.savez_compressed(prep / 'prep.npz', v=v.astype(np.float32), f=f.astype(np.int32), colors=colors, part=part,
                                part_names=np.array(names), cos=np.full(len(f), .9, np.float32))
            (prep / 'prep.json').write_text(json.dumps({}))
            res = wc.run(prep, out, 'right_gauntlet_boot', Palette.legacy_default().describe(), sym.Plane.from_x(0.), levels=('0p4',))
            self.assertIsNone(res['fallback'])
            z = np.load(out / 'wearcut_lod_0p4.npz')
            v2, f2 = z['v'].astype(float), z['f']
            m = z['mirrored'] > 0
            self.assertTrue(m.any() and (~m).any())
            self.assertEqual(int(m.sum()), int((~m).sum()))
            self.assertTrue(np.all(v2[f2[~m]].mean(1)[:, 0] < 1e-6))              # the real piece is on the right (-X)
            self.assertTrue(np.all(v2[f2[m]].mean(1)[:, 0] > -1e-6))
            self.assertEqual(res['levels']['0p4']['pair_check']['duplicate_faces'], 0)
            self.assertLess(res['levels']['0p4']['pair_check']['vertex_reflection_max_error'], 1e-9)
            self.assertTrue(set(z['part'][m].tolist()) <= {1})                    # mirrored faces carry the left counterpart part
            self.assertTrue(set(z['part'][~m].tolist()) <= {0})
            self.assertTrue(np.all(z['src'][m] == fl.SOURCES.index('mirrored')))

    def test_a_tilted_plane_is_refused(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            prep, out = Path(tmp) / 'prep', Path(tmp) / 'out'
            prep.mkdir()
            self.make_prep(prep)
            bad = sym.Plane([np.cos(np.radians(30)), np.sin(np.radians(30)), 0], 0.)
            with self.assertRaises(ValueError):
                wc.run(prep, out, 'body_armor', Palette.legacy_default().describe(), bad, levels=('0p4',))

    def test_palette_hue_shift_is_used_not_refitted(self):
        pal = Palette.legacy_default().describe()
        shifted = dict(pal, hue_shift={'primary': 10.})
        p = wc.palette_from_dict(shifted)
        self.assertEqual(p.hue_shift, {'primary': 10.})
        q = wc.palette_from_dict(pal)
        self.assertFalse(q.hue_shift)


if __name__ == '__main__':
    unittest.main()
