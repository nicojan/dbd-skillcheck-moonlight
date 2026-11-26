"""Replay the opt-in 1-2-3-4! player over a recorded Performance, frame by frame.

    .venv/bin/python tools/replay_performance.py frames/bout_20261125-152526 --from -3000 --to 15700
    .venv/bin/python tools/replay_performance.py <bout> --from ... --to ... --lead 50 70 90

For each press the player would have sent, where the needle ACTUALLY was one round trip
later — read from the recorded frames, not extrapolated from the fit — measured from the
entry edge of the arc it was aimed at. The operator's band is [0, MAX_LAND_DEG]; the
target is LAND_DEG. Also: every arc drawn, and whether it got a press.

What this cannot tell you is whether the GAME accepts a press there. The check shows no
success signal; only the operator's report of the Performance closes that.
"""

import argparse
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbd.AI_model import AI_model
from dbd.utils.performance import (Arc, LAND_DEG, LOCATE_FRAMES, MAX_LAND_DEG,
                                   Session, locate_ring, read_frame, session_pressed,
                                   session_step)
from dbd.utils.wide_capture import centre_slice, geometry_from_describe, to_model_size


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("bout")
    p.add_argument("--from", dest="t_from", type=float, default=-1e9)
    p.add_argument("--to", dest="t_to", type=float, default=1e9)
    p.add_argument("--lead", type=float, nargs="+", default=[70.0],
                   help="round trip(s) in ms to replay at; the same value is the lead")
    p.add_argument("--ring", type=float, nargs=3, metavar=("CX", "CY", "R"),
                   help="the ring ordinary checks located this run, as the live player "
                        "gets it from perf.known_ring; omit to locate from 8 frames")
    p.add_argument("--negative", type=float, nargs="*", metavar="START_MS",
                   help="NEGATIVE CONTROL: replay every frame OUTSIDE the Performance "
                        "windows starting at these bout times (ms), as if an ability press "
                        "that started nothing had opened a window the whole time — a fresh "
                        "session every 16 s and after every lapse. Must press 0 times.")
    return p.parse_args()


def load(bout, t_from, t_to):
    meta = json.load(open(os.path.join(bout, "bout.json")))
    geometry = geometry_from_describe(meta["geometry"])
    recs = [json.loads(l) for l in open(os.path.join(bout, "manifest.jsonl")) if l.strip()]
    recs = [r for r in recs if t_from <= r["t_ms"] <= t_to]
    # The live source exactly: the centre slice at model size, never `look`'s crop.
    imgs = [to_model_size(centre_slice(cv2.imread(os.path.join(bout, r["frame"])), geometry))
            for r in recs]
    return [r["t_ms"] for r in recs], imgs


def needle_at(times, unwrapped, t):
    """The recorded needle angle at time t, linearly interpolated between frames."""
    return float(np.interp(t, times, unwrapped)) % 360.0


def replay(times, imgs, preds, lead_ms, ring=None):
    """`session_step` over the frames exactly as `autorun.play_performance_frame` drives
    it — gates, ring location, staleness and all — on the frame clock. One session for the
    whole range, so run it over ONE Performance window (--from the ability press)."""

    frame_ms = float(np.median(np.diff(times)))
    session = Session()
    presses, notes = [], []
    for t, img, pred in zip(times, imgs, preds):
        session, p, note = session_step(session, img, t / 1000.0, pred, t, lead_ms, frame_ms,
                                        ring=ring)
        if note:
            notes.append((t, note))
        if p is not None:
            at = max(t, p.press_at_ms)
            presses.append({"at": at, "entry": p.entry_deg, "rate": p.rate_deg_s,
                            "arc": session.state.arc, "planned": p.land_deg})
            session = session_pressed(session, at)
    return presses, notes


def negative(args):
    window_ms = 16000.0
    starts = args.negative or []
    times, imgs = load(args.bout, args.t_from, args.t_to)
    model = AI_model("models/model.onnx", use_gpu=False)
    keep = [i for i, t in enumerate(times)
            if not any(s - 1000.0 <= t <= s + window_ms + 1000.0 for s in starts)]
    lead = args.lead[0]
    frame_ms = float(np.median(np.diff(times)))
    session, opened, presses, arcs_seen, confirmed_frames = Session(), None, [], 0, 0
    prev_t = None
    for i in keep:
        t = times[i]
        if (opened is None or t - opened > window_ms or session.done is not None
                or (prev_t is not None and t - prev_t > 1000.0)):
            session, opened = Session(), t      # a new false window, as worst case
        prev_t = t
        pred = model.predict(imgs[i][:, :, ::-1])[0]
        ring = None if args.ring is None else ((args.ring[0], args.ring[1]), args.ring[2])
        session, p, _ = session_step(session, imgs[i], t / 1000.0, pred, t, lead, frame_ms,
                                     ring=ring)
        if session.state.arc_seen_ms == t:
            arcs_seen += 1
        from dbd.utils.performance import is_playing
        confirmed_frames += is_playing(session)
        if p is not None:
            presses.append((t, pred, p.entry_deg))
            session = session_pressed(session, max(t, p.press_at_ms))
    print(f"NEGATIVE CONTROL {args.bout}: {len(keep)} frames outside "
          f"{len(starts)} Performance window(s), {arcs_seen} frames with an outline arc "
          f"read, {confirmed_frames} frames the player would have been playing, "
          f"{len(presses)} PRESSES (must be 0)")
    for t, pred, entry in presses:
        print(f"  PRESS at bout t {t:.0f} ms, class {pred}, arc entry {entry:.0f}")
    return len(presses)


def main():
    args = parse_args()
    if args.negative is not None:
        sys.exit(1 if negative(args) else 0)
    times, imgs = load(args.bout, args.t_from, args.t_to)
    # Exactly the live path: the ring located once from the first frames, then every frame
    # read with `read_frame`. Start --from at the Active Ability press, as the live run does.
    model = AI_model("models/model.onnx", use_gpu=False)
    preds = [model.predict(img[:, :, ::-1])[0] for img in imgs]
    centre, ring_r, _peak = locate_ring(imgs[:LOCATE_FRAMES])
    if args.ring is not None:
        centre, ring_r = (args.ring[0], args.ring[1]), args.ring[2]
    print(f"{args.bout}: {len(times)} frames, ring ({centre[0]:.1f}, {centre[1]:.1f}) "
          f"r={ring_r:.1f}, located from the first {LOCATE_FRAMES}")
    read = [read_frame(img, centre, ring_r) for img in imgs]
    reads = [(a, None) for a, _ in read]
    arcs = [z for _, z in read]
    known = [(t, a) for t, (a, _) in zip(times, reads) if a is not None]
    kt = [t for t, _ in known]
    unwrapped_known = np.degrees(np.unwrap(np.radians([a for _, a in known])))

    drawn = []
    for t, z in zip(times, arcs):
        if z is None:
            continue
        if drawn and Arc(z.zone_start, z.zone_end).same_as(Arc(drawn[-1][1], drawn[-1][2])):
            continue
        drawn.append((t, z.zone_start, z.zone_end))
    print(f"{len(drawn)} distinct arcs drawn")

    # The arc as last READ within 200 ms: `find_outline_arc` drops exactly the frames where
    # the needle stands on the arc (its glow splits the outline), which are the beat frames.
    beats, last_arc, last_arc_t = [], None, None
    for i in range(1, len(times)):
        if arcs[i] is not None:
            last_arc, last_arc_t = arcs[i], times[i]
        z = last_arc if last_arc_t is not None and times[i] - last_arc_t <= 200.0 else None
        a_now, a_prev = reads[i][0], reads[i - 1][0]
        if a_now is None or a_prev is None or z is None:
            continue
        sign = 1.0 if ((a_now - a_prev + 180.0) % 360.0 - 180.0) >= 0 else -1.0
        entry = z.zone_start if sign > 0 else z.zone_end
        past = (((a_now - entry) * sign) + 180.0) % 360.0 - 180.0
        if 0.0 <= past <= 15.0 and (not beats or times[i] - beats[-1] > 300.0):
            beats.append(times[i])

    for lead in args.lead:
        ring = None if args.ring is None else ((args.ring[0], args.ring[1]), args.ring[2])
        presses, notes = replay(times, imgs, preds, lead, ring)
        for t, note in notes:
            print(f"  note at t {t / 1000 + 26:7.2f}*: {note}")
        offs = []
        for p in presses:
            land = needle_at(kt, unwrapped_known, p["at"] + lead)
            off = ((land - p["entry"]) * (1 if p["rate"] > 0 else -1)) % 360.0
            offs.append(off - 360.0 if off > 180 else off)
        inside = sum(1 for o in offs if 0.0 <= o <= MAX_LAND_DEG)
        print(f"\n--- round trip {lead:.0f} ms: {len(presses)} presses, {inside} landed in "
              f"[0, {MAX_LAND_DEG:.0f}] deg past entry (target {LAND_DEG:.0f}) ---")
        for p, o in zip(presses, offs):
            flag = "" if 0.0 <= o <= MAX_LAND_DEG else "   <-- OUTSIDE BAND"
            print(f"  t {p['at'] / 1000 + 26:7.2f}*  entry {p['entry']:5.1f}  "
                  f"rate {p['rate']:5.0f}  landed {o:+6.1f} deg{flag}")
        # A BEAT is a frame where the recorded needle stands 0-15 deg past the entry edge of
        # an arc drawn in that same frame, entry taken from the needle's own direction.
        # Counting drawn arcs instead counts every partial read of one arc as another arc.
        missed = [b for b in beats if not any(b - 400 <= p["at"] <= b + 400 for p in presses)]
        print(f"  beats the needle reached: {len(beats)}, with no press within 400 ms: "
              f"{len(missed)}" + "".join(f"\n    t {m / 1000 + 26:7.2f}*" for m in missed))
    print("\n* t is printed as seconds past 15:25:00 for the 2026-11-25 bout only "
          "(bout t=0 is 15:25:26); for any other bout read it as t_ms/1000 + 26")


if __name__ == "__main__":
    main()
