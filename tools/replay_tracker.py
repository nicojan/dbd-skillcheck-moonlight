"""Replay the predictive tracker against recorded checks and score where the press lands.

    .venv/bin/python tools/replay_tracker.py recordings_missed
    .venv/bin/python tools/replay_tracker.py recordings --round-trip-ms 72

This is the tracker's evidence. It runs the same `dbd/utils/needle_tracker.py` the live
loop runs, frame by frame in recorded order, and then scores the scheduled press against
ground truth measured from the whole check: the needle's fitted position at the moment the
key would land, versus the Great band read off the drawn pixels.

`recordings_missed` is the set that can answer the question. Those checks were
deliberately let pass, so the needle sweeps clear through the zone and both edges stay
drawn — the landing angle is a real measurement rather than an extrapolation. In
`recordings` every check was hit, which freezes the needle partway through, so ground
truth there is the pre-hit fit extended forwards and the verdict is weaker.

Nothing here is allowed to see the future: the tracker is fed one frame at a time and
must commit to a press time before the last frame it could still act on. Ground truth is
computed separately, afterwards, from all the frames.
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
    ROUND_TRIP_MS, Fit, Sample, TrackerState, decide, find_zone, fit_sweep, lit_span,
    needle_angle, observe, refine_centre, static_image, trim_frozen_tail,
)

SWEEPING = ("repair-heal", "full white", "full black")  # wiggle oscillates; excluded


def parse_args():
    p = argparse.ArgumentParser(description="Score the predictive tracker on recordings")
    p.add_argument("recordings", nargs="?", default="recordings_missed")
    p.add_argument("--round-trip-ms", type=float, default=ROUND_TRIP_MS,
                   help="latency the tracker BELIEVES it has to lead by")
    p.add_argument("--true-round-trip-ms", type=float, default=None,
                   help="latency actually applied when scoring; differ from the above to "
                        "measure how much the 72 ms constant can be wrong by")
    p.add_argument("--decimate", type=int, default=1,
                   help="keep every Nth frame — the recordings run at ~40 fps and the live "
                        "loop manages 34, so 2 is a pessimistic stress test")
    p.add_argument("--verbose", action="store_true", help="print each check's decision trail")
    return p.parse_args()


def load_check(check_dir):
    """(records, images) for the frames where a check is on screen, in capture order."""

    manifest_path = os.path.join(check_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return [], []

    with open(manifest_path) as f:
        manifest = json.load(f)

    records, images = [], []
    for rec in manifest:
        if rec["desc"] == "None":
            continue  # pre-roll and aftermath; the live loop never starts a track on these
        img = cv2.imread(os.path.join(check_dir, rec["frame"]))
        if img is not None:
            records.append(rec)
            images.append(img)
    return records, images


def kind(records):
    descs = " ".join(r["desc"] for r in records)
    if "wiggle" in descs:
        return "wiggle"
    for name in SWEEPING:
        if name in descs:
            return name
    return "unknown"


def ground_truth(records, images, round_trip_ms):
    """Fit and zone measured from the whole check — the answer the tracker is scored against.

    Frames after a hit are dropped: the game freezes the needle on a successful hit, and
    those frames drag a straight-line fit badly. The freeze is invisible to a frame-equality
    test because the stream encoder keeps jittering pixels while the angle sits still, so
    it is detected by the needle failing to advance in its OWN direction — a clockwise
    assumption throws away every counter-clockwise Madness check on its second frame.
    """

    static = static_image(images)
    cx, cy, ring_r, peak = refine_centre(static)
    centre = (cx, cy)
    zone = find_zone(static, centre, ring_r)

    rows = [(r["t_ms"], *needle_angle(img, centre)) for r, img in zip(records, images)]

    # Keep only the block where a needle is really drawn. Without this the frames either
    # side of the check contribute stray-red angles that drag the whole-check rate — on
    # check_005 from 325 to 293 deg/s, which then scores a good press as an early miss.
    samples = tuple(Sample(t, a, s) for t, a, s in rows)
    start, stop = lit_span(samples)
    kept = trim_frozen_tail(samples[start:stop])

    if len(kept) < 5:
        return None, zone, centre, peak, 0

    t = np.array([s.t_ms for s in kept])
    angles = np.rad2deg(np.unwrap(np.deg2rad([s.angle for s in kept])))
    slope, intercept = np.polyfit(t, angles, 1)
    rms = float(np.sqrt(np.mean((angles - (slope * t + intercept)) ** 2)))
    fit = Fit(float(slope * 1000.0), float(intercept), rms, len(kept))
    return fit, zone, centre, peak, len(kept)


def replay(records, images, round_trip_ms, verbose=False):
    """Feed frames in order and return the press time the tracker commits to.

    Commit rule, matching the live loop: keep refining while another frame would still
    arrive before the press is due, and commit on the last frame that can still act.
    """

    state = TrackerState()
    trail = []
    for i, (rec, img) in enumerate(zip(records, images)):
        now = rec["t_ms"]
        state = observe(state, img, now)
        decision = decide(state, now, round_trip_ms)
        trail.append((now, decision))

        if decision.press_at_ms is None:
            continue
        next_t = records[i + 1]["t_ms"] if i + 1 < len(records) else float("inf")
        if decision.press_at_ms <= next_t:
            return decision, trail  # no further frame arrives in time to improve on this

    last = trail[-1][1] if trail else None
    return (last if last and last.press_at_ms is not None else None), trail


def score(decision, truth_fit, zone, round_trip_ms):
    """Where the press lands, and whether that is Great, Good or a miss."""

    if decision is None or decision.press_at_ms is None:
        return {"verdict": "NO FIRE"}
    if truth_fit is None or zone is None:
        return {"verdict": "UNSCORED"}

    lands_ms = decision.press_at_ms + round_trip_ms
    lands_deg = truth_fit.angle_at(lands_ms) % 360.0

    into_great = (lands_deg - zone.great_start) % 360.0
    into_zone = (lands_deg - zone.zone_start) % 360.0
    zone_width = (zone.zone_end - zone.zone_start) % 360.0
    err = (lands_deg - zone.great_mid + 540.0) % 360.0 - 180.0

    if into_great <= zone.great_width:
        verdict = "GREAT"
    elif into_zone <= zone_width:
        verdict = "good"
    else:
        verdict = "MISS"

    return {
        "verdict": verdict,
        "lands_deg": lands_deg,
        "err_deg": err,
        "err_ms": err / abs(truth_fit.rate_deg_s) * 1000.0,
    }


def main():
    args = parse_args()
    dirs = sorted(glob.glob(os.path.join(args.recordings, "check_*")))
    if not dirs:
        raise SystemExit(f"no check_* dirs under {args.recordings}")

    header = (f"{'check':<14}{'type':>13}{'deg/s':>8}{'great':>7}{'mid':>7}"
              f"{'lands':>8}{'err deg':>9}{'err ms':>8}  verdict")
    print(header)
    print("-" * len(header))

    scored = []
    for d in dirs:
        name = os.path.basename(d)
        records, images = load_check(d)
        if len(records) < 8:
            print(f"{name:<14}  SKIP — only {len(records)} lit frames")
            continue

        check_kind = kind(records)
        if check_kind == "wiggle":
            # Wiggle oscillates rather than sweeping, so a linear fit is wrong by
            # construction. It is also the one case that already works reactively.
            print(f"{name:<14}{'wiggle':>13}  — reactive path, not tracked")
            continue

        # Ground truth always uses every frame; only what the TRACKER sees is decimated.
        truth_fit, zone, centre, peak, n = ground_truth(records, images, args.round_trip_ms)
        decision, trail = replay(records[::args.decimate], images[::args.decimate],
                                 args.round_trip_ms, args.verbose)
        result = score(decision, truth_fit, zone,
                       args.true_round_trip_ms
                       if args.true_round_trip_ms is not None else args.round_trip_ms)

        if args.verbose:
            print(f"\n{name}: centre={centre[0]:.1f},{centre[1]:.1f} peak={peak:.0f} "
                  f"truth n={n} zone={zone}")
            for t, dec in trail:
                print(f"    t={t:7.1f}  {dec.reason:<28}"
                      + (f" press@{dec.press_at_ms:.0f} land@{dec.lands_at_ms:.0f} "
                         f"target={dec.target_deg:.1f}" if dec.press_at_ms else ""))

        rate = truth_fit.rate_deg_s if truth_fit else float("nan")
        gw = zone.great_width if zone else float("nan")
        gm = zone.great_mid if zone else float("nan")
        if result["verdict"] in ("NO FIRE", "UNSCORED"):
            reason = decision.reason if decision else (trail[-1][1].reason if trail else "-")
            print(f"{name:<14}{check_kind:>13}{rate:>8.0f}{gw:>7.1f}{gm:>7.1f}"
                  f"{'—':>8}{'—':>9}{'—':>8}  {result['verdict']} ({reason})")
            scored.append((name, result["verdict"], None))
            continue

        print(f"{name:<14}{check_kind:>13}{rate:>8.0f}{gw:>7.1f}{gm:>7.1f}"
              f"{result['lands_deg']:>8.1f}{result['err_deg']:>9.1f}"
              f"{result['err_ms']:>8.1f}  {result['verdict']}")
        scored.append((name, result["verdict"], result))

    if not scored:
        return

    greats = [s for s in scored if s[1] == "GREAT"]
    goods = [s for s in scored if s[1] == "good"]
    misses = [s for s in scored if s[1] == "MISS"]
    nofire = [s for s in scored if s[1] in ("NO FIRE", "UNSCORED")]
    errs = np.array([s[2]["err_deg"] for s in scored if s[2]])

    print(f"\n{len(scored)} sweeping checks scored against pixel-measured zones:")
    print(f"  GREAT {len(greats)}   good {len(goods)}   MISS {len(misses)}   "
          f"no fire {len(nofire)}")
    if len(errs):
        print(f"  landing error vs Great centre: median {np.median(np.abs(errs)):.1f} deg, "
              f"bias {errs.mean():+.1f} deg, worst {np.abs(errs).max():.1f} deg")
        print(f"  Great band is +/-5.25 deg about its centre, so the error budget is 5.25")


if __name__ == "__main__":
    main()
