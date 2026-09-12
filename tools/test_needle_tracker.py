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
    AIM_BIAS_DEG, CENTRE_PRIOR, Fit, MIN_NEEDLE_STRENGTH, OUTLINE_AIM_DEG, OUTLINE_KEEP_DEG,
    OUTLINE_MAX_DEG, OUTLINE_MIN_DEG, Reading, Sample, TrackerState, ZONE_KEEP_DEG,
    Zone, aim_bias_for, decide, find_outline_arc, find_zone,
    fit_sweep, leading_edge, lit_floor, lit_span, read_watch, score_freeze,
    strength_reference, time_to_angle, trim_frozen_tail, _longest_run,
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
        # The aim sits the bias past the middle of the band, in the needle's direction.
        # Asked of `aim_bias_for` rather than recomputed here: a test that restates the
        # clamp cannot catch the clamp being wrong, and this one did not when the bound
        # moved from the Great band to the zone.
        expected_travel = (120.0 + aim_bias_for(zone)) / abs(rate) * 1000.0
        check(f"decide leads by the round trip at {rate:+.0f} deg/s",
              decision.press_at_ms is not None
              and abs(decision.lands_at_ms - (now + expected_travel)) < 2.0
              and abs(decision.lands_at_ms - decision.press_at_ms - 72.0) < 1e-6,
              f"{decision.reason}")


def test_aim_bias_is_clamped_to_the_zone_not_the_great_band():
    """The bound that matters is the SUCCESS zone's trailing edge, not the Great band's.

    The old clamp was `great_width / 4`, which answers "how far can I go and still be
    Great" — the wrong question once a good beats a miss. These cases pin the difference:
    on a normal check the bias is applied in full where the old bound would have clipped
    it to 2.5, and on a zone too narrow to hold it the new bound still binds.
    """

    # A drawn check: 10 deg Great at the leading edge of a 49 deg zone. Room to the
    # trailing edge is 49 - 5 - 8 = 36 deg, so the full bias applies.
    normal = Zone(great_start=0.0, great_end=10.0, zone_start=0.0, zone_end=49.0)
    check("the full bias applies on a normal zone",
          abs(aim_bias_for(normal) - AIM_BIAS_DEG) < 1e-9,
          f"got {aim_bias_for(normal)}")
    check("...which the old great_width/4 bound would have clipped",
          min(AIM_BIAS_DEG, normal.great_width / 4.0) < AIM_BIAS_DEG,
          "the old bound no longer binds here, so this test proves nothing")

    # A zone barely wider than its own Great: room is 14 - 5 - 8 = 1 deg.
    narrow = Zone(great_start=0.0, great_end=10.0, zone_start=0.0, zone_end=14.0)
    check("a narrow zone clamps the bias down",
          abs(aim_bias_for(narrow) - 1.0) < 1e-9, f"got {aim_bias_for(narrow)}")

    # `full white` fills its zone with one solid block, so the band reads 33-59 deg. That
    # is NOT a no-room case, and asserting it was is how this test first failed: great_mid
    # sits in the middle of the block, so a 50 deg zone still leaves 17 deg behind the aim.
    full = Zone(great_start=0.0, great_end=50.0, zone_start=0.0, zone_end=50.0)
    check("a full-white zone has room and takes the full bias",
          abs(aim_bias_for(full) - AIM_BIAS_DEG) < 1e-9, f"got {aim_bias_for(full)}")

    # The case that genuinely has no room: a zone no wider than its own Great. The aim
    # must fall back to great_mid rather than going negative and aiming EARLY, which is
    # the one direction that turns a good into a miss.
    cramped = Zone(great_start=0.0, great_end=10.0, zone_start=0.0, zone_end=10.0)
    check("a zone with no room gives no bias rather than a negative one",
          aim_bias_for(cramped) == 0.0, f"got {aim_bias_for(cramped)}")
    check("and no zone at all is the same",
          aim_bias_for(None) == 0.0, f"got {aim_bias_for(None)}")

    check("the keep margin is what separates the two narrow cases",
          ZONE_KEEP_DEG == 8.0, f"got {ZONE_KEEP_DEG}")


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


def test_a_full_white_zone_is_not_graded():
    """A `full white` check reads as one solid block, so the Great run fills the zone.

    Nine such fires on 2026-08-16 all scored GREAT against a 33-59 deg "Great band" —
    every landing inside the zone was a Great by construction, which flattered the match
    tally from 78% to 84%. The band is unmeasured on this type, so it must not be graded.
    """

    solid = Zone(great_start=100.0, great_end=159.0, zone_start=100.0, zone_end=160.0)
    check("a real Great band is gradeable",
          Zone(great_start=100.0, great_end=110.0, zone_start=100.0,
               zone_end=150.0).great_measured)
    check("a zone-wide Great band is not", not solid.great_measured,
          f"great_width={solid.great_width}")
    check("a landing inside an ungraded zone is not called GREAT",
          score_freeze(solid, 130.0)[0] == "ungraded", score_freeze(solid, 130.0))
    check("a landing outside it is still a MISS",
          score_freeze(solid, 300.0)[0] == "MISS", score_freeze(solid, 300.0))
    check("the error is still reported, for the spread",
          score_freeze(solid, 130.0)[1] is not None)

    # Hyperfocus SHRINKS the Great band, so narrow-but-real must keep grading.
    narrow = Zone(great_start=100.0, great_end=106.5, zone_start=100.0, zone_end=150.0)
    check("a shrunken Hyperfocus band is still graded", narrow.great_measured,
          f"great_width={narrow.great_width}")


def watch_readings(spec, dt_ms=25.0, t0=0.0):
    """(angle, strength) pairs on a fixed cadence, as the freeze watch collects them."""

    return tuple(Reading(t0 + i * dt_ms / 1000.0, a, s) for i, (a, s) in enumerate(spec))


def test_the_strength_floor_is_relative_to_the_checks_own_peak():
    # A drawn needle scores 70-150; the strays left behind once the check clears reach
    # 20-45, which clears the absolute floor of 20. Judging against the check's own peak
    # is what separates them, and it is why lit_span exists — the freeze watch was reading
    # the strays as needle and taking their jittering angles as "still sweeping".
    check("the reference is the check's own peak, not its mean",
          abs(strength_reference([30.0, 100.0, 110.0, 120.0, 130.0]) - 125.0) < 1e-6,
          strength_reference([30.0, 100.0, 110.0, 120.0, 130.0]))
    check("no strengths, no reference", strength_reference([]) is None)

    floor = lit_floor(120.0)
    check("a stray at 40 falls below a 120-peak floor", floor > 45.0, floor)
    check("a needle at 90 clears it", floor <= 90.0, floor)
    check("without a reference the absolute floor still applies",
          lit_floor(None) == MIN_NEEDLE_STRENGTH, lit_floor(None))
    # A dim check must not raise its own floor above its own needle.
    check("the floor never exceeds the absolute minimum for a dim check",
          lit_floor(20.0) == MIN_NEEDLE_STRENGTH, lit_floor(20.0))


def test_a_freeze_followed_by_the_check_clearing_still_reads_as_frozen():
    # THE BUG THIS EXISTS FOR. freeze_angle was applied to the last three of ALL readings.
    # A press that connects freezes the needle, and the check then leaves the screen well
    # inside the 800 ms watch — so the last three readings are strays, they disagree, and
    # a landing that was perfect reports "still sweeping": indistinguishable in the log
    # from a press that never arrived. Six of nine armed fires printed that line.
    frozen_then_gone = watch_readings(
        [(100.0, 110.0), (107.0, 115.0), (114.0, 120.0),   # still sweeping
         (118.0, 118.0), (118.5, 116.0), (118.0, 119.0),   # the freeze
         (118.5, 117.0),
         (12.0, 31.0), (300.0, 28.0), (77.0, 35.0)])       # check gone, strays only
    watch = read_watch(frozen_then_gone, reference=118.0)
    check("the freeze is found inside the lit block", watch.outcome == "frozen", watch)
    check("and the settled angle is the needle, not a stray",
          watch.angle is not None and abs(watch.angle - 118.5) < 1.0, watch)
    check("the onset is the first frozen read",
          watch.onset is not None and abs(watch.onset - 3 * 0.025) < 1e-6, watch)
    check("the strays are excluded from the lit block", watch.lit == 7, watch)
    check("and are counted as a dark tail", watch.dark_tail == 3, watch)


def test_a_check_that_sweeps_to_the_end_and_vanishes_is_not_a_freeze():
    # A press that never reached the game leaves the check to run out its sweep and
    # disappear. That must NOT read as frozen, and it is worth distinguishing from a
    # needle still sweeping at the end of the window — one says the press was lost, the
    # other says the watch was too short or this is Merciless Storm.
    swept_then_gone = watch_readings(
        [(a, 110.0) for a in (100.0, 107.0, 114.0, 121.0, 128.0, 135.0)]
        + [(11.0, 30.0), (250.0, 26.0), (140.0, 33.0), (9.0, 29.0)])
    watch = read_watch(swept_then_gone, reference=110.0)
    check("a sweep that ends in nothing is not frozen", watch.outcome == "sweeping", watch)
    check("and the dark tail says the check cleared", watch.dark_tail == 4, watch)

    still_going = watch_readings([(100.0 + 7 * i, 110.0) for i in range(12)])
    watch = read_watch(still_going, reference=110.0)
    check("a needle lit and moving throughout is still sweeping",
          watch.outcome == "sweeping" and watch.dark_tail == 0, watch)


def test_a_watch_that_saw_nothing_says_so():
    check("no readings at all", read_watch((), reference=110.0).outcome == "no reads")
    check("too few to judge",
          read_watch(watch_readings([(1.0, 110.0), (8.0, 110.0)]),
                     reference=110.0).outcome == "no reads")

    strays_only = watch_readings([(11.0, 30.0), (250.0, 26.0), (140.0, 33.0), (9.0, 29.0)])
    watch = read_watch(strays_only, reference=110.0)
    check("strays alone are darkness, not a sweep", watch.outcome == "dark", watch)
    check("and none of them counted as lit", watch.lit == 0, watch)


def test_the_lit_block_is_the_longest_run_not_the_first():
    # A dropped frame mid-freeze must not truncate the block and hide the freeze behind
    # a two-read fragment.
    gappy = watch_readings(
        [(100.0, 110.0), (12.0, 20.0),                     # one dropped read
         (114.0, 115.0), (118.0, 118.0), (118.5, 116.0), (118.0, 119.0)])
    watch = read_watch(gappy, reference=118.0)
    check("the longest lit run is the one judged", watch.outcome == "frozen", watch)
    check("and the dropped read is not in it", watch.lit == 4, watch)



# --- the relocating outline check (`1234!`) ------------------------------------------

def drawn_check(start_deg, width_deg, fill_px, centre=CENTRE_PRIOR, ring_r=65.0):
    """A synthetic static image: the base ring everywhere, plus one arc over `width_deg`.

    `fill_px` is the arc's RADIAL thickness, which is the only thing separating an outline
    from a solid band — 2 px is what `1234!` and Merciless Storm both draw, 8 px is an
    ordinary Great. Built as an image rather than as a Zone because the width bounds that
    keep this off Storm live in the pixel reader, and a test that hands `find_outline_arc`
    a ready-made Zone cannot exercise them.
    """

    img = np.zeros((224, 224), dtype=np.float32)
    ys, xs = np.mgrid[0:224, 0:224]
    r = np.hypot(xs - centre[0], ys - centre[1])
    # Angle 0 is up and increases clockwise, matching `sample_rays`.
    theta = np.rad2deg(np.arctan2(xs - centre[0], centre[1] - ys)) % 360.0

    # The arc is drawn OUTSIDE the base ring, not over it. Overlapping them hides the
    # first pixel of the arc: `find_outline_arc` subtracts the per-radius median over
    # angle to remove the ring, so at a radius the ring occupies, the arc has to clear
    # HOT above the ring's own brightness rather than above the dark background.
    img[(r >= ring_r - 1.0) & (r <= ring_r + 1.0)] = 200.0   # base ring, every angle
    on_arc = ((theta - start_deg) % 360.0) <= width_deg
    img[on_arc & (r >= ring_r + 1.5) & (r <= ring_r + 1.5 + fill_px)] = 255.0
    return img


def test_find_outline_arc_reads_a_wide_unfilled_arc():
    arc = find_outline_arc(drawn_check(130.0, 111.0, fill_px=2.0), CENTRE_PRIOR, 65.0)
    check("a 111 deg outline arc is found", arc is not None)
    if arc is not None:
        check("...as an outline zone", arc.outline)
        check("...at the drawn leading edge",
              abs((arc.zone_start - 130.0 + 180.0) % 360.0 - 180.0) <= 4.0,
              f"got {arc.zone_start}")
        check("...with the drawn width",
              abs(arc.zone_width - 111.0) <= 6.0, f"got {arc.zone_width}")


def test_find_outline_arc_refuses_merciless_storm():
    """THE safety test. Storm draws the same unfilled outline at 39-40 deg and the tracker
    is supposed to abstain on it; a width floor is the only thing separating the two."""

    for width in (13.0, 39.0, 56.0):      # the range measured across both Storm sets
        arc = find_outline_arc(drawn_check(90.0, width, fill_px=2.0), CENTRE_PRIOR, 65.0)
        check(f"a {width:.0f} deg outline (Merciless Storm) is refused", arc is None,
              f"got {arc}")
    check("the floor is what refuses them", OUTLINE_MIN_DEG > 56.0,
          f"OUTLINE_MIN_DEG={OUTLINE_MIN_DEG}")
    check("and the ceiling refuses a near-complete ring",
          find_outline_arc(drawn_check(0.0, 300.0, fill_px=2.0), CENTRE_PRIOR, 65.0) is None)


def test_find_outline_arc_refuses_anything_with_a_solid_band():
    """`find_zone` returning None is not proof the check is an outline — it also returns
    None for a band too narrow to pass MIN_GREAT_DEG. Aiming a 111 deg rule at one of
    those would aim a leading-edge offset at a zone that has a real band."""

    solid = drawn_check(130.0, 111.0, fill_px=8.0)
    check("a solid 111 deg block is not an outline arc",
          find_outline_arc(solid, CENTRE_PRIOR, 65.0) is None)
    check("...because find_zone owns it", find_zone(solid, CENTRE_PRIOR, 65.0) is not None)


def test_an_outline_zone_is_never_graded():
    """It has no Great band to have landed in, so a press there is a hit, not a Great."""

    outline = Zone(great_start=10.0, great_end=121.0, zone_start=10.0, zone_end=121.0,
                   outline=True)
    check("an outline zone reports no measured Great band", not outline.great_measured)
    check("a landing inside it is ungraded, not GREAT",
          score_freeze(outline, 60.0)[0] == "ungraded", score_freeze(outline, 60.0))
    check("a landing outside it is still a MISS",
          score_freeze(outline, 200.0)[0] == "MISS", score_freeze(outline, 200.0))


def test_leading_edge_follows_the_direction_of_travel():
    zone = Zone(great_start=10.0, great_end=121.0, zone_start=10.0, zone_end=121.0,
                outline=True)
    check("clockwise, the needle meets zone_start first",
          leading_edge(zone, +300.0) == 10.0)
    check("counter-clockwise (Madness), it meets zone_end first",
          leading_edge(zone, -300.0) == 121.0)


def outline_state(rate, needle_now, edge_offset, width=111.0, n=8, dt_ms=25.0):
    """A tracker sitting `edge_offset` deg (along travel) short of a `width` deg arc."""

    start = (needle_now - rate * (n - 1) * dt_ms / 1000.0) % 360.0
    samples = sweep_samples(rate, start_deg=start, n=n, dt_ms=dt_ms)
    direction = 1.0 if rate > 0 else -1.0
    edge = (needle_now + edge_offset * direction) % 360.0
    far = (edge + width * direction) % 360.0
    zone = Zone(great_start=edge, great_end=far,
                zone_start=edge if rate > 0 else far,
                zone_end=far if rate > 0 else edge, outline=True)
    return samples, TrackerState(samples=samples, zone=zone, centre_fixed=True)


def test_decide_presses_only_once_the_needle_is_inside_the_outline_arc():
    """The press is the SOONEST landing inside the arc, never a scheduled wait. The arc
    expires on its own ~679 ms timer, so anything the tracker waits for it may not live
    to see — and a press aimed a whole revolution ahead is a press onto nothing."""

    for rate in (300.0, -300.0):
        # Needle 90 deg short of the arc: a press now lands well before it.
        samples, state = outline_state(rate, needle_now=0.0, edge_offset=90.0)
        d = decide(state, samples[-1].t_ms, round_trip_ms=46.0)
        check(f"short of the arc, no press is scheduled at {rate:+.0f} deg/s",
              d.press_at_ms is None and "short of the arc" in d.reason, d.reason)
        check(f"...and it does not fall back to reacting at {rate:+.0f} deg/s",
              not d.may_react, d.reason)

        # Needle just inside: a press now lands OUTLINE_AIM_DEG or more past the edge.
        samples, state = outline_state(rate, needle_now=0.0, edge_offset=-5.0)
        now = samples[-1].t_ms
        d = decide(state, now, round_trip_ms=46.0)
        travel = abs(rate) * 46.0 / 1000.0
        check(f"inside the arc, the press goes immediately at {rate:+.0f} deg/s",
              d.press_at_ms is not None and abs(d.press_at_ms - now) < 1e-6, d.reason)
        if d.press_at_ms is not None:
            landed = ((d.target_deg - state.zone.zone_start) * (1 if rate > 0 else -1)) % 360.0
            if rate < 0:
                landed = ((state.zone.zone_end - d.target_deg)) % 360.0
            check(f"...landing inside the arc at {rate:+.0f} deg/s",
                  OUTLINE_AIM_DEG <= landed <= 111.0 - OUTLINE_KEEP_DEG,
                  f"landed {landed:.1f} deg past the leading edge")
            check(f"...exactly one round trip ahead at {rate:+.0f} deg/s",
                  abs(d.lands_at_ms - d.press_at_ms - 46.0) < 1e-6
                  and abs(travel - abs(rate) * 0.046) < 1e-9)

        # Needle near the far end: declined rather than aimed at the trailing edge.
        samples, state = outline_state(rate, needle_now=0.0, edge_offset=-90.0)
        d = decide(state, samples[-1].t_ms, round_trip_ms=46.0)
        check(f"past the arc, no press is scheduled at {rate:+.0f} deg/s",
              d.press_at_ms is None and "trailing edge" in d.reason, d.reason)


def test_the_outline_path_leaves_ordinary_checks_alone():
    """A drawn zone with a real Great band must aim at the band, not at a leading edge."""

    samples = sweep_samples(320.0, start_deg=0.0, n=8)
    now = samples[-1].t_ms
    here = (320.0 * now / 1000.0) % 360.0
    mid = (here + 120.0) % 360.0
    zone = Zone(great_start=(mid - 5.0) % 360.0, great_end=(mid + 5.0) % 360.0,
                zone_start=(mid - 5.0) % 360.0, zone_end=(mid + 44.0) % 360.0)
    d = decide(TrackerState(samples=samples, zone=zone, centre_fixed=True), now, 72.0)
    check("an ordinary zone still aims at great_mid plus the bias",
          d.press_at_ms is not None
          and abs((d.target_deg - (zone.great_mid + aim_bias_for(zone))) % 360.0) < 1e-6,
          f"{d.reason} target={d.target_deg}")


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
