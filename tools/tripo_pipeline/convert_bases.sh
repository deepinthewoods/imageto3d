#!/usr/bin/env bash
# Convert bare marker base views to 3D with Tripo Studio, then run the standard reduction.
#
# Usage: tools/tripo_pipeline/convert_bases.sh <out-root> <race> [race ...]
#   e.g. tools/tripo_pipeline/convert_bases.sh models/base_tripo/20261022 dwarf_female orc_male
#
# Per race: <out-root>/<race>/bare/ gets the Studio submission record and exports, source.glb and
# reduced/ (reduce_model.py with the race's active transforms.json + regions.json; no module, so
# the wear prior is neutral). Same settings as convert_job.py (H3.0, 2K, Triangle, no PBR).
# Resumable like convert_job.py: an existing submission record is collected, never resubmitted;
# a reduced/manifest.json means the race is done. Races run one after another (Studio allows one
# generation at a time per account).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TOOLS="$ROOT/tools/tripo_pipeline"
BLENDER=${BLENDER:-/usr/local/bin/blender}
out_root=$(realpath -m "${1:?out root}")
shift
cd "$ROOT"
for race in "$@"; do
  views="$ROOT/base_views/$race"
  out="$out_root/$race/bare"
  [[ -f "$views/front.png" ]] || { echo "no base views for $race" >&2; exit 2; }
  mkdir -p "$out"
  if [[ ! -f "$out/studio_submission.json" ]]; then
    echo "== $race: submit"
    node "$TOOLS/studio.mjs" submit "$views" "$out"
  fi
  if ! python3 -c "import json,sys; s=json.load(open(sys.argv[1])); sys.exit(0 if s.get('status')=='exported' and 'GLB' in s.get('exports',{}) else 1)" "$out/studio_submission.json"; then
    echo "== $race: collect"
    node "$TOOLS/studio.mjs" collect "$out"
  fi
  if [[ ! -f "$out/source.glb" ]]; then
    cp "$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['exports']['GLB']['path'])" "$out/studio_submission.json")" "$out/source.glb"
  fi
  if [[ ! -f "$out/reduced/manifest.json" ]]; then
    echo "== $race: reduce"
    "$BLENDER" -b -t 6 --python "$TOOLS/reduce_model.py" -- "$out/source.glb" "$out/reduced" \
      --cameras "$views/transforms.json" --regions "$views/regions.json" > "$out/reduce.log" 2>&1 \
      || { echo "reduce failed for $race; see $out/reduce.log" >&2; exit 1; }
  fi
  echo "== $race: done"
done
