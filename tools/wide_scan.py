"""DIAGNOSTIC: find WHERE skill checks actually appear on screen.

The production path crops 224x224 from the centre because the upstream dataset was
collected that way. If skill checks also render off-centre, that crop never contains
their pixels and the model never gets a chance to classify them — a silent miss, not
an error. Training coverage cannot fix a cropping problem.

This slides a 224 window across the whole streamed frame and records where detections
fire. It is deliberately slow and is NOT for live play: it exists to produce the
position distribution needed to design the real capture region.

    python tools/wide_scan.py --seconds 300

Measured costs on this machine: full-frame capture 26.9 ms (only 4.7 ms more than a
224 crop), inference 2.02 ms per tile. Frame time is therefore dominated by tile count:

    stride 224 (no overlap)  ~32 tiles  ~91 ms/frame  ~11 fps
    stride 112 (50% overlap) ~144 tiles ~317 ms/frame ~3 fps

A skill check is on screen for roughly a second, so even the slow setting samples it a
few times. Overlap matters because a check straddling a tile boundary can be missed by
both neighbours.

On every detection it writes an annotated full frame to <out>/, and on exit it prints a
histogram of hit locations.
"""

import argparse
import os
import sys
from collections import Counter
from time import monotonic, sleep, strftime

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.AI_model import AI_model
from dbd.utils.focus_watcher import FocusWatcher
from dbd.utils.monitoring_window import Monitoring_window

TILE = 224


def parse_args():
    p = argparse.ArgumentParser(description="Locate skill checks across the whole frame")
    p.add_argument("--window", default="Moonlight")
    p.add_argument("--model", default="models/model.onnx")
    p.add_argument("--aspect", default="16:9")
    p.add_argument("--stride", type=int, default=112,
                   help="tile step in px; 112 = 50%% overlap, 224 = none (faster)")
    p.add_argument("--seconds", type=float, default=300.0)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--out", default="scan_hits")
    p.add_argument("--save-all", action="store_true",
                   help="also save frames where nothing fired (debugging the scanner)")
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


def tile_origins(width, height, stride):
    """Top-left corners covering the frame, always including the right/bottom edges."""

    xs = list(range(0, max(width - TILE, 0) + 1, stride))
    ys = list(range(0, max(height - TILE, 0) + 1, stride))
    if xs and xs[-1] != width - TILE and width >= TILE:
        xs.append(width - TILE)
    if ys and ys[-1] != height - TILE and height >= TILE:
        ys.append(height - TILE)
    return [(x, y) for y in ys for x in xs]


def main():
    args = parse_args()
    os.makedirs(args.out, exist_ok=True)

    monitor = Monitoring_window(
        window_query=args.window,
        crop_size=TILE,
        stream_aspect=parse_aspect(args.aspect),
        full_window=True,          # grab the entire content area, then tile it ourselves
    )
    content = monitor.content
    origins = tile_origins(content["width"], content["height"], args.stride)

    centre = (content["width"] // 2 - TILE // 2, content["height"] // 2 - TILE // 2)
    log(f"content {content['width']}x{content['height']}, {len(origins)} tiles, stride {args.stride}")
    log(f"production centre crop would be at {centre}")

    model = AI_model(args.model, use_gpu=False, nb_cpu_threads=args.threads, monitoring=monitor)
    watcher = FocusWatcher(query=args.window)

    hits = Counter()
    frames = 0
    detections = 0
    deadline = monotonic() + args.seconds
    log(f"scanning for {args.seconds:.0f}s — switch to the stream and play")

    try:
        while monotonic() < deadline:
            if not watcher.is_active():
                sleep(0.2)
                continue

            frame = np.array(monitor.get_raw_frame(), dtype=np.uint8)
            frame_rgb = np.flip(frame[:, :, :3], 2)
            frames += 1

            found = []
            for (x, y) in origins:
                tile = np.ascontiguousarray(frame_rgb[y:y + TILE, x:x + TILE])
                if tile.shape[:2] != (TILE, TILE):
                    continue
                pred, desc, probs, should_hit = model.predict(tile)
                if should_hit:
                    found.append((x, y, desc, float(max(probs.values()))))

            if found or args.save_all:
                annotated = np.ascontiguousarray(frame[:, :, :3])  # BGR for cv2
                # green = production centre crop, red = where a detection fired
                cv2.rectangle(annotated, centre, (centre[0] + TILE, centre[1] + TILE), (0, 200, 0), 2)
                for (x, y, desc, conf) in found:
                    cv2.rectangle(annotated, (x, y), (x + TILE, y + TILE), (0, 0, 255), 3)
                    cv2.putText(annotated, f"{desc} {conf:.2f}", (x, max(y - 8, 12)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

                if found:
                    detections += 1
                    stamp = strftime("%H%M%S")
                    path = os.path.join(args.out, f"hit_{stamp}_{detections:03d}.png")
                    # Save the CLEAN frame too: the annotations sit exactly on top of
                    # the skill check, destroying the pixels any later analysis needs.
                    clean_path = os.path.join(args.out, f"hit_{stamp}_{detections:03d}_clean.png")
                    cv2.imwrite(clean_path, np.ascontiguousarray(frame[:, :, :3]))
                    cv2.imwrite(path, annotated)
                    for (x, y, desc, conf) in found:
                        offset = (x - centre[0], y - centre[1])
                        hits[(x, y)] += 1
                        log(f"HIT tile=({x},{y}) offset_from_centre={offset} {desc} ({conf:.3f})")
                    log(f"  saved {path}")
                    sleep(0.5)  # same cooldown as production, avoid re-logging one check

    except KeyboardInterrupt:
        log("interrupted")
    finally:
        model.cleanup()

    log(f"scanned {frames} frames, {detections} detections")
    if hits:
        log("hit locations (tile origin -> count):")
        for (pos, count) in hits.most_common():
            offset = (pos[0] - centre[0], pos[1] - centre[1])
            centred = abs(offset[0]) < TILE // 2 and abs(offset[1]) < TILE // 2
            log(f"  {pos}  x{count}  offset={offset}  {'CENTRE' if centred else 'OFF-CENTRE'}")
    else:
        log("no detections — if you saw skill checks, the scanner itself needs debugging")


if __name__ == "__main__":
    main()
