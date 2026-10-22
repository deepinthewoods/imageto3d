# Outfit concepts and 3D conversion

The current workflow uses **Tripo Studio** with studio plain paper concept images. Generate front-view armor pairs or regular outfits in `concepts/`. The user moves chosen complete sets to `approved/`. When asked in chat, the assistant generates the other three views and stages verified four-view sets in `multiview_ready/`. Firefox Playwright submit/collect is available for manual conversions, with Triangle topology as the default; the unattended watcher and automatic reduction handoff are not yet connected.

Read [AGENTS.md](AGENTS.md) for asset rules, [PROMPTING_GUIDE.md](PROMPTING_GUIDE.md) for image prompts, and [tools/tripo_pipeline/README.md](tools/tripo_pipeline/README.md) for folder states, commands and current automation status. The selected source/reduction study notes are in `experiments/lowpoly_study_README.md`; the reduction library is in [tools/lowpoly/](tools/lowpoly/README.md).

The old Pixal3D queue is disabled; its code and existing models remain for recovery. The active `base_views/human_male/` now holds the selected red/yellow/magenta/cyan studio-plain marker views. The former kraft-paper views for all races are preserved intact in `archive/base_views_kraft_20261021/`; since 2026-10-22 all other races have matching active marker views in `base_views/<race>/`. Prior `completed/`, `models/`, `pending/` and `experiments/` artifacts remain preserved.

The active human-male marker base was natively rerendered at **1254 × 1254** with the same cameras and surface. The former 1024 base is archived at `archive/base_views_studio_plain_markers_1024_20261021/human_male/`; the already staged 1024 armor set uses its own frozen base snapshot. A separate 2048 experiment remains at `experiments/base_2k_studio_plain_20261021/`. New generated outfit images must natively match their chosen base; do not resize either side.

For a concise next-chat state and the pending 2K image-generation check, read [TRIPO_HANDOFF.md](TRIPO_HANDOFF.md).
