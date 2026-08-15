"""Record clean frame sequences of real skill checks, for building the predictive tracker.

Reactive firing lands ~72 ms late because we press when we SEE the needle. Predicting
instead requires knowing the needle's angle over time, and that cannot be developed
from a single still — it needs the sweep as a sequence.

A ring buffer holds the last N frames continuously. When the model reports any skill
check class, the buffer is flushed to disk along with the frames that follow, so each
capture contains the approach, the zone crossing, and the aftermath.

    python tools/record_checks.py --seconds 300

Frames are saved raw and unannotated (a previous tool drew debug boxes over the exact
pixels it was meant to preserve). A manifest.json records each frame's timestamp and
the model's prediction, which gives both the CV target and ground truth for checking a
velocity estimate later.

Presses nothing. This is a data collection pass.
"""

import argparse
import json
import os
import sys
from collections import deque
from time import monotonic, sleep, strftime

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.AI_model import AI_model
from dbd.utils.focus_watcher import FocusWatcher
from dbd.utils.monitoring_window import Monitoring_window

NONE_CLASS = 0


def parse_args():
    p = argparse.ArgumentParser(description="Record skill check frame sequences")
    p.add_argument("--window", default="Moonlight")
    p.add_argument("--model", default="models/model.onnx")
    p.add_argument("--aspect", default="16:9")
    p.add_argument("--pre", type=int, default=25, help="frames kept before the trigger")
    p.add_argument("--post", type=int, default=35, help="frames recorded after the trigger")
    p.add_argument("--seconds", type=float, default=300.0)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--out", default="recordings")
    return p.parse_args()


def parse_aspect(text):
    if text.lower() in ("fill", "none"):
        return None
    if ":" in text:
        w, h = text.split(":")
        return float(w) / float(h)
    return float(text)


def log(message):
    print(f"[{strftime('%H:%M:%S')}] {message}", flush=True)


def flush_capture(out_dir, index, buffered, model, args, monitor, watcher):
    """Write the pre-trigger buffer, then keep grabbing for `post` more frames."""

    capture_dir = os.path.join(out_dir, f"check_{index:03d}")
    os.makedirs(capture_dir, exist_ok=True)

    records = []
    t0 = buffered[0][0] if buffered else monotonic()

    for (timestamp, frame, pred, desc, conf) in buffered:
        records.append((timestamp - t0, frame, pred, desc, conf))

    for _ in range(args.post):
        if not watcher.is_active():
            break
        timestamp = monotonic()
        frame = monitor.get_frame_np()
        pred, desc, probs, _ = model.predict(frame)
        records.append((timestamp - t0, frame, pred, desc, float(max(probs.values()))))

    manifest = []
    for i, (offset_s, frame, pred, desc, conf) in enumerate(records):
        name = f"{i:03d}.png"
        cv2.imwrite(os.path.join(capture_dir, name), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        manifest.append({
            "frame": name,
            "t_ms": round(offset_s * 1000, 1),
            "pred": pred,
            "desc": desc,
            "confidence": round(conf, 4),
        })

    with open(os.path.join(capture_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)

    span = manifest[-1]["t_ms"] if manifest else 0
    classes = sorted({m["desc"] for m in manifest if m["pred"] != NONE_CLASS})
    log(f"  saved {len(manifest)} frames over {span:.0f} ms -> {capture_dir}")
    log(f"  classes seen: {classes}")


def main():
    args = parse_args()
    os.makedirs(args.out, exist_ok=True)

    monitor = Monitoring_window(
        window_query=args.window,
        crop_size=224,
        stream_aspect=parse_aspect(args.aspect),
    )
    log(f"capture region {monitor.region}")

    model = AI_model(args.model, use_gpu=False, nb_cpu_threads=args.threads, monitoring=monitor)
    watcher = FocusWatcher(query=args.window)

    buffer = deque(maxlen=args.pre)
    captured = 0
    deadline = monotonic() + args.seconds
    log(f"recording for {args.seconds:.0f}s — play normally, nothing will be pressed")

    try:
        while monotonic() < deadline:
            if not watcher.is_active():
                sleep(0.2)
                continue

            timestamp = monotonic()
            frame = monitor.get_frame_np()
            pred, desc, probs, _ = model.predict(frame)
            confidence = float(max(probs.values()))
            buffer.append((timestamp, frame.copy(), pred, desc, confidence))

            if pred != NONE_CLASS:
                captured += 1
                log(f"skill check #{captured}: {desc} ({confidence:.3f})")
                flush_capture(args.out, captured, list(buffer), model, args, monitor, watcher)
                buffer.clear()
                sleep(1.0)  # let this skill check finish before arming again

    except KeyboardInterrupt:
        log("interrupted")
    finally:
        model.cleanup()

    log(f"done — {captured} skill checks recorded to {args.out}/")
    if captured:
        log("each folder holds the approach, the zone crossing, and manifest.json")


if __name__ == "__main__":
    main()
