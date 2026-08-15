"""Turn an arbitrary gameplay recording into the recordings/ dataset format.

Third-party clips are the only practical source for skill checks we cannot get ourselves —
the Doctor's Madness (off-centre, sometimes counter-clockwise) and Merciless Storm. This
finds the checks in an mp4 and writes each one out exactly like tools/record_checks.py
does, so tools/analyse_needle.py and tools/measure_zone.py run on it unchanged.

    .venv/bin/python tools/ingest_video.py videos/merciless-storm.mp4 --out recordings_video

How a check is found, and why each step is here:

  1. Frames are scaled to 1080p height first. Foreign recordings arrive at 720p and 4K, and
     everything downstream (ring radius 65 px, the 224 crop) is calibrated to 1080p.
  2. HoughCircles over a 2x downscale proposes rings; a ring is only kept if a strong red
     radial line sits inside it. Hough alone happily returns perk icons and HUD chrome.
  3. Candidates are clustered by position over time and each cluster is scored for
     DIRECTIONAL COHERENCE. A needle sweeps one way and never reverses; static UI sits
     still and encoder noise jitters. Nothing else separates them reliably -- see the
     off-centre traps in NOTES-local.md.
  4. A cluster is split where the needle jumps discontinuously, because back-to-back checks
     at the same position otherwise merge into one bogus event.
  5. Merciless Storm's needle never resets, so an event spanning several revolutions is cut
     at each full turn. Each piece then holds one zone position, which is what
     measure_zone.py assumes.
  6. The ring centre is refined per event the way measure_zone.py does it -- the base ring
     is a full circle, so at the true centre the angle-median radial profile is a tall
     narrow spike. Hough is only accurate to ~3 px and that error lands straight in the
     needle angle.

Crops place the ring centre at (112, 102), matching what record_checks.py produces, so the
centre priors baked into the analysis tools still apply.
"""

import argparse
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CROP = 224
CROP_CENTRE = (112.0, 102.0)   # where record_checks.py puts the ring inside the crop
TARGET_HEIGHT = 1080           # everything downstream is calibrated to 1080p
ANGLE_STEP = 0.5
ANGLES = np.arange(0, 360, ANGLE_STEP)

MIN_REDNESS = 55.0             # peak needle response; real checks run 80-150
CLUSTER_PX = 45.0
GAP_FRAMES = 6
MIN_FRAMES = 6
MIN_COHERENCE = 0.75           # net rotation / path length
MIN_SWEEP_DEG = 30.0
CENTRE_SPAN = 12.0             # px searched around the Hough seed
CENTRE_STEP = 0.25
CENTRED_PX = 20.0              # closer than this to frame centre is a normal check


def parse_args():
    p = argparse.ArgumentParser(description="Extract skill checks from a video recording")
    p.add_argument("video")
    p.add_argument("--out", default="recordings_video",
                   help="parent directory; a subdirectory named after the video is created")
    p.add_argument("--pad", type=int, default=8,
                   help="frames of pre-roll and post-roll kept around each event")
    p.add_argument("--model", default="models/model.onnx",
                   help="classifier used to fill in manifest predictions; '' to skip")
    return p.parse_args()


def polar(img, cx, cy, radii):
    th = np.deg2rad(ANGLES)[:, None]
    xs = np.clip(np.round(cx + radii[None, :] * np.sin(th)).astype(int), 0, img.shape[1] - 1)
    ys = np.clip(np.round(cy - radii[None, :] * np.cos(th)).astype(int), 0, img.shape[0] - 1)
    return img[ys, xs]


def redness(frame):
    b, g, r = (frame[:, :, i].astype(np.float32) for i in range(3))
    return r - np.maximum(g, b)  # the needle is red; the white zone arc cancels out


def needle_angle(red, cx, cy, r):
    """(angle_deg, peak) of the strongest red ray about (cx, cy). 0 = up, clockwise."""

    lo = int(r * 0.55)
    radii = np.arange(lo, max(int(r * 0.95), lo + 2))
    profile = polar(red, cx, cy, radii).mean(axis=1)
    return float(ANGLES[int(np.argmax(profile))]), float(profile.max())


def refine_centre(grey, cx0, cy0, r):
    """Centre that makes the base ring land at one radius for every angle."""

    radii = np.arange(r * 0.7, r * 1.3, CENTRE_STEP)
    grid = np.arange(-CENTRE_SPAN, CENTRE_SPAN + 1e-9, CENTRE_STEP)
    best = (-1.0, cx0, cy0)
    for dx in grid:
        for dy in grid:
            peak = np.median(polar(grey, cx0 + dx, cy0 + dy, radii), axis=0).max()
            if peak > best[0]:
                best = (peak, cx0 + dx, cy0 + dy)
    peak, cx, cy = best
    prof = np.median(polar(grey, cx, cy, radii), axis=0)
    return cx, cy, float(radii[int(np.argmax(prof))]), float(peak)


def wrap(d):
    return (d + 180.0) % 360.0 - 180.0


def scan(video):
    """Every (frame_index, cx, cy, r, angle, peak) candidate, plus fps and frame size."""

    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise SystemExit(f"cannot open {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0

    rmin, rmax = int(0.035 * TARGET_HEIGHT / 2), int(0.095 * TARGET_HEIGHT / 2)
    rows, idx, size = [], -1, None
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        idx += 1
        frame = to_1080p(frame)
        size = (frame.shape[1], frame.shape[0])
        small = cv2.resize(frame, (frame.shape[1] // 2, frame.shape[0] // 2))
        grey = cv2.medianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), 3)
        found = cv2.HoughCircles(grey, cv2.HOUGH_GRADIENT, dp=1, minDist=60,
                                 param1=100, param2=30, minRadius=rmin, maxRadius=rmax)
        if found is None:
            continue
        red = redness(frame)
        for cx, cy, r in found[0]:
            cx, cy, r = float(cx) * 2, float(cy) * 2, float(r) * 2
            ang, peak = needle_angle(red, cx, cy, r)
            if peak >= MIN_REDNESS:
                rows.append((idx, cx, cy, r, ang, peak))
    cap.release()
    return rows, fps, size, idx + 1


def to_1080p(frame):
    if frame.shape[0] == TARGET_HEIGHT:
        return frame
    scale = TARGET_HEIGHT / frame.shape[0]
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LANCZOS4
    return cv2.resize(frame, (int(round(frame.shape[1] * scale)), TARGET_HEIGHT),
                      interpolation=interp)


def cluster(rows):
    """Group candidates into positional runs, then cut them where the needle jumps."""

    runs = []
    for row in rows:
        idx, cx, cy = row[0], row[1], row[2]
        for run in runs:
            last = run[-1]
            if abs(cx - last[1]) < CLUSTER_PX and abs(cy - last[2]) < CLUSTER_PX \
                    and idx - last[0] <= GAP_FRAMES:
                run.append(row)
                break
        else:
            runs.append([row])

    pieces = []
    for run in runs:
        steps = [abs(wrap(b[4] - a[4])) for a, b in zip(run, run[1:])]
        if not steps:
            continue
        cut = max(20.0, 3.0 * float(np.median(steps)))
        piece = [run[0]]
        for prev, cur, step in zip(run, run[1:], steps):
            if step > cut or cur[0] - prev[0] > 3:
                pieces.append(piece)
                piece = []
            piece.append(cur)
        pieces.append(piece)
    return pieces


def split_revolutions(piece):
    """Cut a continuous sweep at each full turn.

    Merciless Storm rotates without ever resetting the needle, so one event can span 17
    revolutions with a different zone position in each. measure_zone.py medians over a
    check's frames, which only means anything if the zone stays put.
    """

    angs = np.rad2deg(np.unwrap(np.deg2rad([r[4] for r in piece])))
    out, start = [], 0
    for i in range(len(angs)):
        if abs(angs[i] - angs[start]) >= 360.0:
            out.append(piece[start:i])
            start = i
    out.append(piece[start:])
    return [p for p in out if len(p) >= MIN_FRAMES]


def score(piece, fps, size):
    angs = [r[4] for r in piece]
    steps = [wrap(b - a) for a, b in zip(angs, angs[1:])]
    net, path = abs(sum(steps)), sum(abs(s) for s in steps)
    coherence = net / path if path else 0.0
    dt = (piece[-1][0] - piece[0][0]) / fps
    cx = float(np.median([r[1] for r in piece]))
    cy = float(np.median([r[2] for r in piece]))
    w, h = size
    return dict(coherence=coherence, net_deg=net, rate=sum(steps) / dt if dt else 0.0,
                cx=cx, cy=cy, r=float(np.median([r[3] for r in piece])),
                off=(cx - (w - 1) / 2, cy - (h - 1) / 2))


class Classifier:
    """Just the ONNX forward pass; AI_model.__init__ would start a screen grabber."""

    def __init__(self, path):
        import onnxruntime as ort
        from dbd.AI_model import AI_model
        self.pred_dict = AI_model.pred_dict
        self.mean, self.std = AI_model.MEAN, AI_model.STD
        self.session = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name

    def predict(self, bgr):
        img = np.asarray(bgr, dtype=np.float32) / 255.0
        img = np.transpose(img, (2, 0, 1))
        img = (img - self.mean[:, None, None]) / self.std[:, None, None]
        logits = np.squeeze(self.session.run(None, {self.input_name: img[None]})[0])
        e = np.exp(logits - logits.max())
        probs = e / e.sum()
        pred = int(np.argmax(logits))
        return pred, self.pred_dict[pred]["desc"], float(probs[pred])


def write_check(video, out_dir, piece, fps, pad, total, classifier):
    """Write one check's 224 crops and manifest; returns its summary row."""

    f0, f1 = max(piece[0][0] - pad, 0), min(piece[-1][0] + pad, total - 1)
    cap = cv2.VideoCapture(video)
    cap.set(cv2.CAP_PROP_POS_FRAMES, f0)
    frames = []
    for _ in range(f1 - f0 + 1):
        ok, f = cap.read()
        if not ok:
            break
        frames.append(to_1080p(f))
    cap.release()
    if len(frames) < MIN_FRAMES:
        return None

    live = [frames[i - f0] for i, *_ in piece if f0 <= i <= f1]
    grey = np.median(np.stack([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in live]),
                     axis=0).astype(np.float32)
    seed = score(piece, fps, (frames[0].shape[1], frames[0].shape[0]))
    cx, cy, ring_r, peak = refine_centre(grey, seed["cx"], seed["cy"], seed["r"])

    x0 = int(round(cx - CROP_CENTRE[0]))
    y0 = int(round(cy - CROP_CENTRE[1]))
    h, w = frames[0].shape[:2]
    x0, y0 = max(0, min(x0, w - CROP)), max(0, min(y0, h - CROP))

    os.makedirs(out_dir, exist_ok=True)
    manifest = []
    for n, frame in enumerate(frames):
        crop = frame[y0:y0 + CROP, x0:x0 + CROP]
        name = f"{n:03d}.png"
        cv2.imwrite(os.path.join(out_dir, name), crop)
        if classifier:
            pred, desc, conf = classifier.predict(crop)
        else:
            # Without the model, mark the event's own frames live so the analysis tools,
            # which skip desc == "None" as pre-roll, still see them.
            inside = piece[0][0] <= f0 + n <= piece[-1][0]
            pred, desc, conf = (-1, "unclassified", 0.0) if inside else (0, "None", 0.0)
        manifest.append({"frame": name, "t_ms": round((f0 + n) / fps * 1000.0, 1),
                         "pred": pred, "desc": desc, "confidence": round(conf, 4)})
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)

    return dict(dir=os.path.basename(out_dir), frame0=f0, frame1=f1,
                t0=round(piece[0][0] / fps, 3), t1=round(piece[-1][0] / fps, 3),
                n_live=len(piece), n_frames=len(frames),
                centre=[round(cx, 2), round(cy, 2)], ring_r=round(ring_r, 2),
                centre_peak=round(peak, 1), crop_origin=[x0, y0],
                off=[round(seed["off"][0], 1), round(seed["off"][1], 1)],
                off_px=round(float(np.hypot(*seed["off"])), 1),
                centred=bool(np.hypot(*seed["off"]) < CENTRED_PX),
                rate_deg_s=round(seed["rate"], 1),
                direction="cw" if seed["rate"] > 0 else "ccw",
                coherence=round(seed["coherence"], 3))


def main():
    args = parse_args()
    name = os.path.splitext(os.path.basename(args.video))[0]
    out_root = os.path.join(args.out, name)
    os.makedirs(out_root, exist_ok=True)

    rows, fps, size, total = scan(args.video)
    print(f"{args.video}: {total} frames @ {fps:.2f} fps, scaled to {size[0]}x{size[1]}, "
          f"{len(rows)} ring candidates")

    events = []
    for piece in cluster(rows):
        if len(piece) < MIN_FRAMES:
            continue
        s = score(piece, fps, size)
        if s["coherence"] < MIN_COHERENCE or s["net_deg"] < MIN_SWEEP_DEG:
            continue
        events.extend(split_revolutions(piece) if s["net_deg"] >= 360 else [piece])
    events.sort(key=lambda p: p[0][0])

    classifier = None
    if args.model and os.path.exists(args.model):
        classifier = Classifier(args.model)
    elif args.model:
        print(f"warning: {args.model} not found; manifests will have no predictions")

    header = (f"{'check':<12}{'frames':>8}{'live':>6}{'ring r':>8}{'off px':>8}"
              f"{'position':>16}{'rate':>9}{'dir':>5}{'coh':>7}")
    print(header)
    print("-" * len(header))
    summary = []
    for i, piece in enumerate(events, 1):
        out_dir = os.path.join(out_root, f"check_{i:03d}")
        row = write_check(args.video, out_dir, piece, fps, args.pad, total, classifier)
        if row is None:
            continue
        summary.append(row)
        tag = "centre" if row["centred"] else "OFF"
        print(f"{row['dir']:<12}{row['n_frames']:>8}{row['n_live']:>6}{row['ring_r']:>8.2f}"
              f"{row['off_px']:>8.1f}  {tag:<6}"
              f"({row['off'][0]:+6.1f},{row['off'][1]:+6.1f}){row['rate_deg_s']:>9.1f}"
              f"{row['direction']:>5}{row['coherence']:>7.2f}")

    with open(os.path.join(out_root, "events.json"), "w") as fh:
        json.dump({"video": args.video, "fps": fps, "frames": total,
                   "size": list(size), "checks": summary}, fh, indent=1)
    off = [r for r in summary if not r["centred"]]
    ccw = [r for r in summary if r["direction"] == "ccw"]
    print(f"\n{len(summary)} checks -> {out_root}  "
          f"({len(off)} off-centre, {len(ccw)} counter-clockwise)")


if __name__ == "__main__":
    main()
