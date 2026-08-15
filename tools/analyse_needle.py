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

CENTRE = 112.0          # frames from record_checks.py are 224x224
R_IN, R_OUT = 55, 100   # annulus containing the needle, inside the ring, outside the prompt
ANGLE_STEP = 0.5
MIN_NEEDLE_STRENGTH = 20.0
ROUND_TRIP_MS = 72.0


def parse_args():
    p = argparse.ArgumentParser(description="Measure needle sweep constancy in recordings")
    p.add_argument("recordings", nargs="?", default="recordings")
    p.add_argument("--round-trip-ms", type=float, default=ROUND_TRIP_MS,
                   help="measured keypress->pixel latency the prediction must cover")
    p.add_argument("--verbose", action="store_true", help="print per-frame angles")
    return p.parse_args()


def needle_angle(bgr):
    """(angle_deg, strength) of the strongest red radial line. 0 deg = up, clockwise."""

    b, g, r = (bgr[:, :, i].astype(np.float32) for i in range(3))
    redness = r - np.maximum(g, b)  # needle is red; the white zone arc cancels out

    angles = np.arange(0, 360, ANGLE_STEP)
    radii = np.arange(R_IN, R_OUT)
    theta = np.deg2rad(angles)[:, None]
    xs = CENTRE + radii[None, :] * np.sin(theta)
    ys = CENTRE - radii[None, :] * np.cos(theta)

    xi = np.clip(np.round(xs).astype(int), 0, redness.shape[1] - 1)
    yi = np.clip(np.round(ys).astype(int), 0, redness.shape[0] - 1)
    profile = redness[yi, xi].mean(axis=1)  # mean along each ray

    return float(angles[int(np.argmax(profile))]), float(profile.max())


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
    """

    kept = []
    stalled = 0
    for i, row in enumerate(rows):
        if i > 0:
            advanced = (row[1] - rows[i - 1][1] + 540) % 360 - 180  # signed, wrap-safe
            stalled = stalled + 1 if advanced <= 0 else 0
            if stalled >= 2:
                break
        kept.append(row)
    return kept


def analyse(check_dir, round_trip_ms, verbose=False):
    manifest_path = os.path.join(check_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return None

    with open(manifest_path) as f:
        manifest = json.load(f)

    rows = []
    for rec in manifest:
        if rec["desc"] == "None":
            continue  # pre-roll, before the check appeared
        img = cv2.imread(os.path.join(check_dir, rec["frame"]))
        if img is None:
            continue
        angle, strength = needle_angle(img)
        if strength < MIN_NEEDLE_STRENGTH:
            continue
        rows.append((rec["t_ms"], angle, strength, rec["desc"]))

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
        print(f"{name:<14}{r['frames']:>4}{r['deg_per_s']:>9.1f}{r['rms_deg']:>9.2f}"
              f"{r['max_dev_deg']:>9.2f}{r['lead_deg']:>10.1f}{r['err_pct_of_lead']:>9.0f}%{tag}")

    sweeps = [r for r in results if not r["wiggle"]]
    if not sweeps:
        return

    rates = np.array([r["deg_per_s"] for r in sweeps])
    errs = np.array([r["err_pct_of_lead"] for r in sweeps])
    good = [r for r in sweeps if r["err_pct_of_lead"] < 25]

    print(f"\n{len(sweeps)} sweeping checks (wiggle excluded):")
    print(f"  sweep rate spans {rates.min():.0f}-{rates.max():.0f} deg/s "
          f"(median {np.median(rates):.0f}) — varies BETWEEN checks, so the rate must be "
          "estimated live per check, not hardcoded")
    print(f"  fit error vs required lead: median {np.median(errs):.0f}%, "
          f"{len(good)}/{len(sweeps)} checks under 25%")
    print("  => constant angular velocity holds WITHIN a check; predictive firing is sound"
          if len(good) >= len(sweeps) * 0.6 else
          "  => velocity is NOT reliably constant; predictive firing needs a better model")


if __name__ == "__main__":
    main()
