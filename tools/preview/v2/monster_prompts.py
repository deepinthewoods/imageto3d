"""Prompt templates of the monster extra views (DESIGN_PHASE5.md section C).

Three extra views are made from a picked monster's `views/<v>/front.png` (Image 1) by Codex jobs that the imageto3d view runner
launches with the monster pipeline's own `tools/codex_imagegen.sh` (task field `"launcher": "monsters"`):

    left.png   the camera walked a quarter of the way round to the creature's own LEFT side (about 90 degrees)
    back.png   the camera walked round to the opposite side (180 degrees): the established Hobgoblin back wording
    right.png  the camera walked a quarter of the way round to the creature's own RIGHT side (about 90 degrees)

All three share: the same design and the same marker colours (one line per role of the front's colours.json), front-only ornaments
not copied, the whole creature in frame, a transparent background and the "ONE hue" paragraph of the monster PROMPTING_GUIDE.md.
No framing, pose or colour checks and no aesthetic retries (user, 2026-11-25; phase 5 review F17): Codex checks only the technical
format of its saved file (a decodable 8-bit RGBA PNG with real transparency) and regenerates once only when that is wrong. The view runner
validates the same format and the provenance record (task fields `validate` / `provenance`) and fails the attempt otherwise.

    image_prompt(view, monster, colours)   the image-generation prompt (what the `<name>.prompts.json` record calls `prompt`)
    job_prompt(view, monster, variant, front, out, colours)   the whole self-contained Codex job prompt (the view task's `prompt`)

Everything returned is plain ASCII: tools/view_batch.py writes the prompt file with the platform's default encoding.
"""
from __future__ import annotations

from pathlib import Path

VIEWS = ('left', 'back', 'right')                 # the extra views, in task order
ROLE_ORDER = ('skin', 'clothing', 'straps', 'hard', 'eyes', 'extra')
# monster_palette.json (monster_palette_v1): role -> (colour name, hex); a colours.json hex wins for the line
PALETTE = {'skin': ('red', '#E61923'), 'clothing': ('green', '#19D228'), 'straps': ('blue', '#1E41EB'),
           'hard': ('yellow', '#F5DC14'), 'eyes': ('magenta', '#E119DC'), 'extra': ('cyan', '#14DCEB')}
HEX_NAMES = {hexv.upper(): name for name, hexv in PALETTE.values()}

CAMERA = {
    'back': ('Image 1 shows a papercraft creature from the front. Draw the SAME creature from BEHIND: a back three-quarter view from '
             'slightly above. The camera has walked round to the opposite side of the creature (turned 180 degrees around it) and kept '
             'the same height and distance. The creature has not moved: the same pose, every limb, tail, wing and other part where it '
             'is in Image 1. We now see the back of its head, its back and the backs of its limbs.\n\n'
             'It faces away from the camera, so the head is seen from behind with at most a sliver of the face.'),
    'left': ('Image 1 shows a papercraft creature from the front. Draw the SAME creature from its LEFT side: the camera has walked a '
             'quarter of the way round the creature to the creature\'s own LEFT side (about 90 degrees around it) and kept the same '
             'height and distance, still looking from slightly above. The creature has not moved: the same pose, every limb, tail, wing '
             'and other part where it is in Image 1. We now see its LEFT side: its left side is nearest the camera and its right side '
             'is hidden behind its body.\n\n'
             'LEFT means the creature\'s own anatomical left (the side of its left eye, left arm or left foreleg), not the left of '
             'the picture. Its head is seen in profile, facing across the picture.'),
    'right': ('Image 1 shows a papercraft creature from the front. Draw the SAME creature from its RIGHT side: the camera has walked a '
              'quarter of the way round the creature to the creature\'s own RIGHT side (about 90 degrees around it) and kept the same '
              'height and distance, still looking from slightly above. The creature has not moved: the same pose, every limb, tail, '
              'wing and other part where it is in Image 1. We now see its RIGHT side: its right side is nearest the camera and its left '
              'side is hidden behind its body.\n\n'
              'RIGHT means the creature\'s own anatomical right (the side of its right eye, right arm or right foreleg), not the right '
              'of the picture. Its head is seen in profile, facing across the picture.'),
}

SAME_DESIGN = ('It is exactly the same creature and the same design as Image 1: the same proportions, the same parts in the same '
               'places, the same colours, the same folded-paper construction with broad flat polygon planes, crisp folds, matte paper '
               'and soft shading. Do not redesign it and do not add or remove any part. The creature is left-right symmetrical unless '
               'Image 1 shows otherwise. Where Image 1 cannot show a surface, continue the design that is visible from the front in the '
               'simplest way: garments, armour plates, belts and straps run on round the body as plain continuations.')

FRONT_ONLY = {
    'back': ('Ornaments, buckles, pendants, chest plates and anything else that sits on the FRONT in Image 1 are not visible from '
             'behind; do not copy them onto the back.'),
    'left': ('Ornaments, buckles, pendants, chest plates and anything else that sits on the FRONT in Image 1 are seen edge-on from the '
             'side at most; do not move them onto the side, and do not copy anything from the hidden right side onto the left.'),
    'right': ('Ornaments, buckles, pendants, chest plates and anything else that sits on the FRONT in Image 1 are seen edge-on from the '
              'side at most; do not move them onto the side, and do not copy anything from the hidden left side onto the right.'),
}
NO_PROPS = 'No weapons or props, and no cape, backpack, quiver, pouch or other ornament that Image 1 does not have.'

ONE_HUE = ('Parts that touch and are different things never share a colour, exactly as in Image 1, and each part is ONE colour from '
           'edge to edge. Each region is ONE hue. Paper shading may make it lighter or darker along the folds, but never shifts it '
           'toward another hue: no orange or pink skin, no teal or yellow-green clothing, no purple straps, no orange hard parts. No '
           'gradients or blending between two regions; the boundary between two colours is a crisp edge along the part\'s outline. No '
           'other colours anywhere on the creature, no tan or brown (dark crease shadows are fine). The red is a marker, not a final '
           'colour, so do not darken or desaturate it toward brown.')

BACKGROUND = ('Background: genuinely transparent (real alpha), no floor, no cast shadow, no backdrop, no text. Output an RGBA PNG with '
              'the whole creature in frame, every extremity included (horns, crests, ears, tails, wing tips, feet), with clear margin '
              'on every side.')

ASCII_FIX = {'‘': "'", '’': "'", '“': '"', '”': '"', '–': '-', '—': '-', '°': ' degrees',
             '×': 'x', '…': '...', ' ': ' ', '·': '-'}


def ascii_text(text: str) -> str:
    """`text` as plain ASCII (the usual typographic characters replaced, anything else dropped as '?')."""
    for bad, good in ASCII_FIX.items():
        text = text.replace(bad, good)
    return text.encode('ascii', 'replace').decode('ascii')


def _parts(value) -> list[str]:
    if isinstance(value, dict):
        parts = value.get('parts')
        return [str(p) for p in parts if isinstance(p, (str, int, float))] if isinstance(parts, list) else []
    return []


def colour_lines(colours: dict | None) -> list[str]:
    """One `- <colour> <hex>: <parts>` line per role the front's colours.json lists (palette order; unknown roles after them). A role
    without parts still gets its line (the colour must stay the same). Without a usable colours.json: the palette's default meanings."""
    roles = (colours or {}).get('roles') if isinstance(colours, dict) else None
    lines = []
    if not isinstance(roles, dict) or not roles:
        for role in ROLE_ORDER:
            name, hexv = PALETTE[role]
            lines.append(f'- {name} {hexv}: the {role} parts, exactly where Image 1 has this colour.')
        return lines
    order = [r for r in ROLE_ORDER if r in roles] + sorted(r for r in roles if r not in ROLE_ORDER)
    for role in order:
        value = roles[role] if isinstance(roles[role], dict) else {}
        hexv = str(value.get('hex') or PALETTE.get(role, ('', '#??????'))[1]).upper()
        name = HEX_NAMES.get(hexv) or PALETTE.get(role, (role, hexv))[0]
        parts = _parts(value)
        meaning = value.get('meaning')
        text = '; '.join(parts) if parts else f'the {role} parts, exactly where Image 1 has this colour'
        if isinstance(meaning, str) and meaning.strip():
            text = f'{text} ({meaning.strip()})'
        lines.append(f'- {name} {hexv}: {text}.')
    return lines


def image_prompt(view: str, monster: str, colours: dict | None) -> str:
    """The image prompt of one extra view (`left`, `back` or `right`)."""
    if view not in CAMERA:
        raise ValueError(f'view must be one of {list(VIEWS)}')
    colour = ('This is a colour-coded model sheet, not final art. Use exactly the same flat marker colours as Image 1:\n'
              + '\n'.join(colour_lines(colours))
              + '\nA part that is new to this view takes the colour of the thing it belongs to.')
    text = '\n\n'.join([CAMERA[view], f'The creature is a {monster}. ' + SAME_DESIGN + ' ' + FRONT_ONLY[view] + ' ' + NO_PROPS,
                        colour, ONE_HUE, BACKGROUND])
    return ascii_text(text)


def record_name(view: str) -> str:
    """The record Codex writes beside the view: `<view>.prompts.json` (the Hobgoblin `2-back.prompts.json` field convention)."""
    return f'{view}.prompts.json'


RECORD_FIELDS = ('source_image', 'prompt', 'reference_paths', 'date', 'job_name', 'attempt_count', 'generated_file', 'attempts',
                 'image_processing', 'saved_pixel_size', 'png_mode', 'fully_transparent_pixel_share', 'source_pixel_size')


def job_prompt(view: str, monster: str, variant: str, front: Path, out: Path, colours: dict | None) -> str:
    """The whole Codex job prompt of one extra view: task, output path, the image prompt, the technical format check of its own file (no
    framing / pose / colour inspection, F17), the record, the scope limits and the report (monsters/aaa/CLAUDE.md "Launching a Codex image
    job")."""
    front, out = Path(front), Path(out)
    record = out.with_name(record_name(view))
    side = {'left': 'the creature\'s own LEFT side (about 90 degrees round)', 'back': 'behind (180 degrees round)',
            'right': 'the creature\'s own RIGHT side (about 90 degrees round)'}[view]
    fields = ', '.join(f'`{f}`' for f in RECORD_FIELDS)
    text = f"""# Monster extra view: {monster} variant {variant}, {view} view

Task: make ONE image, the {view} view of this colour-coded monster: the same creature as Image 1, seen from {side}. Follow this
folder's AGENTS.md and PROMPTING_GUIDE.md (papercraft style, whole creature in frame, no weapons or props, genuinely transparent
background, 8-bit RGBA PNG). Use the built-in image generation tool only (the imagegen skill); never the CLI/API fallback, never
another tool, and never resize, crop, pad or recolour the image.

Image 1 (attached): `{front.as_posix()}`, the picked conversion image of this variant (the front view). It is the design and colour
reference; draw the creature as it looks from the new camera.

Output: `{out.as_posix()}`. Copy the chosen generation from `~/.codex/generated_images/...` to exactly that path, bytes unchanged. Do not
overwrite an existing file; if the output already exists, stop and report.

## Image prompt (use it as written)

{image_prompt(view, monster, colours)}

## Technical check of your saved file

Check only the technical format of the saved PNG: it decodes as an 8-bit RGBA image and has real transparency (transparent pixels, not an
opaque background). Regenerate ONCE only if that format is wrong. Do NOT inspect or judge framing, pose, camera angle, colours or taste and
do not regenerate for them: one generation otherwise; report what you notice instead of fixing it. Do not ask for a pixel size.

## Record

Write `{record.as_posix()}` (JSON, UTF-8) with the fields of the Hobgoblin `pipeline/Hobgoblin/2-back.prompts.json` record: {fields}
(`source_image` = Image 1's path; `prompt` = the exact image prompt you used; `attempts` = one entry per generation with `attempt`,
`prompt`, `generated_file`, `saved_as`, and `reason_not_selected` or `review`; `image_processing` = "Byte-for-byte copy only; no
resizing, cropping, padding, recolouring or background extraction."), plus `view` = "{view}", `monster` = "{monster}" and
`variant` = "{variant}".

## Scope

No conversion or 3D service calls, no Blender, no git commands. Write only the two files named above, inside
`{out.parent.as_posix()}`; move, rename or delete nothing.

## Report

End with a short report: the files written, the PNG's pixel size, mode and transparent share, the number of attempts, and an honest
list of what did not follow the plan.
"""
    return ascii_text(text)
