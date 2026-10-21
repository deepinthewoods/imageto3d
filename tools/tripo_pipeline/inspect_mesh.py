"""Blender headless mesh topology report for a source GLB or OBJ."""
import bpy, bmesh, json, sys
from collections import Counter
from pathlib import Path

source=Path(sys.argv[sys.argv.index('--')+1]).resolve()
output=Path(sys.argv[sys.argv.index('--')+2]).resolve()
bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)
if source.suffix.lower()=='.glb': bpy.ops.import_scene.gltf(filepath=str(source))
elif source.suffix.lower()=='.obj': bpy.ops.wm.obj_import(filepath=str(source))
else: raise SystemExit(f'Unsupported: {source}')
meshes=[obj for obj in bpy.context.scene.objects if obj.type=='MESH']
parts=[]
for obj in meshes:
 mesh=obj.data; bm=bmesh.new();bm.from_mesh(mesh);bm.normal_update()
 face_sizes=Counter(len(f.verts) for f in bm.faces)
 boundary=sum(e.is_boundary for e in bm.edges)
 nonmanifold=sum(not e.is_manifold for e in bm.edges)
 loose=sum(e.is_wire for e in bm.edges)
 world=[obj.matrix_world @ v.co for v in mesh.vertices]
 bounds=[[min(v[i] for v in world),max(v[i] for v in world)] for i in range(3)] if world else None
 parts.append({'object':obj.name,'vertices':len(mesh.vertices),'edges':len(mesh.edges),'polygons':len(mesh.polygons),'triangle_equivalent':sum(len(p.vertices)-2 for p in mesh.polygons),'face_sizes':dict(face_sizes),'boundary_edges':boundary,'nonmanifold_edges':nonmanifold,'loose_edges':loose,'bounds_xyz':bounds,'materials':[m.name for m in mesh.materials if m]})
 bm.free()
report={'source':str(source),'mesh_count':len(meshes),'parts':parts,'totals':{'vertices':sum(p['vertices'] for p in parts),'polygons':sum(p['polygons'] for p in parts),'triangle_equivalent':sum(p['triangle_equivalent'] for p in parts),'quads':sum(p['face_sizes'].get(4,0) for p in parts)}}
output.write_text(json.dumps(report,indent=2))
print(json.dumps(report['totals']))
