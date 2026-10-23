"""Generated proxy props for the sockets view, plus the temp-data helpers shared with views.py.

Everything created here is registered in a `reg` dict ({'objs': [...], 'data': [(kind, name)]}) so the
view reset can delete it again. Props are simple boxes/cylinders/spheres parented to the socket bones."""
import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

VIEW_COLLECTION = 'OS_View'


def new_reg():
    return {'objs': [], 'data': []}


def view_collection(scene):
    c = bpy.data.collections.get(VIEW_COLLECTION)
    if c is None:
        c = bpy.data.collections.new(VIEW_COLLECTION)
    if c.name not in scene.collection.children:
        scene.collection.children.link(c)
    return c


def new_mat(reg, name, rgba):
    m = bpy.data.materials.new(name)
    m.diffuse_color = rgba
    reg['data'].append(('materials', m.name))
    return m


def new_mesh(reg, name, bm_or_pydata, mat=None):
    me = bpy.data.meshes.new(name)
    if isinstance(bm_or_pydata, bmesh.types.BMesh):
        bm_or_pydata.to_mesh(me)
        bm_or_pydata.free()
    else:
        me.from_pydata(*bm_or_pydata)
        me.update()
    if mat:
        me.materials.append(mat)
    reg['data'].append(('meshes', me.name))
    return me


def new_obj(reg, scene, name, data, matrix=None):
    o = bpy.data.objects.new(name, data)
    view_collection(scene).objects.link(o)
    if matrix is not None:
        o.matrix_world = matrix
    reg['objs'].append(o.name)
    return o


def parent_to_bone(obj, rig, bone, world, pose_matrix=None):
    """Bone-parent `obj` so it sits at `world` now and follows `bone` afterwards (bone parents use the bone tail).

    `pose_matrix` = world matrix of the posed bone (see `evaluated_pose_matrix`); default is the rest matrix."""
    b = rig.data.bones[bone]
    pm = (pose_matrix if pose_matrix is not None else rig.matrix_world @ b.matrix_local) @ Matrix.Translation((0, b.length, 0))
    obj.parent, obj.parent_type, obj.parent_bone = rig, 'BONE', bone
    obj.matrix_parent_inverse = pm.inverted()
    obj.matrix_basis = world


def evaluated_pose_matrix(rig, bone, depsgraph):
    """World matrix of a posed bone (head at the translation) from the evaluated rig."""
    ev = rig.evaluated_get(depsgraph)
    return ev.matrix_world @ ev.pose.bones[bone].matrix


# ---------------------------------------------------------------- shapes (bone local, +Y along the bone)
def _box(bm, centre, size):
    bmesh.ops.create_cube(bm, size=1.0, matrix=Matrix.LocRotScale(Vector(centre), None, Vector(size)))


def _cyl(bm, centre, radius, depth, axis=2):
    rot = {0: Matrix.Rotation(np.pi / 2, 3, 'Y'), 1: Matrix.Rotation(np.pi / 2, 3, 'X'), 2: Matrix.Identity(3)}[axis]
    bmesh.ops.create_cone(bm, cap_ends=True, segments=24, radius1=radius, radius2=radius, depth=depth,
                          matrix=Matrix.Translation(Vector(centre)) @ rot.to_4x4())


def _ball(bm, centre, radius):
    bmesh.ops.create_uvsphere(bm, u_segments=12, v_segments=8, radius=radius, matrix=Matrix.Translation(Vector(centre)))


def _axis_toward(world_dir, basis):
    """Local principal axis (0/1/2) of rotation matrix `basis` that best aligns with `world_dir`."""
    return int(np.argmax(np.abs(np.array(basis.inverted() @ Vector(world_dir)))))


def _sword(bm, u, _):
    _box(bm, (0, 0.50 * u, 0), (0.05 * u, 0.80 * u, 0.012 * u))      # blade
    _box(bm, (0, 0.08 * u, 0), (0.18 * u, 0.025 * u, 0.04 * u))      # guard
    _cyl(bm, (0, -0.02 * u, 0), 0.016 * u, 0.20 * u, axis=1)         # grip


def _shield(bm, u, ax):
    _cyl(bm, (0, 0, 0), 0.20 * u, 0.025 * u, axis=ax)
    _ball(bm, (0, 0, 0), 0.035 * u)


def _sheath(length):
    def f(bm, u, _):
        _box(bm, (0, length * u / 2, 0), (0.05 * u, length * u, 0.03 * u))
    return f


def _glow(bm, u, _):
    _ball(bm, (0, 0, 0), 0.035 * u)


def _marker(bm, u, _):
    _box(bm, (0, 0, 0), (0.03 * u, 0.03 * u, 0.03 * u))


#            socket                 shape            colour
PROPS = (('RightHandAttachment', _sword, (0.75, 0.78, 0.85, 1)),
         ('LeftHandAttachment', _sword, (0.75, 0.78, 0.85, 1)),
         ('ShieldAttachment', _shield, (0.55, 0.30, 0.15, 1)),
         ('BackShield', _shield, (0.45, 0.28, 0.15, 1)),
         ('BackSheath', _sheath(0.9), (0.30, 0.20, 0.12, 1)),
         ('HipSheath', _sheath(0.38), (0.30, 0.20, 0.12, 1)),
         ('CastEmissionRight', _glow, (0.2, 0.9, 1.0, 1)),
         ('CastEmissionLeft', _glow, (0.2, 0.9, 1.0, 1)),
         ('LeftKneePole', _marker, (0.9, 0.6, 0.1, 1)),
         ('RightKneePole', _marker, (0.9, 0.6, 0.1, 1)))


def build(context, reg, unit=1.0):
    """Create the proxy props at the socket bones (rest placement); `unit` = body height / 1.75 m."""
    scene = context.scene
    from . import project
    rig, _ = project.find_base(scene)
    made = []
    for name, shape, col in PROPS:
        if name not in rig.data.bones:
            continue
        M = rig.matrix_world @ rig.data.bones[name].matrix_local
        facing = (0, -1, 0) if name == 'ShieldAttachment' else (0, 1, 0)
        bm = bmesh.new()
        shape(bm, unit, _axis_toward(facing, M.to_3x3()))
        mat = new_mat(reg, f'OSV_{name}', col)
        o = new_obj(reg, scene, f'OSV_prop_{name}', new_mesh(reg, f'OSV_{name}', bm, mat))
        parent_to_bone(o, rig, name, M)
        made.append(o)
    return made


CLASSES = ()
