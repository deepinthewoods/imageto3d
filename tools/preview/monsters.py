"""Monster variant scan of the preview app (added 2026-11-21 for the old site's Monsters tab; used by v2 since 2026-11-25).

Monsters are a much smaller pipeline than outfits (one image, one model, no races or modules; see
characters/monsters/aaa/AGENTS.md), and their files live outside this project, in

    characters/monsters/aaa/pipeline/<Monster Name>/views/<variant>/   front.png, colours.json, prompts.json
    characters/monsters/aaa/pipeline/<Monster Name>/models/<variant>/  hyper3d/glb_*.glb, reduced/lod_error_<lv>.glb,
                                                                        chosen_lod.json

so this module keeps its own scan, file route and one write (the LOD / approval choice). The v2 app (v2/monsters_v2.py,
v2/server.py) uses `cards`, `monsters_root`, `FILE_PREFIX`, `SERVED`, `LOD_LEVELS` and `CHOICE`; it writes the LOD / approval
itself under a per-variant lock (v2/decisions.py), so `write_choice` and `resolve_file` (the old site's route and writer) have no
caller left and are kept only with their tests. Nothing here is cached: there are few monsters.

Card shape (one per monster variant; deliberately none of the keys the outfit cards use to pick a modal view:
`races`, `views`, `jobs`, `entries`):

    {id: 'monster:<Monster>/<variant>', kind: 'monster', name, monster, variant, status, image, source_image,
     roles: [{role, hex, parts}], highpoly: url|None, lods: {'0p8': {url, faces, sections?, section_materials?, section_warnings?}, ...} (coarse to fine), lod, approved, record}
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from urllib.parse import quote, unquote

LOD_LEVELS = ('0p8', '0p6', '0p4', '0p2', '0p1')
CHOICE = 'chosen_lod.json'
FILE_PREFIX = '/mfiles/'
SERVED = ('.png', '.jpg', '.jpeg', '.webp', '.glb')


def monsters_root(project_root: Path) -> Path:
    """`characters/monsters/aaa` beside the imageto3d project (`$IMAGETO3D_MONSTERS` overrides). A test root in a temp
    folder has no such sibling, so its Monsters tab is simply empty."""
    override = os.environ.get('IMAGETO3D_MONSTERS')
    return Path(override) if override else Path(project_root).parent / 'monsters' / 'aaa'


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _dirs(path: Path) -> list[str]:
    try:
        with os.scandir(path) as entries:
            return sorted(e.name for e in entries if e.is_dir(follow_symlinks=False) and not e.name.startswith('.'))
    except OSError:
        return []


def _url(base: Path, path: Path) -> str | None:
    """`/mfiles/` URL of a regular file under the monsters root, or None. The version is the file's modification time in
    nanoseconds plus its size (2026-11-25: whole seconds let a file replaced within the same second keep its URL, so a
    browser could show the cached old model under the new one's name)."""
    try:
        if not path.is_file() or path.is_symlink():
            return None
        rel = path.resolve().relative_to(base.resolve()).as_posix()
        st = path.stat()
    except (OSError, ValueError):
        return None
    return FILE_PREFIX + quote(rel, safe='/') + f'?v={st.st_mtime_ns}-{st.st_size}'


def _faces(manifest: dict, level: str) -> int | None:
    """Face count of one reduced level from reduced/manifest.json (written by tools/reduce_monster.py in the monsters
    folder, or by reduce_model.py); None when the manifest has no such number."""
    entry = (manifest.get('levels') or {}).get(level)
    if not isinstance(entry, dict):
        entry = (manifest.get('levels') or {}).get(level.replace('p', '.'))
    for key in ('faces', 'triangles', 'face_count'):
        if isinstance(entry, dict) and isinstance(entry.get(key), int):
            return entry[key]
    return None


def _card(base: Path, monster: str, variant: str) -> dict | None:
    views = base / 'pipeline' / monster / 'views' / variant
    model = base / 'pipeline' / monster / 'models' / variant
    image = _url(base, views / 'front.png')
    record = _read(model / 'hyper3d' / 'studio_submission.json')
    highpoly = None
    try:
        names = sorted(n for n in os.listdir(model / 'hyper3d') if n.lower().endswith('.glb') and n.startswith('glb_'))
        highpoly = _url(base, model / 'hyper3d' / names[0]) if names else None
    except OSError:
        pass
    manifest = _read(model / 'reduced' / 'manifest.json')
    # sections/: the same levels with one material per section (monsters' tools/split_monster_sections.py)
    split = (_read(model / 'sections' / 'materials.json').get('levels') or {})
    lods = {}
    for level in LOD_LEVELS:
        url = _url(base, model / 'reduced' / f'lod_error_{level}.glb')
        if url:
            lods[level] = {'url': url, 'faces': _faces(manifest, level)}
            sections = _url(base, model / 'sections' / f'lod_error_{level}.glb')
            materials = (split.get(level) or {}).get('materials') if isinstance(split.get(level), dict) else None
            if sections and isinstance(materials, dict):
                lods[level] |= {'sections': sections,
                                'section_materials': [{'name': name, 'hex': value.get('display_hex'), 'kind': value.get('kind'), 'faces': value.get('faces')}
                                                      for name, value in materials.items() if isinstance(value, dict)],
                                'section_warnings': [w for w in (split[level].get('warnings') or []) if isinstance(w, str)]}
    if not image and not highpoly and not lods and not record:
        return None
    colours = _read(views / 'colours.json')
    roles = [{'role': role, 'hex': value.get('hex'), 'parts': value.get('parts') or []}
             for role, value in (colours.get('roles') or {}).items() if isinstance(value, dict)]
    prompts = _read(views / 'prompts.json')
    source = next((prompts[k] for k in ('source_image', 'source_image_path', 'source') if isinstance(prompts.get(k), str)), None)
    choice = _read(model / CHOICE)
    status = 'reduced' if lods else 'converted' if highpoly else record.get('status') or 'image only'
    return {'id': f'monster:{monster}/{variant}', 'kind': 'monster', 'name': f'{monster} {variant}', 'monster': monster,
            'variant': variant, 'status': status, 'image': image,
            'source_image': _url(base, Path(source)) if isinstance(source, str) and source else None,
            'roles': roles, 'highpoly': highpoly, 'highpoly_faces': (record.get('studio_result') or {}).get('faces'),
            'lods': lods, 'lod': choice.get('lod') if choice.get('lod') in LOD_LEVELS else None,
            'approved': choice.get('approved') is True,
            'record': {key: record.get(key) for key in ('status', 'stage', 'model_url', 'credits_committed') if key in record}}


def cards(project_root: Path) -> list[dict]:
    """One card per monster variant that has a conversion image or a model, monsters and variants in name order."""
    base = monsters_root(project_root)
    result = []
    for monster in _dirs(base / 'pipeline'):
        folder = base / 'pipeline' / monster
        for variant in sorted(set(_dirs(folder / 'views')) | set(_dirs(folder / 'models'))):
            card = _card(base, monster, variant)
            if card:
                result.append(card)
    return result


def resolve_file(project_root: Path, url_path: str) -> Path | None:
    """The file behind a `/mfiles/...` URL path: an image or GLB inside the monsters root, never anything else."""
    base = monsters_root(project_root)
    file = base / unquote(url_path.removeprefix(FILE_PREFIX))
    if file.suffix.lower() not in SERVED:
        return None
    try:
        file.resolve(strict=True).relative_to(base.resolve(strict=True))
    except (ValueError, OSError):
        return None
    return file if file.is_file() else None


def write_choice(project_root: Path, data: object) -> dict:
    """POST /api/monsters/choice {id: 'monster:<Monster>/<variant>', lod?: one of LOD_LEVELS, approved?: bool}.

    Writes pipeline/<Monster>/models/<variant>/chosen_lod.json, keeping the fields the request does not send. The
    monster and variant must be names the scan itself lists, so the request cannot point anywhere else."""
    if not isinstance(data, dict) or not isinstance(data.get('id'), str) or not data['id'].startswith('monster:'):
        raise ValueError('Invalid monster id')
    monster, _, variant = data['id'].removeprefix('monster:').partition('/')
    base = monsters_root(project_root)
    if monster not in _dirs(base / 'pipeline') or variant not in _dirs(base / 'pipeline' / monster / 'models'):
        raise ValueError('Monster model folder not found')
    folder = base / 'pipeline' / monster / 'models' / variant
    record = _read(folder / CHOICE)
    if 'lod' in data:
        if data['lod'] not in LOD_LEVELS:
            raise ValueError('Invalid LOD')
        if not (folder / 'reduced' / f'lod_error_{data["lod"]}.glb').is_file():
            raise ValueError('That LOD has no file')
        record |= {'lod': data['lod'], 'reduced': f'reduced/lod_error_{data["lod"]}.glb'}
    if 'approved' in data:
        if not isinstance(data['approved'], bool):
            raise ValueError('approved must be true or false')
        if data['approved'] and record.get('lod') not in LOD_LEVELS:
            raise ValueError('Choose a LOD before approving')
        record['approved'] = data['approved']
    record['updated'] = datetime.now(timezone.utc).isoformat()
    tmp = folder / (CHOICE + '.tmp')
    tmp.write_text(json.dumps(record, indent=1), encoding='utf-8')
    os.replace(tmp, folder / CHOICE)
    return {'ok': True, 'lod': record.get('lod'), 'approved': record.get('approved') is True}
