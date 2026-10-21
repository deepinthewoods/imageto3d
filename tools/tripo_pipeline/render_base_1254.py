"""Render native 1254-square marker views from the frozen studio-plain scene.

Usage: blender -b -t 6 --python tools/tripo_pipeline/render_base_1254.py
The output is a versioned candidate; promotion into base_views is a separate step.
"""
import bpy
from mathutils import Matrix
from pathlib import Path
import hashlib, json, shutil, time

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'lowpoly algo/results/armor_09/studio_plain_input.blend'
CAMERAS=ROOT/'base_views/human_male/transforms.json'
DEST=ROOT/'experiments/base_1254_studio_plain_20261021/human_male'
SIZE=1254
VIEWS=('front.png','front_three_quarter.png','back.png','back_three_quarter.png')
def digest(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as stream:
  for b in iter(lambda:stream.read(1<<20),b''):h.update(b)
 return h.hexdigest()
if DEST.exists():raise SystemExit(f'Render destination exists; preserve it: {DEST}')
DEST.mkdir(parents=True)
source_hash=digest(SOURCE);camera_hash=digest(CAMERAS)
bpy.ops.wm.open_mainfile(filepath=str(SOURCE))
scene=bpy.context.scene
assert scene.camera and len([o for o in scene.objects if o.type=='MESH'])==1
scene.render.resolution_x=scene.render.resolution_y=SIZE
scene.render.resolution_percentage=100
scene.render.image_settings.file_format='PNG'
scene.render.image_settings.color_mode='RGBA'
scene.render.film_transparent=True
frames=json.loads(CAMERAS.read_text())['frames']
assert tuple(f['file_path'] for f in frames)==VIEWS
began=time.time()
for frame in frames:
 scene.camera.matrix_world=Matrix(frame['transform_matrix'])
 scene.render.filepath=str(DEST/frame['file_path'])
 bpy.ops.render.render(write_still=True)
 print('RENDERED',frame['file_path'],flush=True)
assert digest(SOURCE)==source_hash and digest(CAMERAS)==camera_hash
shutil.copy2(CAMERAS,DEST/'transforms.json')
record={'resolution':[SIZE,SIZE],'status':'candidate_not_active','source_scene':str(SOURCE),'source_sha256':source_hash,'camera_file':str(CAMERAS),'camera_sha256':camera_hash,'views':{name:digest(DEST/name) for name in VIEWS},'render_seconds':round(time.time()-began,2),'alpha':'RGBA transparent','method':'native Blender Cycles render; no image resizing'}
(DEST/'render.json').write_text(json.dumps(record,indent=2)+'\n')
