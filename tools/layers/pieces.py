"""Swappable pieces (2026-11-24).

User: "ultimately our end result is something that we can mix and match at will in the game engine" ... "the ones where the armour goes beyond
the slice points, we can just leave those and let the other side overlap ... it'll be covering the slice point still".

The combined results (geodiv.run_armour / run_outfit) join every module of ONE pairing on one shared copy of the body, so a gauntlet swap means a
rebuild. Here every module is built ALONE against the bare game body, restricted to its SLOT (a set of the game body's parts), so the body skin of a
piece ends exactly on the game body's own ring at the slot border (the same vertices for every piece of a race: elbow, knee, neck, wrist, ankle).
Any pieces of one race that fill the slots without overlap join there without being processed together.

  armour scheme   torso (body armour: all but head and lower limbs) + lower_arm.r/l (forearm + hand: gauntlets) + lower_leg.r/l (shin + foot:
                  boots) + head; the left gauntlet / boot is the right one mirrored and fitted to the left slot
  outfit scheme   outfit (all but head, hands and the feet it leaves bare) + head + hand.l/r (+ foot.l/r when bare)
  bare pieces     the game body's own faces of every slot (head, hands, feet, lower arms / legs, torso), for slots nothing is worn on

Garment past a ring is kept whole and lies over the next piece: its loops are left free (imprint.build loop_outside), nothing is pulled onto the
ring. A piece whose skin under such an overhang was removed is open at that ring there (hidden under its own garment).

  python experiments/shell_slots_20261122/pieces.py [--races dwarf_female,...] [--workers 11] [--gpu] [--only <set substring>]
Writes out/pieces/<race>/<slot>__<set>.glb (+ .json record, scenes/*.npz) and out/pieces/index.json; out/pieces/mix.html shows them together.
"""
import argparse
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
OUTP = HERE / 'out' / 'pieces'
RING_TOL_MM = 0.5            # a piece vertex this close to a ring vertex is that ring vertex (restored exactly after the morphs)


def _setup():
    sys.path.insert(0, str(HERE))
    import geodiv as gd  # noqa: F401


def slots(body, gd):
    """Slot name -> part ids, per scheme (parts the race has)."""
    P = lambda *n: gd.parts(body, *n)  # noqa: E731
    head = P('head.x')
    out = {'head': head}
    for s in 'lr':
        out[f'hand.{s}'] = P(f'hand.{s}')
        out[f'foot.{s}'] = P(f'foot.{s}')
        out[f'lower_arm.{s}'] = gd.lower_arm(body, s)
        out[f'lower_leg.{s}'] = gd.lower_leg(body, s)
    low = sum((out[f'lower_arm.{s}'] + out[f'lower_leg.{s}'] for s in 'lr'), [])
    out['torso'] = [p for p in gd.all_parts(body) if p not in head + low]
    return {k: v for k, v in out.items() if v}


def sub_body(body, slot, bf):
    """The game body's faces of the slot as their own Body (same positions, so the ring vertices are bit-identical across pieces) + ring
    positions (the open border of the slot)."""
    fm = np.isin(body.part, slot)
    used, Fs = np.unique(body.F[fm], return_inverse=True)
    Fs = Fs.reshape(-1, 3)
    sub = bf.Body(body.V[used], Fs, body.part[fm], body.part_names)
    E = np.sort(np.concatenate([Fs[:, [0, 1]], Fs[:, [1, 2]], Fs[:, [2, 0]]]), axis=1)
    ue, c = np.unique(E, axis=0, return_counts=True)
    ring = np.unique(ue[c == 1])
    return sub, sub.V[ring]


def ring_ids(V, ring_pos):
    from scipy.spatial import cKDTree
    if not len(ring_pos) or not len(V):
        return np.zeros(0, int), np.zeros(0, int)
    d, i = cKDTree(V).query(ring_pos)
    ok = d < RING_TOL_MM / 1000
    return i[ok], np.flatnonzero(ok)


DROP_HIDDEN_GARMENT = False  # (tried: at Amount 0 the body hides garment lying inside it, so a bodice back went)  garment faces no view sees (with the bare body in every other slot) are dropped before the expansion: an inner
                             # wall of a double-walled skirt sits inside the body, the expansion pushes it out past the outer wall and its back
                             # shows (gamurra dress: 10.6 % of what is seen at Amount 100)


def drop_hidden_garment(out, body, slot, gd):
    import visibility as vis
    from shell_slots import Mesh
    rest = gd.game_faces(body, [p for p in gd.all_parts(body) if p not in slot])
    both = gd.concat([out, rest])
    v = vis.visibility(both.V, both.F, px=.004, see_through=False)
    seen = (v['front'] + v['back'])[:len(out.F)] > 0
    drop = (out.role != gd.SKIN) & (out.prov != 'lip') & ~seen
    if not drop.any():
        return out, 0
    return gd.compact(Mesh(out.V, out.F[~drop], out.role[~drop], out.part[~drop], out.prov[~drop])), int(drop.sum())


HIDE_UNSEEN_SKIN = True      # after the expansion: the piece's body skin that no view sees (the piece on the bare body, ~180 directions) is removed
UNSEEN_PX_MM = 3.0           # (2026-11-24: a robe kept the whole torso and arm skin because the inner thighs under it count as uncovered for the
                             # region test along the normal; that skin poked through the sleeves). Faces touching the ring stay (joins closed).


def hide_unseen_skin(out, U1, Ud, rest, ring_vids, gd):
    import visibility as vis
    from shell_slots import Mesh
    cand = (out.role == gd.SKIN) & (out.prov == 'band')
    if len(ring_vids):
        on_ring = np.zeros(len(out.V), bool)
        on_ring[ring_vids] = True
        cand &= ~on_ring[out.F].any(1)
    if not cand.any():
        return out, U1, Ud, 0
    both = gd.concat([Mesh(U1, out.F, out.role, out.part, out.prov), rest])
    dirs = vis.view_dirs(rings=(-75, -60, -45, -30, -15, 0, 15, 30, 45, 60, 75), per_ring=16)
    v = vis.visibility(both.V, both.F, px=UNSEEN_PX_MM / 1000, dirs=dirs, see_through=False)
    seen = (v['front'] + v['back'])[:len(out.F)] > 0
    drop = cand & ~seen
    if not drop.any():
        return out, U1, Ud, 0
    keep = ~drop
    used, F2 = np.unique(out.F[keep], return_inverse=True)
    m = Mesh(out.V[used], F2.reshape(-1, 3), out.role[keep], out.part[keep], out.prov[keep])
    return m, U1[used], (Ud[used] if Ud is not None else None), int(drop.sum())


FILL_LAYER_GAP_MM = 3.0      # the expansion stops a garment vertex this short of any other garment surface in its way


def fill_layer_guard(out, U1, gd):
    """The expansion must not push one garment layer through another (a double-walled skirt's inner wall lies inside the body, is pushed out
    past the outer wall and shows its back: gamurra dress 10.6 % of what is seen). Each moved garment vertex looks along its move for another
    garment surface (as it was, not touching the vertex); the move ends FILL_LAYER_GAP_MM short of it. In place on U1. Returns notes."""
    from shell_slots import SKIN
    gmask = out.role != SKIN
    D = U1 - out.V
    L = np.linalg.norm(D, axis=1)
    gv = np.unique(out.F[gmask])
    mv = gv[L[gv] > 1e-3]
    if not len(mv):
        return {'clamped': 0}
    Fg = out.F[gmask]
    T = out.V[Fg]
    d = D[mv] / L[mv, None]
    gap = FILL_LAYER_GAP_MM / 1000
    t = np.full(len(mv), np.inf)
    for k in range(0, len(mv), 64):                                   # per vertex: its own faces do not count
        sl = slice(k, k + 64)
        tt = gd.ray_first_hit(out.V[mv[sl]] + d[sl] * 1e-4, d[sl], T[:, 0], T[:, 1], T[:, 2])
        t[sl] = tt
    own = np.zeros(len(mv), bool)
    hit = np.isfinite(t) & (t < L[mv] + gap)
    if hit.any():                                                     # re-check hits against faces not touching the vertex
        for i in np.flatnonzero(hit):
            v = mv[i]
            keep = ~(Fg == v).any(1)
            tt = gd.ray_first_hit(out.V[v][None] + d[i][None] * 1e-4, d[i][None], T[keep, 0], T[keep, 1], T[keep, 2])[0]
            t[i] = tt
            own[i] = not (np.isfinite(tt) and tt < L[v] + gap)
    clamp = np.isfinite(t) & (t < L[mv] + gap) & ~own
    newL = np.maximum(t[clamp] - gap, 0.)
    U1[mv[clamp]] = out.V[mv[clamp]] + d[clamp] * newL[:, None]
    return {'clamped': int(clamp.sum()), 'moved': int(len(mv))}


FILL_FOLD_DEG = 90.0         # a garment face the expansion turns further than this (it folds over: its back shows) ...
FILL_FOLD_ROUNDS = 10        # ... has its vertices' expansion halved, round after round (the last round: undone)


def fill_fold_guard(out, U1):
    """The expansion (fill) is a smooth displacement field; where it pushes hip vertices far out while a hanging skirt below stays, skirt faces turn
    over (gamurra dress: 10.6 % of what is seen was garment backs at Amount 100, 0.1 % at 0). Moves of vertices of faces turned more than
    FILL_FOLD_DEG are halved until none is (in place on U1). Returns notes."""
    from shell_slots import SKIN
    gf = np.flatnonzero(out.role != SKIN)
    if not len(gf):
        return None
    Fg = out.F[gf]
    n0 = np.cross(out.V[Fg[:, 1]] - out.V[Fg[:, 0]], out.V[Fg[:, 2]] - out.V[Fg[:, 0]])
    D = U1 - out.V
    cosl = np.cos(np.radians(FILL_FOLD_DEG))
    first = None
    for it in range(FILL_FOLD_ROUNDS):
        X = out.V + D
        n1 = np.cross(X[Fg[:, 1]] - X[Fg[:, 0]], X[Fg[:, 2]] - X[Fg[:, 0]])
        cs = (n0 * n1).sum(1) / np.maximum(np.linalg.norm(n0, axis=1) * np.linalg.norm(n1, axis=1), 1e-18)
        bad = cs < cosl
        if first is None:
            first = int(bad.sum())
        if not bad.any():
            break
        v = np.unique(Fg[bad])
        D[v] *= 0. if it == FILL_FOLD_ROUNDS - 1 else .5
    U1[:] = out.V + D
    return {'faces_folded': first, 'left_folded': int(bad.sum()) if first else 0}


def finish_piece(out, body, slot, ring_pos, clear_parts, gen, fields, gd):
    """geodiv.finish for one piece: the same steps, ring vertices restored exactly afterwards, inverted faces judged with the rest of the body
    in place (bare game faces of every other slot close the rings, so the inside is not seen through the opening)."""
    import seal
    from shell_slots import Mesh
    notes = {}
    out = gd.compact(out)
    notes['garment_pushed_off_game_pieces'] = gd.clear_of_pieces(out, body, list(clear_parts))
    if gd.SPIKE_CLEAN:
        notes['folded_slivers_collapsed'] = gd.collapse_folded_slivers(out)
    if gd.MERGE_FLAT_SKIN:
        sk = (out.role == gd.SKIN) & np.isin(out.prov, ['band', 'game'])
        F2, alive, rem = seal.merge_flat(out.V, out.F, sk)
        out = gd.compact(Mesh(out.V, F2[alive], out.role[alive], out.part[alive], out.prov[alive]))
        notes['flat_skin_merged_vertices'] = int(rem)
    if DROP_HIDDEN_GARMENT:
        out, notes['hidden_garment_faces_dropped'] = drop_hidden_garment(out, body, slot, gd)
    ri, rk = ring_ids(out.V, ring_pos)
    margin = gd.fl.DEFAULTS['margin_pct'] / 100 * body.H
    U1, notes['fill'] = gd.fill_grouped(out, body, margin)
    notes['fill_layer_guard'] = fill_layer_guard(out, U1, gd)
    notes['fill_fold_guard'] = fill_fold_guard(out, U1)
    if gd.KEPT_SKIN_SINK:
        notes['kept_skin_sunk'] = gd.sink_kept_skin(out, U1, gen)
    Ud, notes['inflation'] = gd.deflate_positions(out, list(fields))
    moved = np.linalg.norm(out.V[ri] - ring_pos[rk], axis=1) if len(ri) else np.zeros(0)
    for X in (out.V, U1, Ud):                                          # the ring is shared with the next piece: never moved
        if X is not None and len(ri):
            X[ri] = ring_pos[rk]
    notes['ring'] = {'vertices': int(len(ring_pos)), 'in_piece': int(len(ri)), 'restored_mm_max': round(float(moved.max() * 1000), 2) if len(moved) else 0.}
    rest = gd.game_faces(body, [p for p in gd.all_parts(body) if p not in slot])
    if HIDE_UNSEEN_SKIN:
        out, U1, Ud, notes['unseen_skin_faces_removed'] = hide_unseen_skin(out, U1, Ud, rest, ri, gd)
    if gd.FIX_INVERTED and len(out.F):
        both = gd.concat([out, rest])
        flip = seal.fix_inverted(both.V, both.F, body, always=both.prov == 'lip')[:len(out.F)]
        out.F[flip] = out.F[flip][:, ::-1]
        notes['inverted_faces_flipped'] = int(flip.sum())
    try:                                                               # joined with bare game pieces on every other slot: does it close?
        import watertight as wt
        both = gd.compact(gd.concat([out, rest]))
        sm = wt.summary(wt.scan(both.V, both.F, np.array([f'{r}:{p}' for r, p in zip(both.role, both.prov)])))
        sk_open = 0.
        s_ = wt.scan(both.V, both.F, np.array([f'{r}:{p}' for r, p in zip(both.role, both.prov)]))
        E = s_['E']
        if len(E):                                                     # open edges between two skin faces' vertices: rings left open
            skin_v = np.zeros(len(both.V), bool)
            skin_v[both.F[both.role == gd.SKIN].ravel()] = True
            gar_v = np.zeros(len(both.V), bool)
            gar_v[both.F[both.role != gd.SKIN].ravel()] = True
            only_skin = skin_v & ~gar_v
            sk_open = float(s_['len'][only_skin[E[:, 0]] & only_skin[E[:, 1]]].sum())
        notes['with_bare_rest'] = {'open_m': sm['open_m'], 'skin_only_open_m': round(sk_open, 3), 'nonmanifold_edges': sm['nonmanifold_edges']}
    except Exception as error:  # noqa: BLE001
        notes['with_bare_rest'] = {'error': f'{type(error).__name__}: {error}'}
    return out, U1, Ud, notes


def build_piece(m, slot, body, gd, clear_parts=(), st=False, gen=(), fields=()):
    import imprint
    keep, dropped = gd.kept_garment(m, slot, list(clear_parts) or None, body)
    g = m.subset(keep)
    if not len(g.F) or not (g.role != gd.SKIN).any():
        return None, None, None, {'skipped': 'no garment in this slot'}
    sub, ring_pos = sub_body(body, slot, gd.bf)
    out, nt = imprint.build(sub, [(g, slot, st)], [], log=lambda *a: None, keep_ring_band=True,
                            loop_outside=lambda P: ~np.isin(body.part[body.closest(P)[1]], slot))
    nt.pop('loop_list', None)
    nt['garment_dropped_cm2'] = round(float(dropped), 1)
    out, U1, Ud, notes = finish_piece(out, body, slot, ring_pos, clear_parts, gen, fields, gd)
    notes['imprint'] = nt
    return out, U1, Ud, notes


def write_piece(race, slot_key, set_name, out, U1, Ud, rec, gd):
    from shell_slots import face_colours, glb_bytes
    d = OUTP / race
    (d / 'scenes').mkdir(parents=True, exist_ok=True)
    stem = f'{slot_key}__{set_name}'
    (d / f'{stem}.glb').write_bytes(glb_bytes(out, U1, extra={'deflate': Ud} if Ud is not None else None))
    for tag, X in (('a0', out.V), ('a100', U1 if U1 is not None else out.V)):
        np.savez_compressed(d / 'scenes' / f'{stem}__{tag}.npz', gV=X.astype(np.float32), gF=out.F.astype(np.int32),
                            gC=face_colours(out, 'role').astype(np.float32))
    rec.update({'race': race, 'slot': slot_key, 'set': set_name, 'glb': f'{race}/{stem}.glb', 'faces': int(len(out.F)),
                'code_mtime': gd.CODE_MTIME})
    (d / f'{stem}.json').write_text(json.dumps(rec, default=float, indent=1), encoding='utf-8')


def task_bare(race):
    import geodiv as gd
    body = gd.game_body(race)
    S = slots(body, gd)
    for k, ids in S.items():
        g = gd.game_faces(body, ids)
        write_piece(race, k, 'bare', g, None, None, {'kind': 'bare', 'parts': [body.part_names[i] for i in ids]}, gd)
    return f'{race}: {len(S)} bare pieces'


def task_module(mod):
    import geodiv as gd
    t0 = time.monotonic()
    mod = Path(mod).resolve()
    s, j, race, module = gd.module_identity(mod)
    body = gd.game_body(race)
    rig = gd.ab.Rig.from_npz(gd.bf.BASE_CACHE / f'{race}_rig.npz')
    S = slots(body, gd)
    m, tripo, info = gd.prepare_geo(mod, body, rig, race)
    st = gd.see_through(mod)
    rel = mod.relative_to(gd.ROOT).as_posix()
    made = []

    def one(slot_key, mm, ids, clear_parts, gen, fields, extra):
        out, U1, Ud, notes = build_piece(mm, ids, body, gd, clear_parts, st, gen, fields)
        if out is None:
            made.append(f'{slot_key}: {notes["skipped"]}')
            return
        rec = {'kind': module, 'module': rel, 'covers': [body.part_names[i] for i in ids], 'notes': notes, 'load': info, **extra,
               'seconds': round(time.monotonic() - t0, 1)}
        write_piece(race, slot_key, s, out, U1, Ud, rec, gd)
        made.append(f'{slot_key}: {len(out.F)} faces, overhang loops {notes["imprint"].get("overhang", 0)}, '
                    f'open with bare rest {notes["with_bare_rest"].get("open_m")} m')

    fields = [m.infl] if hasattr(m, 'infl') else []
    if module == 'outfit':
        game = S['head'] + S.get('hand.l', []) + S.get('hand.r', [])
        bare_feet = []
        for side in 'lr':
            fp = S.get(f'foot.{side}', [])
            if fp and gd.has_footwear(m, body, side) < 20:
                game += fp
                bare_feet.append(f'foot.{side}')
        ids = [p for p in gd.all_parts(body) if p not in game]
        one('outfit', m, ids, game, [tripo], fields, {'bare_feet': bare_feet})
    elif module == 'body_armor':
        one('torso', m, S['torso'], S['head'], [tripo], fields, {})
    elif module == 'right_gauntlet_boot':
        if gd.CENTRE_LIMBS:
            info['centred_on_game_limbs'] = gd.centre_limb_pieces(m, body, 'r')
        x0 = gd.fl.mirror_plane(race, body)[0]
        ml = gd.mirror_mesh(m, x0, body)
        tl = gd.mirror_mesh(tripo, x0, body)
        fl_ = [gd.mirrored_field(f, x0) for f in fields]
        for side, mm, gen, ff in (('r', m, [tripo], fields), ('l', ml, [tl], fl_)):
            for limb in ('lower_arm', 'lower_leg'):
                k = f'{limb}.{side}'
                if k in S:
                    one(k, mm, S[k], S['head'], gen, ff, {'mirrored_from_right': side == 'l'})
    return f'{race} {s} {module}: ' + '; '.join(made) + f' ({time.monotonic() - t0:.0f} s)'


def _run(task):
    kind, arg = task
    try:
        return task_bare(arg) if kind == 'bare' else task_module(arg)
    except Exception as error:  # noqa: BLE001
        import traceback
        return f'FAILED {arg}: {type(error).__name__}: {error}\n{traceback.format_exc(limit=3)}'


def write_index():
    recs = []
    for p in sorted(OUTP.glob('*/*.json')):
        r = json.loads(p.read_text(encoding='utf-8'))
        recs.append({k: r.get(k) for k in ('race', 'slot', 'set', 'glb', 'kind', 'faces', 'covers', 'bare_feet', 'mirrored_from_right')} |
                    {'overhang_loops': (r.get('notes') or {}).get('imprint', {}).get('overhang'),
                     'with_bare_rest': (r.get('notes') or {}).get('with_bare_rest'), 'ring': (r.get('notes') or {}).get('ring')})
    (OUTP / 'index.json').write_text(json.dumps({'pieces': recs}, indent=0), encoding='utf-8')
    return len(recs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--races', default='')
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--gpu', action='store_true')
    ap.add_argument('--only', default='', help='only modules whose set name contains this')
    a = ap.parse_args()
    if a.gpu:
        os.environ['BODY_FIT_GPU'] = '1'
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    sys.path.insert(0, str(HERE))
    import process_all as pa
    races = [r for r in a.races.split(',') if r] or None
    mods = []
    for jb in pa.jobs(races):
        for m in jb['modules']:
            if a.only in m and m not in mods:
                mods.append(m)
    rs = sorted({m.split('/')[3] for m in mods} | set(races or []))
    tasks = [('bare', r) for r in rs] + [('module', str(pa.ROOT / m)) for m in mods]
    OUTP.mkdir(parents=True, exist_ok=True)
    print(time.strftime('%H:%M:%S'), f'{len(tasks)} tasks ({len(mods)} modules, {len(rs)} races), {a.workers} workers', flush=True)
    with mp.Pool(a.workers, initializer=_setup) as pool:
        for msg in pool.imap_unordered(_run, tasks):
            print(time.strftime('%H:%M:%S'), msg, flush=True)
    print(time.strftime('%H:%M:%S'), 'index:', write_index(), 'pieces', flush=True)


if __name__ == '__main__':
    main()
