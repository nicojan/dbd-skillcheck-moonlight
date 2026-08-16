"""Unit tests for the predictive tracker's logic.

    .venv/bin/python tools/test_needle_tracker.py

`tools/replay_tracker.py` is the tracker's real evidence — it runs the whole thing against
recorded checks and scores where the press lands. These tests cover what the recordings
cannot:

  * the counter-clockwise path. The Doctor's Madness reverses a check, and the only
    reversed footage we have is Merciless Storm, which draws no solid Great band, so no
    recorded check can score a reversed press. Synthetic angles are the only way to test
    it, and a direction bug is silent and mis-times by twice the lead.
  * the geometry helpers at their boundaries, where a wrap-around bug hides. One already
    lived here: searching a slice of the circle circularly joined the zone's trailing and
    leading ends into a phantom Great band, landing the press ~50 deg early.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.utils.needle_tracker import (
    AIM_BIAS_DEG, Fit, Sample, TrackerState, Zone, decide, fit_sweep, lit_span, score_freeze,
    time_to_angle, trim_frozen_tail, _longest_run,
)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def sweep_samples(rate_deg_s, start_deg=0.0, n=20, dt_ms=25.0, noise=0.0, seed=0):
    """A synthetic constant-velocity sweep, wrapped into 0-360 as the detector reports it."""

    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        t = i * dt_ms
        angle = (start_deg + rate_deg_s * t / 1000.0 + rng.normal(0, noise)) % 360.0
        out.append(Sample(t, angle, 120.0))
    return tuple(out)


def test_fit_recovers_rate_and_sign():
    for rate in (327.0, -327.0, 406.0, -290.0):
        fit = fit_sweep(sweep_samples(rate))
        check(f"fit recovers {rate:+.0f} deg/s",
              fit is not None and abs(fit.rate_deg_s - rate) < 1.0,
              f"got {fit.rate_deg_s if fit else None}")


def test_fit_survives_the_wrap():
    # A sweep starting at 300 deg crosses 360 partway through. Without unwrapping, the
    # fitted rate collapses towards zero and the press is scheduled a revolution out.
    fit = fit_sweep(sweep_samples(327.0, start_deg=300.0))
    check("fit survives a 0/360 crossing",
          fit is not None and abs(fit.rate_deg_s - 327.0) < 1.0,
          f"got {fit.rate_deg_s if fit else None}")


def test_time_to_angle_both_directions():
    # Clockwise from 10 deg at 300 deg/s reaches 100 deg in 300 ms.
    cw = Fit(rate_deg_s=300.0, intercept=10.0, rms_deg=1.0, n=10)
    check("cw reaches a target ahead of it",
          abs(time_to_angle(cw, 100.0, 0.0) - 300.0) < 1e-6,
          f"got {time_to_angle(cw, 100.0, 0.0)}")

    # Counter-clockwise from 10 deg reaches 100 deg only after going nearly all the way
    # round: 270 deg of travel, 900 ms. A clockwise assumption would say 300 ms.
    ccw = Fit(rate_deg_s=-300.0, intercept=10.0, rms_deg=1.0, n=10)
    check("ccw travels the long way to the same target",
          abs(time_to_angle(ccw, 100.0, 0.0) - 900.0) < 1e-6,
          f"got {time_to_angle(ccw, 100.0, 0.0)}")

    # And reaches a target behind it quickly.
    check("ccw reaches a target behind it",
          abs(time_to_angle(ccw, 340.0, 0.0) - 100.0) < 1e-6,
          f"got {time_to_angle(ccw, 340.0, 0.0)}")


def test_decide_leads_by_the_round_trip_both_directions():
    """The press must be scheduled so the needle ARRIVES at Great, not so it leaves then."""

    for rate in (320.0, -320.0):
        samples = sweep_samples(rate, start_deg=0.0, n=8)
        now = samples[-1].t_ms
        # Put the Great band 120 deg ahead in the needle's own direction.
        here = (rate * now / 1000.0) % 360.0
        mid = (here + 120.0 * (1 if rate > 0 else -1)) % 360.0
        zone = Zone(great_start=(mid - 5.0) % 360.0, great_end=(mid + 5.0) % 360.0,
                    zone_start=(mid - 5.0) % 360.0, zone_end=(mid + 44.0) % 360.0)
        state = TrackerState(samples=samples, zone=zone, centre_fixed=True)

        decision = decide(state, now, round_trip_ms=72.0)
        # The aim sits a degree past the middle of the band, in the needle's direction.
        expected_travel = (120.0 + min(AIM_BIAS_DEG, 10.0 / 4.0)) / abs(rate) * 1000.0
        check(f"decide leads by the round trip at {rate:+.0f} deg/s",
              decision.press_at_ms is not None
              and abs(decision.lands_at_ms - (now + expected_travel)) < 2.0
              and abs(decision.lands_at_ms - decision.press_at_ms - 72.0) < 1e-6,
              f"{decision.reason}")


def test_decide_refuses_when_it_cannot_win():
    samples = sweep_samples(320.0, n=8)
    now = samples[-1].t_ms
    here = (320.0 * now / 1000.0) % 360.0

    # Great 10 deg ahead is 31 ms away — inside the 72 ms round trip, so unreachable.
    near = Zone((here + 5.0) % 360, (here + 15.0) % 360, (here + 5.0) % 360, (here + 54) % 360)
    late = decide(TrackerState(samples=samples, zone=near), now, 72.0)
    check("refuses a Great band closer than the round trip",
          late.press_at_ms is None and "too late" in late.reason, late.reason)

    # No zone drawn at all: Merciless Storm. Guessing is worse than not pressing.
    none = decide(TrackerState(samples=samples, zone=None), now, 72.0)
    check("refuses when no Great band is drawn",
          none.press_at_ms is None, none.reason)

    # Static UI: a confident class on a menu, with a needle that never moves.
    still = tuple(Sample(i * 25.0, 42.0, 120.0) for i in range(10))
    frozen = decide(TrackerState(samples=still, zone=near), 225.0, 72.0)
    check("refuses a needle that is not sweeping",
          frozen.press_at_ms is None, frozen.reason)


def test_trim_frozen_tail_tolerates_quantisation():
    """A frozen needle wobbles; a strict stall test lets the tail through."""

    sweeping = list(sweep_samples(320.0, n=12, dt_ms=25.0))
    frozen_at = sweeping[-1].angle
    wobble = [0.0, -0.5, 0.5, 0.0, -0.5, 0.5]  # what quantisation looks like on a still needle
    tail = [Sample(sweeping[-1].t_ms + (i + 1) * 25.0, (frozen_at + w) % 360.0, 120.0)
            for i, w in enumerate(wobble)]

    kept = trim_frozen_tail(tuple(sweeping + tail))
    check("frozen tail is trimmed despite half-degree wobble",
          len(kept) <= len(sweeping) + 1, f"kept {len(kept)} of {len(sweeping) + len(tail)}")

    fit = fit_sweep(kept)
    check("rate survives the freeze",
          fit is not None and abs(fit.rate_deg_s - 320.0) < 5.0,
          f"got {fit.rate_deg_s if fit else None}")

    # And a counter-clockwise sweep must not be mistaken for a stall on frame two.
    ccw = sweep_samples(-320.0, n=12)
    check("counter-clockwise sweep is not trimmed as frozen",
          len(trim_frozen_tail(ccw)) == len(ccw),
          f"kept {len(trim_frozen_tail(ccw))} of {len(ccw)}")


def test_lit_span_drops_stray_red():
    """Frames either side of a check carry stray red that the classifier still labels."""

    real = [Sample(i * 25.0, i * 8.0, 125.0) for i in range(20)]
    stray = [Sample((20 + i) * 25.0, 300.0 - i * 3.0, 35.0) for i in range(8)]
    start, stop = lit_span(tuple(real + stray))
    check("stray-red tail is excluded", (start, stop) == (0, 20), f"got {(start, stop)}")


def test_longest_run_wrapping():
    mask = np.array([True, True, False, False, True, True, True])
    check("circular run joins the ends", _longest_run(mask, 1.0, min_deg=0, circular=True)
          == (4, 5), f"got {_longest_run(mask, 1.0, min_deg=0, circular=True)}")
    check("linear run does not", _longest_run(mask, 1.0, min_deg=0, circular=False)
          == (4, 3), f"got {_longest_run(mask, 1.0, min_deg=0, circular=False)}")


def test_score_freeze():
    zone = Zone(great_start=100.0, great_end=110.0, zone_start=100.0, zone_end=150.0)
    check("freeze inside Great scores GREAT", score_freeze(zone, 105.0)[0] == "GREAT")
    check("freeze past Great scores good", score_freeze(zone, 130.0)[0] == "good")
    check("freeze outside the zone scores MISS", score_freeze(zone, 200.0)[0] == "MISS")
    check("no zone means no verdict", score_freeze(None, 105.0)[0] == "unknown")

    wrapped = Zone(great_start=355.0, great_end=5.0, zone_start=355.0, zone_end=45.0)
    check("a Great band across 0 deg still scores",
          score_freeze(wrapped, 2.0)[0] == "GREAT", score_freeze(wrapped, 2.0))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            print(f"\n{name}")
            fn()

    print()
    if FAILURES:
        raise SystemExit(f"{len(FAILURES)} failing: {', '.join(FAILURES)}")
    print("all passing")


if __name__ == "__main__":
    main()
