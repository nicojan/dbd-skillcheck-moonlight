"""Pin the contract of `autorun.seen_in` — which window a check's record says it was read in.

    .venv/bin/python tools/test_seen_in.py

The fields exist to answer one question from the check queue alone: did the bot press on
an off-centre (Doctor Madness) check, or a centred one? Before them the only way through
was replaying the recorded frames through `wide_capture.look`, which answers only for the
checks that happened to be recorded — on the 09-15 Doctor match, 5 of the run's 7 locks.

The case that matters most here is `--no-wide`. That path never sweeps, so it cannot tell
a centred check from a displaced one it simply never saw. Reporting `off_centre: false`
there would assert "this was centred" on the strength of never having looked, and a later
reader tallying those records would conclude the Madness gap does not exist — which is the
exact reasoning, on the exact question, that `bout_session.OFF_CENTRE_UNKNOWN` was written
to prevent. None is the only honest answer, and it must stay distinguishable from False.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from autorun import seen_in

FAILED = []


def check(what, ok):
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        FAILED.append(what)


class _Held:
    """Stands in for an `OffCentreCrop`; `seen_in` only ever tests it for None."""


def test_a_centred_check_on_the_wide_path():
    print("\na centred check, wide capture on")
    got = seen_in(True, (49, 90), None)
    check("is reported centred", got["off_centre"] is False)
    check("and still carries where it was cropped", got["crop_origin"] == [49, 90])


def test_an_off_centre_check_carries_its_origin():
    print("\nan off-centre check, wide capture on")
    got = seen_in(True, (253, 91), _Held())
    check("is reported off-centre", got["off_centre"] is True)
    check("with the located origin, as a JSON-safe list",
          got["crop_origin"] == [253, 91] and isinstance(got["crop_origin"], list))


def test_no_crop_yet():
    print("\nbefore any crop has been taken")
    got = seen_in(True, None, None)
    check("the origin is null rather than invented", got["crop_origin"] is None)
    check("but the centred answer is still real", got["off_centre"] is False)


def test_the_no_wide_path_answers_unknown_not_centred():
    print("\nthe --no-wide path, which never looks")
    got = seen_in(False, (49, 90), None)
    check("does not claim the check was centred", got["off_centre"] is not False)
    check("it says UNKNOWN", got["off_centre"] is None)
    check("and volunteers no origin", got["crop_origin"] is None)

    held = seen_in(False, (253, 91), _Held())
    check("and says so even if a stale lock is still in hand",
          held["off_centre"] is None and held["crop_origin"] is None)


def test_the_fields_are_always_both_present():
    print("\nevery answer carries both fields")
    for wide in (True, False):
        for origin in (None, (1, 2)):
            for held in (None, _Held()):
                got = seen_in(wide, origin, held)
                check(f"wide={wide} origin={origin} held={held is not None}",
                      set(got) == {"crop_origin", "off_centre"})


if __name__ == "__main__":
    test_a_centred_check_on_the_wide_path()
    test_an_off_centre_check_carries_its_origin()
    test_no_crop_yet()
    test_the_no_wide_path_answers_unknown_not_centred()
    test_the_fields_are_always_both_present()
    print("\n" + ("all passing" if not FAILED
                  else f"{len(FAILED)} failing: " + ", ".join(FAILED)))
    raise SystemExit(1 if FAILED else 0)
