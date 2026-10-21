#!/usr/bin/env bash
# Run one Codex image-generation job in this project (Claude Code delegates all
# raster generation here; see ../CLAUDE.md).
#
# Usage: tools/codex_imagegen.sh <job-name> <prompt-file> [reference.png ...]
#
# Codex runs with this folder as its workdir, so it loads AGENTS.md and the
# built-in imagegen skill. Reference images are attached with -i so the
# built-in image_gen edit flow can see them. Output goes to .codex_jobs/<job>/:
# prompt.md, log.txt, result.md (Codex's final message), exit_code, .done.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
job=${1:?job name}
prompt=$(readlink -f "${2:?prompt file}")
shift 2

logdir="$ROOT/.codex_jobs/$job"
if [[ -e "$logdir" ]]; then
  echo "job dir exists: $logdir (pick a new job name)" >&2
  exit 2
fi
mkdir -p "$logdir"
cp "$prompt" "$logdir/prompt.md"

imgs=()
for img in "$@"; do
  imgs+=(-i "$(readlink -f "$img")")
done

# danger-full-access: the workspace-write/read-only sandboxes fail on this
# machine (bwrap --ro-bind-try). `--` stops -i from eating the prompt;
# </dev/null keeps a background run from waiting on stdin.
timeout "${CODEX_TIMEOUT:-3600}" codex exec --skip-git-repo-check \
  -s danger-full-access -C "$ROOT" -o "$logdir/result.md" "${imgs[@]}" \
  -- "$(cat "$logdir/prompt.md")" < /dev/null > "$logdir/log.txt" 2>&1
code=$?
echo "$code" > "$logdir/exit_code"
touch "$logdir/.done"
echo "codex job $job finished: exit $code ($logdir)"
exit "$code"
