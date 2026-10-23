# Claude Code in the image → 3D pipeline

@AGENTS.md

`AGENTS.md` (imported above) is the authoritative pipeline contract; it was written
for Codex. Follow it exactly as Codex would, with one substitution: **Claude cannot
generate images, so every raster-generation step is delegated to Codex.** Read
`PROMPTING_GUIDE.md` and `tools/tripo_pipeline/README.md` before starting a
concept or view task.

## Who does what

| Step | Owner |
| --- | --- |
| Front concepts of a batch, one `concepts/<set_id>/<race_gender>/` folder per concept with `<module>_front.png` + `<module>_prompts.json` (no rejected attempts kept) | Codex job |
| `workflow.py promote concepts/<batch_id> <NN> [--set ..] [--approve]` (only for the two existing flat legacy batches) | Claude |
| Remaining three views in `processing_views/<set>--<job-id>/...` | Codex job |
| Regenerating a rejected view | Codex job |
| `workflow.py status/check/claim/ready`, folder moves | Claude |
| Visual review of every generated image (dimensions, alpha, side, boundaries, colors) | Claude, then Codex for retries |
| `convert_job.py` (Tripo submit/collect + `reduce_model.py`, then for `piece_colours` body armour the segment-first rigid-piece split with `split_pieces.py --rigid auto` as automatic fallback), material review, `completed/` | Claude |

Wherever `AGENTS.md`/`PROMPTING_GUIDE.md` say "use the imagegen skill / built-in
image generation tool", that means: write a Codex job prompt and launch it. Never
use the imagegen CLI/API fallback (`scripts/image_gen.py`, `OPENAI_API_KEY`), never
generate images with other tools (Blender renders, PIL drawing, Hyper3D, etc.), and
never resize/crop/pad an image to make it pass.

## Launching a Codex image job

Write the prompt to the scratchpad, then run in the background (Bash
`run_in_background: true`; you are notified when it exits):

```bash
tools/codex_imagegen.sh <job-name> /abs/path/prompt.md base_views/human_male/front.png [other refs...]
```

The script runs `codex exec` with this folder as workdir (so Codex loads
`AGENTS.md` and its imagegen skill), attaches the reference images, and writes
`.codex_jobs/<job-name>/{prompt.md,log.txt,result.md,exit_code,.done}`. Job names
must be new. Set `CODEX_TIMEOUT` (seconds, default 3600) for long batches.

Each prompt must be self-contained and state:

- The task (front concepts of a batch, saved per concept as `<module>_front.png` + `<module>_prompts.json` in `concepts/<set_id>/<race_gender>/`, or remaining views for a claimed job) and the
  exact output paths. Codex must copy the chosen output from
  `~/.codex/generated_images/...` into those paths and must not overwrite existing
  images (concepts: do not keep rejected attempts, record only the attempt count in the prompts JSON; view retries use versioned names).
- The design brief, module(s), race_gender and the base view used as edit target
  (Image 1), plus the approved front as Image 2 for remaining views, and the
  established `back.png` as a reference for `back_three_quarter.png`. For three-quarter
  views, describe the camera: front-left from 45° above / back-right from 45° below, 45° around
  (see `PROMPTING_GUIDE.md`); without this imagegen drifts back to a level view.
- To open with the framing-lock sentence from `python3 tools/framing_lock.py <base view>` (before the design brief).
- To follow `AGENTS.md` and `PROMPTING_GUIDE.md`: native base dimensions, RGBA
  alpha, marker colors and swatches, anatomical side, bare anchors, module
  boundaries.
- To verify the saved PNG's real pixel size and alpha itself and regenerate on
  mismatch, then record exact prompts, references, dimensions/hashes and swatches
  in the concept json (or `prompts.json` and the design record for views).
- Scope limits: no `workflow.py` state changes, no folder moves between pipeline
  stages, no Tripo calls, no git commits, no edits outside the named output paths.
- To end with a short report: files written, dimensions, any rejected attempts and
  known issues. For concepts also the per-concept `check_framing.py` result.

Independent modules (e.g. `body_armor` and `right_gauntlet_boot`, or separate sets)
can run as parallel jobs. Keep the three remaining views of one module in one job,
since `back_three_quarter` depends on `back`.

## After a job

1. Check `exit_code` and `result.md`; if the log tail shows a usage/rate limit,
   tell the user rather than retrying in a loop. A killed job can be continued with
   `codex exec resume <session id from log.txt> "continue ..."`.
2. Run `python3 tools/check_framing.py <base.png> <images...>` first; any `FRAMING`/`POSE` flag means regenerate (see `PROMPTING_GUIDE.md`). Then inspect every image yourself (Read the PNGs; verify size/mode with
   `python3 -c "from PIL import Image; ..."` or `identify`) against the review list
   in `AGENTS.md`. Run `workflow.py check` where it applies.
3. For a failed view, launch a new Codex job naming the specific failure and
   repeating the invariants (as `PROMPTING_GUIDE.md` describes). Keep prior
   versions outside the ready set.
4. Only then run `workflow.py ready` and continue with the Tripo/reduction steps,
   which Claude runs directly.

The user approves or rejects concepts in the local preview site (`python3 tools/preview/serve.py --open`), which moves sets into `approved/` or restorable `trash/concepts/`. Never approve a set on the user's behalf; process `approved/` only when the user asks.
