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

# Git for Windows (2026-11-08): hand codex.exe Windows paths (F:/...) explicitly and stop MSYS
# from rewriting arguments, so the prompt text reaches Codex unchanged. No-op on Linux.
if command -v cygpath >/dev/null 2>&1; then
  hostpath() { cygpath -m "$1"; }
  export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
else
  hostpath() { printf '%s' "$1"; }
fi

imgs=()
for img in "$@"; do
  imgs+=(-i "$(hostpath "$(readlink -f "$img")")")
done

# danger-full-access: the workspace-write/read-only sandboxes fail on this
# machine (bwrap --ro-bind-try). `--` stops -i from eating the prompt;
# </dev/null keeps a background run from waiting on stdin.
# Model and effort are pinned here (user, 2026-11-13) so image jobs do not inherit
# the global ~/.codex/config.toml default (gpt-6-astra at xhigh burned the usage
# allowance). Override per run with CODEX_MODEL / CODEX_EFFORT.
timeout "${CODEX_TIMEOUT:-3600}" codex exec --skip-git-repo-check \
  -m "${CODEX_MODEL:-gpt-6.1-sol}" -c "model_reasoning_effort=\"${CODEX_EFFORT:-medium}\"" \
  -s danger-full-access -C "$(hostpath "$ROOT")" -o "$(hostpath "$logdir/result.md")" "${imgs[@]}" \
  -- "$(cat "$logdir/prompt.md")" < /dev/null > "$logdir/log.txt" 2>&1
code=$?
echo "$code" > "$logdir/exit_code"
touch "$logdir/.done"
echo "codex job $job finished: exit $code ($logdir)"
exit "$code"
