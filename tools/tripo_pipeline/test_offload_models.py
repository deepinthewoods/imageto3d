import os
from pathlib import Path

import offload_models as om


def make(root):
    module = root / 'models' / 'set_a' / 'job1' / 'human_male' / 'outfit'
    (module / 'reduced').mkdir(parents=True)
    (module / 'wearcut' / 'prep').mkdir(parents=True)
    (module / 'source.glb').write_bytes(b'high poly')
    os.link(module / 'source.glb', module / 'glb_tripo_convert_x.glb')
    (module / 'reduced' / 'review.blend').write_bytes(b'review')
    (module / 'reduced' / 'lod_error_0p4.glb').write_bytes(b'low poly')
    (module / 'reduced' / 'manifest.json').write_text('{}')
    (module / 'wearcut' / 'prep' / 'prep.npz').write_bytes(b'prep')
    return module


def test_dry_run_moves_nothing(tmp_path):
    module = make(tmp_path)
    result = om.offload([tmp_path / 'models' / 'set_a' / 'job1'], tmp_path, tmp_path / 'archive', apply=False)
    assert result['files'] == 3 and not result['errors']
    assert (module / 'source.glb').is_file() and not (tmp_path / 'archive').exists()


def test_apply_moves_heavy_files_and_keeps_low_poly(tmp_path):
    module = make(tmp_path)
    dest = tmp_path / 'archive'
    result = om.offload([tmp_path / 'models' / 'set_a' / 'job1'], tmp_path, dest, apply=True)
    assert result['files'] == 3 and not result['errors'] and result['skipped'] is None
    moved = dest / 'set_a' / 'job1' / 'human_male' / 'outfit'
    assert (moved / 'source.glb').read_bytes() == b'high poly'
    assert os.path.samefile(moved / 'source.glb', moved / 'glb_tripo_convert_x.glb')   # still one file
    assert (moved / 'reduced' / 'review.blend').is_file() and (moved / 'wearcut' / 'prep' / 'prep.npz').is_file()
    assert not (module / 'glb_tripo_convert_x.glb').exists() and not (module / 'reduced' / 'review.blend').exists()
    assert (module / 'reduced' / 'lod_error_0p4.glb').read_bytes() == b'low poly'       # low poly stays
    assert (module / 'reduced' / 'manifest.json').is_file()
    source = module / 'source.glb'
    assert (source.is_symlink() and source.read_bytes() == b'high poly') or not source.exists()
    again = om.offload([tmp_path / 'models' / 'set_a' / 'job1'], tmp_path, dest, apply=True)
    assert again['files'] == 0                                                          # symlinks are not moved again


def test_missing_destination_drive_is_skipped(tmp_path):
    module = make(tmp_path)
    result = om.offload([tmp_path / 'models' / 'set_a' / 'job1'], tmp_path, Path('Q:/nowhere/models'), apply=True)
    assert result['skipped'] and result['files'] == 0 and (module / 'source.glb').is_file()
