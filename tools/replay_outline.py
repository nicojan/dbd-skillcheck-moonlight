"""Score the tracker's press on a RELOCATING OUTLINE check, against the arc drawn at the
moment the key lands.

    .venv/bin/python tools/replay_outline.py recordings_1234
    .venv/bin/python tools/replay_outline.py recordings_1234 --round-trip-ms 46

`tools/replay_tracker.py` cannot answer this one. It scores against a zone read from the
static median of the whole check, and on this check there is no such thing: the arc
relocates four to five times in 2.9 s, so a median over the check is a median over four
different positions. Its verdict on `recordings_1234` is UNSCORED, correctly.

So the ground truth here is per-frame, and split in two because the needle masks what it
stands on. WHERE the key lands is scored against the arc as the tracker acquired it, which
is the last clean read of the leading edge. WHETHER the arc was still there is asked of the
pixels in the frame nearest the landing, probing 20 deg further along the sweep so the
needle's own glow is not mistaken for the arc having gone.

Getting that split wrong is not a hypothetical. Scoring the landing against a re-read at
press time called all four presses OUTSIDE by ~6 deg, purely because the needle had eaten
that much of the leading edge; requiring `find_outline_arc` to pass its width gate at the
landing frame called them "arc moved", because a masked 109 deg arc reads 87 and the gate
throws the frame away. Both are the same mistake `NOTES-local.md` records against the first
wiggle measurement: measuring the instrument instead of the thing.
"""

import argparse
import glob
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.utils.needle_tracker import (
    ANGLE_STEP, HOT, OUTLINE_AIM_DEG, RADIUS_STEP, ROUND_TRIP_MS, Sample, WINDOW_IN,
    WINDOW_OUT, ZONE_THICK_PX, _close_gaps, find_outline_arc, leading_edge, lit_span,
    needle_angle, refine_centre, sample_rays, static_image, trim_frozen_tail,
)
from dbd.utils.needle_tracker import TrackerState, decide, observe
from tools.replay_tracker import load_check


def replay_keeping_state(records, images, round_trip_ms):
    """`replay_tracker.replay`, but returning the tracker STATE at the decision too.

    The zone has to come from the tracker, not from a fresh read at press time. By then
    the needle is sitting on the arc's leading edge and masking 6-12 deg of it, so a
    re-read answers a different question than the one the tracker acted on.
    """

    state = TrackerState()
    for i, (rec, img) in enumerate(zip(records, images)):
        now = rec["t_ms"]
        state = observe(state, img, now)
        decision = decide(state, now, round_trip_ms)
        if decision.press_at_ms is None:
            continue
        next_t = records[i + 1]["t_ms"] if i + 1 < len(records) else float("inf")
        if decision.press_at_ms <= next_t:
            return decision, state
    return None, state


def parse_args():
    p = argparse.ArgumentParser(description="Score outline-check presses per frame")
    p.add_argument("recordings", nargs="?", default="recordings_1234")
    p.add_argument("--round-trip-ms", type=float, default=ROUND_TRIP_MS,
                   help="latency the tracker BELIEVES it has to lead by")
    p.add_argument("--true-round-trip-ms", type=float, default=None,
                   help="latency actually applied when scoring. Differ from the above to "
                        "ask how wrong the lead can be before the press leaves the arc — "
                        "the link ran 21-105 ms on 2026-09-11")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def truth_fit(records, images, centre):
    """Needle rate over the check, with the frozen tail that follows it removed.

    Untrimmed this reads 292 deg/s at 13 deg RMS and trimmed 300 at 1.8, because the check
    ends ~300 ms before the recording does and a stopped needle is not a slow one. The
    untrimmed number is also what the live log reported as `fit too poor`, which is why
    that line was never evidence about the needle.
    """

    rows = [(r["t_ms"],) + needle_angle(img, centre) for r, img in zip(records, images)]
    samples = tuple(Sample(t, a, s) for t, a, s in rows)
    start, stop = lit_span(samples)
    kept = trim_frozen_tail(samples[start:stop])
    if len(kept) < 5:
        return None, None
    t = np.array([s.t_ms for s in kept])
    ang = np.rad2deg(np.unwrap(np.deg2rad([s.angle for s in kept])))
    slope, intercept = np.polyfit(t, ang, 1)
    rms = float(np.sqrt(np.mean((ang - (slope * t + intercept)) ** 2)))
    return (float(slope * 1000.0), float(intercept)), rms


def arc_at(records, images, centre, ring_r, t_ms):
    """The arc drawn in the recorded frame nearest `t_ms`, and how far that frame is."""

    i = int(np.argmin([abs(r["t_ms"] - t_ms) for r in records]))
    return (find_outline_arc(static_image([images[i]]), centre, ring_r),
            abs(records[i]["t_ms"] - t_ms))


def run_containing(records, images, centre, ring_r, t_ms, angle_deg):
    """(start, end) of the drawn run covering `angle_deg`, in the frame nearest `t_ms`.

    Deliberately NOT `find_outline_arc`: that applies OUTLINE_MIN_DEG, and at the moment
    the key lands the needle is standing on the arc and masking 6-12 deg of it, so a
    109 deg arc reads 87 and the width gate throws away the very frame being asked about.
    Asking "is the arc still drawn where the key landed" has to be asked of the pixels.
    """

    i = int(np.argmin([abs(r["t_ms"] - t_ms) for r in records]))
    radii = np.arange(ring_r + WINDOW_IN, ring_r + WINDOW_OUT, RADIUS_STEP)
    angles, polar = sample_rays(static_image([images[i]]), centre[0], centre[1], radii)
    hot = (polar - np.median(polar, axis=0, keepdims=True)) > HOT
    drawn = _close_gaps(hot.sum(axis=1) * RADIUS_STEP >= ZONE_THICK_PX, ANGLE_STEP)

    n = len(drawn)
    k = int(round(angle_deg / ANGLE_STEP)) % n
    if not drawn[k]:
        return None
    lo = k
    while drawn[(lo - 1) % n] and (k - lo) % n < n - 1:
        lo -= 1
    hi = k
    while drawn[(hi + 1) % n] and (hi - k) % n < n - 1:
        hi += 1
    return float(angles[lo % n]), float(angles[(hi + 1) % n])


# How far the needle's own red mask eats into the arc it is standing on. `static_image`
# blanks anything redder than NEEDLE_REDNESS, and the needle is drawn with a glow, so the
# arc's LEADING edge reads 6-12 deg late once the needle is on it. Measured, not assumed:
# check_001's arc reads 130 while the needle approaches and 148 once the needle sits at
# 142. The TRAILING edge is untouched — the needle is nowhere near it — which is why the
# arc is identified by its trailing edge here and scored against the leading edge read
# when the tracker acquired it, before the needle got there.
TRAILING_TOL_DEG = 8.0


def main():
    args = parse_args()
    dirs = sorted(glob.glob(os.path.join(args.recordings, "check_*")))
    rows = []

    print(f"{'check':12} {'deg/s':>6} {'rms':>5} {'arc at landing':>15} {'lands':>7} "
          f"{'past edge':>10} {'margin to far end':>18}  verdict")
    print("-" * 96)

    for d in dirs:
        records, images = load_check(d)
        if len(records) < 10:
            continue
        centre_xy = refine_centre(static_image(images))
        centre, ring_r = (centre_xy[0], centre_xy[1]), centre_xy[2]

        decision, state = replay_keeping_state(records, images, args.round_trip_ms)
        fit, rms = truth_fit(records, images, centre)
        name = os.path.basename(d)

        if decision is None or decision.press_at_ms is None:
            print(f"{name:12} {'':>6} {'':>5} {'':>15} {'':>7} {'':>10} {'':>18}  NO FIRE")
            rows.append("NO FIRE")
            continue
        if fit is None:
            print(f"{name:12}  no usable needle fit")
            rows.append("UNSCORED")
            continue

        direction = 1.0 if fit[0] > 0 else -1.0
        true_trip = (args.true_round_trip_ms if args.true_round_trip_ms
                     is not None else args.round_trip_ms)
        lands_ms = decision.press_at_ms + true_trip
        lands = (fit[0] * lands_ms / 1000.0 + fit[1]) % 360.0

        # The arc the tracker actually aimed at: read when it ACQUIRED the arc, before
        # the needle got near enough to mask the leading edge.
        cue = state.zone
        gap_ms = min(abs(r["t_ms"] - lands_ms) for r in records)

        if cue is None:
            print(f"{name:12} {fit[0]:6.1f} {rms:5.2f}  no arc readable at the cue frame")
            rows.append("UNSCORED")
            continue

        edge = leading_edge(cue, fit[0])
        past = ((lands - edge) * direction) % 360.0

        # Still the same arc where the key landed? Probed 20 deg further along the sweep,
        # clear of the needle's own mask, and identified by the trailing edge — the end
        # the needle has not reached and so cannot have eaten into.
        run = run_containing(records, images, centre, ring_r, lands_ms,
                             (lands + 20.0 * direction) % 360.0)
        trailing = None if run is None else (run[1] if direction > 0 else run[0])
        same = (trailing is not None
                and abs((trailing - cue.zone_end + 180.0) % 360.0 - 180.0)
                <= TRAILING_TOL_DEG)
        past = past - 360.0 if past > 180.0 else past     # signed: negative is short of it
        width = cue.zone_width
        inside = same and 0.0 <= past <= width
        verdict = "INSIDE" if inside else ("OUTSIDE (arc moved)" if not same else "OUTSIDE")
        rows.append("INSIDE" if inside else "OUTSIDE")
        print(f"{name:12} {fit[0]:6.1f} {rms:5.2f} {cue.zone_start:6.0f}-{cue.zone_end:<3.0f}"
              f"{f'({width:.0f})':>5} {lands:7.1f} {past:9.1f} deg "
              f"{width - past:15.1f} deg  {verdict}"
              + (f"   [frame {gap_ms:.0f} ms off]" if gap_ms > 30 else ""))

    inside = rows.count("INSIDE")
    print(f"\n{len(rows)} outline checks: {inside} landed inside the drawn arc, "
          f"{rows.count('OUTSIDE')} outside, {rows.count('NO FIRE')} no fire, "
          f"{rows.count('UNSCORED')} unscored")
    print(f"the press is the soonest landing at least {OUTLINE_AIM_DEG:.0f} deg inside "
          f"the arc; the arc itself expires on a ~679 ms timer of its own")
    return 0 if inside == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
