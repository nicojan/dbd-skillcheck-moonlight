"""Unit tests for the wide capture path — the Doctor/Madness off-centre coverage.

    .venv/bin/python tools/test_wide_capture.py

The real evidence for this module is `tools/replay_centre_crop.py --framing wide` against
the nine confirmed off-centre checks of `frames/session_20260829_154535`, which is what
says it works on actual pixels. These tests cover what that replay cannot:

  * the geometry at non-1080p scales, where the crop and the box are computed from two
    different constants and could drift apart silently;
  * the LOCK RULES in `look`, which are where this design's failure modes live — locking
    on a ring the model has not confirmed, and carrying angles across a crop that moved.
    Both were observed on real frames before they were rules, and neither raises anything;
  * the loop wiring in `autorun.run`. A helper that is correct and never called looks
    exactly like a match in which nothing was dropped — this project's signature failure.
"""

import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                   # tools/

from dbd.utils.needle_tracker import CENTRE_PRIOR
from dbd.utils.wide_capture import NONE_CLASS, centre_slice, look, wide_geometry

FAILURES = []


def check(label, condition, detail=None):
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}"
          + ("" if condition or detail is None else f"   [{detail}]"))
    if not condition:
        FAILURES.append(label)


def content(width=1920, height=1080, left=0, top=0):
    return {"left": left, "top": top, "width": width, "height": height}


# --- geometry ------------------------------------------------------------------------

def test_the_box_contains_the_production_crop_at_every_scale():
    """The centre slice must be the frame the 224 path would have grabbed, exactly.

    Everything downstream rests on this: if the slice is off by a pixel, every centred
    check is being classified against a framing the model was not trained on, and the
    change would look like a model regression rather than a geometry bug.
    """

    for height in (1080, 1440, 900, 720):
        rect = content(int(height * 16 / 9), height, left=320, top=17)
        geometry = wide_geometry(rect)
        crop, box = geometry.crop_region, geometry.region

        check(f"{height}p: the crop sits inside the box",
              box["left"] <= crop["left"]
              and box["top"] <= crop["top"]
              and crop["left"] + crop["width"] <= box["left"] + box["width"]
              and crop["top"] + crop["height"] <= box["top"] + box["height"],
              (box, crop))
        check(f"{height}p: and `centre` points at it",
              geometry.centre == (crop["left"] - box["left"], crop["top"] - box["top"]),
              geometry.centre)
        check(f"{height}p: the box stays inside the game content",
              box["left"] >= rect["left"] and box["top"] >= rect["top"]
              and box["left"] + box["width"] <= rect["left"] + rect["width"]
              and box["top"] + box["height"] <= rect["top"] + rect["height"],
              (rect, box))
        check(f"{height}p: both sides scale together",
              abs(geometry.side / geometry.crop_side - 672.0 / 224.0) < 0.05,
              (geometry.side, geometry.crop_side))


def test_the_centre_slice_is_the_centre_crop():
    """Same pixels, not merely the same size — checked against a marked wide frame."""

    geometry = wide_geometry(content())
    wide = np.zeros((geometry.side, geometry.side, 3), dtype=np.uint8)
    x, y = geometry.centre
    wide[y:y + geometry.crop_side, x:x + geometry.crop_side] = 200

    sliced = centre_slice(wide, geometry)
    check("the slice is crop-sized", sliced.shape[:2] == (geometry.crop_side,) * 2,
          sliced.shape)
    check("and it is the marked region", bool((sliced == 200).all()))


def test_a_box_that_would_run_off_the_screen_is_clamped_not_cropped():
    """A tiny content rect cannot hold the box; the grab must still be inside the game."""

    # A 16:9 box never overruns 16:9 content, so the clamp is defensive code. Force it
    # with an oversized box rather than pretending a realistic geometry triggers it.
    rect = content()
    geometry = wide_geometry(rect, box=1000.0)
    box = geometry.region
    check("the clamp is reported", geometry.clamped)
    check("and the box is still inside the content",
          box["left"] >= 0 and box["top"] >= 0
          and box["left"] + box["width"] <= rect["width"]
          and box["top"] + box["height"] <= rect["height"], box)


# --- the lock rules ------------------------------------------------------------------

class Recorder:
    """A scripted classifier. Returns a verdict per crop, keyed on what the crop contains.

    The pixels carry the answer, so a test can say "the centre is empty and there is a
    check at (400, 300)" without the fixture having to know which crop `look` will take.
    """

    def __init__(self, marks):
        self.marks = marks          # value in the crop -> (pred, desc, should_hit)
        self.calls = []

    def __call__(self, crop):
        seen = int(crop.max())
        pred, desc, hit = self.marks.get(seen, (NONE_CLASS, "None", False))
        self.calls.append((seen, desc))
        return pred, desc, {desc: 1.0}, hit


def ringed(geometry, at, value):
    """A wide frame with a check-sized ring drawn at `at`, filled with `value`."""

    import cv2

    wide = np.zeros((geometry.side, geometry.side, 3), dtype=np.uint8)
    cv2.circle(wide, at, 65, (value,) * 3, 3)
    return wide


def test_a_ring_alone_does_not_lock_the_crop():
    """The sweep fires on ~9% of quiet frames. Locking on a circle the model does not
    confirm would hand the tracker a window on background for the rest of the check."""

    geometry = wide_geometry(content())
    wide = ringed(geometry, (500, 470), 220)          # a ring nothing classifies as a check
    predict = Recorder({})                            # everything is None

    seen = look(predict, wide, geometry)
    check("the sweep was consulted", len(predict.calls) == 2, predict.calls)
    check("but nothing was locked", seen.held is None)
    check("and the verdict is the centre crop's", seen.origin == geometry.centre)


def test_a_confirmed_off_centre_check_locks_and_is_held():
    geometry = wide_geometry(content())
    wide = ringed(geometry, (500, 470), 220)
    predict = Recorder({220: (1, "repair-heal (out)", False)})

    seen = look(predict, wide, geometry)
    check("the off-centre crop is taken", seen.pred == 1, seen.desc)
    check("and locked", seen.held is not None)
    check("it cost the second inference", seen.inferences == 2, seen.inferences)
    check("the ring lands where a centred check puts it",
          seen.held is not None
          and abs(seen.prior[0] - CENTRE_PRIOR[0]) <= 4
          and abs(seen.prior[1] - CENTRE_PRIOR[1]) <= 4, None if seen.held is None else seen.prior)

    # Held: the next frame must reuse the window without sweeping again.
    held_predict = Recorder({220: (1, "repair-heal (out)", False)})
    again = look(held_predict, wide, geometry, seen.held)
    check("a held crop costs one inference", again.inferences == 1, again.inferences)
    check("and does not move", again.origin == seen.origin, (again.origin, seen.origin))


def test_the_centre_crop_wins_when_it_sees_anything():
    """Centre-first is the no-regression guarantee: the working path is never diverted."""

    geometry = wide_geometry(content())
    wide = ringed(geometry, (500, 470), 220)
    x, y = geometry.centre
    wide[y + 10:y + 20, x + 10:x + 20] = 150         # something in the centre crop
    predict = Recorder({150: (1, "repair-heal (out)", False),
                        220: (1, "repair-heal (out)", False)})

    seen = look(predict, wide, geometry)
    check("only the centre was classified", seen.inferences == 1, predict.calls)
    check("nothing was locked", seen.held is None)
    check("and the crop is the centre one", seen.origin == geometry.centre)


def test_the_prior_is_honest_when_the_crop_is_clamped():
    """Near the box edge the window cannot be centred on the ring. Saying it was — by
    reporting (112, 102) regardless — is the silent-rejection failure: the tracker would
    then refine +-3 px about a point the ring is nowhere near."""

    geometry = wide_geometry(content())
    wide = ringed(geometry, (70, 470), 220)           # too close to the left edge to centre
    predict = Recorder({220: (1, "repair-heal (out)", False)})

    seen = look(predict, wide, geometry)
    check("the check is still taken", seen.held is not None)
    check("and the prior says where the ring really is",
          seen.held is not None and abs(seen.prior[0] - 70) <= 4,
          None if seen.held is None else seen.prior)


# --- the loop wiring -----------------------------------------------------------------

def test_the_loop_restarts_the_track_when_the_crop_moves():
    """A displaced check is often visible at the edge of the centre crop for its first
    frames, so the centre path claims it and measures angles about a point the ring is not
    at. Carrying those samples across the switch held frame 18475's fit at 55 deg RMS for
    a whole check that was never pressed. The loop must start again instead."""

    import autorun

    printed = []
    saved = (autorun.AI_model, autorun.Monitoring_wide, autorun.FocusWatcher,
             autorun.sleep, autorun.log, autorun.observe)
    autorun.log = printed.append

    geometry = wide_geometry(content())
    # Frame 1-2: a check the CENTRE crop claims. Frame 3-4: the centre goes empty and the
    # sweep takes over, which moves the crop. Then quiet, then stop.
    centre_frame = np.zeros((geometry.side, geometry.side, 3), dtype=np.uint8)
    cx, cy = geometry.centre
    centre_frame[cy + 10:cy + 20, cx + 10:cx + 20] = 150
    off_frame = ringed(geometry, (500, 470), 220)
    quiet = np.zeros((geometry.side, geometry.side, 3), dtype=np.uint8)
    script = [centre_frame] * 2 + [off_frame] * 2 + [quiet] * 4

    class StubWatcher:
        last_frontmost = "Moonlight"
        def is_active(self):
            return True

    class StubWide:
        region = {"left": 0, "top": 0, "width": geometry.side, "height": geometry.side}
        def __init__(self):
            self.geometry = geometry
            self.calls = 0
        def describe(self):
            return "stub"
        def refresh(self):
            pass
        def grab_wide(self):
            if self.calls >= len(script):
                raise KeyboardInterrupt
            frame = script[self.calls]
            self.calls += 1
            return frame

    class StubModel:
        def __init__(self, **kw):
            pass
        def check_provider(self):
            return "stub"
        def predict(self, rgb):
            seen = int(rgb.max())
            if seen in (150, 220):
                return 1, "repair-heal (out)", {"repair-heal (out)": 1.0}, False
            return NONE_CLASS, "None", {"None": 1.0}, False
        def cleanup(self):
            pass

    starts = []
    real_observe = autorun.observe

    def watching_observe(state, frame, t_ms):
        starts.append(len(state.samples))
        return real_observe(state, frame, t_ms)

    autorun.AI_model = StubModel
    autorun.Monitoring_wide = lambda **kw: StubWide()
    autorun.FocusWatcher = lambda **kw: StubWatcher()
    autorun.sleep = lambda _s: None
    try:
        autorun.observe = watching_observe
        autorun.run(autorun.parse_args(["--dry-run", "--no-check-log",
                                        "--no-landing-log"]))
    finally:
        (autorun.AI_model, autorun.Monitoring_wide, autorun.FocusWatcher,
         autorun.sleep, autorun.log, autorun.observe) = saved

    check("all four check frames were tracked", len(starts) == 4, starts)
    # Samples before each observe: 0, 1 for the centre pair; then the crop moves, so the
    # off-centre pair must start from 0 again rather than continuing at 2, 3.
    check("the track restarted when the crop moved", starts == [0, 1, 0, 1], starts)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
            print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILING: " + ", ".join(FAILURES))
        sys.exit(1)
    print("all passing")


if __name__ == "__main__":
    main()
