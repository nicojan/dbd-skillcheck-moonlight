"""Replay the LIVE decision path against video, showing the bot only its real centre crop.

    .venv/bin/python tools/replay_centre_crop.py recordings_video/merciless-storm/events.json
    .venv/bin/python tools/replay_centre_crop.py recordings_video/merciless-storm-madness2/events.json --fps 41

This exists because `replay_tracker.py` cannot answer "would the bot press here?" for the
two game states it matters most for. Two reasons, and both of them hid a real answer:

1. **It replays `recordings_video/`, whose crops are re-centred on the ring** — 224 crops
   placed so the ring lands at (112, 102). That is the right frame for measuring
   kinematics and the wrong one for asking what the bot sees, because live it grabs a
   fixed centre region and an off-centre Madness check is exactly the case where those two
   differ. Feeding it re-centred crops silently answers a question about a check that was
   never off-centre. This tool crops the source video the way `centre_crop_region` does.

2. **It only exercises the predictive path.** `decide` returning `may_react=True` is not a
   decision not to press — `autorun.py` then fires reactively on the classifier's cue. So
   "the tracker abstains" and "the bot does not press" are different claims, and NOTES
   recorded the first as if it were the second for Merciless Storm. It presses on 17 of 17
   revolutions.

Mirrors `autorun.py`'s armed loop: same `TRACKED_PREDS` gate, same `observe`/`decide` with
the BGR conversion, same `may_react` fallback condition. It does not mirror the wall-clock
parts — no sleeping to the press time, no `frame_ms` hold, no landing report — so it says
WHETHER and WHERE the bot commits, not how well the press lands. Grading a Merciless Storm
press needs a zone this repo cannot yet measure; see NOTES "What third-party recordings
settled".

**The crop is not the frame's geometric centre, and getting that wrong inverts the answer.**
Our own live recordings put the ring at (111.25, 101.75) with a quarter pixel of scatter
across ten checks — 10 px ABOVE the crop centre, which is where the `(112, 102)` constant in
`record_checks.py` and `ingest_video.py` comes from. The downloaded clips draw a centred
check at the frame centre instead, so cropping the geometric centre lands the ring at
(112, 112) and shows the model a framing the live bot never produces. Ten pixels changes the
classification wholesale: geometric-centre crops of `merciless-storm.mp4` call 3-8 frames per
revolution `full black (great)`, correctly framed crops call 0-1. The crop is therefore
offset by `RING_ABOVE_CENTRE_PX` so a centred check lands where live puts it, and it stays a
FIXED box so an off-centre Madness check is displaced within it rather than re-centred.

Whether the 10 px is DBD drawing the check high or our `content_rect` framing 10 px low is
not settled here; either way it is what the live grab sees, and the live grab is the thing
being reproduced.

The crop scales with frame height the way the live grab does, so a 720p clip is cropped at
149 px and resized to 224 rather than being cropped at 224 and silently framed wrong.
`events.json`'s `size` is the ingest target, NOT the source resolution — read it from the
decoder instead. A first pass that trusted that field cropped a 1280x720 clip as if it were
1080p and reported "no check detected" on all 17 revolutions of a check that is plainly
there.
"""

import argparse
import json
import os
import sys
from collections import Counter

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.AI_model import AI_model
from dbd.utils.monitoring_window import TRAINING_REFERENCE_CROP, TRAINING_REFERENCE_HEIGHT
from dbd.utils.needle_tracker import ROUND_TRIP_MS, TrackerState, decide, mark_fired, observe

TRACKED_PREDS = (1, 2, 3, 4, 5, 6, 7)  # keep in step with autorun.py
CROP = int(TRAINING_REFERENCE_CROP)  # the constant is a float, and cv2.resize wants ints
RING_ABOVE_CENTRE_PX = 10.0  # measured live: ring at y=101.75 in a 224 crop, not 112


def centre_crop(bgr, side, offset_y):
    """The region the live grab would take, resized to training size if the stream is not 1080p."""

    h, w = bgr.shape[:2]
    y0, x0 = h // 2 - side // 2 + offset_y, w // 2 - side // 2
    crop = bgr[y0:y0 + side, x0:x0 + side]
    if side != CROP:
        crop = cv2.resize(crop, (CROP,) * 2, interpolation=cv2.INTER_CUBIC)
    return crop


def replay_check(model, cap, check, fps, side, step, lead_ms, offset_y):
    """What the armed loop would do on one check. Returns (action, prediction counts)."""

    cap.set(cv2.CAP_PROP_POS_FRAMES, check["frame0"])
    tracker, t0, preds = None, None, []

    for i in range(check["frame0"], check["frame1"] + 1):
        ok, bgr = cap.read()
        if not ok:
            break
        if (i - check["frame0"]) % step:
            continue

        t_ms = (i - check["frame0"]) / fps * 1000.0
        crop = centre_crop(bgr, side, offset_y)
        pred, desc, _, should_hit = model.predict(crop[:, :, ::-1])  # predict wants RGB
        preds.append(desc)

        if pred not in TRACKED_PREDS:
            continue
        if tracker is None:
            tracker, t0 = TrackerState(), t_ms
        tracker = observe(tracker, crop, t_ms - t0)
        decision = decide(tracker, t_ms - t0, lead_ms)

        if decision.press_at_ms is not None:
            mark_fired(tracker, t_ms - t0)
            return (f"FIRE predictive — aiming {decision.target_deg:.1f} deg, "
                    f"press at t={decision.press_at_ms:.0f} ms ({desc})"), preds
        if should_hit and decision.may_react:
            return (f"HIT reactive ({desc}) — tracker stood down: "
                    f"{decision.reason}"), preds

    return "NO PRESS", preds


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("events", help="recordings_video/<clip>/events.json")
    p.add_argument("--model", default="models/model.onnx")
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--round-trip-ms", type=float, default=ROUND_TRIP_MS)
    p.add_argument("--ring-above-centre", type=float, default=RING_ABOVE_CENTRE_PX,
                   help="px a centred check's ring sits above the live crop centre; the crop "
                        "is shifted down by this so the clip is framed as live frames it. "
                        "0 crops the frame's geometric centre, which is NOT what live sees")
    p.add_argument("--fps", type=float, default=0.0,
                   help="feed frames at about this rate; 0 uses every frame. The live loop "
                        "runs at 33-41 fps, so a 60 fps clip fed whole gives the tracker "
                        "more samples than it will ever have")
    args = p.parse_args()

    events = json.load(open(args.events))
    cap = cv2.VideoCapture(events["video"])
    if not cap.isOpened():
        sys.exit(f"cannot open {events['video']}")

    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = events["fps"]
    scale = height / TRAINING_REFERENCE_HEIGHT
    side = max(8, int(round(TRAINING_REFERENCE_CROP * scale)))
    offset_y = int(round(args.ring_above_centre * scale))
    step = 1 if args.fps <= 0 else max(1, round(fps / args.fps))

    print(f"{events['video']}  {width}x{height} @{fps:.1f}  centre crop {side} px -> "
          f"{CROP}  crop shifted {offset_y:+d} px  feeding {fps / step:.0f} fps  "
          f"lead {args.round_trip_ms:.0f} ms")

    model = AI_model(model_path=args.model, use_gpu=False,
                     nb_cpu_threads=args.threads, monitoring=None)
    print(f"provider: {model.check_provider()}\n")

    pressed = 0
    for check in events["checks"]:
        action, preds = replay_check(model, cap, check, fps, side, step,
                                     args.round_trip_ms, offset_y)
        pressed += action != "NO PRESS"
        top = ", ".join(f"{d} x{n}" for d, n in Counter(preds).most_common(3))
        print(f"{check['dir']}  off {check['off_px']:5.1f} px  "
              f"{'centred' if check['centred'] else 'MADNESS'}  "
              f"{check['direction']} {check['rate_deg_s']:+.0f} deg/s")
        print(f"   classifier: {top}")
        print(f"   -> {action}\n")

    print(f"{pressed} of {len(events['checks'])} checks would have been pressed")
    cap.release()


if __name__ == "__main__":
    main()
