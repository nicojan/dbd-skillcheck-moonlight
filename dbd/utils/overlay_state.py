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

DEFAULT_PATH = os.path.join("state", "overlay.json")
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


def bot_mode(active, armed, performance_until, now):
    """The one word the badge shows. Precedence is "least pressing wins"."""

    if not active:
        return "paused"
    if not armed:
        return "disarmed"
    if now < performance_until:
        return "standdown"
    return "armed"


def _round(value, digits):
    return None if value is None else round(float(value), digits)


def snapshot(mode, wall, landings=(), reactive=0, standdown_left=None, load=None,
             fps=None, capture=None, content=None):
    """A new, JSON-ready dict describing the bot right now. `wall` is `time.time()`.

    Wall-clock rather than monotonic because it is read by another process.
    """

    if mode not in MODES:
        raise ValueError(f"unknown overlay mode {mode!r}")
    verdicts = [getattr(l, "verdict", None) for l in landings]
    return {
        "v": SCHEMA_VERSION,
        "t": float(wall),
        "mode": mode,
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
    if age > STALE_SECONDS:
        return Badge("BOT NOT RESPONDING", "stop", f"no update for {age:.0f}s")
    mode = snap["mode"]
    detail = _detail(snap)
    if mode == "armed":
        return Badge("ARMED", "go", detail)
    if mode == "standdown":
        left = snap.get("standdown_left")
        return Badge("STAND DOWN" + (f" · {left:.0f}s" if left is not None else ""),
                     "hold", "1-2-3-4! is yours · " + detail)
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
