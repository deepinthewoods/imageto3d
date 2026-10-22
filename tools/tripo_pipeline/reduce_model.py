"""Blender background processor for one Tripo Studio GLB.

Usage: blender -b -t 6 --python tools/tripo_pipeline/reduce_model.py -- source.glb output_dir [swatches.json]
Reduces whole geometry at 0.4/0.2/0.1% height, then samples source texture
and assigns simple colors to whole faces. No fixed polygon targets.
With a base `parts.npz` (base_parts.py; found next to the resolved regions.json, or --parts) the
base body parts are fitted to the mesh (trimmed ICP) and transferred onto every level as the FACE
attribute `body_part`; they also drive the module prior. `--no-parts` turns that off.
Sources whose area is mostly enclosed (an inner shell welded to the outer skin) are stripped of those
faces before reduction (`--interior auto|strip|flag`); the manifest records the `interior` audit.
"""
from pathlib import Path
import hashlib
import json
import math
import sys
import time

import bpy
import bmesh
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / 'tools/lowpoly'
sys.path.insert(0, str(LAB))
import lowpoly_lab as lab
import polygon_materials05 as texture_tools
import tripo_adaptive_study as adaptive
import tripo_reduce as tripo
from adaptive_cleanup04 import cancel_pairs
from adaptive_lod04 import compact, measure

sys.path.insert(0, str(Path(__file__).resolve().parent))
import face_labels  # noqa: E402

LEVELS = [('0p4', .004), ('0p2', .002), ('0p1', .001)]
# Review when more of the surface than this was decided by prior/neighbours rather than seen texture.
LOW_EVIDENCE_REVIEW = .2
ROLES = ('base', 'primary', 'secondary', 'uncertain')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def swatch_rgb(value):
    if not isinstance(value, str) or len(value) != 7 or not value.startswith('#'):
        raise ValueError('Swatches must be #RRGGBB strings')
    # Blender image.pixels for this imported Tripo JPG tracks encoded RGB values.
    # Compare chromatic ratios in that same space; no one-sided gamma transform.
    return np.array([int(value[i:i+2], 16) for i in (1, 3, 5)], float) / 255


def chroma(rgb):
    return rgb / np.maximum(rgb.sum(axis=-1, keepdims=True), 1e-9)


def cluster_face_colors(means, area, swatches, reference_palette=None):
    """Classify a face from its averaged source color; keep ambiguous faces visible."""
    x = chroma(means)
    if swatches is not None:
        marker_colors = [swatches['base'], *swatches.get('base_markers', {}).values()]
        centers = np.array([swatch_rgb(value) for value in
                            [*marker_colors, swatches['primary'], swatches['secondary']]])
        dist = np.linalg.norm(x[:, None] - chroma(centers)[None], axis=-1)
        nearest = dist.argmin(axis=1)
        ordered = np.sort(dist, axis=1)
        uncertain = (ordered[:, 0] > .13) | ((ordered[:, 1] - ordered[:, 0]) < .025)
        marker_count = len(marker_colors)
        labels = np.where(nearest < marker_count, 0, nearest - marker_count + 1)
        labels[uncertain] = 3
        palette = np.vstack([swatch_rgb(swatches[name]) for name in ROLES[:3]] +
                            [np.array([1., .4, .03])])
        return labels, palette, {'mode': 'recorded_swatches', 'swatches': swatches,
                                  'base_marker_count': marker_count,
                                  'uncertain_faces': int(uncertain.sum())}
    if reference_palette is not None:
        ids = ((x[:, None] - chroma(reference_palette)) ** 2).sum(axis=-1).argmin(axis=1)
        return ids, reference_palette, {'mode': 'unlabeled_reference_clusters',
                                         'note': 'Colors and IDs follow the 0.4% baseline; semantic roles need concept swatches.'}
    # No semantic swatches were recorded for older examples. Keep labels neutral.
    sample = x
    centers = [sample[np.argmax(area)]]
    distance = np.full(len(sample), np.inf)
    for _ in range(2):
        distance = np.minimum(distance, ((sample - centers[-1]) ** 2).sum(axis=1))
        centers.append(sample[distance.argmax()])
    centers = np.asarray(centers)
    for _ in range(25):
        group = ((sample[:, None] - centers) ** 2).sum(axis=-1).argmin(axis=1)
        updated = np.array([np.average(sample[group == i], axis=0, weights=area[group == i])
                            if np.any(group == i) else centers[i] for i in range(3)])
        if np.max(np.abs(updated - centers)) < 1e-6: break
        centers = updated
    ids = ((sample[:, None] - centers) ** 2).sum(axis=-1).argmin(axis=1)
    colors = np.array([np.average(means[ids == i], axis=0, weights=area[ids == i])
                       if np.any(ids == i) else np.array([.5, .5, .5]) for i in range(3)])
    return ids, colors, {'mode': 'unlabeled_three_color_clusters',
                         'note': 'COLOR_00/01/02 are visual groups only; supply concept swatches for semantic roles.'}


def classify_samples(colors, swatches):
    """Role per texture sample (0 base, 1 primary, 2 secondary, 3 unusable); see face_labels."""
    return face_labels.classify_samples(colors, swatches)[0]


def assign_materials(obj, labels, palette, semantic, cache):
    obj.data.materials.clear()
    names = list(ROLES) if semantic else [f'COLOR_{i:02}' for i in range(len(palette))]
    for name, color in zip(names, palette):
        if name not in cache:
            mat = bpy.data.materials.new(name)
            mat.use_nodes = True
            rgba = (*np.clip(color, 0, 1).tolist(), 1.)
            mat.diffuse_color = rgba
            shader = mat.node_tree.nodes.get('Principled BSDF')
            shader.inputs['Base Color'].default_value = rgba
            shader.inputs['Roughness'].default_value = .9
            cache[name] = mat
        mat = cache[name]
        obj.data.materials.append(mat)
    obj.data.polygons.foreach_set('material_index', np.asarray(labels, np.int32))


def export_native(obj, info, dest):
    copy = obj.copy()
    copy.data = obj.data.copy()
    bpy.context.scene.collection.objects.link(copy)
    co = np.empty((len(copy.data.vertices), 3), np.float32)
    copy.data.vertices.foreach_get('co', co.ravel())
    rotation = np.array(info['rotation_to_review'])
    original = (co / info['normalization_scale'] + info['normalization_origin_rotated']) @ rotation
    copy.data.vertices.foreach_set('co', original.ravel())
    copy.data.update()
    lab.active(copy)
    bpy.ops.export_scene.gltf(filepath=str(dest), use_selection=True, export_format='GLB')
    count = tripo.glb_count(dest)
    lab.drop(copy)
    return count


def part_colors(count):
    """Distinct, evenly spread colours for body part ids (golden-ratio hue steps)."""
    import colorsys
    return [colorsys.hsv_to_rgb((i * .618034) % 1, .55 + .25 * (i % 2), .95 - .2 * (i % 3) / 2) for i in range(count)]


def part_object(source, names, parts, name, location, cache):
    """Review aid: a copy of `source` with one flat material per body part (material index = part id)."""
    copy = source.copy()
    copy.data = source.data.copy()
    copy.name = name
    bpy.context.scene.collection.objects.link(copy)
    copy.data.materials.clear()
    for part_name, color in zip(names, part_colors(len(names))):
        if part_name not in cache:
            mat = bpy.data.materials.new('part_' + part_name)
            mat.diffuse_color = (*color, 1.)
            mat.use_nodes = True
            mat.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (*color, 1.)
            mat.node_tree.nodes['Principled BSDF'].inputs['Roughness'].default_value = .9
            cache[part_name] = mat
        copy.data.materials.append(cache[part_name])
    copy.data.polygons.foreach_set('material_index', np.asarray(parts, np.int32))
    copy.location = location
    return copy


def set_parts(mesh, names, parts):
    """FACE int attribute `body_part` + JSON mesh property `body_part_names` (build_marker_base.py's form)."""
    if 'body_part' in mesh.attributes:
        mesh.attributes.remove(mesh.attributes['body_part'])
    mesh.attributes.new('body_part', 'INT', 'FACE').data.foreach_set('value', np.asarray(parts, np.int32))
    mesh['body_part_names'] = json.dumps(list(names))


MODULES = ('body_armor', 'right_gauntlet_boot', 'outfit')
JOB_STATES = ('processing', 'multiview_ready', 'completed', 'failed', 'processing_views')


def default_swatches():
    palette = json.loads((Path(__file__).resolve().parent / 'config.json').read_text())['marker_palette_srgb']
    return {'base': palette['body_red'],
            'base_markers': {'head': palette['head_yellow'], 'left_hand': palette['left_hand_magenta'],
                             'left_foot': palette['left_foot_cyan']},
            'primary': palette['primary_green'], 'secondary': palette['secondary_blue']}


def resolve_inputs(source, swatch_path, cameras_path, regions_path, module, parts_path=None, use_parts=True):
    """Fill unset inputs from the standard layout models/<set>/<job-id>/<race>/<module>/source.glb.

    Cameras come from the job's frozen `.base_views/<race>/transforms.json`; for jobs frozen before
    that file was copied, from whichever active/archived base set has the same three-quarter view
    hashes as the job's frozen images (never blindly from the active base). `parts.npz` is taken from
    the folder of the resolved regions.json. Returns the resolved values and a note of where each
    came from.
    """
    notes = {}
    folder = source.parent
    if module is None and folder.name in MODULES:
        module, notes['module'] = folder.name, 'model folder name'
    if swatch_path is None:
        if (folder / 'swatches.json').is_file():
            swatch_path, notes['swatches'] = folder / 'swatches.json', 'swatches.json beside source'
        else:
            notes['swatches'] = 'config.json marker_palette_srgb'
    race = folder.parent.name
    parts = source.parts
    snapshot = None
    if len(parts) >= 6 and parts[-6] == 'models':
        set_name, job_id = parts[-5], parts[-4]
        for state in JOB_STATES:
            candidate = ROOT / state / f'{set_name}--{job_id}' / '.base_views' / race
            if candidate.is_dir():
                snapshot = candidate
                break
    if regions_path is None:
        for candidate in [snapshot, ROOT / 'base_views' / race]:
            if candidate is not None and (candidate / 'regions.json').is_file():
                regions_path, notes['regions'] = candidate / 'regions.json', str(candidate)
                break
    if not use_parts:
        parts_path, notes['parts'] = None, 'disabled (--no-parts)'
    elif parts_path is not None:
        notes['parts'] = '--parts'
    elif regions_path is not None and (Path(regions_path).parent / 'parts.npz').is_file():
        parts_path, notes['parts'] = Path(regions_path).parent / 'parts.npz', 'beside regions.json'
    else:
        notes['parts'] = 'none found; landmark prior'
    if cameras_path is None:
        if snapshot is not None and (snapshot / 'transforms.json').is_file():
            cameras_path, notes['cameras'] = snapshot / 'transforms.json', 'job snapshot'
        elif snapshot is not None:
            wanted = {n: digest(snapshot / n) for n in ('front_three_quarter.png', 'back_three_quarter.png')}
            sets = [ROOT / 'base_views' / race] + sorted(ROOT.glob(f'archive/*/{race}'))
            for candidate in sets:
                if all((candidate / n).is_file() and digest(candidate / n) == h for n, h in wanted.items()) \
                        and (candidate / 'transforms.json').is_file():
                    cameras_path = candidate / 'transforms.json'
                    notes['cameras'] = f'matched frozen view hashes: {candidate.relative_to(ROOT)}'
                    break
        if cameras_path is None:
            notes['cameras'] = 'none found; visibility weighting off'
    return swatch_path, cameras_path, regions_path, parts_path, module, notes


def canonical_order(v, f):
    """Sort vertices by position and faces by their (rotated, winding-preserving) vertex triples."""
    order = np.lexsort((v[:, 2], v[:, 1], v[:, 0]))
    rank = np.empty(len(v), np.int64)
    rank[order] = np.arange(len(v))
    f = rank[f]
    shift = f.argmin(axis=1)
    f = np.take_along_axis(f, (shift[:, None] + np.arange(3)[None]) % 3, axis=1)
    f = f[np.lexsort((f[:, 2], f[:, 1], f[:, 0]))]
    return v[order], f


def run(source, out, swatch_path=None, cameras_path=None, regions_path=None, module=None,
        parts_path=None, use_parts=True, interior_mode='auto', use_off_body=True):
    source = Path(source).resolve(); out = Path(out).resolve()
    if not source.is_file() or source.suffix.lower() != '.glb':
        raise ValueError(f'Expected a source GLB: {source}')
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f'Output directory is not empty; keep earlier attempts: {out}')
    out.mkdir(parents=True, exist_ok=True)
    swatch_path, cameras_path, regions_path, parts_path, module, input_notes = resolve_inputs(
        source, swatch_path, cameras_path, regions_path, module, parts_path, use_parts)
    if cameras_path and not regions_path:
        cameras_path, input_notes['cameras'] = None, 'found but no regions.json; visibility weighting off'
    swatches = json.loads(Path(swatch_path).read_text()) if swatch_path else default_swatches()
    if swatches is not None:
        if set(swatches) not in [set(ROLES[:3]), set(ROLES[:3]) | {'base_markers'}]:
            raise ValueError('Swatches need base, primary, secondary and optional base_markers')
        for name in ROLES[:3]: swatch_rgb(swatches[name])
        if 'base_markers' in swatches:
            if not isinstance(swatches['base_markers'], dict) or not swatches['base_markers']:
                raise ValueError('base_markers must be a nonempty name-to-hex map')
            for value in swatches['base_markers'].values(): swatch_rgb(value)
    regions = json.loads(Path(regions_path).read_text()) if regions_path else None
    if cameras_path and regions is None:
        raise ValueError('--cameras needs --regions (base frame for mapping the camera matrices)')
    if module is not None and module not in ('body_armor', 'right_gauntlet_boot', 'outfit'):
        raise ValueError(f'Unknown module: {module}')
    source_hash = digest(source)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(source), merge_vertices=False)
    objects = [obj for obj in bpy.context.scene.objects if obj.type == 'MESH']
    if len(objects) != 1:
        raise ValueError(f'Expected one Tripo mesh; found {len(objects)}')
    obj = objects[0]
    bpy.ops.file.pack_all()
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(out / 'source_import.blend'), compress=True)
    lab.active(obj)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    native, _ = lab.arrays(obj)
    yaw = 0.0  # Studio plain-paper sample faces -Y in native coordinates.
    rotation = np.asarray(Matrix.Rotation(math.radians(yaw), 3, 'Z'))
    turned = native @ rotation.T
    low, high = turned.min(axis=0), turned.max(axis=0)
    origin = (low + high) / 2
    origin[2] = low[2]
    scale = 2 / (high[2] - low[2])
    co = (turned - origin) * scale
    obj.data.vertices.foreach_set('co', co.ravel())
    obj.data.update()
    surface = texture_tools.TextureSurface(obj)
    source_texture = surface.image_name
    # Interior surface: some Tripo exports contain a second, inward-facing copy of the body welded to the
    # outer skin. Measure how much of the area looks only at other surface; above the threshold strip it
    # (the GLB itself is never changed).
    sv, sf = surface.v.astype(np.float64), surface.f.astype(np.int64)
    interior = face_labels.interior_metrics(sv, sf, surface.tree)
    interior.update(mode=interior_mode, threshold=face_labels.INTERIOR_THRESHOLD, stripped=False)
    flagged = interior['interior_area_share'] >= face_labels.INTERIOR_THRESHOLD
    removed = None
    if interior_mode == 'strip' or (interior_mode == 'auto' and flagged):
        enclosed = face_labels.enclosed_faces(sv, sf, surface.tree)
        if len(obj.data.polygons) != len(sf):
            raise ValueError('Cannot strip interior surface: source is not a pure triangle mesh')
        _, shell_area = face_labels.face_normals(sv, sf)
        removed = {'v': sv, 'f': sf[enclosed]}
        interior.update(stripped=True, removed_faces=int(enclosed.sum()),
                        removed_area_share=float(shell_area[enclosed].sum() / shell_area.sum()),
                        source_faces=len(sf))
        surface.f, surface.uv = surface.f[~enclosed], surface.uv[~enclosed]   # texture lookups ignore it too
        surface.tree = BVHTree.FromPolygons(surface.v.tolist(), surface.f.tolist(), all_triangles=True)
    bm = bmesh.new(); bm.from_mesh(obj.data)
    if removed is not None:
        bm.faces.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[bm.faces[i] for i in np.flatnonzero(enclosed)], context='FACES')
        bmesh.ops.delete(bm, geom=[x for x in bm.verts if not x.link_faces], context='VERTS')
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=2e-7)
    bm.to_mesh(obj.data); bm.free(); obj.data.update()
    obj.name = 'source'
    rv, rf = lab.arrays(obj)
    np.savez_compressed(out / 'source_geometry.npz', v=rv, f=rf)
    review_reasons = []
    if removed is not None:
        np.savez_compressed(out / 'interior_removed.npz', **removed)
        after = face_labels.interior_metrics(rv, rf)
        interior['after_strip'] = after
        review_reasons.append(f"interior surface: {interior['interior_area_share']:.0%} of the source area was enclosed; "
                              f"stripped {interior['removed_faces']} faces ({interior['removed_area_share']:.0%} of area)")
        if after['interior_area_share'] >= face_labels.INTERIOR_THRESHOLD:
            review_reasons.append(f"interior surface still {after['interior_area_share']:.0%} after stripping")
    elif flagged:
        review_reasons.append(f"interior surface: {interior['interior_area_share']:.0%} of the source area is enclosed "
                              "(inner shell / duplicated body); not stripped (--interior flag)")
    source_stats = lab.stats(obj)
    cleaned_v, cleaned_f, cleanup = adaptive.local_repair(rv, rf)
    # local_repair walks Python sets of BMesh elements, so its output order changes from run to run
    # and the order-sensitive simplifier then differs; a canonical order makes runs reproducible.
    cleaned_v, cleaned_f = canonical_order(cleaned_v, cleaned_f)
    tree = BVHTree.FromPolygons(rv.tolist(), rf.tolist(), all_triangles=True)
    refpoints, _ = lab.sample_surface(rv, rf, 10000, seed=940)
    base_parts = part_fit = parts_info = None
    if parts_path:
        # bare-mesh clearance per part (base_clearance.py): beside the parts file, else the active base's
        # (job snapshots made before it existed lack the file)
        clearance_path = next((c for c in (Path(parts_path).with_name('clearance.json'),
                                           ROOT / 'base_views' / source.parent.parent.name / 'clearance.json')
                               if c.is_file()), None)
        if not use_off_body:
            clearance_path = None
        input_notes['clearance'] = str(clearance_path) if clearance_path else 'none; no off-body rule'
        base_parts = face_labels.BaseParts.load(parts_path, clearance_path)
        scale_fit, shift_fit, fit_info = face_labels.fit_base(base_parts, rv, rf, tree=tree)
        part_fit = base_parts.moved(scale_fit, shift_fit) if fit_info['used'] == 'icp' else base_parts
        parts_info = {'file': str(Path(parts_path).resolve()), 'sha256': digest(parts_path),
                      'base_scene': str(np.load(parts_path)['source_scene']), 'names': part_fit.names,
                      'fit': fit_info, 'prior': 'parts' if swatches is not None else None,
                      'clearance': str(clearance_path) if clearance_path else None}
        print('PARTS_FIT', json.dumps(fit_info), flush=True)
    info = {'source': str(source), 'source_sha256': source_hash,
            'source_texture': source_texture, 'source_stats': source_stats,
            'source_audit': tripo.audit(rv, rf), 'cleanup': cleanup,
            'normalization_origin_rotated': origin.tolist(),
            'normalization_scale': float(scale), 'rotation_to_review': rotation.tolist(),
            'target_polygon_count': None,
            'material_order': 'reduce whole geometry, then label faces from texture hue weighted by '
                              'pixel quality and concept-view visibility, with module prior, mirror '
                              'face and crease-aware neighbours (face_labels.py)',
            'module': module, 'input_sources': input_notes,
            'cameras': str(cameras_path) if cameras_path else None,
            'regions': str(regions_path) if regions_path else None,
            'parts': parts_info, 'interior': interior, 'source_review_reasons': review_reasons,
            'levels': {}}
    if swatches is not None:
        # Which parts of the high-poly surface did any concept view actually see?
        hv, hf = cleaned_v, cleaned_f
        hi_normals, _ = face_labels.face_normals(hv, hf)
        hi_tree = BVHTree.FromPolygons(hv.tolist(), hf.tolist(), all_triangles=True)
        cameras = face_labels.load_cameras(cameras_path, regions) if cameras_path else []
        hi_cos = face_labels.visibility(hv[hf].mean(1), hi_normals, hi_tree, cameras) if cameras else \
            np.ones(len(hf))
        hi_vis = face_labels.visibility_weight(hi_cos, bool(cameras))
        hi_area = face_labels.face_normals(hv, hf)[1]
        info['highpoly_unseen_area_share'] = \
            float(hi_area[hi_cos <= 0].sum() / hi_area.sum()) if cameras else None
    material_cache, part_material_cache = {}, {}
    reduced_x = {'0p4': 0, '0p2': 2.8, '0p1': 5.6}
    reference_palette = None
    for label, fraction in LEVELS:
        start = time.monotonic()
        v, f, internal = adaptive.reduce_error(cleaned_v, cleaned_f, fraction, weight=0)
        generator_seconds = time.monotonic() - start
        keep = cancel_pairs(v, f)
        v, f = compact(v, f[keep])
        reduced = lab.make_obj('lod_error_' + label, v, f)
        audit = tripo.audit(v, f)
        stats = lab.stats(reduced)
        valid = (audit['finite_and_valid_indices'] and
                 all(audit[key] == 0 for key in ['boundary_edges', 'nonmanifold_edges',
                     'inconsistent_winding_edges', 'degenerate_triangles', 'duplicate_triangles'])
                 and stats['face_components'] == source_stats['face_components'])
        score = measure(v, f, tree, refpoints)
        points, owner, area, counts = texture_tools.polygon_samples(reduced.data,
                                                                     density=80000, minimum=24, maximum=384)
        colors, projection = surface.sample(points)
        totals = np.bincount(owner, minlength=len(reduced.data.polygons))
        means = np.stack([np.bincount(owner, weights=colors[:, i], minlength=len(totals))
                          for i in range(3)], axis=1) / np.maximum(totals[:, None], 1)
        role_share = None
        level_parts = None if part_fit is None else face_labels.transfer_parts(v, f, part_fit)
        if swatches is not None:
            sample_role, sample_weight = face_labels.classify_samples(colors, swatches)
            nearest = np.array([hi_tree.find_nearest(Vector(p))[2] for p in points], int)
            sample_weight = sample_weight * hi_vis[nearest]
            cent = v[f].mean(1)
            zones = None
            if part_fit is not None:
                zones = face_labels.part_zones(
                    part_fit.membership(cent, face_labels.face_normals(v, f)[0]), part_fit.names)
            prior = face_labels.wear_prior(cent, module, regions, zones)
            # a face clearly outside the fitted base body cannot be bare base (only where the module
            # lets the garment be; head/hands/feet etc. are exempt through the prior)
            off = face_labels.off_body_excess(v, f, level_parts, part_fit, prior) \
                if part_fit is not None and module is not None else None
            labels, label_source, evidence_arrays = face_labels.resolve_faces(
                v, f, owner, sample_role, sample_weight, prior,
                module in face_labels.SYMMETRIC_MODULES, None if off is None else off[0])
            riser = None
            if off is not None:
                labels, label_source, riser = face_labels.hem_risers(v, f, labels, label_source, part_fit, prior)
            role_share = evidence_arrays['vote_share']
            palette = np.vstack([swatch_rgb(swatches[name]) for name in ROLES[:3]] +
                                [np.array([1., .4, .03])])
            face_area = face_labels.face_normals(v, f)[1]
            by_source = {name: float(face_area[label_source == i].sum() / face_area.sum())
                         for i, name in enumerate(face_labels.SOURCES)}
            color_info = {'mode': 'hue_visibility_prior', 'swatches': swatches, 'module': module,
                          'prior_source': 'parts' if zones is not None else
                                          'landmarks' if regions is not None else 'neutral',
                          'symmetric': module in face_labels.SYMMETRIC_MODULES,
                          'area_share_by_source': by_source,
                          'faces_by_source': {name: int((label_source == i).sum())
                                              for i, name in enumerate(face_labels.SOURCES)},
                          'low_evidence_area_share': by_source['prior'] + by_source['neighbours'],
                          'off_body': None if off is None else {
                              'faces_beyond_threshold': int((off[0] > 0).sum()),
                              'faces_flipped_from_base': int((label_source == face_labels.SOURCES.index('off_body')).sum()),
                              'area_share_flipped': by_source['off_body'],
                              'riser_faces': int(riser.sum()), 'riser_area_share': by_source['riser']},
                          'uncertain_faces': 0}
            attr = reduced.data.attributes.new('label_source', 'INT', 'FACE')
            attr.data.foreach_set('value', label_source.astype(np.int32))
        else:
            labels, palette, color_info = cluster_face_colors(means, area, swatches, reference_palette)
        if reference_palette is None:
            reference_palette = palette.copy()
        assign_materials(reduced, labels, palette, swatches is not None, material_cache)
        parts_extra = parts_summary = None
        if part_fit is not None:
            face_area = face_labels.face_normals(v, f)[1]
            set_parts(reduced.data, part_fit.names, level_parts)
            part_object(reduced, part_fit.names, level_parts, reduced.name + '_parts',
                        (reduced_x[label], 0, -2.4), part_material_cache)
            # glTF has no face attributes and the exporter groups faces by material slot (stable), so
            # the sidecar also carries the parts in each GLB's face order.
            glb_order = np.argsort(labels, kind='stable')
            keep = np.flatnonzero(labels != 0)
            wearable_order = keep[np.argsort(labels[keep], kind='stable')]
            parts_extra = {'body_part': level_parts, 'body_part_names': np.array(part_fit.names),
                           'glb_face_order': glb_order, 'body_part_glb_order': level_parts[glb_order],
                           'wearable_face_order': wearable_order,
                           'wearable_body_part_glb_order': level_parts[wearable_order]}
            parts_summary = {'area_share': face_labels.part_area_shares(level_parts, face_area, part_fit.names),
                             'wearable_area_share': face_labels.part_area_shares(
                                 level_parts[keep], face_area[keep], part_fit.names)}
        evidence = out / (reduced.name + '_colors.npz')
        np.savez_compressed(evidence, face_mean_source_rgb=means, face_area=area,
                            material_id=labels, material_palette_source_rgb=palette,
                            texture_sample_count=totals,
                            **({'face_role_vote_share': role_share,
                                'face_confidence': evidence_arrays['confidence'],
                                'face_wear_prior': evidence_arrays['wear_prior'],
                                'face_label_source': label_source,
                                'label_source_names': np.array(face_labels.SOURCES),
                                **({'face_off_body_distance': off[1], 'face_off_body_threshold': off[2]}
                                   if off is not None else {})}
                               if role_share is not None else {}),
                            **(parts_extra or {}))
        native_glb = out / (reduced.name + '.glb')
        count = export_native(reduced, info, native_glb)
        valid &= count == len(f)
        wearable_glb = None
        wearable_triangles = None
        if swatches is not None:
            # Reduce the full reconstruction first; cut bare body faces only
            # after assigning whole-face material roles.
            wearable = reduced.copy()
            wearable.data = reduced.data.copy()
            bpy.context.scene.collection.objects.link(wearable)
            wearable.name = reduced.name + '__wearable'
            mesh = bmesh.new(); mesh.from_mesh(wearable.data)
            bmesh.ops.delete(mesh, geom=[face for face in mesh.faces if face.material_index == 0],
                             context='FACES')
            loose = [vert for vert in mesh.verts if not vert.link_faces]
            if loose:
                bmesh.ops.delete(mesh, geom=loose, context='VERTS')
            mesh.to_mesh(wearable.data); mesh.free(); wearable.data.update()
            if len(wearable.data.polygons):
                wearable_glb = out / (wearable.name + '.glb')
                wearable_triangles = export_native(wearable, info, wearable_glb)
            lab.drop(wearable)
        uncertain_share = color_info.get('uncertain_faces', 0) / max(len(f), 1)
        confident_wearable_faces = int(np.count_nonzero((labels == 1) | (labels == 2)))
        low_evidence = color_info.get('low_evidence_area_share', 0)
        material_review = (bool(review_reasons) or swatches is None or uncertain_share > .1 or low_evidence > LOW_EVIDENCE_REVIEW or
                           float(np.percentile(projection, 95)) > .01 or confident_wearable_faces == 0)
        reduced['source_sha256'] = source_hash
        reduced['error_fraction_of_height'] = fraction
        reduced['mesh_checks_pass'] = bool(valid)
        reduced['material_mode'] = color_info['mode']
        reduced.location.x = reduced_x[label]
        info['levels'][label] = {'error_fraction_of_height': fraction, 'triangles': len(f),
                                 'generator_seconds': generator_seconds, 'total_level_seconds': time.monotonic()-start,
                                 'internal_quadric_error': internal, 'mesh_checks_pass': bool(valid),
                                 'audit': audit, 'components': stats['face_components'],
                                 'surface': score, 'texture_projection_p95': float(np.percentile(projection, 95)),
                                 'texture_samples': len(points), 'material': color_info,
                                 'material_review_required': bool(material_review),
                                 'color_evidence': str(evidence),
                                 'body_parts': parts_summary,
                                 'material_colors_source_rgb': palette.tolist(),
                                 'material_face_counts': np.bincount(labels, minlength=len(palette)).tolist(),
                                 'native_glb': str(native_glb), 'serialized_triangles': count,
                                 'wearable_glb': str(wearable_glb) if wearable_glb else None,
                                 'wearable_triangles': wearable_triangles,
                                 'wearable_is_open_surface': wearable_glb is not None}
        dump(out / 'manifest.json', info)
        print('TRIPO_LOD', label, len(f), 'pass' if valid else 'REVIEW_REQUIRED', flush=True)
    assert digest(source) == source_hash
    obj.location.x = -2.8
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            sp = area.spaces.active
            sp.shading.type = 'SOLID'; sp.shading.color_type = 'MATERIAL'
            sp.region_3d.view_rotation = Vector((0, 1, 0)).to_track_quat('-Z', 'Y')
            sp.region_3d.view_perspective = 'ORTHO'
            sp.region_3d.view_location = (1.4, 0, 1)
            sp.region_3d.view_distance = 8.5
    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.view_layer.objects.active = None
    bpy.ops.wm.save_as_mainfile(filepath=str(out / 'review.blend'), compress=True)
    dump(out / 'manifest.json', info)
    # Separate file of just the wearable meshes (base markers removed), one per level.
    # Runs last: it resets the Blender session to import the exported GLBs.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import make_wearable_blend
    make_wearable_blend.build(out)
    if swatches is not None:
        # High-poly evidence file beside the source GLB: detected face materials + original texture.
        import make_highpoly_blend
        make_highpoly_blend.build(source, swatches, cameras_path=cameras_path, regions_path=regions_path,
                                  base_parts=base_parts, part_fit=part_fit, module=module)
    return info


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
        prog='blender -b --python reduce_model.py --',
        description='Reduce one Tripo GLB to 0.4/0.2/0.1% levels with material roles.')
    parser.add_argument('source')
    parser.add_argument('output_dir')
    parser.add_argument('swatches', nargs='?')
    parser.add_argument('--cameras', help='transforms.json of the base views the concept used')
    parser.add_argument('--regions', help='regions.json of that base (base_regions.py)')
    parser.add_argument('--parts', help='parts.npz of that base (base_parts.py); default: beside regions.json')
    parser.add_argument('--no-parts', action='store_true', help='ignore parts.npz; landmark prior, no body_part')
    parser.add_argument('--no-off-body', action='store_true', help='skip the off-body rule (faces outside the base body cannot be base)')
    parser.add_argument('--interior', choices=['auto', 'strip', 'flag'], default='auto',
                        help='enclosed (inner shell) faces: auto = strip above the calibrated threshold, '
                             'strip = always, flag = measure and flag only')
    parser.add_argument('--module', choices=['body_armor', 'right_gauntlet_boot', 'outfit'])
    a = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    run(a.source, a.output_dir, a.swatches, a.cameras, a.regions, a.module, a.parts, not a.no_parts, a.interior, not a.no_off_body)
