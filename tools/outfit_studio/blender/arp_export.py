"""Thin wrapper over blender/scripts/character_export: ARP's native Humanoid/Godot GLB export.

The character_export modules are reused as a library (no queue, no Armor Forge). Each export
changes the session (scene.arp_* settings, selection, mode), so batch callers run one job per
Blender process. Exported skeletons are fingerprinted by native_audit (skeleton_sha256).
"""
from pathlib import Path
import sys

import bpy

REPOSITORY = Path(__file__).resolve().parents[5]          # .../godot/blender
SCRIPTS = REPOSITORY / 'scripts'
ARP_MODULE = 'bl_ext.user_default.auto_rig_pro'


def ce(name):
    """Import one character_export submodule, e.g. ce('skin')."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import importlib
    return importlib.import_module('character_export.' + name)


def enable_arp():
    import addon_utils
    if not addon_utils.check(ARP_MODULE)[1]:
        bpy.ops.preferences.addon_enable(module=ARP_MODULE)


def fix_weights(meshes, rig):
    """Top-4 + normalise deform weights in place (export copies only); returns {name: [vertex ids]}."""
    skin = ce('skin')
    return {o.name: skin.normalize_export_weights(o, rig) for o in meshes}


def export_glb(path, rig, meshes, animations=False, twist=True):
    """Export meshes skinned to rig as a Godot GLB; returns native_audit.audit() of the file."""
    enable_arp()
    ce('standard_arp').export_native(path, rig, list(meshes), animations=animations)
    return ce('native_audit').audit(path, require_twist_weights=twist)
