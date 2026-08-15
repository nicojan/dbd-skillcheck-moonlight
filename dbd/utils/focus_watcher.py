"""Decide whether the stream is the thing currently receiving keystrokes.

DO NOT use NSWorkspace.frontmostApplication() here. It caches, and without a Cocoa
run loop pumping the process it never refreshes. Measured on this machine, polling
both signals inside one 60-second process while switching to a fullscreen Moonlight
and back:

    time       NSWorkspace     CGWindowList top   moonlight_onscreen
    22:35:38   iTerm2          iTerm2             False
    22:35:46   iTerm2          iTerm2             True
    22:35:48   iTerm2          Moonlight          True    <- in Moonlight
    22:36:12   iTerm2          iTerm2             False   <- switched back

NSWorkspace was frozen on the launching terminal for the entire run. The trap is that
it looks correct in short-lived processes (a fresh query is accurate), so it passes
casual testing and then fails in the long-running loop that actually matters.

CGWindowList re-queries the window server on every call, costs ~1.09 ms, and is
truthful. That is cheap against a ~24 ms capture+inference frame, so the gate runs
every frame by default rather than on a throttle: a stale gate means keystrokes land
in whatever app you just switched to.
"""

from time import monotonic

import Quartz

DEFAULT_DEEP_CHECK_INTERVAL = 0.0  # 0 = re-check every call; correctness over cost
MIN_STREAM_WIDTH = 640
MIN_STREAM_HEIGHT = 360
MIN_FOREGROUND_WIDTH = 200
MIN_FOREGROUND_HEIGHT = 200


def frontmost_app_name():
    """Owner of the topmost normal-layer window on the active Space.

    This is the CGWindowList-derived answer, not NSWorkspace's cached one. Windows are
    returned in front-to-back order, so the first real window is the foreground app.
    Layer != 0 skips menu bars and overlays; the size floor skips helper widgets.
    """

    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []

    for w in windows:
        if int(w.get("kCGWindowLayer", 0)) != 0:
            continue
        bounds = w.get("kCGWindowBounds", {})
        if bounds.get("Width", 0) < MIN_FOREGROUND_WIDTH:
            continue
        if bounds.get("Height", 0) < MIN_FOREGROUND_HEIGHT:
            continue
        return str(w.get("kCGWindowOwnerName", ""))

    return ""


def stream_window_on_screen(query, min_width=MIN_STREAM_WIDTH, min_height=MIN_STREAM_HEIGHT):
    """True if a window matching `query` is on the *active* Space at a playable size.

    kCGWindowListOptionOnScreenOnly reports only the active Space, which is exactly the
    behaviour wanted here: a fullscreen Moonlight on a background Space is not visible,
    and its pixels are not what the framebuffer holds.
    """

    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    needle = query.lower()

    for w in windows:
        if "kCGWindowBounds" not in w:
            continue
        owner = str(w.get("kCGWindowOwnerName", "")).lower()
        title = str(w.get("kCGWindowName", "")).lower()
        if needle not in owner and needle not in title:
            continue
        bounds = w["kCGWindowBounds"]
        if bounds["Width"] >= min_width and bounds["Height"] >= min_height:
            return True

    return False


def inspect_windows(query, min_width=MIN_STREAM_WIDTH, min_height=MIN_STREAM_HEIGHT):
    """One window-server query answering both questions at once.

    Returns (frontmost_owner, stream_on_screen). Doing this in a single pass halves the
    per-frame cost versus calling frontmost_app_name() and stream_window_on_screen()
    separately, since each of those is its own ~1.09 ms round trip.
    """

    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    needle = query.lower()

    frontmost = ""
    stream_present = False

    for w in windows:
        owner = str(w.get("kCGWindowOwnerName", ""))
        title = str(w.get("kCGWindowName", ""))
        bounds = w.get("kCGWindowBounds", {})
        width = bounds.get("Width", 0)
        height = bounds.get("Height", 0)
        layer = int(w.get("kCGWindowLayer", 0))

        # front-to-back order, so the first qualifying normal window is the foreground
        if (not frontmost and layer == 0
                and width >= MIN_FOREGROUND_WIDTH and height >= MIN_FOREGROUND_HEIGHT):
            frontmost = owner

        if not stream_present and width >= min_width and height >= min_height:
            if needle in owner.lower() or needle in title.lower():
                stream_present = True

    return frontmost, stream_present


class FocusWatcher:
    """'Is the stream active right now' gate, built on CGWindowList.

    require_on_screen=True additionally asserts a stream-sized window exists on the
    active Space, which catches a minimised or closed stream while the app still owns
    the foreground.
    """

    def __init__(self, query="Moonlight", require_on_screen=True,
                 deep_check_interval=DEFAULT_DEEP_CHECK_INTERVAL):
        self.query = query
        self.require_on_screen = require_on_screen
        self.deep_check_interval = deep_check_interval

        self._last_check_at = 0.0
        self._last_result = (False, "", False)

    def _check(self):
        """(active, frontmost, stream_present), re-queried unless throttling is on."""

        now = monotonic()
        if self.deep_check_interval > 0 and now - self._last_check_at < self.deep_check_interval:
            return self._last_result

        frontmost, stream_present = inspect_windows(self.query)
        active = self.query.lower() in frontmost.lower()
        if self.require_on_screen:
            active = active and stream_present

        self._last_result = (active, frontmost, stream_present)
        self._last_check_at = now
        return self._last_result

    def is_active(self):
        return self._check()[0]

    @property
    def last_frontmost(self):
        """Foreground app as of the most recent check, without re-querying.

        Lets a caller log focus changes using the answer it already paid for, rather
        than doubling the per-frame window-server cost.
        """

        return self._last_result[1]

    def status(self):
        active, frontmost, stream_present = self._check()
        return {
            "query": self.query,
            "frontmost": frontmost,
            "on_screen": stream_present,
            "active": active,
        }
