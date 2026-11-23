"""Tests for body_fit.py (synthetic capsule-like body, synthetic multi-primitive GLBs). Run with characters/.venv/bin/python:

    python -m unittest tools/tripo_pipeline/test_body_fit.py
"""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import body_fit as bf  # noqa: E402

R = .1                      # body radius (m), height 1 m, axis z, centred on x = y = 0
MANIFEST = {'rotation_to_review': np.eye(3).tolist(), 'normalization_scale': 1., 'normalization_origin_rotated': [0, 0, 0],
            'parts': {'fit': {'scale': 1., 'translation': [0, 0, 0]}}, 'module': 'body_armor'}


def cylinder(radius=R, height=1., n=24, rings=5):
    """Closed, outward-wound cylinder (caps included). Parts: 0 = lower half, 1 = upper half of the side and the caps."""
    ang = np.linspace(0, 2 * np.pi, n, endpoint=False)
    zs = np.linspace(0, height, rings)
    V = [[radius * np.cos(a), radius * np.sin(a), z] for z in zs for a in ang]
    V += [[0, 0, 0], [0, 0, height]]
    F, part = [], []
    for r in range(rings - 1):
        for i in range(n):
            a, b = r * n + i, r * n + (i + 1) % n
            c, d = a + n, b + n
            F += [[a, b, d], [a, d, c]]
            part += [0 if zs[r] < height / 2 else 1] * 2
    bot, top = len(V) - 2, len(V) - 1
    for i in range(n):
        j = (i + 1) % n
        F.append([bot, j, i])
        part.append(0)
        F.append([top, (rings - 1) * n + i, (rings - 1) * n + j])
        part.append(1)
    return np.array(V), np.array(F), np.array(part)


def make_body(drop=()):
    V, F, part = cylinder()
    keep = np.ones(len(F), bool)
    keep[list(drop)] = False
    return bf.Body(V, F[keep], part[keep], ['spine_01.x', 'root.x'])


def to_gltf_native(world, body):
    """World points -> the glTF-space coordinates of a native GLB for the identity manifest."""
    return bf.bl_to_gl(bf.tf.world_to_native(world, MANIFEST, MANIFEST['parts']['fit'], body.H, body.o)).astype(np.float32)


def build_glb(prims, nodes=None, scenes=None, compact=True):
    """prims: list of (positions (n,3) float32, indices (m,) uint16 or None, with_normals); one glTF mesh with these primitives,
    unless `nodes`/`scenes` are given (then the caller supplies mesh/node layout through `meshes`)."""
    blob, views, accessors, meshes_prims = bytearray(), [], [], []

    def add(arr, comp, typ, target, minmax=False):
        off = len(blob)
        blob.extend(arr.tobytes())
        blob.extend(b'\0' * (-len(blob) % 4))
        views.append({'buffer': 0, 'byteOffset': off, 'byteLength': arr.nbytes, 'target': target})
        acc = {'bufferView': len(views) - 1, 'componentType': comp, 'count': len(arr), 'type': typ}
        if minmax:
            acc['min'], acc['max'] = [float(x) for x in arr.min(0)], [float(x) for x in arr.max(0)]
        accessors.append(acc)
        return len(accessors) - 1
    for pos, idx, normals in prims:
        attrs = {'POSITION': add(pos.astype(np.float32), 5126, 'VEC3', 34962, True)}
        if normals is not None:
            attrs['NORMAL'] = add(normals.astype(np.float32), 5126, 'VEC3', 34962)
        prim = {'attributes': attrs, 'material': 0}
        if idx is not None:
            prim['indices'] = add(idx.astype(np.uint16), 5123, 'SCALAR', 34963)
        meshes_prims.append(prim)
    js = {'asset': {'version': '2.0'}, 'scene': 0, 'scenes': [{'nodes': [0]}], 'nodes': [{'mesh': 0, 'name': 'wear'}],
          'meshes': [{'name': 'wear', 'primitives': meshes_prims}], 'materials': [{'name': 'primary'}],
          'accessors': accessors, 'bufferViews': views, 'buffers': [{'byteLength': len(blob)}]}
    if nodes is not None:
        js.update(nodes)
    text = json.dumps(js, separators=(',', ':') if compact else None).encode()
    text += b' ' * (-len(text) % 4)
    body = struct.pack('<II', len(text), 0x4E4F534A) + text + struct.pack('<II', len(blob), 0x004E4942) + bytes(blob)
    return struct.pack('<4sII', b'glTF', 2, 12 + len(body)) + body


def shell_prims(body, inside=.07, outside=.13, z0=.3, z1=.7, n=16):
    """An open tube of height z0..z1 around the body; vertices at angle index < n/2 sit at radius `inside` (inside the body),
    the others at `outside`. Two primitives (two halves of the ring) with duplicated seam vertices, one indexed, one not."""
    ang = np.linspace(0, 2 * np.pi, n, endpoint=False)
    rad = np.where(np.arange(n) < n // 2, inside, outside)

    def ring(z):
        return np.stack([rad * np.cos(ang), rad * np.sin(ang), np.full(n, z)], 1)
    lo, hi = ring(z0), ring(z1)
    tris = []
    for i in range(n):
        j = (i + 1) % n
        tris += [(lo[i], lo[j], hi[j]), (lo[i], hi[j], hi[i])]
    half = len(tris) // 2
    out = []
    for k, group in enumerate((tris[:half], tris[half:])):
        pts = np.array([p for t in group for p in t])
        if k == 0:                                   # indexed, vertices shared per quad
            uniq, inv = np.unique(np.round(pts, 9), axis=0, return_inverse=True)
            out.append((to_gltf_native(uniq, body), inv.astype(np.uint16).ravel(), None))
        else:                                        # non-indexed
            out.append((to_gltf_native(pts, body), None, None))
    return out


def with_normals(prims):
    res = []
    for pos, idx, _ in prims:
        t = idx.reshape(-1, 3) if idx is not None else np.arange(len(pos)).reshape(-1, 3)
        n = np.zeros((len(pos), 3), np.float32)
        c = np.cross(pos[t[:, 1]] - pos[t[:, 0]], pos[t[:, 2]] - pos[t[:, 0]])
        c /= np.linalg.norm(c, axis=1, keepdims=True)
        for i in range(3):
            n[t[:, i]] = c
        res.append((pos, idx, n))
    return res


def convex(V, F):
    """Outward-wound triangles of a convex shape around the origin (flip every triangle that faces the origin)."""
    V, F = np.asarray(V, float), np.asarray(F)
    n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    flip = np.einsum('ij,ij->i', n, V[F].mean(1)) < 0
    F = F.copy()
    F[flip] = F[flip][:, [0, 2, 1]]
    return V, F


def cube(h=1.):
    V = np.array([[x, y, z] for x in (-h, h) for y in (-h, h) for z in (-h, h)], float)
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    return convex(V, [t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))])


def icosphere(r=1., sub=2):
    t = (1 + 5 ** .5) / 2
    V = [(-1, t, 0), (1, t, 0), (-1, -t, 0), (1, -t, 0), (0, -1, t), (0, 1, t), (0, -1, -t), (0, 1, -t), (t, 0, -1), (t, 0, 1), (-t, 0, -1), (-t, 0, 1)]
    F = [(0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11), (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
         (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9), (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1)]
    V = [np.array(v, float) / np.linalg.norm(v) for v in V]
    for _ in range(sub):
        mid, G = {}, []

        def m(a, b):
            k = (min(a, b), max(a, b))
            if k not in mid:
                V.append((V[a] + V[b]) / np.linalg.norm(V[a] + V[b]))
                mid[k] = len(V) - 1
            return mid[k]
        for a, b, c in F:
            ab, bc, ca = m(a, b), m(b, c), m(c, a)
            G += [(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)]
        F = G
    return convex(np.array(V) * r, F)


def solid(V, F):
    return bf.Body(V, F, np.zeros(len(F), int), ['a'])


def sign_modes(body, P):
    """(pseudonormal, winding) inside flags of points P."""
    dist, tri, bary = body.closest(P)
    return body.inside(P, dist, tri, bary), body.winding(P) > .5


class PseudonormalTests(unittest.TestCase):
    def points_around(self, body, rng, n=400):
        """Random points, points just above / below faces, over edges and vertices (all along the outward / inward pseudonormal)."""
        box = rng.uniform(body.lo - .5, body.hi + .5, (n, 3))
        w = rng.dirichlet([1, 1, 1], n)
        ti = rng.integers(0, len(body.F), n)
        face = w[:, :1] * body.A[ti] + w[:, 1:2] * body.B[ti] + w[:, 2:] * body.C[ti]
        eps = rng.choice([-1, 1], n)[:, None] * 10 ** rng.uniform(-4, -1, n)[:, None]
        t = rng.uniform(0, 1, (n, 1))
        edge = body.A[ti] * (1 - t) + body.B[ti] * t
        vi = rng.integers(0, len(body.V), n)
        return np.concatenate([box, face + body.tri_n[ti] * eps, edge + (body.tri_n[ti] + rng.normal(0, .3, (n, 3))) * eps, body.V[vi] + body.vn[vi] * eps])

    def test_closed_shapes_match_winding_without_fallback(self):
        rng = np.random.default_rng(0)
        for name, (V, F) in (('cube', cube()), ('icosphere', icosphere())):
            body = solid(V, F)
            pn = body._pseudonormals()
            self.assertFalse(pn['fbad'].any() or pn['ebad'].any() or pn['vbad'].any(), name)      # nothing needs the winding number
            a, w = sign_modes(body, self.points_around(body, rng))
            self.assertTrue((a == w).all(), f'{name}: {(a != w).sum()} mismatches')
            self.assertTrue(a.any() and not a.all())

    def test_cube_corner_and_edge_features(self):
        body = solid(*cube())
        P = np.array([[1.2, 1.2, 1.2], [.9, .9, .9], [1.3, 1.3, 0.], [.95, .95, 0.], [0., 0., 1.2], [0., 0., .9], [2., 0., 0.], [-.5, .5, -.5]])
        a, w = sign_modes(body, P)
        self.assertEqual(a.tolist(), [False, True, False, True, False, True, False, True])        # vertex / edge / face features, outside and inside
        self.assertEqual(a.tolist(), w.tolist())

    def test_measure_modes_agree(self):
        body = solid(*icosphere(.5, 2))
        P = np.random.default_rng(1).uniform(-.8, .8, (300, 3))
        a, b = body.measure(P, 'pseudonormal'), body.measure(P, 'winding')
        self.assertTrue((a['d'] == b['d']).all())
        self.assertTrue((np.abs(a['d'])[a['d'] < 0] <= .5 + 1e-9).all())

    def test_open_mesh_falls_back_to_winding(self):
        V, F, part = cylinder()
        side = [i for i in range(len(F)) if np.all(V[F[i]][:, 2] > .2) and np.all(V[F[i]][:, 2] < .8) and V[F[i]][:, 0].mean() > .05]
        keep = np.ones(len(F), bool)
        keep[side[:3]] = False
        body = bf.Body(V, F[keep], part[keep], ['a', 'b'])
        pn = body._pseudonormals()
        self.assertTrue(pn['ebad'].any() and pn['vbad'].any())
        rng = np.random.default_rng(2)
        P = np.concatenate([self.points_around(body, rng), rng.normal(0, .03, (200, 3)) + [.1, 0, .5]])         # many near the hole
        a, w = sign_modes(body, P)
        self.assertTrue((a == w).all(), f'{(a != w).sum()} mismatches')

    def test_overlapping_parts_follow_the_union(self):
        # two cubes that overlap: a point inside both is nearest to the inner faces of the other part, still inside the union
        V1, F1 = cube()
        V2, F2 = cube(.8)
        body = solid(np.concatenate([V1, V2 + [1.2, 0, 0]]), np.concatenate([F1, F2 + len(V1)]))
        self.assertTrue(body._pseudonormals()['fbad'].any())
        P = np.random.default_rng(3).uniform([-1.5, -1.3, -1.3], [2.4, 1.3, 1.3], (600, 3))
        a, w = sign_modes(body, P)
        self.assertTrue((a == w).all(), f'{(a != w).sum()} mismatches')
        self.assertTrue(w[(P[:, 0] > .5) & (P[:, 0] < .9) & (np.abs(P[:, 1]) < .5) & (np.abs(P[:, 2]) < .5)].all())

    def test_outside_bbox_is_outside_and_on_surface_uses_winding(self):
        body = solid(*cube())
        a, w = sign_modes(body, np.array([[5., 5., 5.], [1., 0., 0.], [1., .3, .2], [-.4, .2, 1.]]))
        self.assertFalse(a[0])
        self.assertEqual(a.tolist(), w.tolist())

    def test_game_bases_match_winding(self):
        found = False
        for race in ('elf_male', 'human_male', 'gnome_female'):                 # a clean base and two with overlapping parts / holes
            if not (bf.BASE_CACHE / f'{race}.npz').is_file():
                continue
            found = True
            body = bf.Body.load(race)
            a, w = sign_modes(body, self.points_around(body, np.random.default_rng(4), 500))
            self.assertTrue((a == w).all(), f'{race}: {(a != w).sum()} mismatches')
        if not found:
            self.skipTest('no cached game base')


class BodyTests(unittest.TestCase):
    def test_signed_distance(self):
        body = make_body()
        m = body.measure(np.array([[0, 0, .5], [.15, 0, .5], [.09, 0, .5], [0, 0, 1.2], [0, 0, -.05]]))
        d = m['d']
        self.assertAlmostEqual(d[0], -R * np.cos(np.pi / 24), delta=2e-3)          # polygonal cylinder: inradius
        self.assertAlmostEqual(d[1], .15 - R, delta=3e-3)
        self.assertLess(d[2], 0)
        self.assertAlmostEqual(d[3], .2, places=6)
        self.assertAlmostEqual(d[4], .05, places=6)
        self.assertAlmostEqual(m['n'][1][0], 1, delta=.05)                        # outward normal at the side
        self.assertAlmostEqual(m['n'][3][2], 1, delta=1e-6)                       # and at the cap

    def test_closest_matches_brute_force(self):
        body = make_body()
        rng = np.random.default_rng(3)
        P = rng.uniform([-.3, -.3, -.2], [.3, .3, 1.2], (60, 3))
        d = body.closest(P)[0]
        for p, di in zip(P, d):
            bar = bf.tri_closest(np.repeat(p[None], len(body.F), 0), body.A, body.B, body.C)
            q = bar[:, :1] * body.A + bar[:, 1:2] * body.B + bar[:, 2:] * body.C
            self.assertAlmostEqual(np.linalg.norm(q - p, axis=1).min(), di, places=9)

    def test_inside_test_survives_a_hole(self):
        V, F, part = cylinder()
        side = [i for i in range(len(F)) if np.all(V[F[i]][:, 2] > .2) and np.all(V[F[i]][:, 2] < .8) and V[F[i]][:, 0].mean() > .05]
        body = make_body(drop=side[:3])
        self.assertLess(body.measure(np.array([[0, 0, .5]]))['d'][0], 0)
        self.assertGreater(body.measure(np.array([[.3, 0, .5]]))['d'][0], 0)

    def test_winding_orientation_is_fixed(self):
        V, F, part = cylinder()
        body = bf.Body(V, F[:, [0, 2, 1]], part, ['a', 'b'])                        # inverted winding on input
        self.assertGreater(body.winding(np.array([[0, 0, .5]]))[0], .9)


class GlbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.body = make_body()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_roundtrip_patch_keeps_everything_else(self):
        prims = with_normals(shell_prims(self.body))
        path = self.write('a.glb', build_glb(prims))
        glb = bf.Glb(path)
        same = glb.patched()
        self.assertEqual(same, path.read_bytes())                                   # no change -> byte-identical file
        new = {0: glb.positions(0) + [0, .01, 0], 1: glb.positions(1) + [0, .01, 0]}
        normals = {k: glb.normals(k) for k in (0, 1)}
        data = glb.patched(new, normals)
        out = self.write('b.glb', data)
        g2 = bf.Glb(out)                                                            # loadable
        self.assertEqual(len(g2.prims), 2)
        a, b = json.loads(glb.chunks[0][1]), json.loads(g2.chunks[0][1])
        for acc in a['accessors'] + b['accessors']:
            acc.pop('min', None), acc.pop('max', None)
        self.assertEqual(a, b)                                                      # everything but min/max in the JSON identical
        pos_ranges = []
        for k in range(2):
            acc = a['accessors'][glb.prims[k]['def']['attributes']['POSITION']]
            view = a['bufferViews'][acc['bufferView']]
            pos_ranges.append((view['byteOffset'], view['byteOffset'] + acc['count'] * 12))
        mask = np.ones(len(glb.blob), bool)
        for lo, hi in pos_ranges:
            mask[lo:hi] = False
        self.assertEqual(bytes(np.frombuffer(glb.blob, np.uint8)[mask]), bytes(np.frombuffer(g2.blob, np.uint8)[mask]))
        np.testing.assert_allclose(g2.positions(0), new[0].astype(np.float32), atol=1e-7)
        mm = json.loads(g2.chunks[0][1])['accessors'][g2.prims[0]['def']['attributes']['POSITION']]
        self.assertAlmostEqual(mm['max'][1], float(g2.positions(0)[:, 1].max()), places=6)   # accessor min/max updated
        self.assertEqual(normals[0].shape, g2.normals(0).shape)

    def test_clearance_triangle_order_multi_primitive(self):
        prims = shell_prims(self.body)
        path = self.write('m.glb', build_glb(prims))
        surf = bf.Surface(bf.Glb(path), MANIFEST, self.body)
        meas = bf.measure_surface(surf)
        doc = bf.clearance_doc(surf, meas, .004, bf.summarize(meas, .004, self.body.part_names))
        self.assertEqual([m['mesh_index'] for m in doc['meshes']], [0, 1])
        self.assertEqual([len(m['d_mm']) for m in doc['meshes']], [16, 16])
        # triangle t of primitive k: the minimum of its three vertices' signed distances
        for k, (pos, idx, _) in enumerate(prims):
            t = np.arange(len(pos)).reshape(-1, 3) if idx is None else idx.reshape(-1, 3)
            world = bf.tf.native_to_world(bf.gl_to_bl(pos.astype(np.float64)), MANIFEST, MANIFEST['parts']['fit'], self.body.H, self.body.o)
            d = self.body.measure(world)['d']
            expect = d[t].min(1) * 1000
            np.testing.assert_allclose(doc['meshes'][k]['d_mm'], np.round(expect, 1), atol=.051)
        self.assertEqual(doc['model'], str(path) if not path.is_relative_to(bf.ROOT) else path.relative_to(bf.ROOT).as_posix())

    def test_traversal_order_follows_nodes_depth_first(self):
        pos = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.float32)
        glb = build_glb([(pos, None, None), (pos + 5, None, None)])
        js_len = struct.unpack_from('<I', glb, 12)[0]
        js = json.loads(glb[20:20 + js_len])
        js['meshes'] = [{'name': 'a', 'primitives': [js['meshes'][0]['primitives'][0]]}, {'name': 'b', 'primitives': [js['meshes'][0]['primitives'][1]]}]
        js['nodes'] = [{'name': 'root', 'children': [2, 1]}, {'name': 'n1', 'mesh': 0}, {'name': 'n2', 'mesh': 1, 'translation': [0, 0, 1]}]
        js['scenes'] = [{'nodes': [0]}]
        text = json.dumps(js, separators=(',', ':')).encode()
        text += b' ' * (-len(text) % 4)
        data = glb[:12] + struct.pack('<II', len(text), 0x4E4F534A) + text + glb[20 + js_len:]
        g = bf.Glb(self.write('t.glb', data))
        self.assertEqual([p['name'] for p in g.prims], ['b', 'a'])                  # children in listed order: node 2 then node 1
        self.assertAlmostEqual(g.prims[0]['matrix'][2, 3], 1.)


class FitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.body = make_body()
        self.margin = .004

    def tearDown(self):
        self.tmp.cleanup()

    def test_push_clears_margin_and_keeps_structure(self):
        prims = with_normals(shell_prims(self.body))
        path = self.dir / 'w.glb'
        path.write_bytes(build_glb(prims))
        glb = bf.Glb(path)
        surf = bf.Surface(glb, MANIFEST, self.body)
        before = bf.measure_surface(surf)
        self.assertLess(before['tri_d'].min(), 0)                                    # the shell does poke into the body
        info = bf.fit_file(surf, before, self.margin, 'push', self.dir / 'f.glb', self.body, MANIFEST)
        self.assertTrue(info['bend'])
        g2 = bf.Glb(self.dir / 'f.glb')
        s2 = bf.Surface(g2, MANIFEST, self.body)
        after = bf.measure_surface(s2)
        self.assertGreaterEqual(after['d'].min(), self.margin - 6e-4)               # cleared (within the 0.5 mm tolerance + float32)
        self.assertEqual(s2.K, surf.K)                                              # topology: same welded graph
        for k in range(2):
            np.testing.assert_array_equal(g2.triangles(k), glb.triangles(k))        # face order and indices untouched
            self.assertEqual(g2.prims[k]['name'], glb.prims[k]['name'])
        self.assertEqual(json.loads(glb.chunks[0][1])['materials'], json.loads(g2.chunks[0][1])['materials'])
        # welded seams stay welded: corners that shared a position still share it
        np.testing.assert_array_equal(np.unique(s2.wid), np.unique(surf.wid))
        W0, W1 = surf.Wc, s2.Wc
        for w in range(surf.K):
            members = np.flatnonzero(surf.wid == w)
            self.assertLess(np.ptp(W1[members], axis=0).max(), 2e-6)
        # normals were recomputed (flat shaded: they follow the new faces)
        t = g2.triangles(1)
        p = g2.positions(1)
        fn = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
        fn /= np.linalg.norm(fn, axis=1, keepdims=True)
        nn = g2.normals(1)[t[:, 0]]
        self.assertGreater((fn * nn).sum(1).min(), .999)
        # the poke-through is the only thing that moved much
        self.assertGreater(info['displacement']['max_mm'], 20)
        self.assertEqual(W0.shape, W1.shape)

    def test_push_leaves_clear_geometry_alone(self):
        prims = shell_prims(self.body, inside=.13, outside=.14)
        path = self.dir / 'w.glb'
        path.write_bytes(build_glb(prims))
        surf = bf.Surface(bf.Glb(path), MANIFEST, self.body)
        meas = bf.measure_surface(surf)
        info = bf.fit_file(surf, meas, self.margin, 'push', self.dir / 'f.glb', self.body, MANIFEST)
        self.assertEqual(info['displacement']['max_mm'], 0)
        self.assertEqual((self.dir / 'f.glb').read_bytes(), path.read_bytes())

    def test_rigid_scale_keeps_planes_planar(self):
        """A flat panel (plane x = .09) poking into the body: the affine scale about the up axis moves it but keeps it a plane."""
        ys, zs = np.meshgrid(np.linspace(-.03, .03, 4), np.linspace(.3, .7, 5))
        pts = np.stack([np.full(ys.size, .09), ys.ravel(), zs.ravel()], 1)
        tris = []
        for i in range(4):
            for j in range(3):
                a, b, c, d = i * 4 + j, i * 4 + j + 1, (i + 1) * 4 + j + 1, (i + 1) * 4 + j
                tris += [[a, b, c], [a, c, d]]
        path = self.dir / 'p.glb'
        path.write_bytes(build_glb([(to_gltf_native(pts, self.body), np.array(tris, np.uint16).ravel(), None)]))
        surf = bf.Surface(bf.Glb(path), MANIFEST, self.body)
        before = bf.measure_surface(surf)
        self.assertLess(before['tri_d'].min(), 0)
        info = bf.fit_file(surf, before, self.margin, 'rigid', self.dir / 'r.glb', self.body, MANIFEST)
        self.assertEqual(info['method'], 'scale')
        self.assertGreater(info['s'], 1.0)
        self.assertLessEqual(info['s'], bf.S_MAX)
        self.assertEqual(info['axis_source'], 'up')
        s2 = bf.Surface(bf.Glb(self.dir / 'r.glb'), MANIFEST, self.body)
        W = s2.U - s2.U.mean(0)
        sv = np.linalg.svd(W, compute_uv=False)
        self.assertLess(sv[2] / sv[0], 1e-5)                                         # still planar
        self.assertGreaterEqual(bf.measure_surface(s2)['d'].min(), self.margin - 6e-4)
        # minimal: a slightly smaller scale would not clear
        smaller = bf.scaled(surf.U, np.array(info['pivot']), np.array(info['axis']), info['s'] - .01)
        self.assertLess(bf.measure_surface(surf, smaller)['d'].min(), self.margin - bf.TOL)

    def test_rigid_falls_back_to_push_when_scale_cannot_clear(self):
        # a tube all around the axis at r = .02 (deep inside): scaling about the axis by <= 1.35 cannot reach the surface
        ang = np.linspace(0, 2 * np.pi, 12, endpoint=False)
        ring = lambda z: np.stack([.02 * np.cos(ang), .02 * np.sin(ang), np.full(12, z)], 1)
        pts = np.concatenate([ring(.4), ring(.6)])
        tris = []
        for i in range(12):
            j = (i + 1) % 12
            tris += [[i, j, 12 + j], [i, 12 + j, 12 + i]]
        path = self.dir / 'd.glb'
        path.write_bytes(build_glb([(to_gltf_native(pts, self.body), np.array(tris, np.uint16).ravel(), None)]))
        surf = bf.Surface(bf.Glb(path), MANIFEST, self.body)
        before = bf.measure_surface(surf)
        info = bf.fit_file(surf, before, self.margin, 'rigid', self.dir / 'd_fit.glb', self.body, MANIFEST)
        self.assertEqual(info['method'], 'push')
        self.assertIn('needs_review', info)
        s2 = bf.Surface(bf.Glb(self.dir / 'd_fit.glb'), MANIFEST, self.body)
        self.assertGreaterEqual(bf.measure_surface(s2)['d'].min(), self.margin - 6e-4)


class PokeTests(unittest.TestCase):
    """A big flat panel (two triangles) whose corners are all outside the body but whose middle is behind the body surface."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.body = make_body()
        self.margin = .004
        ys = np.array([-.3, .3])
        pts = np.array([[.095, ys[0], .3], [.095, ys[1], .3], [.095, ys[1], .7], [.095, ys[0], .7]])
        # wound so the panel faces +x (outward), like the garment
        tris = np.array([[0, 1, 2], [0, 2, 3]], np.uint16)
        self.path = self.dir / 'panel.glb'
        self.path.write_bytes(build_glb([(to_gltf_native(pts, self.body), tris.ravel(), None)]))

    def tearDown(self):
        self.tmp.cleanup()

    def surf(self, path=None):
        return bf.Surface(bf.Glb(path or self.path), MANIFEST, self.body)

    def test_poke_is_measured_although_every_vertex_is_outside(self):
        surf = self.surf()
        self.assertGreater(bf.measure_surface(surf)['d'].min(), self.margin)          # the vertex test sees nothing
        scene = bf.scene_for(self.body, [surf.U])
        pk = scene.poke([scene.cast(*bf.surf_tris(surf, surf.U))])
        m = bf.poke_metrics(pk, self.body.part_names, scene.spacing)
        self.assertGreater(m['area_cm2'], 20)                                          # a strip of the cylinder about 2 cm wide x 40 cm
        self.assertGreater(m['depth_mm']['max'], 3)
        self.assertLess(m['depth_mm']['max'], 6.5)
        self.assertEqual(set(pk['src']), {0})
        # the sidecar folds it in: both triangles read negative although all vertices are clear
        meas, metrics, _, _ = bf.measure_surfaces({'p': surf}, self.body, self.margin, {'p': self.path})
        doc = json.loads(self.path.with_suffix('.clearance.json').read_text())
        self.assertTrue(all(d < 0 for d in doc['meshes'][0]['d_mm']))
        self.assertGreater(doc['metrics']['poke']['area_cm2'], 20)
        self.assertGreater(metrics['p']['min_d_mm'], 0)                                 # the vertex metrics are unchanged

    def test_other_surface_covering_the_body_removes_the_poke(self):
        surf = self.surf()
        scene = bf.scene_for(self.body, [surf.U])
        cover = scene.cast(*(x + [.05, 0, 0] for x in bf.surf_tris(surf, surf.U)))      # the same panel 5 cm further out
        own = scene.cast(*bf.surf_tris(surf, surf.U))
        self.assertEqual(len(scene.poke([own, cover])['depth']), 0)

    def test_full_fit_clears_the_body_and_vertex_fit_does_not(self):
        surf = self.surf()
        meas = bf.measure_surface(surf)
        info = bf.fit_file(surf, meas, self.margin, 'push', self.dir / 'full.glb', self.body, MANIFEST)
        self.assertTrue(info['push']['converged'])
        fs = self.surf(self.dir / 'full.glb')
        scene = bf.scene_for(self.body, [fs.U])
        pk = scene.poke([scene.cast(*bf.surf_tris(fs, fs.U))])
        self.assertTrue(bf.poke_ok(pk))
        self.assertEqual(len(pk['depth']), 0)
        self.assertGreaterEqual(bf.measure_surface(fs)['d'].min(), self.margin - 6e-4)
        np.testing.assert_array_equal(bf.Glb(self.dir / 'full.glb').triangles(0), bf.Glb(self.path).triangles(0))
        # method `vertex` (the old algorithm) leaves the panel alone: every vertex already clears
        bf.fit_file(surf, meas, self.margin, 'push', self.dir / 'vertex.glb', self.body, MANIFEST, method='vertex')
        self.assertEqual((self.dir / 'vertex.glb').read_bytes(), self.path.read_bytes())

    def test_rigid_scale_searches_both_tests(self):
        surf = self.surf()
        meas = bf.measure_surface(surf)
        info = bf.fit_file(surf, meas, self.margin, 'rigid', self.dir / 'rigid.glb', self.body, MANIFEST)
        self.assertIn(info['method'], ('scale', 'push'))
        fs = self.surf(self.dir / 'rigid.glb')
        scene = bf.scene_for(self.body, [fs.U])
        self.assertTrue(bf.poke_ok(scene.poke([scene.cast(*bf.surf_tris(fs, fs.U))])))


if __name__ == '__main__':
    unittest.main()
