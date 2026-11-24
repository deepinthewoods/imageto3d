"""Local Pixal3D (camera-aware multi-view TRELLIS.2, pixal3d.cpp) as a free image->3D backend for convert_job.py.

  pixal3d_backend.py submit <views_dir> <out_dir> [--seed-offset N]   stage the views + the job's frozen cameras, write the record
  pixal3d_backend.py collect <out_dir>                                 generate on the local GPU (WSL), export, record the GLB

Contract with convert_job.py: <out_dir>/studio_submission.json is the record Tripo's studio.mjs writes (`status`
submitted -> exported, `exports.GLB.path`) plus `backend: "pixal3d"`. `submit` is instant (nothing is spent); the
generation runs in `collect`, so a failed or interrupted generation leaves the module resumable through the normal
collect path. Nothing here costs credits, so convert_job may regenerate a broken mesh with another seed.

Inputs: the module's view PNGs (8-bit RGBA, alpha baked in; no matting is done) and the cameras of the job's frozen
`.base_views/<race>/transforms.json` (else base_views/<race>/transforms.json), restricted to the views that exist (a
three-view module passes three frames; the multiview weights are validated at four). Pixal3D is camera-aware, so the
dwarf 20-degree front three-quarter and the gnome back three-quarter camera need nothing special.

Output: Pixal3D writes its model facing glTF -Z; Tripo's face +Z, which reduce_model's visibility weighting and the
base fit assume. The download is therefore rotated `rotate_y_deg` (180) about the up axis, in the vertex data itself
(POSITION/NORMAL/TANGENT), and saved as `glb_pixal3d.glb` (a `glb_*.glb` name, so offload_models.py treats it as the
backend download). The mesh is remeshed (closed, one component, ~1M faces) with a 2048 texture atlas.

Runs on Windows (through `wsl.exe -d <distro>`) and on Linux (directly). One generation at a time per machine
(config lock_file): the model needs most of the 16 GB GPU. Configuration: config.json; see README.md.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
from array import array
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TOOLS = ROOT / 'tools/tripo_pipeline'
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import portable  # noqa: E402

CONFIG = HERE / 'config.json'
RECORD = 'studio_submission.json'
DOWNLOAD = 'glb_pixal3d.glb'
INPUT_DIR = 'pixal3d_input'
VIEW_ORDER = ('front', 'front_three_quarter', 'back', 'back_three_quarter')
WINDOWS = os.name == 'nt'


class Stop(Exception):
    """A failure that leaves the module resumable (the record keeps status `submitted`)."""


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def load_config(path=CONFIG):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(8 << 20), b''):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- inputs

def find_transforms(views_dir):
    """(path, note) of the cameras for `views_dir` = <job>/<race>/<module>: the job's frozen snapshot, else the active base."""
    views_dir = Path(views_dir)
    race, job = views_dir.parent.name, views_dir.parent.parent
    for candidate, note in ((job / '.base_views' / race / 'transforms.json', 'job snapshot'),
                            (ROOT / 'base_views' / race / 'transforms.json', 'active base (job has no frozen transforms.json)')):
        if candidate.is_file():
            return candidate, note
    raise Stop(f'no transforms.json for race {race!r} (looked in {job / ".base_views" / race} and base_views/{race})')


def select_frames(transforms, views_dir):
    """transforms.json restricted to the view PNGs that exist in `views_dir`, front first, file_path = '<view>.png'."""
    frames = []
    for frame in transforms['frames']:
        name = frame.get('name') or Path(frame.get('file_path', '')).stem
        if (Path(views_dir) / f'{name}.png').is_file():
            frames.append(dict(frame, name=name, file_path=f'{name}.png'))
    frames.sort(key=lambda f: VIEW_ORDER.index(f['name']) if f['name'] in VIEW_ORDER else len(VIEW_ORDER))
    if not frames or frames[0]['name'] != 'front':
        raise Stop(f'{views_dir}: front.png is required (Pixal3D frame 0 is the main/front view)')
    return dict(transforms, frames=frames)


def check_png(path):
    """Width, height of an 8-bit RGBA PNG (Pixal3D does no matting: the alpha must be real)."""
    with open(path, 'rb') as handle:
        head = handle.read(33)
    if head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        raise Stop(f'{path} is not a PNG')
    width, height, depth, colour = struct.unpack('>IIBB', head[16:26])
    if depth != 8 or colour != 6:
        raise Stop(f'{path} is not 8-bit RGBA (bit depth {depth}, colour type {colour})')
    return width, height


def stage(views_dir, out_dir):
    """Copy (hard-link) the views and the matching cameras into <out_dir>/pixal3d_input/. Returns (frames, transforms note)."""
    source, note = find_transforms(views_dir)
    transforms = select_frames(json.loads(source.read_text(encoding='utf-8-sig')), views_dir)
    sizes = {check_png(Path(views_dir) / f['file_path']) for f in transforms['frames']}
    if len(sizes) != 1:
        raise Stop(f'{views_dir}: the views differ in size {sorted(sizes)}')
    staged = Path(out_dir) / INPUT_DIR
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True)
    for frame in transforms['frames']:
        src, dst = Path(views_dir) / frame['file_path'], staged / frame['file_path']
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    write_json(staged / 'transforms.json', transforms)
    return [f['name'] for f in transforms['frames']], f'{note}: {portable_rel(source)}'


def portable_rel(path):
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------- GLB

def read_glb(path):
    data = Path(path).read_bytes()
    magic, version, length = struct.unpack_from('<III', data, 0)
    if magic != 0x46546C67:
        raise Stop(f'{path} is not a GLB')
    offset, gltf, binary = 12, None, b''
    while offset < length:
        size, kind = struct.unpack_from('<II', data, offset)
        chunk = data[offset + 8: offset + 8 + size]
        if kind == 0x4E4F534A:
            gltf = json.loads(chunk.decode('utf-8'))
        elif kind == 0x004E4942:
            binary = bytearray(chunk)
        offset += 8 + size
    if gltf is None:
        raise Stop(f'{path} has no JSON chunk')
    return gltf, binary


def write_glb(path, gltf, binary):
    text = json.dumps(gltf, separators=(',', ':')).encode('utf-8')
    text += b' ' * (-len(text) % 4)
    binary = bytes(binary) + b'\0' * (-len(binary) % 4)
    total = 12 + 8 + len(text) + (8 + len(binary) if binary else 0)
    with open(path, 'wb') as handle:
        handle.write(struct.pack('<III', 0x46546C67, 2, total))
        handle.write(struct.pack('<II', len(text), 0x4E4F534A) + text)
        if binary:
            handle.write(struct.pack('<II', len(binary), 0x004E4942) + binary)


def face_count(gltf):
    faces = 0
    for mesh in gltf.get('meshes', []):
        for prim in mesh.get('primitives', []):
            if 'indices' in prim:
                faces += gltf['accessors'][prim['indices']]['count'] // 3
            else:
                faces += gltf['accessors'][prim['attributes']['POSITION']]['count'] // 3
    return faces


def rotate_y180(gltf, binary):
    """Rotate every mesh 180 degrees about +Y in place: x -> -x, z -> -z for POSITION, NORMAL and TANGENT (winding is kept,
    the rotation has determinant +1). Refuses node transforms (the rotation would no longer be about the model's up axis)."""
    for node in gltf.get('nodes', []):
        if any(k in node for k in ('matrix', 'rotation', 'scale', 'translation')):
            raise Stop(f'GLB node {node.get("name")!r} has its own transform; rotate it in Blender instead')
    done = set()
    for mesh in gltf.get('meshes', []):
        for prim in mesh.get('primitives', []):
            for name in ('POSITION', 'NORMAL', 'TANGENT'):
                index = prim.get('attributes', {}).get(name)
                if index is None or index in done:
                    continue
                done.add(index)
                acc = gltf['accessors'][index]
                if acc.get('componentType') != 5126 or acc.get('sparse'):
                    raise Stop(f'{name} accessor is not plain float32')
                width = {'VEC3': 3, 'VEC4': 4}[acc['type']]
                view = gltf['bufferViews'][acc['bufferView']]
                start = view.get('byteOffset', 0) + acc.get('byteOffset', 0)
                stride = view.get('byteStride') or width * 4
                count = acc['count']
                if stride == width * 4:
                    values = array('f')
                    values.frombytes(bytes(binary[start: start + count * stride]))
                    for axis in (0, 2):
                        values[axis::width] = array('f', (-x for x in values[axis::width]))
                    binary[start: start + count * stride] = values.tobytes()
                else:
                    for i in range(count):
                        at = start + i * stride
                        x, y, z = struct.unpack_from('<fff', binary, at)
                        struct.pack_into('<fff', binary, at, -x, y, -z)
                if name == 'POSITION' and 'min' in acc and 'max' in acc:
                    low, high = acc['min'], acc['max']
                    acc['min'] = [-high[0], low[1], -high[2]] + low[3:]
                    acc['max'] = [-low[0], high[1], -low[2]] + high[3:]
    return gltf, binary


# --------------------------------------------------------------------------- generation

def to_linux(path):
    """A path as the generator sees it: F:\\a\\b -> /mnt/f/a/b under WSL; unchanged on Linux."""
    text = str(Path(path).resolve())
    if not WINDOWS:
        return text
    match = re.match(r'^([A-Za-z]):[\\/](.*)$', text)
    if not match:
        raise Stop(f'cannot map {text} into WSL')
    return f'/mnt/{match.group(1).lower()}/' + match.group(2).replace('\\', '/')


def generation_script(config, input_dir, dest, seed, tag):
    """bash script: run trellis-cli in a private work dir, copy the GLB to `dest`, remove the work dir (PLY etc.)."""
    q = shlex.quote
    work = f'{config["work_dir"].rstrip("/")}/{tag}'
    variables = {**config.get('env', {}), 'LD_LIBRARY_PATH': f'/opt/rocm/lib:{config["lib_dir"]}'}
    if not config.get('remesh', True):   # patched build: keep the raw single-sided dual-grid mesh (see README.md)
        variables['PIXAL3D_NO_REMESH'] = '1'
    env = ' '.join(f'{k}={q(str(v))}' for k, v in variables.items())
    cli = ' '.join(q(a) for a in [config['cli'], '--views', input_dir, '--models', config['models'], *config['args'],
                                  '--seed', str(seed), f'{work}/model.glb'])
    return (f'set -o pipefail; rm -rf {q(work)}; mkdir -p {q(work)} && cd {q(work)} || exit 2\n'
            f'env {env} {cli}; rc=$?\n'
            f'if [ $rc -eq 0 ]; then cp {q(work + "/model.glb")} {q(dest)} || rc=3; fi\n'
            f'rm -rf {q(work)}; exit $rc\n')


def run_generation(config, out_dir, seed, log):
    """Generate into <out_dir>/glb_pixal3d_raw.glb; returns seconds. Raises Stop on failure."""
    out_dir = Path(out_dir)
    raw = out_dir / 'glb_pixal3d_raw.glb'
    raw.unlink(missing_ok=True)
    parts = out_dir.resolve().parts
    tag = re.sub(r'[^A-Za-z0-9_.-]+', '_', '__'.join(parts[-4:]))[-120:] + f'__s{seed}'
    script = generation_script(config, to_linux(out_dir / INPUT_DIR), to_linux(raw), seed, tag)
    # --exec: wsl.exe hands the arguments to bash unchanged (with `--` it re-parses them through the default shell,
    # which splits the multi-line script and expands $rc too early)
    command = (['wsl.exe', '-d', config['wsl_distro'], '--exec', 'bash', '-c', script] if WINDOWS else ['bash', '-c', script])
    lock = ROOT / config.get('lock_file', '.codex_jobs/_batch/pixal3d_gpu.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(log, 'a', encoding='utf-8') as handle:
        handle.write(f'\n== {now()} waiting for the GPU lock {portable_rel(lock)}\n')
        handle.flush()
        with portable.file_lock(lock, exclusive=True):
            handle.write(f'== {now()} generate seed {seed}\n')
            handle.flush()
            start = time.time()
            try:
                run = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, timeout=config.get('timeout_s', 3600))
            except subprocess.TimeoutExpired:
                raise Stop(f'generation timed out after {config.get("timeout_s", 3600)} s (log {log})')
            seconds = time.time() - start
    if run.returncode != 0 or not raw.is_file():
        tail = ' | '.join(Path(log).read_text(encoding='utf-8', errors='replace').strip().splitlines()[-5:])
        raise Stop(f'trellis-cli exited {run.returncode} (log {log}): {tail}')
    return seconds


# --------------------------------------------------------------------------- commands

def submit(views_dir, out_dir, seed_offset=0, config=None):
    config = config or load_config()
    out_dir = Path(out_dir)
    record = out_dir / RECORD
    if record.exists():
        raise Stop(f'{record} already exists; collect it instead of submitting again')
    out_dir.mkdir(parents=True, exist_ok=True)
    views, note = stage(views_dir, out_dir)
    write_json(record, {'backend': 'pixal3d', 'status': 'submitted', 'submitted_at': now(), 'project_id': None,
                        'views_dir': portable_rel(views_dir), 'views': views, 'cameras': note,
                        'seed': int(config.get('seed', 42)) + int(seed_offset), 'args': config['args'],
                        'cli': config['cli'], 'exports': {}})
    print(f'pixal3d: staged {len(views)} views ({", ".join(views)}); cameras from {note}; generation runs at collect')
    return 0


def collect(out_dir, config=None):
    config = config or load_config()
    out_dir = Path(out_dir)
    path = out_dir / RECORD
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if data.get('backend') != 'pixal3d':
        raise Stop(f'{path} belongs to backend {data.get("backend")!r}')
    exported = data.get('exports', {}).get('GLB')
    if data.get('status') == 'exported' and exported and Path(exported['path']).is_file():
        print(f'pixal3d: already exported {exported["path"]}')
        return 0
    if not (out_dir / INPUT_DIR / 'transforms.json').is_file():
        raise Stop(f'{out_dir / INPUT_DIR} is missing; submit again')
    log = out_dir / 'pixal3d.log'
    seed = int(data.get('seed', config.get('seed', 42)))
    print(f'pixal3d: generating ({len(data.get("views", []))} views, seed {seed}); log {log}', flush=True)
    try:
        seconds = run_generation(config, out_dir, seed, log)
    except Stop as error:
        data.setdefault('errors', []).append({'at': now(), 'error': str(error)})
        write_json(path, data)
        raise
    raw = out_dir / 'glb_pixal3d_raw.glb'
    gltf, binary = read_glb(raw)
    faces = face_count(gltf)
    if faces < config.get('min_faces', 20000):
        raise Stop(f'generated mesh has only {faces} faces (log {log})')
    if config.get('rotate_y_deg', 180) % 360 == 180:
        gltf, binary = rotate_y180(gltf, binary)
    elif config.get('rotate_y_deg', 180) % 360 != 0:
        raise Stop('rotate_y_deg must be 0 or 180')
    final = out_dir / DOWNLOAD
    write_glb(final, gltf, binary)
    raw.unlink()
    data.update(status='exported', exported_at=now(),
                exports={'GLB': {'path': str(final.resolve()), 'bytes': final.stat().st_size, 'sha256': sha256_file(final)}},
                generation={'seconds': round(seconds, 1), 'faces': faces, 'seed': seed,
                            'rotated_y_deg': config.get('rotate_y_deg', 180), 'log': portable_rel(log)})
    write_json(path, data)
    print(f'pixal3d: exported {final} ({faces:,} faces, {seconds:.0f} s)')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('submit')
    s.add_argument('views_dir')
    s.add_argument('out_dir')
    s.add_argument('--seed-offset', type=int, default=0, help='added to config seed (convert_job retries a broken mesh with 1, 2, ...)')
    s.add_argument('--dry-run', action='store_true', help='stage and print only; no record is written')
    c = sub.add_parser('collect')
    c.add_argument('out_dir')
    args = parser.parse_args(argv)
    try:
        if args.command == 'submit':
            if args.dry_run:
                Path(args.out_dir).mkdir(parents=True, exist_ok=True)
                views, note = stage(args.views_dir, args.out_dir)
                print(f'pixal3d dry run: would generate from {views} with cameras from {note}')
                return 0
            return submit(args.views_dir, args.out_dir, args.seed_offset)
        return collect(args.out_dir)
    except Stop as error:
        print(f'pixal3d: STOP {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
