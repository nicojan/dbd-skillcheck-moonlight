"""Record full streamed frames during live play, for OFFLINE off-centre analysis.

The production path only ever looks at a 224 centre crop, so a skill check rendering
anywhere else is never captured and never classified — a silent miss. `wide_scan.py`
attacks that live, but tiling 144 windows costs ~317 ms/frame (~3 fps) and a check is
only on screen for about a second, so it samples each check a handful of times at best.
It has never caught one, and at 3 fps we cannot tell whether that means off-centre
checks are rare or that the scanner blinked past them.

This tool removes inference from the hot loop entirely. Capture is cheap (26.9 ms for a
full frame, only 4.7 ms more than a 224 crop); classification is what is expensive. So
record frames at play speed here, and let `scan_frames.py` do the 224-tile sweep
afterwards with no time pressure. Play time is the scarce resource, not CPU.

    .venv/bin/python tools/record_frames.py --seconds 900

Presses nothing. Writes JPEGs plus a manifest; frames are unannotated.

Measured on this machine at 1920x1080. Capture is 29.0 ms and JPEG encode + write is
12.3 ms, which serialized caps the loop near 24 fps. They are independent work, though,
and OpenCV releases the GIL while encoding, so handing writes to worker threads lifts the
measured ceiling well past that:

    1 writer thread   34.2 fps
    2 writer threads  41.5 fps      <- default
    3 writer threads  42.6 fps      (diminishing; capture is now the limit)

The default is 30 fps, comfortably under that ceiling. Frame rate matters more than it
first appears: a sped-up skill check can finish in ~0.5 s, which is only ~6 frames at
12 fps — too few to fit a sweep rate with any confidence.

Quality costs almost nothing in time (q92 12.3 ms vs q75 11.6 ms), so it stays high — a
needle smeared by compression is useless to any later CV pass.

Disk is the real constraint: ~350 KB/frame at q92, so 30 fps is ~630 MB/min. Size
--max-gb accordingly; it stops the recording before the disk does.
"""

import argparse
import json
import os
import queue
import sys
import threading
from time import monotonic, sleep, strftime

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.utils.focus_watcher import FocusWatcher
from dbd.utils.monitoring_window import Monitoring_window

BYTES_PER_GB = 1024 ** 3


def parse_args():
    p = argparse.ArgumentParser(description="Record full frames for offline tile scanning")
    p.add_argument("--window", default="Moonlight")
    p.add_argument("--aspect", default="16:9")
    p.add_argument("--fps", type=float, default=30.0,
                   help="target sample rate; sped-up checks can finish in ~0.5s, so higher is "
                        "better. With 2 writer threads the measured ceiling is ~41 fps")
    p.add_argument("--seconds", type=float, default=900.0)
    p.add_argument("--quality", type=int, default=92,
                   help="JPEG quality; high because a smeared needle is useless for later CV")
    p.add_argument("--max-gb", type=float, default=8.0, help="stop recording after this much on disk")
    p.add_argument("--writers", type=int, default=2,
                   help="encode/write threads; capture stays on the main thread")
    p.add_argument("--out", default="frames")
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


class FrameWriter:
    """Encode and write JPEGs on worker threads so capture never waits on them.

    Capture (29.0 ms) and encode+write (12.3 ms) were serialized, capping the loop at
    ~24 fps. They are independent, and encoding releases the GIL inside OpenCV, so moving
    the write off the capture thread lifts the ceiling to whatever `mss` alone can do
    (~34 fps full-frame). The queue is bounded: if the writers ever fall behind, we drop
    frames deliberately and say so, rather than growing memory without bound.
    """

    def __init__(self, quality, max_pending=64, workers=2):
        self.params = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
        self.queue = queue.Queue(maxsize=max_pending)
        self.bytes_written = 0
        self.dropped = 0
        self.failed = 0
        self._lock = threading.Lock()
        self._stop = object()
        self.threads = [threading.Thread(target=self._run, daemon=True) for _ in range(workers)]
        for t in self.threads:
            t.start()

    def _run(self):
        while True:
            item = self.queue.get()
            if item is self._stop:
                self.queue.task_done()
                return
            path, frame = item
            try:
                if cv2.imwrite(path, frame, self.params):
                    size = os.path.getsize(path)
                    with self._lock:
                        self.bytes_written += size
                else:
                    with self._lock:
                        self.failed += 1
            except OSError:
                with self._lock:
                    self.failed += 1
            finally:
                self.queue.task_done()

    def submit(self, path, frame):
        """True if queued, False if the writers are saturated and the frame was dropped."""

        try:
            self.queue.put_nowait((path, frame))
            return True
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return False

    def close(self):
        self.queue.join()
        for _ in self.threads:
            self.queue.put(self._stop)
        for t in self.threads:
            t.join(timeout=5)


def make_session_dir(root):
    """A directory per run, so successive games do not overwrite each other."""

    session = os.path.join(root, f"session_{strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(session, exist_ok=True)
    return session


def main():
    args = parse_args()
    if args.fps <= 0:
        sys.exit("--fps must be positive")

    session = make_session_dir(args.out)

    monitor = Monitoring_window(
        window_query=args.window,
        crop_size=224,
        stream_aspect=parse_aspect(args.aspect),
        full_window=True,  # the whole content rect; tiling happens offline
    )
    content = monitor.content
    log(f"content {content['width']}x{content['height']} at ({content['left']},{content['top']})")
    log(f"scale vs 1080p: {monitor.describe()['scale_vs_1080p']}")

    watcher = FocusWatcher(query=args.window)
    monitor.start()

    # Recorded once, at the top: scan_frames.py needs the geometry to place tiles and to
    # work out where the production centre crop would have been.
    meta = {
        "content": dict(content),
        "window": dict(monitor.window["bounds"]),
        "aspect": args.aspect,
        "fps_target": args.fps,
        "quality": args.quality,
    }
    with open(os.path.join(session, "session.json"), "w") as f:
        json.dump(meta, f, indent=1)

    writer = FrameWriter(args.quality, workers=args.writers)
    budget_bytes = args.max_gb * BYTES_PER_GB

    interval = 1.0 / args.fps
    deadline = monotonic() + args.seconds
    manifest_path = os.path.join(session, "manifest.jsonl")

    saved = 0
    written_bytes = 0
    skipped_unfocused = 0
    start = monotonic()
    next_frame_at = start

    log(f"recording up to {args.seconds:.0f}s / {args.max_gb:.1f} GB -> {session}")
    log("switch to the stream and play — nothing will be pressed")

    try:
        with open(manifest_path, "w") as manifest:
            while monotonic() < deadline:
                if not watcher.is_active():
                    skipped_unfocused += 1
                    sleep(0.2)
                    next_frame_at = monotonic()  # do not burst to catch up after a pause
                    continue

                now = monotonic()
                if now < next_frame_at:
                    sleep(min(next_frame_at - now, interval))
                    continue
                next_frame_at = now + interval

                raw = monitor.get_raw_frame()
                # np.array() copies out of the mss buffer, which the next grab reuses —
                # the writer thread must not be handed a view onto it.
                frame_bgr = np.array(raw, dtype=np.uint8)[:, :, :3]  # BGRA -> BGR

                name = f"{saved:06d}.jpg"
                if not writer.submit(os.path.join(session, name), frame_bgr):
                    continue  # writers saturated; dropped and counted, keep capturing

                written_bytes = writer.bytes_written
                manifest.write(json.dumps({
                    "frame": name,
                    "t_ms": round((now - start) * 1000, 1),
                }) + "\n")
                saved += 1

                if saved % 200 == 0:
                    manifest.flush()
                    elapsed = monotonic() - start
                    log(f"  {saved} frames, {written_bytes / BYTES_PER_GB:.2f} GB, "
                        f"{saved / max(elapsed, 1e-6):.1f} fps effective")

                if written_bytes >= budget_bytes:
                    log(f"disk budget of {args.max_gb:.1f} GB reached — stopping")
                    break

    except KeyboardInterrupt:
        log("interrupted")
    finally:
        monitor.stop()
        writer.close()
        written_bytes = writer.bytes_written

    elapsed = monotonic() - start
    log(f"done — {saved} frames, {written_bytes / BYTES_PER_GB:.2f} GB over {elapsed:.0f}s "
        f"({saved / max(elapsed, 1e-6):.1f} fps effective)")
    if skipped_unfocused:
        log(f"{skipped_unfocused} polls skipped while the stream was not focused")
    if writer.dropped or writer.failed:
        log(f"WARNING: {writer.dropped} frames dropped (writers saturated), "
            f"{writer.failed} failed to write — lower --fps or raise --writers")
    if saved:
        log(f"next: .venv/bin/python tools/scan_frames.py --frames {session}")
    else:
        log("nothing recorded — was the stream focused? check focus_watcher first")


if __name__ == "__main__":
    main()
