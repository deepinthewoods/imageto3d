#!/usr/bin/env bash
set -euo pipefail
workflow_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
workflow_venv="${HYPER3D_VENV:-/home/niz/.cache/codex-firefox-playwright/venv}"
if [[ -z "${DISPLAY:-}" && -S /tmp/.X11-unix/X0 ]]; then
    export DISPLAY=:0
fi
exec "$workflow_venv/bin/python" "$workflow_dir/browser.py" "$@"
