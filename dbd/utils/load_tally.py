"""What the machine's load was DURING the match, not just before it.

WHY THIS EXISTS. A contended scheduler inflates `round_trip_ms` indistinguishably from
the link, so a match played under load yields timing data that reads as a link regression
and is not one. The `dbd` shell function already warns when the 1-minute load is above
the gate — but it reads that number ONCE, before the stream is even up, and a match takes
twenty minutes. That is the wrong end of the run to measure.

It has cost four matches. The clearest is 2026-09-13 17:17, which armed at a 1-minute
load of 2.33 — comfortably under the gate, launch warned about nothing — and came out
with a 5-minute average of 12.77. The bias-3.0 target is still unvalidated after four
attempts, and three of the four were lost exactly this way. `NOTES-local.md` responded by
telling the operator to run `uptime` after the match as well as before; that instruction
has now been forgotten four times, which is the normal fate of a manual step that only
matters in hindsight. So the run measures itself instead.

WHAT IT SAMPLES, AND WHY THE PEAK. The 1-minute average, against the same absolute gate
the shell guard uses (`DBD_LOAD_GATE`, default 6) so that the two can never disagree. The
statistic that matters is the PEAK over the run, not the mean and not the final reading:
load arrives in bursts — a build kicking off in another session — and a burst that
straddles ten checks has already corrupted them whether or not the machine is quiet again
by the time the match ends. A mean dilutes exactly the event being looked for.

THIS NEVER BLOCKS ANYTHING, and it never raises. A busy machine is a fine evening to
PLAY; it is only a bad evening to SCORE. Refusing a match to protect a data point is the
wrong way round, and every read is guarded because a load reader that can break an armed
run is worse than no load reader — `os.getloadavg()` is not available on every platform
this file might be imported on.
"""

import os

# Matching the shell guard exactly. An absolute figure rather than per-core, because that
# is what the guard in ~/.zshrc compares and two thresholds that disagree are worse than
# one that is arguable.
DEFAULT_GATE = 6.0

# How often to read. `os.getloadavg()` is a cheap syscall, but the armed loop runs at
# ~35 fps and there is nothing to learn from sampling a 1-MINUTE average 35 times a
# second. Ten seconds is far finer than the statistic's own averaging window.
SAMPLE_SECONDS = 10.0


def read_load():
    """The 1-minute load average, or None where the platform has no such thing."""

    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return None


class LoadTally:
    """Samples the load average on an interval and states whether the run is scorable.

    Pure apart from the two injected collaborators, so the whole thing is testable without
    a game: `clock` is a monotonic source and `reader` returns a 1-minute load average.
    """

    def __init__(self, gate=None, clock=None, reader=read_load,
                 interval=SAMPLE_SECONDS):
        from time import monotonic

        self.gate = DEFAULT_GATE if gate is None else float(gate)
        self.clock = monotonic if clock is None else clock
        self.reader = reader
        self.interval = interval
        self.samples = 0
        self.over = 0            # samples at or above the gate
        self.peak = None         # highest 1-minute average seen
        self.peak_at = None      # clock reading when the peak was taken
        self.last = None         # most recent reading
        self.unreadable = 0
        self._next_at = None
        self._t0 = None

    def sample(self, now=None):
        """Take a reading if the interval has elapsed. Safe to call every frame.

        Returns the reading actually taken, or None when this call was a no-op — so a
        caller that wants to log a crossing can, without the tally deciding for it.
        """

        now = self.clock() if now is None else now
        if self._t0 is None:
            self._t0 = now
        if self._next_at is not None and now < self._next_at:
            return None
        self._next_at = now + self.interval

        value = self.reader()
        if value is None:
            self.unreadable += 1
            return None

        self.samples += 1
        self.last = value
        if value >= self.gate:
            self.over += 1
        if self.peak is None or value > self.peak:
            self.peak, self.peak_at = value, now
        return value

    @property
    def scorable(self):
        """False only when a reading actually proved otherwise.

        Unknown is NOT unscorable. A run on a platform with no load average, or one too
        short to take a single sample, has no evidence against it, and inventing some
        would quietly discard good matches — the opposite of this file's purpose.
        """

        return self.peak is None or self.peak < self.gate

    def summary(self):
        """One line for the shutdown block. Always says something, including when quiet."""

        if self.samples == 0:
            why = "no readable load average" if self.unreadable else "run too short to sample"
            return f"load: not measured — {why}"

        head = (f"load: peak {self.peak:.2f}, last {self.last:.2f} "
                f"over {self.samples} samples (gate {self.gate:g})")
        if self.scorable:
            # A quiet run has to say so out loud. "Nothing was printed" is not evidence
            # the machine was idle — it is indistinguishable from the tally never running.
            return head + " — SCORABLE"
        held = self.over * self.interval
        return (head + f" — DO NOT SCORE: {self.over} of {self.samples} samples at or "
                f"above the gate (~{held:.0f}s). Round trips reflect the LOAD, not the link.")
