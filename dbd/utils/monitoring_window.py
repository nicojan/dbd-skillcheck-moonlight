"""Capture a specific application window (e.g. Moonlight) instead of a whole monitor.

Why this exists
---------------
Monitoring_mss crops the centre of an entire *display* and sizes that crop from the
display height:

    crop_px = 224 / 1080 * monitor_height

That scale factor is not cosmetic. The ONNX model was trained on skill checks that
occupy a fixed fraction of a 1080p frame, so the crop has to track the height of the
*rendered game*, not the height of the screen it happens to be sitting on.

When the game arrives through Moonlight those two heights come apart:

  * the stream is letterboxed / pillarboxed whenever its aspect ratio differs from
    the window it is displayed in, so part of the window is black bars;
  * a windowed (non-fullscreen) Moonlight makes the game smaller than the display;
  * a second monitor with a different height changes the scale again.

This module resolves the Moonlight window at runtime, subtracts the bars to get the
true content rect, and sizes the centre crop from the content height. The frame handed
to the model then matches training scale regardless of window geometry.
"""

import cv2
import numpy as np
import Quartz
from mss import mss
from PIL import Image

from dbd.utils.monitoring_mss import Monitoring

DEFAULT_WINDOW_QUERY = "Moonlight"
TRAINING_REFERENCE_HEIGHT = 1080.0
TRAINING_REFERENCE_CROP = 224.0


class WindowNotFoundError(RuntimeError):
    pass


def list_windows(on_screen_only=False):
    """Return owner/title/bounds for every window.

    A fullscreen app lives on its own macOS Space, and CGWindowListCopyWindowInfo's
    kCGWindowListOptionOnScreenOnly reports only the *active* Space. A fullscreen
    Moonlight is therefore invisible to an on-screen-only query whenever you are
    looking at another desktop, so the default here enumerates all windows.
    """

    options = (
        Quartz.kCGWindowListOptionOnScreenOnly if on_screen_only
        else Quartz.kCGWindowListOptionAll
    ) | Quartz.kCGWindowListExcludeDesktopElements
    raw = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []

    return [
        {
            "owner": str(w.get("kCGWindowOwnerName", "")),
            "title": str(w.get("kCGWindowName", "")),
            "on_screen": bool(w.get("kCGWindowIsOnscreen", False)),
            "layer": int(w.get("kCGWindowLayer", 0)),
            "bounds": {
                "left": int(w["kCGWindowBounds"]["X"]),
                "top": int(w["kCGWindowBounds"]["Y"]),
                "width": int(w["kCGWindowBounds"]["Width"]),
                "height": int(w["kCGWindowBounds"]["Height"]),
            },
        }
        for w in raw
        if "kCGWindowBounds" in w
    ]


def find_window(query=DEFAULT_WINDOW_QUERY, min_width=640, min_height=360):
    """Largest window whose owner or title contains `query` (case-insensitive).

    Moonlight registers a swarm of small helper windows (menu-bar overlays, a 64px
    cursor layer). The size floor keeps those out; the largest survivor is the stream.
    """

    needle = query.lower()
    matches = [
        w for w in list_windows()
        if needle in w["owner"].lower() or needle in w["title"].lower()
    ]
    matches = [
        w for w in matches
        if w["bounds"]["width"] >= min_width and w["bounds"]["height"] >= min_height
    ]

    if not matches:
        raise WindowNotFoundError(
            f"No window matching {query!r} at least {min_width}x{min_height}. "
            "Is it running and streaming?"
        )

    return max(matches, key=lambda w: w["bounds"]["width"] * w["bounds"]["height"])


def content_rect(bounds, stream_aspect=None, inset_top=0):
    """Strip letterbox / pillarbox bars from a window rect.

    `stream_aspect` is the aspect ratio of the streamed game (e.g. 16/9). When None the
    whole window is assumed to be game content. Returns a new rect; `bounds` is untouched.
    """

    left = bounds["left"]
    top = bounds["top"] + inset_top
    width = bounds["width"]
    height = bounds["height"] - inset_top

    if stream_aspect is None or width <= 0 or height <= 0:
        return {"left": left, "top": top, "width": width, "height": height}

    window_aspect = width / height

    if window_aspect > stream_aspect:  # window wider than stream -> pillarboxed
        content_w = int(round(height * stream_aspect))
        content_h = height
    else:  # window taller than stream -> letterboxed
        content_w = width
        content_h = int(round(width / stream_aspect))

    return {
        "left": left + (width - content_w) // 2,
        "top": top + (height - content_h) // 2,
        "width": content_w,
        "height": content_h,
    }


def centre_crop_region(content, crop_size=224, zoom=1.0):
    """Centre region of `content`, scaled so the skill check matches training size."""

    scale = content["height"] / TRAINING_REFERENCE_HEIGHT
    side = int(round(TRAINING_REFERENCE_CROP * scale * zoom))
    side = max(side, 8)

    return {
        "left": content["left"] + content["width"] // 2 - side // 2,
        "top": content["top"] + content["height"] // 2 - side // 2,
        "width": side,
        "height": side,
    }


class Monitoring_window(Monitoring):
    """Grab the centre of a named window, scaled to the model's training geometry.

    Set full_window=True to grab the entire window instead. That is a debugging /
    calibration view only: it shrinks the skill check far below the size the model was
    trained on, so predictions from it are not meaningful.
    """

    def __init__(
        self,
        window_query=DEFAULT_WINDOW_QUERY,
        crop_size=224,
        stream_aspect=16 / 9,
        inset_top=0,
        zoom=1.0,
        full_window=False,
    ):
        super().__init__()
        self.window_query = window_query
        self.crop_size = crop_size
        self.stream_aspect = stream_aspect
        self.inset_top = inset_top
        self.zoom = zoom
        self.full_window = full_window

        self.sct = None
        self.window = None
        self.content = None
        self.region = None
        self.refresh()

    def refresh(self):
        """Re-resolve window geometry. Call after moving/resizing the window."""

        window = find_window(self.window_query)
        content = content_rect(window["bounds"], self.stream_aspect, self.inset_top)
        region = content if self.full_window else centre_crop_region(content, self.crop_size, self.zoom)

        self.window = window
        self.content = content
        self.region = region
        return region

    def describe(self):
        return {
            "owner": self.window["owner"],
            "title": self.window["title"],
            "window": dict(self.window["bounds"]),
            "content": dict(self.content),
            "capture_region": dict(self.region),
            "scale_vs_1080p": round(self.content["height"] / TRAINING_REFERENCE_HEIGHT, 3),
        }

    def start(self):
        self.sct = mss()

    def stop(self):
        if self.sct is not None:
            self.sct.close()
            self.sct = None

    @staticmethod
    def get_monitors_info():
        windows = list_windows()
        return [
            (f"{w['owner']} — {w['bounds']['width']}x{w['bounds']['height']}", w["owner"])
            for w in windows
            if w["bounds"]["width"] > 200 and w["bounds"]["height"] > 200
        ]

    def get_raw_frame(self):
        if self.sct is None:
            raise RuntimeError("Monitoring_window not started. Call start() before grabbing frames.")

        return self.sct.grab(self.region)

    def get_frame_pil(self) -> Image:
        frame = self.get_raw_frame()
        frame = Image.frombytes("RGB", frame.size, frame.bgra, "raw", "BGRX")

        if frame.height != self.crop_size or frame.width != self.crop_size:
            frame = frame.resize((self.crop_size, self.crop_size), Image.Resampling.BICUBIC)

        return frame

    def get_frame_np(self) -> np.ndarray:
        frame = self.get_raw_frame()
        frame = np.array(frame, dtype=np.uint8)
        frame = np.flip(frame[:, :, :3], 2)  # BGRA -> RGB

        if frame.shape[:2] != (self.crop_size, self.crop_size):
            frame = cv2.resize(frame, (self.crop_size, self.crop_size), interpolation=cv2.INTER_CUBIC)

        return frame
