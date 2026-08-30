"""The four things in `ClipRecorder` that can lose data or stall the armed loop.

Gap rotation, ring eviction, the no-double-write rule across overlapping triggers, and
drop-on-saturation. Everything runs against a fake clock and a fake writer, so a 300 s
bout gap does not take 300 s to test and no JPEG is ever encoded here.
"""

import json
import os
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbd.utils.clip_recorder import ClipRecorder

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeWriter:
    """Stands in for the encode/write threads. Records what it was asked to write."""

    def __init__(self, capacity=None):
        self.written = []       # (path, t_ms)
        self.dropped = 0
        self.capacity = capacity
        self.bytes_written = 0

    def submit(self, path, frame, t_ms):
        if self.capacity is not None and len(self.written) >= self.capacity:
            self.dropped += 1
            return False
        self.written.append((path, t_ms))
        self.bytes_written += 1024
        return True

    def close(self):
        pass


GEOMETRY = {
    "wide_region": {"left": 624, "top": 194, "width": 672, "height": 672},
    "crop_region": {"left": 848, "top": 418, "width": 224, "height": 224},
    "centre_in_box": [224, 224],
    "side": 672,
    "crop_side": 224,
    "scale": 1.0,
    "clamped": False,
}
CONTENT = {"left": 0, "top": 0, "width": 1920, "height": 1080}


def frame(value=0):
    return np.full((8, 8, 3), value, dtype=np.uint8)


def make(root, clock, writer, **kw):
    opts = dict(pre_seconds=3.0, post_seconds=1.0, gap_seconds=300.0)
    opts.update(kw)
    return ClipRecorder(root=root, content=CONTENT, geometry=GEOMETRY,
                        clock=clock, writer=writer, **opts)


def feed(rec, clock, seconds, fps=30.0):
    """Offer frames at `fps` for `seconds` of fake time."""

    step = 1.0 / fps
    for _ in range(int(round(seconds * fps))):
        rec.offer(frame(), clock())
        clock.advance(step)


def bout_dirs(root):
    if not os.path.isdir(root):
        return []
    return sorted(n for n in os.listdir(root) if n.startswith("bout_"))


def test_ring_holds_only_pre_seconds():
    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter()
        rec = make(root, clock, writer, pre_seconds=3.0, post_seconds=0.0)
        feed(rec, clock, 10.0)          # ten seconds offered
        rec.trigger()
        rec.close()
        # Only the last 3 s may survive: 90 frames at 30 fps, give or take one boundary.
        check("ring keeps ~pre_seconds only", 88 <= len(writer.written) <= 92,
              f"{len(writer.written)} frames written")
    finally:
        shutil.rmtree(root)


def test_untriggered_frames_are_never_written():
    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter()
        rec = make(root, clock, writer)
        feed(rec, clock, 30.0)          # a long quiet stretch, no check
        rec.close()
        check("no trigger writes nothing", writer.written == [],
              f"{len(writer.written)} frames written with no trigger")
        check("no trigger opens no bout", bout_dirs(root) == [], str(bout_dirs(root)))
    finally:
        shutil.rmtree(root)


def test_post_hold_writes_frames_after_the_trigger():
    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter()
        rec = make(root, clock, writer, pre_seconds=1.0, post_seconds=2.0)
        feed(rec, clock, 1.0)
        rec.trigger()
        before = len(writer.written)
        feed(rec, clock, 2.0)           # inside the hold
        during = len(writer.written)
        feed(rec, clock, 5.0)           # past the hold
        rec.close()
        check("hold writes during post_seconds", during - before >= 55,
              f"{during - before} frames during a 2 s hold at 30 fps")
        check("hold stops after post_seconds", len(writer.written) - during <= 1,
              f"{len(writer.written) - during} frames written past the hold")
    finally:
        shutil.rmtree(root)


def test_overlapping_triggers_never_write_a_frame_twice():
    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter()
        rec = make(root, clock, writer, pre_seconds=3.0, post_seconds=1.0)
        feed(rec, clock, 3.0)
        rec.trigger()
        feed(rec, clock, 0.5)
        rec.trigger()                   # second check inside the first one's window
        feed(rec, clock, 1.0)
        rec.close()
        paths = [p for p, _ in writer.written]
        check("no frame written twice", len(paths) == len(set(paths)),
              f"{len(paths) - len(set(paths))} duplicate paths")
        times = [t for _, t in writer.written]
        check("frames written in time order", times == sorted(times), "out of order")
    finally:
        shutil.rmtree(root)


def test_gap_rotates_the_bout():
    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter()
        rec = make(root, clock, writer, pre_seconds=1.0, post_seconds=0.0, gap_seconds=300.0)
        feed(rec, clock, 1.0)
        rec.trigger()
        check("first trigger opens a bout", len(bout_dirs(root)) == 1, str(bout_dirs(root)))

        clock.advance(299.0)            # still inside the gap
        feed(rec, clock, 1.0)
        rec.trigger()
        check("under the gap stays in one bout", len(bout_dirs(root)) == 1,
              str(bout_dirs(root)))

        clock.advance(301.0)            # over the gap
        feed(rec, clock, 1.0)
        rec.trigger()
        rec.close()
        check("over the gap starts a new bout", len(bout_dirs(root)) == 2,
              str(bout_dirs(root)))
    finally:
        shutil.rmtree(root)


def test_bout_json_declares_the_framing():
    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter()
        rec = make(root, clock, writer, pre_seconds=1.0, post_seconds=0.0)
        feed(rec, clock, 1.0)
        rec.trigger({"desc": "great", "path": "predictive"})
        rec.close()
        path = os.path.join(root, bout_dirs(root)[0], "bout.json")
        meta = json.load(open(path))
        check("bout.json marks pre-cropped frames", meta.get("kind") == "wide_bout",
              repr(meta.get("kind")))
        check("bout.json carries the wide geometry",
              meta.get("geometry", {}).get("side") == 672, repr(meta.get("geometry")))
        check("bout.json carries the live content rect",
              meta.get("content", {}).get("height") == 1080, repr(meta.get("content")))
        check("bout.json starts unreviewed", meta.get("reviewed") is False,
              repr(meta.get("reviewed")))
        check("bout.json tallies its checks", len(meta.get("checks", [])) == 1,
              repr(meta.get("checks")))
    finally:
        shutil.rmtree(root)


def test_saturated_writer_drops_and_never_blocks():
    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter(capacity=10)
        rec = make(root, clock, writer, pre_seconds=3.0, post_seconds=0.0)
        feed(rec, clock, 3.0)
        rec.trigger()
        rec.close()
        check("saturated writer drops the rest", writer.dropped > 0,
              "nothing dropped, so the cap never engaged")
        check("recorder counts what it dropped", rec.dropped == writer.dropped,
              f"recorder {rec.dropped} vs writer {writer.dropped}")
    finally:
        shutil.rmtree(root)


def test_manifest_matches_the_frames_written():
    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter()
        rec = make(root, clock, writer, pre_seconds=1.0, post_seconds=0.0)
        feed(rec, clock, 1.0)
        rec.trigger()
        rec.close()
        bout = os.path.join(root, bout_dirs(root)[0])
        with open(os.path.join(bout, "manifest.jsonl")) as f:
            records = [json.loads(line) for line in f if line.strip()]
        names = [os.path.basename(p) for p, _ in writer.written]
        check("manifest lists exactly the frames written",
              [r["frame"] for r in records] == names,
              f"{len(records)} manifest rows vs {len(names)} frames")
        check("manifest timestamps rise",
              [r["t_ms"] for r in records] == sorted(r["t_ms"] for r in records),
              "t_ms out of order")
    finally:
        shutil.rmtree(root)


def main():
    print("clip_recorder")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if FAILED:
        print(f"\n{len(FAILED)} FAILED: {', '.join(FAILED)}")
        return 1
    print("\nall ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
