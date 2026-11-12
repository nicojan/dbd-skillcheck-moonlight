"""What the armed loop publishes for the on-stream overlay, and how the overlay renders it.

The overlay answers one question mid-match: is the bot pressing right now? It runs as its
own process (`tools/overlay.py`) and learns the answer from a small JSON file the armed
loop rewrites on every state change and on a heartbeat. A file, not a socket or a thread,
on purpose: the overlay can crash, hang or be killed without the hot loop noticing, and
the loop's cost is one small atomic write a few times a second.

Two failure directions are designed out, and `tools/test_overlay_state.py` pins both:

* A badge that says ARMED when the bot is not pressing. Every non-pressing mode renders as
  not green, and a file older than `STALE_SECONDS` stops claiming anything at all — a
  crashed or frozen bot leaves its last ARMED behind, and that must not be believed.
* A panel inside the capture box. The bot reads the SCREEN (mss), not the video stream, so
  anything drawn over the box lands in the frames the classifier reads — the same reason
  ReShade and DBD's gamma slider are off-limits. `place` refuses any rect within
  `CAPTURE_PAD` of the box, and with no box known it refuses everything.

Coordinates are global screen points, top-left origin — the convention of CGWindowList and
mss, which is where the capture box comes from. `to_appkit` flips to Cocoa's bottom-left.
"""

import json
import os
from dataclasses import dataclass

# On the INTERNAL disk, not in the repo: the repo is on an external volume, and a stall
# there must not freeze the overlay's read (which runs on its UI thread) at the moment the
# bot's own writes to the same volume are stalling it too.
DEFAULT_PATH = os.path.join(os.path.expanduser("~/Library/Caches"), "dbd-skillcheck",
                            "overlay.json")
SCHEMA_VERSION = 1

# The loop writes at least every HEARTBEAT_SECONDS. The freeze watch after a fire blocks
# the loop for about a second, so the stale limit sits well clear of that.
HEARTBEAT_SECONDS = 0.5
STALE_SECONDS = 4.0

MODES = ("armed", "standdown", "disarmed", "paused", "stopped")
VERDICTS = ("GREAT", "good", "MISS")

CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")
DEFAULT_CORNER = "top-right"
DEFAULT_MARGIN = 16
CAPTURE_PAD = 24      # points of clear space the panel must keep from the capture box


def bot_mode(active, armed, performance_until, now, holds_reactive=True):
    """The one word the badge shows. Precedence is "least pressing wins".

    `holds_reactive` is False under `--no-predict`: the Performance guard sits only in the
    predictive path's reactive branch, so without that path nothing is held and saying
    STAND DOWN would be a lie.
    """

    if not active:
        return "paused"
    if not armed:
        return "disarmed"
    if holds_reactive and now < performance_until:
        # ONLY the reactive path holds inside a Performance; a sweeping check that turns
        # up during one is still fired by the predictive path. The badge says so.
        return "standdown"
    return "armed"


def _round(value, digits):
    return None if value is None else round(float(value), digits)


def snapshot(mode, wall, landings=(), reactive=0, standdown_left=None, load=None,
             fps=None, capture=None, content=None, dry_run=False, can_press=None,
             guard=True):
    """A new, JSON-ready dict describing the bot right now. `wall` is `time.time()`.

    Wall-clock rather than monotonic because it is read by another process.

    The three flags are what `mode` cannot say: `dry_run` (presses are logged, never
    sent), `can_press` (does this process hold Accessibility — False means presses are
    dropped silently, as after the 2026-10-15 iTerm2 update; None means unknown), and
    `guard` (is the key tap up — without it there is no backspace toggle and no
    Performance guard).
    """

    if mode not in MODES:
        raise ValueError(f"unknown overlay mode {mode!r}")
    verdicts = [getattr(l, "verdict", None) for l in landings]
    return {
        "v": SCHEMA_VERSION,
        "t": float(wall),
        "mode": mode,
        "dry_run": bool(dry_run),
        "can_press": None if can_press is None else bool(can_press),
        "guard": bool(guard),
        "standdown_left": _round(standdown_left, 1),
        "fires": len(verdicts),
        "great": verdicts.count("GREAT"),
        "good": verdicts.count("good"),
        "miss": verdicts.count("MISS"),
        "reactive": int(reactive),
        "load": _round(load, 2),
        "fps": _round(fps, 1),
        "capture": None if capture is None else {k: int(capture[k]) for k in
                                                 ("left", "top", "width", "height")},
        "content": None if content is None else {k: int(content[k]) for k in
                                                 ("left", "top", "width", "height")},
    }


def write_state(snap, path=DEFAULT_PATH):
    """Atomically replace the state file. Returns False on any failure; never raises.

    Never raises because the caller is the armed loop: an overlay that cannot be fed is
    worth nothing compared to the match it would interrupt.
    """

    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        return False


def read_state(path=DEFAULT_PATH):
    """The last snapshot, or None if missing, unreadable, or from another schema."""

    try:
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict) or snap.get("v") != SCHEMA_VERSION:
        return None
    if snap.get("mode") not in MODES or not isinstance(snap.get("t"), (int, float)):
        return None
    return snap


class Poller:
    """Re-reads a slow fact on its own thread; `.value` is the latest answer.

    For checks too slow for the frame loop — `AXIsProcessTrusted` measures ~160 us here,
    an IPC to the TCC daemon. `.value` is None until the first read and whenever `read`
    raises, which the badge treats as unknown rather than as fine or broken.
    """

    def __init__(self, read, interval=2.0):
        import threading

        self._read = read
        self._interval = interval
        self._stop = threading.Event()
        self.value = None
        self._thread = threading.Thread(target=self._run, name="overlay-poller",
                                        daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            try:
                self.value = self._read()
            except Exception:                               # noqa: BLE001
                self.value = None
            if self._stop.wait(self._interval):
                return

    def close(self):
        self._stop.set()


def accessibility_trusted():
    """True/False from macOS, or None where it cannot be asked."""

    try:
        from HIServices import AXIsProcessTrusted
    except ImportError:
        return None
    return bool(AXIsProcessTrusted())


class StateWriter:
    """Writes snapshots on a background thread, so the armed loop never touches the disk.

    The repo lives on an external volume, and a rename there is usually well under a
    millisecond but not always — a stall right before a fire would add straight to the
    frame age the lead is aimed with. `offer` is a lock and an assignment; only the latest
    snapshot is kept, because an older state is worthless once a newer one exists.
    """

    def __init__(self, path=DEFAULT_PATH, write=None):
        import threading

        self.path = path
        self._write = write or write_state
        self._cond = threading.Condition()
        self._pending = None
        self._closed = False
        self.failures = 0
        self._thread = threading.Thread(target=self._run, name="overlay-writer",
                                        daemon=True)
        self._thread.start()

    def offer(self, snap):
        with self._cond:
            self._pending = snap
            self._cond.notify()

    def _run(self):
        while True:
            with self._cond:
                while self._pending is None and not self._closed:
                    self._cond.wait()
                snap, self._pending = self._pending, None
                closed = self._closed
            if snap is not None and not self._write(snap, self.path):
                self.failures += 1
            if closed and snap is None:
                return

    def close(self, final=None, timeout=2.0):
        """Flush, then write `final` (if given) last. Never raises."""

        with self._cond:
            if final is not None:
                self._pending = final
            self._closed = True
            self._cond.notify()
        self._thread.join(timeout=timeout)


@dataclass(frozen=True)
class Badge:
    headline: str
    tone: str        # go | hold | stop | idle — the overlay maps these to colours
    detail: str


def _detail(snap):
    parts = [f"{snap.get('fires', 0)} fires",
             f"{snap.get('great', 0)} great",
             f"{snap.get('good', 0)} good",
             f"{snap.get('miss', 0)} miss"]
    if snap.get("reactive"):
        parts.append(f"{snap['reactive']} reactive")
    if snap.get("load") is not None:
        parts.append(f"load {snap['load']:.1f}")
    if snap.get("fps") is not None:
        parts.append(f"{snap['fps']:.0f} fps")
    return " · ".join(parts)


def badge(snap, now):
    """What to draw for this snapshot at wall time `now`. Pure."""

    if snap is None:
        return Badge("BOT NOT RUNNING", "stop", "no state file")
    age = now - snap["t"]
    # Negative too: after the wall clock steps backwards an old ARMED looks brand new.
    if age > STALE_SECONDS or age < -1.0:
        return Badge("BOT NOT RESPONDING", "stop", f"no update for {age:.0f}s")
    mode = snap["mode"]
    detail = _detail(snap)
    if mode in ("armed", "standdown"):
        # Checked before the mode: either one means nothing reaches the game, so neither
        # may ever wear the green.
        if snap.get("dry_run"):
            return Badge("DRY RUN · NOT PRESSING", "stop", "--dry-run · " + detail)
        if snap.get("can_press") is False:
            return Badge("NO KEY ACCESS", "stop",
                         "presses are dropped: grant the terminal Accessibility · " + detail)
    if mode == "armed":
        if not snap.get("guard", True):
            return Badge("ARMED · NO TOGGLE", "hold",
                         "no key tap: backspace and the 1-2-3-4! guard are off · " + detail)
        return Badge("ARMED", "go", detail)
    if mode == "standdown":
        left = snap.get("standdown_left")
        return Badge("STAND DOWN" + (f" · {left:.0f}s" if left is not None else ""),
                     "hold", "reactive held for 1-2-3-4! · sweeping checks still fire · "
                     + detail)
    if mode == "disarmed":
        return Badge("DISARMED", "stop", "backspace ×2 to re-arm · " + detail)
    if mode == "paused":
        return Badge("PAUSED", "idle", "game window not focused · " + detail)
    return Badge("BOT STOPPED", "stop", detail)


def overlaps(a, b, pad=0):
    """Do rects `a` and `b` (left/top/width/height) come within `pad` of each other?"""

    return not (a["left"] + a["width"] + pad <= b["left"]
                or b["left"] + b["width"] + pad <= a["left"]
                or a["top"] + a["height"] + pad <= b["top"]
                or b["top"] + b["height"] + pad <= a["top"])


def place(content, capture, size, corner=DEFAULT_CORNER, margin=DEFAULT_MARGIN):
    """The panel's rect in a corner of the stream content, or None if it is not safe.

    None whenever the rect would come within CAPTURE_PAD of the capture box, and whenever
    the box is unknown: a panel we cannot prove is out of the frames is not drawn.
    """

    if corner not in CORNERS:
        raise ValueError(f"unknown corner {corner!r}; one of {', '.join(CORNERS)}")
    if content is None or capture is None:
        return None
    width, height = int(size[0]), int(size[1])
    left = (content["left"] + margin if corner.endswith("left")
            else content["left"] + content["width"] - margin - width)
    top = (content["top"] + margin if corner.startswith("top")
           else content["top"] + content["height"] - margin - height)
    rect = {"left": left, "top": top, "width": width, "height": height}
    if overlaps(rect, capture, pad=CAPTURE_PAD):
        return None
    return rect


def to_appkit(rect, primary_height):
    """Top-left-origin rect -> Cocoa (x, y, w, h), whose origin is the primary screen's
    bottom-left."""

    return (rect["left"], primary_height - rect["top"] - rect["height"],
            rect["width"], rect["height"])
