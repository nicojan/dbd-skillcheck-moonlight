"""Test the two assumptions predictive firing rests on, against recorded skill checks.

Reactive firing lands ~72 ms late because we press when we SEE the needle. Predicting
instead means estimating where the needle WILL be, which is only sound if:

  1. the needle can be extracted from a compressed stream frame at all, and
  2. its angular velocity is constant within a single check.

Run against the output of `record_checks.py`:

    .venv/bin/python tools/analyse_needle.py recordings

The needle is a red radial line; the success zone is a white arc. Scoring each pixel by
redness (R - max(G,B)) isolates the needle and cancels the white zone. Sampling that
score along rays through an annulus gives the needle angle per frame; a straight-line fit
against time then measures how constant the sweep rate is.

Errors are reported in the currency that matters: degrees of lead needed to cover the
~72 ms round trip. A fit error that is small next to that lead means prediction works.
"""

import argparse
import glob
import json
import os

import cv2
import numpy as np

CENTRE_PRIOR = (112.0, 102.0)  # where the ring sits in a 224 crop — NOT the crop's centre
CENTRE_SPAN = 8.0              # px searched around the prior, per check
CENTRE_STEP = 0.5
RING_SEARCH = (45.0, 85.0)
R_IN, R_OUT = 55, 100   # annulus containing the needle, inside the ring, outside the prompt
ANGLE_STEP = 0.5
MIN_NEEDLE_STRENGTH = 20.0
RELATIVE_FLOOR = 0.5    # of the check's own peak needle response; see drawn_frames()
ROUND_TRIP_MS = 72.0


def parse_args():
    p = argparse.ArgumentParser(description="Measure needle sweep constancy in recordings")
    p.add_argument("recordings", nargs="?", default="recordings")
    p.add_argument("--round-trip-ms", type=float, default=ROUND_TRIP_MS,
                   help="measured keypress->pixel latency the prediction must cover")
    p.add_argument("--verbose", action="store_true", help="print per-frame angles")
    return p.parse_args()


def sample_rays(img, cx, cy, radii):
    angles = np.arange(0, 360, ANGLE_STEP)
    theta = np.deg2rad(angles)[:, None]
    xs = np.clip(np.round(cx + radii[None, :] * np.sin(theta)).astype(int), 0, img.shape[1] - 1)
    ys = np.clip(np.round(cy - radii[None, :] * np.cos(theta)).astype(int), 0, img.shape[0] - 1)
    return angles, img[ys, xs]


def locate_ring(imgs):
    """Ring centre for one check, refined the way measure_zone.py does it.

    The angle is measured ABOUT this centre, so an error here lands straight in the angle
    and then in the velocity fit. Measuring about the crop's centre instead of the ring's
    inflates the residual roughly tenfold on clean footage — enough to make a needle that
    holds 300.0 deg/s to half a degree look like it is not moving at constant velocity.
    The ring is a full circle, so at the true centre its angle-median radial profile is a
    tall narrow spike; off centre the ring smears across radii and the peak drops.
    """

    stack = np.stack(imgs).astype(np.float32)
    b, g, r = stack[..., 0], stack[..., 1], stack[..., 2]
    whiteness = np.minimum(np.minimum(r, g), b)
    masked = np.where(r - np.maximum(g, b) > MIN_NEEDLE_STRENGTH, np.nan, whiteness)
    masked[:, np.all(np.isnan(masked), axis=0)] = 0.0  # never-uncovered pixels
    ring = np.nanmedian(masked, axis=0)

    radii = np.arange(*RING_SEARCH, CENTRE_STEP)
    grid = np.arange(-CENTRE_SPAN, CENTRE_SPAN + 1e-9, CENTRE_STEP)
    best = (-1.0, CENTRE_PRIOR[0], CENTRE_PRIOR[1])
    for dx in grid:
        for dy in grid:
            cx, cy = CENTRE_PRIOR[0] + dx, CENTRE_PRIOR[1] + dy
            peak = np.median(sample_rays(ring, cx, cy, radii)[1], axis=0).max()
            if peak > best[0]:
                best = (peak, cx, cy)
    return best[1], best[2]


def needle_angle(bgr, centre=CENTRE_PRIOR):
    """(angle_deg, strength) of the strongest red radial line. 0 deg = up, clockwise."""

    b, g, r = (bgr[:, :, i].astype(np.float32) for i in range(3))
    redness = r - np.maximum(g, b)  # needle is red; the white zone arc cancels out

    angles, rays = sample_rays(redness, centre[0], centre[1], np.arange(R_IN, R_OUT))
    profile = rays.mean(axis=1)  # mean along each ray

    return float(angles[int(np.argmax(profile))]), float(profile.max())


def drawn_frames(rows):
    """Keep the contiguous block of frames where the needle is actually on screen.

    A fixed strength floor is not enough. The classifier labels check-free frames either
    side of a real check with a confident class of its own — `full black (out)` on this
    footage — so those frames survive the `desc == "None"` pre-roll filter and reach the
    fit, where the brightest stray red pixel supplies a meaningless angle that drifts the
    opposite way. That was enough to reverse the inferred sweep direction and make the
    frozen-tail trimmer discard the check on its second frame.

    Judging strength relative to the check's own peak is what generalises: a drawn needle
    scores 70-125 here and 80-150 on our own captures, while these strays reach 20-45.
    """

    strengths = np.array([r[2] for r in rows])
    if not len(strengths):
        return rows
    ref = float(np.median(strengths[strengths >= np.percentile(strengths, 75)]))
    lit = strengths >= max(MIN_NEEDLE_STRENGTH, RELATIVE_FLOOR * ref)

    best, run, start = (0, 0), 0, 0
    for i, on in enumerate([*lit, False]):
        if on:
            start = i if run == 0 else start
            run += 1
        else:
            best, run = max(best, (run, start)), 0
    length, start = best
    return rows[start:start + length]


def trim_frozen_tail(rows):
    """Drop frames after the needle stops dead.

    On a successful hit the game freezes the needle at the hit position. Those frames are
    not part of the sweep, and leaving them in drags a straight-line fit badly — they were
    the entire reason several checks first looked like they had non-constant velocity.

    Detect the freeze by the needle failing to ADVANCE, not by frames being identical:
    stream encoder noise keeps jittering the pixels (and so the response strength) while
    the angle sits at exactly the same value, so an equality test on the frame content
    never fires. Two consecutive non-advancing frames means the sweep is over; a single
    one can just be angular quantisation at a slow sweep rate.

    "Not advancing" cannot mean "did not move at all". Angular quantisation makes a frozen
    needle wobble half a degree either way, which resets a strict counter and lets the
    whole tail through: on `recordings_missed/check_005` a 200 ms frozen tail survived and
    dragged the fitted rate from 325 to 293 deg/s. The bar is a fraction of the check's own
    median step, matching `dbd/utils/needle_tracker.py`.

    "Advance" is signed by the check's own direction. The Doctor's Madness makes a check
    rotate counter-clockwise, and against a hardcoded clockwise assumption every frame of
    such a check reads as stalled — so the whole check is thrown away on its second frame,
    silently, and the reversed checks never reach the fit at all.
    """

    steps = [(b[1] - a[1] + 540) % 360 - 180 for a, b in zip(rows, rows[1:])]
    if not steps:
        return rows
    sign = 1.0 if float(np.median(steps)) >= 0 else -1.0
    bar = max(2.0, 0.4 * float(np.median(np.abs(steps))))

    kept = []
    stalled = 0
    for i, row in enumerate(rows):
        if i > 0:
            advanced = sign * ((row[1] - rows[i - 1][1] + 540) % 360 - 180)  # wrap-safe
            stalled = stalled + 1 if advanced < bar else 0
            if stalled >= 2:
                return kept[:-1]  # the first stalled frame is already frozen too
        kept.append(row)
    return kept


def analyse(check_dir, round_trip_ms, verbose=False):
    manifest_path = os.path.join(check_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return None

    with open(manifest_path) as f:
        manifest = json.load(f)

    live = []
    for rec in manifest:
        if rec["desc"] == "None":
            continue  # pre-roll, before the check appeared
        img = cv2.imread(os.path.join(check_dir, rec["frame"]))
        if img is not None:
            live.append((rec, img))
    if len(live) < 6:
        return {"dir": check_dir, "skip": f"only {len(live)} frames"}

    centre = locate_ring([img for _, img in live])

    rows = [(rec["t_ms"], *needle_angle(img, centre), rec["desc"]) for rec, img in live]
    rows = drawn_frames(rows)
    rows = trim_frozen_tail(rows)
    if len(rows) < 6:
        return {"dir": check_dir, "skip": f"only {len(rows)} usable frames"}

    t = np.array([r[0] for r in rows])
    angles = np.rad2deg(np.unwrap(np.deg2rad([r[1] for r in rows])))
    slope, intercept = np.polyfit(t, angles, 1)
    residuals = angles - (slope * t + intercept)
    rms = float(np.sqrt(np.mean(residuals ** 2)))
    lead_deg = slope * round_trip_ms

    if verbose:
        for (ts, ang, strength, desc) in rows:
            print(f"    t={ts:7.0f} angle={ang:6.1f} strength={strength:6.1f}  {desc}")

    return {
        "dir": check_dir,
        "centre": centre,
        "frames": len(rows),
        "deg_per_s": slope * 1000.0,
        "rms_deg": rms,
        "max_dev_deg": float(np.abs(residuals).max()),
        "lead_deg": float(lead_deg),
        "err_pct_of_lead": float(rms / max(abs(lead_deg), 1e-9) * 100.0),
        "wiggle": any("wiggle" in r[3] for r in rows),
    }


def main():
    args = parse_args()
    dirs = sorted(glob.glob(os.path.join(args.recordings, "check_*")))
    if not dirs:
        raise SystemExit(f"no check_* dirs under {args.recordings}")

    print(f"{len(dirs)} recordings, lead required for {args.round_trip_ms:.0f} ms round trip\n")
    header = f"{'check':<14}{'n':>4}{'deg/s':>9}{'RMS deg':>9}{'max dev':>9}{'lead deg':>10}{'err/lead':>10}"
    print(header)
    print("-" * len(header))

    results = []
    for d in dirs:
        r = analyse(d, args.round_trip_ms, args.verbose)
        if r is None:
            continue
        name = os.path.basename(r["dir"])
        if "skip" in r:
            print(f"{name:<14}  SKIP — {r['skip']}")
            continue
        results.append(r)
        tag = "  wiggle (oscillates, linear fit invalid)" if r["wiggle"] else ""
        if r["deg_per_s"] < 0:
            tag = "  COUNTER-CLOCKWISE (Madness)" + tag
        print(f"{name:<14}{r['frames']:>4}{r['deg_per_s']:>9.1f}{r['rms_deg']:>9.2f}"
              f"{r['max_dev_deg']:>9.2f}{r['lead_deg']:>10.1f}{r['err_pct_of_lead']:>9.0f}%{tag}")

    sweeps = [r for r in results if not r["wiggle"]]
    if not sweeps:
        return

    rates = np.abs([r["deg_per_s"] for r in sweeps])
    ccw = [r for r in sweeps if r["deg_per_s"] < 0]
    errs = np.array([r["err_pct_of_lead"] for r in sweeps])
    good = [r for r in sweeps if r["err_pct_of_lead"] < 25]

    print(f"\n{len(sweeps)} sweeping checks (wiggle excluded):")
    spread = (rates.max() - rates.min()) / np.median(rates)
    note = (" — varies BETWEEN checks, so the rate must be estimated live per check, "
            "not hardcoded" if spread > 0.05 else
            " — one rate across this set; do not generalise, other sets vary by 10-30%")
    print(f"  sweep rate spans {rates.min():.0f}-{rates.max():.0f} deg/s "
          f"(median {np.median(rates):.0f}){note}")
    if ccw:
        print(f"  {len(ccw)}/{len(sweeps)} rotate counter-clockwise — the sign of the "
              "fitted slope is load-bearing, never assume clockwise")
    print(f"  fit error vs required lead: median {np.median(errs):.0f}%, "
          f"{len(good)}/{len(sweeps)} checks under 25%")
    print("  => constant angular velocity holds WITHIN a check; predictive firing is sound"
          if len(good) >= len(sweeps) * 0.6 else
          "  => velocity is NOT reliably constant; predictive firing needs a better model")


if __name__ == "__main__":
    main()
