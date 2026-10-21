"""Read GLB metadata without changing the source files."""
import hashlib
import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parent
PATHS = {
    'pixal_high': ROOT.parent / 'models/base/human_male/human_male.glb',
    'hyper_high': ROOT.parent / 'hyper3d/human male highpoly/base_basic_shaded.glb',
    'hyper_low': ROOT.parent / 'hyper3d/human male lowpoly/base_basic_shaded.glb',
    'hyper_high_pbr': ROOT.parent / 'hyper3d/human male highpoly/base_basic_pbr.glb',
    'hyper_low_pbr': ROOT.parent / 'hyper3d/human male lowpoly/base_basic_pbr.glb',
}


def inspect(path):
    raw = path.read_bytes()
    size, kind = struct.unpack_from('<II', raw, 12)
    data = json.loads(raw[20:20+size])
    bin_start = 20 + size + 8
    primitives = []
    for mesh in data.get('meshes', []):
        for prim in mesh['primitives']:
            attrs = {}
            for key, idx in {**prim['attributes'], 'indices': prim.get('indices')}.items():
                if idx is None:
                    continue
                acc = data['accessors'][idx]
                view = data['bufferViews'][acc['bufferView']]
                offset = bin_start + view.get('byteOffset', 0)
                attrs[key] = {**acc, 'buffer_sha256': hashlib.sha256(raw[offset:offset+view['byteLength']]).hexdigest()}
            primitives.append({'mode': prim.get('mode', 4), 'attributes': attrs})
    return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'generator': data.get('asset'), 'primitives': primitives,
            'materials': data.get('materials'), 'nodes': data.get('nodes'),
            'images': [{k:v for k,v in i.items() if k != 'uri'} for i in data.get('images', [])]}


if __name__ == '__main__':
    out = ROOT / 'results/run_01'
    out.mkdir(parents=True, exist_ok=True)
    results = {name: inspect(path) for name, path in PATHS.items()}
    (out / 'input_manifest.json').write_text(json.dumps(results, indent=2))
    for name, data in results.items():
        print(name, [(p['attributes']['POSITION']['count'], p['attributes']['indices']['count']//3)
                     for p in data['primitives']])
        print('materials', json.dumps(data['materials'])[:900])
