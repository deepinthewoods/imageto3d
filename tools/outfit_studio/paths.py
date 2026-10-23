"""Filesystem layout of the Outfit Studio add-on (SPEC section 1). Symlinks (the add-on folder
is linked into Blender's addons dir) are resolved, so ROOT is always the imageto3d folder."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[2]
PROJECTS = ROOT / 'projects'
OUTFIT_PROJECTS = ROOT / 'outfit_projects'
MODELS = ROOT / 'models'
BASE_VIEWS = ROOT / 'base_views'
BLENDER = ROOT.parents[1]                      # .../godot/blender
ANIMS = BLENDER / 'anims'
HEADS = BLENDER / 'export' / 'heads'
EXPORT = BLENDER / 'export' / 'outfit_studio'
VIEW_STAGES = ('completed', 'multiview_ready', 'processing', 'processing_views')


def bases():
    """The base names listed in projects/manifest.json (race_gender)."""
    return sorted(json.loads((PROJECTS / 'manifest.json').read_text()))


def base_project(base):
    return PROJECTS / f'{base}.blend'


def outfit_project(base):
    return OUTFIT_PROJECTS / f'{base}_outfits.blend'


def base_views(base):
    return BASE_VIEWS / base


def export_dir(batch):
    return EXPORT / batch


def view_image(set_id, job_id, race, module, view):
    """First existing `<stage>/<set>--<job>/<race>/<module>/<view>.png`, else None."""
    for stage in VIEW_STAGES:
        p = ROOT / stage / f'{set_id}--{job_id}' / race / module / f'{view}.png'
        if p.exists():
            return p
    return None
