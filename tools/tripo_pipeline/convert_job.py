"""Convert one staged four-view job: Tripo Studio submit/collect, then reduction, module by module.

Usage (from the imageto3d folder):
  python3 tools/tripo_pipeline/convert_job.py <set>--<job-id> [--dry-run]

- Takes the job from multiview_ready/ (moving it to processing/) or continues one already in processing/.
- Modules run strictly one after another: Studio disables Generate while another model of the
  account is generating, and studio.mjs waits for that anyway.
- Resumable: a module with a submission record is collected, not resubmitted (no double charge);
  a collected module with reduced/manifest.json is skipped.
- Outputs per module in models/<set>/<job-id>/<race>/<module>/: Tripo OBJ/GLB, source.glb,
  highpoly.blend, reduced/ (0.4/0.2/0.1% GLBs, wearable GLBs, review.blend, wearable.blend).
  reduce_model.py finds module, swatches, cameras and body regions from that layout.
- Leaves the job in processing/ with state `converted` and prints which levels need review.
  Moving it to completed/ is a separate step after that review.
--dry-run: opens Studio, fills the form and screenshots it without pressing Generate.
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
BLENDER = os.environ.get('BLENDER', '/usr/local/bin/blender')


def write_record(path, record):
    path.write_text(json.dumps(record, indent=2) + '\n')


def main(argv):
    if not argv or argv[0].startswith('-'):
        raise SystemExit(__doc__)
    job_name, dry_run = argv[0], '--dry-run' in argv[1:]
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
    summary = {}
    for entry in record['modules']:
        race, module = entry['race_gender'], entry['module']
        key = f'{race}/{module}'
        out = ROOT / 'models' / record['set'] / record['job_id'] / race / module
        if dry_run:  # never leave a dry-run record where the real submission will go
            out = Path(tempfile.mkdtemp(prefix=f'tripo_dryrun_{module}_'))
        submission = out / 'studio_submission.json'
        env = dict(os.environ, **({'TRIPO_DRY_RUN': '1'} if dry_run else {}))
        if not submission.exists():
            print(f'== {key}: submit', flush=True)
            subprocess.run(['node', str(TOOLS / 'studio.mjs'), 'submit', str(job / race / module), str(out)],
                           check=True, cwd=ROOT, env=env)
        if dry_run:
            summary[key] = f'dry run: form filled, nothing submitted; screenshot {out / "before_submit.png"}'
            continue
        state = json.loads(submission.read_text())
        if state.get('status') not in ('submitted', 'exported'):
            raise SystemExit(f'{key}: submission record says {state.get("status")!r}; inspect {submission}')
        if state.get('status') != 'exported' or 'GLB' not in state.get('exports', {}):
            print(f'== {key}: collect', flush=True)
            subprocess.run(['node', str(TOOLS / 'studio.mjs'), 'collect', str(out)], check=True, cwd=ROOT)
            state = json.loads(submission.read_text())
        source = out / 'source.glb'
        if not source.exists():
            shutil.copy2(state['exports']['GLB']['path'], source)
        manifest = out / 'reduced' / 'manifest.json'
        if not manifest.exists():
            print(f'== {key}: reduce', flush=True)
            subprocess.run([BLENDER, '-b', '-t', '6', '--python', str(TOOLS / 'reduce_model.py'), '--',
                            str(source), str(out / 'reduced')], check=True, cwd=ROOT)
        levels = json.loads(manifest.read_text())['levels']
        review = [k for k, v in levels.items()
                  if not v['mesh_checks_pass'] or v['material_review_required']]
        record['module_conversion'][key] = {'project_id': state.get('project_id'),
                                            'models': str(out.relative_to(ROOT)),
                                            'levels_needing_review': review}
        write_record(record_path, record)
        summary[key] = {k: v['triangles'] for k, v in levels.items()} | {'review': review}
    if not dry_run:
        record['state'] = 'converted'
        record['conversion'] = 'tripo_and_reduction_done_pending_review'
        write_record(record_path, record)
    print(json.dumps({'job': str(job.relative_to(ROOT)), 'modules': summary}, indent=2))


if __name__ == '__main__':
    main(sys.argv[1:])
