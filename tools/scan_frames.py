"""OFFLINE: sweep a 224 tile across recorded frames to find WHERE skill checks appear.

Companion to `record_frames.py`. That tool captures full frames at play speed with no
inference; this one does the expensive part afterwards, when nothing is waiting on it.

Why not just use `wide_scan.py`: that one tiles live at ~3 fps, so a check on screen for
about a second gets sampled a couple of times and can be missed entirely. Here every
recorded frame is scanned, however long it takes.

    .venv/bin/python tools/scan_frames.py --frames frames/session_20260811_143000

Two things this fixes relative to `wide_scan.py`:

  * Tiles are sized from the recorded content height, not hardcoded to 224. The model was
    trained on checks occupying 224/1080 of the frame, so on any content height other than
    1080 a raw 224 tile is the wrong crop and the check arrives at the wrong scale.
  * Detections are grouped into *events*. One check spans many consecutive frames, so raw
    tile counts overstate how often it actually happened.

Writes annotated PNGs for the frames that fired and prints a position histogram.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from time import monotonic, strftime

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.AI_model import AI_model
from dbd.utils import bout_session
from dbd.utils.monitoring_mss import Monitoring
from tools.analyse_needle import MIN_NEEDLE_STRENGTH

MODEL_INPUT = 224
TRAINING_REFERENCE_HEIGHT = 1080.0
NONE_CLASS = 0
EVENT_GAP_FRAMES = 5   # frames of silence at a position before it counts as a new check
MIN_RUN_FRAMES = 3     # need 2+ steps to tell coherent sweeping from random jitter
MIN_COHERENCE = 0.8    # net rotation / path length; a real needle never reverses
RING_MIN_RADIUS = 60   # the check ring in a 224 tile; smaller circles are menu chrome
RING_MAX_RADIUS = 110


class NullMonitoring(Monitoring):
    """AI_model insists on a monitor and start()s it. Offline there is no screen to grab."""

    def start(self):
        pass

    def stop(self):
        pass

    def get_frame_np(self):
        raise RuntimeError("NullMonitoring holds no screen; frames come from disk")


def parse_args():
    p = argparse.ArgumentParser(description="Offline tile sweep over recorded frames")
    p.add_argument("--frames", required=True, help="session dir written by record_frames.py")
    p.add_argument("--model", default="models/model.onnx")
    p.add_argument("--stride-frac", type=float, default=0.5,
                   help="tile step as a fraction of tile size; 0.5 = 50%% overlap")
    p.add_argument("--min-conf", type=float, default=0.90,
                   help="confidence floor; thousands of frames x ~144 tiles surfaces weak false positives")
    p.add_argument("--hits-only", action="store_true",
                   help="only count classes the bot would act on, not 'out'/'frontier' sightings")
    p.add_argument("--every", type=int, default=1, help="scan every Nth frame (speed dial)")
    p.add_argument("--max-event-frames", type=int, default=25,
                   help="detections persisting longer than this are static UI, not skill checks; "
                        "the loadout menu's red perk rings classify as repair-heal at 1.00 confidence "
                        "and would otherwise look like the most solid detection in the session")
    p.add_argument("--min-advance-deg", type=float, default=15.0,
                   help="a run must sweep at least this many degrees to count as a real check; "
                        "static UI (bloodweb nodes, perk icons, score screens) sweeps ~0 and "
                        "flickers, which defeats any duration-based filter")
    p.add_argument("--limit", type=int, default=0, help="stop after N frames (0 = all)")
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--out", default=None, help="where to write annotated hits (default <frames>/hits)")
    return p.parse_args()


def log(message):
    print(f"[{strftime('%H:%M:%S')}] {message}", flush=True)


def tile_origins(width, height, tile, stride):
    """Top-left corners covering the frame, always including the right/bottom edges."""

    xs = list(range(0, max(width - tile, 0) + 1, stride))
    ys = list(range(0, max(height - tile, 0) + 1, stride))
    if width >= tile and xs and xs[-1] != width - tile:
        xs.append(width - tile)
    if height >= tile and ys and ys[-1] != height - tile:
        ys.append(height - tile)
    return [(x, y) for y in ys for x in xs]


def load_frame_list(session):
    """Frame names in recorded order, from the manifest if present, else from disk."""

    manifest_path = os.path.join(session, "manifest.jsonl")
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            records = [json.loads(line) for line in f if line.strip()]
        return [(r["frame"], r.get("t_ms")) for r in records]

    names = sorted(n for n in os.listdir(session) if n.lower().endswith((".jpg", ".png")))
    return [(n, None) for n in names]


def scan_frame(frame_rgb, origins, tile, model, min_conf, hits_only):
    """Every tile that classified as a skill check. Returns (x, y, pred, desc, conf)."""

    found = []
    for (x, y) in origins:
        patch = frame_rgb[y:y + tile, x:x + tile]
        if patch.shape[:2] != (tile, tile):
            continue
        if tile != MODEL_INPUT:
            # The model wants 224 exactly (ONNX input is pinned [1,3,224,224]); the tile is
            # sized in *screen* px so the check fills the same fraction it did in training.
            patch = cv2.resize(patch, (MODEL_INPUT, MODEL_INPUT), interpolation=cv2.INTER_CUBIC)
        patch = np.ascontiguousarray(patch)

        pred, desc, probs, should_hit = model.predict(patch)
        if pred == NONE_CLASS:
            continue
        if hits_only and not should_hit:
            continue
        conf = float(max(probs.values()))
        if conf < min_conf:
            continue
        found.append((x, y, pred, desc, conf))

    return found


def suppress_overlaps(found, tile):
    """Collapse tiles that are all seeing the SAME check down to one detection.

    At 50% overlap a single centred check lands inside the four tiles around the centre,
    and partially inside their neighbours. Counting tiles therefore invents detections —
    and worse, a neighbour tile sits far enough from centre to be labelled OFF-CENTRE,
    which would fabricate exactly the finding this tool exists to test for.

    Greedy non-max suppression: strongest tile wins, anything overlapping it is the same
    check and is dropped.
    """

    accepted = []
    for candidate in sorted(found, key=lambda f: -f[4]):
        x, y = candidate[0], candidate[1]
        if any(abs(x - ax) < tile and abs(y - ay) < tile for (ax, ay, *_rest) in accepted):
            continue
        accepted.append(candidate)
    return accepted


def locate_ring(patch):
    """(cx, cy, r) of the skill check's ring inside a 224 tile, or None.

    The ring is a strong, well-sized circle. Menu chrome that fools the classifier —
    bloodweb nodes, perk icons, the score screen — either has no circle at this radius or
    none at all, so this doubles as a first-pass reality check on a detection.
    """

    grey = cv2.medianBlur(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), 5)
    found = cv2.HoughCircles(grey, cv2.HOUGH_GRADIENT, dp=1, minDist=100,
                             param1=100, param2=30,
                             minRadius=RING_MIN_RADIUS, maxRadius=RING_MAX_RADIUS)
    if found is None:
        return None
    cx, cy, r = np.round(found[0, 0]).astype(int)
    return int(cx), int(cy), int(r)


def needle_angle_about(patch, ring):
    """Needle angle in degrees (0 = up, clockwise) measured about a given ring centre."""

    cx, cy, r = ring
    b, g, red = (patch[:, :, i].astype(np.float32) for i in range(3))
    redness = red - np.maximum(g, b)

    angles = np.arange(0, 360, 0.5)
    radii = np.arange(int(r * 0.55), int(r * 0.95))
    if len(radii) < 2:
        return 0.0, 0.0

    theta = np.deg2rad(angles)[:, None]
    xs = cx + radii[None, :] * np.sin(theta)
    ys = cy - radii[None, :] * np.cos(theta)
    xi = np.clip(np.round(xs).astype(int), 0, patch.shape[1] - 1)
    yi = np.clip(np.round(ys).astype(int), 0, patch.shape[0] - 1)
    profile = redness[yi, xi].mean(axis=1)

    return float(angles[int(np.argmax(profile))]), float(profile.max())


def needle_advance(session, pos, frame_names, tile):
    """(total degrees swept, directional coherence, frames used) for a run.

    Duration alone cannot separate a real check from menu chrome: detection on a static
    element FLICKERS, so minutes of bloodweb get chopped into many short runs that each
    look check-like. Motion is the honest discriminator — a real needle sweeps ~330 deg/s,
    so even two samples apart show tens of degrees, while a perk icon or bloodweb node
    sits still no matter how long it is on screen.
    """

    x, y = pos
    angles = []
    for name in frame_names:
        img = cv2.imread(os.path.join(session, name))
        if img is None:
            continue
        patch = img[y:y + tile, x:x + tile]
        if patch.shape[:2] != (tile, tile):
            continue
        if tile != MODEL_INPUT:
            patch = cv2.resize(patch, (MODEL_INPUT, MODEL_INPUT), interpolation=cv2.INTER_CUBIC)

        # The angle must be measured about the CHECK's centre, not the tile's. A tile here
        # is a fixed grid position, so the check sits off-centre inside it by up to half a
        # tile — measuring about the tile centre samples an annulus of background and
        # reports no needle at all, which would silently reject genuine checks.
        ring = locate_ring(patch)
        if ring is None:
            continue
        angle, strength = needle_angle_about(patch, ring)
        if strength >= MIN_NEEDLE_STRENGTH:
            angles.append(angle)

    if len(angles) < MIN_RUN_FRAMES:
        # Too short to judge direction. A 2-frame run gives a single step, which is
        # trivially "coherent" — the animated fire on the score screen passes that.
        return 0.0, 0.0, len(angles)

    steps = [(b - a + 540) % 360 - 180 for a, b in zip(angles, angles[1:])]
    total = sum(abs(s) for s in steps)
    # A real needle only ever advances one way, so the net rotation is nearly the whole
    # path length. Flickering highlights on animated UI jump back and forth and cancel.
    coherence = abs(sum(steps)) / total if total else 0.0
    return float(total), float(coherence), len(angles)


def annotate(frame_bgr, found, centre, tile):
    """Copy of the frame with the production crop in green and detections in red."""

    canvas = frame_bgr.copy()  # never draw on the pixels a later pass may need
    cv2.rectangle(canvas, centre, (centre[0] + tile, centre[1] + tile), (0, 200, 0), 2)
    for (x, y, _pred, desc, conf) in found:
        cv2.rectangle(canvas, (x, y), (x + tile, y + tile), (0, 0, 255), 3)
        cv2.putText(canvas, f"{desc} {conf:.2f}", (x, max(y - 8, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    return canvas


def summarise(hits, events, centre, tile, frames_scanned, frames_with_hits, max_event_frames,
              session=None, min_advance_deg=0.0):
    log(f"scanned {frames_scanned} frames, {frames_with_hits} had a detection")
    if not hits:
        log("no detections at all — either no checks were played, or the sweep is broken; "
            "re-run with --min-conf 0.5 --hits-only off to see near-misses")
        return

    log("hit positions (tile origin -> frames / checks / multi-frame checks):")
    off_centre_solid = 0
    off_centre_fleeting = 0
    for (pos, count) in hits.most_common():
        offset = (pos[0] - centre[0], pos[1] - centre[1])
        # Within half a tile of centre means the production crop would have contained it.
        centred = abs(offset[0]) < tile // 2 and abs(offset[1]) < tile // 2
        runs = events[pos]
        # A real check is TRANSIENT: present for ~1s, then gone. Too short (one isolated
        # frame) is probably background noise; too long is static UI, not a skill check.
        candidates = [r for r in runs if 2 <= r[1] <= max_event_frames]
        persistent = [r for r in runs if r[1] > max_event_frames]
        # Duration is not enough — verify the needle actually MOVED.
        if session and min_advance_deg > 0:
            solid, static = [], []
            for r in candidates:
                total, coherence, _n = needle_advance(session, pos, r[2], tile)
                real = total >= min_advance_deg and coherence >= MIN_COHERENCE
                (solid if real else static).append(r)
        else:
            solid, static = candidates, []
        if not centred:
            off_centre_solid += len(solid)
            off_centre_fleeting += len(runs) - len(solid) - len(persistent)
        flags = "CENTRE" if centred else "OFF-CENTRE"
        dropped = len(persistent) + len(static)
        if dropped:
            flags += f"  [{dropped} run(s) dropped as static UI: no needle motion]"
        log(f"  {pos}  x{count} frames / {len(runs)} runs / {len(solid)} check-like  "
            f"offset={offset}  {flags}")

    log("")
    if off_centre_solid:
        log(f"=> {off_centre_solid} multi-frame checks fired OFF-CENTRE. The production 224 crop "
            "misses these entirely; the capture region needs to cover those positions.")
    else:
        log("=> no multi-frame detection landed outside the production centre crop.")
    if off_centre_fleeting:
        log(f"   ({off_centre_fleeting} single-frame off-centre blips ignored as probable false "
            "positives — inspect the annotated PNGs if you want to confirm)")


def main():
    args = parse_args()
    session = args.frames
    if not os.path.isdir(session):
        sys.exit(f"no such session dir: {session}")

    frame_list = load_frame_list(session)
    if args.limit:
        frame_list = frame_list[:args.limit]
    frame_list = frame_list[::max(args.every, 1)]
    if not frame_list:
        sys.exit(f"no frames found in {session}")

    out_dir = args.out or os.path.join(session, "hits")
    os.makedirs(out_dir, exist_ok=True)

    first = cv2.imread(os.path.join(session, frame_list[0][0]))
    if first is None:
        sys.exit(f"could not read {frame_list[0][0]}")
    height, width = first.shape[:2]

    # Tile sized the way the production crop is sized: the model was trained on checks
    # occupying 224/1080 of the frame height, so the tile must track CONTENT height.
    #
    # For a `record_frames.py` session the image is the content rect and the two are the
    # same number. For a bout from `clip_recorder.py` they are not: the image is a 672 px
    # wide box cut from a 1080 px frame, and sizing off the image would give a 139 px tile
    # — checks at the wrong scale, classified wrongly, with nothing to show it had gone
    # wrong. The bout records the content rect it came from, so use that.
    bout = bout_session.load(session)
    content_height = bout["content"]["height"] if bout else height
    tile = max(int(round(MODEL_INPUT * content_height / TRAINING_REFERENCE_HEIGHT)), 8)
    stride = max(int(round(tile * args.stride_frac)), 1)
    origins = tile_origins(width, height, tile, stride)
    if bout:
        # Inside a bout the production crop is at a known offset in the box, not at the
        # box's centre — the box is deliberately offset from the crop.
        cx, cy = bout["geometry"]["centre_in_box"]
        centre = (int(cx), int(cy))
    else:
        centre = (width // 2 - tile // 2, height // 2 - tile // 2)

    if bout:
        log(f"bout: {width}x{height} wide box from {bout['content']['width']}x"
            f"{content_height} content, {len(bout.get('checks', []))} checks")
    log(f"{len(frame_list)} frames at {width}x{height}")
    log(f"tile {tile}px (resized to {MODEL_INPUT} for the model), stride {stride}, {len(origins)} tiles/frame")
    log(f"production centre crop would sit at {centre}")

    model = AI_model(args.model, use_gpu=False, nb_cpu_threads=args.threads,
                     monitoring=NullMonitoring())

    hits = Counter()
    events = defaultdict(list)      # tile origin -> list of event start indices
    last_seen = {}                  # tile origin -> last frame index it fired on
    frames_with_hits = 0
    scanned = 0
    start = monotonic()

    try:
        for index, (name, t_ms) in enumerate(frame_list):
            frame_bgr = cv2.imread(os.path.join(session, name))
            if frame_bgr is None:
                log(f"WARNING: unreadable frame {name}, skipping")
                continue
            frame_rgb = np.flip(frame_bgr, 2)
            scanned += 1

            found = scan_frame(frame_rgb, origins, tile, model, args.min_conf, args.hits_only)
            # One check lights up several overlapping tiles; collapse them or the counts
            # (and the centre/off-centre verdict) are fiction.
            found = suppress_overlaps(found, tile)

            if found:
                frames_with_hits += 1
                for (x, y, _pred, desc, conf) in found:
                    pos = (x, y)
                    hits[pos] += 1
                    # A check spans many frames; only a gap of silence starts a new event.
                    if pos not in last_seen or index - last_seen[pos] > EVENT_GAP_FRAMES:
                        events[pos].append([index, 0, []])
                    events[pos][-1][1] += 1
                    events[pos][-1][2].append(name)
                    last_seen[pos] = index
                    offset = (x - centre[0], y - centre[1])
                    log(f"{name} tile=({x},{y}) offset={offset} {desc} ({conf:.3f})")

                cv2.imwrite(os.path.join(out_dir, f"hit_{index:06d}.png"),
                            annotate(frame_bgr, found, centre, tile))

            if scanned % 100 == 0:
                elapsed = monotonic() - start
                rate = scanned / max(elapsed, 1e-6)
                remaining = (len(frame_list) - scanned) / max(rate, 1e-6)
                log(f"  {scanned}/{len(frame_list)} frames, {rate:.1f} fps, ~{remaining / 60:.1f} min left")

    except KeyboardInterrupt:
        log("interrupted — summarising what was scanned so far")
    finally:
        model.cleanup()

    log("")
    summarise(hits, events, centre, tile, scanned, frames_with_hits, args.max_event_frames,
              session=session, min_advance_deg=args.min_advance_deg)
    if frames_with_hits:
        log(f"annotated frames in {out_dir}/ (originals in {session}/ are untouched)")


if __name__ == "__main__":
    main()
