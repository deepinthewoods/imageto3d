"""The attachment-point group vocabulary (names, regions, directions) and default settings.

Same groups as the old generator (`pipeline.py` LIMB_SEGMENTS / TORSO_LINES / HAIR_CORNROWS):
`crown`, `templeL/R`, `scalp_{c,l1,r1,l2,r2}`, `scalp_rand`, `face_rand`, the limb rows
`<armL|armR|forearmL|...|shoulderR>_<dir>`, `torso_<f|fl|l|bl|b|br|r|fr>`, `<region>_rand`, `torso_rand`.
"""
LIMBS = {   # region -> (part (material) name, axis bone, direction tokens)
    'armL': ('arm_stretch.l', 'arm_stretch.l', ('up', 'dn', 'fwd', 'bk')),
    'armR': ('arm_stretch.r', 'arm_stretch.r', ('up', 'dn', 'fwd', 'bk')),
    'forearmL': ('forearm_stretch.l', 'forearm_stretch.l', ('up', 'dn', 'fwd', 'bk')),
    'forearmR': ('forearm_stretch.r', 'forearm_stretch.r', ('up', 'dn', 'fwd', 'bk')),
    'thighL': ('thigh_stretch.l', 'thigh_stretch.l', ('in', 'out', 'fwd', 'bk')),
    'thighR': ('thigh_stretch.r', 'thigh_stretch.r', ('in', 'out', 'fwd', 'bk')),
    'shinL': ('leg_stretch.l', 'leg_stretch.l', ('in', 'out', 'fwd', 'bk')),
    'shinR': ('leg_stretch.r', 'leg_stretch.r', ('in', 'out', 'fwd', 'bk')),
    'shoulderL': ('shoulder.l', 'shoulder.l', ('up', 'dn', 'fwd', 'bk')),
    'shoulderR': ('shoulder.r', 'shoulder.r', ('up', 'dn', 'fwd', 'bk')),
}
TORSO_ANGLE = {'f': 0, 'fl': 45, 'l': 90, 'bl': 135, 'b': 180, 'br': 225, 'r': 270, 'fr': 315}
TORSO_PARTS = ('spine_01.x', 'root.x')
TORSO_BONES = ('spine_01.x', 'spine_02.x', 'spine_03.x')
HEAD_PART = 'head.x'
SINGLES = ('crown', 'templeL', 'templeR')
HAIR_ROWS = ('c', 'l1', 'r1', 'l2', 'r2')
ROW_OFFSET = {'c': 0.0, 'l1': 0.31, 'l2': 0.62}      # lateral offset of the scalp rows, fraction of the scalp half-width

DEFAULT_COUNT = {'hair': 12, 'limbs': 6, 'torso': 8, 'scatter': 24}

DEFAULTS = {
    'strategy_hair': 'landmark_geodesic',     # landmark_geodesic | template | texture | legacy (scalp + crown/temples)
    'strategy_scatter': 'landmark_geodesic',  # same choices, for scalp_rand / face_rand
    'strategy_limbs': 'cylinder',             # cylinder | keep (leave the old points alone)
    'strategy_torso': 'cylinder',
    'gen_hair': True, 'gen_scatter': True, 'gen_limbs': True, 'gen_torso': True,
    'row_mode': 'keep',                       # keep: reuse the rig's counts; spacing: count = row length / spacing
    'spacing': 0.04,                          # metres: row spacing ('spacing' mode) and minimum scatter gap (0 = count only)
    'density': 1.0,                           # multiplies every count
    'exclude_joints': True,
    'joint_purity': 0.8,                      # a point is at a joint if its largest segment weight is below this
    'joint_floor': 0.55,                      # a row/scatter relaxes the purity down to this when the pure part is a sliver
    'hairline': 0.20,                         # hairline distance from the brow, fraction of head height
    'normal_radius': 0.015,                   # normal smoothing radius, fraction of body height
    'symmetry': True,
    'template': 'default',                    # reference head name for the template strategy
    'texture_image': '',                      # image name for the texture strategy
    'limb_tangent': 'proximal',               # proximal | distal: which way a limb point's +Z runs along the bone
}


def family_of(group):
    """'hair' | 'scatter' | 'limbs' | 'torso' | None for a group name."""
    if group in SINGLES or (group.startswith('scalp_') and group != 'scalp_rand'):
        return 'hair'
    if group.endswith('_rand'):
        return 'scatter'
    if group.startswith('torso_') and group[6:] in TORSO_ANGLE:
        return 'torso'
    region = group.rsplit('_', 1)[0]
    if region in LIMBS and group.rsplit('_', 1)[1] in LIMBS[region][2]:
        return 'limbs'
    return None


def mirror_of(group):
    """The group on the other side (`armL_up` -> `armR_up`), the group itself for a centre group."""
    if group in ('templeL', 'templeR'):
        return {'templeL': 'templeR', 'templeR': 'templeL'}[group]
    if group.startswith('scalp_') and group[6:] in ('l1', 'r1', 'l2', 'r2'):
        return 'scalp_' + {'l': 'r', 'r': 'l'}[group[6]] + group[7:]
    if group.startswith('torso_') and group[6:] in TORSO_ANGLE:
        t = group[6:]
        return 'torso_' + (t.replace('l', 'r') if 'l' in t else t.replace('r', 'l') if 'r' in t else t)
    region, _, tail = group.rpartition('_')
    if region in LIMBS:
        return f"{region[:-1] + {'L': 'R', 'R': 'L'}[region[-1]]}_{tail}"
    return group


def is_left(group):
    """True for the groups generated first: those on the left (+X) side. Centre groups are not."""
    if group == 'templeL':
        return True
    if group.startswith('scalp_'):
        return group[6:] in ('l1', 'l2')
    if group.startswith('torso_'):
        return group[6:] in ('fl', 'l', 'bl')
    region = group.rpartition('_')[0]
    return region in LIMBS and region.endswith('L')


def default_groups(families=('hair', 'scatter', 'limbs', 'torso')):
    """Every group name with its default count, for a rig that has no points yet."""
    out = {}
    if 'hair' in families:
        out.update({g: 1 for g in SINGLES})
        out.update({f'scalp_{r}': DEFAULT_COUNT['hair'] for r in HAIR_ROWS})
    if 'limbs' in families:
        for region, (_, _, dirs) in LIMBS.items():
            out.update({f'{region}_{d}': DEFAULT_COUNT['limbs'] for d in dirs})
    if 'torso' in families:
        out.update({f'torso_{t}': DEFAULT_COUNT['torso'] for t in TORSO_ANGLE})
    if 'scatter' in families:
        out.update({'scalp_rand': DEFAULT_COUNT['scatter'], 'face_rand': DEFAULT_COUNT['scatter'],
                    'torso_rand': DEFAULT_COUNT['scatter']})
        out.update({f'{r}_rand': DEFAULT_COUNT['scatter'] for r in LIMBS})
    return out
