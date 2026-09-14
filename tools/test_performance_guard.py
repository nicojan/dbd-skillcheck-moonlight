"""The stand-down that keeps the bot's hands off a `1-2-3-4!` Performance.

The operator plays every beat of a Performance by hand, so a bot press inside one can only
ever spoil a beat: a missed check cancels the whole Performance, and the arcs are ~110 deg
with the needle inside for ~367 ms, so there is no aim problem for the bot to solve. The
hold is keyed on the operator's own Active Ability press, which is the only signal about
this check that has ever been reliable — the check itself gives no visual feedback at all.

`performance_deadline` is the whole decision. The branch that consumes it lives inside the
armed loop and is not reachable without a game, so this covers the part that can be wrong
in a way nobody would notice: a hold that never starts, or one that ends early.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbd.utils.key_watcher import ABILITY_KEYCODE, SPACE_KEYCODE
from tools.autorun import PERFORMANCE_SECONDS, performance_deadline

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def event(t, keycode=SPACE_KEYCODE, source=1):
    return {"t": float(t), "keycode": keycode, "source": source}


def test_no_events_leaves_the_deadline_alone():
    check("an empty drain does not move the deadline",
          performance_deadline([], 100.0) == 100.0)


def test_space_alone_never_starts_a_hold():
    beats = [event(1.0), event(1.69), event(2.38)]
    got = performance_deadline(beats, 0.0)
    check("beats do not start a Performance hold", got == 0.0, f"{got}")


def test_ability_press_starts_the_hold():
    got = performance_deadline([event(10.0, ABILITY_KEYCODE)], 0.0)
    check("an Active Ability press holds for PERFORMANCE_SECONDS",
          got == 10.0 + PERFORMANCE_SECONDS, f"{got}")


def test_hold_covers_a_whole_performance():
    """15 s is the perk's own maximum, so the hold has to outlast it."""

    start = 10.0
    got = performance_deadline([event(start, ABILITY_KEYCODE)], 0.0)
    check("the hold outlasts a full 15 s Performance", got >= start + 15.0, f"{got}")


def test_a_second_press_cannot_shorten_a_running_hold():
    """The press that starts a Performance and one that fails to look identical to a tap.

    A press on cool-down, or while not standing idle, starts nothing — but it still arrives
    as keycode 3. Taking the LATEST deadline means such a press can only ever extend the
    hold, never cut one short and let the bot back in mid-Performance.
    """

    running = 100.0 + PERFORMANCE_SECONDS
    got = performance_deadline([event(95.0, ABILITY_KEYCODE)], running)
    check("an earlier press cannot cut the hold short", got == running, f"{got}")


def test_a_later_press_extends_the_hold():
    running = 100.0 + PERFORMANCE_SECONDS
    got = performance_deadline([event(120.0, ABILITY_KEYCODE)], running)
    check("a later press extends it", got == 120.0 + PERFORMANCE_SECONDS, f"{got}")


def test_mixed_drain_picks_the_ability_press_out():
    drained = [event(5.0), event(5.7, ABILITY_KEYCODE), event(6.4)]
    got = performance_deadline(drained, 0.0)
    check("the ability press is found among beats",
          got == 5.7 + PERFORMANCE_SECONDS, f"{got}")


def test_bot_sourced_ability_press_still_holds():
    """Deliberately not filtered on `source`. If the bot ever presses this key itself, that
    press starts a Performance too and standing down is still the right answer."""

    got = performance_deadline([event(3.0, ABILITY_KEYCODE, source=0)], 0.0)
    check("a source-0 ability press holds too", got == 3.0 + PERFORMANCE_SECONDS, f"{got}")


def test_the_input_is_not_mutated():
    drained = [event(5.0, ABILITY_KEYCODE)]
    before = [dict(e) for e in drained]
    performance_deadline(drained, 0.0)
    check("the drained events are left untouched", drained == before)


def main():
    print("performance_guard")
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
