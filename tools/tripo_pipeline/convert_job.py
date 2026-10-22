"""Convert one staged four-view job: Tripo Studio submit/collect, then reduction, module by module.

Usage (from the imageto3d folder):
  python3 tools/tripo_pipeline/convert_job.py <set>--<job-id> [--dry-run] [--separable] [--piece-colours]
      [--split-method segment-first|per-face|none]
      [--backend tripo|hyper3d] [--allow-paid --accept-preview]   (the last two only matter for hyper3d)

- Takes the job from multiview_ready/ (moving it to processing/) or continues one already in processing/.
- Modules run strictly one after another: Studio disables Generate while another model of the
  account is generating, and studio.mjs waits for that anyway.
- Resumable: a module with a submission record is collected, not resubmitted (no double charge);
  a collected module with reduced/manifest.json is skipped.
- Outputs per module in models/<set>/<job-id>/<race>/<module>/: Tripo OBJ/GLB, source.glb,
  highpoly.blend, reduced/ (0.4/0.2/0.1% GLBs, wearable GLBs, review.blend, wearable.blend).
  reduce_model.py finds module, swatches, cameras and body regions from that layout.
- A module whose Tripo mesh is broken (currently: a doubled interior shell, see reduce_model's
  interior audit) is regenerated with the Studio project's free retries (studio.mjs retry); each
  failed attempt is kept as <module>_attemptNN_<reason>/. When the free retries are used up it never
  pays for a new generation: the last attempt stays as the module output, the module is flagged
  `free_retries_exhausted` / `needs_manual_review` in the job record, the job state becomes
  `converted_with_errors`, and the script prints ERROR lines and exits 1.
- Split step (after the reduction, resumable, never fails the conversion): a `piece_colours` `body_armor` module is cut
  into rigid pieces by SEGMENT-FIRST (the default): segment_first_prep.py in Blender -> <module>/segment_first/prep/, then
  segment_first.py (venv python with numpy + scipy) -> <module>/segment_first/{levels,pieces,segment_first.json}. If that
  fails it falls back to `split_pieces.py <module>/reduced --rigid auto` -> <module>/pieces/ and prints/records why.
  `--split-method per-face` runs only that fallback path, `none` skips the step (the default for every other job, including
  separable joint-band jobs). The method used is recorded in .tripo-job.json module_conversion.<race>/<module>.split and in
  <module>/split_step.json.
- Leaves the job in processing/ with state `converted` and prints which levels need review.
  Moving it to completed/ is a separate step after that review.
--dry-run: opens Studio, fills the form and screenshots it without pressing Generate.
--backend hyper3d: optional alternative image->3D step (Hyper3D Rodin, high-poly textured GLB, no Smart
  Low-poly; tools/hyper3d_pipeline/README.md). Tripo stays the default. Submit starts only the FREE preview;
  collect needs --allow-paid --accept-preview and a configured expected credit cost, otherwise it stops with
  a message (exit 1) and the module stays resumable. The Tripo free-retry loop is skipped for hyper3d
  (its retries cost credits): a broken mesh is only flagged for manual review.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / 'tools/tripo_pipeline'
HYPER3D = ROOT / 'tools/hyper3d_pipeline'
BACKENDS = ('tripo', 'hyper3d')
BLENDER = os.environ.get('BLENDER', '/usr/local/bin/blender')
SPLIT_METHODS = ('segment-first', 'per-face', 'none')


def write_record(path, record):
    path.write_text(json.dumps(record, indent=2) + '\n')


def hyper3d_python():
    """Interpreter with selenium for the Hyper3D browser runner (config.json, overridable by $HYPER3D_VENV)."""
    if os.environ.get('HYPER3D_VENV'):
        return str(Path(os.environ['HYPER3D_VENV']) / 'bin/python')
    return json.loads((HYPER3D / 'config.json').read_text())['venv_python']


def submit_command(backend, views, out, dry_run=False):
    if backend == 'hyper3d':
        return [hyper3d_python(), str(HYPER3D / 'rodin_backend.py'), 'submit', str(views), str(out)] \
            + (['--dry-run'] if dry_run else [])
    return ['node', str(TOOLS / 'studio.mjs'), 'submit', str(views), str(out)]


def collect_command(backend, out, options=None):
    if backend == 'hyper3d':
        options = options or {}
        return [hyper3d_python(), str(HYPER3D / 'rodin_backend.py'), 'collect', str(out)] \
            + (['--allow-paid'] if options.get('allow_paid') else []) \
            + (['--accept-preview'] if options.get('accept_preview') else [])
    return ['node', str(TOOLS / 'studio.mjs'), 'collect', str(out)]


def option_value(argv, name, default=None):
    """`--name value` or `--name=value` from argv."""
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + '='):
            return arg.split('=', 1)[1]
    return default


def job_is_separable(job, race, module):
    """True when the job's records say separable / joint bands: `.tripo-job.json` (separable, joint_bands), a
    `prompts.json` / `<module>_prompts.json` beside the views or in `.concept_notes/` with such a key or the words
    'joint band' in its text (plain pauldron-only separable jobs do not need the fifth colour). `--separable` on the
    command line forces it."""
    def flagged(path):
        try:
            text = path.read_text()
            data = json.loads(text)
        except (OSError, ValueError):
            return False
        if isinstance(data, dict) and (data.get('joint_bands') or data.get('separable') in ('joint_bands', 'bands')):
            return True
        return any(w in text.lower() for w in ('joint band', 'joint_band'))
    record = job / '.tripo-job.json'
    candidates = [record, job / race / module / 'prompts.json', job / race / f'{module}_prompts.json',
                  job / race / '.concept_notes' / f'{module}_prompts.json', job / race / '.concept_notes' / 'prompts.json']
    return any(flagged(c) for c in candidates if c.is_file())


def job_is_piece_colours(job, race, module):
    """True when the job's records say rigid sections with per-piece base colours (`piece_colours` / `rigid_sections` key,
    or the words 'per-piece base colour' / 'piece colours' in `.tripo-job.json` / `prompts.json`). Such a job uses the
    standard 4-colour palette (no joint role) but the reduction fits the palette hues to the model (`--adaptive-hue on`).
    `--piece-colours` on the command line forces it."""
    def flagged(path):
        try:
            text = path.read_text()
            data = json.loads(text)
        except (OSError, ValueError):
            return False
        if isinstance(data, dict) and (data.get('piece_colours') or data.get('rigid_sections')):
            return True
        return any(w in text.lower() for w in ('per-piece base colour', 'per-piece base color', 'piece colours', 'piece_colours'))
    candidates = [job / '.tripo-job.json', job / race / module / 'prompts.json', job / race / f'{module}_prompts.json',
                  job / race / '.concept_notes' / f'{module}_prompts.json', job / race / '.concept_notes' / 'prompts.json']
    return any(flagged(c) for c in candidates if c.is_file())


def collect_and_reduce(key, out, backend='tripo', options=None, separable=False, piece_colours=False):
    """Collect a submitted module from Studio (if needed), then reduce it; returns (state, manifest)."""
    submission = out / 'studio_submission.json'
    state = json.loads(submission.read_text())
    if state.get('backend', 'tripo') != backend:
        raise SystemExit(f'{key}: submission record belongs to backend {state.get("backend", "tripo")!r}, '
                         f'not {backend!r}; inspect {submission}')
    if state.get('status') not in ('submitted', 'exported'):
        raise SystemExit(f'{key}: submission record says {state.get("status")!r}; inspect {submission}')
    if state.get('status') != 'exported' or 'GLB' not in state.get('exports', {}):
        print(f'== {key}: collect', flush=True)
        if backend == 'tripo':
            subprocess.run(collect_command('tripo', out), check=True, cwd=ROOT)
        else:
            run = subprocess.run(collect_command(backend, out, options), cwd=ROOT)
            if run.returncode != 0:  # a guard stop (credits/permissions/sign-in) leaves the module resumable
                raise SystemExit(f'{key}: {backend} collect stopped (exit {run.returncode}); '
                                 f'fix what it printed and re-run; record {submission}')
        state = json.loads(submission.read_text())
    source = out / 'source.glb'
    if not source.exists():
        shutil.copy2(state['exports']['GLB']['path'], source)
    manifest = out / 'reduced' / 'manifest.json'
    if not manifest.exists():
        print(f'== {key}: reduce', flush=True)
        subprocess.run([BLENDER, '-b', '-t', '6', '--python', str(TOOLS / 'reduce_model.py'), '--',
                        str(source), str(out / 'reduced')] + (['--separable'] if separable else []) + (['--adaptive-hue', 'on'] if piece_colours else []), check=True, cwd=ROOT)
    return state, json.loads(manifest.read_text())


def segment_first_config():
    """config.json `segment_first` block (default_method, venv_python, levels); {} when absent."""
    try:
        return json.loads((TOOLS / 'config.json').read_text()).get('segment_first') or {}
    except (OSError, ValueError):
        return {}


def segment_first_python():
    """Interpreter with numpy + scipy + Pillow for segment_first.py: $SEGMENT_FIRST_PYTHON, config.json segment_first.venv_python,
    else the characters/.venv beside the imageto3d folder. Raises FileNotFoundError naming every place it looked."""
    candidates = [os.environ.get('SEGMENT_FIRST_PYTHON'), segment_first_config().get('venv_python'),
                  str(ROOT.parent / '.venv/bin/python')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise FileNotFoundError('no python with numpy+scipy for segment_first.py; looked for $SEGMENT_FIRST_PYTHON, '
                            f'config.json segment_first.venv_python and {ROOT.parent / ".venv/bin/python"} '
                            f'(tried: {[c for c in candidates if c]})')


def choose_split_method(requested, module, piece_colours, separable):
    """(method, why) for the split step. requested: the --split-method value or None. The default is segment-first for a
    piece_colours body_armor job (config.json segment_first.default_method can change it) and none for everything else,
    joint-band separable jobs included (those keep split_pieces bands, run by hand). The rigid split only exists for
    body_armor, so any other module is always `none`."""
    if requested is not None and requested not in SPLIT_METHODS:
        raise ValueError(f'--split-method must be one of {SPLIT_METHODS}, not {requested!r}')
    if module != 'body_armor':
        return 'none', f'{module} has no rigid piece split'
    if requested is not None:
        return requested, '--split-method'
    if separable:
        return 'none', 'separable joint-band job (split_pieces --rigid bands is run by hand)'
    if piece_colours:
        method = segment_first_config().get('default_method', 'segment-first')
        return (method if method in SPLIT_METHODS else 'segment-first'), 'piece_colours job default'
    return 'none', 'not a piece_colours job'


def relative(path):
    """`path` relative to ROOT when it lies under it, else as it is."""
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def run_logged(command, log, label):
    """Run `command` from ROOT with its output in `log`; raises RuntimeError with the log tail on a non-zero exit."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, 'w') as handle:
        run = subprocess.run(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
    if run.returncode != 0:
        tail = ' | '.join(log.read_text(errors='replace').strip().splitlines()[-6:])
        raise RuntimeError(f'{label} exited {run.returncode} (log {log}): {tail}')


def segment_first_split(out, module):
    """Segment-first split of a reduced module folder: prep (Blender) then segment_first.py; both skipped when their output exists.
    Inputs (cameras, regions, palette) are the ones the reduction recorded in reduced/manifest.json."""
    sf = out / 'segment_first'
    prep = sf / 'prep'
    if not (prep / 'prep.npz').exists():
        manifest = json.loads((out / 'reduced' / 'manifest.json').read_text())
        sf.mkdir(parents=True, exist_ok=True)
        command = [BLENDER, '-b', '-t', '6', '--python', str(TOOLS / 'segment_first_prep.py'), '--',
                   str(out / 'source.glb'), str(prep), '--module', module]
        if manifest.get('cameras'):
            command += ['--cameras', manifest['cameras']]
        if manifest.get('regions'):
            command += ['--regions', manifest['regions']]
        if manifest.get('palette'):             # the palette the reduction used (job snapshot / base), frozen beside the run
            write_record(sf / 'palette_used.json', manifest['palette'])
            command += ['--palette', str(sf / 'palette_used.json')]
        print(f'== {out.parent.name}/{module}: split (segment-first): prep', flush=True)
        run_logged(command, sf / 'prep.log', 'segment_first_prep.py')
        if not (prep / 'prep.npz').exists():
            raise RuntimeError(f'segment_first_prep.py wrote no prep.npz (log {sf / "prep.log"})')
    if (sf / 'pieces' / 'pieces.json').exists():
        return sf
    print(f'== {out.parent.name}/{module}: split (segment-first): pieces', flush=True)
    levels = segment_first_config().get('levels')
    run_logged([segment_first_python(), str(TOOLS / 'segment_first.py'), str(out), str(sf), '--reduced', str(out / 'reduced'),
                '--source', str(out / 'source.glb'), '--prep-dir', str(prep)] + (['--levels', levels] if levels else []),
               sf / 'segment_first.log', 'segment_first.py')
    if not (sf / 'pieces' / 'pieces.json').exists():
        raise RuntimeError(f'segment_first.py wrote no pieces/pieces.json (log {sf / "segment_first.log"})')
    return sf


def per_face_split(out, module):
    """Fallback / `per-face` method: split_pieces.py --rigid auto on the per-face reduction -> <module>/pieces/."""
    pieces = out / 'pieces'
    if (pieces / 'pieces.json').exists():
        return pieces
    try:
        python = segment_first_python()
    except FileNotFoundError:
        python = sys.executable
    print(f'== {out.parent.name}/{module}: split (per-face): split_pieces --rigid auto', flush=True)
    run_logged([python, str(TOOLS / 'split_pieces.py'), str(out / 'reduced'), '--module', module, '--rigid', 'auto'],
               out / 'pieces_per_face.log', 'split_pieces.py')
    if not (pieces / 'pieces.json').exists():
        raise RuntimeError(f'split_pieces.py wrote no pieces/pieces.json (log {out / "pieces_per_face.log"})')
    return pieces


def split_step(out, module, method, why=''):
    """Run the split `method` on the reduced module folder `out`; never raises. Returns the record written to
    <out>/split_step.json and .tripo-job.json: {method (used), requested, why, status done|fallback|failed|skipped,
    outputs, fallback_reason?, error?}. A failed segment-first falls back to the per-face split_pieces."""
    record = {'requested': method, 'method': method, 'why': why}
    if method == 'none':
        record['status'] = 'skipped'
        return record
    try:
        if method == 'segment-first':
            try:
                record['outputs'] = relative(segment_first_split(out, module))
                record['status'] = 'done'
            except Exception as error:  # noqa: BLE001  the split must never fail the conversion
                record['fallback_reason'] = f'segment-first failed: {error}'
                print(f'WARNING {out.parent.name}/{module}: {record["fallback_reason"]}; falling back to '
                      f'split_pieces --rigid auto', flush=True)
                record['method'], record['status'] = 'per-face', 'fallback'
                record['outputs'] = relative(per_face_split(out, module))
        else:
            record['outputs'] = relative(per_face_split(out, module))
            record['status'] = 'done'
    except Exception as error:  # noqa: BLE001
        record['status'], record['error'] = 'failed', str(error)
        print(f'WARNING {out.parent.name}/{module}: split step failed ({error}); the reduction is intact', flush=True)
    if out.is_dir():
        write_record(out / 'split_step.json', record)
    return record


def regenerate_reason(info):
    """A Tripo mesh worth regenerating: currently a doubled interior shell (reduce_model's audit)."""
    interior = info.get('interior') or {}
    share = interior.get('interior_area_share', 0)
    if share >= interior.get('threshold', float('inf')):
        return {'slug': 'interior_shell', 'text': f'doubled interior shell ({share:.0%} of the area is enclosed)'}
    return None


def main(argv):
    if not argv or argv[0].startswith('-'):
        raise SystemExit(__doc__)
    job_name, dry_run = argv[0], '--dry-run' in argv[1:]
    backend = option_value(argv[1:], '--backend', 'tripo')
    if backend not in BACKENDS:
        raise SystemExit(f'--backend must be one of {BACKENDS}, not {backend!r}')
    options = {'allow_paid': '--allow-paid' in argv[1:], 'accept_preview': '--accept-preview' in argv[1:]}
    requested_split = option_value(argv[1:], '--split-method')
    if requested_split is not None and requested_split not in SPLIT_METHODS:
        raise SystemExit(f'--split-method must be one of {SPLIT_METHODS}, not {requested_split!r}')
    job = ROOT / 'processing' / job_name
    staged = ROOT / 'multiview_ready' / job_name
    if not job.is_dir():
        if not staged.is_dir():
            raise SystemExit(f'No job {job_name} in multiview_ready/ or processing/')
        if not dry_run:
            # validate before taking it out of the ready queue
            subprocess.run([sys.executable, str(TOOLS / 'workflow.py'), 'check', str(staged), '--complete'],
                           check=True, stdout=subprocess.DEVNULL)
            job.parent.mkdir(exist_ok=True)
            os.rename(staged, job)
        else:
            job = staged
    record_path = job / '.tripo-job.json'
    record = json.loads(record_path.read_text())
    record.setdefault('module_conversion', {})
    summary, errors = {}, []
    for entry in record['modules']:
        race, module = entry['race_gender'], entry['module']
        key = f'{race}/{module}'
        separable = '--separable' in argv[1:] or job_is_separable(job, race, module)
        if separable:
            print(f'== {key}: separable job (joint-band palette)', flush=True)
        piece_colours = '--piece-colours' in argv[1:] or job_is_piece_colours(job, race, module)
        if piece_colours:
            print(f'== {key}: per-piece base colours job (standard palette, adaptive hue fit on)', flush=True)
        out = ROOT / 'models' / record['set'] / record['job_id'] / race / module
        if dry_run:  # never leave a dry-run record where the real submission will go
            out = Path(tempfile.mkdtemp(prefix=f'tripo_dryrun_{module}_'))
        submission = out / 'studio_submission.json'
        env = dict(os.environ, **({'TRIPO_DRY_RUN': '1'} if dry_run else {}))
        if not submission.exists():
            print(f'== {key}: submit', flush=True)
            subprocess.run(submit_command(backend, job / race / module, out, dry_run),
                           check=True, cwd=ROOT, env=env)
        if dry_run:
            summary[key] = f'dry run: form filled, nothing submitted; screenshot {out / "before_submit.png"}'
            continue
        state, info = collect_and_reduce(key, out, backend, options, separable, piece_colours)
        # Hyper3D retries cost credits, so only Tripo's free-retry loop runs; a broken hyper3d mesh is flagged
        reason = regenerate_reason(info) if backend == 'tripo' else None
        previous = record['module_conversion'].get(key, {})
        while reason and not previous.get('free_retries_exhausted'):
            # a broken Tripo mesh is regenerated with the project's free retries, never by paying again
            failed = out.parent / f'{module}_attempt{len(list(out.parent.glob(module + "_attempt*"))) + 1:02d}_{reason["slug"]}'
            os.rename(out, failed)
            print(f'== {key}: {reason["text"]}; moved to {failed.name}; free retry', flush=True)
            run = subprocess.run(['node', str(TOOLS / 'studio.mjs'), 'retry', str(failed), str(out)], cwd=ROOT)
            retry_state = json.loads(submission.read_text()) if submission.exists() else {}
            if run.returncode != 0:
                if retry_state.get('status') != 'no_free_retries':
                    if not out.exists():  # never leave the module empty: a resume would pay for a new submission
                        os.rename(failed, out)
                    raise SystemExit(f'{key}: free retry failed; inspect {out}')
                # keep the last attempt as the module output, flagged; the retry record goes beside it
                n = len(list(out.parent.glob(module + '_retry_exhausted_record*'))) + 1
                os.rename(out, out.parent / f'{module}_retry_exhausted_record{n:02d}')
                os.rename(failed, out)
                state, info = collect_and_reduce(key, out, separable=separable)
                previous = {'free_retries_exhausted': True}
                errors.append(f'{key}: {reason["text"]} and all free retries are used; '
                              f'needs manual review (do not pay for another generation blindly)')
                break
            state, info = collect_and_reduce(key, out, separable=separable)
            reason = regenerate_reason(info)
        if backend != 'tripo' and regenerate_reason(info):
            errors.append(f'{key}: {regenerate_reason(info)["text"]}; hyper3d has no free retry, '
                          f'needs manual review (do not pay for another generation blindly)')
        method, why = choose_split_method(requested_split, module, piece_colours, separable)
        split = split_step(out, module, method, why)
        if method != 'none':
            print(f'== {key}: split {split["status"]} ({split["method"]})', flush=True)
        levels = info['levels']
        review = [k for k, v in levels.items()
                  if not v['mesh_checks_pass'] or v['material_review_required']]
        record['module_conversion'][key] = {'backend': backend, 'project_id': state.get('project_id'),
                                            'models': str(out.relative_to(ROOT)),
                                            'levels_needing_review': review,
                                            'split': split,
                                            **({'free_retries_exhausted': True, 'needs_manual_review':
                                                regenerate_reason(info)['text']}
                                               if previous.get('free_retries_exhausted') and regenerate_reason(info) else {})}
        write_record(record_path, record)
        summary[key] = {k: v['triangles'] for k, v in levels.items()} | {'review': review}
    if not dry_run:
        record['state'] = 'converted_with_errors' if errors else 'converted'
        record['conversion'] = f'{backend}_and_reduction_done_pending_review'
        write_record(record_path, record)
    print(json.dumps({'job': str(job.relative_to(ROOT)), 'modules': summary}, indent=2))
    for line in errors:
        print(f'ERROR {line}', file=sys.stderr)
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main(sys.argv[1:])
