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


def keys_of(root, bout=None):
    bout = bout or bout_dirs(root)[0]
    path = os.path.join(root, bout, "keys.jsonl")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def press(t, keycode=49):
    return {"t": t, "keycode": keycode, "source": 1}


def test_keys_off_by_default_writes_no_file():
    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter()
        rec = make(root, clock, writer, pre_seconds=1.0, post_seconds=0.0)
        feed(rec, clock, 1.0)
        rec.trigger()
        rec.note_key(press(clock()))
        rec.close()
        bout = bout_dirs(root)[0]
        check("no keys.jsonl when not watching", keys_of(root) is None)
        with open(os.path.join(root, bout, "bout.json")) as f:
            meta = json.load(f)
        # The whole point of the flag: without it, "0 presses" would read as "the
        # operator never pressed", which is the wrong conclusion to draw from a run that
        # was never listening.
        check("bout says keys were not watched", meta["keys_watched"] is False)
    finally:
        shutil.rmtree(root)


def test_presses_land_on_the_frame_clock():
    """A press and a frame at the same instant must agree to the millisecond.

    This is the whole reason the watcher lives in this process: an external log aligned
    through `bout.json`'s second-resolution `started` carries up to 500 ms of error, and
    the arcs being labelled live ~650 ms.
    """

    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter()
        rec = make(root, clock, writer, pre_seconds=1.0, post_seconds=1.0,
                   watch_keys=True)
        feed(rec, clock, 1.0)
        rec.trigger()                       # opens the bout; _t0 is now
        t0 = clock()
        clock.advance(0.25)
        rec.offer(frame(), clock())
        rec.note_key(press(clock()))
        rec.close()
        rows = keys_of(root)
        with open(os.path.join(root, bout_dirs(root)[0], "manifest.jsonl")) as f:
            frames = [json.loads(line) for line in f if line.strip()]
        last = frames[-1]["t_ms"]
        check("one press recorded", rows is not None and len(rows) == 1,
              f"{None if rows is None else len(rows)} rows")
        check("press shares the frame's timestamp", rows and rows[0]["t_ms"] == last,
              f"key {rows[0]['t_ms'] if rows else '?'} vs frame {last}")
        check("keycode and source survive",
              rows and rows[0]["keycode"] == 49 and rows[0]["source"] == 1)
        check("t0 unchanged by the press", clock() > t0)
    finally:
        shutil.rmtree(root)


def test_lead_in_presses_are_kept_and_stamp_negative():
    """A press in the ring window belongs to the clip the ring is about to flush."""

    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter()
        rec = make(root, clock, writer, pre_seconds=3.0, post_seconds=0.0,
                   watch_keys=True)
        feed(rec, clock, 1.0)
        rec.note_key(press(clock()))        # 1 s of lead-in in, no bout open yet
        feed(rec, clock, 1.0)
        rec.trigger()
        rec.close()
        rows = keys_of(root)
        check("lead-in press is kept", rows is not None and len(rows) == 1,
              f"{None if rows is None else len(rows)} rows")
        check("lead-in press stamps negative", rows and rows[0]["t_ms"] < 0,
              f"t_ms {rows[0]['t_ms'] if rows else '?'}")
        check("and by about the right amount",
              rows and -1100 < rows[0]["t_ms"] < -900,
              f"t_ms {rows[0]['t_ms'] if rows else '?'}")
    finally:
        shutil.rmtree(root)


def test_presses_older_than_the_ring_are_dropped():
    """The key buffer holds the same window as the ring, not an unbounded history."""

    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter()
        rec = make(root, clock, writer, pre_seconds=1.0, post_seconds=0.0,
                   watch_keys=True)
        rec.note_key(press(clock()))        # far older than the ring will hold
        feed(rec, clock, 5.0)
        rec.trigger()
        rec.close()
        rows = keys_of(root)
        check("a press older than pre_seconds is evicted", rows == [],
              f"{rows}")
    finally:
        shutil.rmtree(root)


def test_presses_between_clips_are_still_recorded():
    """Keeping only the presses inside a clip would discard the ones around a check."""

    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter()
        rec = make(root, clock, writer, pre_seconds=1.0, post_seconds=0.5,
                   watch_keys=True)
        feed(rec, clock, 1.0)
        rec.trigger()
        clock.advance(30.0)                 # long past post_seconds, bout still open
        rec.note_key(press(clock()))
        rec.close()
        rows = keys_of(root)
        check("a press outside the clip is kept", rows is not None and len(rows) == 1,
              f"{None if rows is None else len(rows)} rows")
        with open(os.path.join(root, bout_dirs(root)[0], "bout.json")) as f:
            meta = json.load(f)
        check("bout counts it", meta["keys"] == 1, f"keys={meta['keys']}")
        check("bout says keys were watched", meta["keys_watched"] is True)
    finally:
        shutil.rmtree(root)


def test_a_new_bout_starts_its_own_key_file():
    root = tempfile.mkdtemp()
    try:
        clock, writer = FakeClock(), FakeWriter()
        rec = make(root, clock, writer, pre_seconds=1.0, post_seconds=0.0,
                   gap_seconds=10.0, watch_keys=True)
        feed(rec, clock, 1.0)
        rec.trigger()
        rec.note_key(press(clock()))
        clock.advance(60.0)                 # past the gap: next trigger rotates
        feed(rec, clock, 1.0)
        rec.trigger()
        rec.note_key(press(clock()))
        rec.close()
        bouts = bout_dirs(root)
        check("two bouts", len(bouts) == 2, f"{bouts}")
        first, second = keys_of(root, bouts[0]), keys_of(root, bouts[1])
        check("each bout has its own press", len(first or []) == 1 and len(second or []) == 1,
              f"{len(first or [])} / {len(second or [])}")
        # Each bout re-bases on its own _t0, so the second file must not carry the first
        # bout's 60-second offset.
        check("second bout re-bases its clock", second and abs(second[0]["t_ms"]) < 2000,
              f"t_ms {second[0]['t_ms'] if second else '?'}")
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
