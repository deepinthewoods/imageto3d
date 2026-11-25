"""rebuild_match.py <races,|all> <set substring> [module (body_armor|outfit|right_gauntlet_boot)]: rebuild the matching modules' layers (writes
out/layers like the full build), then rewrite the index."""
import multiprocessing as mp
import os
import sys
import time

HERE = 'F:/godot/blender/characters/imageto3d/experiments/shell_slots_20261122'

if __name__ == '__main__':
    os.environ['BODY_FIT_GPU'] = '1'
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    sys.path.insert(0, HERE)
    import layers as L
    import process_all as pa
    races = None if sys.argv[1] == 'all' else sys.argv[1].split(',')
    sub = sys.argv[2]
    module = sys.argv[3] if len(sys.argv) > 3 else None
    mods = sorted({m for jb in pa.jobs(races) for m in jb['modules']
                   if sub in m.split('/')[1] and (module is None or m.split('/')[4] == module) and (races is None or m.split('/')[3] in races)})
    print(time.strftime('%H:%M:%S'), f'{len(mods)} modules', flush=True)
    with mp.Pool(min(11, max(1, len(mods))), initializer=L._setup) as pool:
        for msg in pool.imap_unordered(L._run, [('module', str(pa.ROOT / m)) for m in mods]):
            print(time.strftime('%H:%M:%S'), msg[:400], flush=True)
    print(time.strftime('%H:%M:%S'), f'index: {L.write_index()} layers', flush=True)
