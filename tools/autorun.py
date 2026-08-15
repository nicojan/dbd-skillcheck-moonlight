"""Run the detector only while the stream is focused; pause automatically when it is not.

    python tools/autorun.py --dry-run     # detect and log, never press a key
    python tools/autorun.py               # armed

Why the gate matters beyond convenience: synthetic keystrokes go to the *focused*
application. An ungated loop that is still running after you switch away will type
SPACE into whatever you switched to. Pausing on focus loss is the correctness fix, and
resuming on focus gain is what makes it usable.

State transitions are logged with timestamps so you can confirm the pause/resume edges
are firing where you expect.
"""

import argparse
import os
import sys
from time import monotonic, sleep, strftime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.AI_model import AI_model
from dbd.utils.directkeys import PressKey, ReleaseKey, SPACE
from dbd.utils.focus_watcher import FocusWatcher
from dbd.utils.monitoring_window import Monitoring_window, WindowNotFoundError

IDLE_POLL_SECONDS = 0.20
HIT_COOLDOWN_SECONDS = 0.5
ANTE_FRONTIER_PRED = 2

# A Space transition takes time to settle, and Moonlight registers ~16 windows while
# it does. Resolving geometry mid-transition picked a menu-bar-inset window once
# (218px crop instead of 224), so wait for the dust to settle before re-resolving.
RESUME_SETTLE_SECONDS = 0.75


def parse_args():
    p = argparse.ArgumentParser(description="Focus-gated skill check detector")
    p.add_argument("--window", default="Moonlight", help="substring of the window owner/title")
    p.add_argument("--model", default="models/model.onnx")
    p.add_argument("--aspect", type=str, default="16:9", help="stream aspect ratio, or 'fill'")
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--hit-ante", type=int, default=0, help="ms delay on ante-frontier hits")
    p.add_argument("--dry-run", action="store_true", help="log detections without pressing keys")
    p.add_argument("--no-require-on-screen", action="store_true",
                   help="gate on focus alone, skipping the window-list confirmation")
    p.add_argument("--pin-geometry", action="store_true",
                   help="lock the startup capture region instead of re-resolving on resume")
    return p.parse_args()


def parse_aspect(text):
    if text.lower() in ("fill", "none"):
        return None
    if ":" in text:
        w, h = text.split(":")
        return float(w) / float(h)
    return float(text)


def log(message):
    print(f"[{strftime('%H:%M:%S')}] {message}", flush=True)


def run(args):
    monitoring = Monitoring_window(
        window_query=args.window,
        crop_size=224,
        stream_aspect=parse_aspect(args.aspect),
    )
    log(f"window: {monitoring.describe()}")

    watcher = FocusWatcher(
        query=args.window,
        require_on_screen=not args.no_require_on_screen,
    )

    model = AI_model(
        model_path=args.model,
        use_gpu=False,
        nb_cpu_threads=args.threads,
        monitoring=monitoring,
    )
    log(f"provider: {model.check_provider()}   dry_run: {args.dry_run}")
    log(f"waiting for '{args.window}' to become active (ctrl-c to quit)")

    active = False
    frames = 0
    hits = 0
    window_start = monotonic()
    seen_frontmost = None

    try:
        while True:
            now_active = watcher.is_active()

            # Log focus changes using the cached answer from the check above. Without
            # this, a run with no PAUSED/RESUMED lines is indistinguishable from a run
            # where the gate silently stopped tracking.
            current_frontmost = watcher.last_frontmost
            if current_frontmost != seen_frontmost:
                log(f"  focus: {seen_frontmost!r} -> {current_frontmost!r}")
                seen_frontmost = current_frontmost

            # --- edge: active -> paused -------------------------------------------
            if active and not now_active:
                # Never leave SPACE held down in whatever app just took focus.
                if not args.dry_run:
                    ReleaseKey(SPACE)
                elapsed = monotonic() - window_start
                fps = frames / elapsed if elapsed > 0 else 0
                log(f"PAUSED  (focus lost) — {frames} frames, {hits} hits, {fps:.0f} fps")
                active = False

            # --- edge: paused -> active -------------------------------------------
            elif not active and now_active:
                sleep(RESUME_SETTLE_SECONDS)  # let the Space transition finish
                previous_region = dict(monitoring.region)

                if args.pin_geometry:
                    log(f"RESUMED (focus gained) — pinned to {monitoring.region}")
                    active = True
                    frames = 0
                    hits = 0
                    window_start = monotonic()
                    continue

                try:
                    # The window may have moved or resized while we were away.
                    monitoring.refresh()
                except WindowNotFoundError as e:
                    log(f"active but window lookup failed: {e}")
                    sleep(IDLE_POLL_SECONDS)
                    continue

                # Silent geometry drift is the dangerous failure: the model keeps
                # running against a mis-scaled crop and simply stops detecting.
                if dict(monitoring.region) != previous_region:
                    log(f"WARNING geometry changed: {previous_region} -> {monitoring.region}")
                    log("  if detection degrades, re-run with --pin-geometry")

                log(f"RESUMED (focus gained) — capturing {monitoring.region}")
                active = True
                frames = 0
                hits = 0
                window_start = monotonic()

            if not active:
                sleep(IDLE_POLL_SECONDS)
                continue

            # --- armed loop --------------------------------------------------------
            frame = model.grab_screenshot()
            frames += 1
            pred, desc, probs, should_hit = model.predict(frame)

            if should_hit:
                if pred == ANTE_FRONTIER_PRED and args.hit_ante > 0:
                    sleep(args.hit_ante * 0.001)

                hits += 1
                confidence = float(max(probs.values()))
                log(f"{'WOULD HIT' if args.dry_run else 'HIT'}: {desc} ({confidence:.3f})")

                if not args.dry_run:
                    PressKey(SPACE)
                    sleep(0.005)
                    ReleaseKey(SPACE)

                sleep(HIT_COOLDOWN_SECONDS)  # don't re-trigger on the same skill check
                window_start = monotonic()
                frames = 0

    except KeyboardInterrupt:
        log("stopping")
    finally:
        if not args.dry_run:
            ReleaseKey(SPACE)  # belt and braces: never exit with the key held
        model.cleanup()


if __name__ == "__main__":
    run(parse_args())
