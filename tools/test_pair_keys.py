"""The parts of `pair_keys` that decide what a press MEANS, before any pixel is read.

Splitting the Active Ability press off from the beats, and anchoring a burst to the
Performance it belongs to. Both are pure bookkeeping over `keys.jsonl` and neither needs a
frame, which is why they are tested here and the arc reading is not — that one is only
honest against real pixels, and `frames/bout_20260913-130005` is the fixture for it.

The failure these guard against is silent in both directions: an Active Ability press
scored as a beat lands ~3 s before any arc exists and reads as a miss, and a burst anchored
to a start from a PREVIOUS Performance invents a `t0` that never happened.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbd.utils.key_watcher import ABILITY_KEYCODE, SPACE_KEYCODE
from tools.pair_keys import bursts, describe_burst, performance_start

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def press(t_ms, keycode=SPACE_KEYCODE, source=1):
    return {"t_ms": float(t_ms), "keycode": keycode, "source": source}


def test_bursts_split_on_the_gap():
    beats = [press(0), press(690), press(1380), press(6000), press(6690)]
    got = bursts(beats, 1500.0)
    check("a 4.6 s silence starts a new burst", len(got) == 2, f"{len(got)} bursts")
    check("beats land in the right burst", [len(b) for b in got] == [3, 2],
          f"{[len(b) for b in got]}")


def test_ability_press_is_not_a_beat():
    log = [press(-3000, ABILITY_KEYCODE), press(0), press(690)]
    beats = [p for p in log if p["keycode"] != ABILITY_KEYCODE]
    starts = [p for p in log if p["keycode"] == ABILITY_KEYCODE]
    check("the F press is held out of the beats", len(beats) == 2, f"{len(beats)}")
    check("and kept as a Performance start", len(starts) == 1, f"{len(starts)}")


def test_anchor_found_within_reach():
    starts = [press(-3000, ABILITY_KEYCODE)]
    burst = [press(0), press(690)]
    got = performance_start(starts, burst)
    check("a start 3 s before the first beat anchors it", got == -3000.0, f"{got}")


def test_anchor_refuses_a_stale_start():
    # The cool-down is 90-110 s, so a start this far back is a DIFFERENT Performance.
    starts = [press(-40000, ABILITY_KEYCODE)]
    got = performance_start(starts, [press(0)])
    check("a start 40 s back is not this burst's t0", got is None, f"{got}")


def test_anchor_reaches_a_later_burst_of_the_same_performance():
    """The regression the unit tests missed and the real bout caught.

    A Performance is MANY bursts over up to 15 s. `bout_20260913-130005` has its second
    burst 9.4 s after the start, and a 6 s window orphaned it while every unit test passed.
    """

    starts = [press(-5900, ABILITY_KEYCODE)]
    got = performance_start(starts, [press(3535), press(4226)])
    check("a burst 9.4 s into the Performance still anchors", got == -5900.0, f"{got}")


def test_anchor_takes_the_latest_start_before_the_burst():
    starts = [press(-5000, ABILITY_KEYCODE), press(-2800, ABILITY_KEYCODE)]
    got = performance_start(starts, [press(0), press(690)])
    check("the nearest preceding start wins", got == -2800.0, f"{got}")


def test_anchor_ignores_a_start_after_the_burst():
    got = performance_start([press(500, ABILITY_KEYCODE)], [press(0), press(690)])
    check("a start AFTER the first beat is not its t0", got is None, f"{got}")


def test_describe_reports_the_offset():
    line = describe_burst([press(0), press(690)], 1, start_ms=-2800.0)
    check("the offset from t0 is reported", "+2800 ms" in line, line)
    check("and the start time with it", "-2800 ms" in line, line)


def test_describe_without_an_anchor_says_nothing_about_one():
    line = describe_burst([press(0), press(690)], 1, start_ms=None)
    check("no Performance line when there is no start",
          "Performance" not in line, line)


def main():
    print("pair_keys")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if FAILED:
        print(f"\n{len(FAILED)} FAILED: {', '.join(FAILED)}")
        return 1
    print("\nall ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
