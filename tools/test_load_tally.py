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

The third is the warm-up grace, added 2026-09-17, and it faces both ways at once. `dbd`
quits eleven apps immediately before the bot starts and the 1-minute average lags that
teardown by a minute, so without a grace the peak of a quiet evening is the sound of the
machine being made quiet — a DO NOT SCORE for the event that proves the setup worked. But
a grace is also exactly how you would hide a real burst, so the cases below pin both: it
must not count the early spike against the run, it must still REPORT it, it must still
stamp `last` for the fires inside it, and contention that outlasts it must be caught in
full.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dbd.utils.load_tally import DEFAULT_GATE, WARMUP_SECONDS, LoadTally

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
        # warmup=0 unless a test says otherwise. These fakes run a whole match in 40
        # simulated seconds, so the default 90 s grace would swallow every case that is
        # not about the grace; the grace has its own tests below, including one that the
        # DEFAULT is on.
        kw.setdefault("warmup", 0)
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


def test_the_warmup_grace_does_not_count_the_quit_spike():
    """The grace: game mode's own teardown must not condemn a quiet match."""

    # 90 s of grace at one sample per 10 s is the first nine samples. The spike sits in
    # them and nowhere else, which is the shape of a clean evening.
    f = Fake([9.9, 8.4, 7.1, 5.0] + [1.2] * 8, step=10.0)
    t = f.run(f.tally(warmup=WARMUP_SECONDS))
    check("the run is scorable", t.scorable is True, t.summary())
    check("the spike is not the run's peak", t.peak == 1.2, t.peak)
    check("but it is reported, not swallowed", t.warmup_peak == 9.9, t.warmup_peak)
    check("and counted as over-gate inside the grace", t.warmup_over == 3, t.warmup_over)
    check("the summary says the grace caught something", "warm-up grace" in t.summary(),
          t.summary())
    check("and still says SCORABLE", "SCORABLE" in t.summary(), t.summary())


def test_the_grace_hides_nothing_that_outlasts_it():
    """A real burst that straddles the grace is still caught in full."""

    # Loud from the start and STILL loud after 90 s — a build in another session, not a
    # teardown. The grace must not launder this.
    f = Fake([9.9] * 12, step=10.0)
    t = f.run(f.tally(warmup=WARMUP_SECONDS))
    check("NOT scorable", t.scorable is False, t.summary())
    check("says DO NOT SCORE", "DO NOT SCORE" in t.summary(), t.summary())
    check("only the post-grace samples are counted against the gate",
          t.over == t.samples and t.samples == 3, (t.over, t.samples))


def test_a_fire_inside_the_grace_still_carries_its_own_load():
    """`last` is what the check record stamps per fire, and the grace must not touch it."""

    # The run-level verdict is the thing being graced. A fire is judged on its own
    # `load_1min`, so a graced sample that does not update `last` would quietly stamp
    # None (= UNKNOWN) onto the very checks most likely to be corrupt.
    f = Fake([9.9], step=10.0)
    t = LoadTally(clock=f.clock, reader=f.read, interval=10.0, warmup=WARMUP_SECONDS)
    t.sample()
    check("last is set during the grace", t.last == 9.9, t.last)
    check("while the run has nothing to judge yet", t.peak is None, t.peak)
    check("and reports itself as warming", t.warming is True)


def test_a_run_shorter_than_the_grace_says_which_silence_it_is():
    """"Not measured" must not be ambiguous with a broken tally."""

    f = Fake([2.0, 2.2], step=10.0)
    t = f.run(f.tally(warmup=WARMUP_SECONDS))
    check("no counted samples", t.samples == 0, t.samples)
    check("but the grace saw some", t.warmup_samples == 2, t.warmup_samples)
    check("scorable — no evidence against it", t.scorable is True)
    check("and it names the grace as the reason", "warm-up grace" in t.summary(), t.summary())


def test_the_grace_is_on_by_default():
    # The whole point is that a run gets it without autorun.py asking. A default of 0
    # would pass every test above and change nothing in production.
    t = LoadTally(clock=lambda: 0.0, reader=lambda: 1.0)
    check("default warmup is WARMUP_SECONDS", t.warmup == WARMUP_SECONDS, t.warmup)
    check("which is 90 s", WARMUP_SECONDS == 90.0, WARMUP_SECONDS)


def test_a_run_too_short_to_sample_still_reports():
    f = Fake([4.0])
    t = LoadTally(clock=f.clock, reader=f.read, interval=10.0, warmup=0)
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
