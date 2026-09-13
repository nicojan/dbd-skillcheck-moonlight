"""Timestamp the operator's own key presses, on the recorder's clock.

WHY THIS EXISTS. The `1234!` check draws four to six unfilled arcs in ~2.9 s and gives NO
visual feedback of any kind when a beat is hit — verified 2026-09-12 against an operator
who passed it by hand: duration, arc count, arc widths and the ~250-280 ms terminal needle
freeze are IDENTICAL between a check that passed and one nobody touched. Nothing in the
672 px grab distinguishes them.

So the success signal cannot be recovered from the pixels, and every recording of this
check ever made is unlabelled. The one place the label still exists is the keyboard: if
the operator hits every beat and the check passes, then EVERY press in that recording is a
confirmed success, and a recorded press time turns an unlabelled clip into ~5 labelled
positive examples. That is the calibration set the detector never had.

WHY IT LIVES IN THE ARMED PROCESS AND NOT IN A SCRIPT BESIDE IT. A press has to be placed
on the frame timeline to mean anything, and `bout.json`'s `started` is a `strftime` string
with SECOND resolution. Aligning an external log through it carries up to 500 ms of error
against an arc that lives ~650 ms — which is not a measurement, it is a coin flip. Reading
the 16:44:19 fire off that same second-resolution stamp already cost this project a whole
wrong hypothesis on 2026-09-12. Presses are stamped here with the same `monotonic()` the
recorder stamps frames with, so the two share an origin exactly.

WHAT IT DOES NOT DO. It never blocks the armed loop and never writes a file. The tap
thread's only job is to append a timestamp to a deque; `drain` is called from the loop and
does the rest. A tap that cannot be created is a warning and a `False`, never an exit — a
missing Input Monitoring grant must not cost a match.

THE ONE THING TO WATCH. A tap sees synthetic events too, so an ARMED run records the bot's
own presses alongside the operator's. `source` carries `kCGEventSourceStateID`, and three
presses injected through `directkeys.PressKey` on 2026-09-12 all read **source 0** — so it
probably does separate them, since hardware events are documented to carry the HID system
state. PROBABLY is the operative word: no human press has been read back yet, and the
whole value of this file is that every line in it is a confirmed label. So do not lean on
`source`. The clean calibration run is `--dry-run`, where the bot presses nothing at all
and the question does not arise.
"""

import threading
from collections import deque
from time import monotonic

SPACE_KEYCODE = 49

# Quartz hands a disabled tap back to its own callback rather than raising. Both reasons
# are recoverable by re-enabling, and a tap that silently stops is indistinguishable from
# an operator who stopped pressing — which would read as "the beat was never hit".
_DISABLED = ("timeout", "user input")


class KeyWatcher:
    """A listen-only CGEventTap on a background thread, draining to the caller.

    The timestamp is taken inside the tap callback, so it is the moment the event reached
    this process — not the moment the armed loop got around to draining it. Draining is
    therefore free to lag a frame without costing accuracy, which is what keeps this off
    the hot path.
    """

    def __init__(self, keycodes=(SPACE_KEYCODE,), clock=monotonic):
        self.keycodes = set(keycodes) if keycodes else None
        self.clock = clock
        self.events = deque()           # appended by the tap thread, drained by the loop
        self.presses = 0
        self.re_enabled = 0
        self.error = None
        self._thread = None
        self._runloop = None
        self._tap = None
        self._ready = threading.Event()

    # --- lifecycle -------------------------------------------------------------------

    def start(self):
        """Begin watching. Returns True, or False with the reason in `self.error`.

        Never raises. The caller is an armed match; a keystroke log that cannot start is
        worth a line of warning and nothing more.
        """

        try:
            import Quartz                                   # noqa: F401
        except ImportError as exc:                          # pragma: no cover - env only
            self.error = f"Quartz unavailable ({exc})"
            return False

        self._thread = threading.Thread(target=self._run, name="key-watcher", daemon=True)
        self._thread.start()
        # The tap is created on the thread, so success is only knowable after it tries.
        # Two seconds is far longer than creating a tap takes and far shorter than the
        # operator will notice at launch.
        self._ready.wait(timeout=2.0)
        if self.error is not None:
            return False
        if self._tap is None:
            self.error = "tap did not come up"
            return False
        return True

    def stop(self):
        import Quartz

        if self._runloop is not None:
            Quartz.CFRunLoopStop(self._runloop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # --- the tap thread --------------------------------------------------------------

    def _run(self):
        import Quartz

        try:
            mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
            tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                # Listen-only cannot alter or swallow an event, so a bug here can never
                # eat the operator's own key. The armed loop already injects keys; this
                # must not become a second thing standing between them and the game.
                Quartz.kCGEventTapOptionListenOnly,
                mask,
                self._callback,
                None,
            )
            if not tap:
                # The usual cause, and it is not obvious from the failure: macOS gates a
                # keyboard tap behind Privacy & Security -> Input Monitoring, which is a
                # SEPARATE grant from the Accessibility one that lets this repo inject
                # keys. An app can hold one and not the other.
                self.error = ("tap refused — grant Input Monitoring to this terminal "
                              "(Privacy & Security -> Input Monitoring), then restart it")
                return
            self._tap = tap
            source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            self._runloop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(self._runloop, source, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(tap, True)
        except Exception as exc:                            # pragma: no cover - env only
            self.error = f"tap failed ({exc})"
            return
        finally:
            self._ready.set()

        Quartz.CFRunLoopRun()

    def _callback(self, proxy, type_, event, refcon):
        import Quartz

        try:
            if type_ in (Quartz.kCGEventTapDisabledByTimeout,
                         Quartz.kCGEventTapDisabledByUserInput):
                Quartz.CGEventTapEnable(self._tap, True)
                self.re_enabled += 1
                return event
            code = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode)
            if self.keycodes is None or code in self.keycodes:
                self.events.append({
                    "t": self.clock(),
                    "keycode": int(code),
                    "source": int(Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGEventSourceStateID)),
                })
                self.presses += 1
        except Exception:                                   # pragma: no cover - env only
            # A raising callback would tear the tap down mid-match. Losing one keystroke
            # is recoverable; losing the tap silently is the failure this whole module is
            # built to avoid.
            pass
        return event

    # --- the loop's side -------------------------------------------------------------

    def drain(self):
        """Every press seen since the last call, oldest first.

        `deque.popleft` against the tap thread's `append` needs no lock of its own —
        both are atomic under CPython — and the loop calling this late costs nothing,
        because each event already carries the time it happened.
        """

        out = []
        while True:
            try:
                out.append(self.events.popleft())
            except IndexError:
                return out

    def summary(self):
        note = f"{self.presses} key press(es) recorded"
        if self.re_enabled:
            note += f" — tap re-enabled {self.re_enabled}x"
        return note
