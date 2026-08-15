"""Measure the sweep rate of every skill check in a recorded frame session.

`analyse_needle.py` answers the same question from `record_checks.py` output, but that
tool only fires on checks it caught live at 40 fps. This one works from the full-frame
sessions written by `record_frames.py`, which means a whole evening of play can be
re-examined after the fact — including check types nobody thought to look for at the time.

It classifies ONLY the centre crop, not the full tile grid, so it costs one inference per
frame instead of 153. A 13k-frame session takes well under a minute.

    .venv/bin/python tools/sweep_rates.py --frames frames/session_20260811_153049

Reports deg/s per check and how well a constant-velocity model fits, which is what
predictive firing depends on. Sped-up checks (Hyperfocus, Overcharge and similar) are the
interesting case: if they sweep faster but each still sweeps at a *constant* rate, then
estimating the rate live per check handles them and no retraining is needed. If they
accelerate mid-sweep, a linear model is not enough.
"""

import argparse
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.AI_model import AI_model
from tools.analyse_needle import MIN_NEEDLE_STRENGTH, trim_frozen_tail
from tools.scan_frames import (MODEL_INPUT, NONE_CLASS, NullMonitoring, load_frame_list,
                               locate_ring, needle_angle_about)

TRAINING_REFERENCE_HEIGHT = 1080.0
GAP_FRAMES = 4  # frames of None before the next detection counts as a separate check


def parse_args():
    p = argparse.ArgumentParser(description="Per-check sweep rates from a recorded session")
    p.add_argument("--frames", required=True)
    p.add_argument("--model", default="models/model.onnx")
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--min-frames", type=int, default=4,
                   help="a check needs this many usable needle frames to fit a rate")
    p.add_argument("--round-trip-ms", type=float, default=72.0)
    p.add_argument("--rescan", action="store_true",
                   help="re-run inference even if a detection cache exists")
    return p.parse_args()


def cache_path(session):
    return os.path.join(session, "centre_detections.json")


def log(message):
    print(message, flush=True)


def group_runs(detections, gap):
    """Consecutive detections into separate checks, split on a gap of quiet frames."""

    runs = []
    for det in detections:
        if runs and det["index"] - runs[-1][-1]["index"] <= gap:
            runs[-1].append(det)
        else:
            runs.append([det])
    return runs


def fit_run(run, min_frames):
    """(deg/s, rms, n) for one check, or None if the needle is not measurable."""

    # Locate the ring ONCE per check, from the median of the per-frame detections.
    # HoughCircles wobbles by a few pixels frame to frame, and since the angle is measured
    # about that centre, the wobble lands directly in the angle. Left per-frame it inflated
    # the fit error from ~3.9 to ~7.9 deg median — enough to look like the needle was
    # accelerating when it was not. The check does not move, so one ring per check is both
    # more accurate and more honest about what is being measured.
    per_frame = [(det, locate_ring(det["patch"])) for det in run]
    found = [r for _det, r in per_frame if r is not None]
    if not found:
        return None
    median_ring = tuple(int(np.median([r[i] for r in found])) for i in range(3))

    rows = []
    for det, own_ring in per_frame:
        angle, strength = needle_angle_about(det["patch"], median_ring)
        if strength < MIN_NEEDLE_STRENGTH and own_ring is not None:
            # Rare: the median is wrong for this frame (a mis-detection skewed it).
            # Fall back rather than silently dropping a frame from the fit.
            angle, strength = needle_angle_about(det["patch"], own_ring)
        if strength >= MIN_NEEDLE_STRENGTH:
            rows.append((det["t_ms"], angle, strength, det["desc"]))

    rows = trim_frozen_tail(rows)  # the needle stops dead on a hit; those frames are not sweep
    if len(rows) < min_frames:
        return None

    t = np.array([r[0] for r in rows])
    angles = np.rad2deg(np.unwrap(np.deg2rad([r[1] for r in rows])))
    slope, intercept = np.polyfit(t, angles, 1)
    rms = float(np.sqrt(np.mean((angles - (slope * t + intercept)) ** 2)))
    classes = sorted({r[3] for r in rows})
    return slope * 1000.0, rms, len(rows), classes


def load_cached_detections(session, geometry):
    """Cached centre-crop detections for this session, or None.

    The cache is keyed on the crop geometry: a cache taken with a different crop describes
    different pixels and must not be reused silently.
    """

    path = cache_path(session)
    if not os.path.isfile(path):
        return None
    with open(path) as handle:
        blob = json.load(handle)
    if tuple(blob.get("geometry", ())) != tuple(geometry):
        log(f"  ignoring cache at {path}: it was taken with crop {blob.get('geometry')}, "
            f"not {list(geometry)}")
        return None
    return blob["detections"]


def save_detections(session, geometry, records):
    path = cache_path(session)
    with open(path, "w") as handle:
        json.dump({"geometry": list(geometry), "detections": records}, handle)
    log(f"  cached {len(records)} detections to {path} — reuse with no inference next time")


def detect_centre(session, frame_list, geometry, args):
    """Classify the centre crop of every frame; return one record per detection.

    Records carry no pixels, only where to find them, so the result is small enough to
    cache. Inference over a full session is the expensive step (minutes); re-reading the
    handful of frames that actually fired is seconds.
    """

    tile, left, top = geometry
    model = AI_model(args.model, use_gpu=False, nb_cpu_threads=args.threads,
                     monitoring=NullMonitoring())
    records = []
    try:
        for index, (name, t_ms) in enumerate(frame_list):
            img = cv2.imread(os.path.join(session, name))
            if img is None:
                continue
            patch = img[top:top + tile, left:left + tile]
            if patch.shape[:2] != (tile, tile):
                continue
            model_patch = patch if tile == MODEL_INPUT else cv2.resize(
                patch, (MODEL_INPUT, MODEL_INPUT), interpolation=cv2.INTER_CUBIC)

            pred, desc, _probs, _hit = model.predict(np.ascontiguousarray(np.flip(model_patch, 2)))
            if pred != NONE_CLASS:
                records.append({"frame": name, "index": index,
                                "t_ms": t_ms if t_ms is not None else index, "desc": desc})

            if index % 2000 == 0 and index:
                log(f"  {index}/{len(frame_list)} frames, {len(records)} detections")
    finally:
        model.cleanup()
    return records


def read_patches(session, records, geometry):
    """Attach the model-sized patch to each detection record."""

    tile, left, top = geometry
    detections = []
    for record in records:
        img = cv2.imread(os.path.join(session, record["frame"]))
        if img is None:
            continue
        patch = img[top:top + tile, left:left + tile]
        if patch.shape[:2] != (tile, tile):
            continue
        model_patch = patch if tile == MODEL_INPUT else cv2.resize(
            patch, (MODEL_INPUT, MODEL_INPUT), interpolation=cv2.INTER_CUBIC)
        detections.append({**record, "patch": model_patch})
    return detections


def main():
    args = parse_args()
    session = args.frames
    frame_list = load_frame_list(session)
    if not frame_list:
        raise SystemExit(f"no frames in {session}")

    # Not frame_list[0]: a pruned session keeps its full manifest on purpose (so the timeline
    # stays interpretable) while the frames themselves are gone, so the first entry usually
    # does not exist on disk. Probe geometry from the first frame that actually reads.
    first = next((img for img in (cv2.imread(os.path.join(session, name))
                                  for name, _t_ms in frame_list) if img is not None), None)
    if first is None:
        raise SystemExit(f"no readable frames in {session} — manifest lists "
                         f"{len(frame_list)}, none present on disk")
    height, width = first.shape[:2]
    tile = max(int(round(MODEL_INPUT * height / TRAINING_REFERENCE_HEIGHT)), 8)
    left, top = width // 2 - tile // 2, height // 2 - tile // 2
    log(f"{len(frame_list)} frames at {width}x{height}, centre crop {tile}px at ({left},{top})")

    geometry = (tile, left, top)
    records = None if args.rescan else load_cached_detections(session, geometry)
    if records is None:
        records = detect_centre(session, frame_list, geometry, args)
        save_detections(session, geometry, records)
    else:
        log(f"  reusing {len(records)} cached detections (no inference)")

    detections = read_patches(session, records, geometry)
    runs = group_runs(detections, GAP_FRAMES)
    log(f"\n{len(detections)} detections in {len(runs)} distinct checks\n")

    # Start time and gap since the previous check are what make token-stacking perks
    # (Hyperfocus) readable: tokens accrue over a chain of checks close together in time and
    # reset when the chain breaks, so a rate is only interpretable next to its neighbours.
    header = (f"{'check':>6}{'n':>5}{'t (s)':>9}{'gap (s)':>9}"
              f"{'deg/s':>9}{'RMS deg':>9}{'lead deg':>10}  classes")
    log(header)
    log("-" * (len(header) + 20))

    rates = []
    previous_end_ms = None
    for i, run in enumerate(runs, 1):
        start_ms, end_ms = run[0]["t_ms"], run[-1]["t_ms"]
        gap_s = (start_ms - previous_end_ms) / 1000.0 if previous_end_ms is not None else float("nan")
        previous_end_ms = end_ms

        fitted = fit_run(run, args.min_frames)
        if fitted is None:
            continue
        deg_s, rms, n, classes = fitted
        if deg_s <= 0:
            continue  # wiggle reversing, or a mis-tracked needle
        lead = deg_s * args.round_trip_ms / 1000.0
        rates.append((deg_s, rms, lead))
        label = ", ".join(c.replace("repair-heal", "rh").replace("full ", "") for c in classes)
        log(f"{i:>6}{n:>5}{start_ms / 1000.0:>9.1f}{gap_s:>9.1f}"
            f"{deg_s:>9.1f}{rms:>9.2f}{lead:>10.1f}  {label}")

    if not rates:
        log("\nno measurable sweeps")
        return

    arr = np.array(rates)
    deg_s, rms, lead = arr[:, 0], arr[:, 1], arr[:, 2]
    err_pct = rms / np.maximum(lead, 1e-9) * 100

    log(f"\n{len(arr)} measurable checks")
    log(f"  sweep rate  {deg_s.min():.0f}-{deg_s.max():.0f} deg/s, median {np.median(deg_s):.0f}")
    log(f"  fit error   RMS {np.median(rms):.1f} deg median, "
        f"{np.median(err_pct):.0f}% of the {np.median(lead):.0f} deg lead needed")

    # State the result in the units that decide whether this is worth building. A fit
    # error in degrees means nothing until it sits next to the latency it must correct.
    ms_err = np.median(rms) / max(np.median(deg_s), 1e-9) * 1000
    log(f"  timing error implied by the fit: ~{ms_err:.0f} ms, against the "
        f"{args.round_trip_ms:.0f} ms the reactive path currently misses by")

    # The question sped-up checks actually pose: is the SPREAD wide enough that a fixed
    # rate would mis-time a press, while each individual check stays linear?
    fast = arr[deg_s > np.median(deg_s) * 1.3]
    if len(fast):
        log(f"  {len(fast)} checks are >30% above median rate (max {deg_s.max():.0f} deg/s) — "
            f"their fit error is {np.median(fast[:, 1]):.1f} deg RMS")
        log("  => speed varies BETWEEN checks but each stays linear: estimate the rate live "
            "per check from its first frames, never hardcode it")


if __name__ == "__main__":
    main()
