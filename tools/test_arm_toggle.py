"""The BACKSPACE arm/disarm toggle, which hands the keyboard back mid-match.

WHY IT EXISTS. Some checks are the operator's to play: `1-2-3-4!` already has its own
stand-down, but Merciless Storm draws no solid band, so the zone read degenerates (the
2026-09-13 22:08 storm read "58 deg great in a 59 deg zone" on all four fires) and the bot
aims at a centre it never measured. Until that is fixed the honest move is to take the
keyboard back, and it has to be possible without killing the run — a ctrl-c costs the
recorder's bout, the link level and the whole session's landings.

WHY IT IS A DOUBLE-TAP AND NOT A PRESS. It was one press, one flip, until 2026-09-18.
That run logged 13 backspace key-downs between 23:33:24 and 23:33:27; 13 is odd, so it
left the bot DISARMED, and it stayed that way for 4m18s. Three repair-heal checks went
past untouched (23:37:10, 23:37:20, 23:37:43 in `frames/bout_20260918-233614/bout.json`,
all `"path": "disarmed"`), and the run's own summary still ended "the run ended armed" —
which is exactly what the operator saw and why they believed the match was fine.

The premise that broke is written in `key_watcher.py`: backspace was chosen because "DBD
binds nothing to it, so a press inside the game is inert". Inert for gameplay actions, but
NOT when an in-game text field has focus, where backspace is the delete key. A burst of
backspaces is what deleting a line of text looks like.

So a burst is text and only an ISOLATED PAIR is a toggle. Parity is gone: no run of rapid
presses, odd or even, can move the state. That costs a `TOGGLE_DOUBLE_TAP_SECONDS` delay
before a real toggle takes effect, which is nothing against a stand-down that lasts a
check, and it buys back the failure that has no symptom.

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
from tools.autorun import TOGGLE_DOUBLE_TAP_SECONDS as WINDOW
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


def test_one_press_does_not_disarm():
    """THE 2026-09-18 INCIDENT. A lone backspace is a character being deleted, not a
    toggle. Before this, one press disarmed the bot and nothing on screen said so."""

    armed, pending = toggle_armed([event(1.0, TOGGLE_KEYCODE)], True, None, now=1.0)
    check("a lone backspace leaves an armed bot armed", armed is True)
    check("the lone press is held as a burst in progress", pending is not None)


def test_a_lone_press_that_goes_quiet_still_does_not_flip():
    """The burst closes when the window passes. A burst of one is never a toggle."""

    armed, pending = toggle_armed([event(1.0, TOGGLE_KEYCODE)], True, None, now=1.0)
    armed, pending = toggle_armed([], armed, pending, now=1.0 + 5 * WINDOW)
    check("a closed burst of one leaves the bot armed", armed is True)
    check("the burst is retired once it closes", pending is None)


def test_a_deliberate_double_tap_disarms():
    pair = [event(1.0, TOGGLE_KEYCODE), event(1.08, TOGGLE_KEYCODE)]
    armed, pending = toggle_armed(pair, True, None, now=1.08)
    check("the flip waits for the burst to close", armed is True)
    armed, pending = toggle_armed([], armed, pending, now=1.08 + 5 * WINDOW)
    check("a clean double-tap disarms", armed is False)
    check("nothing is left pending", pending is None)


def test_a_deliberate_double_tap_re_arms():
    pair = [event(1.0, TOGGLE_KEYCODE), event(1.08, TOGGLE_KEYCODE)]
    armed, pending = toggle_armed(pair, False, None, now=1.08)
    armed, pending = toggle_armed([], armed, pending, now=1.08 + 5 * WINDOW)
    check("a clean double-tap re-arms", armed is True)


def test_the_double_tap_survives_being_split_across_drains():
    """A drain happens every frame (~26 ms), so the two halves of a 80 ms double-tap
    usually arrive in DIFFERENT drains. The pending burst is what carries it across."""

    armed, pending = toggle_armed([event(1.0, TOGGLE_KEYCODE)], True, None, now=1.0)
    armed, pending = toggle_armed([event(1.08, TOGGLE_KEYCODE)], armed, pending, now=1.08)
    armed, pending = toggle_armed([], armed, pending, now=1.08 + 5 * WINDOW)
    check("a split double-tap still disarms", armed is False)


def test_two_presses_too_far_apart_are_two_separate_bursts():
    """Slow deliberate typing is not a double-tap. Each press closes as a burst of one."""

    slow = [event(1.0, TOGGLE_KEYCODE), event(1.0 + 3 * WINDOW, TOGGLE_KEYCODE)]
    armed, pending = toggle_armed(slow, True, None, now=1.0 + 3 * WINDOW)
    armed, pending = toggle_armed([], armed, pending, now=1.0 + 9 * WINDOW)
    check("two slow presses leave the bot armed", armed is True)


def test_the_13_press_burst_that_caused_the_incident_is_inert():
    """THE REGRESSION TEST. 23:33:24-27 on 2026-09-18: 13 backspace key-downs in ~4 s
    disarmed the bot for 4m18s and cost three repair-heal checks at 23:37:10, :20 and :43.
    Under the old one-press-one-flip rule that odd count left it disarmed. A burst this
    long is someone deleting text, and it must not move the state at all."""

    burst = [event(1.0 + i * 0.14, TOGGLE_KEYCODE) for i in range(13)]
    armed, pending = toggle_armed(burst, True, None, now=1.0 + 12 * 0.14)
    armed, pending = toggle_armed([], armed, pending, now=1.0 + 12 * 0.14 + 5 * WINDOW)
    check("a 13-press burst leaves the bot ARMED", armed is True)
    check("a 13-press burst leaves nothing pending", pending is None)


def test_any_burst_of_three_or_more_is_inert():
    """Not just 13. ANY run of rapid presses is text. Only an isolated pair is a toggle,
    so no odd/even parity anywhere in a burst can flip the bot."""

    for n in (3, 4, 5, 6, 7, 11):
        burst = [event(1.0 + i * 0.1, TOGGLE_KEYCODE) for i in range(n)]
        armed, pending = toggle_armed(burst, True, None, now=1.0 + (n - 1) * 0.1)
        armed, _ = toggle_armed([], armed, pending, now=1.0 + n * 0.1 + 5 * WINDOW)
        check(f"a burst of {n} leaves the bot armed", armed is True)


def test_autorepeat_is_not_a_press():
    """A HELD backspace emits a stream of key-downs flagged `repeat`. Whether the
    2026-09-18 burst was held or tapped was never settled — no keys.jsonl was written
    that run — so both doors are shut."""

    held = [event(1.0, TOGGLE_KEYCODE)]
    held += [dict(event(1.0 + i * 0.03, TOGGLE_KEYCODE), repeat=1) for i in range(1, 20)]
    armed, pending = toggle_armed(held, True, None, now=1.0 + 20 * 0.03)
    armed, _ = toggle_armed([], armed, pending, now=1.0 + 20 * 0.03 + 5 * WINDOW)
    check("a held backspace is one press, so it never toggles", armed is True)


def test_a_repeat_does_not_complete_a_double_tap():
    """The repeat must not pair with the real press that started it."""

    held = [event(1.0, TOGGLE_KEYCODE), dict(event(1.05, TOGGLE_KEYCODE), repeat=1)]
    armed, pending = toggle_armed(held, True, None, now=1.05)
    armed, _ = toggle_armed([], armed, pending, now=1.05 + 5 * WINDOW)
    check("press-then-repeat is not a double-tap", armed is True)


def test_other_keys_never_flip_it():
    noise = [event(1.0), event(1.1, ABILITY_KEYCODE), event(1.2)]
    armed, _ = toggle_armed(noise, True, None, now=1.2)
    check("SPACE and Active Ability do not disarm the bot", armed is True)


def test_a_double_tap_buried_in_traffic_is_still_seen():
    mixed = [event(1.0), event(1.02, TOGGLE_KEYCODE), event(1.05, ABILITY_KEYCODE),
             event(1.09, TOGGLE_KEYCODE), event(1.15)]
    armed, pending = toggle_armed(mixed, True, None, now=1.15)
    armed, _ = toggle_armed([], armed, pending, now=1.15 + 5 * WINDOW)
    check("other keys between the two taps do not break the pair", armed is False)


def test_an_empty_drain_changes_nothing():
    armed, pending = toggle_armed([], True, None, now=1.0)
    check("an empty drain leaves an armed bot armed", armed is True)
    check("an empty drain leaves nothing pending", pending is None)
    armed, _ = toggle_armed([], False, None, now=1.0)
    check("an empty drain leaves a disarmed bot disarmed", armed is False)


def test_a_bot_sourced_double_tap_flips_too():
    """Not filtered on `source`: a bot that disarms itself must be visible, not hidden."""

    pair = [event(1.0, TOGGLE_KEYCODE, source=0), event(1.08, TOGGLE_KEYCODE, source=0)]
    armed, pending = toggle_armed(pair, True, None, now=1.08)
    armed, _ = toggle_armed([], armed, pending, now=1.08 + 5 * WINDOW)
    check("source 0 flips the same way", armed is False)


def test_it_returns_a_real_bool():
    """The caller compares the result against what it passed in to decide whether to log
    the change. A truthy non-bool would compare unequal every drain and spam the log."""

    got, _ = toggle_armed([event(1.0, TOGGLE_KEYCODE)], True, None, now=1.0)
    check("the return is a bool", isinstance(got, bool), f"{type(got).__name__}")


def test_the_input_is_not_mutated():
    drained = [event(5.0, TOGGLE_KEYCODE)]
    before = [dict(e) for e in drained]
    toggle_armed(drained, True, None, now=5.0)
    check("the drained events are left untouched", drained == before)


def test_now_is_optional_and_only_closes_bursts():
    """`now` omitted means 'I cannot tell you the time' — the burst stays open rather
    than closing early, so a caller that forgets it under-toggles instead of mis-toggling."""

    pair = [event(1.0, TOGGLE_KEYCODE), event(1.08, TOGGLE_KEYCODE)]
    armed, pending = toggle_armed(pair, True, None)
    check("with no clock the flip is deferred, not lost", armed is True and pending is not None)


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
