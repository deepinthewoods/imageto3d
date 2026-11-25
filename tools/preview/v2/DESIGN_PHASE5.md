# Preview app v2 - phase 5 contract (monsters as a kind)

Inputs: PIPELINE_V2_PLAN.md §8 and §13; DESIGN_ORCHESTRATOR.md "Monster lanes". Unless stated otherwise, paths are relative to the
monster root `F:/godot/blender/characters/monsters/aaa`. That root is in the **blender** repo, not in imageto3d. Its own rules are in
`AGENTS.md`, `CLAUDE.md`, `PROMPTING_GUIDE.md` and `tools/`.

## A. Concept intake
- **What counts as a concept:**
  - `concepts/<Folder>/codexN-front.png` with its `codexN-front.prompt.json` (and `.colours.json` when present). Shown.
  - `codexN-front_attemptK.png` files, numbers with JSON but no PNG, and loose legacy PNGs without JSON. Hidden.
  - There are 28 folders and about 1,739 final images.
- **State file:** `concepts/<Folder>/.v2_state.json`, which v2 writes atomically under a per-folder lock:
  `{"schema":1,"concepts":{"codexN":{"stage":"concepts|provisional|approved|archive|trash","time":"...","monster":"War Horse",
  "variant":"2"}}}`.
  - A concept the file doesn't list is in stage `concepts`.
  - Monster files never move; Reject and Archive are state changes only.
  - Trash is restorable from the Trash tab.
- **Migration** (`migrate.py --monsters --apply`, run once by Claude with a dry-run report first):
  - Every final concept that exists at cut-over goes to `archive`, unless it is already picked.
  - Already picked means either of:
    - `pipeline/<M>/variant_names.json` `variants[k].concept == "codexN-front.png"`;
    - a sha256-identical `pipeline/<M>/<k>-front.png`.
  - Picked concepts become `approved`, with their monster and variant recorded.
  - Goblin Shaman's codex4 is picked; it is known only through `was`, so handle it with an explicit override list in the migration.
- **Folder to monster:** match `concepts/<Folder>` against `pipeline/<M>` by lowercase with spaces removed (`Warhorse` ↔ `War Horse`).
  With no match, the monster name is the folder name, used for a new pipeline folder.
- **Cards:** id `mconcept:<Folder>/<codexN>`, type monster, kind monster.
  - The title is `<Monster> · codexN`.
  - The thumbnail is the PNG.
  - Details come from the prompt json (design, notes), plus the per-N intent from the folder's set files (`adjectives.json` etc.)
    when present.
  - Actions: concepts get provisional, approve and reject; provisional gets approve and unapprove; archive gets approve and
    unarchive; trash gets restore. These go in the action allowlist.

## B. Approve = pick
These steps run under the monster lock.
1. Assign the variant:
   - a brand-new monster (no `pipeline/<M>`) gets `main`;
   - a monster that already has `main` keeps it and gets the next number from 2 (main counts as 1; never rename `main`);
   - a numbered monster gets max numeric + 1, counting retired numbers in `pipeline/<M>/variant_names.json` and the folders, and
     never reusing one;
   - War Horse `<n>-<armour>` ids are out of scope (armour variants stay manual).
2. Copy `codexN-front.png` to `pipeline/<M>/views/<v>/front.png`, and the `.colours.json` and `.prompt.json` to `colours.json` and
   `prompts.json`. Do not write the flat `<k>-front.*` files; nothing reads them.
3. Add `{"was": "codexN", "concept": "codexN-front.png", "picked": "<time>", "by": "v2"}` to
   `pipeline/<M>/variant_names.json` `variants[<v>]`.
4. Write `pipeline/<M>/views/<v>/views.json`:
   `{"schema":1,"views":[{"name":"front","file":"front.png","enabled":true}],"unattended":<from the approve dialog>,
   "frozen":null}`.
5. Queue the three view tasks (section C).
6. If `<M>` is not in `tools/build_monster_blends.py` `PIPELINE_LIBRARY`, the card gets a badge "no library folder mapping (add to
   PIPELINE_LIBRARY)".

Undo of an approve: allowed only while no view task has started. It removes the copied views folder (to the Recycle Bin via the
trash helper or a `.v2_withdrawn` rename) and the variant_names entry.

## C. Extra views (Codex, no checks)
- Three outputs per variant: `left.png`, `back.png` and `right.png` in `views/<v>/`, each with a `<name>.prompts.json` record that
  follows the Hobgoblin `2-back.prompts.json` field convention.
- Generation goes through the imageto3d view runner with a new **task field `"launcher": "monsters"`** (`tools/view_batch.py`):
  - Launch `F:/godot/blender/characters/monsters/aaa/tools/codex_imagegen.sh` with cwd = the monster root.
  - The job dir is `<monster root>/.codex_jobs/<job>` (`.done`, `exit_code`, `log.txt`).
  - This changes the script path, cwd, `jobdir`, `job_alive` and the name-collision loop. Default behaviour is unchanged.
  - The runner picks this up only after a restart, which the Queue tab can do (phase 4).
- Task: `checks: []`, `max_attempts: 2` (one retry for a missing or invalid file), priority 3. The output must be 8-bit RGBA with
  transparency; the runner only checks that the file exists, and the conversion validates the format.
- Prompt templates live in `v2/monster_prompts.py`.
  - Back: the established wording ("Image 1 shows a papercraft creature from the front. Draw the SAME creature from BEHIND: a back
    three-quarter view from slightly above. The camera has walked round to the opposite side ... turned 180 degrees ... same
    height and distance. The creature has not moved ...").
  - Left and right: the same, with "a quarter of the way round to the creature's LEFT (RIGHT) side (about 90 degrees) ... we now
    see its LEFT (RIGHT) side". Use anatomical sides, stated explicitly.
  - All three share: the same design and the same marker colours (from colours.json roles), front-only ornaments not copied, the
    whole creature in frame, transparent background, the "ONE hue" paragraph from PROMPTING_GUIDE.
  - Image 1 = `views/<v>/front.png`.
- No framing, pose or colour checks, and no aesthetic retries (user, 2026-11-25).

## D. Choose views → convert
- When the view tasks are done or failed, the card says **Choose views**.
- The Approved modal for a monster variant shows the 4 images large with an **Enabled** tick each (front included). A failed view
  shows **Retry**.
- **Use enabled views** (verb `convert`, `args.enabled`) writes `views.json` `frozen = {time, files:[...], sha256:{...}}` and queues
  the conversion task. At least one image must be enabled.
- With `unattended: true`, the freeze happens automatically with every available view once the view tasks finish.
- Changing ticks after freezing makes a new draft. Converting again needs an explicit **Reconvert** (paid) button, which is only
  available in the 3D tab.
- Legacy variants (no `views.json`) are already converted and need no choice.

## E. Conversion lane (orchestrator lane `monster_convert`, limit 1)
- **Gating:** the same as conversions. PAUSE scope `conversions` or supervisor mode `pause`/`pause-conversions` means wait; in
  mode `views-run`/`auto`, it runs only when the user is away. A per-task **Run now** (Queue tab) overrides the idle gate but not
  a manual pause, unless the user also presses it while paused (it then asks for confirmation).
- **Browser lock:** `.codex_jobs/_batch/browser_hyper3d.lock` (imageto3d root) is a cross-process msvcrt/fcntl lock.
  - Acquire it in `tools/hyper3d_pipeline/rodin_backend.py` `SeleniumRodin.__enter__` and release it on exit, waiting up to 10 min;
    if still busy, `Stop('Hyper3D profile busy')`. The lane treats that as "requeue after 5 min", not as a failure.
  - The outfit `convert_job.py` starts fresh python processes, so it picks this up without a restart.
- **Steps** (each resumable from the record; never pay twice):
  1. If `models/<v>/hyper3d/studio_submission.json` exists, go to collect or inspect according to its `status`, `actions` and
     `pending_action`. An existing paid action or `credits_committed > 0` means never submit again.
  2. Submit (free):
     - one enabled view: `hyper3d_single.py submit` with `view_order = [that file]` (generalise `single_image_config`);
     - 2-4 views: a new `tools/hyper3d_multiview.py` (generalising `hyper3d_twoview.py`):
       - `view_order` = the frozen list, in that order;
       - `directions = {}`, `multi_view = True`;
       - `prepare_multiview` compares against `config['view_order']`;
       - `validate_views` without the equal-size rule, keeping the RGBA/PNG checks.
     - Run it with `%USERPROFILE%\.cache\hyper3d-venv\Scripts\python.exe -X utf8` from the monster root.
  3. `collect --allow-paid --accept-preview` (PAID, 0.5 credits). Append to the imageto3d `.codex_jobs/_app/paid_log.jsonl`:
     `{time, kind:"hyper3d", monster, variant, views, credits: 0.5, record}`.
  4. `python tools/reduce_monster.py "<M>/<v>"` (Blender, about 1.5 min). It writes `models/<v>/reduced/` with 5 LODs.
- **Failure:** show the record status, the wrapper's Stop message and screenshot paths. Retry resumes from the record.

## F. 3D → Completed
- The 3D modal (monster3d.js) gets **Approve**: it writes `chosen_lod.json` `{lod, approved: true}` through the locked v2 monster
  writer.
- **Unapprove** in Completed clears `approved`.
- The approved LOD stays locked; this already exists.
- **Reconvert** in the 3D modal (paid; confirmation states the cost) starts a new conversion:
  - it moves `models/<v>/hyper3d` and `reduced` to `*_prev_<time>`;
  - it requires the frozen view list (the current ticks).
- Completed monster cards show the library hint (B.6).

## G. Pilot (Claude, after implementation; at most 4 Hyper3D conversions in total, user 2026-11-25)
1. Never pick concepts on the user's behalf. The pilot uses variants the user ALREADY picked but that were never converted:
   - Gibbering Mouther, Hobgoblin, Lizardfolk, Merfolk and Sahuagin have flat `pipeline/<M>/<k>-front.*` picks and
     `variant_names.json`, but no views/ or models/.
   - Migration maps those picks to `approved`. The app's "import pick" path creates `views/<k>/` from the flat files with the
     same rules as B.2–B.4, without renumbering. That queues the extra views.
   - Use 2 such variants: one with `unattended` off, one on.
2. Let the views generate (Codex), choose views on the first: enable front + 2 others.
3. Run the conversions with Run now. That is 2 paid conversions, logged in paid_log.jsonl.
4. Verify that the mixed-size multi-view upload works. If it doesn't, the fallback is resizing on our side? **NO**: never resize.
   Report and fall back to the largest same-size subset or a single view, and ask the user.
5. Approve one LOD in 3D, check Completed, then unapprove.
6. Report the credits used.
