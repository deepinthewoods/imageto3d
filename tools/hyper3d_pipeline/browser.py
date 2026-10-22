"""Prepare and watch Hyper3D Firefox tabs without uploading or generating.

This deliberately performs only navigation and DOM reads. A later, separately
authorized submission adapter can use the recorded slots and authenticated UI.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from playwright.async_api import async_playwright


WORKSPACE = "https://hyper3d.ai/workspace/rodin"
LOGIN = "https://hyper3d.ai/workspace/login"
DEFAULT_ROOT = Path.home() / ".cache" / "codex-firefox-playwright"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def collect_images(paths: list[str]) -> list[Path]:
    images: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            images.extend(sorted(item for item in path.iterdir()
                                 if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS))
        elif path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            images.append(path)
        else:
            raise ValueError(f"Expected a PNG, JPG, WebP, or image directory: {path}")
    return list(dict.fromkeys(images))


async def visit(page, url: str) -> dict:
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3000)
        return {"url": page.url, "http_status": response.status if response else None}
    except Exception as exc:
        return {"url": page.url, "navigation_error": str(exc).splitlines()[0]}


async def account_state(context) -> dict:
    """Read the sign-in form; never inspect or export browser credentials."""
    page = await context.new_page()
    navigation = await visit(page, LOGIN)
    try:
        email_inputs = await page.locator('input[placeholder="Your email address"]').count()
        password_inputs = await page.locator('input[type="password"]').count()
        state = "signed_out" if email_inputs and password_inputs else "unknown_or_signed_in"
    except Exception:
        email_inputs, password_inputs, state = None, None, "unknown"
    await page.close()
    return {"state": state, "sign_in_form_visible": bool(email_inputs and password_inputs),
            **navigation}


async def observe(page, slot: int, image_path: Path | None, auth_state: str) -> dict:
    """Collect bounded, read-only signals from one tab."""
    item = {"slot": slot, "image": str(image_path) if image_path else None,
            "url": page.url, "status": "workspace_open"}
    if page.is_closed():
        item["status"] = "tab_closed"
        return item
    try:
        item["title"] = await page.title()
        item["upload_inputs"] = await page.locator('input[type="file"][accept*=".png"]').count()
        buttons = [text.strip().lower() for text in await page.locator("button:visible").all_text_contents()]
        item["visible_actions"] = sorted({name for name in ("generate", "download", "export")
                                          if any(re.search(r"\b" + name + r"\b", text) for text in buttons)})
        item["progress_bars"] = await page.locator('[role="progressbar"]:visible').count()
        item["progress_values"] = await page.locator('[role="progressbar"]:visible').evaluate_all(
            "nodes => nodes.map(node => node.getAttribute('aria-valuenow')).filter(Boolean)")
        signals = await page.locator(
            '[role="status"], [aria-live], [class*="progress"], [class*="Progress"]'
        ).all_text_contents()
        item["stage_signals"] = list(dict.fromkeys(
            value.strip()[:100] for value in signals if value.strip()))[:12]
        if "/workspace/login" in page.url or await page.locator(
                'input[placeholder="Your email address"]').count():
            item["status"] = "login_page"
        elif auth_state == "signed_out":
            item["status"] = "login_required_before_conversion"
        elif item["progress_bars"]:
            item["status"] = "progress_visible"
        else:
            item["status"] = "workspace_open"
    except Exception as exc:
        item["status"] = "inspection_error"
        item["error"] = str(exc).splitlines()[0]
    return item


def save_snapshot(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


async def run(args) -> None:
    root = Path(args.runtime_root).expanduser().resolve()
    profile = root / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    lock = profile / "lock"
    if lock.is_symlink():
        match = re.search(r":\+?(\d+)$", os.readlink(lock))
        if match:
            command = Path(f"/proc/{match.group(1)}/cmdline")
            if command.exists() and str(profile).encode() in command.read_bytes().split(b"\0"):
                raise ValueError(
                    "The workflow Firefox profile is already open. Use that window to sign in, "
                    "then close it before running inspect or prepare. Your session will be saved.")
    async with async_playwright() as playwright:
        context = await playwright.firefox.launch_persistent_context(
            str(profile), headless=args.headless,
            viewport={"width": 1440, "height": 900})
        try:
            auth = await account_state(context)
            if args.command == "inspect":
                page = await context.new_page()
                navigation = await visit(page, WORKSPACE)
                snapshot = await observe(page, 1, None, auth["state"])
                print(json.dumps({"authentication": auth, "workspace": navigation,
                                  "browser": snapshot, "profile": str(profile)}, indent=2))
                return

            if args.command == "login":
                page = await context.new_page()
                await visit(page, LOGIN)
                print("Sign in in this separate Firefox window, then close the window or press Ctrl+C.", flush=True)
                while not page.is_closed():
                    await asyncio.sleep(2)
                return

            images = collect_images(args.inputs)
            batch = images[args.offset:args.offset + args.slots]
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            run_dir = root / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            pages = [await context.new_page() for _ in range(args.slots)]
            navigation = await asyncio.gather(*(visit(page, WORKSPACE) for page in pages))
            payload = {
                "run_id": run_id, "workspace": WORKSPACE,
                "authentication": auth, "profile": str(profile),
                "credit_spending_enabled": False, "uploads_performed": False,
                "input_count": len(images), "offset": args.offset,
                "remaining_after_batch": max(0, len(images) - args.offset - len(batch)),
                "slot_count": args.slots, "navigation": navigation,
            }
            print(f"Opened {args.slots} Firefox tabs. Account: {auth['state']}. "
                  f"Prepared {len(batch)} input paths. No images uploaded; no conversions submitted.",
                  flush=True)
            cycle = 0
            while True:
                cycle += 1
                observations = await asyncio.gather(*(
                    observe(page, number + 1, batch[number] if number < len(batch) else None,
                            auth["state"])
                    for number, page in enumerate(pages)))
                timestamp = datetime.now(timezone.utc).isoformat()
                snapshot = {"time_utc": timestamp, "slots": observations}
                payload["latest"] = snapshot
                save_snapshot(run_dir / "run.json", payload)
                with (run_dir / "history.jsonl").open("a") as history:
                    history.write(json.dumps(snapshot) + "\n")
                counts = {}
                for item in observations:
                    counts[item["status"]] = counts.get(item["status"], 0) + 1
                print(f"Cycle {cycle}: {counts}; log: {run_dir / 'run.json'}", flush=True)
                if args.cycles and cycle >= args.cycles:
                    break
                await asyncio.sleep(args.interval)
        finally:
            await context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", default=str(DEFAULT_ROOT),
                        help="Separate Firefox profile and run logs (default: %(default)s)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect", help="Read login and workspace state")
    inspect.set_defaults(headless=True)
    login = subparsers.add_parser("login", help="Open a separate Firefox window for manual sign-in")
    login.set_defaults(headless=False)
    prepare = subparsers.add_parser("prepare", help="Open and monitor tabs without uploading or generating")
    prepare.add_argument("inputs", nargs="*", help="Individual image files or folders of images")
    prepare.add_argument("--slots", type=int, default=10, choices=range(1, 21), metavar="1..20")
    prepare.add_argument("--offset", type=int, default=0, help="Start at this image in the input queue")
    prepare.add_argument("--interval", type=float, default=15, help="Seconds between read-only checks")
    prepare.add_argument("--cycles", type=int, default=0, help="Stop after N checks; 0 runs until Ctrl+C")
    prepare.add_argument("--headless", action="store_true", help="Run Firefox without visible windows")
    args = parser.parse_args()
    if args.command == "prepare" and (args.offset < 0 or args.interval < 1 or args.cycles < 0):
        parser.error("offset and cycles must be nonnegative; interval must be at least one second")
    if args.command in {"prepare", "login"} and not args.headless and not os.environ.get("DISPLAY"):
        parser.error("A visible Firefox window needs DISPLAY; use prepare --headless on this machine")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Stopped. The separate Firefox profile and run logs were kept.")
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
