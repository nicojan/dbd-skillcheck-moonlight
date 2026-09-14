"""The BACKSPACE arm/disarm toggle, which hands the keyboard back mid-match.

WHY IT EXISTS. Some checks are the operator's to play: `1-2-3-4!` already has its own
stand-down, but Merciless Storm draws no solid band, so the zone read degenerates (the
2026-09-13 22:08 storm read "58 deg great in a 59 deg zone" on all four fires) and the bot
aims at a centre it never measured. Until that is fixed the honest move is to take the
keyboard back, and it has to be possible without killing the run — a ctrl-c costs the
recorder's bout, the link level and the whole session's landings.

WHAT IS COVERED HERE. `toggle_armed` is the whole decision and it is pure, so it is
testable without a game. The branch that consumes it is inside the armed loop and is not
reachable from a test; what that branch must do is written down in `autorun.py` beside it.

The failure this guards against is not a wrong flip — it is a SILENT one. A run that is
quietly doing nothing looks exactly like a quiet match.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbd.utils.key_watcher import (ABILITY_KEYCODE, SPACE_KEYCODE, TOGGLE_KEYCODE)
from tools.autorun import toggle_armed

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def event(t, keycode=SPACE_KEYCODE, source=1):
    return {"t": float(t), "keycode": keycode, "source": source}


def test_the_toggle_key_is_backspace_not_forward_delete():
    """117 is the delete key above the arrows, and it is NOT the one bound here."""

    check("TOGGLE_KEYCODE is kVK_Delete (51)", TOGGLE_KEYCODE == 51, f"{TOGGLE_KEYCODE}")


def test_the_toggle_key_collides_with_nothing_else_watched():
    watched = (SPACE_KEYCODE, ABILITY_KEYCODE)
    check("the toggle key is not already spoken for", TOGGLE_KEYCODE not in watched)


def test_an_empty_drain_changes_nothing():
    check("an empty drain leaves an armed bot armed", toggle_armed([], True) is True)
    check("an empty drain leaves a disarmed bot disarmed", toggle_armed([], False) is False)


def test_other_keys_never_flip_it():
    noise = [event(1.0), event(1.1, ABILITY_KEYCODE), event(1.2)]
    check("SPACE and Active Ability do not disarm the bot",
          toggle_armed(noise, True) is True)


def test_one_press_disarms():
    check("backspace disarms an armed bot",
          toggle_armed([event(1.0, TOGGLE_KEYCODE)], True) is False)


def test_one_press_re_arms():
    check("backspace re-arms a disarmed bot",
          toggle_armed([event(1.0, TOGGLE_KEYCODE)], False) is True)


def test_two_presses_in_one_drain_come_back_where_they_started():
    """A drain can hold a whole frame's worth of events, so a double-tap can arrive whole."""

    pair = [event(1.0, TOGGLE_KEYCODE), event(1.08, TOGGLE_KEYCODE)]
    check("a double-tap inside one drain is a no-op", toggle_armed(pair, True) is True)


def test_it_is_a_fold_not_a_last_event_wins():
    """Three presses is an odd number of flips whatever order they are drained in."""

    triple = [event(t, TOGGLE_KEYCODE) for t in (1.0, 1.1, 1.2)]
    check("three presses land disarmed", toggle_armed(triple, True) is False)


def test_a_press_buried_in_traffic_is_still_seen():
    mixed = [event(1.0), event(1.1, TOGGLE_KEYCODE), event(1.2, ABILITY_KEYCODE)]
    check("a toggle between other presses still flips", toggle_armed(mixed, True) is False)


def test_a_bot_sourced_press_flips_too():
    """Not filtered on `source`: a bot that disarms itself must be visible, not hidden."""

    check("source 0 flips the same way",
          toggle_armed([event(1.0, TOGGLE_KEYCODE, source=0)], True) is False)


def test_it_returns_a_real_bool():
    """The caller compares the result against what it passed in to decide whether to log
    the change. A truthy non-bool would compare unequal every drain and spam the log."""

    got = toggle_armed([event(1.0, TOGGLE_KEYCODE)], True)
    check("the return is a bool", isinstance(got, bool), f"{type(got).__name__}")


def test_the_input_is_not_mutated():
    drained = [event(5.0, TOGGLE_KEYCODE)]
    before = [dict(e) for e in drained]
    toggle_armed(drained, True)
    check("the drained events are left untouched", drained == before)


def main():
    print("arm_toggle")
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
