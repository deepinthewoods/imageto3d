"""Build a reversible Blender comparison scene from two original source meshes."""
import bpy, math, sys
from pathlib import Path
from mathutils import Vector
root=Path(sys.argv[sys.argv.index('--')+1]).resolve()
old=root/'tripo/low-poly humanoid 3d model 2k nopbr 25credits studio plain paper.glb'
new=root/'models/base_quad_comparison_20261021/obj/low-poly_humanoid_3d_model.obj'
out=root/'models/base_quad_comparison_20261021'
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
def collection(name):
 c=bpy.data.collections.new(name);bpy.context.scene.collection.children.link(c);return c
def import_group(path,name,target_x,color):
 before=set(bpy.context.scene.objects)
 if path.suffix=='.glb':bpy.ops.import_scene.gltf(filepath=str(path))
 else:bpy.ops.wm.obj_import(filepath=str(path))
 objs=[o for o in bpy.context.scene.objects if o not in before]
 meshes=[o for o in objs if o.type=='MESH']
 c=collection(name)
 for o in objs:
  for oc in list(o.users_collection):oc.objects.unlink(o)
  c.objects.link(o)
 worldcoords=[o.matrix_world@v.co for o in meshes for v in o.data.vertices]
 minz=min(v.z for v in worldcoords); maxz=max(v.z for v in worldcoords)
 minx=min(v.x for v in worldcoords); maxx=max(v.x for v in worldcoords)
 midy=(min(v.y for v in worldcoords)+max(v.y for v in worldcoords))/2
 scale=1/(maxz-minz)
 root_obj=bpy.data.objects.new(name+'_PLACEMENT',None);c.objects.link(root_obj)
 root_obj.location=(target_x-(minx+maxx)/2*scale,-midy*scale,-minz*scale)
 root_obj.scale=(scale,)*3
 for o in objs:
  if o.parent not in objs:
   world=o.matrix_world.copy();o.parent=root_obj;o.matrix_world=world
 for o in meshes:o.color=(*color,1)
 return meshes
import_group(old,'Triangle reference',-0.60,(0.86,0.54,0.28))
import_group(new,'Quad test',0.60,(0.35,0.72,0.92))
for name,x,color in [('TRIANGLE / 660,574 faces',-0.60,(0.95,0.75,0.49)),('QUAD / 44,388 faces',0.60,(0.55,0.82,0.98))]:
 curve=bpy.data.curves.new(name,'FONT');curve.body=name;curve.size=.065;curve.align_x='CENTER'
 obj=bpy.data.objects.new(name,curve);bpy.context.scene.collection.objects.link(obj);obj.location=(x,-.03,-.15);obj.rotation_euler[0]=math.pi/2;obj.color=(*color,1)
cam_data=bpy.data.cameras.new('Comparison camera');cam=bpy.data.objects.new('Comparison camera',cam_data);bpy.context.scene.collection.objects.link(cam);bpy.context.scene.camera=cam;cam_data.type='ORTHO';cam_data.ortho_scale=2.55
scene=bpy.context.scene;scene.render.engine='BLENDER_WORKBENCH';scene.display.shading.light='STUDIO';scene.display.shading.color_type='OBJECT';scene.display.shading.show_shadows=True;scene.display.shading.show_cavity=True;scene.display.shading.cavity_type='BOTH';scene.display.shading.curvature_ridge_factor=1.25;scene.display.shading.curvature_valley_factor=1.25
scene.render.resolution_x=1800;scene.render.resolution_y=1000;scene.render.resolution_percentage=100
scene.render.image_settings.file_format='PNG';scene.render.film_transparent=False;scene.world.color=(.06,.06,.06)
def point_camera(pos):
 cam.location=pos;target=Vector((0,0,.46));cam.rotation_euler=(target-cam.location).to_track_quat('-Z','Y').to_euler()
point_camera((0,-4,.55));scene.render.filepath=str(out/'comparison_front.png');bpy.ops.render.render(write_still=True)
point_camera((1.1,-4,.65));scene.render.filepath=str(out/'comparison_three_quarter.png');bpy.ops.render.render(write_still=True)
point_camera((0,-4,.55));bpy.ops.wm.save_as_mainfile(filepath=str(out/'quad_vs_triangle.blend'))
