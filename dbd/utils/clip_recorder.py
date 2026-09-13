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
                 clock=monotonic, writer=None, watch_keys=False):
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

        self.watch_keys = watch_keys

        self.ring = deque()             # (t, frame) newest last
        # Presses seen before a bout was open, evicted by age exactly like the ring. A
        # press in the lead-in belongs to the clip the ring is about to flush, so the two
        # buffers have to hold the same window or the first beat of a check loses its
        # keystroke while its frames survive.
        self.key_ring = deque()
        self.directory = None
        self.meta = None
        self.manifest = None
        self.keys_file = None
        self.keys_written = 0           # this bout
        self.keys_total = 0             # this session
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
        while self.key_ring and self.key_ring[0]["t"] < cutoff:
            self.key_ring.popleft()

    # --- operator key presses ---------------------------------------------------------

    def note_key(self, event):
        """One press from `KeyWatcher.drain`, placed on this bout's frame timeline.

        Written immediately whenever a bout is open — including between clips, where the
        frames are not being kept. A keystroke is one line and the whole point of it is to
        label what the pixels cannot, so the cheap thing is to keep them all and let the
        reader window them; dropping the ones outside a clip would silently discard the
        presses on either side of a check, which is where the interesting ones are.

        Before any bout exists the press goes to `key_ring` and is flushed by `_open_bout`,
        so the lead-in the ring is about to write keeps its keystrokes.
        """

        if not self.watch_keys:
            return False
        if self.directory is None:
            self.key_ring.append(dict(event))
            self._evict(self.clock())
            return False
        return self._write_key(event)

    def _write_key(self, event):
        if self.keys_file is None or self._t0 is None:
            return False
        t_ms = round((event["t"] - self._t0) * 1000.0, 1)
        self.keys_file.write(
            '{"t_ms": %s, "keycode": %d, "source": %d}\n'
            % (t_ms, int(event.get("keycode", -1)), int(event.get("source", -1))))
        self.keys_file.flush()
        self.keys_written += 1
        self.keys_total += 1
        if self.meta is not None:
            self.meta["keys"] = self.keys_written
        return True

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
        self.keys_written = 0
        self._last_written_t = None
        self._t0 = now
        self.bouts += 1

        if self.watch_keys:
            self.meta["keys_watched"] = True
            self.keys_file = open(os.path.join(directory, bout_session.KEYS_FILE),
                                  "a", encoding="utf-8")
            # `_t0` is `now`, so a press from the lead-in stamps NEGATIVE — the same
            # convention the manifest already uses for the ring's frames. Anything older
            # than the ring holds has already been evicted and is not ours to write.
            for event in list(self.key_ring):
                if event["t"] >= now - self.pre_seconds:
                    self._write_key(event)
            self.key_ring.clear()
            self._save_meta()
        return directory

    def _close_bout(self):
        if self.manifest is not None:
            self.manifest.close()
            self.manifest = None
        if self.keys_file is not None:
            self.keys_file.close()
            self.keys_file = None
        if self.meta is not None:
            self.meta["frames"] = self.frames_written
            # Clear the live marker before the last write, so the review tool can offer
            # this bout the moment the recorder is done with it — and not one moment
            # earlier, while a discard would race the writer threads.
            bout_session.mark_closed(self.meta)
            self._save_meta()
        self.directory = None
        self.meta = None
        self._hot_until = None

    def _save_meta(self):
        if self.directory is not None and self.meta is not None:
            self.meta["frames"] = self.frames_written
            self.meta["keys"] = self.keys_written
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
        self.key_ring.clear()
        self.writer.close()

    def summary(self):
        gb = self.writer.bytes_written / BYTES_PER_GB
        note = (f"recorded {self.bouts} bout(s), {self.total_written} frames, {gb:.2f} GB")
        if self.watch_keys:
            note += f", {self.keys_total} key press(es)"
        if self.dropped:
            note += f" — {self.dropped} dropped (writers saturated)"
        if self.budget_hit:
            note += " — disk budget reached, recording stopped"
        return note
