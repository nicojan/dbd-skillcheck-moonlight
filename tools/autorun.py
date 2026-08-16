"""Run the detector only while the stream is focused; pause automatically when it is not.

    python tools/autorun.py --dry-run     # detect and log, never press a key
    python tools/autorun.py               # armed
    python tools/autorun.py --no-predict  # upstream's reactive behaviour only

Sweeping checks are fired PREDICTIVELY by default. Reacting to a Great classification
cannot land one here: the Great band is 10.5 deg, so it lasts 26-37 ms, against a 72 ms
keypress-to-pixel round trip through Moonlight. The needle has left before the key
arrives, which is why repair and heal checks used to land in Good every time and why
`--hit-ante` could not fix it. `dbd/utils/needle_tracker.py` fits the sweep and schedules
the press to LAND in Great; see its docstring and NOTES-local.md.

Wiggle keeps the reactive path — it oscillates rather than sweeping, so a linear fit is
wrong by construction, and it is the one check type that already worked.

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
from dbd.utils.needle_tracker import (
    MIN_NEEDLE_STRENGTH, ROUND_TRIP_MS, TrackerState, decide, freeze_angle, mark_fired,
    needle_angle, observe, score_freeze,
)

IDLE_POLL_SECONDS = 0.20
HIT_COOLDOWN_SECONDS = 0.5
ANTE_FRONTIER_PRED = 2

# Every non-wiggle class that means a check is on screen. `full black (out)` is included
# because Merciless Storm classifies as that, and because the model labels the frames
# either side of a real check with it too — the tracker's own gates (a drawn Great band,
# a plausible rate, a clean fit) are what reject those, not the class.
TRACKED_PREDS = (1, 2, 3, 4, 5, 6, 7)
WIGGLE_PREDS = (8, 9, 10)

DEFAULT_FRAME_MS = 30.0   # seed for the frame-period estimate, before any is measured
FRAME_MS_DECAY = 0.8      # EMA weight on the previous estimate
TRACK_DROP_FRAMES = 3     # consecutive check-free frames that end a track
FREEZE_WATCH_SECONDS = 0.30   # how long to watch for the needle to freeze after a press

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
    p.add_argument("--no-predict", dest="predict", action="store_false",
                   help="disable predictive firing; react to the classifier as upstream does")
    p.add_argument("--round-trip-ms", type=float, default=ROUND_TRIP_MS,
                   help="measured keypress->pixel latency the prediction leads by")
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


def fire(args, wait_seconds):
    """Sleep out the remaining lead, then tap SPACE. The sleep is the whole point."""

    if wait_seconds > 0:
        sleep(wait_seconds)
    if args.dry_run:
        return
    PressKey(SPACE)
    sleep(0.005)
    ReleaseKey(SPACE)


def report_landing(model, tracker, track_t0, args):
    """Read where the press actually landed, from the angle the needle freezes at.

    A successful hit stops the needle dead at the hit position, so the frozen angle is the
    landing — measured in the same frames, about the same centre, against the same drawn
    zone the press was aimed at. Without this the bot's accuracy could only be inferred
    from a Great/Good tally at the end of a match, which is how a 3x measurement error
    went unnoticed here before.
    """

    if args.dry_run or tracker.zone is None:
        return

    deadline = monotonic() + FREEZE_WATCH_SECONDS
    angles = []
    while monotonic() < deadline:
        bgr = model.grab_screenshot()[:, :, ::-1]
        angle, strength = needle_angle(bgr, tracker.centre)
        if strength >= MIN_NEEDLE_STRENGTH:
            angles.append(angle)

    if len(angles) < 3:
        log("  landing: needle gone before it could be read")
        return

    settled = freeze_angle(angles)
    if settled is None:
        log("  landing: needle still sweeping after the press — it did not connect, "
            "or this is Merciless Storm, which never stops")
        return

    verdict, err = score_freeze(tracker.zone, settled)
    log(f"  landed {settled:.1f} deg — {verdict}"
        + (f", {err:+.1f} deg from Great centre" if err is not None else ""))


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
    log(f"firing: {'predictive' if args.predict else 'reactive only'}"
        + (f", leading by {args.round_trip_ms:.0f} ms" if args.predict else ""))
    log(f"waiting for '{args.window}' to become active (ctrl-c to quit)")

    active = False
    frames = 0
    hits = 0
    window_start = monotonic()
    seen_frontmost = None

    tracker = None          # TrackerState for the check in progress
    track_t0 = None         # monotonic origin for that check's timestamps
    dropped = 0             # consecutive check-free frames since the last tracked one
    last_capture = None
    frame_ms = DEFAULT_FRAME_MS

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
                tracker, last_capture = None, None  # the check is long gone by the time
                                                    # focus comes back

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
            captured = monotonic()
            frame = model.grab_screenshot()
            frames += 1
            pred, desc, probs, should_hit = model.predict(frame)

            if last_capture is not None:
                dt = (captured - last_capture) * 1000.0
                if dt < 200.0:  # ignore the gap across a pause or a cooldown
                    frame_ms = FRAME_MS_DECAY * frame_ms + (1 - FRAME_MS_DECAY) * dt
            last_capture = captured

            # --- predictive path: sweeping checks ----------------------------------
            if args.predict and pred in TRACKED_PREDS:
                if tracker is None:
                    tracker, track_t0 = TrackerState(), captured
                dropped = 0

                # grab_screenshot returns RGB; the needle test is R - max(G,B) on a BGR
                # array, so an unconverted frame silently measures blueness instead.
                tracker = observe(tracker, frame[:, :, ::-1], (captured - track_t0) * 1000.0)
                decision = decide(tracker, (monotonic() - track_t0) * 1000.0,
                                  args.round_trip_ms)

                if decision.press_at_ms is not None:
                    now_ms = (monotonic() - track_t0) * 1000.0
                    # Hold only while another frame would land in time to sharpen the fit.
                    # The margin is on the safe side of the frame-period estimate: waiting
                    # for a frame that arrives late costs the whole check, whereas firing
                    # a frame early costs a few frames of fit quality.
                    if decision.press_at_ms - now_ms > frame_ms * 1.25:
                        continue

                    hits += 1
                    tracker = mark_fired(tracker, now_ms)
                    fire(args, decision.press_at_ms - now_ms)
                    fit = decision.fit
                    log(f"{'WOULD FIRE' if args.dry_run else 'FIRE'} predictive: {desc} — "
                        f"{fit.rate_deg_s:+.0f} deg/s, fit {fit.rms_deg:.1f} deg RMS over "
                        f"{fit.n} frames, aiming {decision.target_deg:.1f} deg")
                    report_landing(model, tracker, track_t0, args)
                    tracker = None
                    sleep(HIT_COOLDOWN_SECONDS)
                    window_start, last_capture = monotonic(), None
                    frames = 0

                # A fitted check with nowhere to aim (no Great band drawn, or the band
                # already passed) is exactly the reactive case: pressing on the model's
                # cue lands in Good, which beats not pressing at all.
                elif should_hit and decision.may_react:
                    hits += 1
                    fire(args, 0.0)
                    log(f"{'WOULD HIT' if args.dry_run else 'HIT'} reactive: {desc} — "
                        f"tracker stood down ({decision.reason})")
                    tracker = None
                    sleep(HIT_COOLDOWN_SECONDS)
                    window_start, last_capture = monotonic(), None
                    frames = 0
                continue

            if pred in WIGGLE_PREDS:
                tracker = None  # wiggle oscillates; a linear fit is wrong by construction
            elif tracker is not None:
                dropped += 1
                if dropped >= TRACK_DROP_FRAMES:
                    tracker = None

            # --- reactive path: wiggle, and everything when --no-predict ------------
            if should_hit:
                if pred == ANTE_FRONTIER_PRED and args.hit_ante > 0:
                    sleep(args.hit_ante * 0.001)

                hits += 1
                confidence = float(max(probs.values()))
                log(f"{'WOULD HIT' if args.dry_run else 'HIT'}: {desc} ({confidence:.3f})")
                fire(args, 0.0)

                sleep(HIT_COOLDOWN_SECONDS)  # don't re-trigger on the same skill check
                window_start, last_capture = monotonic(), None
                frames = 0

    except KeyboardInterrupt:
        log("stopping")
    finally:
        if not args.dry_run:
            ReleaseKey(SPACE)  # belt and braces: never exit with the key held
        model.cleanup()


if __name__ == "__main__":
    run(parse_args())
