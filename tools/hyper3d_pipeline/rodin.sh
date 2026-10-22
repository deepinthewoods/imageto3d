#!/usr/bin/env bash
# Hyper3D Rodin backend runner (submit | collect | inspect | bind); see rodin_backend.py / README.md.
set -euo pipefail
workflow_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
workflow_venv="${HYPER3D_VENV:-/home/niz/.cache/codex-firefox-playwright/venv}"
if [[ -z "${DISPLAY:-}" && -S /tmp/.X11-unix/X0 ]]; then
    export DISPLAY=:0
fi
exec "$workflow_venv/bin/python" "$workflow_dir/rodin_backend.py" "$@"
