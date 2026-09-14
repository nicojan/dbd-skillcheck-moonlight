"""Pair the operator's recorded key presses against the arc drawn when each one landed.

    .venv/bin/python tools/pair_keys.py frames/bout_20260913-130005
    .venv/bin/python tools/pair_keys.py frames/bout_20260913-130005 --sweep-trip

Nothing else in the repo reads `keys.jsonl`. It exists because the `1234!` check gives no
visual feedback whatsoever — duration, arc count, arc width and the terminal needle freeze
are indistinguishable between a check that passed and one nobody touched (NOTES-local.md,
2026-09-12) — so the only available label is the keyboard. In a `--dry-run --record-keys`
match the bot presses nothing, so every press in the file is the operator's, and if the
operator hit every beat and the check passed then every press is a CONFIRMED SUCCESS.

That inverts the usual direction of these tools. `replay_outline.py` scored the tracker's
press against the arc and asked whether the press was good. Here the press is known good
and the ARC READING is what is on trial: a confirmed-success press that reads OUTSIDE is
evidence against the detector's geometry, its round-trip assumption, or both.

What this tool does NOT know is which presses belong to a `1234!` check. Presses are
grouped into bursts on `--gap-ms` and the burst shape is reported (a `1234!` check is
~5 beats at ~679 ms); reading a burst as a check is the operator's call, not this tool's.

`find_outline_arc` is duplicated here rather than imported. It was removed from the tracker
in `8a1c62d` and that removal stands — this is an analysis copy, and it is deliberately not
a route back into the live path.
"""

import argparse
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.utils.needle_tracker import (
    ANGLE_STEP, FILL_RUN_PX, HOT, MIN_CENTRE_PEAK, MIN_GREAT_DEG, RADIUS_STEP,
    ROUND_TRIP_MS, Sample,
    WINDOW_IN, WINDOW_OUT, ZONE_THICK_PX, Zone, _close_gaps, _longest_run, _max_contiguous,
    fit_sweep, needle_angle, refine_centre, sample_rays, static_image,
)
from dbd.utils.key_watcher import ABILITY_KEYCODE, SPACE_KEYCODE
from dbd.utils.wide_capture import centre_slice, geometry_from_describe

# Recovered from 8a1c62d^ alongside find_outline_arc. The measurements behind them are in
# NOTES-local.md: `1234!` never reads below 106 deg, Merciless Storm draws the same unfilled
# outline at 39-40 and Madness Storm at 37-56, so 90 separates them with 34 deg of daylight.
OUTLINE_MIN_DEG = 90.0
OUTLINE_MAX_DEG = 140.0

# How far the needle's own red mask eats into the arc it is standing on. `static_image`
# blanks anything redder than NEEDLE_REDNESS and the needle is drawn with a glow, so the
# arc's LEADING edge reads 6-12 deg late once the needle sits on it. The TRAILING edge is
# untouched, which is why the arc is identified by it here.
TRAILING_TOL_DEG = 8.0
PROBE_AHEAD_DEG = 20.0    # clear of the needle's own mask when asking "is the arc still there"

FIT_WINDOW_MS = 220.0     # frames each side of the landing used for the local rate fit
LOOKBACK_MS = 500.0       # how far back to hunt for a clean read of the leading edge


def find_outline_arc(static, centre, ring_r, angle_step=ANGLE_STEP):
    """The drawn arc of a relocating outline check, as a Zone, or None.

    Verbatim from `8a1c62d^:dbd/utils/needle_tracker.py` bar the dropped `outline=True`
    flag, which the current Zone has no field for. Read from ONE frame, not the static
    median every other zone read uses: the arc relocates four to five times in 2.9 s and a
    median over STATIC_FRAMES straddles a jump and smears two positions into one.
    """

    radii = np.arange(ring_r + WINDOW_IN, ring_r + WINDOW_OUT, RADIUS_STEP)
    angles, polar = sample_rays(static, centre[0], centre[1], radii, angle_step=angle_step)
    hot = (polar - np.median(polar, axis=0, keepdims=True)) > HOT

    # Any solid fill at all and this is not our check. The exact complement of `find_zone`'s
    # Great-band test: a contiguous angular RUN of filled angles, not any single one.
    runs = np.array([_max_contiguous(hot[i]) for i in range(hot.shape[0])])
    band = _longest_run(runs >= FILL_RUN_PX, angle_step)
    if band is not None and band[1] * angle_step >= MIN_GREAT_DEG:
        return None

    thickness = hot.sum(axis=1) * RADIUS_STEP
    found = _longest_run(_close_gaps(thickness >= ZONE_THICK_PX, angle_step), angle_step)
    if found is None:
        return None

    start, length = found
    width = length * angle_step
    if not (OUTLINE_MIN_DEG <= width <= OUTLINE_MAX_DEG):
        return None

    edge = float(angles[start])
    return Zone(great_start=edge, great_end=(edge + width) % 360.0,
                zone_start=edge, zone_end=(edge + width) % 360.0)


def drawn_mask(img, centre, ring_r):
    """Per-angle "the arc is drawn here", as `find_outline_arc` reads it but ungated.

    Deliberately NOT `find_outline_arc` for the landing frame: that applies OUTLINE_MIN_DEG,
    and with the needle standing on the arc a 109 deg arc reads 87 and the width gate throws
    away the very frame being asked about.
    """

    radii = np.arange(ring_r + WINDOW_IN, ring_r + WINDOW_OUT, RADIUS_STEP)
    angles, polar = sample_rays(static_image([img]), centre[0], centre[1], radii)
    hot = (polar - np.median(polar, axis=0, keepdims=True)) > HOT
    return angles, _close_gaps(hot.sum(axis=1) * RADIUS_STEP >= ZONE_THICK_PX, ANGLE_STEP)


def run_containing(img, centre, ring_r, angle_deg):
    """(start, end) of the drawn run covering `angle_deg`, or None if nothing is drawn."""

    angles, drawn = drawn_mask(img, centre, ring_r)
    n = len(drawn)
    k = int(round(angle_deg / ANGLE_STEP)) % n
    if not drawn[k]:
        return None
    lo = hi = k
    while drawn[(lo - 1) % n] and (k - lo) % n < n - 1:
        lo -= 1
    while drawn[(hi + 1) % n] and (hi - k) % n < n - 1:
        hi += 1
    return float(angles[lo % n]), float(angles[(hi + 1) % n])


def load_bout(bout_dir):
    """(meta, geometry, records, presses) for a recorded bout. Frames load on demand.

    A bout stores the WIDE 672 grab, not the production 224 crop, so every frame has to go
    through `centre_slice` before the tracker's constants mean anything — CENTRE_PRIOR is a
    location in the 224 crop, and reading a 672 frame directly puts the ring 161 px off.
    """

    path = os.path.join(bout_dir, "bout.json")
    if not os.path.exists(path):
        raise SystemExit(f"{bout_dir}: no bout.json. An interrupted bout is MOVED to "
                         f"frames/discard/ rather than deleted, so look there before "
                         f"concluding it is gone")
    with open(path) as f:
        meta = json.load(f)
    if not meta.get("keys_watched"):
        raise SystemExit(f"{bout_dir}: recorded without --record-keys, there is nothing "
                         f"to pair (keys_watched={meta.get('keys_watched')!r})")

    geometry = geometry_from_describe(meta["geometry"])
    with open(os.path.join(bout_dir, "manifest.jsonl")) as f:
        records = [json.loads(line) for line in f if line.strip()]
    with open(os.path.join(bout_dir, "keys.jsonl")) as f:
        presses = [json.loads(line) for line in f if line.strip()]
    return meta, geometry, records, presses


def read_frames(bout_dir, geometry, records):
    """Every frame of the bout as its production 224 crop, in capture order."""

    out = []
    for r in records:
        wide = cv2.imread(os.path.join(bout_dir, r["frame"]))
        out.append(None if wide is None else centre_slice(wide, geometry))
    return out


def read_arcs(images, centre, ring_r):
    """Per frame: (gated arc or None, drawn run under the needle or None, needle angle).

    Two readings of the same pixels, and keeping them apart is the whole correctness story.

    `find_outline_arc` applies OUTLINE_MIN_DEG, so it answers "where is the arc" only while
    the needle is somewhere else. The moment the needle enters the arc it splits the drawn
    run in two and a 110 deg arc reads 84, the gate throws the frame away, and the frame
    discarded is the one being asked about. A first pass here used it for both questions and
    reported all five presses ~186 deg outside the arc — a constant offset, which is the
    shape a broken instrument makes, not the shape a wrong operator makes.

    So "is the needle standing on the arc" is asked of the raw drawn mask instead, probed
    PROBE_AHEAD_DEG further along the sweep, clear of the needle's own red glow.
    """

    out = []
    for img in images:
        if img is None:
            out.append((None, None, None))
            continue
        angle, _ = needle_angle(img, centre)
        arc = find_outline_arc(static_image([img]), centre, ring_r)
        run = run_containing(img, centre, ring_r, (angle + PROBE_AHEAD_DEG) % 360.0)
        out.append((arc, run, angle))
    return out


def epochs(records, readings):
    """The life of each drawn arc: when it first appears, when it goes, and where it starts.

    An arc is identified by its TRAILING edge, the end the needle has not reached and so
    cannot have masked. The leading edge is taken from the FIRST frame the arc appears in —
    the last clean look at where it starts, before the needle arrives and eats 6-12 deg of
    it. Reading the leading edge at press time instead is the second way to get this wrong
    and it was measured on 2026-09-12: it called all four tracker presses outside by ~6 deg
    purely because of the mask.
    """

    out = []
    for rec, (arc, _run, _angle) in zip(records, readings):
        if arc is None:
            continue
        if out and abs((arc.zone_end - out[-1]["trailing"] + 180.0) % 360.0 - 180.0) \
                <= TRAILING_TOL_DEG:
            out[-1]["last_ms"] = rec["t_ms"]
            continue
        out.append({"first_ms": rec["t_ms"], "last_ms": rec["t_ms"],
                    "leading": arc.zone_start, "trailing": arc.zone_end,
                    "width": arc.zone_width})
    return out


def on_arc_at(records, readings, t_ms):
    """Was the needle standing on a drawn arc in the frame nearest `t_ms`, and where."""

    i = int(np.argmin([abs(r["t_ms"] - t_ms) for r in records]))
    _arc, run, angle = readings[i]
    return records[i]["t_ms"], angle, run


def score_press(records, readings, spans, t_ms, window_ms):
    """The arc a press acted on, found by the relocation that follows it.

    The press is not scored against the arc drawn `round trip` ms later, which is what the
    first version of this tool did. On this check that frame is already the NEXT arc: the
    relocation is the response to the press, so by the time the press is visible to us the
    thing it was aimed at has gone. The arc acted on is the one that ENDS at the relocation,
    and the needle position that matters is its last frame.

    `window_ms` bounds how long after a press a relocation may still be called its
    consequence. It is a link round trip, not a guess about the game.
    """

    nxt = [e for e in spans if 0.0 <= e["first_ms"] - t_ms <= window_ms]
    if not nxt:
        return {"verdict": "NO RELOCATION"}
    onset = nxt[0]
    prior = [e for e in spans if e["last_ms"] < onset["first_ms"]]
    if not prior:
        return {"verdict": "NO PRIOR ARC", "lag_ms": onset["first_ms"] - t_ms}

    arc = prior[-1]
    frame_ms, angle, run = on_arc_at(records, readings, arc["last_ms"])
    if angle is None:
        return {"verdict": "NO NEEDLE", "lag_ms": onset["first_ms"] - t_ms}

    past = (angle - arc["leading"]) % 360.0
    past = past - 360.0 if past > 180.0 else past
    return {"verdict": "ON ARC" if run is not None and 0.0 <= past <= arc["width"]
                       else "OFF ARC",
            "lag_ms": onset["first_ms"] - t_ms, "frame_ms": frame_ms, "needle": angle,
            "arc": arc, "past": past, "run": run}


def bursts(presses, gap_ms):
    """Presses grouped into bursts, split wherever the gap exceeds `gap_ms`.

    A burst is a CANDIDATE check and nothing more: nothing here distinguishes five beats on
    a `1234!` check from five ordinary checks in a row. A `1234!` check is five beats at
    ~679 ms, the arc's own relocation interval when nobody presses.
    """

    out = []
    for p in presses:
        if not out or p["t_ms"] - out[-1][-1]["t_ms"] > gap_ms:
            out.append([])
        out[-1].append(p)
    return out


def describe_burst(burst, index, start_ms=None):
    """One burst, and its offset from the Performance start when there is one.

    `start_ms` is the Active Ability press. The operator starts every Performance by hand,
    so that press is a real `t0` on the frame clock rather than an inferred one, and beat
    offsets measured from it are the thing a beat grid would have to predict.
    """

    gaps = [b["t_ms"] - a["t_ms"] for a, b in zip(burst, burst[1:])]
    sources = sorted({p["source"] for p in burst})
    tag = "operator" if sources == [1] else ("BOT" if sources == [0] else f"mixed {sources}")
    head = (f"burst {index}: {len(burst)} beats over "
            f"{burst[-1]['t_ms'] - burst[0]['t_ms']:.0f} ms  [{tag}]")
    if start_ms is not None:
        head += (f"\n  Performance started (Active Ability) at {start_ms:.0f} ms; "
                 f"first beat +{burst[0]['t_ms'] - start_ms:.0f} ms")
    return head + f"\n  intervals: {'  '.join(f'{g:.0f}' for g in gaps) or 'single beat'} ms"


def performance_start(starts, burst, reach_ms=16000.0):
    """The Active Ability press this burst belongs to, or None.

    Bounded by `reach_ms` rather than taken as "the most recent one ever", because a start
    far behind the burst is a DIFFERENT Performance and pairing to it invents a `t0` that
    never happened. 16 s is deliberately generous: a Performance runs up to 15 s and a
    Performance is MANY bursts, not one — in `bout_20260913-130005` the second burst begins
    9.4 s after the start and is plainly the same run. The cool-down is 90-110 s, so the
    next real start cannot arrive for another minute and a half and there is nothing for a
    wide window to collide with.

    A first pass used 6000 ms and orphaned exactly that second burst. The unit tests all
    passed; the end-to-end run against a real bout is what showed it.
    """

    before = [s for s in starts if 0.0 <= burst[0]["t_ms"] - s["t_ms"] <= reach_ms]
    return before[-1]["t_ms"] if before else None


def parse_args():
    p = argparse.ArgumentParser(description="Pair recorded key presses against drawn arcs")
    p.add_argument("bout", help="a frames/bout_* directory recorded with --record-keys")
    p.add_argument("--window-ms", type=float, default=120.0,
                   help="how long after a press a relocation may still be called its "
                        "consequence. A link round trip, not a guess about the game")
    p.add_argument("--gap-ms", type=float, default=1500.0,
                   help="press gap that starts a new burst; a 1234! beat is ~679 ms")
    p.add_argument("--bot-presses", action="store_true",
                   help="score source-0 presses too. Off by default: in a --dry-run "
                        "calibration match every press is the operator's, and a source-0 "
                        "press means the run was not a dry run")
    return p.parse_args()


def main():
    args = parse_args()
    meta, geometry, records, presses = load_bout(args.bout)
    if not records:
        raise SystemExit(f"{args.bout}: no frames in manifest.jsonl")

    spacing = float(np.median(np.diff([r["t_ms"] for r in records])))
    human = [p for p in presses if args.bot_presses or p["source"] != 0]
    dropped = len(presses) - len(human)

    # The Active Ability press STARTS the Performance and is not a beat. Scoring it as one
    # would put a press ~3 s before the first arc exists and report it as a miss.
    starts = [p for p in human if p.get("keycode") == ABILITY_KEYCODE]
    kept = [p for p in human if p.get("keycode", SPACE_KEYCODE) != ABILITY_KEYCODE]

    print(f"{args.bout}")
    print(f"  {len(records)} frames at {spacing:.1f} ms spacing, "
          f"t {records[0]['t_ms']:.0f} to {records[-1]['t_ms']:.0f} ms")
    print(f"  {len(presses)} presses: {len(kept)} beats, "
          f"{len(starts)} Performance starts"
          + (f", {dropped} source-0 (bot) excluded" if dropped else ""))
    print(f"  checks logged by the bout: {len(meta.get('checks', []))}")
    if not starts:
        print("  NOTE: no Active Ability press in this bout — either it predates the "
              "two-keycode watcher, or the start fell outside the clip window, or the "
              "in-game binding no longer matches ABILITY_KEYCODE")
    print()
    for i, burst in enumerate(bursts(kept, args.gap_ms), 1):
        print(describe_burst(burst, i, performance_start(starts, burst)))

    images = read_frames(args.bout, geometry, records)
    # The centre is refined over the whole bout: it is the ring's place in a FIXED grab, so
    # it does not move between checks, and more frames is a steadier median.
    cx, cy, ring_r, peak = refine_centre(static_image([i for i in images if i is not None]))
    centre = (cx, cy)
    print(f"\nring at ({cx:.2f}, {cy:.2f}) r={ring_r:.2f}, centre peak {peak:.1f}"
          + ("  [BELOW MIN_CENTRE_PEAK: this is the prior, not a measurement]"
             if peak < MIN_CENTRE_PEAK else ""))

    readings = read_arcs(images, centre, ring_r)
    spans = epochs(records, readings)
    print(f"{len(spans)} arc positions read across the bout:")
    for e in spans:
        print(f"  {e['first_ms']:8.0f} to {e['last_ms']:8.0f} ms   "
              f"{e['leading']:.0f}-{e['trailing']:.0f} ({e['width']:.0f} deg)")

    print(f"\nscored against the arc that ENDS at the relocation following each press "
          f"(within {args.window_ms:.0f} ms)\n")
    print(f"{'press ms':>9} {'relocated':>10} {'lag ms':>7} {'needle':>7} {'arc':>14} "
          f"{'past edge':>10} {'to far end':>11}  verdict")
    print("-" * 96)
    rows = [score_press(records, readings, spans, p["t_ms"], args.window_ms) for p in kept]
    for p, r in zip(kept, rows):
        if "past" not in r:
            lag = f"{r['lag_ms']:7.0f}" if "lag_ms" in r else " " * 7
            print(f"{p['t_ms']:9.0f} {'':>10} {lag} " + " " * 45 + f"  {r['verdict']}")
            continue
        arc = r["arc"]
        print(f"{p['t_ms']:9.0f} {p['t_ms'] + r['lag_ms']:10.0f} {r['lag_ms']:7.0f} "
              f"{r['needle']:7.1f} "
              f"{f"{arc['leading']:.0f}-{arc['trailing']:.0f}({arc['width']:.0f})":>14} "
              f"{r['past']:9.1f} deg {arc['width'] - r['past']:10.1f} deg  {r['verdict']}")

    v = [r["verdict"] for r in rows]
    lags = [r["lag_ms"] for r in rows if "lag_ms" in r]
    print(f"\n{len(rows)} presses: {v.count('ON ARC')} with the needle on the arc, "
          f"{v.count('OFF ARC')} off it, "
          f"{len(rows) - v.count('ON ARC') - v.count('OFF ARC')} unscored")
    if lags:
        print(f"press to relocation: {np.mean(lags):.0f} ms mean, sd {np.std(lags):.0f}, "
              f"range {min(lags):.0f}-{max(lags):.0f}, over {len(lags)} presses "
              f"(frame spacing is {spacing:.0f} ms, so that is the resolution floor)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
