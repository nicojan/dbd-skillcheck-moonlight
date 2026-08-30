"""Record the frames the armed loop already grabbed, around the checks it already saw.

WHY THIS EXISTS. `record_frames.py` cannot run while the bot is armed: it opens a second
`mss` client, and two concurrent clients on macOS 26.6 mutually starve on a ~30 s timeout
(NOTES-local.md, 2026-08-20 — the recorder flatlined to one frame every 30.0 s and the
armed loop died with it). So every armed match has been played blind, and any rare perk
or state that turned up in one is gone.

The fix is to stop adding capture clients. `autorun.py` already grabs a 672 px wide box
every frame via `Monitoring_wide.grab_wide`, and that grab already copies out of the mss
buffer — so recording is "keep the frames we have", with no second client and nothing new
to contend with. It is also strictly better evidence than a parallel recorder could give:
these are the exact pixels the bot decided on, so `replay_centre_crop.py --frames` can
re-run a decision against its own input.

WHAT LANDS ON DISK. Not everything. Continuous 672 capture is 64 KB/frame at q92, so
~2.0 MB/s, ~7.4 GB/hour — against 70 GB free on this machine, that is two evenings. Nearly
all of it would be floor and generator, because a check is on screen for about a second.

So the ring buffer holds the last `pre_seconds` in memory and writes NOTHING until a check
fires, then flushes the ring and keeps writing for `post_seconds`. Every check lands with
its lead-in intact, at roughly a twentieth of the disk.

WHY THE RING HOLDS RAW FRAMES. Encoding on ingest would shrink the ring twentyfold
(1.35 MB raw against 64 KB encoded, so ~130 MB against ~6 MB for three seconds) but costs
1.57 ms of encode on every frame — 5% of a ~31 ms loop that every constant in this repo is
calibrated against. Hot-loop milliseconds are the scarce resource here and RAM is not, so
the ring takes raw frames and `offer` is a `deque.append`. Encoding happens on the writer
threads, only for frames that are actually kept.

BOUTS. Rotation is by gap, exactly as `check_log.CheckLog` does it and for the same
reason: a new bout starts when no check has fired for `gap_seconds`, so one directory is
one continuous stretch of play — a match, near enough — which is the unit worth keeping or
discarding as a whole. A wall-clock rotation would cut a match in half and a size rotation
would join two. The threshold is five minutes rather than the check log's one, because the
unit here is a match rather than a generator run.

Nothing here may ever block the armed loop. The writer queue is bounded and drops on
saturation, counting what it dropped, the way `record_frames.FrameWriter` does.
"""

import os
import queue
import threading
from collections import deque
from time import monotonic, strftime

import cv2
import numpy as np

from dbd.utils import bout_session

DEFAULT_ROOT = "frames"
DEFAULT_PRE_SECONDS = 3.0
DEFAULT_POST_SECONDS = 1.5
DEFAULT_GAP_SECONDS = 300.0
DEFAULT_QUALITY = 92
DEFAULT_WORKERS = 2
DEFAULT_MAX_GB = 20.0

BYTES_PER_GB = 1024 ** 3

# A memory backstop on the ring, independent of `pre_seconds`. The ring evicts by age, so
# a stall that stops draining it cannot grow it without bound — but if the loop ever ran
# far faster than expected, age alone would let it. 3 s at 60 fps is 180.
RING_MAX_FRAMES = 240


class ClipWriter:
    """Encode and write JPEGs on worker threads, so the armed loop never waits on one.

    The same bargain `record_frames.FrameWriter` makes: `cv2.imencode` releases the GIL,
    the queue is bounded, and a saturated queue DROPS the frame and says so rather than
    growing memory or stalling the caller. A dropped frame costs one sample; a stalled
    caller costs the match.
    """

    def __init__(self, quality=DEFAULT_QUALITY, workers=DEFAULT_WORKERS, max_pending=96):
        self.params = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
        self.queue = queue.Queue(maxsize=max_pending)
        self.bytes_written = 0
        self.dropped = 0
        self.failed = 0
        self._lock = threading.Lock()
        self._stop = object()
        self.threads = [threading.Thread(target=self._run, daemon=True)
                        for _ in range(workers)]
        for t in self.threads:
            t.start()

    def _run(self):
        while True:
            item = self.queue.get()
            if item is self._stop:
                self.queue.task_done()
                return
            path, frame, _t_ms = item
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

    def submit(self, path, frame, t_ms):
        try:
            self.queue.put_nowait((path, frame, t_ms))
            return True
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return False

    def close(self):
        for _ in self.threads:
            self.queue.put(self._stop)
        for t in self.threads:
            t.join(timeout=5.0)


class ClipRecorder:
    """Ring-buffer the wide grab; write clips around checks; rotate bouts on a gap.

    Stateful for the same reason `CheckLog` is — the ring and the open bout have to
    outlive the call. `clock` and `writer` are injected so the gap rule and the drop rule
    are testable without waiting five minutes or encoding a JPEG.
    """

    def __init__(self, root=DEFAULT_ROOT, content=None, geometry=None,
                 pre_seconds=DEFAULT_PRE_SECONDS, post_seconds=DEFAULT_POST_SECONDS,
                 gap_seconds=DEFAULT_GAP_SECONDS, quality=DEFAULT_QUALITY,
                 workers=DEFAULT_WORKERS, max_gb=DEFAULT_MAX_GB,
                 clock=monotonic, writer=None):
        self.root = root
        self.content = content or {}
        self.geometry = geometry or {}
        self.pre_seconds = pre_seconds
        self.post_seconds = post_seconds
        self.gap_seconds = gap_seconds
        self.quality = quality
        self.max_bytes = max_gb * BYTES_PER_GB
        self.clock = clock
        self.writer = writer if writer is not None else ClipWriter(quality, workers)

        self.ring = deque()             # (t, frame) newest last
        self.directory = None
        self.meta = None
        self.manifest = None
        self.frames_written = 0         # this bout
        self.total_written = 0          # this session
        self.bouts = 0
        self.dropped = 0
        self.budget_hit = False
        self._hot_until = None
        self._last_trigger = None
        self._last_written_t = None
        self._t0 = None

    # --- the hot path ----------------------------------------------------------------

    def offer(self, frame_bgr, t=None):
        """Take one wide grab. A `deque.append` in the quiet case, which is nearly always.

        `frame_bgr` must be a frame the caller will not overwrite — `grab_wide` already
        copies out of the mss buffer, so the armed loop's frame qualifies. A view onto a
        reused buffer would be rewritten under the writer threads.
        """

        now = self.clock() if t is None else t
        if self._hot_until is not None and now <= self._hot_until:
            self._write(now, frame_bgr)
            return

        if self._hot_until is not None and now > self._hot_until:
            self._hot_until = None

        self.ring.append((now, frame_bgr))
        self._evict(now)

    def _evict(self, now):
        cutoff = now - self.pre_seconds
        while self.ring and self.ring[0][0] < cutoff:
            self.ring.popleft()
        while len(self.ring) > RING_MAX_FRAMES:
            self.ring.popleft()

    # --- triggers --------------------------------------------------------------------

    def trigger(self, record=None):
        """A check fired. Flush the ring, then keep writing for `post_seconds`.

        Rotates the bout first if `gap_seconds` have passed with no trigger, so the gap is
        measured between checks — not between frames, which never stop arriving.
        """

        now = self.clock()
        if self._rotate_due(now):
            self._close_bout()
        if self.directory is None:
            self._open_bout(now)

        for t, frame in list(self.ring):
            self._write(t, frame)
        self.ring.clear()

        self._hot_until = now + self.post_seconds
        self._last_trigger = now

        if record is not None and self.meta is not None:
            self.meta["checks"].append(dict(record))
            self._save_meta()

    def _rotate_due(self, now):
        return (self._last_trigger is not None
                and (now - self._last_trigger) > self.gap_seconds)

    # --- bout lifecycle --------------------------------------------------------------

    def _open_bout(self, now):
        os.makedirs(self.root, exist_ok=True)
        stamp = strftime("%Y%m%d-%H%M%S")
        directory = os.path.join(self.root, f"bout_{stamp}")
        # A restart inside the same second would otherwise append into the previous bout
        # and silently join two that the gap rule had just separated — the same hazard
        # `CheckLog._open` guards against, with the same fix.
        suffix = 1
        while os.path.exists(directory):
            suffix += 1
            directory = os.path.join(self.root, f"bout_{stamp}-{suffix}")
        os.makedirs(directory)

        self.directory = directory
        self.meta = bout_session.new_meta(self.content, self.geometry, stamp,
                                          self.gap_seconds, self.quality)
        self._save_meta()
        self.manifest = open(os.path.join(directory, bout_session.MANIFEST_FILE),
                             "a", encoding="utf-8")
        self.frames_written = 0
        self._last_written_t = None
        self._t0 = now
        self.bouts += 1
        return directory

    def _close_bout(self):
        if self.manifest is not None:
            self.manifest.close()
            self.manifest = None
        if self.meta is not None:
            self.meta["frames"] = self.frames_written
            self._save_meta()
        self.directory = None
        self.meta = None
        self._hot_until = None

    def _save_meta(self):
        if self.directory is not None and self.meta is not None:
            self.meta["frames"] = self.frames_written
            bout_session.save(self.directory, self.meta)

    # --- writing ---------------------------------------------------------------------

    def _write(self, t, frame):
        """One frame to disk, at most once.

        Two checks inside one window both flush the ring, and the second flush overlaps
        the first. Writing by timestamp order and refusing anything not newer than the
        last write makes a duplicate impossible without tracking frame identity.
        """

        if self.directory is None or self.manifest is None:
            return False
        if self._last_written_t is not None and t <= self._last_written_t:
            return False
        if self.budget_hit:
            return False
        if self.writer.bytes_written >= self.max_bytes:
            self.budget_hit = True
            return False

        name = f"{self.frames_written:06d}.jpg"
        t_ms = round((t - self._t0) * 1000.0, 1)
        # np.ascontiguousarray, not the frame itself: a slice of the wide grab would keep
        # the whole box alive in the ring and hand the writer a non-contiguous view.
        payload = np.ascontiguousarray(frame)
        if not self.writer.submit(os.path.join(self.directory, name), payload, t_ms):
            self.dropped += 1
            return False

        self.manifest.write('{"frame": "%s", "t_ms": %s}\n' % (name, t_ms))
        self.manifest.flush()
        self.frames_written += 1
        self.total_written += 1
        self._last_written_t = t
        return True

    # --- teardown --------------------------------------------------------------------

    def close(self):
        self._close_bout()
        self.ring.clear()
        self.writer.close()

    def summary(self):
        gb = self.writer.bytes_written / BYTES_PER_GB
        note = (f"recorded {self.bouts} bout(s), {self.total_written} frames, {gb:.2f} GB")
        if self.dropped:
            note += f" — {self.dropped} dropped (writers saturated)"
        if self.budget_hit:
            note += " — disk budget reached, recording stopped"
        return note
