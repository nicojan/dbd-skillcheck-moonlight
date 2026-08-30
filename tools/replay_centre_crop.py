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
from dbd.utils.wide_capture import centre_slice, look, to_model_size, wide_geometry

TRACKED_PREDS = (1, 2, 3, 4, 5, 6, 7)  # keep in step with autorun.py
CROP = int(TRAINING_REFERENCE_CROP)  # the constant is a float, and cv2.resize wants ints
RING_ABOVE_CENTRE_PX = 10.0  # measured live: ring at y=101.75 in a 224 crop, not 112
DEFAULT_FRAME_MS = 30.0      # fallback spacing when a recorded session has no manifest


def centre_crop(bgr, side, offset_y):
    """The region the live grab would take, resized to training size if the stream is not 1080p."""

    h, w = bgr.shape[:2]
    y0, x0 = h // 2 - side // 2 + offset_y, w // 2 - side // 2
    crop = bgr[y0:y0 + side, x0:x0 + side]
    if side != CROP:
        crop = cv2.resize(crop, (CROP,) * 2, interpolation=cv2.INTER_CUBIC)
    return crop


def replay_stream(model, frames, lead_ms, prior=None):
    """What the armed loop would do, given a stream of crops. Returns (action, preds).

    `frames` yields (t_ms, crop_bgr) already at model size. `prior` is where the check's
    ring sits inside those crops, in crop pixels — the tracker refines about it. Leave it
    None for a centred check and the tracker's own constant is used.

    This is the whole of the live decision: same TRACKED_PREDS gate, same observe/decide,
    same may_react fallback. Both the video path and the recorded-frames path go through
    it so neither can drift into answering a different question from the other.
    """

    tracker, t0, preds = None, None, []

    for t_ms, crop in frames:
        pred, desc, _, should_hit = model.predict(crop[:, :, ::-1])  # predict wants RGB
        preds.append(desc)

        if pred not in TRACKED_PREDS:
            continue
        if tracker is None:
            tracker = TrackerState() if prior is None else TrackerState(centre=prior)
            t0 = t_ms
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


def video_frames(cap, check, fps, side, step, offset_y):
    """(t_ms, centre crop) for one check in a source video."""

    cap.set(cv2.CAP_PROP_POS_FRAMES, check["frame0"])
    for i in range(check["frame0"], check["frame1"] + 1):
        ok, bgr = cap.read()
        if not ok:
            return
        if (i - check["frame0"]) % step:
            continue
        yield (i - check["frame0"]) / fps * 1000.0, centre_crop(bgr, side, offset_y)


def replay_check(model, cap, check, fps, side, step, lead_ms, offset_y):
    """What the armed loop would do on one check of a source video."""

    return replay_stream(model, video_frames(cap, check, fps, side, step, offset_y), lead_ms)


def video_wide(model, cap, check, fps, step, geometry, lead_ms, offset_y):
    """The same, through the wide path — the box sliced out of the clip's own frames.

    The only Madness footage in the repo is also Merciless Storm footage, so this is the
    one place Doctor-plus-Storm can be asked about at all. The clip draws a centred check
    at the frame's geometric centre while live puts the ring 10 px above the crop centre,
    so the box is slid down by `offset_y` — the same correction `centre_crop` applies to
    its crop, applied to the box instead, which keeps the centre slice equal to it.
    """

    def predict(crop):
        return model.predict(crop[:, :, ::-1])  # predict wants RGB

    region = geometry.region
    cap.set(cv2.CAP_PROP_POS_FRAMES, check["frame0"])
    tracker, t0, held, origin, preds = None, None, None, None, []

    for i in range(check["frame0"], check["frame1"] + 1):
        ok, bgr = cap.read()
        if not ok:
            break
        if (i - check["frame0"]) % step:
            continue

        top = region["top"] + offset_y
        wide = bgr[top:top + region["height"],
                   region["left"]:region["left"] + region["width"]]
        if wide.shape[:2] != (region["height"], region["width"]):
            break                                   # the box ran off this clip's frame

        t_ms = (i - check["frame0"]) / fps * 1000.0
        seen = look(predict, wide, geometry, held)
        held = seen.held
        preds.append(seen.desc)
        if origin is not None and seen.origin != origin:
            tracker = None
        origin = seen.origin

        if seen.pred not in TRACKED_PREDS:
            continue
        if tracker is None:
            tracker, t0 = TrackerState(centre=seen.prior), t_ms
        tracker = observe(tracker, seen.crop, t_ms - t0)
        decision = decide(tracker, t_ms - t0, lead_ms)

        if decision.press_at_ms is not None:
            mark_fired(tracker, t_ms - t0)
            return (f"FIRE predictive — aiming {decision.target_deg:.1f} deg, "
                    f"press at t={decision.press_at_ms:.0f} ms ({seen.desc})"), preds
        if seen.should_hit and decision.may_react:
            return (f"HIT reactive ({seen.desc}) — tracker stood down: "
                    f"{decision.reason}"), preds

    return "NO PRESS", preds


def replay_video(args, model):
    """Every check in an ingested clip, cropped the way the live grab crops."""

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
          f"lead {args.round_trip_ms:.0f} ms\n")

    geometry = None
    if args.framing == "wide":
        geometry = wide_geometry({"left": 0, "top": 0, "width": width, "height": height})
        print(f"  wide box {geometry.region}, slid {offset_y:+d} px\n")

    pressed = 0
    for check in events["checks"]:
        if geometry is not None:
            action, preds = video_wide(model, cap, check, fps, step, geometry,
                                       args.round_trip_ms, offset_y)
        else:
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



# --- recorded frame sessions ---------------------------------------------------------
#
# `record_frames.py` captures the whole content rect, and `Monitoring_window` resolves that
# same rect live, so a recorded session replays the live grab exactly — including the wide
# box, which is a slice of it. This is the only way to ask "would the bot press here?" of a
# Madness check: there is no clip of one, and the answer turns on capture geometry rather
# than on the model.

def session_geometry(session):
    """Content rect and wide-box geometry for a recorded session.

    The recorded JPEG *is* the content rect, so within the image the rect starts at the
    origin — but its height still sets the scale, exactly as live.
    """

    first = sorted(n for n in os.listdir(session) if n.lower().endswith((".jpg", ".png")))
    if not first:
        sys.exit(f"no frames in {session}")
    img = cv2.imread(os.path.join(session, first[0]))
    if img is None:
        sys.exit(f"could not read {first[0]}")
    height, width = img.shape[:2]
    content = {"left": 0, "top": 0, "width": width, "height": height}
    return content, wide_geometry(content)


def frame_times(session, indices):
    """(index, t_ms) for a run of frames, using the recorder's own timestamps."""

    path = os.path.join(session, "manifest.jsonl")
    stamps = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                if line.strip():
                    record = json.loads(line)
                    stamps[int(os.path.splitext(record["frame"])[0])] = record.get("t_ms")
    return [(i, stamps.get(i)) for i in indices]


def session_frames(session, indices, geometry, framing, tile=None):
    """(t_ms, crop) for a run of recorded frames, under one fixed framing.

    Two framings live here, both of which hand over a crop chosen without looking:

      centre  what the production path grabs today
      tile    a fixed grid tile, the way a tiling scanner would hand it over

    The `wide` path is not a framing in this sense — it decides what to crop by looking —
    so it goes through `wide_frames` and `wide_capture.look` instead.
    """

    for index, t_ms in frame_times(session, indices):
        wide = read_wide(session, index, geometry)
        if wide is None:
            continue
        if framing == "centre":
            yield t_ms, to_model_size(centre_slice(wide, geometry))
        else:
            region = geometry.region
            x, y = tile[0] - region["left"], tile[1] - region["top"]
            yield t_ms, to_model_size(wide[y:y + geometry.crop_side,
                                           x:x + geometry.crop_side])


def read_wide(session, index, geometry):
    """The wide box out of one recorded frame, or None if the frame is unreadable."""

    img = cv2.imread(os.path.join(session, f"{index:06d}.jpg"))
    if img is None:
        return None
    region = geometry.region
    return img[region["top"]:region["top"] + region["height"],
               region["left"]:region["left"] + region["width"]]


def replay_wide(model, session, indices, geometry, lead_ms):
    """The shipped wide path over a run of recorded frames. Returns (action, preds).

    Mirrors the armed loop the way `replay_stream` does, and goes through the same
    `wide_capture.look` the live loop calls, so the two cannot answer different questions.
    """

    def predict(crop):
        return model.predict(crop[:, :, ::-1])  # predict wants RGB

    tracker, t0, held, preds, prior, origin = None, None, None, [], None, None

    for index, t_ms in frame_times(session, indices):
        wide = read_wide(session, index, geometry)
        if wide is None:
            continue

        seen = look(predict, wide, geometry, held)
        held = seen.held
        preds.append(seen.desc)
        if prior is None and held is not None:
            prior = held.prior

        # The crop moved, so every angle measured in the old one is in a different frame
        # of reference. Start again rather than fit a line through two conventions.
        if origin is not None and seen.origin != origin:
            tracker = None
        origin = seen.origin

        if seen.pred not in TRACKED_PREDS:
            continue
        if tracker is None:
            tracker, t0 = TrackerState(centre=seen.prior), t_ms
        tracker = observe(tracker, seen.crop, t_ms - t0)
        decision = decide(tracker, t_ms - t0, lead_ms)

        if decision.press_at_ms is not None:
            mark_fired(tracker, t_ms - t0)
            return (f"FIRE predictive — aiming {decision.target_deg:.1f} deg, "
                    f"press at t={decision.press_at_ms:.0f} ms ({seen.desc})"), preds, prior
        if seen.should_hit and decision.may_react:
            return (f"HIT reactive ({seen.desc}) — tracker stood down: "
                    f"{decision.reason}"), preds, prior

    return "NO PRESS", preds, prior


def replay_session(args, model):
    """Replay one framing over a run of recorded frames around each named frame."""

    content, geometry = session_geometry(args.frames)
    tile = tuple(int(v) for v in args.tile.split(",")) if args.tile else None
    if args.framing == "tile" and tile is None:
        sys.exit("--framing tile needs --tile X,Y (the grid origin, in content pixels)")

    print(f"{args.frames}  content {content['width']}x{content['height']}  "
          f"framing {args.framing}  lead {args.round_trip_ms:.0f} ms")
    print(f"  {json.dumps(geometry.describe())}\n")

    pressed = 0
    for centre_frame in args.at:
        indices = range(max(centre_frame - args.before, 0), centre_frame + args.after + 1)
        if args.framing == "wide":
            action, preds, prior = replay_wide(model, args.frames, indices, geometry,
                                               args.round_trip_ms)
        else:
            stream = session_frames(args.frames, indices, geometry, args.framing, tile)
            action, preds = replay_stream(model, stream, args.round_trip_ms)
            prior = None
        pressed += action != "NO PRESS"
        top = ", ".join(f"{d} x{n}" for d, n in Counter(preds).most_common(3))
        where = "" if prior is None else f"  ring prior ({prior[0]:.1f}, {prior[1]:.1f})"
        print(f"frame {centre_frame}  {len(preds)} frames{where}")
        print(f"   classifier: {top or 'nothing'}")
        print(f"   -> {action}\n")

    print(f"{pressed} of {len(args.at)} checks would have been pressed")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("events", nargs="?", help="recordings_video/<clip>/events.json")
    p.add_argument("--frames", help="a session dir written by record_frames.py, replayed "
                                    "instead of a clip; needs --at")
    p.add_argument("--at", type=int, nargs="+", default=(),
                   help="frame indices to replay around, one run each")
    p.add_argument("--before", type=int, default=25, help="frames replayed before each --at")
    p.add_argument("--after", type=int, default=25, help="frames replayed after each --at")
    p.add_argument("--framing", choices=("centre", "tile", "wide"), default=None,
                   help="how the crop handed to the model is chosen; see session_frames. "
                        "Defaults to `centre` for a clip, which is the path this tool was "
                        "written to test, and to `wide` for a frame session, which is the "
                        "path that needs one. Pass it explicitly to compare the two")
    p.add_argument("--tile", help="grid origin X,Y for --framing tile, in content pixels")
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

    if not args.frames and not args.events:
        p.error("give an events.json, or --frames SESSION --at N [N ...]")
    if args.frames and not args.at:
        p.error("--frames needs --at N [N ...]")
    if args.framing is None:
        args.framing = "wide" if args.frames else "centre"

    model = AI_model(model_path=args.model, use_gpu=False,
                     nb_cpu_threads=args.threads, monitoring=None)
    print(f"provider: {model.check_provider()}\n")

    if args.frames:
        replay_session(args, model)
    else:
        replay_video(args, model)


if __name__ == "__main__":
    main()
