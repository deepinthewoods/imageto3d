"""Render a candidate 2048px marker base from the frozen studio-plain scene.

Never replaces active base_views. Usage: blender -b -t 6 --python .../render_base_2k.py
"""
import bpy
from mathutils import Matrix
from pathlib import Path
import hashlib, json, time

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'assets/studio_plain_input.blend'
CAMERAS=ROOT/'base_views/human_male/transforms.json'
DEST=ROOT/'experiments/base_2k_studio_plain_20261021/human_male'
VIEWS=('front.png','front_three_quarter.png','back.png','back_three_quarter.png')
def digest(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as stream:
  for b in iter(lambda:stream.read(1<<20),b''):h.update(b)
 return h.hexdigest()
if DEST.exists():raise SystemExit(f'Candidate exists; preserve it: {DEST}')
DEST.mkdir(parents=True)
source_hash=digest(SOURCE);camera_hash=digest(CAMERAS)
bpy.ops.wm.open_mainfile(filepath=str(SOURCE))
scene=bpy.context.scene
assert scene.camera and len([o for o in scene.objects if o.type=='MESH'])==1
scene.render.resolution_x=scene.render.resolution_y=2048
scene.render.resolution_percentage=100
scene.render.image_settings.file_format='PNG'
scene.render.image_settings.color_mode='RGBA'
scene.render.film_transparent=True
camera_doc=json.loads(CAMERAS.read_text())
assert tuple(f['file_path'] for f in camera_doc['frames'])==VIEWS
began=time.time()
for frame in camera_doc['frames']:
 scene.camera.matrix_world=Matrix(frame['transform_matrix'])
 scene.render.filepath=str(DEST/frame['file_path'])
 bpy.ops.render.render(write_still=True)
 print('RENDERED',frame['file_path'],flush=True)
assert digest(SOURCE)==source_hash and digest(CAMERAS)==camera_hash
record={'candidate_resolution':[2048,2048],'status':'candidate_not_active','source_scene':str(SOURCE),'source_sha256':source_hash,'camera_file':str(CAMERAS),'camera_sha256':camera_hash,'views':{name:digest(DEST/name) for name in VIEWS},'render_seconds':round(time.time()-began,2),'note':'Camera matrices, geometry, materials and lighting from the frozen studio-plain marker scene. Active 1024 base views and staged armor images are unchanged.'}
(DEST/'render.json').write_text(json.dumps(record,indent=2)+'\n')
