"""Measure the Great and Good zone widths geometrically, from the drawn pixels.

This is the independent check on `greatZoneSize`, the one simulator constant nothing else
of ours corroborates. It does NOT use the classifier: the model's `great` label was hand
annotated as "press about here" and is deliberately generous, so measuring the label
overstates the zone by roughly 3x. Here we read the arc the game actually draws.

Run against the output of `record_checks.py`:

    .venv/bin/python tools/measure_zone.py recordings_missed

Requires recordings where the check went UNHIT. A hit freezes the needle and ends the
check early, so the far edge of the zone is never drawn long enough to survive the median.

How it works
------------
Taking the per-pixel median across a check's frames removes the sweeping needle and leaves
only the static UI, so one angular profile describes the whole check.

The zone is drawn as an annulus straddling the base ring, and its two parts differ in
RADIAL THICKNESS rather than in brightness or position:

    Good  (outline)    two thin rails with a dark gap    ~2.2 px of lit radius
    Great (solid fill) a filled band                     ~8.5 px of lit radius

Subtracting the per-radius median over angle removes the base ring, which is present at
every angle, along with any radial trend in the background.

Two traps, both of which produced confident wrong answers before being fixed:

  * `cv2.HoughCircles` locates the ring centre only to about +/-3 px, and an off-centre
    origin shifts apparent radius by that much — enough to move the zone out of any fixed
    radial band and make the arc vanish on ~40% of checks. The centre is refined instead by
    maximising the angle-median radial peak, which lands it within a quarter pixel and
    yields ring_r = 65.00 on every check recorded so far.
  * The "SPACE" prompt box is static, bright, and has the same radial thickness signature
    as the Good outline. It sits near r=45 while the ring is at r=65, so the radial window
    below excludes it. Widening that window silently re-admits it as a phantom zone.
"""

import argparse
import glob
import json
import os

import cv2
import numpy as np

ANGLE_STEP = 0.5
RADIUS_STEP = 0.25
NEEDLE_REDNESS = 25.0      # per-pixel red dominance marking the needle, not the arc
CENTRE_PRIOR = (112.0, 102.0)
CENTRE_SPAN = 5.0          # search +/- this many px around the prior
RING_SEARCH = (45.0, 85.0)
WINDOW_IN, WINDOW_OUT = -6.0, 10.0   # radial window around the ring; excludes the prompt
HOT = 60.0                 # residual whiteness counted as "drawn"
FILL_RUN_PX = 4.0          # contiguous lit radius that means a solid fill, not an outline
ZONE_THICK_PX = 1.5        # total lit radius that means the arc is present at all
MIN_RUN_DEG = 3.0          # ignore shorter angular runs; they are compression speckle
CLOSE_DEG = 4.0            # bridge dropouts this short; antialiasing and stream
                           # compression punch 1-2 deg holes in the arc, and an unbridged
                           # hole truncates the zone at the hole instead of at its end
MIN_CENTRE_PEAK = 100.0    # a good centre fit peaks ~145; far below that means it failed


def parse_args():
    p = argparse.ArgumentParser(description="Measure Great/Good zone widths from pixels")
    p.add_argument("recordings", nargs="?", default="recordings_missed")
    p.add_argument("--verbose", action="store_true", help="print the per-angle profile")
    return p.parse_args()


def median_frame(check_dir):
    """Median whiteness image over the check's live frames, with the needle removed."""

    manifest_path = os.path.join(check_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return None

    with open(manifest_path) as f:
        manifest = json.load(f)

    imgs = []
    for rec in manifest:
        if rec["desc"] == "None":
            continue  # pre-roll, before the check appeared
        img = cv2.imread(os.path.join(check_dir, rec["frame"]))
        if img is not None:
            imgs.append(img)

    if len(imgs) < 8:
        return None

    stack = np.stack(imgs).astype(np.float32)
    b, g, r = stack[..., 0], stack[..., 1], stack[..., 2]
    whiteness = np.minimum(np.minimum(r, g), b)
    masked = np.where(r - np.maximum(g, b) > NEEDLE_REDNESS, np.nan, whiteness)

    all_needle = np.all(np.isnan(masked), axis=0)
    masked[:, all_needle] = 0.0  # a pixel the needle never left; nanmedian would warn
    return np.nanmedian(masked, axis=0)


def polar(img, cx, cy, radii):
    angles = np.arange(0, 360, ANGLE_STEP)
    th = np.deg2rad(angles)[:, None]
    xs = np.clip(np.round(cx + radii[None, :] * np.sin(th)).astype(int), 0, img.shape[1] - 1)
    ys = np.clip(np.round(cy - radii[None, :] * np.cos(th)).astype(int), 0, img.shape[0] - 1)
    return angles, img[ys, xs]


def refine_centre(img):
    """Centre that makes the base ring land at one radius for every angle.

    The ring is a full circle, so at the true centre the angle-median radial profile is a
    tall narrow spike. Off centre, the ring smears across radii and the peak drops.
    """

    radii = np.arange(*RING_SEARCH, RADIUS_STEP)
    grid = np.arange(-CENTRE_SPAN, CENTRE_SPAN + 1e-9, RADIUS_STEP)
    best = (-1.0, CENTRE_PRIOR[0], CENTRE_PRIOR[1])
    for dx in grid:
        for dy in grid:
            cx, cy = CENTRE_PRIOR[0] + dx, CENTRE_PRIOR[1] + dy
            peak = np.median(polar(img, cx, cy, radii)[1], axis=0).max()
            if peak > best[0]:
                best = (peak, cx, cy)
    peak, cx, cy = best
    prof = np.median(polar(img, cx, cy, radii)[1], axis=0)
    return cx, cy, float(radii[int(np.argmax(prof))]), float(peak)


def close_gaps(mask, width_deg=CLOSE_DEG):
    """Fill False gaps shorter than width_deg, wrapping around 360."""

    n = len(mask)
    span = int(round(width_deg / ANGLE_STEP))
    out = mask.copy()
    i = 0
    while i < 2 * n:  # two laps so a gap straddling 0 deg is seen whole
        if not out[i % n]:
            j = i
            while j < 2 * n and not out[j % n]:
                j += 1
            if j - i <= span and j < 2 * n:
                for k in range(i, j):
                    out[k % n] = True
            i = j
        else:
            i += 1
    return out


def longest_run(mask):
    """Longest circular run of True, as (start_index, length), or None if too short."""

    n = len(mask)
    if not mask.any():
        return None
    if mask.all():
        return (0, n)

    best_len = best_start = cur_len = cur_start = 0
    start = int(np.argmax(~mask))  # begin at a False so no run is split by the wrap
    for k in range(n):
        i = (start + k) % n
        if mask[i]:
            if cur_len == 0:
                cur_start = i
            cur_len += 1
            if cur_len > best_len:
                best_len, best_start = cur_len, cur_start
        else:
            cur_len = 0

    if best_len * ANGLE_STEP < MIN_RUN_DEG:
        return None
    return (best_start, best_len)


def max_contiguous(row):
    """Longest contiguous True run within one angle's radial samples, in pixels."""

    best = cur = 0
    for v in row:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best * RADIUS_STEP


def measure(check_dir):
    img = median_frame(check_dir)
    if img is None:
        return {"dir": check_dir, "skip": "too few frames"}

    cx, cy, ring_r, peak = refine_centre(img)
    if peak < MIN_CENTRE_PEAK:
        return {"dir": check_dir, "skip": f"centre fit failed (peak {peak:.0f})"}

    radii = np.arange(ring_r + WINDOW_IN, ring_r + WINDOW_OUT, RADIUS_STEP)
    angles, pm = polar(img, cx, cy, radii)
    resid = pm - np.median(pm, axis=0, keepdims=True)  # removes the base ring
    hot = resid > HOT

    thickness = hot.sum(axis=1) * RADIUS_STEP
    runs = np.array([max_contiguous(hot[i]) for i in range(hot.shape[0])])

    zone = longest_run(close_gaps(thickness >= ZONE_THICK_PX))
    if zone is None:
        return {"dir": check_dir, "skip": "no zone found", "ring_r": ring_r}
    z_start, z_len = zone
    idx = [(z_start + k) % len(angles) for k in range(z_len)]

    great = longest_run(runs[idx] >= FILL_RUN_PX)
    great_deg = great[1] * ANGLE_STEP if great else 0.0

    return {
        "dir": check_dir,
        "centre": (cx, cy),
        "ring_r": ring_r,
        "great_deg": great_deg,
        "good_deg": z_len * ANGLE_STEP - great_deg,
        "zone_deg": z_len * ANGLE_STEP,
        "span": (float(angles[idx[0]]), float(angles[idx[-1]])),
        "great_leads": bool(great and great[0] < z_len / 2),
        "profile": (angles, thickness, runs),
    }


def main():
    args = parse_args()
    dirs = sorted(glob.glob(os.path.join(args.recordings, "check_*")))
    if not dirs:
        raise SystemExit(f"no check_* dirs under {args.recordings}")

    header = (f"{'check':<14}{'ring r':>8}{'great':>8}{'good':>8}{'zone':>8}"
              f"{'span':>14}{'great at':>12}")
    print(header)
    print("-" * len(header))

    results = []
    for d in dirs:
        m = measure(d)
        name = os.path.basename(d)
        if "skip" in m:
            print(f"{name:<14}  SKIP — {m['skip']}")
            continue
        if args.verbose:
            angles, thickness, runs = m["profile"]
            print(f"\n{name} profile")
            for i in range(0, len(angles), 4):
                if thickness[i] > 0.5:
                    print(f"    {angles[i]:5.1f}  thick={thickness[i]:5.2f} "
                          f"maxrun={runs[i]:5.2f}")
            print()

        results.append((name, m["great_deg"], m["good_deg"], m["zone_deg"]))
        span = "{:.0f}-{:.0f}".format(*m["span"])
        where = "leading" if m["great_leads"] else "TRAILING"
        print(f"{name:<14}{m['ring_r']:>8.2f}{m['great_deg']:>8.1f}{m['good_deg']:>8.1f}"
              f"{m['zone_deg']:>8.1f}{span:>14}{where:>12}")

    if not results:
        return

    gw = np.array([r[1] for r in results])
    gd = np.array([r[2] for r in results])
    zw = np.array([r[3] for r in results])
    print(f"\n{len(results)} checks measured geometrically (degrees):")
    for label, arr in (("Great", gw), ("Good", gd), ("Total", zw)):
        print(f"  {label:<6} median {np.median(arr):5.1f}   mean {arr.mean():5.1f}   "
              f"range {arr.min():5.1f}-{arr.max():5.1f}")
    print("\n  simulator constants for comparison: greatZoneSize 10-20 (default 15), "
          "successZoneSize 50")


if __name__ == "__main__":
    main()
