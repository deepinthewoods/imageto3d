#!/bin/bash
# Re-run reduce_model.py for every module of a converted job, keeping the previous outputs.
#
# Usage: tools/tripo_pipeline/rereduce_job.sh <models job dir> <tag>
#   e.g. tools/tripo_pipeline/rereduce_job.sh models/plain_plate_mail_01/cb0e04f84bfb offbody
#
# For each <race>/<module>/source.glb under the job dir (module = body_armor, right_gauntlet_boot or
# outfit; other folders such as *_attempt01_* are skipped) it moves reduced/ to reduced_<tag>/ and
# highpoly.blend to highpoly_<tag>.blend, then runs reduce_model.py on the source with its standard
# inputs (module, swatches, regions, parts, cameras are resolved from the folder layout and the job's
# frozen base snapshot). Log: <module dir>/reduce_<tag>.log. Nothing is deleted or overwritten; a module
# whose reduced_<tag>/ or highpoly_<tag>.blend already exists is skipped as a whole. Set JOBS (default 2)
# for parallel modules and THREADS (default 4) for Blender's threads per module.
set -u
if [ $# -ne 2 ]; then sed -n '2,15p' "$0"; exit 2; fi
JOB=$(realpath "$1"); TAG=$2
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BLENDER=${BLENDER:-/usr/local/bin/blender}
[ -d "$JOB" ] || { echo "no such job dir: $JOB" >&2; exit 2; }
case "$TAG" in ''|*/*|*[!A-Za-z0-9_.-]*) echo "bad tag: $TAG" >&2; exit 2;; esac

one() {
  dir=$1; tag=$2
  if [ -e "$dir/reduced_$tag" ] || [ -e "$dir/highpoly_$tag.blend" ]; then
    echo "SKIP $dir: reduced_$tag or highpoly_$tag.blend already exists"; return 0
  fi
  [ -d "$dir/reduced" ] && mv "$dir/reduced" "$dir/reduced_$tag"
  [ -f "$dir/highpoly.blend" ] && mv "$dir/highpoly.blend" "$dir/highpoly_$tag.blend"
  [ -f "$dir/highpoly.blend1" ] && mv "$dir/highpoly.blend1" "$dir/highpoly_$tag.blend1"
  echo "RUN  $dir"
  "$BLENDER" -b -t "${THREADS:-4}" --python "$ROOT/tools/tripo_pipeline/reduce_model.py" -- \
    "$dir/source.glb" "$dir/reduced" > "$dir/reduce_$tag.log" 2>&1
  status=$?
  grep -h "TRIPO_LOD\|PARTS_FIT" "$dir/reduce_$tag.log" | cut -c1-200
  echo "DONE $dir exit=$status"
}
export -f one
export ROOT BLENDER

find "$JOB" -mindepth 3 -maxdepth 3 -name source.glb | sort | while read -r src; do
  dir=$(dirname "$src")
  case "$(basename "$dir")" in body_armor|right_gauntlet_boot|outfit) echo "$dir";; esac
done | xargs -P "${JOBS:-2}" -I{} bash -c 'one "$@"' _ {} "$TAG"
