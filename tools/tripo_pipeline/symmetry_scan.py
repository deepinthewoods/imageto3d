"""Mirror-symmetry scan of converted models (read-only; see symmetry.py).

Batch mode (plain python3, needs numpy + Pillow for the sheets):

    python3 tools/tripo_pipeline/symmetry_scan.py models/<set> [models/<set> ... | models/<set>/<job>/<race>/<module>]
        [--out experiments/symmetry_scan_20261024] [--threads 2] [--samples 24000] [--no-render] [--force]
        [--stratify N] [--limit N] [--summary-only] [--dry-run]

runs `blender -b -t <threads> --python symmetry_scan.py -- --module-dir ... --out-dir ...` once per module, serially,
for every module with `reduced/manifest.json`, writing ONLY under `<out>/<set>/<job_id>/<race>/<module>/`:

    symmetry.json           fitted plane (+ comparison with the pipeline's bbox-centre plane x=0 and the base-mapped
                            plane), global / per-body-part-family / height-band / hot-spot statistics, regions
                            flagged as well-seen asymmetric, regions that are asymmetric by design
    heat_geometry.png       mirror distance on the low-poly model, front | back (blue 0 -> red >= 4% of height)
    heat_colour.png         colour agreement of mirror partners, front | back
    plane_overlay_front.png / plane_overlay_top.png
                            model (blue) + its reflection across the fitted plane (orange), x-ray; green line =
                            fitted plane, yellow = the pipeline's x = 0, cyan = the base-mapped plane

and afterwards `<out>/summary.json`, `summary.csv` and contact sheets of the 8 most / 8 least symmetric `body_armor`
models (`<out>/contact_sheets/`, tools/contact_sheet.py). Nothing under models/ or processing/ is touched.

Blender mode (`blender -b --python symmetry_scan.py -- --module-dir D --out-dir O [--samples N] [--no-render]`) scans one
module; the batch mode calls it.
"""
import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

try:
    import bpy  # noqa: F401
except ImportError:
    bpy = None

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import symmetry as sym  # noqa: E402

DEFAULT_OUT = ROOT / 'experiments/symmetry_scan_20261024'
BLENDER = '/usr/local/bin/blender'
HEAT_SIZE = (1800, 900)

# colour heatmap classes (sRGB)
CLASS_COLOURS = np.array([(.62, .74, .66),    # 0 partners agree
                          (.86, .08, .08),    # 1 disagree, both sides well seen
                          (.98, .62, .10),    # 2 disagree, a side poorly seen
                          (.22, .22, .25),    # 3 not compared (unusable colour or no real counterpart)
                          (.30, .34, .95)])   # 4 disagree inside a region that is asymmetric by design
GEO_STOPS = [(0., (.10, .12, .45)), (.5, (.12, .55, .80)), (1., (.20, .75, .35)), (2., (.98, .85, .15)), (4., (.90, .10, .10))]


def jsonable(x):
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, (np.floating, float)):
        return None if not math.isfinite(float(x)) else float(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, np.ndarray):
        return jsonable(x.tolist())
    return x


def dump(path, value):
    Path(path).write_text(json.dumps(jsonable(value), indent=1, sort_keys=False) + '\n')


def module_identity(module_dir):
    """(set, job id, race, module) from models/<set>/<job>/<race>/<module>."""
    p = Path(module_dir).resolve().parts
    return p[-4], p[-3], p[-2], p[-1]


def backend_of(module_dir):
    """Generation backend from the submission record (`backend`; Tripo records have no such key)."""
    path = Path(module_dir) / 'studio_submission.json'
    if not path.is_file():
        return 'unknown'
    try:
        return str(json.loads(path.read_text(encoding='utf-8-sig')).get('backend') or 'tripo')
    except (OSError, ValueError):
        return 'unknown'


# ================================================================= Blender part

def srgb_to_linear(c):
    return np.asarray(c, float) ** 2.2


def geo_colours(pct):
    stops_x = np.array([s[0] for s in GEO_STOPS])
    stops_c = np.array([s[1] for s in GEO_STOPS])
    return np.stack([np.interp(pct, stops_x, stops_c[:, i]) for i in range(3)], axis=1)


def resolve_inputs(module_dir, manifest):
    """Cameras / regions / parts exactly as the reduction used them (manifest paths while they exist, else the same
    resolution reduce_model.py does, which also finds jobs that have since moved between pipeline folders)."""
    import reduce_model as rm
    module_dir = Path(module_dir)
    found = {}
    try:
        _, cam, reg, parts, _, notes, _ = rm.resolve_inputs(module_dir / 'source.glb', None, None, None,
                                                            manifest.get('module'), None, True, None, False)
        found = {'cameras': cam, 'regions': reg, 'parts': parts}
    except Exception as exc:   # noqa: BLE001 - report and carry on without
        found = {'error': repr(exc)}
    out = {}
    for key, recorded in (('cameras', manifest.get('cameras')), ('regions', manifest.get('regions')),
                          ('parts', (manifest.get('parts') or {}).get('file'))):
        for candidate in (recorded, found.get(key)):
            if candidate and Path(candidate).is_file():
                out[key] = Path(candidate)
                break
        else:
            out[key] = None
    if out['parts'] is None:
        fallback = ROOT / 'base_views' / module_dir.parent.name / 'parts.npz'
        if fallback.is_file() and manifest.get('parts'):
            out['parts'] = fallback
    out['note'] = found.get('error')
    return out


def palette_from_manifest(manifest):
    from palette import Palette
    d = dict(manifest['palette'])
    pal = Palette(d, d.get('source', 'manifest'))
    pal.separable = bool(d.get('separable'))
    shifts = d.get('hue_shift') or {}
    return pal.with_hue_shifts(shifts) if shifts else pal


def load_surface(module_dir, manifest):
    """The source GLB in reduce_model's normalized frame (interior strip repeated from the recorded removed faces)."""
    import bpy
    import reduce_model as rm
    source = Path(module_dir) / 'source.glb'
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(source), merge_vertices=False)
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    if len(meshes) != 1:
        raise ValueError(f'Expected one mesh in {source}; found {len(meshes)}')
    obj = meshes[0]
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    n = len(obj.data.vertices)
    native = np.empty(n * 3, np.float32)
    obj.data.vertices.foreach_get('co', native)
    turned = native.reshape(-1, 3).astype(np.float64) @ np.asarray(manifest['rotation_to_review']).T
    co = (turned - np.asarray(manifest['normalization_origin_rotated'])) * manifest['normalization_scale']
    obj.data.vertices.foreach_set('co', co.ravel())
    obj.data.update()
    ts = rm.texture_tools.TextureSurface(obj)
    del ts.tree
    f, uv = ts.f, ts.uv
    stripped = 0
    removed_path = Path(module_dir) / 'reduced' / 'interior_removed.npz'
    if (manifest.get('interior') or {}).get('stripped') and removed_path.is_file():
        rf = np.load(removed_path)['f'].astype(np.int64)
        big = np.int64(n)
        key = lambda t: (lambda s: (s[:, 0] * big + s[:, 1]) * big + s[:, 2])(np.sort(t.astype(np.int64), axis=1))
        gone = np.isin(key(f), key(rf))
        stripped = int(gone.sum())
        f, uv = f[~gone], uv[~gone]
    return sym.Surface(ts.v, f, uv, ts.pixels), {'faces': int(len(f)), 'vertices': n, 'interior_faces_removed': stripped}


def load_lod(module_dir, manifest, names=('lod_error_0p2.glb', 'lod_error_0p4.glb')):
    """Reduced low-poly level (default 0.2%, else 0.4%) in the normalized frame, for heat maps and overlays."""
    import bpy
    for name in names:
        path = Path(module_dir) / 'reduced' / name
        if path.is_file():
            break
    else:
        return None
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(path), merge_vertices=False)
    obj = [o for o in bpy.context.scene.objects if o.type == 'MESH'][0]
    mw = np.array(obj.matrix_world)
    mesh = obj.data
    mesh.calc_loop_triangles()
    v = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get('co', v)
    v = v.reshape(-1, 3).astype(np.float64) @ mw[:3, :3].T + mw[:3, 3]
    f = np.empty(len(mesh.loop_triangles) * 3, np.int64)
    mesh.loop_triangles.foreach_get('vertices', f)
    v = (v @ np.asarray(manifest['rotation_to_review']).T - np.asarray(manifest['normalization_origin_rotated'])) \
        * manifest['normalization_scale']
    return v, f.reshape(-1, 3), path.name


def plane_comparison(surface, fitted, others, points):
    """Residual of each plane (reflected samples vs surface, % height) and how far each other plane's mirror image of the
    samples lies from the fitted plane's."""
    out = {'fitted': plane_residual(surface, fitted, points)}
    for name, plane in others.items():
        r = plane_residual(surface, plane, points)
        shift = np.linalg.norm(plane.reflect(points) - fitted.reflect(points), axis=1) * sym.PCT
        r.update(plane=plane.to_dict(),
                 mirror_displacement_vs_fitted_pct_height={'mean': float(shift.mean()), 'p95': float(np.quantile(shift, .95)),
                                                          'max': float(shift.max())},
                 x_mid_height_minus_fitted_pct_height=float((plane.x_at(0, 1) - fitted.x_at(0, 1)) * sym.PCT))
        out[name] = r
    return out


def plane_residual(surface, plane, points):
    d = surface.distances(plane.reflect(points)) * sym.PCT
    return {'p50_pct_height': float(np.quantile(d, .5)), 'p95_pct_height': float(np.quantile(d, .95)),
            'trimmed_mean_pct_height': sym.trimmed_mean(d)}


def class_codes(rec, d, expected_families):
    """Per point colour class (CLASS_COLOURS index)."""
    code = np.full(len(rec['geo']), 3)
    comp = d['compared']
    code[comp & ~d['disagree']] = 0
    dis = comp & d['disagree']
    code[dis & d['seen_both']] = 1
    code[dis & ~d['seen_both']] = 2
    if 'family' in rec and len(expected_families):
        code[dis & np.isin(rec['family'], expected_families)] = 4
    return code


def face_heat(surface, plane, lod_v, lod_f, pal, cameras, parts, module):
    """Per low-poly face: mean mirror distance (% height) and the share of its 4 sample points in each colour class."""
    cent = lod_v[lod_f].mean(1)
    pts = np.concatenate([cent] + [cent + .5 * (lod_v[lod_f[:, i]] - cent) for i in range(3)])
    rec = sym.measure(surface, plane, pts, pal, cameras, parts)
    names = None
    if parts is not None:
        names = parts.names
        fam = [sym.part_family(n) for n in names]
        rec['family'] = np.array([fam[p] for p in rec['part']])
    d = sym.derived(rec, pal.n_garments)
    code = class_codes(rec, d, sym.expected_asymmetric_families(module))
    k = len(lod_f)
    geo = (rec['geo'] * sym.PCT).reshape(4, k).mean(0)
    shares = np.stack([(code.reshape(4, k) == c).mean(0) for c in range(len(CLASS_COLOURS))], axis=1)
    return geo, shares


# ---- rendering (Blender workbench)

def new_scene(world=(.52, .52, .55)):
    import bpy
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.render.film_transparent = False
    scene.view_settings.view_transform = 'Standard'
    scene.display.shading.light = 'FLAT'
    scene.display.shading.show_object_outline = False
    scene.display.shading.show_cavity = False
    scene.display.shading.show_specular_highlight = False
    scene.world = bpy.data.worlds.new('w')
    scene.world.color = srgb_to_linear(world)
    return scene


def add_mesh(name, v, f, face_rgb=None, flip=False, colour=None):
    """Mesh object; `face_rgb` (faces, 3) sRGB becomes a corner colour attribute (viewport colour 'Vertex')."""
    import bpy
    faces = np.asarray(f)[:, ::-1] if flip else np.asarray(f)
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(np.asarray(v, float).tolist(), [], faces.tolist())
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    if face_rgb is not None:
        attr = mesh.color_attributes.new('heat', 'FLOAT_COLOR', 'CORNER')
        rgba = np.concatenate([srgb_to_linear(np.repeat(face_rgb, 3, axis=0)), np.ones((len(faces) * 3, 1))], axis=1)
        attr.data.foreach_set('color', rgba.astype(np.float32).ravel())
        mesh.color_attributes.active_color = attr
        mesh.color_attributes.render_color_index = list(mesh.color_attributes).index(attr)
    if colour is not None:
        obj.color = (*srgb_to_linear(colour), 1.)
    return obj


def add_box(name, centre, axes, half, colour):
    """Thin box with local axes (3 unit vectors) and half sizes (3), as an object colour `colour`."""
    corners = [centre + sum(s * h * a for s, h, a in zip(signs, half, axes))
               for signs in ((-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1), (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1))]
    faces = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (0, 3, 7, 4)]
    import bpy
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([list(map(float, c)) for c in corners], [], faces)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.color = (*srgb_to_linear(colour), 1.)
    obj.show_in_front = True
    return obj


def set_camera(location, rotation, scale, size):
    import bpy
    cam = bpy.data.objects.new('cam', bpy.data.cameras.new('cam'))
    bpy.context.scene.collection.objects.link(cam)
    cam.data.type = 'ORTHO'
    cam.data.ortho_scale = scale
    cam.location, cam.rotation_euler = location, rotation
    bpy.context.scene.camera = cam
    bpy.context.scene.render.resolution_x, bpy.context.scene.render.resolution_y = size
    bpy.context.scene.render.resolution_percentage = 100


def render_to(path):
    import bpy
    bpy.context.scene.render.filepath = str(path)
    bpy.context.scene.render.image_settings.file_format = 'PNG'
    bpy.ops.render.render(write_still=True)


def render_front_back(v, f, face_rgb, path):
    """The mesh seen from the front (model faces -Y) and, rotated half a turn beside it, from the back."""
    import bpy
    scene = new_scene()
    scene.display.shading.color_type = 'VERTEX'
    width = float(v[:, 0].max() - v[:, 0].min())
    s = max(width / 2 + .2, 1.15)
    front = add_mesh('front', v, f, face_rgb)
    front.location.x = -s
    back = add_mesh('back', v, f, face_rgb)
    back.rotation_euler.z = math.pi
    back.location.x = s
    set_camera((0., -12., 1.0), (math.pi / 2, 0., 0.), 4 * s, HEAT_SIZE)
    render_to(path)


def render_overlay(v, f, plane, planes, paths):
    """Model (blue) + reflection across the fitted plane (orange) seen from the front and from above, x-ray."""
    import bpy
    scene = new_scene((.93, .93, .94))
    scene.display.shading.color_type = 'OBJECT'
    scene.display.shading.show_xray = True
    scene.display.shading.xray_alpha = .55
    add_mesh('model', v, f, colour=(.20, .42, .95))
    add_mesh('mirror', plane.reflect(v), f, flip=True, colour=(.98, .50, .08))
    for (name, p, colour), thickness in zip(planes, (.018, .012, .006)):    # nested so near-identical planes all show
        n = p.n
        u1 = np.array([0., 1., 0.]) - n * n[1]
        u1 /= np.linalg.norm(u1)
        u2 = np.cross(n, u1)
        add_box(name, np.array([p.x_at(0., 1.), 0., 1.]), [u1, u2, n], [1.2, 1.2, thickness], colour)
    for suffix, location, rotation in (('front', (0., -12., 1.), (math.pi / 2, 0., 0.)), ('top', (0., 0., 12.), (0., 0., 0.))):
        for o in [o for o in bpy.context.scene.objects if o.type == 'CAMERA']:
            bpy.data.objects.remove(o)
        set_camera(location, rotation, 2.7, (1000, 1000))
        render_to(paths[suffix])


# ---- one module

def scan_module(module_dir, out_dir, samples=24000, render=True):
    t0 = time.time()
    module_dir, out_dir = Path(module_dir).resolve(), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((module_dir / 'reduced' / 'manifest.json').read_text(encoding='utf-8-sig'))
    module = manifest.get('module') or module_dir.name
    set_name, job, race, _ = module_identity(module_dir)
    pal = palette_from_manifest(manifest)
    inputs = resolve_inputs(module_dir, manifest)
    cameras = []
    if inputs['cameras'] and inputs['regions']:
        from face_labels import load_cameras
        cameras = load_cameras(inputs['cameras'], json.loads(inputs['regions'].read_text(encoding='utf-8-sig')), manifest.get('omit_views') or ())
    surface, source_info = load_surface(module_dir, manifest)
    t_load = time.time() - t0

    import face_labels
    part_fit, base_x, base_info = None, None, None
    if inputs['parts'] is not None and manifest.get('parts'):
        cops = manifest.get('knee_cops') or {}      # a knee-cop module is scanned on the relabelled base the reduction used
        base = face_labels.BaseParts.load(inputs['parts'], knee_cops=cops if cops.get('enabled') else None)
        fit = manifest['parts']['fit']
        scale, shift = (fit['scale'], np.asarray(fit['translation'])) if fit['used'] == 'icp' else (1., np.zeros(3))
        part_fit = base.moved(scale, shift) if fit['used'] == 'icp' else base
        base_sym = face_labels.symmetry_plane(base)
        base_info = {'parts_file': str(inputs['parts']), 'icp_scale': scale, 'icp_translation': shift, 'base_symmetry': base_sym}
        if base_sym['x'] is not None:
            base_x = scale * base_sym['x'] + shift[0]

    # plane fit on the 0.1% low-poly level (the objective needs ~10^5 nearest-surface queries; the high-poly BVH is ~5x
    # slower and the level is within 0.1% of height of it), started from the coarse x = const scan and the base-mapped plane
    fit_surface = surface
    lod_fit = load_lod(module_dir, manifest, ('lod_error_0p1.glb', 'lod_error_0p2.glb', 'lod_error_0p4.glb'))
    if lod_fit is not None:
        fit_surface = sym.Surface(lod_fit[0], lod_fit[1])
    fit_pts = fit_surface.sample(2500, seed=1)[0]
    inits = [('base_mapped', sym.Plane.from_x(base_x))] if base_x is not None else []
    plane, fit_info = sym.fit_plane(fit_surface.distances, fit_pts, inits, scan=.12, scan_step=.04)
    fit_info['surface'] = lod_fit[2] if lod_fit is not None else 'source (high-poly)'
    t_fit = time.time() - t0 - t_load

    # measurement on an independent sample set
    pts, _, _ = surface.sample(samples, seed=2)
    rec = sym.measure(surface, plane, pts, pal, cameras, part_fit)
    summary = sym.summarise(rec, part_fit.names if part_fit is not None else None, module, pal.n_garments)
    head = sym.headline(summary)
    compare_pts = surface.sample(6000, seed=3)[0]
    others = {'bbox_centre_x0': sym.Plane.from_x(0.)}
    if base_x is not None:
        others['base_mapped'] = sym.Plane.from_x(base_x)
    planes = plane_comparison(surface, plane, others, compare_pts)
    result = {'module_dir': str(module_dir), 'set': set_name, 'job': job, 'race': race, 'module': module,
              'backend': backend_of(module_dir), 'palette_scheme': pal.scheme, 'source': source_info,
              'cameras': str(inputs['cameras']) if inputs['cameras'] else None, 'omit_views': manifest.get('omit_views') or [],
              'cameras_used': len(cameras), 'base': base_info, 'input_note': inputs['note'],
              'plane': {**plane.to_dict(), 'fit': fit_info, 'comparison': planes}, 'summary': summary, 'headline': head,
              'asymmetric_regions_well_seen': summary['asymmetric_regions'], 'renders': {},
              'seconds': {'load': t_load, 'plane_fit': t_fit}}

    if render:
        lod = load_lod(module_dir, manifest)
        if lod is not None:
            lod_v, lod_f, lod_name = lod
            geo, shares = face_heat(surface, plane, lod_v, lod_f, pal, cameras, part_fit, module)
            render_front_back(lod_v, lod_f, geo_colours(geo), out_dir / 'heat_geometry.png')
            render_front_back(lod_v, lod_f, shares @ CLASS_COLOURS, out_dir / 'heat_colour.png')
            overlay_planes = [('x0', sym.Plane.from_x(0.), (.95, .85, .05)), ('fitted', plane, (.05, .62, .15))]
            if base_x is not None:
                overlay_planes.insert(1, ('base', sym.Plane.from_x(base_x), (.05, .75, .85)))
            render_overlay(lod_v, lod_f, plane, overlay_planes, {'front': out_dir / 'plane_overlay_front.png',
                                                                  'top': out_dir / 'plane_overlay_top.png'})
            result['renders'] = {'lod': lod_name, 'files': ['heat_geometry.png', 'heat_colour.png', 'plane_overlay_front.png',
                                                           'plane_overlay_top.png'],
                                 'geometry_scale': 'blue 0, cyan 0.5, green 1, yellow 2, red >= 4 (% of height)',
                                 'colour_legend': {'grey-green': 'partners agree', 'red': 'disagree, both sides well seen',
                                                   'orange': 'disagree, one side poorly seen', 'dark grey': 'not compared',
                                                   'blue': 'disagree in a region asymmetric by design'}}
    result['seconds']['total'] = time.time() - t0
    dump(out_dir / 'symmetry.json', result)
    print('SYMMETRY_SCAN', module_dir.relative_to(ROOT) if module_dir.is_relative_to(ROOT) else module_dir,
          json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in head.items() if k != 'flagged_families'}),
          f"plane_shift={planes['bbox_centre_x0']['x_mid_height_minus_fitted_pct_height']:.2f}%", f"{result['seconds']['total']:.0f}s",
          flush=True)
    return result


# ================================================================= batch part

def discover(roots):
    mods = []
    for root in roots:
        root = Path(root).resolve()
        if (root / 'reduced' / 'manifest.json').is_file():
            mods.append(root)
        else:
            # root = models/<set> (job/race/module below), models/<set>/<job> or models/<set>/<job>/<race>
            for pattern in ('*/*/*/reduced/manifest.json', '*/*/reduced/manifest.json', '*/reduced/manifest.json'):
                found = sorted(m.parent.parent for m in root.glob(pattern))
                if found:
                    mods.extend(found)
                    break
    seen, out = set(), []
    for m in mods:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return [m for m in out if reduction_finished(m)]


def reduction_finished(module_dir):
    """manifest.json is rewritten after every level, so a module still being reduced has fewer than three levels (or a
    half-written file): not scanned yet."""
    try:
        manifest = json.loads((Path(module_dir) / 'reduced' / 'manifest.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return False
    return len(manifest.get('levels') or {}) >= 3 and (Path(module_dir) / 'source.glb').is_file() \
        and (Path(module_dir) / 'reduced' / 'review.blend').is_file()


def stratify(mods, per_set):
    """About `per_set` modules of every set, round-robin over (race, module) so every race and module is covered."""
    by_set = {}
    for m in mods:
        by_set.setdefault(module_identity(m)[0], []).append(m)
    keep = []
    for items in by_set.values():
        cells = {}
        for m in items:
            _, _, race, mod = module_identity(m)
            cells.setdefault((race, mod), []).append(m)
        order = sorted(cells, key=lambda c: (c[1] != 'body_armor', c[0]))
        pick, i = [], 0
        while len(pick) < min(per_set, len(items)):
            pick.append(cells[order[i % len(order)]][i // len(order)] if i // len(order) < len(cells[order[i % len(order)]]) else None)
            i += 1
            if i > 4 * len(items):
                break
        keep.extend(p for p in pick if p is not None)
    return keep


def out_dir_of(out_root, module_dir):
    s, j, r, m = module_identity(module_dir)
    return Path(out_root) / s / j / r / m


def run_blender(module_dir, out_dir, args):
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ['nice', '-n', '10', args.blender, '--factory-startup', '-b', '-t', str(args.threads), '--python', str(Path(__file__).resolve()), '--',
           '--module-dir', str(module_dir), '--out-dir', str(out_dir), '--samples', str(args.samples)]
    if args.no_render:
        cmd.append('--no-render')
    env = dict(os.environ, OMP_NUM_THREADS=str(args.threads), OPENBLAS_NUM_THREADS=str(args.threads),
               MKL_NUM_THREADS=str(args.threads))
    t = time.time()
    with open(out_dir / 'scan.log', 'w') as log:
        try:
            proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=args.timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code = -9
    return code, time.time() - t


def collect_rows(out_root):
    rows = []
    for path in sorted(Path(out_root).glob('*/*/*/*/symmetry.json')):
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        s = data['summary']
        cmp_ = data['plane']['comparison']
        x0 = cmp_['bbox_centre_x0']
        base = cmp_.get('base_mapped')
        row = {'set': data['set'], 'job': data['job'], 'race': data['race'], 'module': data['module'],
               'backend': data['backend'], 'dir': str(path.parent),
               'plane_tilt_deg': data['plane']['tilt_deg'], 'plane_yaw_deg': data['plane']['yaw_deg'],
               'plane_pitch_deg': data['plane']['pitch_deg'],
               'plane_x_mid_pct_height': data['plane']['x_at_mid_height'] * sym.PCT,
               'plane_residual_p50_pct': cmp_['fitted']['p50_pct_height'], 'plane_residual_p95_pct': cmp_['fitted']['p95_pct_height'],
               'x0_residual_p95_pct': x0['p95_pct_height'],
               'x0_mirror_displacement_mean_pct': x0['mirror_displacement_vs_fitted_pct_height']['mean'],
               'x0_mirror_displacement_p95_pct': x0['mirror_displacement_vs_fitted_pct_height']['p95'],
               'x0_shift_mid_pct': -x0['x_mid_height_minus_fitted_pct_height'],
               'base_mirror_displacement_mean_pct': base['mirror_displacement_vs_fitted_pct_height']['mean'] if base else None,
               'base_shift_mid_pct': -base['x_mid_height_minus_fitted_pct_height'] if base else None,
               'base_residual_p95_pct': base['p95_pct_height'] if base else None,
               **data['headline'], 'asymmetric_regions': data['asymmetric_regions_well_seen'],
               'families': {fam: {'flags': st['flags'], 'expected': st['expected_asymmetric'], 'samples': st['samples'],
                                  'geo_seen_p95_pct': (st.get('geo_seen_pct_height') or {}).get('p95'),
                                  'geo_p95_pct': (st.get('geo_pct_height') or {}).get('p95'),
                                  'colour_well_seen_disagree': (st.get('colour') or {}).get('well_seen_disagree'),
                                  'colour_agree': (st.get('colour') or {}).get('agree'),
                                  'colour_garment_agree': (st.get('colour') or {}).get('garment_agree')}
                            for fam, st in s.get('by_family', {}).items()},
               'cells': s.get('asymmetric_cells', []), 'seconds': data['seconds']['total']}
        row['asymmetry_score'] = sym.asymmetry_score(row)
        rows.append(row)
    return rows


def stats(values):
    v = np.array([x for x in values if x is not None and math.isfinite(x)], float)
    if not len(v):
        return None
    return {'n': int(len(v)), 'mean': float(v.mean()), 'median': float(np.median(v)), 'p10': float(np.quantile(v, .1)),
            'p90': float(np.quantile(v, .9)), 'min': float(v.min()), 'max': float(v.max())}


AGG_KEYS = ('geo_p50_pct', 'geo_p95_pct', 'geo_seen_p95_pct', 'normal_agree', 'colour_agree', 'colour_garment_agree',
            'colour_well_seen_disagree', 'colour_well_seen_garment_disagree', 'plane_tilt_deg', 'plane_residual_p95_pct',
            'x0_shift_mid_pct', 'x0_mirror_displacement_mean_pct', 'x0_mirror_displacement_p95_pct',
            'base_mirror_displacement_mean_pct', 'expected_asym_geo_p95_pct', 'expected_asym_garment_agree', 'asymmetry_score')


GARMENT_KEYS = ('colour_garment_agree', 'colour_well_seen_garment_disagree', 'expected_asym_garment_agree')
GARMENT_MIN = 300     # compared garment samples a model needs before its garment-only shares enter an aggregate


def aggregate(rows, key):
    groups = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)

    def value(r, k):
        if k in GARMENT_KEYS and (r.get('colour_garment_compared') or 0) < GARMENT_MIN:
            return None
        return r.get(k)
    return {str(g): {'models': len(items), **{k: stats([value(r, k) for r in items]) for k in AGG_KEYS}}
            for g, items in sorted(groups.items(), key=lambda kv: str(kv[0]))}


def region_frequency(rows):
    out = {}
    for module in sorted({r['module'] for r in rows}):
        items = [r for r in rows if r['module'] == module]
        fam = {}
        for r in items:
            for name, st in r['families'].items():
                e = fam.setdefault(name, {'models': 0, 'flagged': 0, 'flagged_colour': 0, 'flagged_geometry': 0, 'expected': st['expected'],
                                          'geo_seen_p95': [], 'colour_dis': []})
                e['models'] += 1
                e['flagged'] += bool(st['flags'])
                e['flagged_colour'] += 'colour' in st['flags']
                e['flagged_geometry'] += 'geometry' in st['flags']
                e['geo_seen_p95'].append(st['geo_seen_p95_pct'])
                e['colour_dis'].append(st['colour_well_seen_disagree'])
        cells = {}
        for r in items:
            for c in r['cells']:
                k = f"|x| {c['cell']['abs_x'][0]:.1f}-{c['cell']['abs_x'][1]:.1f}, z {c['cell']['z'][0]:.1f}-{c['cell']['z'][1]:.1f}"
                e = cells.setdefault(k, {'colour': 0, 'geometry': 0})
                for f in c['flags']:
                    e[f] += 1
        out[module] = {
            'models': len(items),
            'families': {name: {**{k: v for k, v in e.items() if k not in ('geo_seen_p95', 'colour_dis')},
                                'share_flagged': e['flagged'] / e['models'],
                                'median_geo_seen_p95_pct': stats(e['geo_seen_p95']) and stats(e['geo_seen_p95'])['median'],
                                'median_colour_well_seen_disagree': stats(e['colour_dis']) and stats(e['colour_dis'])['median']}
                         for name, e in sorted(fam.items(), key=lambda kv: -kv[1]['flagged'])},
            'cells_flagged_in_n_models': dict(sorted(cells.items(), key=lambda kv: -(kv[1]['colour'] + kv[1]['geometry']))[:25])}
    return out


def write_summary(out_root, rows):
    out_root = Path(out_root)
    shifts = {'all': rows}
    plane = {}
    for name, items in (('all', rows), *[(f'module={m}', [r for r in rows if r['module'] == m]) for m in sorted({r['module'] for r in rows})],
                        *[(f'backend={b}', [r for r in rows if r['backend'] == b]) for b in sorted({r['backend'] for r in rows})]):
        plane[name] = {'models': len(items),
                       'fitted_x_minus_bbox_centre_x0_mid_height_pct_height_abs': stats([abs(r['x0_shift_mid_pct']) for r in items]),
                       'fitted_x_minus_bbox_centre_x0_mid_height_pct_height_signed': stats([-r['x0_shift_mid_pct'] for r in items]),
                       'tilt_deg': stats([r['plane_tilt_deg'] for r in items]),
                       'x0_mirror_displacement_mean_pct_height': stats([r['x0_mirror_displacement_mean_pct'] for r in items]),
                       'x0_mirror_displacement_p95_pct_height': stats([r['x0_mirror_displacement_p95_pct'] for r in items]),
                       'base_mapped_mirror_displacement_mean_pct_height': stats([r['base_mirror_displacement_mean_pct'] for r in items]),
                       'fitted_residual_p95_pct_height': stats([r['plane_residual_p95_pct'] for r in items]),
                       'x0_residual_p95_pct_height': stats([r['x0_residual_p95_pct'] for r in items])}
    summary = {
        'generated': time.strftime('%Y-%m-%d %H:%M:%S'), 'models': len(rows),
        'score': 'asymmetry_score = geo_p95_pct + 20 x (1 - colour_garment_agree); lower = more symmetric',
        'plane_vs_pipeline': plane,
        'by_module': aggregate(rows, lambda r: r['module']), 'by_backend': aggregate(rows, lambda r: r['backend']),
        'by_module_backend': aggregate(rows, lambda r: f"{r['module']}/{r['backend']}"),
        'by_race': aggregate(rows, lambda r: r['race']), 'by_module_race': aggregate(rows, lambda r: f"{r['module']}/{r['race']}"),
        'by_set': aggregate(rows, lambda r: r['set']), 'regions': region_frequency(rows),
        'modules': [{k: v for k, v in r.items() if k not in ('families', 'cells')} for r in rows]}
    dump(out_root / 'summary.json', summary)
    cols = ['set', 'job', 'race', 'module', 'backend', 'asymmetry_score', 'geo_p50_pct', 'geo_p95_pct', 'geo_seen_p95_pct', 'normal_agree',
            'matched_share', 'seen_both_share', 'colour_agree', 'colour_garment_agree', 'colour_garment_compared',
            'colour_well_seen_disagree', 'colour_well_seen_garment_disagree', 'expected_asym_share', 'expected_asym_geo_p95_pct',
            'expected_asym_colour_agree', 'expected_asym_garment_agree', 'plane_tilt_deg', 'plane_yaw_deg', 'plane_pitch_deg',
            'plane_x_mid_pct_height', 'plane_residual_p50_pct', 'plane_residual_p95_pct', 'x0_residual_p95_pct', 'x0_shift_mid_pct',
            'x0_mirror_displacement_mean_pct', 'x0_mirror_displacement_p95_pct', 'base_shift_mid_pct',
            'base_mirror_displacement_mean_pct', 'base_residual_p95_pct', 'flagged_families', 'seconds', 'dir']
    with open(out_root / 'summary.csv', 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([(';'.join(r[c]) if isinstance(r[c], list) else ('' if r.get(c) is None else (round(r[c], 4) if isinstance(r[c], float) else r[c])))
                        for c in cols])
    return summary


def contact_sheets(out_root, rows, count=8):
    """Sheets of the `count` most and least symmetric body_armor models: heat_colour / heat_geometry, front and back halves."""
    out_root = Path(out_root)
    armor = sorted((r for r in rows if r['module'] == 'body_armor' and (Path(r['dir']) / 'heat_colour.png').is_file()),
                   key=lambda r: r['asymmetry_score'])
    if len(armor) < 2:
        return []
    chosen = armor[:count] + armor[-count:] if len(armor) > count else armor
    chosen = list(dict.fromkeys(map(id, chosen)))
    chosen = [r for r in armor if id(r) in chosen]
    links = out_root / 'contact_sheets' / '_links'
    links.mkdir(parents=True, exist_ok=True)
    made = []
    contact = ROOT / 'tools/contact_sheet.py'
    for metric in ('heat_colour', 'heat_geometry'):
        for half, crop in (('front', '0,0,900,900'), ('back', '900,0,1800,900')):
            files = []
            for rank, r in enumerate(chosen):
                end = 'most' if r in armor[:count] else 'least'
                label = f"{end[:1].upper()}{rank + 1:02d} {r['set'].split('_')[0]}{r['set'].split('_')[1]} {r['race']} {r['backend'][:3]} {r['asymmetry_score']:.1f}"
                link = links / metric.replace('heat_', '') / half / f"{label.replace(' ', '_')}.png"
                link.parent.mkdir(parents=True, exist_ok=True)
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(Path(r['dir']) / f'{metric}.png')
                files.append(str(link))
            dest = out_root / 'contact_sheets' / f'body_armor_{metric}_{half}.jpg'
            subprocess.run([sys.executable, str(contact), str(dest), '--title',
                            f'body_armor {metric.replace("heat_", "")} ({half}): top {min(count, len(armor))} most, bottom {min(count, len(armor))} least symmetric',
                            '--cols', '4', '--tile', '460', '--crop', crop, *files], check=True, stdout=subprocess.DEVNULL)
            made.append(str(dest))
    return made


def batch(args):
    out_root = Path(args.out).resolve()
    mods = discover(args.roots)
    if args.stratify:
        mods = stratify(mods, args.stratify)
    if args.limit:
        mods = mods[:args.limit]
    print(f'{len(mods)} modules with reduced/manifest.json', flush=True)
    if args.dry_run:
        for m in mods:
            print(' ', out_dir_of(out_root, m))
        return
    if not args.summary_only:
        t0 = time.time()
        done = 0
        for i, m in enumerate(mods):
            od = out_dir_of(out_root, m)
            if (od / 'symmetry.json').is_file() and not args.force:
                print(f'[{i + 1}/{len(mods)}] skip (done) {od.relative_to(out_root)}', flush=True)
                continue
            code, secs = run_blender(m, od, args)
            ok = (od / 'symmetry.json').is_file() and code == 0
            done += 1
            print(f'[{i + 1}/{len(mods)}] {"ok " if ok else "FAIL"} {secs:5.0f}s  {od.relative_to(out_root)}'
                  f'  (elapsed {(time.time() - t0) / 60:.1f} min)', flush=True)
            if not ok:
                print(f'    see {od / "scan.log"}', flush=True)
    rows = collect_rows(out_root)
    if rows:
        write_summary(out_root, rows)
        for p in contact_sheets(out_root, rows):
            print('contact sheet', p)
        print(f'summary: {out_root / "summary.json"} ({len(rows)} models)')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('roots', nargs='+', help='models/<set> folders or module folders')
    ap.add_argument('--out', default=str(DEFAULT_OUT))
    ap.add_argument('--blender', default=BLENDER)
    ap.add_argument('--threads', type=int, default=2)
    ap.add_argument('--samples', type=int, default=24000)
    ap.add_argument('--timeout', type=int, default=1800)
    ap.add_argument('--no-render', action='store_true')
    ap.add_argument('--force', action='store_true', help='redo modules that already have symmetry.json')
    ap.add_argument('--stratify', type=int, default=0, help='about N modules per set covering every race and module')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--summary-only', action='store_true', help='only rebuild summary / contact sheets from existing symmetry.json')
    ap.add_argument('--dry-run', action='store_true')
    batch(ap.parse_args())


def blender_main():
    argv = sys.argv[sys.argv.index('--') + 1:]
    ap = argparse.ArgumentParser()
    ap.add_argument('--module-dir', required=True)
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--samples', type=int, default=24000)
    ap.add_argument('--no-render', action='store_true')
    a = ap.parse_args(argv)
    scan_module(a.module_dir, a.out_dir, a.samples, not a.no_render)


if __name__ == '__main__':
    if bpy is not None:
        blender_main()
    else:
        main()
