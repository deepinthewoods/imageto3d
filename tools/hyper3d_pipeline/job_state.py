"""Durable per-input Rodin metadata. No credentials or signed download URLs."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse
import uuid
import zipfile

BASE = 'https://hyper3d.ai/workspace/rodin'
MINE = BASE + '/tab/mine'


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def model_id(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.netloc != 'hyper3d.ai':
        return None
    match = re.fullmatch(r'/workspace/rodin/([0-9a-fA-F-]{36})/?', parsed.path)
    if not match:
        return None
    try:
        return str(uuid.UUID(match[1]))
    except ValueError:
        return None


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Job:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.data = json.loads(self.path.read_text())

    @classmethod
    def create(cls, root, image):
        image = Path(image).resolve(strict=True)
        digest = sha256(image)
        path = Path(root).resolve() / digest / 'job.json'
        if not path.exists():
            atomic_json(path, {
                'schema_version': 1, 'created_at': now(), 'updated_at': now(),
                'input': {'path': str(image), 'sha256': digest},
                'model_id': None, 'model_url': None, 'gallery_url': MINE,
                'stage': 'new', 'pending_action': None, 'actions': {},
                'settings': {'model': 'Gen-2.5', 'effort': 'Medium',
                             'mesh': 'Smart Low-poly', 'density': 'High',
                             'mesh_type': 'Quad', 'de_light': True,
                             'format': 'fbx', 'texture_resolution': '2K'},
                'free_redo_limit': 3, 'free_redos': 0,
            })
        return cls(path)

    def save(self, event, **changes):
        self.data.update(changes)
        self.data['updated_at'] = now()
        atomic_json(self.path, self.data)
        with self.path.with_name('events.jsonl').open('a') as stream:
            stream.write(json.dumps({'at': now(), 'event': event, **changes}) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def bind(self, url):
        ident = model_id(url)
        if not ident:
            raise ValueError('Expected a Hyper3D model URL ending in a UUID')
        if self.data.get('model_id') not in (None, ident):
            raise ValueError('This input is already bound to a different model')
        self.save('model_linked', model_id=ident, model_url=BASE + '/' + ident)

    def intent(self, action):
        # Write before clicking: never repeat an uncertain submission automatically.
        if self.data.get('pending_action'):
            raise RuntimeError('An earlier submission is unresolved; inspect the saved model first')
        if action in self.data['actions']:
            raise RuntimeError(f'{action} was already submitted; refusing to repeat it')
        actions = dict(self.data['actions'])
        actions[action] = {'intent_at': now()}
        self.save('submission_intent', pending_action=action, actions=actions)

    def observed(self, stage):
        self.save('server_stage_observed', stage=stage, pending_action=None)

    def verify_input(self):
        if sha256(self.data['input']['path']) != self.data['input']['sha256']:
            raise ValueError('Input image changed since this job was created')


def verify_zip(path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError('ZIP checksum failed')
        names = archive.namelist()
        if not any(n.lower().endswith('.fbx') for n in names):
            raise ValueError('Export has no FBX model')
        if not any(n.lower().endswith('.png') for n in names):
            raise ValueError('Export has no texture PNGs')
    return {'path': str(Path(path).resolve()), 'sha256': sha256(path),
            'bytes': Path(path).stat().st_size, 'files': names}
