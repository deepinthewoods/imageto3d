#!/usr/bin/env python3
"""Queue of Codex image jobs (other-race fronts, remaining views) that survives the imagegen usage limit.

    python3 tools/view_batch.py add-views <processing_views job dir> [--races r1,r2] [--priority N]
    python3 tools/view_batch.py add-fronts <set_id> --design-job <job dir with the approved race> --races r1,r2 [--extra-file f] [--priority N]
    python3 tools/view_batch.py add-concepts <briefs.json> [--after-views set_a,set_b] [--priority N]
    python3 tools/view_batch.py run [--parallel 4]        # until the queue is empty; one event line per outcome
    python3 tools/view_batch.py status

Tasks live in .codex_jobs/_batch/tasks/<name>.json, their state in .codex_jobs/_batch/status/<name>.json, so tasks
can be added while `run` is going (it rescans every loop). Each attempt is a new tools/codex_imagegen.sh job
`<name>-aNN`. When a Codex log reports `usage_limit_reached`, the queue pauses until its `resets_at` (+2 min); on the
account limit ("You've hit your usage limit ... try again at <date>") it pauses until that date or one hour, whichever
is sooner, and probes again. The task is re-run later for its missing outputs only (no attempt counted). Outputs that already exist are never
regenerated or overwritten. A finished task runs tools/check_framing.py on each output; flags are recorded and
reported, never acted on: Claude reviews every image before `workflow.py ready` (see CLAUDE.md).

While .codex_jobs/_batch/PAUSE exists with "views" in its scope (tools/pipeline_supervisor.py: a manual pause; a screen-on
pause only stops conversions) no new job starts (PAUSED, once); running ones continue.

Events (stdout and .codex_jobs/_batch/events.log): DONE <task> ok|flags=..., RETRY, FAILED, LIMIT until <time>, IDLE, PAUSED.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / '.codex_jobs' / '_batch'
TASKS, STATUS, EVENTS = BATCH / 'tasks', BATCH / 'status', BATCH / 'events.log'
PAUSE = BATCH / 'PAUSE'   # written by tools/pipeline_supervisor.py while the screen is on: start nothing new
VIEWS = ('front_three_quarter', 'back', 'back_three_quarter')
# Since 2026-10-28 a claimed set generates ALL FOUR views: the concept front is never a view (it lives in
# .concept_notes/ as the design reference); front.png is generated fresh as an edit of the base front.
ALL_VIEWS = ('front', 'front_three_quarter', 'back', 'back_three_quarter')
ARMOR = ('body_armor', 'right_gauntlet_boot')

RACES = {
    'dwarf_female': 'Stocky dwarf female in a T-pose: short thick legs, broad torso, large white head (the white region '
                    'includes hair/face; keep all of it white and bare).',
    'dwarf_male': 'Stocky dwarf male in a T-pose: short thick legs, broad barrel torso, large white head (includes '
                  'beard; keep all of it white and bare).',
    'elf_female': 'Slender elf female in a T-pose with long thin limbs; the white head includes the pointed ears (keep '
                  'them white and bare).',
    'elf_male': 'Slender elf male in a T-pose with long thin limbs; the white head includes the pointed ears (keep them '
                'white and bare).',
    'gnome_female': 'Gnome female: a round, egg-shaped body with NO legs and NO feet (the open bottom of the body, where '
                    'the feet were removed, is covered by the hem), short stubby arms held out and down, and a round bare '
                    'white head. There are no separate upper legs: body armour/clothing that would cover the thighs '
                    'becomes a skirt around the lower body. A hem may hang slightly below the body; never draw legs or '
                    'feet. Keep everything proportionate to this small round body. Keep the green a pure #19D228 '
                    '(hue ~125 deg) in every facet, never a yellow-green/lime.',
    'halfling_female': 'Halfling female in a T-pose: short, with LARGE bare feet and a large white head (includes hair '
                       'and ears; keep white and bare).',
    'halfling_male': 'Halfling male in a T-pose: short, with LARGE bare feet and a large white head (includes ears; '
                     'keep white and bare).',
    'human_female': 'Slim human female in a T-pose.',
    'human_male': 'Human male in a T-pose.',
    'orc_female': 'Muscular orc female in an A-POSE: the arms hang down and slightly out from the body, NOT a T-pose. '
                  'Keep the A-pose exactly; the hands are at hip height. The white head includes the boar-like snout '
                  'and ears (keep white and bare).',
    'orc_male': 'Huge muscular orc male in an A-POSE: massive arms hang down and out from the body, NOT a T-pose. Keep '
                'the A-pose exactly; the big hands are at hip height. The white head includes the tusked snout and '
                'ears (keep white and bare).',
}

COVERAGE = {
    'body_armor': 'torso and back, shoulders/upper arms ending ABOVE the elbows, waist/hips and both upper legs ending '
                  'ABOVE the knees. Forearms, hands, lower legs, feet, neck and head stay bare base. No gauntlet or boot.',
    'right_gauntlet_boot': 'exactly one gauntlet/glove on the anatomical RIGHT hand and forearm (stopping below the '
                           'elbow) and one boot on the anatomical RIGHT foot and lower leg (ending below the knee). '
                           'Torso, upper arms, thighs and the entire anatomical-LEFT arm and leg stay bare base (the '
                           'left hand and left foot stay white).',
    'outfit': 'the clothing as a whole, including footwear on the anatomical RIGHT foot only if the design has it. The '
              'head, BOTH hands and the anatomical-LEFT foot stay bare (white). No gloves, no headwear.',
}

# User rule 2026-10-25: gnomes get no footwear (their tiny feet under the round body kept drifting in the views).
# The gauntlet/boot module is a glove/gauntlet only. Since 2026-10-26 the gnome base has no feet at all (user).
GNOME_COVERAGE = {
    'body_armor': 'torso and back, shoulders/upper arms ending ABOVE the elbows, and the round lower body: this gnome has '
                  'no legs or feet, so what would cover the thighs becomes a skirt of plates, lames or tassets around the '
                  'lower body (it may hang slightly below the body). Forearms, hands, neck and head stay bare base. No '
                  'gauntlet, greaves or boots.',
    'right_gauntlet_boot':'exactly one gauntlet/glove on the anatomical RIGHT hand and forearm (stopping below the '
                           'elbow) and NO boot: gnomes get no footwear and this base has no feet. Torso, upper arms and '
                           'the entire anatomical-LEFT arm stay bare base (the left hand stays white).',
    'outfit': 'the clothing as a whole WITHOUT footwear: this gnome base has no legs or feet, so never draw legs, feet, '
              'shoes or boots. The hem covers the open bottom of the body and may hang slightly below it (a thin strip '
              'of bare red body under the hem is fine too). The head and BOTH hands stay bare (left hand white, right '
              'hand red). No gloves, no headwear.',
}


# Plate with knee cops (user request 2026-10-25; `knee_cops` in the body_armor prompts json): the green thigh plate ends just
# above the knee and a separate blue cop covers it, ending just below. Gnomes have no knee section.
KNEE_COVERAGE = ('torso and back, shoulders/upper arms ending ABOVE the elbows, waist/hips and both upper legs, plus a '
                 'separate BLUE knee cop on each knee: the green thigh plate ends just ABOVE the knee, the blue cop covers '
                 'the whole knee and ends just BELOW it (about a third of the way to the ankle). Forearms, hands, the shins '
                 'below the knee cops, feet, neck and head stay bare base. No gauntlet or boot.')
FLAG_KEYS = ('piece_colours', 'rigid_sections', 'knee_cops')   # conversion switches read by convert_job.py


def coverage(module, race, knee=False):
    if race.startswith('gnome'):
        return GNOME_COVERAGE.get(module, COVERAGE[module])
    return KNEE_COVERAGE if knee and module == 'body_armor' else COVERAGE[module]


def record_flags(notes, module, race):
    """The conversion switches set in a concept's prompts json (no knee cops for gnomes)."""
    data = load(notes / f'{module}_prompts.json', {})
    return {k: True for k in FLAG_KEYS if data.get(k) and not (k == 'knee_cops' and race.startswith('gnome'))}


CAMERAS = ('front_three_quarter is from the character\'s front-LEFT, 45 degrees around, looking DOWN from 45 degrees '
           'above (shoulders and tops visible; the anatomical-left side nearest the camera). back is level from '
           'directly behind. back_three_quarter is from the character\'s back-RIGHT, 45 degrees around, looking UP '
           'from 45 degrees below (undersides, inner legs and crotch visible, head small and partly hidden; '
           'anatomical-right side nearest the camera). Match Image 1\'s camera exactly; do not revert to a level side '
           'view.')
# Gnome-only back three-quarter camera (user, 2026-10-26; base_views/gnome_female/transforms.json): back-LEFT, 10 degrees below.
GNOME_CAMERAS = ('front_three_quarter is from the character\'s front-LEFT, 45 degrees around, looking DOWN from 45 '
                 'degrees above (shoulders and tops visible; the anatomical-left side nearest the camera). back is level '
                 'from directly behind. back_three_quarter (a GNOME-ONLY camera) is from the character\'s back-LEFT, 45 '
                 'degrees around from straight behind, looking UP only slightly from 10 degrees below (almost level): '
                 'the anatomical-LEFT side is nearest the camera, the near arm reaches out to the viewer\'s left and ends '
                 'in the bare white left hand, and the anatomical-right arm is hidden behind the body. It is NOT the usual '
                 'back-right view; do not mirror it. Match Image 1\'s camera exactly.')


# Dwarf-only front three-quarter camera (user, 2026-10-26; base_views/dwarf_*/transforms.json): 20 degrees above, not 45.
DWARF_CAMERAS = CAMERAS.replace(
    'looking DOWN from 45 degrees above (shoulders and tops visible; the anatomical-left side nearest the camera)',
    'looking DOWN from only 20 degrees above (a DWARF-ONLY camera, lower than the usual 45: the short legs and the white left '
    'foot stand clear below the body; the anatomical-left side nearest the camera)')
assert DWARF_CAMERAS != CAMERAS


def cameras(race):
    return GNOME_CAMERAS if race.startswith('gnome') else DWARF_CAMERAS if race.startswith('dwarf') else CAMERAS

STYLE = ('Matte papercraft cardstock, broad planes, crisp folds. Garment hues are exactly green ~#19D228 primary, blue '
         '~#1E41EB secondary, yellow ~#F5DC14 tertiary and magenta ~#E119DC accent (only those the design uses), lit '
         'with the same light direction, per-facet shading, contact shadows and crease occlusion as the base (hue '
         'assignments, not flat fills). No white/grey/black/cream/orange/other hues on the garment, no outlines, no '
         'sun or moon motifs, no weapons/props/headwear/capes, no text or watermark. Preserve exactly the base pose, '
         'proportions, camera, framing, alpha and the uncovered base colours: red body #E61923 and the white anchor '
         '#EDEDED on the head, anatomical-left hand and anatomical-left foot.')


def emit(line):
    line = f'{datetime.now():%H:%M:%S} {line}'
    print(line, flush=True)
    with EVENTS.open('a') as handle:
        handle.write(line + '\n')


def load(path, default=None):
    # utf-8-sig: Codex on Windows writes some JSON records with a UTF-8 BOM (2026-11-09)
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.replace(path)


def framing_lock(base):
    out = subprocess.run([sys.executable, str(ROOT / 'tools/framing_lock.py'), str(base)],
                         capture_output=True, text=True, check=True).stdout.strip().splitlines()
    return ' '.join(line for line in out if not line.startswith('#')).strip()


def design_line(notes, module):
    """'Design: ...' (or the first prose line) and the colour roles of a concept's design record."""
    text = (notes / f'{module}_design_record.md').read_text()
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith('#')]
    design = next((l for l in lines if not l.startswith(('Colours', 'Review', 'Attempts'))), '')
    colours = next((l for l in lines if l.startswith('Colours')), '')
    return f'{design.removeprefix("Design:").strip()} {colours}'.strip()


def add_task(name, prompt, refs, outputs, priority, gate=None):
    task = {'name': name, 'priority': priority, 'prompt': prompt, 'refs': [Path(r).as_posix() for r in refs],
            'outputs': [Path(o).as_posix() for o in outputs], 'checks': [], 'max_attempts': 3}
    if gate:
        task['gate'] = gate
    for out in outputs:
        out = Path(out)
        if out.suffix == '.png':
            race = out.parent.parent.name if out.parent.name in (*ARMOR, 'outfit') else out.parent.name
            view = out.stem if out.stem in VIEWS else 'front'
            task['checks'].append([(ROOT / 'base_views' / race / f'{view}.png').as_posix(), out.as_posix()])
    if (TASKS / f'{name}.json').exists():
        print(f'exists, skipped: {name}')
        return
    save(TASKS / f'{name}.json', task)
    print(f'added {name}')


ACCOUNT_LIMIT_PROBE = 3600   # seconds; retry an account usage limit hourly instead of waiting for its date
MIN_FREE = 2 * 2**30   # don't start image jobs with less free disk (the disk filled on 2026-10-26)
# Races converted from front + back only. Gnomes were (user rule 2026-10-25) until the footless gnome base with its
# back-left camera (2026-10-26); held gnome tasks made before that keep their views_omitted.json markers.
TWO_VIEW_RACES = ()


def two_view_marker(module_dir):
    """Write views_omitted.json for the three-quarter views a two-view (gnome) module lacks; returns the omitted names."""
    missing = [v for v in ('front_three_quarter', 'back_three_quarter') if not (module_dir / f'{v}.png').exists()]
    if missing and not (module_dir / 'views_omitted.json').exists():
        save(module_dir / 'views_omitted.json', {'omitted': missing, 'date': datetime.now().strftime('%Y-%m-%d'),
             'reason': 'two-view race (gnome): converted from front + back; three-quarter views are not generated '
                       '(user rule 2026-10-25)'})
    return missing


def notes_file(notes: Path, module: str):
    """The claimed set's concept front (design reference) in .concept_notes/, else None."""
    for name in (f'{module}_front.png', f'{module}_concept_front.png'):
        if (notes / name).is_file():
            return notes / name
    return None


def add_views(args):
    job = Path(args.job).resolve()
    races = args.races.split(',') if args.races else sorted(
        p.name for p in job.iterdir() if p.is_dir() and not p.name.startswith('.'))
    set_id = job.name.split('--')[0]
    extra = Path(args.extra_file).read_text().strip() + '\n' if args.extra_file else ''
    for race in races:
        two_view = race.startswith(TWO_VIEW_RACES)
        notes = job / race / '.concept_notes'
        for module_dir in sorted(p for p in (job / race).iterdir() if p.is_dir() and not p.name.startswith('.')):
            module = module_dir.name
            if two_view:
                two_view_marker(module_dir)
            # The concept art is the design reference (never a view): with it, ALL views including a fresh
            # front are generated. A legacy job that still keeps its concept front AS front.png only gets the
            # views that are missing (the pre-2026-10-28 behaviour).
            in_notes = notes_file(notes, module)
            concept_front = in_notes or ((module_dir / 'front.png') if (module_dir / 'front.png').exists() else None)
            regen_front = in_notes is not None
            if two_view:
                views = ('front', 'back') if regen_front else ('back',)
            else:
                views = ALL_VIEWS if regen_front else ('front_three_quarter', 'back', 'back_three_quarter')
            if all((module_dir / f'{v}.png').exists() for v in views):
                continue
            flags = record_flags(notes, module, race)
            locks = '\n'.join(f'- {v}: {framing_lock(job / ".base_views" / race / f"{v}.png")}' for v in views)
            prompt = f'''You are generating {"the back view (this race is converted from front + back only)" if two_view and not regen_front else ("all " + ("two" if two_view else "four") + " views (a fresh front included; user rule 2026-10-28: the concept art is the design reference, never a view)" if regen_front else "the remaining three")} papercraft {"armor" if module in ARMOR else "outfit"} view{"" if two_view else "s"} for ONE module of an approved set. Follow AGENTS.md and PROMPTING_GUIDE.md exactly (read PROMPTING_GUIDE.md first, incl. "Lighting and colour", "Generate remaining views after approval" and "Automated framing check"). Use the built-in image generation tool only (no CLI/API fallback, no PIL drawing, no Blender renders, no resize/crop/pad of any image).

FRAMING LOCK - open EVERY generation prompt with the sentence for that view, before the design brief:
{locks}

Set: {set_id}, race_gender {race}, module: {module}. Base palette white_anchor_v2 ({job}/.base_views/{race}/palette.json).
Character: {RACES[race]}
Output dir: {module_dir}/
{"Generate ONLY back.png. Do NOT generate front_three_quarter.png or back_three_quarter.png (views_omitted.json in the module dir lists them; leave it as is)." if two_view and not regen_front else ("Generate ONLY front.png and back.png (views_omitted.json lists the three-quarter views; leave it as is)." if two_view else "Generate, in this order: front.png (an edit of the base front), front_three_quarter.png (use your saved front.png as an extra design reference), back.png, then back_three_quarter.png (use your saved back.png as an extra design reference).")} {("The approved concept art is " + str(concept_front) + " - the DESIGN REFERENCE ONLY: copy its garment design (parts, colours, folds, coverage), never its framing; it is an unchecked sketch and must NOT be saved or copied as a view." if concept_front else "There is no concept front to reference; follow the design line below.")}
For each view, Image 1 is the matching base view (edit target): {job}/.base_views/{race}/<view>.png; Image 2 is the approved front design. Attached refs: {"base back" if two_view else "base front_three_quarter, base back, base back_three_quarter (in that order)"}, then the approved front.

Cameras (state these explicitly in the prompts): {cameras(race)}

Coverage: {coverage(module, race, flags.get('knee_cops'))} Show only this module.
Design (from the approved front's design record): {design_line(notes, module)}
{extra}{STYLE} Same parts, colours, folds and coverage as the approved concept design; infer hidden detail conservatively; no redesign. Anatomical RIGHT is viewer-left from front, viewer-right from back; follow that limb through the three-quarter views.

Each output: native size of its base view (1254x1254) RGBA with transparent background; verify the saved PNG's real size and alpha with PIL and regenerate on mismatch. Copy the chosen output from ~/.codex/generated_images/... to the target path. Never overwrite existing images: keep rejected attempts as <view>_rejected_NN.png OUTSIDE the module dir, in {notes}/{module}_<view>_rejected_NN.png (the module dir must end with exactly {"front.png and back.png plus views_omitted.json" if two_view else "the four view PNGs"}). Run python3 tools/check_framing.py {job}/.base_views/{race}/<view>.png <new image> on each and regenerate on FRAMING flags (POSE: inspect, usually regenerate; a red-body centroid note alone is acceptable for right_gauntlet_boot). Also run python3 tools/check_concept_colours.py on each saved view if it accepts it. At most 4 NEW attempts per view in THIS run (attempts recorded by earlier runs in the prompts log do not count; always make fresh attempts for missing views); if the image tool hits a usage limit, stop and report.
Scope: write only the {"back view" if two_view and not regen_front else ("two views" if two_view else ("four views" if regen_front else "three views"))} in the module dir, rejected attempts in .concept_notes/, and append the exact prompts, references, dimensions, sha256 and check results to {notes}/{module}_views_prompts.json{f" (keep its top-level keys {json.dumps(flags)})" if flags else ""}. No workflow.py changes, no folder moves, no Tripo/Hyper3D/formy3d calls, no git commits, no other edits. End with a short report: files, dimensions, check_framing results, rejected attempts, known issues.
'''
            refs = [job / '.base_views' / race / f'{v}.png' for v in views] + ([concept_front] if concept_front else [])
            add_task(f'views--{set_id}--{race}--{module}', prompt, refs,
                     [module_dir / f'{v}.png' for v in views], args.priority)


def add_fronts(args):
    design_job = Path(args.design_job).resolve()
    designs = sorted(p.name for p in design_job.iterdir() if p.is_dir() and not p.name.startswith('.'))
    design_race = 'human_male' if 'human_male' in designs else designs[0]   # the race the user approved
    design_label = design_race.replace('_', '-')
    human = design_job / design_race
    extra = Path(args.extra_file).read_text().strip() + '\n\n' if getattr(args, 'extra_file', None) else ''
    modules = sorted(p.name for p in human.iterdir() if p.is_dir() and not p.name.startswith('.'))
    notes = human / '.concept_notes'
    for race in args.races.split(','):
        base = ROOT / 'base_views' / race / 'front.png'
        out_dir = ROOT / 'concepts' / args.set_id / race
        flags = {m: record_flags(notes, m, race) for m in modules}
        outputs = [out_dir / f'{m}_front.png' for m in modules]
        items = '\n'.join(f'{i + 1}. `{out_dir}/{m}_front.png`: the base wearing ONLY the {m} module. Coverage: '
                          f'{coverage(m, race, flags[m].get("knee_cops"))} Design: {design_line(notes, m)}' for i, m in enumerate(modules))
        keys = ''.join(f' Set the top-level keys {json.dumps(f)} in `{m}_prompts.json` (the conversion reads them).'
                       for m, f in flags.items() if f)
        refs_text = '\n'.join(f'- Image {i + 2}: `{human / m / "front.png"}` - the approved {design_label} {m} front of '
                              f'this set. DESIGN REFERENCE ONLY: copy its design language (shapes, layering, colour '
                              f'roles, plainness), never its body, proportions, pose or framing.'
                              for i, m in enumerate(modules))
        prompt = f'''# Task: {args.set_id} FRONT concept{"s" if len(modules) > 1 else ""} for {race}

You are generating the front-view concept{"s" if len(modules) > 1 else ""} of the existing, user-approved set `{args.set_id}` for the base character `{race}`. Follow `AGENTS.md` and `PROMPTING_GUIDE.md` in this folder (read both first). Use the built-in image generation tool (imagegen skill) in its edit flow; never the CLI/API fallback, never other drawing tools, and never resize/crop/pad/stretch any image.

FRAMING LOCK - open every generation prompt with this sentence, before the design brief:
{framing_lock(base)}

## Attached images
- Image 1: `{base}` - the EXACT edit target (native 1254 x 1254 RGBA, transparent background). Edit this image directly.
{refs_text}

## Character
{RACES[race]}

## Outputs ({"separate images; do not combine" if len(modules) > 1 else "one image"})
{items}

Match the {design_label} design closely (it is the same set) but fitted to this body.

{extra}
## Invariants
- Strictly local wardrobe edit on Image 1's canvas. Do not redraw, rescale, move or re-pose the base figure or change the camera.
- {STYLE}
- Output natively at EXACTLY the size of Image 1 (1254 x 1254), PNG RGBA with a genuinely transparent background.

## Verify and retry (do this yourself for each image)
1. Copy the chosen output from `~/.codex/generated_images/...` to the output path. Never overwrite an existing image. Do not keep rejected attempts; count them.
2. Check the saved PNG's real size and mode with PIL.
3. One-shot (user rule 2026-10-28, concept art is never checked): generate each front ONCE and save it. Do NOT run check_framing or check_concept_colours, do NOT inspect, judge or reject the images, do NOT regenerate for style, composition, colours or framing. Verify with python3+PIL only that each saved file is the native size of its base with alpha; if (and only if) the FILE ITSELF is the wrong size or lacks alpha, generate that image once more. If the image tool hits a usage limit, stop and report.
4. Look at each kept image yourself for module coverage and anatomical side.

## Records
Write `{out_dir}/<module>_prompts.json` (set_id, race_gender, module, view front, tool, reference paths + sha256, palette copy of base_views/{race}/palette.json, swatches, exact final prompt, attempt count, dimensions, sha256, check results, notes) and `{out_dir}/<module>_design_record.md` with the lines `Design: ...`, `Colours: {{json of role -> parts}}`, `Review: ...`, `Attempts: N. <check results>` for each module.{keys}

## Scope limits
No `workflow.py` state changes, no folder moves between pipeline stages, no Tripo/Hyper3D/formy3d calls, no git commits, no edits outside `{out_dir}/`.

## Final report
Short: files written, dimensions, attempts and known issues (checks skipped by rule).
'''
        refs = [base] + [human / m / 'front.png' for m in modules]
        add_task(f'fronts--{args.set_id}--{race}', prompt, refs, outputs, args.priority)


CONCEPT_RULES = """COLOURS (hue assignments, lit and shaded exactly like the base: same light direction, per-facet shading, contact shadows, crease occlusion; a shadowed facet is a darker version of the same hue, a lit one lighter; no outlines):
   GREEN ~#19D228 = primary, the dominant/largest garment area.
   BLUE ~#1E41EB = secondary, the second large area.
   YELLOW ~#F5DC14 = tertiary: trim, belts, laces, buttons, borders, studs. Clean lemon yellow, never gold/orange/brown.
   MAGENTA ~#E119DC = accent: small motifs, gems, brooches only. Keep it true magenta (not hot pink).
   Use only the colours the brief names. No other garment hues: no white, grey, silver, black, cream, brown, orange, rust, purple, teal (linen, lace, fur and leather are expressed in these four hues). Keep each colour a large clean region; no gradients between colours, no specks.

INVARIANTS:
- Preserve anatomy, proportions, pose, level front camera, framing/scale/position and transparent alpha exactly.
- Base markers: red body (#E61923) where uncovered; the head, the anatomical-LEFT hand and the anatomical-LEFT foot are WHITE anchors (#EDEDED) and must stay bare, white and unchanged. No helmet/headwear (hats, hoods, veils, coifs, wimples, headdresses), mask, cape, cloak, weapons, shields, props, bags, labels or text.
- NO sun or moon symbology anywhere: no suns, sunbursts, radiating rays, halos, crescents, moons or eclipses.
- Style: medieval, simple papercraft, broad polygon planes, crisp folds and ridges, matte cardstock; no photoreal cloth, metal or leather, no shine, no glow, no fine noise or tiny patterns. Material reads through shape: mail as large embossed ring rows, quilting as raised padded ridges, fur trim as chunky faceted tufts, embroidery as raised cardstock strips.
- Motifs (only where the brief has them) are real geometry: raised/embossed or layered cardstock with visible thickness, bold and large; never painted decals.
- Output native 1254x1254 RGBA PNG (the size of Image 1). One-shot (user rule 2026-10-28, concept art is never checked): generate each concept image exactly ONCE and save it. Do NOT run check_framing or check_concept_colours, do NOT inspect, judge or reject the images, do NOT regenerate for style, composition, colours or framing. Verify with python3+PIL only that each saved file is 1254x1254 RGBA; if (and only if) the FILE ITSELF is the wrong size or lacks alpha, generate that image once more. Never resize, crop, pad or stretch."""

MODULE_RULES = {
    'outfit': 'OUTFIT = ONE image, an edit of Image 1: the whole outfit as one garment set. The head, BOTH hands and the anatomical-LEFT foot stay bare and white (sleeves end at the wrists; no gloves). Footwear only on the anatomical RIGHT foot (viewer-left in this front view) if the brief has it. Skirts and robes may be long but stay close to the legs; no hoops, no trains, nothing floating.',
    'armor': 'EACH CONCEPT = TWO SEPARATE IMAGES (an armour pair), both edits of Image 1, reading as one matching set:\n1. body_armor: torso front and back, shoulders/upper arms ENDING ABOVE THE ELBOWS, waist/hips and both upper legs ENDING ABOVE THE KNEES. Forearms, hands, lower legs, feet, neck and head stay bare base. No glove or boot in this image. Left/right symmetric.\n2. right_gauntlet_boot: exactly ONE glove/gauntlet on the anatomical RIGHT hand and forearm (ending below the elbow) and ONE boot on the anatomical RIGHT foot and lower leg (ending below the knee). Anatomical right is VIEWER-LEFT in this front view. Torso, upper arms, thighs and the whole anatomical-left arm and leg stay bare base (left hand and left foot stay white).',
}


def add_concepts(args):
    """Concept fronts from a briefs JSON (list of {set_id, category, race_gender, tradition, briefs, colours[, style]})."""
    entries = json.loads(Path(args.briefs).read_text())
    extra = Path(args.extra_file).read_text().strip() + '\n\n' if args.extra_file else ''
    # plate series bring mandatory section rules in their extra file; don't tell Codex to ignore them
    ignore = '' if 'rigid sections' in extra.lower() else ' Ignore the optional separable-pauldron / rigid-section colour rules.'
    series = Path(args.briefs).stem
    gate = {'all_views': args.after_views.split(',')} if args.after_views else None
    groups = {}
    for entry in entries:
        groups.setdefault((entry['category'], entry['race_gender']), []).append(entry)
    for (category, race), items in sorted(groups.items()):
        armor = any('body_armor' in e['briefs'] for e in items)
        rules = CONCEPT_RULES.replace('Style: medieval,', f"Style: {items[0].get('style', 'medieval')},")
        size = args.armor_per_job if armor else args.outfits_per_job
        base = ROOT / 'base_views' / race / 'front.png'
        for start in range(0, len(items), size):
            chunk = items[start:start + size]
            concepts, outputs = [], []
            for e in chunk:
                folder = ROOT / 'concepts' / e['set_id'] / race
                lines = [f'### `{e["set_id"]}` ({e.get("tradition", category)})']
                lines += [f'- {module}: {text}' for module, text in e['briefs'].items()]
                lines.append(f'- colour roles: {json.dumps(e.get("colours", {}))}')
                lines.append(f'- output folder: {folder}/')
                concepts.append('\n'.join(lines))
                outputs += [folder / f'{module}_front.png' for module in e['briefs']]
            files = ('body_armor_front.png, right_gauntlet_boot_front.png' if armor else 'outfit_front.png')
            records = ('body_armor_prompts.json, right_gauntlet_boot_prompts.json' if armor else 'outfit_prompts.json')
            prompt = f"""Front concepts for the `{series}` series: {category.replace('_', ' ')} ({race}). Use the imagegen skill / built-in image generation tool only (no CLI/API fallback, no other drawing tools). Read AGENTS.md and PROMPTING_GUIDE.md first (Lighting and colour, Motifs, framing lock, per-concept folder layout) and follow them, except where this prompt overrides them.{ignore}

Image 1 = {base} (1254x1254 RGBA, white-anchor marker base); every image is an edit of it.
Character: {RACES[race]}

{MODULE_RULES['armor' if armor else 'outfit']}

START EVERY IMAGE PROMPT WITH THIS FRAMING LOCK (measured from the base):
{framing_lock(base)}

{rules}

PER-CONCEPT LAYOUT (create folders; never overwrite an existing file; skip any concept whose PNGs already exist):
concepts/<set_id>/{race}/
 - {files} (copy the accepted outputs from ~/.codex/generated_images/...)
 - {records}: {{"set_id", "race_gender": "{race}", "module", "view": "front", "series": "{series}", "category": "{category}", "tradition", "brief", "colours": {{role: what it is in this design}}, "palette": base_views/{race}/palette.json contents, "prompt" (exact final prompt), "reference": "base_views/{race}/front.png", "size", "sha256", "attempts" (count), "check_framing", "check_concept_colours", "notes"}}
 - <module>_design_record.md per image: lines `Design: ...`, `Colours: {{json role -> parts}}`, `Review: ...`, `Attempts: N. Checks skipped (one-shot concept rule).`.
 - Save the first generated image for each concept and keep no other attempts. In the records JSON write "skipped" for the check_framing and check_concept_colours fields and the real attempt count for attempts (1 unless the file had to be regenerated for size/alpha). {"Finish both images of a concept before starting the next concept. " if armor else ""}If the image tool hits a usage limit, stop and report.

SCOPE LIMITS: no workflow.py state changes, no folder moves between stages, no Tripo/Hyper3D/formy3d calls, no git commits, no edits outside the concepts/<set_id>/ folders named below.

END with a short report: files written, verified dimensions, attempts per image, known issues (checks skipped by rule).

{extra}
## Concepts for this job (do them in order)

""" + '\n\n'.join(concepts) + '\n'
            add_task(f'concepts--{series}--{category}--{race}--{start // size + 1:02d}', prompt, [base], outputs,
                     args.priority, gate)


def check_base(base, out):
    """The base view to check `out` against: the job's frozen `.base_views/<race>/<view>.png` when `out` is a view inside a
    claimed job (the active base can change after the claim, e.g. the dwarf 20-degree front three-quarter), else `base`."""
    out = Path(out)
    frozen = out.parents[2] / '.base_views' / out.parent.parent.name / out.name if len(out.parents) > 2 else None
    return str(frozen) if frozen and frozen.is_file() else base


def limit_reset(log, now=None):
    """Epoch to resume at when the job stopped on a usage limit, else None. The daily imagegen limit carries
    `resets_at`; the account limit ("You've hit your usage limit ... try again at Nov 2nd, 2026 7:21 AM") is probed
    again after ACCOUNT_LIMIT_PROBE at the latest, because it has been lifted early before (user, 2026-10-26)."""
    if not log.exists():
        return None
    text = log.read_text(errors='replace')
    found = re.findall(r'usage_limit_reached.*?resets_at\\?":\s*(\d+)', text)
    if found:
        return int(found[-1])
    if 'hit your usage limit' not in text:
        return None
    probe = (now or time.time()) + ACCOUNT_LIMIT_PROBE
    for month, day, year, clock in reversed(re.findall(
            r'try again at ([A-Z][a-z]{2})[a-z]* (\d{1,2})(?:st|nd|rd|th)?, (\d{4}),? (\d{1,2}:\d{2} ?[AP]M)', text)):
        try:
            at = datetime.strptime(f'{month} {day} {year} {clock.replace(" ", "")}', '%b %d %Y %I:%M%p').timestamp()
        except ValueError:
            continue
        return int(min(at, probe))
    return int(probe)


def gate_open(gate, cache):
    """{'all_views': [set ids]}: every module of those sets has all four views for every race with a base view."""
    key = json.dumps(gate, sort_keys=True)
    if key not in cache:
        races = sorted(p.name for p in (ROOT / 'base_views').iterdir() if (p / 'front.png').exists())
        missing = 0
        for set_id in gate.get('all_views', []):
            jobs = [j for stage in ('processing_views', 'multiview_ready', 'processing', 'completed')
                    for j in (ROOT / stage).glob(f'{set_id}--*')]
            modules = {m.name for j in jobs for m in (j / 'human_male').glob('*') if (m / 'front.png').exists()}
            for race in races:
                for module in modules or {'?'}:
                    if not any(all((j / race / module / f'{v}.png').exists() for v in ('front', *VIEWS))
                               for j in jobs):
                        missing += 1
        cache[key] = missing == 0
    return cache[key]


STAGES = ('processing_views', 'multiview_ready', 'processing', 'completed', 'failed')


def current_paths(text):
    """Point `<stage>/<set>--<id>/` paths at the stage the job is in now (jobs move on after a task is queued)."""
    def fix(match):
        job = match.group(2)
        stage = next((s for s in STAGES if (ROOT / s / job).is_dir()), match.group(1))
        return f'/{stage}/{job}/'
    return re.sub(r'/(processing_views|multiview_ready|processing|completed|failed)/([^/\s]+--[0-9a-f]{12})/', fix, text)


def pause_reason():
    try:
        return json.loads(PAUSE.read_text()).get('reason', 'PAUSE file')
    except (OSError, ValueError, AttributeError):
        return 'PAUSE file'


def views_paused():
    """PAUSE stops new image jobs only when its scope lists "views" (a manual pause; a file without a scope pauses
    everything). A screen-on pause has scope ["conversions"]: view generation keeps running (user, 2026-10-26)."""
    if not PAUSE.exists():
        return False
    try:
        scope = json.loads(PAUSE.read_text()).get('scope')
    except (OSError, ValueError, AttributeError):
        return True
    return scope is None or 'views' in scope


# Windows (2026-11-08): the .sh launcher runs under Git Bash (never WSL's bash.exe), and there is no pgrep.
SHELL = [os.environ.get('GIT_BASH') or 'C:/Program Files/Git/bin/bash.exe'] if os.name == 'nt' else []


def job_alive(job, status):
    """A job this runner did not start (runner restarted): pgrep on Linux; on Windows it counts as alive until its
    .done marker appears or its timeout (CODEX_TIMEOUT 5400 s + 10 min) has passed since it started."""
    if os.name != 'nt':
        return subprocess.run(['pgrep', '-f', f'codex_imagegen.sh {job} '], capture_output=True).returncode == 0
    if (ROOT / '.codex_jobs' / job / '.done').exists():
        return False
    try:
        started = datetime.fromisoformat(status['jobs'][-1]['started'])
    except (KeyError, IndexError, ValueError):
        return False
    return (datetime.now() - started).total_seconds() < 5400 + 600


def run(args):
    """Jobs are tracked by their .codex_jobs/<job>/.done marker, so a restarted runner adopts jobs still in flight."""
    procs = {}
    pause_until, idle_reported, low_disk, paused = 0, False, False, False
    while True:
        tasks = sorted((t for t in (load(p) for p in TASKS.glob('*.json')) if t), key=lambda t: (t['priority'], t['name']))
        statuses = {t['name']: load(STATUS / f'{t["name"]}.json') or {'state': 'pending', 'attempts': 0, 'jobs': []}
                    for t in tasks}
        for task in tasks:
            name, status = task['name'], statuses[task['name']]
            if status['state'] != 'running':
                continue
            job = status['jobs'][-1]['job']
            jobdir = ROOT / '.codex_jobs' / job
            alive = (name in procs and procs[name].poll() is None) or (name not in procs and job_alive(job, status))
            if not (jobdir / '.done').exists() and alive:
                continue
            procs.pop(name, None)
            exit_code = (jobdir / 'exit_code').read_text().strip() if (jobdir / 'exit_code').exists() else 'lost'
            missing = [o for o in task['outputs'] if not Path(o).exists()]
            reset = limit_reset(jobdir / 'log.txt')
            status['jobs'][-1]['exit'] = exit_code
            if missing and reset:
                pause_until = max(pause_until, reset + 120)
                status['state'] = 'pending'
                emit(f'LIMIT {name}: Codex usage limit, paused until '
                     f'{datetime.fromtimestamp(pause_until):%Y-%m-%d %H:%M}')
            elif missing:
                status['attempts'] += 1
                status['state'] = 'failed' if status['attempts'] >= task['max_attempts'] else 'pending'
                emit(f'{"FAILED" if status["state"] == "failed" else "RETRY"} {name}: missing '
                     f'{[Path(m).name for m in missing]} (job {job}, exit {exit_code})')
            else:
                flags = {}
                for base, out in task['checks']:
                    check = subprocess.run([sys.executable, str(ROOT / 'tools/check_framing.py'), check_base(base, out), out],
                                           capture_output=True, text=True, cwd=ROOT)
                    flags[Path(out).name] = [l.strip() for l in check.stdout.splitlines()
                                             if l.strip().startswith('-')] if check.returncode else []
                status['state'], status['checks'] = 'done', flags
                bad = {k: v for k, v in flags.items() if v}
                emit(f'DONE {name}: ' + (f'flags={json.dumps(bad)}' if bad else 'ok'))
            save(STATUS / f'{name}.json', status)
        running = [t for t in tasks if statuses[t['name']]['state'] == 'running']
        cache = {}
        pending = [t for t in tasks if statuses[t['name']]['state'] == 'pending'
                   and (not t.get('gate') or gate_open(t['gate'], cache))]
        gated = sum(1 for t in tasks if statuses[t['name']]['state'] == 'pending') - len(pending)
        if not running and not pending:
            if not idle_reported:
                emit(f'IDLE: queue empty' + (f' ({gated} gated task(s) waiting)' if gated else ''))
            if args.exit_when_idle and not gated:
                return
            idle_reported = True
        else:
            idle_reported = False
        free = shutil.disk_usage(ROOT).free
        if not PAUSE.exists():
            paused = False
        if views_paused():
            if not paused and pending:
                emit(f'PAUSED ({pause_reason()}); not starting jobs (running ones continue)')
                paused = True
        elif free < MIN_FREE and pending:
            if not low_disk:
                emit(f'LOW DISK: {free / 2**30:.1f} GB free (minimum {MIN_FREE // 2**30} GB); not starting jobs')
            low_disk = True
        elif time.time() >= pause_until:
            low_disk = False
            for task in pending[:max(0, args.parallel - len(running))]:
                status = statuses[task['name']]
                existing = [o for o in task['outputs'] if Path(o).exists()]
                missing = [o for o in task['outputs'] if not Path(o).exists()]
                prompt = current_paths(task['prompt'])
                if existing:
                    prompt = (f'RESUME NOTE: a previous run of this task already saved {existing}. Keep those files '
                              f'unchanged (do not regenerate or overwrite them, but use them as references where the '
                              f'task says so); generate only the missing outputs: {missing}. Append to the existing '
                              f'records instead of replacing them. Attempt limits apply to THIS run only: attempts recorded by earlier runs do '
                              f'not count, so you have the full number of fresh attempts for each missing output.\n\n') + prompt
                number = len(status['jobs']) + 1
                while (ROOT / '.codex_jobs' / f'{task["name"]}-a{number:02d}').exists():
                    number += 1   # a re-created task (e.g. a redone gnome) must not adopt an old run's finished job folder
                job = f'{task["name"]}-a{number:02d}'
                prompt_file = BATCH / 'prompts' / f'{job}.md'
                prompt_file.parent.mkdir(parents=True, exist_ok=True)
                prompt_file.write_text(prompt)
                procs[task['name']] = subprocess.Popen(
                    [*SHELL, str(ROOT / 'tools/codex_imagegen.sh'), job, str(prompt_file),
                     *(current_paths(r) for r in task['refs'])],
                    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env={**os.environ, 'CODEX_TIMEOUT': '5400'})
                status['state'] = 'running'
                status['jobs'].append({'job': job, 'started': datetime.now().isoformat(timespec='seconds')})
                save(STATUS / f'{task["name"]}.json', status)
                emit(f'START {job}')
        time.sleep(10)


def show_status(_args):
    counts = {}
    for path in sorted(TASKS.glob('*.json')):
        state = (load(STATUS / path.name) or {}).get('state', 'pending')
        counts[state] = counts.get(state, 0) + 1
        if state in ('running', 'failed'):
            print(f'{state:8} {path.stem}')
    print(json.dumps(counts))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    views = sub.add_parser('add-views')
    views.add_argument('job')
    views.add_argument('--races')
    views.add_argument('--extra-file', help='text added after the design line (e.g. plate section rules)')
    views.add_argument('--priority', type=int, default=2)
    fronts = sub.add_parser('add-fronts')
    fronts.add_argument('set_id')
    fronts.add_argument('--design-job', required=True)
    fronts.add_argument('--races', required=True)
    fronts.add_argument('--extra-file', help='text appended to every prompt (e.g. plate section rules)')
    fronts.add_argument('--priority', type=int, default=1)
    concepts = sub.add_parser('add-concepts')
    concepts.add_argument('briefs')
    concepts.add_argument('--after-views', help='comma-separated set ids whose all-race views must be complete first')
    concepts.add_argument('--extra-file', help='series rules added before the concept list')
    concepts.add_argument('--outfits-per-job', type=int, default=5)
    concepts.add_argument('--armor-per-job', type=int, default=3)
    concepts.add_argument('--priority', type=int, default=5)
    runner = sub.add_parser('run')
    runner.add_argument('--parallel', type=int, default=4)
    runner.add_argument('--exit-when-idle', action='store_true')
    sub.add_parser('status')
    args = parser.parse_args()
    BATCH.mkdir(parents=True, exist_ok=True)
    {'add-views': add_views, 'add-fronts': add_fronts, 'add-concepts': add_concepts, 'run': run,
     'status': show_status}[args.command](args)


if __name__ == '__main__':
    main()
