"""Fit lab (2026-11-18): compare garment fit strategies for the aligned LOD 0p4 wearable, with optional left/right symmetry, for the preview viewer.

    python tools/tripo_pipeline/fit_lab.py <module_dir> [<module_dir> ...] | --all [--match S] [--jobs N] [--combos all|ID,ID,..] [--force] [--sections-only]
        [--margin-pct 0.4] [--kappa K] [--iters N] [--rim-ramp-pct 3] [--sym-ramp-pct 3] [--when-away] [--status-file F] [--log-file F]

<module_dir> = models/<set>/<job-id>/<race>/<module>. Input is `reduced/lod_error_0p4__wearable_aligned.glb` (the "original"); every output keeps its exact
topology, vertex order, primitives and materials (only POSITION / NORMAL change, body_fit's `Glb.patched`), so a viewer can blend positions linearly
between two files. Outputs: `<module>/fitlab/lod_0p4/<strategy>__<sym>__<scope>.glb` + `index.json` (per combo: file, seconds, displacement / rim
displacement, poke areas against the drawn body, the hidden sections and the whole body before / after, params, code_version, input stamps; a stale entry
is redone). `original__off__none` is not written (it is the aligned GLB).

Strategies: `original` (no fit; with a symmetry it is only the symmetrised garment), `current` (body_fit Fit A against the whole body, reuses
`..._aligned_fitted.glb` for sym off), `sections_fill` (clear the body AS DRAWN = shown sections + tucked connector meshes with the margin, and the HIDDEN
sections with zero margin, so the garment comes out to the silhouette where it is far inside), `sections_tight` (drawn body only, hidden sections impose
nothing). Both section strategies pin openings: vertices on open boundary loops of the welded garment get displacement weight 0, ramping to 1 over
`--rim-ramp-pct` % of the body height along the garment surface (graph distance); smoothing is local (higher kappa than body_fit). The report says where a
pinned rim still pokes a SHOWN (non-connector) section.

Symmetry (before the fit): `l2r` copies the anatomical-left garment surface onto the right, `r2l` the reverse; scope `limbs` = vertices whose nearest base
part is upper arm / forearm / hand / thigh / shin / foot, `whole` = everything off the mid-plane. The source surface is reflected across the base's mirror
plane (x = x0 in game world; fitted per race, +x = anatomical left on the game bases); every target-side vertex in scope moves toward the closest point of
that reflected surface with a weight ramping 0 -> 1 over `--sym-ramp-pct` % of the height from the scope boundary (the mid-plane for `whole`).

Deep garments (2026-11-19; gnome / orc / dwarf / halfling garments sit 50-200 mm inside the body): the section strategies (1) cap a vertex's displacement at
w * (rim_max + its depth inside the whole game body) and a poke lift at depth + body_fit's POKE_CAP instead of fixed 120 / 150 mm, (2) apply the "deep rule" of
SectionBody.measure (a vertex buried more than `deep_mm` in the WHOLE closed game base must come up to that depth even where the sections would let it ignore the body; the
whole base gives the sign and normal), (3) look `Lab.reach` (body_fit's POKE_IN 0.12, more for a deeply buried garment: p99 vertex depth + 0.05 m) behind a body sample for garment in the reverse test and the metrics, (4) stop
when successive states bring no progress, and (5) `Lab.guard`: a section result whose drawn-body poke is worse than the original's is replaced by the better of `current` and the
original (index entry `info.guard`). Also: (6) a pinned rim vertex that starts more than `rim_release_mm` (6) inside a drawn surface (shown section / connector) is released and
may move at most its depth + margin + `rim_release_reach_mm` (a hem inside a foot, a cuff inside a hand would stay buried while the rest flares), (7) a poke that demands more than
`poke_ignore_mm` (30) + margin + the vertex's depth in the whole body is not acted on, only reported (a sole under a bare foot lifted through it was the orc / halfling failure).
Shape (2026-11-20): (8) the deep rule's burial limit is REGIONAL (`regional_deep`: depth diffused over the garment, 40 mm below the surface where the garment is near it, blending to
the surface + margin where the region is buried 40-80 mm: a gnome chest is a misfit, not a tuck, and a mix of limits sheared straps / panels), (9) the poke lifts of a vertex are capped
at `runaway` x its first demand + extra (a survivor poke used to ratchet the lift round after round: the 138 mm orc neck spike). Two attempts (`runaway_attempts`: strict 1.0 x + 10 mm,
then uncapped; deep-region garments try the uncapped one first); an attempt is clean when its drawn poke is <= min(1.1 x original + 2, max(`clean_frac` x original, `clean_cm2`)), the first
clean one stands, otherwise the lowest score (drawn + `guard_score_full` x whole-body poke) of the attempts, (10) `guard` keeps the section result when its drawn poke is within 25 % + 10 cm2
of the original, else the lowest score of {section result, current, original}.
Thin shells under bare connectors (2026-11-20, halfling / dwarf shoes): rims whose closest base part is a foot are released too (`rim_release_parts`, an open sole has its whole outline on the
boundary and stayed pinned), and the parts where the original already pokes the drawn body by >= `seed_part_cm2` start from the whole-body fit (`seed_toward_current`, each step limited to the vertex's
depth + margin + `seed_reach_mm`, faded to nothing at the pinned openings). The guard's `current` fallback is refused when it fans a vertex out more than `guard_flare_mm` from the body; a kept
section result that failed the 25 % test is recorded as `info.guard_kept`, only a real fallback sets `info.guard`.
Rim snap (2026-11-21, `Lab.snap_rims`, DEFAULTS `rim_snap*`, `--no-rim-snap` turns it off): before a section fit every opening edge is moved to `margin` outside the WHOLE game body
(buried rims out, rims of tight loops floating <= 20 mm pulled in, loose hems untouched, smoothed along the loop, carried into the garment over the rim ramp) and pinned there. The
pinned-but-buried openings were where all the remaining drawn-body poke sat (skin showing along cuffs, leg wraps, collars). Pre-snap caches stay valid (the snap is not part of
requested_params); gapfix.py recomputes with --force.
Suspect garments (suspect_garments.py): modules whose source garment is broken are flagged `suspect: true` in index.json and skipped by batch runs (`--include-suspect` fits them anyway).

Body sections per module come from experiments/body_mask_20261104/body_sections.py --fast (run here when the record is missing, belongs to another job or
is a states-only / pre-fast record without drawn_body.npz). The folder is `out/<set>__<job>__<race>__<module>` (`sections_dir`; the old job-less name is still read
when its record is this job's). `--sections-only` stops after that.
Gap fix (2026-11-21): the record's visibility rule is now 'seen' (skin seen from outside; `SECTION_PARAMS['vis_rule']`, 'legacy' = the old under-garment rule). A record made here
(`ensure_sections`) is pass 1 (aligned wearable only); the full fix is `experiments/body_mask_20261104/gapfix.py` (pass 2 = visibility over the union of the aligned wearable and
this lab's sections_fill / sections_tight results, then these combos are refitted). The cache needs no extra key: every section combo's `inputs` hold the size + mtime of body_sections.json
and drawn_body.npz (`input_stamps`) and symmap.json those of the record, so a rewritten record always recomputes them. Records written before the fix stay valid for `sections_status`
(they are not rewritten implicitly; run gapfix.py on them).

`--all` takes every module with the aligned 0p4 wearable and skips a module a live conversion worker holds (rewear's module lock). `--when-away`
(unattended precache, with --jobs N): modules are dispatched to separate worker processes only while the user is away (C:\\Users\\niall\\.claude\\presence.json
fresh < 30 s and idle >= 300 s) and .codex_jobs/_batch/PAUSE does not exist; in-flight modules finish, then it waits (polls every 15 s). Fully cached modules are
skipped (resumable); progress goes to .codex_jobs/_batch/fit_lab_precache.log and fit_lab_precache.status.json.
"""
import os

for _k in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(_k, '1')
import argparse
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
EXP = ROOT / 'experiments' / 'body_mask_20261104'

import numpy as np   # noqa: E402
from scipy import sparse   # noqa: E402
from scipy.sparse import csgraph   # noqa: E402

import body_fit as bf   # noqa: E402
from rewear import discover, identity, module_busy   # noqa: E402

LV = '0p4'
CODE_VERSION = 'fit-lab-20261120c'
REACH_EXTRA = 0.05         # m. The reverse (body-through-garment) test looks this far BEHIND a body sample for garment: body_fit's POKE_IN (0.12), or, for a garment that starts deeper
#                            inside the body, the 99th percentile of its vertex depth + REACH_EXTRA (`Lab.__init__`, per module). A garment buried 120-250 mm (gnome) was invisible to the fixed
#                            0.12, so the "poke before" of those modules was a fraction of the truth (gnome kings_guard: 135 cm2 at 0.12, 3338 cm2 at 0.30) and a fit that pushed the garment into
#                            reach looked like it made things worse. Garments at normal depth (humans, orcs, halflings: p99 < 70 mm) keep 0.12, a larger reach there only finds garment
#                            triangles that run through the inside of a limb (orc shoe soles) and lifts them for no reason.
REACH_DEFAULT = bf.POKE_IN
STRATEGIES = [{'id': 'original', 'label': 'Original (aligned)'}, {'id': 'current', 'label': 'Current fit (whole body)'},
              {'id': 'sections_fill', 'label': 'Sections: fill to body'}, {'id': 'sections_tight', 'label': 'Sections: tight (drawn body)'}]
SYMMETRY = [('off', 'none'), ('l2r', 'limbs'), ('l2r', 'whole'), ('r2l', 'limbs'), ('r2l', 'whole')]
DEFAULT_CHOICE = {'strategy': 'sections_fill', 'amount': 100, 'sym': 'off', 'scope': 'none'}
SECTION_STRATEGIES = ('sections_fill', 'sections_tight')
LIMB_PARTS = ('arm_stretch', 'forearm_stretch', 'hand', 'thigh_stretch', 'leg_stretch', 'foot')
DEFAULTS = {'margin_pct': 0.4, 'kappa': 0.12, 'iters': 60, 'rim_ramp_pct': 3.0, 'sym_ramp_pct': 3.0, 'rim_max_mm': 120.0, 'deep_mm': 40.0, 'rim_release_mm': 6.0, 'rim_release_reach_mm': 30.0, 'rim_release_parts': ['foot'], 'deep_reg0_mm': 40.0, 'deep_reg1_mm': 80.0, 'deep_reg_iters': 25, 'poke_cap_mm': 150.0, 'poke_ignore_mm': 30.0, 'runaway_attempts': [[1.0, 10.0], [0, 0]], 'seed_current': True, 'seed_part_cm2': 25.0, 'seed_reach_mm': 40.0, 'seed_fade_iters': 3, 'hidden_poke_cap_mm': 30.0, 'clean_cm2': 60.0, 'clean_frac': 0.3, 'guard_score_full': 0.05, 'guard_flare_mm': 150.0, 'free_w': 0.5, 'rounds': 12, 'cleanup': 20,
            # Rim snap (2026-11-21, see Lab.snap_rims): the section fits move every opening edge to `margin` OUTSIDE the whole game body before fitting (and pin it there). Not part of
            # requested_params (pre-snap caches stay usable until their body record changes; the gapfix rollout recomputes with --force); recorded in each entry's info.rim_snap.
            'rim_snap': True, 'rim_snap_max_mm': 80.0, 'rim_snap_tight_mm': 12.0, 'rim_snap_pull_mm': 20.0, 'rim_snap_smooth': 4, 'rim_snap_ramp_pct': 3.0, 'rim_snap_lift_mm': 30.0}
PRESENCE = Path(r'C:\Users\niall\.claude\presence.json')
BATCH = ROOT / '.codex_jobs' / '_batch'
SECTIONS_OUT = EXP / 'out'


# ----------------------------------------------------------------------------------------------- ids / paths

def combo_ids():
    return [f'{s["id"]}__{sym}__{scope}' for sym, scope in SYMMETRY for s in STRATEGIES if not (s['id'] == 'original' and sym == 'off')]


def parse_combo(cid):
    parts = cid.split('__')
    if len(parts) != 3 or parts[0] not in {s['id'] for s in STRATEGIES} or (parts[1], parts[2]) not in SYMMETRY:
        raise ValueError(f'bad combo {cid!r}')
    return parts[0], parts[1], parts[2]


def expand_combos(spec):
    if spec in (None, '', 'all'):
        return combo_ids()
    out = []
    for item in spec.split(','):
        item = item.strip()
        if not item:
            continue
        if '__' in item:
            parse_combo(item)
            out.append(item)
        else:
            out += [c for c in combo_ids() if c.startswith(item + '__')]
    return list(dict.fromkeys(out))


def sections_dir(set_name, job, race, module, out=None):
    """Folder of a module's body sections (body_sections.py --fast). Keyed by JOB since 2026-11-19: `<set>__<job>__<race>__<module>` (two jobs of one set / race / module
    used to overwrite each other's sections under `<set>__<race>__<module>`). The old name is still READ when it holds a record of this job (existing single-job modules
    keep working; their next body_sections run writes the new name); a record of another job under the old name is never used. serve.py uses this too."""
    out = SECTIONS_OUT if out is None else Path(out)
    new = out / f'{set_name}__{job}__{race}__{module}'
    if (new / 'body_sections.json').is_file():
        return new
    old = out / f'{set_name}__{race}__{module}'
    try:
        rec = json.loads((old / 'body_sections.json').read_text())
    except (OSError, ValueError):
        return new
    return old if f'/{job}/' in f'/{rec.get("module", "")}/' else new


def module_paths(module_dir):
    m = Path(module_dir).resolve()
    set_name, job, race, module = identity(m)
    sec_out = sections_dir(set_name, job, race, module)
    return {'module': m, 'set': set_name, 'job': job, 'race': race, 'name': module, 'rel': f'{set_name}/{job}/{race}/{module}',
            'aligned': m / 'reduced' / f'lod_error_{LV}__wearable_aligned.glb', 'manifest': m / 'reduced' / 'manifest.json',
            'fitted': m / 'reduced' / f'lod_error_{LV}__wearable_aligned_fitted.glb',
            'dir': m / 'fitlab' / f'lod_{LV}', 'sec_out': sec_out, 'sec_json': sec_out / 'body_sections.json', 'drawn': sec_out / 'drawn_body.npz'}


def stamp(path):
    try:
        st = Path(path).stat()
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return None


def sections_status(P):
    """(ok, why) of the module's body sections record for the fit lab."""
    try:
        rec = json.loads(P['sec_json'].read_text())
    except (OSError, ValueError):
        return False, 'no record'
    if rec.get('states_only'):
        return False, 'states-only record'
    if f'/{P["job"]}/' not in f'/{rec.get("module", "")}/':
        return False, 'record is for another job'
    if not P['drawn'].is_file() or not rec.get('drawn_body_npz'):
        return False, 'no drawn_body.npz (pre-fast record)'
    base = EXP / str(rec.get('sectioned_base_glb', ''))
    if not base.is_file() or not (P['sec_out'] / str(rec.get('connectors_glb', 'connectors.glb'))).is_file():
        return False, 'sectioned base / connectors missing'
    a, r = stamp(P['aligned']), stamp(P['sec_json'])
    if a and r and a[0] > r[0]:
        return False, 'aligned wearable is newer than the record'
    return True, 'fresh'


def ensure_sections(module_dir, log=print):
    """Run body_sections --fast for the module when needed. Returns seconds spent (0 when fresh)."""
    P = module_paths(module_dir)
    ok, why = sections_status(P)
    if ok:
        return 0.
    t0 = time.monotonic()
    log(f'FIT_LAB sections {P["rel"]}: running body_sections --fast ({why})')
    sys.path.insert(0, str(EXP))
    import body_sections as bs   # noqa: E402
    bs.run_module(P['module'], {'ndir': bs.bm.PARAMS['ndir'], 'tuck_max_mm': 40., 'sections': {'band_cm': bs.SECTION_PARAMS['band_cm']}}, do_pose=False, layouts=('fine',), fast=True)
    ok, why = sections_status(module_paths(module_dir))      # the record may now sit in the job-keyed folder: resolve the paths again
    if not ok:
        raise RuntimeError(f'body sections did not produce a usable record: {why}')
    return round(time.monotonic() - t0, 1)


def requested_params(strategy, sym, over=None):
    d = dict(DEFAULTS, **(over or {}))
    p = {'margin_pct': d['margin_pct']}
    if strategy == 'current':
        p.update({'kappa': bf.KAPPA, 'iters': bf.SMOOTH_ITERS})
    elif strategy in SECTION_STRATEGIES:
        p.update({k: d[k] for k in ('kappa', 'iters', 'rim_ramp_pct', 'rim_max_mm', 'deep_mm', 'rim_release_mm', 'rim_release_reach_mm', 'rim_release_parts', 'deep_reg0_mm', 'deep_reg1_mm', 'deep_reg_iters', 'poke_cap_mm', 'poke_ignore_mm', 'guard_flare_mm', 'runaway_attempts', 'seed_current', 'seed_part_cm2', 'seed_reach_mm', 'seed_fade_iters', 'hidden_poke_cap_mm', 'clean_cm2', 'clean_frac', 'guard_score_full', 'free_w', 'rounds', 'cleanup')})
    if sym != 'off':
        p['sym_ramp_pct'] = d['sym_ramp_pct']
    return p


def input_stamps(P, strategy, sym):
    s = {'aligned': stamp(P['aligned']), 'manifest': stamp(P['manifest']), 'base': stamp(bf.BASE_CACHE / f'{P["race"]}.npz')}
    if strategy == 'current' and sym == 'off':
        s['fitted'] = stamp(P['fitted'])
    if strategy in SECTION_STRATEGIES:
        s['sections'] = stamp(P['sec_json'])
        s['drawn'] = stamp(P['drawn'])
    return s


def read_index(P):
    try:
        return json.loads((P['dir'] / 'index.json').read_text())
    except (OSError, ValueError):
        return {}


def combo_file(P, cid):
    return P['dir'] / f'{cid}.glb'


def entry_fresh(P, idx, cid, over=None):
    """The cached entry of a combo is usable: file exists and code version, params and input stamps match."""
    strategy, sym, scope = parse_combo(cid)
    e = (idx.get('combos') or {}).get(cid)
    if not e or e.get('code_version') != CODE_VERSION or not combo_file(P, cid).is_file():
        return False
    return e.get('params') == requested_params(strategy, sym, over) and e.get('inputs') == input_stamps(P, strategy, sym)


def cached_urls(P, idx=None):
    """combo id -> file path of every fresh cached combo (the aligned GLB stands in for original__off__none)."""
    idx = read_index(P) if idx is None else idx
    out = {'original__off__none': P['aligned']} if P['aligned'].is_file() else {}
    for cid in combo_ids():
        if entry_fresh(P, idx, cid):
            out[cid] = combo_file(P, cid)
    return out


class IndexLock:
    """Tiny cross-process lock file around the read-modify-write of index.json (parallel workers / server)."""

    def __init__(self, folder):
        self.path = Path(folder) / '.index.lock'

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(600):
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 120:
                        self.path.unlink()
                except OSError:
                    pass
                time.sleep(0.1)
        raise TimeoutError(f'index lock {self.path}')

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


def update_index(P, patch):
    with IndexLock(P['dir']):
        idx = read_index(P)
        idx.update({'lod': LV, 'module': P['rel'], 'code_version': CODE_VERSION})
        for k, v in patch.items():
            if k == 'combos':
                idx.setdefault('combos', {}).update(v)
            else:
                idx[k] = v
        bf.write_json(P['dir'] / 'index.json', idx)
    return idx


# ----------------------------------------------------------------------------------------------- body: sections, mirror plane

def _bm():
    sys.path.insert(0, str(EXP))
    import bm_core
    return bm_core


class SectionBody(_bm().MaskedBody):
    """The body AS DRAWN (shown sections, tucked connector meshes, hidden sections, discarded remainder; drawn_body.npz) as one closed mesh for the winding
    number sign. Per closest triangle `kind` (0 shown, 1 hidden, 2 connector, 3 discarded): drawn triangles constrain with the margin, hidden ones with
    zero margin when `fill_margin` is set (d + margin is what the fit sees), discarded ones and hidden ones otherwise impose nothing (d = +1 m)."""

    def __init__(self, V, F, part, part_names, H, o, kind):
        super().__init__(V, F, part, part_names, H, o, np.ones(len(F), bool))
        assert len(self.F) == len(kind)
        self.kind = np.asarray(kind, np.int64)
        self.fill_margin = None
        self.full, self.deep, self.margin = None, None, None   # the whole closed game base (bf.Body) and the burial limit (m) of the "deep" rule, see measure
        self.deep_vec = None                                   # optional per-vertex burial limit (m; negative = stay that far OUTSIDE the game surface), used when len(P) == len(deep_vec)

    def _clusters(self, cell=0.05, theta=3.0):
        """Spatial clusters of triangles for the approximate winding number: area vector, centre and radius per cell."""
        key = np.floor((self.cent - self.lo) / cell).astype(np.int64)
        _, cid = np.unique(key, axis=0, return_inverse=True)
        cid = cid.ravel()
        order = np.argsort(cid, kind='stable')
        cid_s = cid[order]
        starts = np.r_[0, np.flatnonzero(cid_s[1:] != cid_s[:-1]) + 1]
        counts = np.diff(np.r_[starts, len(order)])
        n = len(starts)
        S = np.zeros((n, 3))
        c = np.zeros((n, 3))
        ar = np.zeros(n)
        cl = cid_s
        for a in range(3):
            S[:, a] = np.bincount(cl, weights=(self.tri_n * self.area[:, None])[order][:, a], minlength=n)
            c[:, a] = np.bincount(cl, weights=(self.cent * self.area[:, None])[order][:, a], minlength=n)
        ar = np.bincount(cl, weights=self.area[order], minlength=n)
        c /= np.maximum(ar, 1e-30)[:, None]
        r = np.zeros(n)
        for X in (self.A, self.B, self.C):
            np.maximum.at(r, cl, np.linalg.norm(X[order] - c[cl], axis=1))
        self._cl = {'order': order, 'starts': starts, 'counts': counts, 'S': S, 'c': c, 'r': r, 'theta': theta}
        return self._cl

    def winding(self, P, chunk=80):
        """Generalized winding number (as Body.winding) with far triangle clusters replaced by their area-vector dipole (error ~ (radius / distance)^2 of a tiny
        solid angle, far below the 0.5 threshold); clusters within `theta` radii are summed triangle by triangle. About 10x faster than the exact sum."""
        P = np.asarray(P, np.float64)
        out = np.zeros(len(P))
        box = np.all((P >= self.lo) & (P <= self.hi), axis=1)
        idx = np.flatnonzero(box)
        cl = getattr(self, '_cl', None) or self._clusters()
        for s in range(0, len(idx), chunk):
            ii = idx[s:s + chunk]
            p = P[ii]
            d = cl['c'][None] - p[:, None, :]                              # (n, C, 3)
            dist = np.linalg.norm(d, axis=2)
            near = dist <= cl['theta'] * cl['r'][None] + 1e-9
            far = ~near
            tot = (np.einsum('nck,ck->nc', d, cl['S']) / np.maximum(dist, 1e-12) ** 3 * far).sum(1) / (4 * np.pi)
            pi, ci = np.nonzero(near)
            cnt = cl['counts'][ci]
            pr = np.repeat(pi, cnt)
            off = np.arange(int(cnt.sum())) - np.repeat(np.cumsum(cnt) - cnt, cnt)
            ti = cl['order'][np.repeat(cl['starts'][ci], cnt) + off]
            a, b, c = self.A[ti] - p[pr], self.B[ti] - p[pr], self.C[ti] - p[pr]
            la, lb, lc = (np.linalg.norm(x, axis=1) for x in (a, b, c))
            num = np.einsum('ki,ki->k', a, np.cross(b, c))
            den = la * lb * lc + np.einsum('ki,ki->k', a, b) * lc + np.einsum('ki,ki->k', b, c) * la + np.einsum('ki,ki->k', c, a) * lb
            tot += np.bincount(pr, weights=np.arctan2(num, den) * 2 / (4 * np.pi), minlength=len(p))
            out[ii] = tot
        return out

    def measure(self, P, inside_test=None):
        dist, tri, bary = self.closest(P)
        inside = self.winding(P) > .5
        n = np.einsum('ki,kij->kj', bary, self.vn[self.F[tri]])
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        n = np.where(ln > 1e-9, n / np.maximum(ln, 1e-30), self.tri_n[tri])
        d = np.where(inside, -dist, dist)
        k = self.kind[tri]
        eff = np.full(len(d), 1.0)
        drawn = (k == 0) | (k == 2)
        eff[drawn] = d[drawn]
        if self.fill_margin is not None:
            h = k == 1
            eff[h] = d[h] + self.fill_margin
        part = self.part[tri]
        if self.full is not None and self.deep is not None:
            # Deep rule (2026-11-19): what the sections let a garment ignore (the discarded remainder of a connector section, hidden sections in `tight`) only holds for a garment that
            # is tucked UNDER the body's visible surface by about a connector's tuck. A vertex buried deeper than `deep` in the WHOLE closed game base is a misfit, not a covered
            # section (gnome / orc / dwarf garments sit 50-200 mm inside the body): it must come up to `deep` below the game surface, with that surface's sign and normal (a mixed
            # drawn mesh has no consistent inside deep in the body, and unconstrained neighbours of pushed vertices tore the garment). need = -deep - d_full, continuous at the gate.
            free = (k == 3) | ((k == 1) & (self.fill_margin is None))
            if free.any():
                dv = self.deep_vec if (self.deep_vec is not None and len(self.deep_vec) == len(P)) else np.full(len(P), self.deep)
                mf = self.full.measure(P[free])
                t = dv[free]
                g = mf['d'] < -t
                ii = np.flatnonzero(free)[g]
                eff[ii] = mf['d'][g] + t[g] + (self.margin if self.margin is not None else 0.)
                n[ii] = mf['n'][g]
                part[ii] = mf['part'][g]
        return {'d': eff, 'n': n, 'part': part, 'tri': tri, 'kind': k, 'd_raw': d}


class SectionPokeCtx(bf.PokeCtx):
    """Reverse test (body through garment) restricted to the body samples of the given section kinds. Hidden samples (kind 1) count with zero margin:
    their depth is reduced by the margin, because the fit lifts by depth + margin."""

    def __init__(self, body, window, margin, kinds):
        scene = bf.scene_for(body, [window])
        smp = bf.body_samples(body)
        near = np.flatnonzero(np.all((smp['P'] >= window.min(0) - bf.POKE_OUT) & (smp['P'] <= window.max(0) + bf.POKE_OUT), axis=1))
        k = body.kind[smp['tri'][near]]
        keep = np.isin(k, kinds)
        for key in ('P', 'N', 'area', 'part', 'exit'):
            setattr(scene, key, getattr(scene, key)[keep])
        scene.hidden = k[keep] == 1
        if getattr(body, 'full', None) is not None:
            scene.body = body.full         # the poke test's 'hit point is clear of the other body surfaces' check: the real game base (527 triangles), not the 11k-triangle drawn mesh (6x faster)
        super().__init__(scene)
        self.margin = margin
        self.hidden_zero = True

    def measure(self, surf, U):
        pk = super().measure(surf, U)
        h = self.scene.hidden[pk['sample']]
        pk['depth_raw'] = pk['depth']
        pk['hidden'] = h
        pk['depth'] = pk['depth'] - self.margin * h if self.hidden_zero else pk['depth']
        return pk


def window_of(U, pad=0.2):
    return np.array([U.min(0) - pad, U.max(0) + pad])


_PLANES = {}


def mirror_plane(race, body):
    """(x0, left_sign): the base's mirror plane x = x0 (fitted: the reflection of the surface onto itself is best) and the sign of (x - x0) of the anatomical left."""
    if race in _PLANES:
        return _PLANES[race]
    names = body.part_names
    c = {}
    for side in ('l', 'r'):
        idx = [i for i, n in enumerate(names) if n.endswith('.' + side)]
        c[side] = body.V[np.unique(body.F[np.isin(body.part, idx)])].mean(0)[0]
    sign = 1. if c['l'] > c['r'] else -1.
    rng = np.random.default_rng(0)
    sel = body.V[rng.choice(len(body.V), min(1500, len(body.V)), replace=False)]

    def err(x0):
        R = sel.copy()
        R[:, 0] = 2 * x0 - R[:, 0]
        return float(body.closest(R)[0].mean())
    lo, hi = -0.01, 0.01
    for _ in range(12):
        m1, m2 = lo + (hi - lo) / 3, hi - (hi - lo) / 3
        lo, hi = (lo, m2) if err(m1) < err(m2) else (m1, hi)
    x0 = (lo + hi) / 2
    if err(0.) <= err(x0) * 1.02 or abs(x0) < 5e-4:
        x0 = 0.
    _PLANES[race] = (x0, sign, err(x0))
    return _PLANES[race]


# ----------------------------------------------------------------------------------------------- garment: rims, symmetry

def graph_distance(surf, sources):
    """Distance along the garment's welded edges from the vertices `sources` (index array) to every welded vertex."""
    e = np.unique(surf.edges(), axis=0)
    w = np.linalg.norm(surf.U[e[:, 0]] - surf.U[e[:, 1]], axis=1) + 1e-9
    G = sparse.coo_matrix((np.r_[w, w], (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(surf.K, surf.K)).tocsr()
    if not len(sources):
        return np.full(surf.K, np.inf)
    return csgraph.dijkstra(G, directed=False, indices=np.asarray(sources), min_only=True)


def rim_vertices(surf):
    """Welded vertices on open boundary loops (edges used by exactly one triangle)."""
    T = surf.Tw
    e = np.sort(np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]]), axis=1)
    e = e[e[:, 0] != e[:, 1]]
    u, cnt = np.unique(e, axis=0, return_counts=True)
    return np.unique(u[cnt == 1])


def rim_loops(surf):
    """Open boundary loops of the welded garment: (rim vertex ids (sorted, = rim_vertices), loop id per rim vertex, boundary edges (E, 2) of welded ids)."""
    T = surf.Tw
    e = np.sort(np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]]), axis=1)
    e = e[e[:, 0] != e[:, 1]]
    u, cnt = np.unique(e, axis=0, return_counts=True)
    be = u[cnt == 1]
    rims = np.unique(be)
    if not len(rims):
        return rims, np.zeros(0, np.int64), be
    G = sparse.coo_matrix((np.ones(len(be)), (be[:, 0], be[:, 1])), shape=(surf.K, surf.K))
    _, comp = csgraph.connected_components(G, directed=False)
    return rims, np.unique(comp[rims], return_inverse=True)[1].ravel(), be


def snap_rim_offsets(d, n, loop, be_local, margin, o):
    """Per rim vertex the offset s (m, along the outward body normal n; > 0 out, < 0 in) that puts it `margin` outside the whole game body. `d` = signed distance of the rim vertices to the
    whole body (outside +). Every rim vertex buried less than `rim_snap_max_mm` comes out to the margin. On a TIGHT loop (median gap <= `rim_snap_tight_mm`: collars, cuffs, straps,
    waistbands) a vertex floating up to `rim_snap_pull_mm` off the skin is pulled in to the margin; a loose loop (skirt hem, flared sleeve) is never pulled. The offsets are smoothed along
    the loop (`rim_snap_smooth` neighbour averages, never below the margin floor, never pulling a loose loop) so neighbouring rim vertices move together. -> (s, n_smoothed, stats)."""
    cap, tight_m, pull_m = o['rim_snap_max_mm'] / 1000, o['rim_snap_tight_mm'] / 1000, o['rim_snap_pull_mm'] / 1000
    nl = int(loop.max()) + 1 if len(loop) else 0
    med = np.zeros(nl)
    for li in range(nl):
        med[li] = np.median(d[loop == li])
    tight = med[loop] <= tight_m
    deep = d <= -cap                                  # too deep to be an opening that hugs the skin: left to the fit's release / deep rules
    lower = np.where(deep, 0., margin - d)              # never end inside the margin
    lower = np.where(tight, lower, np.maximum(lower, 0.))      # a loose loop is never pulled in
    s = np.zeros(len(d))
    push = (d < margin) & ~deep
    pull = tight & (d > margin) & (d <= pull_m)
    s[push | pull] = margin - d[push | pull]
    if len(be_local):
        a, b = be_local[:, 0], be_local[:, 1]
        nn = n.copy()
        for _ in range(int(o['rim_snap_smooth'])):
            acc, cnt = s.copy(), np.ones(len(s))
            np.add.at(acc, a, s[b])
            np.add.at(acc, b, s[a])
            np.add.at(cnt, a, 1.)
            np.add.at(cnt, b, 1.)
            s = np.maximum(acc / cnt, lower)
            s = np.where(deep, 0., np.where(tight, s, np.maximum(s, 0.)))
            accn = nn.copy()
            np.add.at(accn, a, nn[b])
            np.add.at(accn, b, nn[a])
            nn = accn / np.maximum(np.linalg.norm(accn, axis=1, keepdims=True), 1e-12)
        n = nn
    stats = {'rim_vertices': int(len(d)), 'loops': int(nl), 'tight_loops': int((med <= tight_m).sum()), 'pushed': int(push.sum()), 'pulled': int(pull.sum()), 'too_deep': int(deep.sum()),
             'buried_before_pct': float(100 * (d < 0).mean()) if len(d) else 0., 'max_out_mm': float(max(s.max(), 0.) * 1000) if len(s) else 0., 'max_in_mm': float(max(-s.min(), 0.) * 1000) if len(s) else 0.}
    return s, n, stats


def rim_weights(surf, rims, ramp_m):
    if not len(rims):
        return np.ones(surf.K)
    return np.clip(graph_distance(surf, rims) / ramp_m, 0., 1.)


def regional_deep(surf, depth, deep, margin, r0, r1, iters):
    """Per-vertex limit `t` (m, below the game surface; negative = outside by that much) of the deep rule. A garment that is buried 40-200 mm over a whole REGION (gnome / dwarf chest)
    is a misfit, not a covered tuck: it has to come out to the surface + margin like the whole-body fit does, otherwise its vertices sit at different depths and the strap / panel
    shapes shear. `depth` (K, m, >= 0) is diffused over the welded edges (`iters` averaging steps), the regional mean blends the limit from `deep` (below r0) to -margin (above r1)."""
    S = bf.smoothing_operator(surf, 0.)
    reg = np.maximum(np.asarray(depth, np.float64), 0.)
    for _ in range(int(iters)):
        reg = S @ reg
    b = np.clip((reg - r0) / max(r1 - r0, 1e-9), 0., 1.)
    return deep * (1 - b) - margin * b, reg, b


def closest_on_soup(P, A, B, C, chunk=150):
    """Closest point on the triangle soup (A, B, C) to every point of P (brute force, the garments are small)."""
    out = np.empty_like(P)
    nT = len(A)
    for s in range(0, len(P), chunk):
        p = P[s:s + chunk]
        n = len(p)
        pp = np.repeat(p, nT, axis=0)
        a, b, c = np.tile(A, (n, 1)), np.tile(B, (n, 1)), np.tile(C, (n, 1))
        bb = bf.tri_closest(pp, a, b, c)
        q = bb[:, :1] * a + bb[:, 1:2] * b + bb[:, 2:] * c
        dd = np.linalg.norm(pp - q, axis=1).reshape(n, nT)
        j = dd.argmin(1)
        out[s:s + n] = q.reshape(n, nT, 3)[np.arange(n), j]
    return out


def symmetrize(surf, body, race, U, sym, scope, ramp_m):
    """Positions U (K,3) with the target side's in-scope vertices moved onto the reflected source side (weight ramps 0 -> 1 over ramp_m). Topology untouched."""
    if sym == 'off':
        return U.copy(), {}
    x0, left, err = mirror_plane(race, body)
    src = left if sym == 'l2r' else -left                       # sign of (x - x0) of the source side
    sd = (U[:, 0] - x0) * src
    eps = 1e-3
    target = sd < -eps
    tri_c = U[surf.Tw].mean(1)
    src_tris = (tri_c[:, 0] - x0) * src > 0
    if scope == 'limbs':
        part = body.measure(U)['part']
        limb = np.array([n.split('.')[0] in LIMB_PARTS for n in body.part_names])[part]
        in_scope = target & limb
        dist = graph_distance(surf, np.flatnonzero(~in_scope))
    else:
        in_scope = target
        dist = np.abs(U[:, 0] - x0)
    wb = np.where(in_scope, np.clip(dist / ramp_m, 0., 1.), 0.)
    R = U.copy()
    R[:, 0] = 2 * x0 - R[:, 0]
    T = surf.Tw[src_tris]
    out = U.copy()
    idx = np.flatnonzero(in_scope & (wb > 0))
    if len(idx) and len(T):
        q = closest_on_soup(U[idx], R[T[:, 0]], R[T[:, 1]], R[T[:, 2]])
        out[idx] = U[idx] + wb[idx, None] * (q - U[idx])
    mv = np.linalg.norm(out - U, axis=1) * 1000
    return out, {'plane_x0_m': x0, 'left_sign': left, 'plane_asymmetry_mean_mm': err * 1000, 'vertices_in_scope': int(in_scope.sum()), 'vertices_moved': int((mv > .05).sum()),
                 'max_move_mm': float(mv.max()), 'mean_move_mm': float(mv[mv > .05].mean()) if (mv > .05).any() else 0.}


# ----------------------------------------------------------------------------------------------- local pinned fit

def fit_local(surf, body, margin, w, ctx, U0, kappa, iters, rounds, cleanup, rim_max, free_w, tol=bf.TOL, depth0=None, stall_states=3, poke_cap=None, poke_ignore=None, vcap=None, runaway=None, hidden_cap=None, rim_lift=None):
    """body_fit.fit_push with pinned openings: smooth lower-bounded displacement field D (screened Laplacian, `kappa`, `iters`) whose vertices on the pinned
    rims (w = 0) stay put and whose magnitude is bounded by w * rim_max (w ramps 0 -> 1 away from the rims), so a vertex near an opening can only move a little
    and the garment flares out gradually instead of tearing away from the rim. `body.measure` gives the margin-adjusted signed distance (SectionBody), `ctx` the
    reverse test (SectionPokeCtx). Convergence is judged on the vertices / pokes with w >= free_w. Returns (D (K,3), info).

    `depth0` (K,) = how far each vertex starts inside the WHOLE game body (m, >= 0; 0 = clear of it). Both absolute caps scale with it (2026-11-19, the gnome / orc / dwarf
    garments sit 50-200 mm inside the body: a fixed 120 mm displacement cap and body_fit's fixed POKE_CAP of 150 mm along the push left them inside the connectors):
    |D| <= w * (rim_max + depth0), and a poke lifts a vertex by at most depth0 + POKE_CAP + margin along its direction. Far from the openings a deep vertex may therefore
    travel as far as it is deep; near a rim (w -> 0) it is still held. The loop stops early when `stall_states` successive states bring no progress (a fit that cannot
    converge used to burn all rounds + clean-up iterations, ~1000 s per gnome module).

    `rim_lift` (m, rim snap on): the pinned rims no longer follow the smoothing (they hold their snapped positions) but take the explicit lifts of the constraints - the margin and
    the poke of the triangles along the opening (a big flat rim triangle whose corners sit on the skin still cuts the curved body between them) - up to `rim_lift`. None = pinned
    rims never move (the behaviour before the snap)."""
    K = surf.K
    S = bf.smoothing_operator(surf, kappa)
    pinned = w <= 0
    free = w >= free_w
    d0 = np.zeros(K) if depth0 is None else np.maximum(np.asarray(depth0, np.float64), 0.)
    lim = w * (rim_max + d0)
    held = pinned.copy()                                             # vertices the smoothing does not move
    if rim_lift is not None:
        lim = np.where(pinned, rim_lift, lim)
        pinned = np.zeros(K, bool)                                   # ... but the constraints may lift them (bounded by lim)
    if vcap is not None:
        lim = np.minimum(lim, vcap)                                  # released rim vertices: only as far as needed to clear the skin (+ a little)
    pcap = (bf.POKE_CAP if poke_cap is None else poke_cap) + margin + d0
    pign = np.full(K, np.inf) if poke_ignore is None else poke_ignore + margin + d0      # a poke demanding more than this is not acted on (reported only)
    pfirst = np.full(K, np.nan)                                      # the first lift a poke demanded of each vertex; `runaway` = (factor, extra m) bounds the TOTAL poke lift to factor * first + extra
    D = np.zeros((K, 3))
    act, pact = np.zeros(K, bool), np.zeros(K, bool)
    nrm, pn = np.zeros((K, 3)), np.zeros((K, 3))
    tgt, pt = np.zeros(K), np.zeros(K)
    info = {'rounds': 0, 'converged': False, 'cleanup_iterations': 0}
    prog = {'best': None, 'stall': 0}
    hidden_flag = [np.zeros(K, bool)]

    def stalled():
        """True after `stall_states` consecutive states that improved neither the residual nor the poke area by 3 %."""
        cur = (info['residual_mm'], info['poke_residual']['area_cm2'])
        b = prog['best']
        if b is None or cur[0] < b[0] * .97 - 1e-6 or cur[1] < b[1] * .97 - 1e-6:
            prog['best'] = cur if b is None else (min(b[0], cur[0]), min(b[1], cur[1]))
            prog['stall'] = 0
        else:
            prog['stall'] += 1
        return prog['stall'] >= stall_states

    def pcap_now(pv, pneed_):
        """Per-vertex cap of the cumulative poke lift. A poke that survives its lift (a big triangle across a shoulder, a sample on a slanted surface) used to ratchet the vertex up round
        after round until the generic cap (orc neck / shoulder: 138 mm spike for a first demand of ~30 mm)."""
        new = pv & np.isnan(pfirst)
        pfirst[new] = pneed_[new]
        cap = pcap
        if hidden_cap is not None:
            cap = np.where(hidden_flag[0], np.minimum(cap, hidden_cap + margin), cap)
        if runaway is None:
            return cap
        return np.minimum(cap, np.where(np.isnan(pfirst), np.inf, pfirst * runaway[0] + runaway[1]))

    def clamp(x):
        mag = np.linalg.norm(x, axis=1)
        x *= np.minimum(1., lim / np.maximum(mag, 1e-12))[:, None]
        return x

    def state(D_):
        m = bf.measure_surface(surf, U0 + D_, body)
        need = margin - m['d']
        pk = ctx.measure(surf, U0 + D_)
        pneed, pdir = bf.poke_vertex_need(surf, U0 + D_, pk, margin)
        hid_only = np.zeros(K, bool)
        if hidden_cap is not None and len(pk['depth']) and pk['hidden'].any():      # vertices whose poke demand comes only from HIDDEN sections (covered by other body parts): their lift is capped
            pd_, _ = bf.poke_vertex_need(surf, U0 + D_, bf.poke_select(pk, ~pk['hidden']), margin)
            hid_only = (pneed > 0) & (pd_ + 1e-9 < pneed)
        hidden_flag[0] = hid_only
        fx = w[surf.Tw[pk['tri']]].max(1) >= free_w if len(pk['depth']) else np.zeros(0, bool)
        pkf = {'depth': pk['depth_raw'][fx], 'area': pk['area'][fx]}
        info['residual_mm'] = float(max(0., need[free].max()) * 1000) if free.any() else 0.
        info['poke_residual'] = {'area_cm2': float(pkf['area'].sum() * 1e4), 'max_depth_mm': float(pkf['depth'].max() * 1000) if len(pkf['depth']) else 0.}
        ok = (need[free].max() <= tol if free.any() else True) and bf.poke_ok(pkf)
        return m, need, pneed, pdir, ok

    for r in range(rounds + 1):
        m, need, pneed, pdir, ok = state(D)
        info['rounds'] = r
        if ok:
            info['converged'] = True
            break
        if r == rounds:
            break
        if stalled():
            info['stalled'] = 'rounds'
            prog['stall'] = 0                      # the hard projections of the clean-up get their own chance
            break
        viol = (need > 0) & ~pinned
        nrm[viol] = m['n'][viol]
        tgt[viol] = bf._dot(D[viol], m['n'][viol]) + need[viol]
        act |= viol
        pv = (pneed > 0) & (pneed <= pign) & ~pinned
        pn[pv] = pdir[pv]
        pc = pcap_now(pv, pneed)
        pt[pv] = np.minimum(bf._dot(D[pv], pdir[pv]) + pneed[pv], pc[pv])
        pact |= pv
        x = D
        for _ in range(iters):
            xs = S @ x
            xs[held] = 0.                                            # held rims: no diffusion; their lifts are re-applied by the projections below
            x = xs
            short = np.maximum(tgt[act] - bf._dot(x[act], nrm[act]), 0.)
            x[act] += short[:, None] * nrm[act]
            if pact.any():
                short = np.maximum(pt[pact] - bf._dot(x[pact], pn[pact]), 0.)
                x[pact] += short[:, None] * pn[pact]
            x = clamp(x)
        D = x
    for it in range(cleanup):
        if info['converged']:
            break
        m, need, pneed, pdir, ok = state(D)
        if ok:
            info['converged'] = True
            break
        if stalled():
            info['stalled'] = 'cleanup'
            break
        viol = (need > tol * .6) & ~pinned
        pv = (pneed > 0) & (pneed <= pign) & ~pinned if it < bf.POKE_CLEANUP else np.zeros(K, bool)
        info['cleanup_iterations'] = it + 1
        if not viol.any() and not pv.any():
            break
        D[viol] += (need[viol] * 1.05)[:, None] * m['n'][viol]
        room = np.maximum(pcap_now(pv, pneed)[pv] - bf._dot(D[pv], pdir[pv]), 0.)
        D[pv] += np.minimum(pneed[pv] * 1.05, room)[:, None] * pdir[pv]
        act |= viol
        nrm[viol] = m['n'][viol]
        tgt[viol] = bf._dot(D[viol], m['n'][viol])
        pact |= pv
        pn[pv] = pdir[pv]
        pt[pv] = bf._dot(D[pv], pdir[pv])
        D = clamp(D)
    else:
        info['converged'] = state(D)[4]
    if info.get('stalled') == 'cleanup':
        info['converged'] = state(D)[4]
    return D, info


# ----------------------------------------------------------------------------------------------- module context

class Lab:
    """Everything loaded once per module: manifest, game body, aligned surface, sections (drawn body), rims, baseline measurements."""

    def __init__(self, module_dir, over=None):
        self.P = P = module_paths(module_dir)
        self.over = dict(DEFAULTS, **(over or {}))
        self.manifest = json.loads(P['manifest'].read_text(encoding='utf-8-sig'))
        self.body = bf.get_body(P['race'])
        self.margin = self.over['margin_pct'] / 100 * self.body.H
        self.surf = bf.Surface(bf.Glb(P['aligned']), self.manifest, self.body)
        depth = np.maximum(-self.body.measure(self.surf.U)['d'], 0.)                # vertex depth inside the whole game body
        self.reach = float(max(REACH_DEFAULT, np.percentile(depth, 99) + REACH_EXTRA)) if len(depth) else REACH_DEFAULT
        if bf.POKE_IN != self.reach:
            bf.POKE_IN = self.reach                                                  # process-wide (one module at a time per process); sample caches hold `exit` for the old reach
            self.body._samples = None
        self._sb = None
        self._rims = None
        self._baseline = None
        self.rims_released = 0
        self.rim_cap = None
        self._fit_after = None

    # lazily loaded pieces
    @property
    def sb(self):
        if self._sb is None:
            z = np.load(self.P['drawn'], allow_pickle=False)
            self._sb = SectionBody(z['V'], z['F'], z['part'], [str(n) for n in z['part_names']], float(z['H']), z['o'], z['kind'])
            self._sb.full, self._sb.margin = self.body, self.margin
            if self.over.get('deep_mm') is not None:
                self._sb.deep = self.over['deep_mm'] / 1000
        return self._sb

    @property
    def rims(self):
        if self._rims is None:
            self._rims = rim_vertices(self.surf)
        return self._rims

    def snap_rims(self, U, info):
        """Rim snap (2026-11-21): the openings no longer stay where the generator put them (35-77 % of the rim samples of the fitted garments sat inside the game body, and 100 % of the
        remaining drawn-body poke lay within one rim ramp of an opening, i.e. the pinned edges). Every rim vertex is moved to `margin` outside the WHOLE game body along the body normal at
        its closest point (`snap_rim_offsets`: buried ones out, floating ones of tight loops in, loose hems untouched), and the move is carried into the garment with the rim ramp's
        smooth falloff (graph distance to the nearest rim, then a few screened-Laplacian rounds with the rims held) so a buried collar band comes out with its edge instead of folding.
        The fit then pins the rims at these positions. Returns the snapped positions (U unchanged when `rim_snap` is off or there are no rims)."""
        o = self.over
        if not o.get('rim_snap'):
            return U
        if getattr(self, '_loops', None) is None:
            self._loops = rim_loops(self.surf)
        rims, loop, be = self._loops
        if not len(rims):
            return U
        m = self.body.measure(U[rims])
        pos = np.full(self.surf.K, -1, np.int64)
        pos[rims] = np.arange(len(rims))
        s, n, stats = snap_rim_offsets(m['d'], m['n'], loop, pos[be], self.margin, o)
        Drim = np.zeros((self.surf.K, 3))
        Drim[rims] = s[:, None] * n
        if not np.any(s):
            info['rim_snap'] = {**stats, 'moved': 0}
            return U
        e = np.unique(self.surf.edges(), axis=0)
        wgt = np.linalg.norm(U[e[:, 0]] - U[e[:, 1]], axis=1) + 1e-9
        G = sparse.coo_matrix((np.r_[wgt, wgt], (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(self.surf.K, self.surf.K)).tocsr()
        dist, _, src = csgraph.dijkstra(G, directed=False, indices=rims, min_only=True, return_predecessors=True)
        ramp = o['rim_snap_ramp_pct'] / 100 * self.body.H
        t = np.clip(np.where(np.isfinite(dist), dist, np.inf) / ramp, 0., 1.)
        fall = 1. - t * t * (3. - 2. * t)
        ok = src >= 0
        D = np.zeros((self.surf.K, 3))
        D[ok] = fall[ok, None] * Drim[src[ok]]
        S = bf.smoothing_operator(self.surf, 0.)                # plain neighbour average (no screening: it would shrink the band)
        near = fall > 0
        for _ in range(10):                                     # even out the seams where two rims' bands meet (both edges of a strap), rims held
            D[near] = (S @ D)[near]
            D[rims] = Drim[rims]
        D[~near] = 0.
        stats.update({'moved': int((np.linalg.norm(D, axis=1) > 1e-6).sum()), 'ramp_mm': ramp * 1000,
                      'params': {k: o[k] for k in ('rim_snap_max_mm', 'rim_snap_tight_mm', 'rim_snap_pull_mm', 'rim_snap_smooth', 'rim_snap_ramp_pct', 'rim_snap_lift_mm')}})
        info['rim_snap'] = stats
        return U + D

    def pinned_rims(self, U=None):
        """The rim vertices that stay pinned: those on open boundary loops EXCEPT the ones that start more than `rim_release_mm` inside a drawn surface (shown section or connector).
        A pinned opening is meant to keep a hem / cuff where the garment was authored; one that lies inside visible skin (orc / dwarf hems inside the feet connectors, sleeve cuffs
        inside the forearm) would stay buried there while the rest of the garment flares out around it, so it is let go and fitted like any other vertex (2026-11-19).
        `U` = the positions the fit starts from (the snapped rims, see snap_rims; default the aligned surface): after the snap only rims too deep to snap are still released."""
        rims = self.rims
        U = self.surf.U if U is None else U
        if not len(rims) or self.over.get('rim_release_mm') is None or not self.P['drawn'].is_file():
            return rims
        sb = self.sb
        old = sb.fill_margin
        sb.fill_margin = None
        m = sb.measure(U[rims])
        sb.fill_margin = old
        inside = np.isin(m['kind'], (0, 2)) & (m['d_raw'] < -self.over['rim_release_mm'] / 1000)
        self.rims_released = int(inside.sum())
        self.rim_cap = np.full(self.surf.K, np.inf)
        self.rim_cap[rims[inside]] = -m['d_raw'][inside] + self.margin + self.over['rim_release_reach_mm'] / 1000      # a released rim vertex may move at most its depth + margin + this
        rp = self.over.get('rim_release_parts')
        if rp:
            # An open shell under a bare foot connector (halfling / dwarf shoe soles) has its whole outline on the open boundary: every vertex is a rim and the fit could not lift the sole
            # (foot poke 100 -> 500 cm2, `current` 1 cm2). Rims whose closest part of the game base is a foot are let go too, bounded like the others (depth in the whole body + margin + reach).
            mf = self.body.measure(U[rims])
            names = np.array([n.split('.')[0] for n in self.body.part_names])
            extra = np.isin(names[mf['part']], rp) & ~inside
            self.rim_cap[rims[extra]] = np.maximum(-mf['d'][extra], 0.) + self.margin + self.over['rim_release_reach_mm'] / 1000
            inside = inside | extra
            self.rims_released = int(inside.sum())
        return rims[~inside]

    def weights(self, U=None):
        return rim_weights(self.surf, self.pinned_rims(U), self.over['rim_ramp_pct'] / 100 * self.body.H)

    # poke measurements (area cm2, max depth mm; raw depth, no margin)
    def poke(self, U):
        win = window_of(U)
        out = {}
        full = bf.PokeCtx(bf.scene_for(self.body, [win]))
        pk = full.measure(self.surf, U)
        out['full'] = _pk(pk, 'depth')
        if self.P['drawn'].is_file():
            for name, kinds in (('drawn', (0, 2)), ('hidden', (1,))):
                c = SectionPokeCtx(self.sb, win, self.margin, kinds)
                c.hidden_zero = False
                out[name] = _pk(c.measure(self.surf, U), 'depth')
        return out

    def poke_by_part(self, U, kinds=(0, 2)):
        """Drawn-body poke area (cm2) of the garment at U per base part name (`foot`, `spine_01`, ...; left / right merged)."""
        sb = self.sb
        c = SectionPokeCtx(sb, window_of(U), self.margin, kinds)
        c.hidden_zero = False
        pk = c.measure(self.surf, U)
        out = {}
        for p, a in zip(c.scene.part[pk['sample']], pk['area']):
            n = sb.part_names[p].split('.')[0]
            out[n] = out.get(n, 0.) + float(a) * 1e4
        return out

    def seed_parts(self):
        """Base parts on which the ORIGINAL garment already pokes the drawn body by at least `seed_part_cm2` (cached). A thin shell under a bare connector (halfling / dwarf shoe soles under the
        foot, a hem inside a thigh) pokes before any fit; the section fit pushes its vertices out of the volume in different directions and the poke grows 2-5x, whereas the whole-body fit
        lifts the whole shell as one offset surface (drawn poke ~1 cm2)."""
        if getattr(self, '_seed_parts', None) is None:
            thr = self.over.get('seed_part_cm2')
            self._seed_parts = sorted(n for n, a in self.poke_by_part(self.surf.U).items() if thr and a >= thr) if self.P['drawn'].is_file() else []
        return self._seed_parts

    def baseline(self):
        if self._baseline is None:
            self._baseline = self.poke(self.surf.U)
        return self._baseline

    def rim_report(self, U):
        """Rim vertices that end up inside a SHOWN (non-connector) section of the drawn body."""
        rims = self.rims
        out = {'rim_vertices': int(len(rims)), 'shown_poke_vertices': 0, 'max_depth_mm': 0., 'parts': []}
        if not len(rims) or not self.P['drawn'].is_file():
            return out
        sb = self.sb
        old = sb.fill_margin
        sb.fill_margin = None
        m = sb.measure(U[rims])
        sb.fill_margin = old
        bad = (m['kind'] == 0) & (m['d_raw'] < -5e-4)
        out['shown_poke_vertices'] = int(bad.sum())
        if bad.any():
            out['max_depth_mm'] = float(-m['d_raw'][bad].min() * 1000)
            out['parts'] = sorted({sb.part_names[i] for i in m['part'][bad]})
        return out

    # strategies -> final welded positions
    def fit_from(self, strategy, U_pre, info):
        """The fit of one strategy starting from positions U_pre (symmetrised or not); returns the fitted welded positions."""
        o = self.over
        if strategy == 'original':
            return U_pre
        if strategy == 'current':
            ctx = bf.PokeCtx(bf.scene_for(self.body, [window_of(np.concatenate([self.surf.U, U_pre]))]))
            disp, finfo = bf.fit_push(self.surf, self.margin, U0=U_pre, poke_fn=lambda X: ctx.measure(self.surf, X))
            info.setdefault('fit', []).append({k: finfo[k] for k in ('rounds', 'converged', 'cleanup_iterations') if k in finfo})
            return U_pre + disp
        sb = self.sb
        fill = strategy == 'sections_fill'
        sb.fill_margin = self.margin if fill else None
        kinds = (0, 1, 2) if fill else (0, 2)
        ctx = SectionPokeCtx(sb, window_of(np.concatenate([self.surf.U, U_pre])), self.margin, kinds)
        depth0 = np.maximum(self.margin - self.body.measure(U_pre)['d'], 0.)      # how far inside the WHOLE game body (closed, pseudonormal sign) each vertex starts
        w = self.weights(U_pre)
        if o.get('deep_mm') is not None and o.get('deep_reg1_mm'):
            dep = np.maximum(-self.body.measure(U_pre)['d'], 0.)
            sb.deep_vec, reg, bdeep = regional_deep(self.surf, dep, o['deep_mm'] / 1000, self.margin, o['deep_reg0_mm'] / 1000, o['deep_reg1_mm'] / 1000, o['deep_reg_iters'])
            info['deep_region_p90_mm'] = float(np.percentile(reg, 90) * 1000)
            if o.get('seed_current') and bdeep.max() > 0:
                # Where the garment is buried over a whole region, start from the whole-body fit's displacement there (a smooth offset of the body surface keeps straps / panels / belts in
                # shape), faded to nothing toward the pinned openings (weights `w`) so they stay unflared; the section constraints then refine it.
                U_pre = self.seed_toward_current(U_pre, bdeep * w)
                depth0 = np.maximum(self.margin - self.body.measure(U_pre)['d'], 0.)
                info['seeded_from_current'] = float((bdeep * w).mean())
        sp = self.seed_parts() if o.get('seed_current') else []
        if sp:
            names = np.array([n.split('.')[0] for n in self.body.part_names])
            vp = names[self.body.measure(U_pre)['part']]
            sel = np.isin(vp, sp).astype(np.float64)
            S0 = bf.smoothing_operator(self.surf, 0.)
            for _ in range(int(o['seed_fade_iters'])):                  # fade the seed out over a few rings around the poking parts
                sel = np.maximum(sel * 0.999, S0 @ sel)
            sel = np.clip(sel, 0., 1.)
            if sel.max() > 0:
                U_pre = self.seed_toward_current(U_pre, sel * w)
                depth0 = np.maximum(self.margin - self.body.measure(U_pre)['d'], 0.)
                info['seeded_parts'] = sp
                info['seeded_parts_share'] = float((sel * w).mean())
        base = self.baseline()
        best = None
        atts = []
        order = [list(x) for x in o['runaway_attempts']]
        if info.get('deep_region_p90_mm', 0.) >= o['deep_reg0_mm']:
            order = order[::-1]          # a garment buried over a whole region (gnome chest) needs the poke lifts to accumulate: the strict cap never passes there
        for ra, extra in order:
            t1 = time.monotonic()
            D, finfo = fit_local(self.surf, sb, self.margin, w, ctx, U_pre, o['kappa'], int(o['iters']), int(o['rounds']), int(o['cleanup']), o['rim_max_mm'] / 1000, o['free_w'], depth0=depth0, poke_cap=o['poke_cap_mm'] / 1000,
                                 poke_ignore=(o['poke_ignore_mm'] / 1000 if o.get('poke_ignore_mm') is not None else None), vcap=self.rim_cap, runaway=((ra, extra / 1000) if ra else None), hidden_cap=(o['hidden_poke_cap_mm'] / 1000 if o.get('hidden_poke_cap_mm') else None),
                                 rim_lift=(o['rim_snap_lift_mm'] / 1000 if o.get('rim_snap') else None))
            U1 = U_pre + D
            aft = self.poke(U1)
            b0 = base['drawn']['area_cm2']
            clean = aft['drawn']['area_cm2'] <= min(b0 * 1.1 + 2, max(b0 * o['clean_frac'], o['clean_cm2']))      # not worse than the original and, when the original pokes a lot, a real improvement
            atts.append({'runaway': [ra, extra], 'drawn_cm2': aft['drawn']['area_cm2'], 'full_cm2': aft['full']['area_cm2'], 'seconds': round(time.monotonic() - t1, 1), 'clean': bool(clean),
                         **{k: finfo[k] for k in ('rounds', 'converged', 'cleanup_iterations', 'stalled', 'residual_mm', 'poke_residual') if k in finfo}})
            if best is None or self.score(aft) < self.score(best[1]):
                best = (U1, aft, finfo)
            if clean:
                best = (U1, aft, finfo)
                break
        sb.fill_margin = None
        sb.deep_vec = None
        info['rims_released'] = self.rims_released
        info['attempts'] = atts
        info.setdefault('fit', []).append({k: best[2][k] for k in ('rounds', 'converged', 'cleanup_iterations', 'stalled', 'residual_mm', 'poke_residual') if k in best[2]})
        self._fit_after = best[1]
        return best[0]

    def run(self, strategy, sym, scope):
        """Returns (U_pre, U, info): the positions the (last) fit started from, the final welded positions, and a record. With a symmetry the fit result is
        mirrored once more (the fit is not exactly mirror-symmetric: the sections' states and the iteration differ per side; a second fit after it did not help). The mirrored
        side can therefore poke a little where its own constraints were stricter; the poke metrics show it."""
        info = {}
        self._fit_after = None
        t0 = time.monotonic()
        ramp = self.over['sym_ramp_pct'] / 100 * self.body.H
        U_pre, sinfo = symmetrize(self.surf, self.body, self.P['race'], self.surf.U, sym, scope, ramp)
        if sinfo:
            info['symmetry'] = sinfo
        if strategy in SECTION_STRATEGIES:
            U_pre = self.snap_rims(U_pre, info)              # openings onto the skin first; the fit pins them there (and the symmetric re-pin below uses these positions)
        fitted = self.P['fitted']
        if strategy == 'current' and sym == 'off' and fitted.is_file() and stamp(fitted)[0] >= stamp(self.P['aligned'])[0]:
            info['reused'] = 'reduced/' + fitted.name
            U = self.positions_of(fitted)
        else:
            U = self.fit_from(strategy, U_pre, info)
            if sym != 'off' and strategy != 'original':
                U, sinfo2 = symmetrize(self.surf, self.body, self.P['race'], U, sym, scope, ramp)
                info['symmetry_after_fit'] = sinfo2
                if strategy in SECTION_STRATEGIES and len(self.rims):      # the openings stay pinned
                    U[self.rims] = U_pre[self.rims]
        info['seconds_fit'] = round(time.monotonic() - t0, 2)
        return U_pre, U, info

    def seed_toward_current(self, U_pre, weight):
        """U_pre moved toward the whole-body fit by `weight` (K,), each vertex's step limited to its depth inside the whole body + margin + `seed_reach_mm`: `current` is a good smooth
        offset surface where a shell sits inside the body, but where it lifts a poking triangle by 150 mm (a halfling shoe sole turned into a 240 mm fan) its step would carry that into
        every section fit."""
        Uc = self.current_positions(U_pre)
        step = (Uc - U_pre) * weight[:, None]
        cap = np.maximum(self.margin - self.body.measure(U_pre)['d'], 0.) + self.margin + self.over['seed_reach_mm'] / 1000
        mag = np.linalg.norm(step, axis=1)
        return U_pre + step * np.minimum(1., cap / np.maximum(mag, 1e-12))[:, None]

    def current_positions(self, U_pre):
        """Positions of the whole-body fit (`current`, body_fit's cached `_aligned_fitted.glb` when fresh, else computed) for U_pre = the unfitted surface; cached per Lab."""
        if getattr(self, '_cur', None) is None:
            fitted = self.P['fitted']
            if fitted.is_file() and stamp(fitted)[0] >= stamp(self.P['aligned'])[0]:
                self._cur = self.positions_of(fitted)
            else:
                self._cur = self.fit_from('current', self.surf.U, {})
        return self._cur

    def flare(self, U):
        """Largest growth (m) of a vertex's distance outside the whole body: d_after - max(d_before, 0). A shoe sole lifted 150 mm by poke demands stands out as a fan (halfling pongo_keeper:
        `current` 207 mm); a legitimately big skirt does not grow at all, a deep garment ends at the surface (~0)."""
        if getattr(self, '_d0', None) is None:
            self._d0 = np.maximum(self.body.measure(self.surf.U)['d'], 0.)
        return float((self.body.measure(U)['d'] - self._d0).max())

    def score(self, a):
        """What a candidate costs in pokes: the drawn-body area (visible) + a share of the whole-body area (anything the body pushes through, hidden or not)."""
        return a['drawn']['area_cm2'] + self.over['guard_score_full'] * a['full']['area_cm2']

    def guard(self, U, after, base, U_pre, info, slack_pct=25., slack_cm2=10.):
        """A section strategy must never end worse than the garment it started from: the result stands when its drawn-body poke area is within `slack_pct` % (+ `slack_cm2`) of the
        original's. Otherwise the whole-body fit (`current`) is tried and the candidate with the lowest score (drawn + `guard_score_full` x whole-body poke area) of {section result, current,
        original} wins: a shoe sole too small for the bare foot connector or a thin garment shell pokes before and after, so the section result is kept when it is the best of the three (recorded in info['guard']). The metric is the fit lab's own (reach `Lab.reach`, so a garment buried deeper than body_fit's 120 mm counts too)."""
        b = base['drawn']['area_cm2']
        if after['drawn']['area_cm2'] <= b * (1 + slack_pct / 100) + slack_cm2:
            return U, after
        g = {'reason': 'drawn poke worse than the original', 'section_after_cm2': after['drawn']['area_cm2'], 'original_cm2': b}
        cands = {'section': (U, after), 'original': (self.surf.U.copy(), base)}
        try:
            Uc = self.fit_from('current', U_pre, {})
            fl_mm = self.flare(Uc) * 1000
            g['current_flare_mm'] = fl_mm
            if fl_mm <= self.over['guard_flare_mm']:         # a whole-body fit that fans a thin shell out is no fallback
                cands['current'] = (Uc, self.poke(Uc))
        except Exception as error:   # noqa: BLE001
            g['current_error'] = f'{type(error).__name__}: {error}'
        pick = min(cands, key=lambda k: self.score(cands[k][1]))
        g['used'] = pick if pick != 'section' else 'section (best of the three)'
        g['candidates_cm2'] = {k: v[1]['drawn']['area_cm2'] for k, v in cands.items()}
        g['candidate_scores'] = {k: self.score(v[1]) for k, v in cands.items()}
        info['guard' if pick != 'section' else 'guard_kept'] = g      # `guard` = a fallback was used; `guard_kept` = the section result failed the 25 % test but is the best of the three
        return cands[pick]

    def positions_of(self, path):
        """Welded world positions of another GLB with this surface's topology."""
        g = bf.Glb(path)
        Wc = np.concatenate([self.surf.to_world(np.asarray((g.positions(k) @ g.prims[k]['matrix'][:3, :3].T + g.prims[k]['matrix'][:3, 3]))) for k in range(len(g.prims))])
        if len(Wc) != len(self.surf.Wc):
            raise ValueError(f'{path}: topology differs from the aligned GLB')
        first = np.full(self.surf.K, -1, np.int64)
        first[self.surf.wid[::-1]] = np.arange(len(Wc))[::-1]
        return Wc[first]

    def write(self, U, path):
        disp = U - self.surf.U
        new_pos = self.surf.apply(disp)
        bf.write_atomic(path, self.surf.glb.patched(new_pos, bf.recomputed_normals(self.surf, new_pos)))


def _pk(pk, key):
    d = pk[key]
    return {'area_cm2': float(pk['area'].sum() * 1e4), 'max_mm': float(d.max() * 1000) if len(d) else 0.}


def topology_ok(a_path, b_path):
    """Same JSON apart from accessor min/max, same index data, same non-position/normal data."""
    A, B = bf.Glb(a_path), bf.Glb(b_path)
    if len(A.prims) != len(B.prims):
        return False
    for k in range(len(A.prims)):
        if not (A.triangles(k) == B.triangles(k)).all() or len(A.positions(k)) != len(B.positions(k)):
            return False
        if A.prims[k]['name'] != B.prims[k]['name'] or A.prims[k]['def'].get('material') != B.prims[k]['def'].get('material'):
            return False
    return A.js['materials'] == B.js['materials'] and len(A.js['accessors']) == len(B.js['accessors'])


def compute_combo(lab, cid, log=print):
    P = lab.P
    strategy, sym, scope = parse_combo(cid)
    if cid == 'original__off__none':
        return None
    t0 = time.monotonic()
    if strategy in SECTION_STRATEGIES:
        ok, why = sections_status(P)
        if not ok:
            raise RuntimeError(f'no body sections ({why}); run ensure_sections first')
    U_pre, U, info = lab.run(strategy, sym, scope)
    base = lab.baseline()
    after = lab._fit_after if (lab._fit_after is not None and strategy in SECTION_STRATEGIES and sym == 'off') else lab.poke(U)
    if strategy in SECTION_STRATEGIES and sym == 'off':
        U, after = lab.guard(U, after, base, U_pre, info)
    out = combo_file(P, cid)
    out.parent.mkdir(parents=True, exist_ok=True)
    lab.write(U, out)
    seconds = round(time.monotonic() - t0, 1)
    disp = np.linalg.norm(U - lab.surf.U, axis=1) * 1000
    fdisp = np.linalg.norm(U - U_pre, axis=1) * 1000
    rims = lab.rims
    entry = {'file': f'fitlab/lod_{LV}/{cid}.glb', 'seconds': seconds, 'max_displacement_mm': float(disp.max()), 'mean_displacement_mm': float(disp.mean()),
             'fit_max_displacement_mm': float(fdisp.max()), 'fit_mean_displacement_mm': float(fdisp.mean()),
             'max_rim_displacement_mm': float(fdisp[rims].max()) if len(rims) else 0.,
             'poke_drawn_cm2': {'before': base['drawn']['area_cm2'], 'after': after['drawn']['area_cm2'], 'after_max_mm': after['drawn']['max_mm']},
             'poke_hidden_cm2': {'before': base['hidden']['area_cm2'], 'after': after['hidden']['area_cm2'], 'after_max_mm': after['hidden']['max_mm']},
             'poke_full_cm2': {'before': base['full']['area_cm2'], 'after': after['full']['area_cm2'], 'after_max_mm': after['full']['max_mm']},
             'area_growth_pct': float(100 * (lab.surf.tri_area(U).sum() / lab.surf.tri_area().sum() - 1)),
             'rim_shown_pokes': lab.rim_report(U), 'info': info, 'params': requested_params(strategy, sym, lab.over), 'code_version': CODE_VERSION,
             'poke_reach_m': lab.reach, 'inputs': input_stamps(P, strategy, sym), 'written': datetime.datetime.now().isoformat(timespec='seconds')}
    update_index(P, {'combos': {cid: entry}, 'baseline': {k: v for k, v in base.items()}, 'body_height_m': lab.body.H})
    return entry


def process_module(module_dir, combos=None, force=False, sections_only=False, over=None, log=print, skip_suspect=False):
    """Make the module's body sections (if needed) and every requested combo that is not cached. Returns a result dict."""
    t_all = time.monotonic()
    P = module_paths(module_dir)
    res = {'module': P['rel'], 'status': 'done', 'combos': {}}
    if not P['aligned'].is_file():
        return {**res, 'status': 'skipped', 'why': 'no aligned 0p4 wearable'}
    if module_busy(P['module']):
        return {**res, 'status': 'skipped', 'why': 'a conversion worker holds this module'}
    if not (bf.BASE_CACHE / f'{P["race"]}.npz').is_file():
        return {**res, 'status': 'skipped', 'why': f'no cached base body for {P["race"]}'}
    try:            # a source garment that is broken (a band around the belly ...) cannot be fitted: flag it in the index (suspect_garments.py); batch runs skip it
        import suspect_garments as sg
        sres = sg.analyse_module(P['module'])
        sg.mark_index(P['module'], sres)
        if sres['suspect'] and skip_suspect:
            return {**res, 'status': 'skipped', 'why': 'suspect source garment: ' + '; '.join(sres['reasons'])}
    except Exception as error:   # noqa: BLE001
        log(f'FIT_LAB {P["rel"]} suspect check failed: {type(error).__name__}: {error}')
    combos = [c for c in (combos or combo_ids()) if c != 'original__off__none']
    if True:       # the metrics (poke against the drawn body) need the sections for every combo
        res['sections_seconds'] = ensure_sections(P['module'], log)
        P = module_paths(module_dir)             # (a record just written lives in the job-keyed folder)
        log(f'FIT_LAB {P["rel"]} sections {res["sections_seconds"]} s')
    if sections_only:
        return {**res, 'seconds': round(time.monotonic() - t_all, 1)}
    idx = read_index(P)
    todo = [c for c in combos if force or not entry_fresh(P, idx, c, over)]
    res['cached'] = [c for c in combos if c not in todo]
    if not todo:
        return {**res, 'seconds': round(time.monotonic() - t_all, 1)}
    t0 = time.monotonic()
    lab = Lab(P['module'], over)
    log(f'FIT_LAB {P["rel"]} loaded in {time.monotonic() - t0:.1f} s; {len(todo)} combos to do')
    for cid in todo:
        try:
            e = compute_combo(lab, cid, log)
            res['combos'][cid] = {'seconds': e['seconds'], 'max_displacement_mm': e['max_displacement_mm'], 'max_rim_displacement_mm': e['max_rim_displacement_mm']}
            log(f'FIT_LAB combo {P["rel"]} {cid} {e["seconds"]} s  disp max {e["max_displacement_mm"]:.1f} mm  rim {e["max_rim_displacement_mm"]:.2f} mm  '
                f'poke drawn {e["poke_drawn_cm2"]["before"]:.1f}->{e["poke_drawn_cm2"]["after"]:.1f} full {e["poke_full_cm2"]["before"]:.0f}->{e["poke_full_cm2"]["after"]:.0f} cm2')
        except Exception as error:   # noqa: BLE001 - one combo must not stop the others
            res['status'] = 'warnings'
            res['combos'][cid] = {'error': f'{type(error).__name__}: {error}'}
            log(f'FIT_LAB combo {P["rel"]} {cid} FAILED {type(error).__name__}: {error}')
    res['seconds'] = round(time.monotonic() - t_all, 1)
    return res


def module_up_to_date(module_dir, combos, over=None):
    P = module_paths(module_dir)
    if not P['aligned'].is_file():
        return True       # nothing to do
    combos = [c for c in (combos or combo_ids()) if c != 'original__off__none']
    if any(parse_combo(c)[0] in SECTION_STRATEGIES for c in combos) and not sections_status(P)[0]:
        return False
    idx = read_index(P)
    return all(entry_fresh(P, idx, c, over) for c in combos)


# ----------------------------------------------------------------------------------------------- unattended scheduling

IGNORE_PAUSE = False


def away_allowed():
    """(allowed, reason): user away >= 5 min per presence.json (fresh < 30 s) and no supervisor PAUSE file (unless --ignore-pause)."""
    if not IGNORE_PAUSE and (BATCH / 'PAUSE').exists():
        return False, 'PAUSE file'
    try:
        p = json.loads(PRESENCE.read_text())
    except (OSError, ValueError):
        return False, 'presence file missing'
    if time.time() - float(p.get('updated_unix', 0)) >= 30:
        return False, 'presence stale'
    if float(p.get('user_idle_seconds', 0)) < 300:
        return False, f'user active (idle {p.get("user_idle_seconds")} s)'
    return True, 'away'


def run_pool(mods, a, over, skip_suspect=True):
    """Dispatch modules to separate worker processes (`--jobs` at a time); with --when-away only while allowed. Returns (done, failed, skipped)."""
    log_path = Path(a.log_file) if a.log_file else BATCH / 'fit_lab_precache.log'
    status_path = Path(a.status_file) if a.status_file else BATCH / 'fit_lab_precache.status.json'
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(msg):
        line = f'{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}'
        print(line, flush=True)
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    combos_arg = a.combos or 'all'
    todo = []
    skipped = 0
    for m in mods:
        try:
            if skip_suspect and read_index(module_paths(m)).get('suspect'):
                skipped += 1
                continue
            if not a.force and module_up_to_date(m, expand_combos(combos_arg), over):
                skipped += 1
                continue
        except Exception:   # noqa: BLE001
            pass
        todo.append(Path(m))
    total = len(todo)
    log(f'FIT_LAB_PRECACHE start: {total} modules to do, {skipped} already cached, jobs {a.jobs}, when_away={a.when_away}')
    running, done, failed, paused_s = {}, 0, 0, 0.
    state = {'paused': False, 'pause_reason': None}
    t_start = time.time()
    queue = list(todo)
    last_status = 0.
    last_tick = time.time()
    pause_logged = None

    def write_status(force=False):
        nonlocal last_status
        if not force and time.time() - last_status < 5:
            return
        last_status = time.time()
        el = time.time() - t_start
        eta = (el / done * (total - done)) if done else None
        bf.write_json(status_path, {'updated': datetime.datetime.now().isoformat(timespec='seconds'), 'total': total, 'done': done, 'failed': failed, 'skipped_cached': skipped,
                                    'running': [Path(k).name for k in running], 'queued': len(queue), 'paused': state['paused'], 'pause_reason': state['pause_reason'],
                                    'paused_seconds': round(paused_s), 'elapsed_s': round(el), 'eta_s': round(eta) if eta is not None else None, 'jobs': a.jobs, 'finished': False})
    while queue or running:
        for k, (proc, t0) in list(running.items()):
            rc = proc.poll()
            if rc is None:
                continue
            del running[k]
            done += 1
            if rc != 0:
                failed += 1
            el = time.time() - t_start
            eta = el / done * (total - done)
            log(f'FIT_LAB_PRECACHE {"ok" if rc == 0 else f"FAILED rc={rc}"} {k} in {time.time() - t0:.0f} s  [{done}/{total}, failed {failed}, ETA {eta / 60:.0f} min]')
        allowed, reason = (away_allowed() if a.when_away else (True, 'always'))
        state['paused'], state['pause_reason'] = (not allowed), (None if allowed else reason)
        if not allowed:
            if queue and pause_logged != reason.split(' (')[0]:
                log(f'FIT_LAB_PRECACHE paused: {reason} ({len(running)} in flight)')
                pause_logged = reason.split(' (')[0]
        elif pause_logged:
            log('FIT_LAB_PRECACHE resumed')
            pause_logged = None
        while allowed and queue and len(running) < a.jobs:
            m = queue.pop(0)
            if a.when_away:       # re-check before every dispatch (a start can take a while)
                allowed, reason = away_allowed()
                if not allowed:
                    queue.insert(0, m)
                    break
            cmd = [sys.executable, '-u', str(Path(__file__).resolve()), str(m), '--combos', combos_arg, '--log-file', str(log_path)]
            if a.force:
                cmd.append('--force')
            if skip_suspect:
                cmd.append('--skip-suspect')
            for flag, key in (('--margin-pct', 'margin_pct'), ('--kappa', 'kappa'), ('--iters', 'iters'), ('--rim-ramp-pct', 'rim_ramp_pct'), ('--sym-ramp-pct', 'sym_ramp_pct')):
                if key in (over or {}):
                    cmd += [flag, str(over[key])]
            if (over or {}).get('rim_snap') is False:
                cmd.append('--no-rim-snap')
            running[str(m)] = (subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT)), time.time())
        now = time.time()
        if not allowed and queue:
            paused_s += now - last_tick
        last_tick = now
        write_status()
        time.sleep(15 if (not allowed and a.when_away) else 1)
    write_status(True)
    st = json.loads(status_path.read_text())
    st['finished'] = True
    bf.write_json(status_path, st)
    log(f'FIT_LAB_PRECACHE finished: {done} done, {failed} failed, {skipped} cached, {(time.time() - t_start) / 60:.1f} min')
    return done, failed, skipped


# ----------------------------------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('modules', nargs='*')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--match')
    ap.add_argument('--jobs', type=int, default=1)
    ap.add_argument('--combos', default='all', help='all, or comma list of <strategy>__<sym>__<scope> (a bare strategy id = its five symmetry states)')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--sections-only', action='store_true')
    ap.add_argument('--margin-pct', type=float)
    ap.add_argument('--kappa', type=float)
    ap.add_argument('--iters', type=int)
    ap.add_argument('--rim-ramp-pct', type=float)
    ap.add_argument('--sym-ramp-pct', type=float)
    ap.add_argument('--no-rim-snap', action='store_true', help='section fits keep the openings where the generator put them (the behaviour before 2026-11-21)')
    ap.add_argument('--when-away', action='store_true', help='unattended: dispatch only while the user is away and no PAUSE file exists')
    ap.add_argument('--ignore-pause', action='store_true', help='with --when-away: follow presence.json only, not the supervisor PAUSE file (user, 2026-11-19: pipeline paused, precache keeps running)')
    ap.add_argument('--skip-suspect', action='store_true', help='do not fit a module flagged suspect (broken source garment); implied for batch runs unless --include-suspect')
    ap.add_argument('--include-suspect', action='store_true')
    ap.add_argument('--status-file')
    ap.add_argument('--log-file')
    a = ap.parse_args(argv)
    global IGNORE_PAUSE
    IGNORE_PAUSE = a.ignore_pause
    over = {k: v for k, v in (('margin_pct', a.margin_pct), ('kappa', a.kappa), ('iters', a.iters), ('rim_ramp_pct', a.rim_ramp_pct), ('sym_ramp_pct', a.sym_ramp_pct)) if v is not None}
    if a.no_rim_snap:
        over['rim_snap'] = False
    mods = [Path(m) for m in a.modules]
    if a.all:
        mods += [m for m in discover() if (m / 'reduced' / f'lod_error_{LV}__wearable_aligned.glb').is_file()]
    mods = [m if m.is_absolute() else ROOT / m for m in mods]
    if a.match:
        mods = [m for m in mods if a.match in str(m)]
    if a.all:
        mods = [m for m in mods if not module_busy(m)]
    if not mods:
        raise SystemExit('no modules (give module folders or --all)')
    combos = expand_combos(a.combos)
    if (a.jobs > 1 or a.when_away) and (len(mods) > 1 or a.when_away):
        done, failed, _ = run_pool(mods, a, over, not a.include_suspect)
        if failed:
            raise SystemExit(1)
        return
    log_file = Path(a.log_file) if a.log_file else None

    def log(msg):
        line = f'{datetime.datetime.now():%H:%M:%S} {msg}'
        print(line, flush=True)
        if log_file:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    bad = 0
    for m in mods:
        try:
            r = process_module(m, combos, a.force, a.sections_only, over, log, skip_suspect=a.skip_suspect)
        except Exception as error:   # noqa: BLE001
            r = {'module': str(m), 'status': 'failed', 'why': f'{type(error).__name__}: {error}'}
            import traceback
            traceback.print_exc()
        tot = sum(c.get('seconds', 0) for c in r.get('combos', {}).values())
        log(f'FIT_LAB {r["status"]:8} {r["module"]} total {r.get("seconds", "")} s (combos {tot:.0f} s, cached {len(r.get("cached", []))}) {r.get("why") or ""}')
        bad += r['status'] == 'failed'
    if bad:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
