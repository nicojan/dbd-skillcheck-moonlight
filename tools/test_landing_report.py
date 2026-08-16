"""Exercise the armed-only code path against recorded frames, without the game.

    .venv/bin/python tools/test_landing_report.py

`report_landing` in autorun.py runs only after a real key press, so a dry-run never
reaches it and the offline replay never touches it. That leaves the bot's self-scoring —
the thing that will tell us whether the offline 29/29 survives the real round trip —
completely unevidenced until the first armed match, which is the worst moment to discover
it is wrong.

The decision it rests on — has the needle actually stopped? — is `freeze_angle`, and it is
driven here with angles measured off real recordings. Both directions matter:

  * a HIT recording, where the needle freezes, must produce a verdict, and
  * an UNHIT recording, where the needle sweeps straight on, must produce NO verdict.

The second is the one that matters. Taking the last angle regardless of whether the needle
actually stopped would report a confident Great about a position the press had nothing to
do with — the failure mode this project has produced over and over.

A first version of this test drove `report_landing` through a stub monitor and passed the
frozen case while silently faking the sweeping one: the function samples against a
wall clock, the stub ran out of frames inside 300 ms and repeated its last one, and three
identical reads look exactly like a freeze. Hence the split — the pure decision is tested
against real angles, and the wall-clock wrapper is tested only for the things a stub can
honestly answer.
"""

import os
import sys
from time import monotonic
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dbd.utils.needle_tracker import (
    TrackerState, find_zone, freeze_angle, freeze_onset, needle_angle, refine_centre,
    score_freeze, static_image,
)
from replay_tracker import load_check

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


class StubMonitor:
    """Hands back recorded frames as RGB, the way grab_screenshot does.

    The channel order is the point of the stub as much as the frames are: recordings are
    BGR on disk and the live capture is RGB, and the needle test is R - max(G,B), so a
    stub that returned BGR would test the opposite of what runs in production.
    """

    def __init__(self, images):
        self.images = images
        self.i = 0

    def grab_screenshot(self):
        img = self.images[min(self.i, len(self.images) - 1)]
        self.i += 1
        return img[:, :, ::-1]  # BGR on disk -> RGB as the capture path delivers it


def zone_and_centre(images):
    static = static_image(images)
    cx, cy, ring_r, _ = refine_centre(static)
    return (cx, cy), find_zone(static, (cx, cy), ring_r)


def capture_log():
    lines = []
    import autorun
    autorun.log = lines.append
    return autorun, lines


def angles_from(check_dir, lo, hi):
    """Needle angles over a slice of a real recording, measured as the live loop would."""

    records, images = load_check(check_dir)
    centre, zone = zone_and_centre(images)
    return [needle_angle(img, centre)[0] for img in images[lo:hi]], zone


def test_a_hit_recordings_tail_reads_as_frozen():
    # A hit stops the needle dead, so the tail of a hit recording is what the live loop
    # sees in the 300 ms after a press that connected.
    # check_001, not check_002: only 10 of the 14 hit recordings actually capture the
    # freeze. The rest end while the needle is still moving, so the notes' "all were hit,
    # so every one has a frozen tail" is true of the checks and not of the recordings.
    angles, zone = angles_from("recordings/check_001", -6, None)
    settled = freeze_angle(angles)
    check("a frozen tail yields a settled angle", settled is not None, angles)
    if settled is not None:
        verdict, err = score_freeze(zone, settled)
        check("and it scores against the drawn zone",
              verdict in ("GREAT", "good", "MISS"), verdict)
        print(f"        settled {settled:.1f} deg -> {verdict} ({err:+.1f} deg)")


def test_a_sweeping_needle_never_reads_as_frozen():
    # An unhit check sweeps straight through. Every window of it must refuse a verdict —
    # not just the one window a test happened to pick.
    records, images = load_check("recordings_missed/check_003")
    centre, _ = zone_and_centre(images)
    angles = [needle_angle(img, centre)[0] for img in images]

    windows = [angles[i:i + 3] for i in range(len(angles) - 12)]  # exclude the dead tail
    verdicts = [freeze_angle(w) for w in windows]
    check("no window of a live sweep reads as frozen",
          all(v is None for v in verdicts),
          f"{sum(v is not None for v in verdicts)} of {len(verdicts)} windows did")


def test_the_boundary_is_the_wobble_not_zero():
    frozen = [211.0, 210.5, 211.0]           # quantisation on a genuinely still needle
    crawling = [211.0, 215.0, 219.0]         # 4 deg a frame is a sweep, not a freeze
    check("half-degree wobble still counts as frozen", freeze_angle(frozen) == 211.0)
    check("a slow sweep does not", freeze_angle(crawling) is None)
    check("too few reads yield nothing", freeze_angle([211.0, 211.0]) is None)

    wrapped = [359.0, 0.5, 359.5]            # a freeze that straddles 0 deg
    check("a freeze across 0 deg is still a freeze", freeze_angle(wrapped) == 359.5,
          freeze_angle(wrapped))


def test_freeze_onset_times_the_moment_the_needle_stopped():
    # The gap between the press going out and the freeze APPEARING in our capture is the
    # round trip, measured through the real pipeline under real load. It is the number the
    # armed run has never had: measure_latency.py reports it idle, in a separate process,
    # against a text field on the host desktop rather than the game.
    sweeping = [(0.000, 100.0), (0.025, 108.0), (0.050, 116.0), (0.075, 124.0)]
    check("a needle that never stops has no onset", freeze_onset(sweeping) is None)

    stopped = sweeping + [(0.100, 130.0), (0.125, 130.5), (0.150, 130.0), (0.175, 130.5)]
    check("the onset is the first read of the frozen tail",
          freeze_onset(stopped) == 0.100, freeze_onset(stopped))

    # The needle sweeps THROUGH the angle it later freezes at. Walking forward for the
    # first matching read would time the fly-past, not the freeze, and report a round trip
    # far shorter than the truth — the flattering direction, and the one that would have
    # let this ship unnoticed.
    revisits = [(0.000, 130.0), (0.025, 200.0), (0.050, 280.0),
                (0.075, 130.0), (0.100, 130.5), (0.125, 130.0)]
    check("an earlier fly-past is not mistaken for the freeze",
          freeze_onset(revisits) == 0.075, freeze_onset(revisits))

    wrapped = [(0.000, 300.0), (0.025, 359.0), (0.050, 0.5), (0.075, 359.5)]
    check("a freeze across 0 deg is timed correctly", freeze_onset(wrapped) == 0.025,
          freeze_onset(wrapped))

    check("too few reads yield no onset", freeze_onset([(0.0, 1.0), (0.025, 1.0)]) is None)


def test_the_round_trip_comes_from_where_the_needle_stopped():
    # Reproduces the armed check of 2026-08-15 22:48. Aimed 131.0, froze at 119.0 — 12 deg
    # short at 326 deg/s, so the press took effect ~37 ms sooner than planned and the true
    # round trip was ~88 ms, not the 130 it led by. The tail-read timing reported 409 ms for
    # the same check, which the needle's own position rules out: at 409 ms it would have
    # been near 222 deg. Deriving the round trip from the fit cannot drift that way.
    from dbd.utils.needle_tracker import Fit, time_to_angle

    fit = Fit(rate_deg_s=326.0, intercept=0.0, rms_deg=1.7, n=8)
    press_ms = 100.0
    settled = fit.angle_at(press_ms + 88.0)          # the freeze, 88 ms of round trip later
    measured = time_to_angle(fit, settled % 360.0, press_ms)
    check("round trip is read off the fit, not the tail reads",
          abs((measured - press_ms) - 88.0) < 1.0, f"{measured - press_ms:.1f} ms")

    # A counter-clockwise check must not read as a near-full revolution of latency.
    ccw = Fit(rate_deg_s=-326.0, intercept=0.0, rms_deg=1.7, n=8)
    settled = ccw.angle_at(press_ms + 88.0)
    measured = time_to_angle(ccw, settled % 360.0, press_ms)
    check("and the same holds counter-clockwise",
          abs((measured - press_ms) - 88.0) < 1.0, f"{measured - press_ms:.1f} ms")


def test_a_wrapped_revolution_is_not_reported_as_latency():
    from dbd.utils.needle_tracker import Fit
    import autorun

    fit = Fit(rate_deg_s=326.0, intercept=0.0, rms_deg=1.7, n=8)
    check("a real latency passes", autorun.plausible_round_trip(88.0, fit))
    # time_to_angle returns the NEXT crossing, so a landing a degree behind the extrapolated
    # press position comes back as a whole revolution (1104 ms at 326 deg/s) rather than a
    # value near zero. One of those in a ten-sample median moves it further than the jitter.
    check("a wrapped revolution does not", not autorun.plausible_round_trip(1104.0, fit))
    check("nor does a negative", not autorun.plausible_round_trip(-5.0, fit))
    check("no fit means no verdict", not autorun.plausible_round_trip(88.0, None))

    # The bound scales with the check: half a revolution is 295 ms at the Hyperfocus ceiling.
    fast = Fit(rate_deg_s=609.0, intercept=0.0, rms_deg=1.7, n=8)
    check("the bound follows the sweep rate", not autorun.plausible_round_trip(400.0, fast)
          and autorun.plausible_round_trip(400.0, fit))


def test_the_summary_reports_what_it_could_not_measure():
    import autorun
    L = autorun.Landing

    check("an empty run says so", "no predictive fire" in autorun.summarise_landings([])[0])

    landings = [
        L("measured", round_trip_ms=88.0, verdict="GREAT", error_deg=1.0),
        L("measured", round_trip_ms=96.0, verdict="GREAT", error_deg=-2.0),
        L("measured", round_trip_ms=104.0, verdict="good", error_deg=4.0),
        L("still sweeping"),
        L("needle gone"),
        L("implausible", verdict="MISS", error_deg=-9.0),
    ]
    text = "\n".join(autorun.summarise_landings(landings))

    check("counts scored against fired", "4 of 6 fires scored" in text, text)
    check("tallies the verdicts", "GREAT 2, good 1, MISS 1" in text, text)
    check("medians the round trips", "median 96 ms, 88-104 (spread 16), n=3" in text, text)
    # The censoring is the point: a spread quoted without the checks it dropped reads
    # tighter than the link is, and the dropped ones are the checks that went worst.
    check("and names what it could not measure",
          "1 implausible" in text and "1 needle gone" in text and "1 still sweeping" in text,
          text)
    check("the implausible one keeps its verdict but not its number",
          "median 96 ms" in text and "n=3" in text, text)


def test_freeze_onset_agrees_with_freeze_angle():
    # The two must never disagree: a settled angle with no onset (or the reverse) would
    # print a round trip for a landing that was refused, or refuse one we had timed.
    records, images = load_check("recordings/check_001")
    centre, _ = zone_and_centre(images)
    angles = [needle_angle(img, centre)[0] for img in images[-6:]]
    readings = [(i * 0.025, a) for i, a in enumerate(angles)]

    check("a real frozen tail has both", (freeze_angle(angles) is None)
          == (freeze_onset(readings) is None))

    records, images = load_check("recordings_missed/check_003")
    centre, _ = zone_and_centre(images)
    angles = [needle_angle(img, centre)[0] for img in images[:-12]]
    readings = [(i * 0.025, a) for i, a in enumerate(angles)]
    check("a real live sweep has neither", freeze_angle(angles) is None
          and freeze_onset(readings) is None)


def test_fire_waits_in_milliseconds():
    # The bug that cost four armed matches: fire() slept `press_at_ms - now_ms` as if it
    # were seconds, so a 20 ms lead became 20 seconds. Nothing caught it because --dry-run
    # returns before the sleep, the reactive path passes 0, and the offline replay never
    # sleeps at all. Only the armed path could ever have shown it, and its symptom was
    # `needle gone before it could be read` — which reads like a detection fault.
    import autorun
    dry = SimpleNamespace(dry_run=True)

    t0 = monotonic()
    autorun.fire(dry, 40.0)
    elapsed_ms = (monotonic() - t0) * 1000.0
    check("a 40 ms lead sleeps ~40 ms, not 40 s", 30.0 <= elapsed_ms <= 200.0,
          f"slept {elapsed_ms:.0f} ms")

    t0 = monotonic()
    pressed_at = autorun.fire(dry, -5.0)
    check("a lead already past does not sleep", (monotonic() - t0) * 1000.0 < 20.0)
    check("fire reports key-down time", t0 <= pressed_at <= monotonic())


def test_stays_silent_in_dry_run_and_without_a_zone():
    records, images = load_check("recordings/check_002")
    centre, zone = zone_and_centre(images)
    autorun, lines = capture_log()
    monitor = StubMonitor(images[-8:])

    autorun.report_landing(monitor, TrackerState(centre=centre, zone=zone), 0.0,
                           SimpleNamespace(dry_run=True), monotonic())
    check("dry run reports nothing", not lines, lines)

    autorun.report_landing(monitor, TrackerState(centre=centre, zone=None), 0.0,
                           SimpleNamespace(dry_run=False), monotonic())
    check("no zone means no verdict", not lines, lines)


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
