"""Verify what the model actually sees inside a streamed window.

Writes three files so you can eyeball the framing:

    <out>/window.png   the full window, with the capture box drawn on it
    <out>/crop.png     the exact 224x224 frame handed to the model
    <out>/report.json  resolved geometry + the model's prediction on that frame

IMPORTANT: screen capture reads the framebuffer that is on the display *right now*.
A fullscreen Moonlight sits on its own macOS Space, so run this while that Space is
the one you are looking at, or you will calibrate against your desktop instead.

    python tools/calibrate_window.py --countdown 5

Use --zoom to test alternate crop scales if detection is unreliable; 1.0 reproduces
the geometry the model was trained on.
"""

import argparse
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.utils.monitoring_window import Monitoring_window


def parse_args():
    p = argparse.ArgumentParser(description="Calibrate skill-check capture against a window")
    p.add_argument("--window", default="Moonlight", help="substring of the window owner/title")
    p.add_argument("--aspect", default="16:9", help="streamed game aspect ratio, or 'fill'")
    p.add_argument("--zoom", type=float, default=1.0, help="scale the crop (1.0 = training geometry)")
    p.add_argument("--countdown", type=int, default=0, help="seconds to wait before grabbing")
    p.add_argument("--model", default="models/model.onnx")
    p.add_argument("--out", default="calibration")
    return p.parse_args()


def parse_aspect(text):
    if text.lower() in ("fill", "none"):
        return None
    if ":" in text:
        w, h = text.split(":")
        return float(w) / float(h)
    return float(text)


def countdown(seconds):
    from time import sleep

    for remaining in range(seconds, 0, -1):
        print(f"  grabbing in {remaining}s — switch to the stream now", end="\r", flush=True)
        sleep(1)
    print(" " * 60, end="\r")


def main():
    args = parse_args()
    os.makedirs(args.out, exist_ok=True)

    monitor = Monitoring_window(
        window_query=args.window,
        crop_size=224,
        stream_aspect=parse_aspect(args.aspect),
        zoom=args.zoom,
    )
    geometry = monitor.describe()
    print(json.dumps(geometry, indent=1))

    if args.countdown:
        countdown(args.countdown)

    # The 224x224 frame the model actually consumes
    monitor.start()
    crop = monitor.get_frame_np()
    monitor.stop()

    # The whole window, for reference, with the capture box drawn on it
    full = Monitoring_window(
        window_query=args.window,
        crop_size=224,
        stream_aspect=parse_aspect(args.aspect),
        full_window=True,
    )
    full.start()
    # slicing off the alpha leaves a non-contiguous view, which cv2.rectangle rejects
    window_img = np.ascontiguousarray(np.array(full.get_raw_frame(), dtype=np.uint8)[:, :, :3])  # BGR
    full.stop()

    box, content = monitor.region, monitor.content
    x0 = box["left"] - content["left"]
    y0 = box["top"] - content["top"]
    cv2.rectangle(window_img, (x0, y0), (x0 + box["width"], y0 + box["height"]), (0, 0, 255), 2)

    cv2.imwrite(os.path.join(args.out, "window.png"), window_img)
    cv2.imwrite(os.path.join(args.out, "crop.png"), cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))

    prediction = None
    if os.path.exists(args.model):
        from dbd.AI_model import AI_model

        model = AI_model(model_path=args.model, use_gpu=False, monitoring=monitor)
        model.monitor.stop()
        pred, desc, probs, should_hit = model.predict(crop)
        prediction = {
            "pred": pred,
            "desc": desc,
            "should_hit": bool(should_hit),
            "confidence": round(float(max(probs.values())), 4),
        }
        print(f"\nprediction: {desc}  (hit={should_hit}, conf={prediction['confidence']})")
        print("a black/desktop frame predicting 'None' means you calibrated the wrong Space")

    report = {"geometry": geometry, "zoom": args.zoom, "prediction": prediction}
    with open(os.path.join(args.out, "report.json"), "w") as f:
        json.dump(report, f, indent=1)

    print(f"\nwrote {args.out}/window.png, {args.out}/crop.png, {args.out}/report.json")


if __name__ == "__main__":
    main()
