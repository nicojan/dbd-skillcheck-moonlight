"""Drive the tracker with a 21 s continuous Merciless Storm check, and time the freeze loop.

    .venv/bin/python tools/test_continuous_check.py

Two things had no evidence behind them after the offline replay and the dry-run, and this
covers both using footage already on disk:

  * **The frame and sample caps.** A discrete check lasts about a second and never reaches
    them. Merciless Storm is ONE check that runs 21 s with no reset and no freeze, and the
    tracker abstains on it, so nothing clears the buffer — which is precisely the case the
    caps exist for. `ingest_video.py` cuts the clip into per-revolution directories, so the
    ingested checks cannot test this; the source video has to be replayed unbroken.

  * **The wall-clock sampling loop in `report_landing`.** `freeze_angle` is unit-tested,
    but the 300 ms loop around it has never executed. Here it runs against a stub paced at
    a real frame interval.

A note on the stub, because an earlier version of this test fooled itself. Repeating frames
is only honest when the frames genuinely show a still needle: cycling six frames of a
frozen tail reproduces what the capture would really see, whereas clamping on the last
frame of a SWEEP invents a stillness that was never there. The first version did the
latter, and its sweeping case passed for that reason rather than on merit.
"""

import os
import sys
import time
from types import SimpleNamespace

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dbd.utils.needle_tracker import (
    MAX_FRAMES, MAX_SAMPLES, TrackerState, Zone, decide, find_zone, observe, refine_centre,
    static_image,
)
from replay_tracker import load_check

CLIP = "videos/merciless-storm.mp4"
FRAME0, FRAME1 = 25, 658        # the continuous check, from the clip's events.json
CROP_ORIGIN = (848, 438)        # ring centred; every revolution agrees to within a pixel
CROP = 224
FPS = 30.0

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def continuous_frames():
    """The whole Merciless Storm check as 224 crops, rescaled to 1080p as ingest does."""

    cap = cv2.VideoCapture(CLIP)
    if not cap.isOpened():
        return []
    frames = []
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if FRAME0 <= i < FRAME1:
            h, w = frame.shape[:2]
            if h != 1080:
                frame = cv2.resize(frame, (round(w * 1080 / h), 1080),
                                   interpolation=cv2.INTER_CUBIC)
            x, y = CROP_ORIGIN
            frames.append(frame[y:y + CROP, x:x + CROP].copy())
        i += 1
        if i >= FRAME1:
            break
    cap.release()
    return frames


class PacedMonitor:
    """Hands back frames at a real frame interval, as RGB, cycling rather than running out.

    The pacing is the point: `report_landing` samples against a wall clock, so a stub that
    returns instantly compresses its whole 300 ms window into microseconds and tests
    nothing about the loop.
    """

    def __init__(self, images, fps=FPS):
        self.images = images
        self.interval = 1.0 / fps
        self.grabs = 0

    def grab_screenshot(self):
        if self.grabs:
            time.sleep(self.interval)
        img = self.images[self.grabs % len(self.images)]
        self.grabs += 1
        return img[:, :, ::-1]  # BGR on disk -> RGB, as the capture path delivers it


def test_a_21_second_check_stays_within_its_caps():
    frames = continuous_frames()
    check(f"decoded the continuous check ({len(frames)} frames)", len(frames) > 500,
          f"got {len(frames)}")
    if len(frames) < 100:
        return

    state = TrackerState()
    scheduled = []
    peak_bytes = 0
    for i, frame in enumerate(frames):
        state = observe(state, frame, i / FPS * 1000.0)
        decision = decide(state, i / FPS * 1000.0)
        if decision.press_at_ms is not None:
            scheduled.append((i, decision.reason, decision.target_deg))
        peak_bytes = max(peak_bytes, sum(f.nbytes for f in state.frames))

    duration = len(frames) / FPS
    check(f"frames stay capped over {duration:.1f} s",
          len(state.frames) <= MAX_FRAMES, f"held {len(state.frames)}")
    check("samples stay capped", len(state.samples) <= MAX_SAMPLES,
          f"held {len(state.samples)}")
    check("seen counts every frame regardless of the caps",
          state.seen == len(frames), f"seen {state.seen} of {len(frames)}")
    check("retained frame memory stays bounded", peak_bytes <= MAX_FRAMES * CROP * CROP * 3,
          f"peaked at {peak_bytes / 1e6:.1f} MB")
    print(f"        {len(frames)} frames / {duration:.1f} s -> "
          f"{len(state.frames)} frames retained, {peak_bytes / 1e6:.2f} MB peak")

    # And the abstention has to hold for the whole check, not just its first revolution.
    check("never schedules a press on Merciless Storm", not scheduled,
          f"{len(scheduled)} presses scheduled, first at frame {scheduled[0] if scheduled else '-'}")


def test_the_freeze_loop_refuses_a_needle_that_never_stops():
    frames = continuous_frames()
    if len(frames) < 100:
        return

    import autorun
    lines = []
    autorun.log = lines.append

    static = static_image(frames[:16])
    cx, cy, ring_r, _ = refine_centre(static)
    # Merciless Storm draws no solid band, so find_zone returns None — which would make
    # report_landing bail before reaching the loop this test is about. Supply a zone so
    # the question under test is whether the loop calls a MOVING needle frozen, not what
    # it would have scored.
    zone = find_zone(static, (cx, cy), ring_r) or Zone(10.0, 20.0, 10.0, 60.0)

    monitor = PacedMonitor(frames[100:140])
    state = TrackerState(centre=(cx, cy), zone=zone)
    autorun.report_landing(monitor, state, 0.0,
                           SimpleNamespace(dry_run=False, round_trip_ms=130.0),
                           time.monotonic())

    check("the loop sampled several frames in its window", monitor.grabs >= 5,
          f"only {monitor.grabs} grabs")
    check("a needle that never stops gets no verdict",
          not any("landed" in l for l in lines), lines)
    check("and the reason says so", any("still sweeping" in l for l in lines), lines)
    print(f"        {monitor.grabs} grabs in {autorun.FREEZE_WATCH_SECONDS}s — "
          f"{lines[-1].strip() if lines else '(silent)'}")


def test_the_freeze_loop_reports_a_real_freeze():
    records, images = load_check("recordings/check_001")  # a hit; the tail is truly frozen
    static = static_image(images)
    cx, cy, ring_r, _ = refine_centre(static)
    zone = find_zone(static, (cx, cy), ring_r)

    import autorun
    lines = []
    autorun.log = lines.append

    # Cycling six frames of a genuinely frozen needle reproduces what the capture sees.
    monitor = PacedMonitor(images[-6:])
    state = TrackerState(centre=(cx, cy), zone=zone)
    autorun.report_landing(monitor, state, 0.0,
                           SimpleNamespace(dry_run=False, round_trip_ms=130.0),
                           time.monotonic())

    check("a real freeze produces a verdict", any("landed" in l for l in lines), lines)
    # The round trip is derived from the fit the press was scheduled from, so with no fit
    # there is nothing to derive it from and none is claimed. Silence is the correct
    # behaviour here: a stub cycling pre-frozen frames has no sweep to measure against, and
    # printing a latency for it would be inventing one.
    check("but no round trip is invented without a fit",
          not any("round trip" in l for l in lines), lines)
    print(f"        {monitor.grabs} grabs — {lines[-1].strip() if lines else '(silent)'}")


def main():
    if not os.path.exists(CLIP):
        raise SystemExit(f"{CLIP} not found — videos/ is gitignored local capture data")

    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            print(f"\n{name}")
            fn()

    print()
    if FAILURES:
        raise SystemExit(f"{len(FAILURES)} failing: {', '.join(FAILURES)}")
    print("all passing")


if __name__ == "__main__":
    main()
