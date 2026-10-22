"""Render the four 1254-square marker views of a marker scene with the active camera matrices.

Usage: blender -b -t 6 --python tools/tripo_pipeline/render_marker_base.py -- <scene.blend> <dest dir> [transforms.json]

Cameras default to base_views/human_male/transforms.json (45 deg three-quarters: front-left from
above, back-right from below). The scene's own render settings, lights and world are used as is.
Writes the PNGs, a copy of the camera file and render.json. Never overwrites a destination.
"""
from pathlib import Path
import hashlib, json, shutil, sys, time

import bpy
from mathutils import Matrix

ROOT = Path(__file__).resolve().parents[2]
SIZE = 1254
VIEWS = ('front.png', 'front_three_quarter.png', 'back.png', 'back_three_quarter.png')


def digest(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as stream:
        for b in iter(lambda: stream.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def main(source, dest, cameras=ROOT / 'base_views/human_male/transforms.json'):
    source, dest, cameras = Path(source).resolve(), Path(dest), Path(cameras).resolve()
    if dest.exists():
        raise SystemExit(f'Render destination exists; preserve it: {dest}')
    dest.mkdir(parents=True)
    source_hash, camera_hash = digest(source), digest(cameras)
    bpy.ops.wm.open_mainfile(filepath=str(source))
    scene = bpy.context.scene
    assert scene.camera and len([o for o in scene.objects if o.type == 'MESH']) == 1
    scene.render.resolution_x = scene.render.resolution_y = SIZE
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.film_transparent = True
    frames = json.loads(cameras.read_text())['frames']
    assert tuple(f['file_path'] for f in frames) == VIEWS
    began = time.time()
    for frame in frames:
        scene.camera.matrix_world = Matrix(frame['transform_matrix'])
        scene.render.filepath = str(dest / frame['file_path'])
        bpy.ops.render.render(write_still=True)
        print('RENDERED', frame['file_path'], flush=True)
    assert digest(source) == source_hash and digest(cameras) == camera_hash
    shutil.copy2(cameras, dest / 'transforms.json')
    record = {'resolution': [SIZE, SIZE], 'status': 'candidate_not_active', 'source_scene': str(source),
              'source_sha256': source_hash, 'camera_file': str(cameras), 'camera_sha256': camera_hash,
              'views': {name: digest(dest / name) for name in VIEWS},
              'render_seconds': round(time.time() - began, 2), 'alpha': 'RGBA transparent',
              'method': 'native Blender Cycles render; no image resizing'}
    (dest / 'render.json').write_text(json.dumps(record, indent=2) + '\n')


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--') + 1:]
    if len(args) not in (2, 3):
        raise SystemExit(__doc__)
    main(*args)
