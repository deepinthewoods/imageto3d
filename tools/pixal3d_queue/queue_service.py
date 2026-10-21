#!/usr/bin/env python3
"""Serial, restartable approved-folder queue for AssetFurnace Pixal3D multiview."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import time
import uuid

from PIL import Image

VIEWS = ('front', 'front_three_quarter', 'back', 'back_three_quarter')
MODULES = ('body_armor', 'right_gauntlet_boot')
LOG = logging.getLogger('pixal3d-queue')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def validate_set(root, folder):
    """Require both modules for each race, with exactly the base's four named views."""
    root, folder = Path(root), Path(folder)
    if folder.is_symlink() or any(p.is_symlink() for p in folder.rglob('*')):
        raise ValueError('Symlinks are not accepted in approved sets')
    races = sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith('.'))
    if not races:
        raise ValueError('Expected <set>/<race_gender>/<module>/<view>.png')
    jobs = []
    for race in races:
        base = root / 'base_views' / race.name
        if not base.is_dir() or base.is_symlink():
            raise ValueError(f'Unknown base: {race.name}')
        transforms = base / 'transforms.json'
        doc = json.loads(transforms.read_text())
        scale = doc.get('mesh_scale')
        if not finite(scale) or scale <= 0:
            raise ValueError('mesh_scale must be positive and finite')
        frames = doc.get('frames', [])
        if len(frames) != 4 or [f.get('file_path') for f in frames] != [v+'.png' for v in VIEWS]:
            raise ValueError('Base transforms must reference the four standard views in order, without path traversal')
        for frame in frames:
            fov = frame.get('camera_angle_x', doc.get('camera_angle_x'))
            if not finite(fov) or not 0 < fov < math.pi:
                raise ValueError('Invalid camera_angle_x')
            matrix = frame.get('transform_matrix')
            if not isinstance(matrix, list) or len(matrix) != 4 or any(
                not isinstance(row, list) or len(row) != 4 or not all(finite(n) for n in row)
                for row in matrix
            ):
                raise ValueError('Invalid camera transform matrix')
            if matrix[3] != [0, 0, 0, 1]:
                raise ValueError('Camera transform must be affine')
        for module in MODULES:
            source = race / module
            files = [source / (v+'.png') for v in VIEWS]
            for picture in files:
                if not picture.is_file():
                    raise ValueError(f'Missing {race.name}/{module}/{picture.name}')
                with Image.open(base / picture.name) as original, Image.open(picture) as image:
                    image.load()
                    if image.size != original.size:
                        raise ValueError(f'{picture}: size {image.size} does not match base {original.size}')
                    if image.mode != 'RGBA' or image.getchannel('A').getextrema()[0] != 0:
                        raise ValueError(f'{picture}: expected RGBA with a transparent background')
                    if image.getchannel('A').getbbox() is None:
                        raise ValueError(f'{picture}: empty image')
            jobs.append({'race': race.name, 'module': module, 'source': str(source),
                         'transforms': str(transforms), 'files': [str(p) for p in files]})
    return jobs


def signature(jobs):
    paths = sorted({p for j in jobs for p in [*j['files'], j['transforms']]})
    return tuple((p, Path(p).stat().st_size, Path(p).stat().st_mtime_ns) for p in paths)


def snapshot(job, dest):
    """Byte-for-byte inputs; never reframe images or rewrite camera metadata."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for source in [*job['files'], job['transforms']]:
        source = Path(source)
        before = digest(source)
        target = dest / source.name
        shutil.copyfile(source, target)
        if digest(source) != before or digest(target) != before:
            raise ValueError(f'Input changed during snapshot: {source}')
        hashes[source.name] = before
    write_json(dest / 'input_hashes.json', hashes)
    return hashes


def finished(output):
    output = Path(output)
    try:
        with output.open('rb') as stream:
            magic, version, length = struct.unpack('<4sII', stream.read(12))
        record = json.loads(output.with_suffix('.provenance.json').read_text())
        return (magic == b'glTF' and version == 2 and length == output.stat().st_size
                and record['output_sha256'] == digest(output))
    except (OSError, ValueError, KeyError, struct.error):
        return False


class Backend:
    def __init__(self, config):
        self.cfg = config
        self.root = Path(config['assetfurnace']).resolve()
        sys.path.insert(0, str(self.root / 'scripts'))
        import pixal3d_generate
        self.api = pixal3d_generate

    def ready(self):
        from image_to_3dlab.provenance import validate_run_policy
        validate_run_policy('pixal3d', 'game', 'private', self.cfg.get('allow_conditional', True))
        result = self.api.multiview_readiness()
        if not result['ready']:
            raise RuntimeError(f'Pixal3D multiview unavailable (no downloads attempted): {result}')
        self.api.resolve_steps(self.cfg.get('steps', 12))

    def run(self, inputs, output, log):
        self.ready()
        api = self.api
        steps = self.cfg.get('steps', 12)
        seed = self.cfg.get('seed', 42)
        gss = self.cfg.get('gss', 10.0)
        command = api.build_multiview_command(inputs, output, 1024, seed, gss=gss)
        command += ['--require-gpu']
        sdk = self.root / 'vendor/rocm-local/sdk'
        env = dict(os.environ, HIP_VISIBLE_DEVICES=str(self.cfg.get('hip_visible_devices', '1')),
                   ROCM_PATH=str(sdk), HIP_PATH='/opt/rocm', HIP_CLANG_PATH='/opt/rocm/llvm/bin',
                   HIP_PLATFORM='amd', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        env['PATH'] = str(sdk/'bin') + os.pathsep + env.get('PATH','')
        env = api.run_env(steps, base=env)
        # This machine uses a local ROCm SDK; put it ahead of the system ROCm.
        env['LD_LIBRARY_PATH'] = str(sdk/'lib') + os.pathsep + env.get('LD_LIBRARY_PATH', '')
        started = time.time()
        with log.open('w') as stream:
            stream.write(json.dumps({'command': command, 'inputs': str(inputs)})+'\n')
            stream.flush()
            process = subprocess.Popen(command, cwd=api.PIXAL3D_ROOT, env=env,
                                       stdout=stream, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=self.cfg.get('timeout_seconds', 14400))
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        if code:
            raise RuntimeError(f'Pixal3D exited {code}; see {log}')
        if not output.is_file():
            raise RuntimeError(f'Pixal3D produced no model; see {log}')
        # Keep native orientation; do not turn the model without turning its camera data.
        doc = json.loads((inputs/'transforms.json').read_text())
        record = api.manifest(inputs/'front.png', output, res=1024, seed=seed,
                              fov=doc.get('camera_angle_x', doc['frames'][0].get('camera_angle_x')),
                              gss=gss, gsh=None, matted=True, matted_here=False,
                              seconds=time.time()-started, steps=steps)
        record.update(output_sha256=digest(output), camera_source='base model, reused unchanged by user request',
                      inputs=json.loads((inputs/'input_hashes.json').read_text()),
                      command=command, orientation='native Pixal3D; no post-rotation',
                      intent={'use_case':'game', 'distribution':'private',
                              'allow_conditional':self.cfg.get('allow_conditional', True)})
        record['parameters']['views'] = list(VIEWS)
        record['parameters']['weights'] = 'multiview'
        write_json(output.with_suffix('.provenance.json'), record)
        if not finished(output):
            raise RuntimeError('Output is not a valid complete GLB container')


class Worker:
    def __init__(self, config, runner=None, readiness=None):
        self.cfg = config
        self.root = Path(config['root']).resolve()
        self.seen = {}
        for directory in ('approved', 'processing', 'completed', 'failed', 'models', '.queue'):
            (self.root/directory).mkdir(parents=True, exist_ok=True)
        backend = Backend(config) if runner is None else None
        self.runner = runner or backend.run
        self.readiness = readiness or backend.ready
        self.status = {'approved': {}, 'active': None}

    def publish(self):
        self.status['updated_at'] = time.time()
        write_json(self.root/'.queue/status.json', self.status)

    def process(self, folder):
        state_path = folder/'.pixal3d-job.json'
        if state_path.exists():
            state = json.loads(state_path.read_text())
        else:
            state = {'id': uuid.uuid4().hex[:12], 'set': folder.name,
                     'created_at': time.time(), 'jobs': {}}
            write_json(state_path, state)
        self.status['active'] = {'set': folder.name, 'id': state['id']}
        self.publish()
        try:
            # Resume established input snapshots even if the base library has changed.
            if not state.get('prepared'):
                jobs = validate_set(self.root, folder)
                initial = signature(jobs)
                for job in jobs:
                    key = job['race']+'/'+job['module']
                    dest = self.root/'models'/state['set']/state['id']/key
                    hashes = snapshot(job, dest/'inputs')
                    state['jobs'][key] = {'directory': str(dest), 'inputs_sha256': hashes, 'attempts': 0}
                if signature(jobs) != initial:
                    raise ValueError('Files changed while preparing the set')
                state['prepared'] = True
                write_json(state_path, state)
            for key, job in state['jobs'].items():
                dest = Path(job['directory'])
                # Only resume outputs with the recorded content hash and a complete GLB.
                if job.get('output') and finished(Path(job['output'])):
                    job['status'] = 'completed'
                    continue
                for name, expected in job['inputs_sha256'].items():
                    if digest(dest/'inputs'/name) != expected:
                        raise ValueError(f'Frozen input was changed: {key}/{name}')
                job['attempts'] += 1
                if job['attempts'] > self.cfg.get('max_attempts', 3):
                    raise RuntimeError(f'{key}: repeated interruption; manual retry required')
                attempt = dest/f"attempt_{job['attempts']:02}"
                attempt.mkdir(parents=True, exist_ok=False)
                output, log = attempt/'model.glb', attempt/'generation.log'
                job.update(output=str(output), log=str(log), status='running')
                write_json(state_path, state)
                self.status['active'].update(module=key, log=str(log))
                self.publish()
                LOG.info('Generating %s / %s', folder.name, key)
                self.runner(dest/'inputs', output, log)
                if not finished(output):
                    raise RuntimeError(f'Missing or incomplete output/provenance: {output}')
                job['status'] = 'completed'
                write_json(state_path, state)
            state.update(status='completed', finished_at=time.time())
            write_json(state_path, state)
            write_json(self.root/'models'/state['set']/state['id']/'results.json', state)
            target = self.root/'completed'/f"{state['set']}--{state['id']}"
            folder.rename(target)
            LOG.info('Completed %s -> %s', state['set'], target)
        except Exception as exc:
            state.update(status='failed', error=str(exc), finished_at=time.time())
            write_json(state_path, state)
            folder.rename(self.root/'failed'/f"{state['set']}--{state['id']}")
            LOG.exception('Failed set %s; inputs and logs retained', state['set'])
        self.status['active'] = None
        self.publish()

    def tick(self):
        self.status['approved'] = {}
        candidates = []
        live = set()
        for folder in sorted((self.root/'approved').iterdir()):
            if not folder.is_dir() or folder.name.startswith('.'):
                continue
            live.add(folder.name)
            try:
                jobs = validate_set(self.root, folder)
                current = signature(jobs)
                previous = self.seen.get(folder.name)
                if previous is None or previous[0] != current:
                    self.seen[folder.name] = (current, time.monotonic())
                    self.status['approved'][folder.name] = {'status': 'settling'}
                    continue
                if time.monotonic()-previous[1] < self.cfg.get('settle_seconds', 20):
                    self.status['approved'][folder.name] = {'status': 'settling'}
                    continue
                self.status['approved'][folder.name] = {'status': 'queued', 'modules': len(jobs)}
                candidates.append(folder)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.seen.pop(folder.name, None)
                self.status['approved'][folder.name] = {'status': 'waiting_for_inputs', 'reason': str(exc)}
        self.seen = {k:v for k,v in self.seen.items() if k in live}
        try:
            self.readiness()
            self.status['backend'] = 'ready'
        except Exception as exc:
            self.status['backend'] = str(exc)
            self.publish()
            return
        self.publish()
        recovering = sorted(p for p in (self.root/'processing').iterdir() if p.is_dir())
        if recovering:
            self.process(recovering[0])
        elif candidates:
            folder = candidates[0]
            target = self.root/'processing'/folder.name
            # No overwrite if another set with this name is in progress.
            if not target.exists():
                folder.rename(target)
                self.status['approved'].pop(folder.name, None)
                self.process(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    parser.add_argument('--once', action='store_true', help='one scan; never bypasses settling')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--check', action='store_true', help='validate backend and report inputs without generating')
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    root = Path(cfg['root'])
    if args.status:
        print((root/'.queue/status.json').read_text())
        return
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.check:
        backend = Backend(cfg)
        backend.ready()
        print('Pixal3D multiview backend ready; no downloads or generation performed.')
        for folder in sorted((root/'approved').iterdir()):
            if folder.is_dir():
                try:
                    jobs = validate_set(root, folder)
                    print(folder.name, 'READY', len(jobs), 'modules')
                except Exception as exc:
                    print(folder.name, 'WAITING:', exc)
        return
    (root/'.queue').mkdir(exist_ok=True)
    with (root/'.queue/worker.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another queue worker is already running')
        def stop(signum, frame):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, stop)
        worker = Worker(cfg)
        try:
            while True:
                worker.tick()
                if args.once:
                    break
                time.sleep(cfg.get('poll_seconds', 10))
        except KeyboardInterrupt:
            LOG.info('Stopped; in-progress set will resume on restart')


if __name__ == '__main__':
    main()
