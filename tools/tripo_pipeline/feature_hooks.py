"""Opt-in feature label passes (studs, chain-mail rings) as hooks of the high-poly label field (2026-10-25).

    --feature-labels studs                  round studs / rivets painted tertiary keep their colour (feature_labels.py)
    --feature-labels rings[:secondary]      chain-mail ring tubes primary, ring centres the secondary role (ring_labels.py)
    --feature-labels rings:void[:auto|all|<z>]   ring centres are removed (label 0): the cut wearable gets real gaps; `auto` (the default)
                                            only above the waist (top of the belt / buckle), `all` everywhere, a number = a height
    --feature-labels none                   explicit off (overrides a job record / config.json choice)

Off by default: nothing here runs unless a spec is given. The spec is carried by

  * `wearcut.py --feature-labels SPEC`   (wearcut.run: `P['label_hook']` / `params['label_hook']`, called on the final high-poly labels
                                          right before the cut field is built; `ctx` = v, f, lab, area, part, part_names, ev, pal, colors,
                                          cos, module, out_dir, log; returns {lab, info, summary, arrays, params});
  * `segment_first.py --feature-labels SPEC`   (segment_first.process(label_hook=...), after fill_slivers, before the sections are cut);
  * `reduce_model.py` / `rewear.py` / `relabel_module.py` / `convert_job.py` / `wearcut_blender.py rewear` pass it on, and
    `feature_relabel.py` transfers the corrected roles to the labelled full LODs (`transfer_roles` below).

Which sets use which spec: `resolve_spec` = command line, else the job record (`.tripo-job.json` `feature_labels`: a spec string, or
{"default": spec, "<module>": spec}), else `config.json` `feature_labels.sets[<set id>]` (for the modules in `feature_labels.modules`).

Only garment roles change (studs) or garment faces become void (rings): a base face is never turned into garment, so the garment/base
boundary, the mirror pooling and the cut line stay where the unmodified pipeline put them (void centres aside, which are the point).
This module imports numpy / scipy only inside `make_hook` and `transfer_roles`, so convert_job.py can use `resolve_spec` anywhere.
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODES = ('studs', 'rings')
SPEC_RE = re.compile(r'^(?P<mode>studs|rings)(?::(?P<centre>[a-z]+))?(?::(?P<z>auto|all|-?\d+(?:\.\d+)?))?$')
DEFAULT_MODULES = ('body_armor', 'right_gauntlet_boot')
VOID_ABOVE_AUTO = 'auto'


def parse_spec(spec):
    """{'mode', 'centre', 'void_above_z'} for a spec string, None for a falsy / 'none' spec; ValueError for anything else."""
    if spec is None or str(spec).strip().lower() in ('', 'none', 'off'):
        return None
    m = SPEC_RE.match(str(spec).strip().lower())
    if not m:
        raise ValueError(f'feature labels spec {spec!r}: expected studs | rings[:<role>|:void[:auto|:all|:<z>]] | none')
    mode, centre, z = m.group('mode'), m.group('centre'), m.group('z')
    if mode == 'studs':
        if centre or z:
            raise ValueError("feature labels spec 'studs' takes no options")
        return {'mode': 'studs', 'centre': None, 'void_above_z': None}
    centre = centre or 'secondary'
    if centre != 'void' and z:
        raise ValueError(f"feature labels spec {spec!r}: a height only goes with 'rings:void'")
    if centre == 'void':
        z = z or VOID_ABOVE_AUTO
        zv = None if z == 'all' else (VOID_ABOVE_AUTO if z == 'auto' else float(z))
    else:
        zv = None
    return {'mode': 'rings', 'centre': centre, 'void_above_z': zv}


def normalise_spec(spec):
    """The canonical spelling of a spec ('rings' -> 'rings:secondary', 'rings:void' -> 'rings:void:auto'), None when off."""
    p = parse_spec(spec)
    if p is None:
        return None
    if p['mode'] == 'studs':
        return 'studs'
    if p['centre'] != 'void':
        return f'rings:{p["centre"]}'
    z = p['void_above_z']
    return 'rings:void:' + ('all' if z is None else 'auto' if z == VOID_ABOVE_AUTO else f'{z:g}')


def config_block():
    try:
        return json.loads((HERE / 'config.json').read_text(encoding='utf-8-sig')).get('feature_labels') or {}
    except (OSError, ValueError):
        return {}


def resolve_spec(set_name, module, record=None, cli=None, config=None):
    """(spec or None, source) for one module of a job. Order: `cli` (the --feature-labels value; 'none' switches it off), the job
    record's `feature_labels`, config.json `feature_labels.sets[set_name]`. Only the modules in config `feature_labels.modules`
    (default body_armor, right_gauntlet_boot) take the config / record spec unless the record names the module itself."""
    cfg = config if config is not None else config_block()
    if cli is not None:
        return normalise_spec(cli), '--feature-labels'
    rec = (record or {}).get('feature_labels')
    modules = tuple(cfg.get('modules') or DEFAULT_MODULES)
    if isinstance(rec, dict):
        if module in rec:
            return normalise_spec(rec[module]), '.tripo-job.json feature_labels'
        rec = rec.get('default')
    if rec is not None:
        return (normalise_spec(rec) if module in modules else None), '.tripo-job.json feature_labels'
    spec = (cfg.get('sets') or {}).get(set_name)
    if spec is not None and module in modules:
        return normalise_spec(spec), 'config.json feature_labels.sets'
    return None, None


# ----------------------------------------------------------------------------------------------- the hooks

def _unchanged(lab, reason):
    return {'lab': lab, 'info': {'skipped': reason}, 'summary': {'skipped': reason}, 'arrays': {}, 'params': {}}


def make_hook(spec, params=None):
    """The label hook for `spec` (callable, attributes .spec and .mode), see the module docstring for its context and result."""
    import numpy as np
    parsed = parse_spec(spec)
    if parsed is None:
        raise ValueError('no feature labels spec')
    canonical = normalise_spec(spec)

    if parsed['mode'] == 'studs':
        import feature_labels as fl

        def hook(ctx):
            lab, pal, log = ctx['lab'], ctx['pal'], ctx.get('log') or print
            roles = list(pal.roles)
            if 'tertiary' not in roles:
                return _unchanged(lab, 'the palette has no tertiary role')
            ter = 1 + roles.index('tertiary')
            res = fl.studs(ctx['v'], ctx['f'], lab, ctx['ev'][:, ter], ter, params, log)
            info = res['info']
            arrays = {'stud_id': res['stud_id'].astype(np.int32), 'spill': res['spill'], 'h': res['h'].astype(np.float32), 'tertiary': np.int8(ter)}
            summary = {'mode': 'studs', 'studs': info['studs'], 'studs_geometry_only': info['studs_geometry_only'],
                       'studs_flat_texture': info['studs_flat_texture'], 'before': info['before'], 'fixed': info['fixed'],
                       'radius': info['radius'], 'faces_to_tertiary': info['faces_to_tertiary'],
                       'spill_components': info['spill'].get('spill_components', 0), 'spill_faces': info['spill'].get('spill_faces', 0),
                       'tertiary_area_before': info['tertiary_area_before'], 'tertiary_area_after': info['tertiary_area_after'],
                       'seconds': info['seconds']}
            return {'lab': res['lab_new'].astype(lab.dtype), 'info': info, 'summary': summary, 'arrays': arrays, 'params': {}}
    else:
        import ring_labels as rl

        def hook(ctx):
            lab, pal, log = ctx['lab'], ctx['pal'], ctx.get('log') or print
            roles = list(pal.roles)
            try:
                ring_idx, centre_idx = rl.role_index(roles, 'primary'), rl.role_index(roles, parsed['centre'])
            except ValueError as error:
                return _unchanged(lab, str(error))
            p = dict(params or {})
            if parsed['centre'] == 'void' and parsed['void_above_z'] is not None:
                p.setdefault('void_above_z', parsed['void_above_z'])
            res = rl.ring_pass(ctx['v'], ctx['f'], lab, ring_idx, centre_idx, p, log)
            stats = res.pop('stats')
            arrays = {'kind': res['kind'], 'h': res['h'], 'score': res['score'], 'hole_id': res['hole_id'].astype(np.int32),
                      'ring_id': res['ring_id'].astype(np.int32), 'hole_centroids': res['holes_centroids'].astype(np.float32),
                      'hole_area': res['holes_area'].astype(np.float32), 'ring_index': np.int8(ring_idx), 'centre_index': np.int8(centre_idx)}
            area = ctx['area']
            new = res['label']
            summary = {'mode': 'rings', 'centre': parsed['centre'], 'ring_index': ring_idx, 'centre_index': centre_idx,
                       'holes': stats['holes']['count'], 'median_hole_diameter': stats['holes']['median_equiv_diameter'],
                       'changed_area_share_of_garment': stats['changed']['area_share_of_garment'],
                       'ring_area_share_of_garment': stats['changed']['ring_area_share_of_garment'],
                       'centre_area_before': float(area[lab == centre_idx].sum()) if centre_idx else None,
                       'centre_area_after': float(area[new == centre_idx].sum()) if centre_idx else None,
                       'voided_area': float(area[(lab > 0) & (new == 0)].sum()), 'void_above_z': stats.get('void_above_z'),
                       'sigma': stats['scale']['sigma'], 'protected_area_share': stats['protected_area_share'],
                       'seconds': stats['seconds']}
            info = {'ring_pass': stats, 'summary': summary}
            # the cut field of a void pass must keep the gaps open: a finer smoothing length than the 0.8% default (prototype value)
            return {'lab': new.astype(lab.dtype), 'info': info, 'summary': summary, 'arrays': arrays,
                    'params': {'sigma_pct': .3} if parsed['centre'] == 'void' else {}}
    hook.spec = canonical
    hook.mode = parsed['mode']
    return hook


# ----------------------------------------------------------------------------------------------- labelled LOD transfer

def transfer_roles(spec, v, f, role, hp_cent, hp_area, arrays, K):
    """New role per triangle of a labelled full LOD (v, f in the normalised frame, role (n,) 0 base, 1.. garment) from the high-poly
    labels of the pass (`arrays` = the loaded feature_labels.npz). Returns (new role (n,), info dict).

    studs: a triangle takes the majority NEW role of the high-poly faces it covers, only where at least 25% of its covered area
    changed (and it was garment). rings: a triangle whose covered ring-region area (hp faces of kind ring / centre) is more than half
    of its coverage takes the majority new label there (0 = void: the triangle becomes base in the full LOD)."""
    import numpy as np
    parsed = parse_spec(spec)
    old, new = arrays['label_old'].astype(np.int64), arrays['label_new'].astype(np.int64)
    if parsed['mode'] == 'studs':
        import feature_labels as fl
        out = fl.transfer_role_array(v, f, role, hp_cent, hp_area, old, new, K)
        rule = 'studs: majority new role where >= 25% of the covered area changed'
    else:
        import ring_labels as rl
        out, _, _ = rl.transfer_role_array(v, f, role, hp_cent, hp_area, new, arrays['kind'], K)
        rule = 'rings: majority new label of the ring/centre region where it covers > 50% of the triangle'
    return out, {'rule': rule, 'faces_changed': int((out != role).sum())}
