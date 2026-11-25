# Preview app v2 - technical design (contract for the implementers)

The user-facing plan is `PIPELINE_V2_PLAN.md` (repo root); read it first. This file fixes the code layout, data shapes and HTTP
contract so the backend, the frontend and the layer builder can be built in parallel. Change it only together with the code.

## 0. Ground rules (all phases)
- The live old site (`tools/preview/serve.py` on :8765, started by the user) and the pipeline services (supervisor, view runner,
  convert worker) and the prototype server (:8790) keep running until cut-over. **Never kill or restart them**; never
  `taskkill /IM python.exe`. Kill only processes you started yourself, by PID.
- **Do not change the behaviour of `tools/preview/serve.py`, `app.js`, `index.html`, `style.css`, `monsters.py`, `monsters.js`**
  (the old site reads app.js per request, so editing it changes the live site at once). v2 imports serve.py / monsters.py as
  libraries. Adding a new function to them is allowed; changing an existing one is not (until phase 6).
- v2 runs on **port 8766** during development: `python tools/preview/v2/server.py --port 8766`.
- No folder moves of real data, no `workflow.py` state changes, no Tripo / Hyper3D / Codex calls in phases 1-3 except through tests
  with temp roots. Tests use temp folders (`tempfile`), never the real root.
- Python: the system `python` (3.12+), stdlib only for the server (http.server, sqlite3, threading, subprocess). Layer builds run
  in a subprocess with their own imports (numpy, scipy, trimesh, moderngl as the prototype uses).
- Writes are atomic (`tmp` + `os.replace`, retry on Windows `PermissionError` a few times).

## 1. Code layout
```
tools/preview/v2/
  server.py      entry: HTTP (ThreadingHTTPServer, 127.0.0.1), static web/, /files/ + /mfiles/ (same rules as serve.py), /api/v2/*
  catalog.py     scans -> cards per tab + item details (reuses serve._listing/_memo/concepts/jobs/... and monsters.cards)
  actions.py     concept-stage verbs (wraps serve.action + new 'provisional' and direct 'approve'), unit decisions
  decisions.py   models/<set>/pipeline_v2.json read/write (rev, cross-process lock), chosen_lod.json writes (transactional), ready.json
  guard.py       (phase 1 review fixes, section 6) conflict errors, OS locks, single-writer server lock, intents, multi-file transaction
  queue.py       (phase 3) SQLite queue  .codex_jobs/_app/queue.sqlite3   -- NAME CLASH: server.py puts this folder first on sys.path, so a
                 file called queue.py would shadow the stdlib `queue` (concurrent.futures needs it). Call it taskqueue.py.
  orchestrator.py (phase 3/4) reconciler loop + lanes;  lanes/*.py
  migrate.py     dry-run report (no writes except the report)
  test_v2.py     unittest, temp roots
tools/preview/web/
  index.html (import map -> vendor/three), main.js, api.js, state.js, tabs.js, cards.js, modal.js, concept.js, approved.js,
  model3d.js, aligned.js (phase 3), mix.js (phase 3), queue.js (phase 4), viewer.js, style.css, vendor/three/ (r160, local copy)
tools/layers/      the production layer builder (phase 2): build.py (API + CLI), layers.py, geodiv.py, shell_slots.py, ... , checks/
```

## 2. Units, stages and derivation
- **Races** = `base_views/<race>/` folders with `palette.json` (`serve._base_races`), currently 11. A set does not need every one of them:
  its **required races** are the races that appear in any of its job records (claimed) UNION the races whose supervisor fronts task
  `.codex_jobs/_batch/tasks/fronts--<set>--<race>.json` exists with a status (`.codex_jobs/_batch/status/<same name>.json` `state`, absent =
  pending) other than `failed` (still planned). A race never claimed and without a live fronts task is NOT required; the item lists it in
  `not_converted: [{race, reason: "never claimed" | "fronts failed"}]` (info only). The 3D approval freezes the list into
  `lod_approval.required_races`; when present that list wins, so a base added later cannot send a reviewed set back to Approved.
- **Character set** `S`: concept folders `concepts|provisional|approved|archive/concepts|trash/concepts/<S>/<race>/<module>_front.png`;
  after claim, jobs `<stage>/<S>--<job>/` (stage in processing_views, multiview_ready, processing, completed, failed,
  held_conversions) with `.tripo-job.json` (`set`, `job_id`, `modules: [{race_gender, module}]`, `state`, `conversion`,
  `module_conversion`); a job folder named `<S>_<race>--<id>` belongs to set `S` (`serve._job_set`), and so does `<S>_gnome--<id>` (the
  documented gnome family suffix, `finish_sets.set_of`). Models: `models/<record set>/<job>/<race>/<module>/reduced/lod_error_<lv>.glb`
  (+ `_colors.npz`, `manifest.json` `levels.<lv>.triangles`) where `<record set>` is the `set` of the job record = the folder-name prefix
  (`S_dwarf_male`, `S_gnome`, ...): the PHYSICAL model dir. Every unit carries `model_set` (that dir name); reads, URLs and LOD writes use it,
  while the decisions (`pipeline_v2.json`) stay under the card's set `S`.
  A job folder with `.pixal3d-job.json` and no `.tripo-job.json` is a retired Pixal3D job: inventoried by migrate.py separately, never a card.
- **Kind**: `armour` when any module is `body_armor`/`right_gauntlet_boot`, `outfit` for `outfit`, `legacy` for flat batch entries,
  `monster` for monsters.
- **Required modules** of a set: the module names that appear in its jobs' `modules` (or its concept fronts before claim).
  **Required units** = every required race x required module; units of races listed in `pipeline_v2.json.dropped` are `dropped`.
- **Unit status** (per race/module), first match wins:
  `dropped` (decision) | `pin_missing` (`lod_approval` names a job whose reduced artifacts are missing or incomplete: `job` = the pin, `error`
  says so, `lods: {}`; **never a substitute mesh**; only "send back to 3D" clears the pin) |
  `reduced` (a module dir with `reduced/manifest.json` and all three `lod_error_<lv>.glb`; when several
  jobs have one, the pinned job from `lod_approval`, else the newest by manifest mtime = `serve.newest_jobs` rule) |
  `converting` (job in processing/ not converted for this unit) | `ready` (job in multiview_ready/) | `views` (job in
  processing_views/) | `held` (job in held_conversions/) | `failed` (job in failed/ or `module_conversion` error) | `missing`.
  Implementation notes (phase 1): every job that carries the unit gives it one of these statuses and the best wins in the order
  converting > ready > views > held > failed > missing; a unit with a recorded error (`conversion_errors` entry `race/module: text`
  or `module_conversion[key].error`) is `failed` instead of `converting` (it would otherwise read "converting" forever), and a
  carrying job in completed/ without a reduced module is `missing` (`failed` with an error). The job set id is `serve._job_set`
  (a `_<race>` suffix belongs to the base set). `models/<set>` folders with no job folder in any stage are ignored (listed in the
  migration report); the models job folder is `models/<job record set>/<job_id>`. A set's modules are the module names of its jobs.
- **Set stage** (post-claim; before claim the folder decides: concepts/provisional/approved/archive/trash):
  1. any required unit that is not `reduced` / `dropped` / `pin_missing` -> **approved** (in progress)
  2. no `lod_approval` -> **3d**
  3. `lod_approval` but any approved unit's layer build not published (phase 3) -> **3d** with badge "building n/m" (failed
     builds: badge error + Retry).  *Phase 1 skips this rule (there are no builds yet): `lod_approval` alone gives aligned.* A `pin_missing` unit keeps the set in aligned /
  completed with an error badge: it cannot advance (phase 3 approve verbs refuse it) until it is sent back to 3D.
  4. no `aligned_approval`, or it no longer holds -> **aligned**
  5. else -> **completed**. The Aligned approval holds (phase 3 review F3, DESIGN_PHASE3.md F.3) only when its units are exactly the live units (frozen
     required races) and the 3D-approved units, every one with its approved build id still published, every approved bare build still published,
     and `models/<S>/ready.json` carries the approval's `approval_id`. 3D-approved units show the FROZEN LOD of `lod_approval`; a unit whose pinned
     source GLB changed is `source_changed` (not built; Back to 3D).
- **Default LOD** shown (not saved, `lod_saved: false`) when a module has no `chosen_lod.json`: `0p2` (124 of the 132 existing picks).
- **Monster variant** (`monsters.cards`): `reduced` LODs and not approved -> **3d**; approved (`chosen_lod.json approved`) ->
  **completed**; otherwise -> **approved** (in progress). Monster concepts: phase 5.

## 3. Decision files
`models/<S>/pipeline_v2.json` (written only by v2, atomic, under a per-set lock; absent = all defaults):
```json
{"schema": 1, "set": "S", "rev": 7,
 "dropped": {"gnome_female": {"time": "...", "reason": "..."}},
 "lod_approval": {"time": "...", "units": {"human_male/body_armor": {"job": "a28c0e5ad46d", "lod": "0p2", "glb_sha256": "..."}},
                  "required_races": ["human_male", "..."]},
 "amount": {"human_male": {"body": 100, "limbs": 100}, "elf_female": {"outfit": 80}},
 "aligned_approval": {"time": "...", "approval_id": "<uuid hex>", "units": {"human_male/body_armor": {"job": "...", "lod": "0p2", "build_id": "..."}},
                      "bare": {"human_male": "bare-..."}, "amount": {"human_male": {"body": 100, "limbs": 100}}},
 "history": [{"time": "...", "action": "approve_3d", "detail": {}}]}
```
- `amount` groups: outfit sets `{"outfit": n}`; armour sets `{"body": n, "limbs": n}` (body = torso layer, limbs = gloves and
  boots layers). Integers 0..100, default 100 (the prototype mixer default).
- Every mutation bumps `rev` **including LOD picks** (their `chosen_lod.json` files and the bumped `pipeline_v2.json` are one transaction, section 6);
  mutating requests carry the `rev` the client saw and get **409** + the current item on mismatch. `lod_approval.required_races` (list) is
  written by the phase 3 approve and freezes the required races of the set.
- The file is only replaced by defaults when it is ABSENT. If it exists but is not valid UTF-8 / JSON, is not an object, has `schema` other than 1,
  or has a wrongly typed `rev` / `dropped` / `amount` / `history` / `lod_approval` / `aligned_approval` (nested records included: approval units,
  amount values, required_races, bare map, approval_id), the item shows `error` (and the card a
  `decisions unreadable` badge) and every mutation of that set (set verbs, LOD writes) answers **409 `code: unreadable`** until it is fixed.
- `chosen_lod.json` per module dir keeps its existing format (`{"lod","reduced","wearable","updated"}`, see `serve.write_lod`);
  v2 writes it in the module dir of the job the client displayed (= the pinned one, else the newest conversion; a mismatch is a 409).
  Characters: `0p4|0p2|0p1`; monsters `0p8|0p6|0p4|0p2|0p1` via `decisions.write_monster_lod` (the `monsters.write_choice` record, written
  under a per-variant lock with a unique temp file).
- `models/<S>/ready.json` (phase 3, on Aligned approval): `{"schema":1,"set","kind","approved_at","approval_id","races":{race:{"amount":{..},
  "units":{module:{"job","lod","build_id","slots":{slot:{"glb": rel path,"sha256"}}}}}},"bare":{race: rel dir}}` (every path relative to the project root, posix; see DESIGN_PHASE3.md D.5).
- Legacy files are left alone: `model_approval.json`, `fit_choice.json`, marks, fitlab outputs.

## 4. HTTP API (v2)
All JSON. Mutations are POST with header `X-Token: <token>`; the token is generated at server start and injected into
`index.html` as `<meta name="v2-token" content="...">`. POSTs without it, or with an `Origin` header whose host differs from the
request `Host`, get 403. Bodies are capped (4 KB unless stated).

### GET /api/v2/state
```json
{"races": ["dwarf_female", "..."],
 "tabs": {"concepts": [Card], "provisional": [Card], "approved": [Card], "3d": [Card], "aligned": [Card], "completed": [Card],
          "review": [Card], "archive": [Card], "trash": [Card]},
 "queue": null, "credits": null, "paused": null,
 "server": {"read_only": false, "message": null, "notes": ["rolled back lod s"]}}
```
`server.read_only` = this server does not hold the single-writer lock (section 6); the UI shows a banner. `notes` = what start-up recovery did.
`thumbnail_kind`: `render` = the thumbnail is the model's own fresh `.thumb.png`; `fallback` = concept / view art shown because no render exists
while `model` is set (the client then renders the visible model once, one at a time, POSTs it and switches); `null` = no model-based thumbnail.
**Card** (one shape for every tab):
```json
{"id": "concepts:S | set:S | legacy:B:K | monster:<Monster>/<variant> | ...", "type": "character|monster",
 "kind": "outfit|armour|monster|legacy", "name": "S", "title": "pretty name", "stage": "concepts",
 "thumbnail": "/files/...png or null", "thumbnail_kind": "render|fallback|null", "model": "/files/...glb or null (3d/aligned/completed preview)",
 "races": ["human_male", "..."],
 "badges": [{"text": "views 3/11", "tone": "info|warn|error|ok"}],
 "actions": ["provisional", "approve", "reject"],
 "progress": {"<race>": {"<module>": "reduced|converting|ready|views|held|failed|missing|dropped"}}  // approved tab only
}
```
`actions` (hover buttons) per stage: concepts `provisional, approve, reject` (legacy flat entries: `approve, reject`);
provisional `approve, unapprove`; approved (unclaimed folder) `unapprove`; archive `approve, unarchive`; trash `restore`;
3d / aligned / completed: none on hover in phase 1 (phase 3 adds them). The Trash tab also lists **Recovery** cards (`kind: "recovery"`, id
`intent:<id>`, actions `complete`, `rollback`; section 6) for interrupted multi-step changes.

### GET /api/v2/item?id=<id>
Concept-stage ids: the card plus `detail.races` exactly as `serve.set_card` races (`{name, images:{module:url}, info:{module:
summary}, base}`) and legacy `info.record`.
`set:S`:
```json
{"id": "set:S", "name": "S", "kind": "armour", "stage": "3d", "rev": 7, "races": [...], "modules": ["body_armor", "right_gauntlet_boot"],
 "units": [{"race": "human_male", "module": "body_armor", "status": "reduced", "job": "a28c0e5ad46d", "jobs": ["..."],
            "folder": "completed/S--a28c0e5ad46d", "model_set": "S",
            "warnings": ["doubled interior shell (25% ...)"], "review_levels": ["0p4"],
            "views": {"front": "/files/...", "front_three_quarter": "...", "back": "...", "back_three_quarter": "..."},
            "lods": {"0p4": {"url": "/files/.../lod_error_0p4.glb", "faces": 1104}, "0p2": {...}, "0p1": {...}},
            "lod": "0p2", "lod_saved": true, "error": null}],
 "locked": false, "error": null, "required_races": [...], "not_converted": [{"race": "orc_male", "reason": "never claimed"}],
 "decisions": {pipeline_v2.json content or defaults},
 "builds": {},
 "card": Card}
```
`units` lists every race x module of the required races (dropped ones with `status: "dropped"`, `lods: {}`); `job` is the chosen job
(pinned, else newest conversion) for a reduced unit, the pin for a `pin_missing` unit, else the job that gives the status; `jobs` = every job id that carries the unit
(more than one converted job = conflict, reported by migrate.py); `backend` = `module_conversion` backend; `warnings` = short strings from the chosen
job's record (`conversion_errors` entry of the unit such as the doubled interior shell, `needs_manual_review`, `free_retries_exhausted`,
`input_warning`, ...; the card shows `warnings <n>` for units that have any); `review_levels` = `module_conversion.levels_needing_review`
(very common, so not counted on the card; the 3D modal flags it when the shown level is in it); `locked` = `lod_approval` exists (LOD picks frozen);
`error` = the unreadable-decisions reason (section 3); `lods.<lv>.faces` =
`manifest.levels.<lv>.triangles` of the full labelled model. `decisions` always has all keys (`dropped`, `lod_approval`, `amount`,
`aligned_approval`, `history`, `rev`).
`monster:...`: the `monsters.cards` card plus `stage`.

### POST /api/v2/action  `{"id": "...", "verb": "...", "rev": n?, "args": {}}`
**Allowlist.** Only these (id type, verb) pairs exist; anything else is a 400 (there is no fallthrough to `serve.action`, so the old site's
`model:` / `approvedmodel:` branches are unreachable): concepts: provisional, approve, reject, archive; archive: provisional, approve, unarchive;
provisional: approve, unapprove; approved: unapprove; legacy: approve, reject, archive; archlegacy: approve, unarchive; trash / trashlegacy:
restore; set: drop_race, undrop_race, unapprove, approve, rebuild; intent: complete, rollback. v2 performs every move itself (section 6).
Concept verbs: `provisional` (concepts|archive -> provisional = old first hop), `approve` (concepts|archive -> approved directly,
provisional -> approved, legacy flat -> `workflow.py promote --approve` as today), `reject`, `unapprove`, `archive`, `unarchive`,
`restore`. Direct approve = rename into `approved/` (refuse when `provisional/<S>` or `approved/<S>` exists) then
`workflow.py --root <root> check approved/<S>` (failure = warning, the move stands), logged to `tools/preview/actions.jsonl`
like `serve.log_action`. Response `{"ok": true, "undo": {"verb": "...", "id": "...", "args": {...}?} | null, "warning": "..."?}` (the client posts the undo
descriptor back as `{id, verb, args, rev?}`). `unapprove` accepts `args.to` (`approved`: provisional|concepts|archive, `provisional`:
concepts|archive; default = one step back) so the undo of a direct approve / of an archive->provisional returns the set to where it came from.
Undo descriptors: provisional -> `unapprove provisional:S {to: origin}`; direct approve -> `unapprove approved:S {to: origin}`; provisional ->
approved -> `unapprove approved:S`; unapprove approved (default, to provisional) -> `approve provisional:S`; **unapprove approved with
`to: concepts|archive` -> `approve concepts:S` / `approve archive:S` (direct approve: back to approved, not provisional)**; unapprove
provisional -> `provisional concepts:S` (or `archive:S`); reject / legacy approve -> `restore <trash id>`; archive <-> unarchive; restore ->
null. Moves return `src`/`dst` (always POSIX paths, also `promoted.path`).
**Legacy approve undo**: the origin sidecar of the trashed flat files records `approved_set` (the exact promoted set). `restore
trashlegacy:...` first withdraws `approved/<approved_set>` (moved to `.codex_jobs/_app/withdrawn/`, never deleted) under the claim lock with the
same checks as unapprove, then moves the flat files back; if a claim owns the set (or it moved on to provisional / concepts / archive) it fails with
a clear message and changes nothing. The response has `withdrawn: "withdrawn"|"absent"|null`.
Set verbs (`drop_race` / `undrop_race` with `args.race`, optional `args.reason`; `unapprove` with optional `args.to` = `3d` | `aligned`): write
`pipeline_v2.json`; `rev` is REQUIRED (400 without it), a stale one gives **409** `{"ok": false, "error", "code": "rev", "rev": current, "item": <current item>}`.
The set must exist (404) BEFORE any file or lock is created. Response `{"ok": true, "undo": {"verb": "undrop_race"|"drop_race", "id": "set:S", "args":
{"race", "meta"?: {time, reason}}, "rev": new}, "rev": new, "stage": new set stage}`: **the undo carries the rev the action returned** (and, for the
undo of an undrop, the prior drop metadata in `args.meta`), the client never re-fetches a rev for an undo; if anything was decided since, the
undo is a 409. A race may only be dropped when it is a required race of the set. Refusals (400): unknown / not required race, already dropped /
not dropped, dropping the last live race. `unapprove` (aligned -> 3d clears `lod_approval` and with it the pin; completed -> aligned clears
`aligned_approval`; `to: 3d` from completed clears both; this is also the way out of `pin_missing`) has no undo in phase 1; phase 3 adds the
cancel of queued tasks. Other verbs on `set:`, `monster:`, `review:` ids -> 400.
`intent:<id>` `complete` / `rollback` resolve a Recovery card.

### POST /api/v2/lod
- single `{"id": "set:S", "race", "module", "lod": "0p2", "job": "<displayed job>", "rev": n}`
- all `{"id", "lod", "all": true, "jobs": {"race/module": job}, "rev": n}`
- restore (Undo of All) `{"id", "units": {"race/module": lod|null}, "jobs": {"race/module": job}, "rev": n}`
- monsters `{"id": "monster:...", "lod": "0p4"}` (no rev).

Every set write carries `rev` and the `job` the client displayed per unit (400 when missing). The server answers **409** when: `rev` is stale
(`code: rev`); the unit's resolved job (pinned, else newest) is not the displayed one (`code: job`); the set has `lod_approval` (`code: locked`,
"approved; send it back to 3D first"; an approved monster too); `pipeline_v2.json` is unreadable (`code: unreadable`). `all` writes every reduced
unit (the `jobs` map must cover them all) and nothing when one lacks the level; `units` writes exactly those (null removes the pick, so an
Undo never turns an absent pick into a saved default). All writes are **transactional** (section 6): the new `chosen_lod.json` files and the bumped
`pipeline_v2.json` are staged in unique temp files, the old contents are backed up (memory + an intent file), then replaced one by one; any error
restores the replaced ones and answers 400; after a crash the next server start completes the rollback. Every successful write bumps the set's
`rev`. Response `{"ok": true, "written": n, "previous": {"race/module": lod|null}, "rev": new}`. The client's Undo of All sends `previous` once
with the rev "All" returned and stops on a conflict.

### POST /api/v2/thumbnail?url=<glb url>  (PNG body <= 8 MB) -> `serve.write_thumb`.
A card's `thumbnail` for 3d/aligned/completed is the first reduced unit's fresh `lod_error_<lod>.thumb.png?v=<mtime>` when present, else the
job's front view; `model` is that unit's chosen-LOD GLB.

### Server behaviour (implemented in phase 1)
`/` = `web/index.html` with `<meta name="v2-token">` injected (a tiny placeholder page when it does not exist); other paths = static files below
`web/` (js mjs css html json png svg ico map txt woff woff2 wasm jpg webp; `.js` = `text/javascript`; unknown suffix, traversal, missing = 404);
`/files/` and `/mfiles/` like serve.py but with the checks of section 6 (**403** for a bad suffix or any forbidden path, **404** for a permitted path
that does not exist; `/files/` sends `Cache-Control: private, max-age=3600`). Every POST needs `X-Token` and, when an `Origin` header is sent, an
origin that is http(s) on one of the allowed hosts (403 otherwise, before the body is read), and is refused with 503 `code: read_only` by a
read-only server. JSON bodies <= 4 KB, thumbnail <= 8 MB; JSON over 4 KB is gzipped when the client accepts it.
`GET /api/v2/state` is cached for 1 s (`catalog.STATE_TTL`); every mutation invalidates it.

### Phase 3+ (contract fixed now, implemented later)
- `POST /api/v2/action` verbs on `set:S`: `approve` in 3d (snapshot -> `lod_approval`, enqueue builds), `unapprove` in aligned (->
  3d, clears `lod_approval`; builds kept), `approve` in aligned (-> `aligned_approval` + ready.json), `unapprove` in completed
  (clears `aligned_approval`), `rebuild` (`args.race?`, `args.module?`).
- `POST /api/v2/amount {"id", "race", "group": "outfit|body|limbs", "value": 0..100, "rev"}`.
- `GET /api/v2/layers?race=R` -> `{"bare": {slot: url}, "layers": [{"set", "module", "slot", "url", "stage", "amount", "build_id"}]}`
  (published builds of every set in aligned or completed, for the donor dropdowns and Mix).
- Monster verbs (phase 5): `views` (`args.enabled: {name: bool}`), `convert` ("Use enabled views").
- `GET /api/v2/queue`, `POST /api/v2/queue {"op": "retry|hold|resume|pause|run_now", ...}` (phase 4).

## 5. Frontend rules
- No build step: ES modules, three.js r160 vendored under `web/vendor/three/` (`build/three.module.js`,
  `examples/jsm/loaders/GLTFLoader.js`, `examples/jsm/controls/OrbitControls.js`, `examples/jsm/utils/BufferGeometryUtils.js`).
- Header: tab bar in two groups [Concepts, Provisional, Approved, 3D, Aligned, Completed] [Mix, Queue, Review, Archive, Trash] with
  counts after filtering; filters (remembered in localStorage): Characters [x], Monsters [x], race select (characters only),
  kind select (all / outfit / armour / monster), search box, grid size.
- Cards: thumbnail (concept front; 3D-stage GLB thumbnail rendered client-side once and POSTed as `.thumb.png` like the old app; a
  `thumbnail_kind: "fallback"` card shows its fallback picture until that render exists),
  title, kind tag (Monster tag visible), badges, hover buttons from `card.actions`. Optimistic removal + undo toast (8 s).
- Concept modal (port of the old `showConcept`): every race's fronts side by side over the race base (`B` toggles base
  under/over/off), details panel, buttons Provisional / Approve / Reject / Archive, prev/next; keys Enter = approve,
  P = provisional, Esc = reject, Space = next, arrows = prev/next. Keyboard rules: opening a modal consumes the opening key event and moves
  focus into the dialog; shortcuts never fire when the event target is a button / input / select / textarea / link / summary or when Ctrl / Alt /
  Meta is held; Enter on a grid card only OPENS the modal; Enter = approve only fires with focus on the dialog itself (never on a button). A click
  on a dialog button hands focus back to the dialog so the shortcuts keep working.
- Approved modal: per race/module rows of the views at a readable size (click = full size), unit status, error text; Drop race /
  Undrop race buttons per race.
- 3D modal (phase 1 = viewer + LOD save; phase 3 adds Approve): in Aligned / Completed (`item.locked`) and for approved monsters the LOD selector is
  VIEW ONLY (it switches the displayed mesh, posts nothing, shows "LOD view only: approved") and a `Back to 3D` button (set verb `unapprove`) is
  offered. The viewer notes list the unit's `warnings`, the levels needing review, dropped races (with Undrop) and not-converted races. Viewer of the unit's LOD GLB (full labelled model), bottom bar:
  race select, module select (armour), LOD select with face counts, **All** (this LOD everywhere, undo toast), < > (sets in the
  tab, filters applied), wireframe toggle, a race x module grid of the chosen LODs. Race / module / camera carry over between sets.
- Viewer basics (viewer.js): GLTFLoader, OrbitControls, flat shading, double-sided with back faces red (shader like
  `experiments/shell_slots_20261122/out/layers/mix.html` backRed), morph weights by name (`fill`), dispose on replace, stale load
  guard, double-click resets camera.

## 6. Safety rules (review of phase 1; every rule has a test in test_v2.py / test_workflow.py)
- **Claim lock** (`tools/tripo_pipeline/claim_lock.py`, stdlib): `workflow.py claim` holds an exclusive OS byte lock on
  `.codex_jobs/_batch/claim_locks/<set>.lock` for its whole run (waits up to 60 s for another claim), before the first `.base_views` write.
  Anything that moves `approved/<set>` out of approved/ (unapprove with or without `args.to`, the withdrawal inside the undo of a legacy approve)
  takes the same lock WITHOUT waiting (**409 `code: busy`**, "is being claimed"), then refuses (400) when `.tripo-job.json` or `.base_views`
  exists in the folder, then renames. The supervisor runs `workflow.py claim` as a subprocess, so the lock applies at once.
- **Moves**: a trash move writes `.preview-origin.json` into the folder BEFORE the rename and removes it again when the rename fails. A flat
  (multi-file) move writes an intent (`.codex_jobs/_app/intents/<id>.json`: files, sidecar, verb, promoted set) first, writes the sidecar, moves the
  files one by one and deletes the intent; on failure it rolls back (legacy approve also withdraws the promoted set) and keeps the intent
  only when the rollback itself failed. Incomplete intents are listed in `/api/v2/state` as Trash **Recovery** cards: `complete` (finish the
  moves) and `rollback`. Interrupted multi-file LOD writes (`kind: files_tx`) are rolled back automatically at server start; the log line is in `server.notes`.
- **Locks**: `models/<S>/.pipeline_v2.lock` = per-thread re-entrant lock + OS byte lock around every read-check-write of `pipeline_v2.json` and
  LOD writes, so two processes cannot both accept the same rev. Monster LOD writes use `models/<monster>/<variant>/.pipeline_v2.lock`, a unique temp
  file and the Windows replace retry. `.codex_jobs/_app/server.lock` is taken at start by the one v2 server that may write; a second server on the
  same root is read-only (POST -> 503 `code: read_only`, banner in the UI, `--read-only` forces it). Locks are released by the OS when the holder dies.
- **Hosts / origin**: allowed `Host` = `127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`, the bind address, plus the tailnet name(s) found
  at start with `tailscale status --json` (`Self.DNSName` without the trailing dot, `HostName`, `TailscaleIPs`) and `allowed_hosts` of
  `.codex_jobs/_app/settings.json`, each with and without the port; everything else gets **421** on every route (index, API, files). `Origin`, when
  sent, must be http(s) on one of those hosts (403).
- **Containment**: the tail of `/`, static, `/files/` and `/mfiles/` URLs is checked as text first (decoded once; backslashes = slashes; reject empty /
  `.` / `..` segments, a leading `/`, any of `<>:"|?*` or control characters, trailing dots / spaces), before any filesystem access; then the suffix
  allowlist, then resolved containment. Forbidden = 403 (static: 404), permitted but missing = 404. `index.html` goes through the same check.
- **Contract for phase 3**: `orchestrator.lock` of DESIGN_PHASE3 stays a separate lock; a server without `server.lock` must also not start the
  orchestrator. Phase 3 verbs reuse `guard.apply_transaction` for anything that writes several files (LOD picks, both approvals, unapprove with its
  ready.json revocation, the orchestrator's revocation).
- **Recovery (phase 3 review F8)**: a `files_tx` intent names its set and the sha256 of every file's old and new content. Recovery (server start and
  every orchestrator loop) runs under the set lock and restores the backups only while every file holds what the transaction wrote or its old content;
  a file changed since makes the intent a `conflict` (Recovery card: `complete` keeps the files, `rollback` restores the backups). Until the intent is
  resolved every write of the set is 409 `recovering`.
- **Promotions (phase 1 review #3, #4)**: a legacy approve marks `approved/<set>/.preview-promotion.json` with a `promotion_id` that the origin sidecar
  and the intent also record; an Undo withdraws only that promotion, and Recovery `complete` accepts a promotion that was already claimed.

### CUTOVER notes (phase 1 review #2 and #9: legacy writers)
The two remaining partial findings are coexistence issues with the OLD site (`tools/preview/serve.py` on :8765), not v2 defects: its Unapprove handler
checks only the job record (no claim lock) and its monster LOD writer uses a fixed temp file without the v2 per-variant lock. Both end at cut-over,
when the old site is stopped (CUTOVER.md step 1) and v2 is the only writer; no code change is made to serve.py / monsters.py (section 0). Until then,
do not use the old site's Unapprove or monster LOD picker on a set / variant that v2 is also changing. A write the old site makes to a set that has an
interrupted v2 transaction is detected by the recovery digest check (F8) and becomes a conflict instead of being overwritten.
