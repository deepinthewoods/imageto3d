"""Outfit Studio: Blender add-on to browse, edit, validate and export pipeline outfits and armour."""
bl_info = {
    'name': 'Outfit Studio',
    'author': 'imageto3d pipeline',
    'version': (0, 1, 0),
    'blender': (5, 1, 0),
    'location': 'View3D > Sidebar > Outfit Studio',
    'description': 'Import pipeline armour/outfits onto linked bases, review, edit and export',
    'category': '3D View',
}

import importlib
import traceback

# registration order matters: props before everything that reads scene.ostudio, ui last
MODULES = ('props', 'project', 'items', 'importer', 'weights', 'morphs', 'views', 'ops_outfit',
           'proxies', 'ap_ops', 'export_base', 'anims', 'spacing', 'validate', 'export_items', 'ui')
_loaded = []


def _import(name):
    full = f'{__name__}.blender.{name}'
    try:
        return importlib.import_module(full)
    except ModuleNotFoundError as e:
        if e.name == full:
            print(f'[outfit_studio] module blender.{name} not present, skipped')
        else:
            print(f'[outfit_studio] blender.{name} needs {e.name!r}, skipped')
    except Exception:
        print(f'[outfit_studio] blender.{name} failed to import, skipped')
        traceback.print_exc()
    return None


def register():
    import bpy
    for name in MODULES:
        mod = _import(name)
        if mod is None:
            continue
        done = []
        try:
            for cls in getattr(mod, 'CLASSES', ()):
                bpy.utils.register_class(cls)
                done.append(cls)
            if hasattr(mod, 'register'):
                mod.register()
        except Exception:
            print(f'[outfit_studio] registering blender.{name} failed')
            traceback.print_exc()
            for cls in reversed(done):
                try:
                    bpy.utils.unregister_class(cls)
                except Exception:
                    pass
            continue
        _loaded.append(mod)


def unregister():
    import bpy
    while _loaded:
        mod = _loaded.pop()
        try:
            if hasattr(mod, 'unregister'):
                mod.unregister()
        except Exception:
            traceback.print_exc()
        for cls in reversed(getattr(mod, 'CLASSES', ())):
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass
