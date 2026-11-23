"""Manual sign-in in normal Firefox, followed by read-only Selenium monitoring."""
import argparse
from datetime import datetime, timezone
import glob
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from browser import collect_images, save_snapshot, WORKSPACE, LOGIN
from platform_compat import WINDOWS

# HYPER3D_PROFILE points parallel conversion workers at their own clone of the signed-in profile
# Windows: deliberately outside %APPDATA%/%LOCALAPPDATA% so the Microsoft Store (MSIX) Firefox's file virtualisation
# cannot redirect the profile into its package directory.
PROFILE = Path(os.environ.get("HYPER3D_PROFILE") or (
    Path.home() / ".local/share/hyper3d-workflow-profile" if WINDOWS
    else Path.home() / "snap/firefox/common/hyper3d-workflow-profile"))
RUNTIME = Path(__file__).resolve().parent / "runs" / "native"


def _appx_firefox():
    """Install folder of the Microsoft Store Firefox. WindowsApps cannot be listed, so ask Appx (about 1 s)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-AppxPackage Mozilla.Firefox | Select-Object -First 1).InstallLocation"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out) / "VFS/ProgramFiles/Firefox Package Root/firefox.exe" if out else None


def find_firefox_windows():
    """firefox.exe to launch/automate: $HYPER3D_FIREFOX, a regular install, the Store (MSIX) package binary,
    the Store execution alias (last resort: the alias stub is not reliably drivable)."""
    candidates = [os.environ.get("HYPER3D_FIREFOX")]
    candidates += [Path(base) / "Mozilla Firefox/firefox.exe"
                   for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")) if base]
    candidates += glob.glob(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "WindowsApps",
                                         "Mozilla.Firefox_*", "VFS", "ProgramFiles", "Firefox Package Root", "firefox.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    appx = _appx_firefox()
    if appx and appx.is_file():
        return str(appx)
    return shutil.which("firefox")


if WINDOWS:
    FIREFOX = BINARY = find_firefox_windows()
    # None: Selenium Manager downloads/caches a matching geckodriver (~/.cache/selenium); $HYPER3D_GECKODRIVER pins one
    GECKODRIVER = os.environ.get("HYPER3D_GECKODRIVER") or shutil.which("geckodriver")
else:
    FIREFOX = "/snap/bin/firefox"
    GECKODRIVER = "/snap/bin/geckodriver"
    BINARY = "/snap/firefox/current/usr/lib/firefox/firefox"


def firefox_service(**kwargs):
    """Selenium geckodriver Service: the pinned GECKODRIVER, or on Windows Selenium Manager's cached download."""
    from selenium.webdriver.firefox.service import Service
    return Service(GECKODRIVER, **kwargs) if GECKODRIVER else Service(**kwargs)


def firefox_extra_args():
    """Extra Firefox arguments for automated launches (never the interactive `login`). Windows: -no-remote, so a
    launch can never hand over to (and open a window in) an already running Firefox that uses another profile, and
    -headless so no window pops up (verified: WebGL, hover/click and screenshots work, no 'Headless' in the user agent;
    there is no hidden X display on Windows). PIPELINE_SHOW_WINDOWS=1 shows the window, as on Linux."""
    if not WINDOWS:
        return []
    return ["-no-remote"] + ([] if os.environ.get("PIPELINE_SHOW_WINDOWS") == "1" else ["-headless"])


def _profile_in_use_windows(profile):
    """Firefox holds parent.lock in the profile directory while it runs (and deletes it on a clean exit)."""
    lock = profile / "parent.lock"
    if not lock.exists():
        return False
    try:
        with open(lock, "rb"):
            return False
    except PermissionError:      # sharing violation: another process has it open
        return True
    except OSError:
        return False


def check_profile(profile):
    profile.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        if _profile_in_use_windows(profile):
            raise ValueError("This workflow Firefox window is already open. Finish signing in and close it before inspect or prepare.")
        return
    lock = profile / "lock"
    if lock.is_symlink():
        match = re.search(r":\+?(\d+)$", os.readlink(lock))
        if match:
            command = Path(f"/proc/{match.group(1)}/cmdline")
            if command.exists() and str(profile).encode() in command.read_bytes().split(b"\0"):
                raise ValueError("This workflow Firefox window is already open. Finish signing in and close it before inspect or prepare.")


def visit(driver, url):
    try:
        driver.get(url)
        time.sleep(3)
        return {"url": driver.current_url}
    except Exception as exc:
        return {"url": driver.current_url, "navigation_error": str(exc).splitlines()[0]}


def observe(driver, slot, image):
    from selenium.webdriver.common.by import By
    actions = []
    for button in driver.find_elements(By.TAG_NAME, "button"):
        if button.is_displayed():
            text = button.text.strip().lower()
            actions.extend(name for name in ("generate", "download", "export")
                           if re.search(r"\b"+name+r"\b", text))
    progress = [p.get_attribute("aria-valuenow")
                for p in driver.find_elements(By.CSS_SELECTOR, '[role="progressbar"]')
                if p.is_displayed()]
    login_form = any(p.is_displayed() for p in driver.find_elements(
        By.CSS_SELECTOR, 'input[type="password"]'))
    return {"slot": slot, "image": str(image) if image else None,
            "url": driver.current_url, "title": driver.title,
            "status": "login_page" if login_form or "/workspace/login" in driver.current_url
                      else "progress_visible" if progress else "workspace_open",
            "visible_actions": sorted(set(actions)), "progress_values": progress,
            "upload_inputs": len(driver.find_elements(By.CSS_SELECTOR,'input[type="file"]'))}


def run(args):
    profile = Path(args.profile).expanduser().resolve()
    check_profile(profile)
    if args.command == "login":
        if not WINDOWS and not os.environ.get("DISPLAY"):
            raise ValueError("No desktop display found. Set DISPLAY to your local desktop display.")
        if not FIREFOX:
            raise ValueError("Firefox not found. Install it or set HYPER3D_FIREFOX to firefox.exe.")
        print("Opening normal Firefox for manual Hyper3D/Google sign-in. No automation is attached.", flush=True)
        print("After Hyper3D is signed in, close this Firefox window before inspect or prepare.", flush=True)
        if WINDOWS:   # no exec(): it would not wait for the window to close
            sys.exit(subprocess.call([FIREFOX, "-no-remote", "-profile", str(profile), LOGIN]))
        os.execv(FIREFOX, [FIREFOX, "--no-remote", "--profile", str(profile), LOGIN])

    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.common.by import By
    images = collect_images(args.inputs) if args.command == "prepare" else []
    batch = images[args.offset:args.offset+args.slots]
    options = Options()
    options.binary_location = BINARY
    options.add_argument("-profile")
    options.add_argument(str(profile))
    for extra in firefox_extra_args():
        options.add_argument(extra)
    if args.headless and "-headless" not in firefox_extra_args():
        options.add_argument("-headless")
    # Use the profile in place so its site session persists. Never copy the user's main profile.
    driver = webdriver.Firefox(service=firefox_service(), options=options)
    driver.set_page_load_timeout(45)
    try:
        auth_nav = visit(driver, LOGIN)
        form = any(p.is_displayed() for p in driver.find_elements(By.CSS_SELECTOR, 'input[type="password"]'))
        auth = {"state": "signed_out" if form else "unknown_or_signed_in",
                "sign_in_form_visible": form, **auth_nav}
        if args.command == "inspect":
            visit(driver, WORKSPACE)
            print(json.dumps({"authentication": auth, "browser": observe(driver, 1, None),
                              "profile": str(profile)}, indent=2))
            return
        handles = []
        for i in range(args.slots):
            if i:
                driver.switch_to.new_window("tab")
            visit(driver, WORKSPACE)
            handles.append(driver.current_window_handle)
        runtime = RUNTIME / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        runtime.mkdir(parents=True, exist_ok=True)
        payload = {"profile": str(profile), "authentication": auth, "slot_count": args.slots,
                   "input_count": len(images), "offset": args.offset,
                   "credit_spending_enabled": False, "uploads_performed": False,
                   "remaining_after_batch": max(0,len(images)-args.offset-len(batch))}
        print(f"Opened {len(handles)} tabs; queued {len(batch)} image paths. No uploads or conversions.", flush=True)
        cycle = 0
        while True:
            cycle += 1
            slots = []
            for i, handle in enumerate(handles):
                if handle not in driver.window_handles:
                    slots.append({"slot": i+1, "status": "tab_closed"})
                    continue
                driver.switch_to.window(handle)
                try:
                    slots.append(observe(driver, i+1, batch[i] if i<len(batch) else None))
                except Exception as exc:
                    slots.append({"slot": i+1, "status": "inspection_error",
                                  "error": str(exc).splitlines()[0]})
            snapshot = {"time_utc": datetime.now(timezone.utc).isoformat(), "slots": slots}
            payload["latest"] = snapshot
            save_snapshot(runtime/"run.json", payload)
            with (runtime/"history.jsonl").open("a") as f:
                f.write(json.dumps(snapshot)+"\n")
            print(f"Cycle {cycle}: {len(slots)} slots; {runtime/'run.json'}", flush=True)
            if (args.cycles and cycle>=args.cycles) or not driver.window_handles:
                break
            time.sleep(args.interval)
    finally:
        driver.quit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=str(PROFILE), help="Dedicated workflow profile")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="Manual sign-in in normal, unautomated Firefox")
    inspect = sub.add_parser("inspect", help="Read Hyper3D session state")
    inspect.set_defaults(headless=True, inputs=[], offset=0, slots=1)
    prepare = sub.add_parser("prepare", help="Open and monitor tabs without uploads or generation")
    prepare.add_argument("inputs", nargs="*")
    prepare.add_argument("--slots", type=int, default=10, choices=range(1,21))
    prepare.add_argument("--offset", type=int, default=0)
    prepare.add_argument("--cycles", type=int, default=0)
    prepare.add_argument("--interval", type=float, default=15)
    prepare.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    if args.command=="prepare" and (args.offset<0 or args.cycles<0 or args.interval<1):
        parser.error("offset/cycles must be nonnegative; interval must be at least one second")
    try:
        run(args)
    except ValueError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        print("Stopped; the workflow profile and logs were preserved.")


if __name__=="__main__":
    main()
