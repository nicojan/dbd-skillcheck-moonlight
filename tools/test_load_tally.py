"""The in-run load sampler, and the two ways it could be wrong without anyone noticing.

The failure this guards is specific: a match that arms clean and goes bad. `bout_20260913-171751`
went in at a 1-minute load of 2.33 — under the gate, so the shell guard said nothing — and
came out with a 5-minute average of 12.77. Three of the four matches lost to load went the
same way, so the case that MATTERS here is not "loud at launch", it is "quiet at launch,
loud in the middle", and a tally that only reported the first or last reading would pass a
naive test and miss every one of them.

The second failure is the opposite direction and is why `scorable` is written the way it
is: a run with no evidence must not be condemned. Unknown is not unscorable, and a tally
that reported "DO NOT SCORE" for an unreadable load average would throw away good matches
to protect a statistic — which is the same trade the shell guard already refuses to make.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dbd.utils.load_tally import DEFAULT_GATE, LoadTally

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


class Fake:
    """A scripted clock and load reader. `values` is consumed one per SAMPLE taken."""

    def __init__(self, values, step=10.0):
        self.values = list(values)
        self.step = step
        self.t = 0.0

    def clock(self):
        return self.t

    def read(self):
        return self.values.pop(0) if self.values else None

    def tally(self, **kw):
        return LoadTally(clock=self.clock, reader=self.read, interval=self.step, **kw)

    def run(self, tally, frames=None):
        """Drive it like the armed loop does — many calls, few samples."""

        frames = len(self.values) * 4 if frames is None else frames
        for _ in range(frames):
            tally.sample()
            self.t += self.step / 4.0   # four frames per sample interval
        return tally


def test_a_quiet_run_is_scorable_and_says_so():
    f = Fake([1.2, 0.9, 2.3, 1.1])
    t = f.run(f.tally())
    check("took a sample per interval, not per frame", t.samples == 4, t.samples)
    check("peak is the highest reading", t.peak == 2.3, t.peak)
    check("scorable", t.scorable is True)
    check("says SCORABLE out loud", "SCORABLE" in t.summary(), t.summary())
    check("does not say DO NOT SCORE", "DO NOT" not in t.summary(), t.summary())


def test_a_match_that_arms_clean_and_goes_bad_is_caught():
    # The 2026-09-13 17:17 shape: fine at launch, 12.77 in the middle, calm at the end.
    f = Fake([2.33, 3.1, 12.77, 9.4, 2.0, 1.8])
    t = f.run(f.tally())
    check("peak is the burst, not the first reading", t.peak == 12.77, t.peak)
    check("peak is not the last reading either", t.last == 1.8, t.last)
    check("NOT scorable", t.scorable is False)
    check("says DO NOT SCORE", "DO NOT SCORE" in t.summary(), t.summary())
    check("counts only the samples over the gate", t.over == 2, t.over)


def test_a_single_sample_at_the_gate_condemns_the_run():
    # At-or-above, not above: the gate is the edge of trustworthy, not a target.
    f = Fake([1.0, DEFAULT_GATE, 1.0])
    t = f.run(f.tally())
    check("a reading exactly at the gate counts as over", t.over == 1, t.over)
    check("and the run is not scorable", t.scorable is False)


def test_unknown_is_not_unscorable():
    f = Fake([])                      # reader returns None: no load average on this platform
    t = f.run(f.tally(), frames=20)
    check("no samples taken", t.samples == 0, t.samples)
    check("counted the unreadable attempts", t.unreadable > 0, t.unreadable)
    check("still scorable — absence of evidence is not evidence", t.scorable is True)
    check("and it says it was not measured", "not measured" in t.summary(), t.summary())


def test_a_run_too_short_to_sample_still_reports():
    f = Fake([4.0])
    t = LoadTally(clock=f.clock, reader=f.read, interval=10.0)
    # The very first call always samples — there is no prior deadline to wait for.
    first = t.sample()
    check("the first call samples immediately", first == 4.0, first)
    check("a second call inside the interval does not", t.sample() is None)
    check("so the sample count stays 1", t.samples == 1, t.samples)


def test_the_gate_is_configurable_and_defaults_to_the_shell_guards():
    check("default gate matches DBD_LOAD_GATE's default of 6", DEFAULT_GATE == 6.0, DEFAULT_GATE)
    f = Fake([8.0, 8.0])
    check("under a raised gate the same load is scorable",
          f.run(f.tally(gate=20)).scorable is True)
    f = Fake([3.0, 3.0])
    check("under a lowered gate a quiet run is not",
          f.run(f.tally(gate=2)).scorable is False)


def test_it_never_raises_on_a_reader_that_explodes():
    # An armed run must survive anything the load reader does. A match is expensive.
    def boom():
        raise OSError("no load average here")

    t = LoadTally(clock=lambda: 0.0, reader=boom)
    try:
        t.sample()
        check("a raising reader does not take the run down", False, "expected it to propagate")
    except OSError:
        # The module's own read_load swallows this; a caller-supplied reader is the
        # caller's problem. Pinning the CURRENT behaviour so a change is deliberate.
        check("a custom reader's exception propagates (read_load is the guarded one)", True)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            print(fn.__doc__ or name)
            fn()
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
