"""The network leg of the round trip, measured on its own, so it can be ruled in or out.

WHY THIS EXISTS. Almost every miss is the first fire after the link snaps fast, and the
measured `round_trip_ms` cannot say WHICH leg moved: network, Sunshine's encode, the
Mac's decode, or the display. Moonlight's stats overlay shows a network figure, but
reading it would mean OCR on the frame the bot is aiming with. An ICMP ping to the
streaming host measures the same leg without touching the capture path.

THIS IS A COVARIATE, NOT A CONTROL. Nothing here changes when the bot presses. An
adaptive lead driven by a running average was tested and lost at every gain (NOTES), so
the readings are stamped on each fire's record for `rescore_policy.py` to test against —
a lead change that rests on them still has to pass a seeded re-score first.

WHAT IS STAMPED. The latest reply, its age, and the worst reply in the last two seconds.
The worst is the one that matters: a spike that resolved 300 ms before the press still
says the link was moving, and "latest" alone would read it as calm. A reply that never
came (ping's "Request timeout") counts as a loss, and losses in the window are stamped too.

THIS NEVER BLOCKS AND NEVER RAISES. One long-lived `ping` process and one reader thread;
a missing host, a missing binary or a dead process leaves every field None, which a
reader must treat as UNKNOWN, never as quiet.
"""

import re
import subprocess
import threading
from collections import deque
from time import monotonic

DEFAULT_HOST = "compute"

# 5 Hz. Unprivileged macOS ping accepts 0.1 s; 0.2 s is fine enough to put a reply within
# a fraction of a needle revolution of every fire, and costs nothing measurable.
INTERVAL_SECONDS = 0.2

# How far back `worst` looks. Two seconds covers the needle's approach to the zone and is
# long enough to hold a spike the latest reply has already forgotten.
WINDOW_SECONDS = 2.0

_REPLY = re.compile(r"icmp_seq=(\d+).*time=([\d.]+) ms")
_TIMEOUT = re.compile(r"Request timeout for icmp_seq (\d+)")


def parse_line(line):
    """('reply', ms) | ('lost', None) | None for anything else ping prints."""

    m = _REPLY.search(line)
    if m:
        return ("reply", float(m.group(2)))
    if _TIMEOUT.search(line):
        return ("lost", None)
    return None


class LinkPinger:
    """Holds recent ping replies; `snapshot()` is what a fire record carries.

    `clock` is injectable so the whole thing is testable without a network; `feed()` is
    the single entry point for a line, whether it came from the real process or a test.
    """

    def __init__(self, host=DEFAULT_HOST, clock=monotonic, window=WINDOW_SECONDS):
        self.host = host
        self.clock = clock
        self.window = window
        self._events = deque()       # (t, ms or None for a loss), pruned to the window
        self._lock = threading.Lock()
        self.last_ms = None
        self.last_at = None
        self.replies = 0
        self.lost = 0
        self.all_ms = []             # for the shutdown line; ~6000 floats a match
        self._proc = None
        self.error = None

    def feed(self, line, now=None):
        parsed = parse_line(line)
        if parsed is None:
            return
        now = self.clock() if now is None else now
        kind, ms = parsed
        with self._lock:
            self._events.append((now, ms))
            if kind == "reply":
                self.replies += 1
                self.last_ms, self.last_at = ms, now
                self.all_ms.append(ms)
            else:
                self.lost += 1
            self._prune(now)

    def _prune(self, now):
        while self._events and now - self._events[0][0] > self.window:
            self._events.popleft()

    def snapshot(self, now=None):
        """The fields a fire record carries. All None when nothing has been heard."""

        now = self.clock() if now is None else now
        with self._lock:
            self._prune(now)
            replies = [ms for _, ms in self._events if ms is not None]
            lost = sum(1 for _, ms in self._events if ms is None)
            return {
                "ping_ms": None if self.last_ms is None else round(self.last_ms, 2),
                "ping_age_ms": (None if self.last_at is None
                                else round((now - self.last_at) * 1000, 0)),
                "ping_worst_2s_ms": round(max(replies), 2) if replies else None,
                "ping_lost_2s": lost if self._events else None,
            }

    def start(self):
        """Spawn ping and its reader thread. Returns False (and says why) on failure."""

        try:
            self._proc = subprocess.Popen(
                ["/sbin/ping", "-n", "-i", str(INTERVAL_SECONDS), self.host],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, bufsize=1)
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            return False
        threading.Thread(target=self._read, daemon=True, name="link-ping").start()
        return True

    def _read(self):
        try:
            for line in self._proc.stdout:
                self.feed(line)
        except Exception as exc:  # a reader thread that dies must not take the run down
            self.error = str(exc)

    def stop(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def summary(self):
        if not self.replies:
            why = self.error or ("no replies" if self.lost else "never heard from")
            return f"ping {self.host}: not measured — {why}"
        s = sorted(self.all_ms)
        pct = lambda q: s[min(len(s) - 1, int(q * len(s)))]
        return (f"ping {self.host}: median {pct(0.5):.1f} ms, p95 {pct(0.95):.1f}, "
                f"max {s[-1]:.1f} over {self.replies} replies, {self.lost} lost")
