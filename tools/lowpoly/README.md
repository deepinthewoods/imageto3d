# Low-poly reduction library

Support modules imported by `../tripo_pipeline/reduce_model.py` (run it with
Blender, see that README). Moved here from the original `lowpoly algo` study.

- `lowpoly_lab.py` mesh arrays/stats/sampling helpers
- `tripo_reduce.py` GLB import, mesh audits
- `tripo_adaptive_study.py` local repair + error-bounded reduction (`reduce_error`)
- `adaptive_cleanup04.py`, `adaptive_lod04.py` cleanup and measurement
- `polygon_materials05.py` area-weighted texture sampling for face materials
- `lib/libmeshoptimizer.so` built by `./build_meshoptimizer.sh` (git-ignored)

The study entry points inside these files (their `main()`s and `results/...`
paths) are historical and not part of the pipeline.
