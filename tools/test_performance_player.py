"""Tests for `dbd/utils/performance.py`, the opt-in 1-2-3-4! player.

    .venv/bin/python tools/test_performance_player.py

Pure: synthetic needle samples and arcs, no frames, no keyboard. The replay against a real
recorded Performance lives in `tools/replay_performance.py`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbd.utils.needle_tracker import Zone
from dbd.utils import performance as perf
from dbd.utils.performance import (LAND_DEG, MAX_LAND_DEG, MIN_PRESS_GAP_MS, PerfState,
                                   observe, operator_pressed_space, plan, pressed)

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


RATE = 300.0        # deg/s, what the 2026-11-25 Performance swept at
FRAME_MS = 27.0
LEAD_MS = 70.0


def arc(start, width=120.0):
    return Zone(great_start=start, great_end=(start + width) % 360.0,
                zone_start=start, zone_end=(start + width) % 360.0)


def needle_at(t_ms, start_deg=0.0, rate=RATE):
    return (start_deg + rate * t_ms / 1000.0) % 360.0


def run(arcs_at, t_end_ms, lead_ms=LEAD_MS, rate=RATE, start_deg=0.0, noise=None,
        needle_until_ms=None):
    """Drive the player frame by frame, pressing whenever the plan falls due before the next
    frame — the same rule autorun uses. `arcs_at(t)` gives the arc drawn at time t or None.
    Returns [(press_ms, arc_entry_deg, landing_offset_deg)]."""

    state = PerfState()
    presses = []
    t = 0.0
    while t <= t_end_ms:
        angle = needle_at(t, start_deg, rate)
        if needle_until_ms is not None and t > needle_until_ms:
            angle = None        # the ring is gone: nothing red is read at all
        elif noise is not None:
            angle = (angle + noise(t)) % 360.0
        state = observe(state, t, angle, arcs_at(t))
        p = plan(state, t, lead_ms)
        if p.press_at_ms is not None and p.press_at_ms - t <= FRAME_MS * 1.25:
            at = max(t, p.press_at_ms)
            land = needle_at(at + lead_ms, start_deg, rate)
            off = (land - p.entry_deg) % 360.0
            presses.append((at, p.entry_deg, off if off < 180 else off - 360))
            state = pressed(state, at)
        t += FRAME_MS
    return presses


def test_one_press_per_arc_landing_at_the_target():
    # An arc 150 deg ahead of the needle: 500 ms of warning, like most beats on 11-25.
    a = arc(150.0)
    presses = run(lambda t: a if t < 700 else None, 900)
    check("exactly one press on one arc", len(presses) == 1, presses)
    if presses:
        off = presses[0][2]
        check("it lands at the operator's band, LAND_DEG past the leading edge",
              abs(off - LAND_DEG) <= RATE * FRAME_MS / 1000.0 + 1e-6, off)


def test_no_arc_no_press():
    check("a sweeping needle with no arc drawn is never pressed",
          run(lambda t: None, 2000) == [])


def test_a_late_arc_inside_the_band_is_still_pressed_at_once():
    # 11-25 had arcs drawn only 7-13 deg ahead of the needle. 70 ms of lead at 300 deg/s
    # is 21 deg, so pressing now lands ~14 deg in: inside the band, take it.
    # The needle is tracked across the whole Performance, so it already has a fit when the
    # late arc appears: drawn at t=216 ms, when the needle is ~3 deg short of its entry.
    a = arc(10.0)
    drawn = 8 * FRAME_MS
    presses = run(lambda t: a if drawn <= t < 800 else None, 800,
                  start_deg=3.0 - RATE * drawn / 1000.0)
    check("pressed", len(presses) == 1, presses)
    if presses:
        check("...promptly, and inside the band",
              presses[0][0] <= drawn + 2 * FRAME_MS and 0 <= presses[0][2] <= MAX_LAND_DEG,
              presses[0])


def test_an_arc_already_past_the_band_is_left_alone():
    # Needle already 60 deg into the arc: any press lands at 80+, outside what the operator
    # has ever been seen to pass with. Skip it rather than press late.
    a = arc(0.0)
    presses = run(lambda t: a if t < 150 else None, 150, start_deg=60.0)
    check("too far in: no press", presses == [], presses)


def test_consecutive_beats_each_get_their_own_press():
    # Three arcs relocating every ~680 ms, each ~150 deg ahead of the needle when drawn.
    beats = [(0, 150.0), (680, 354.0), (1360, 198.0)]

    def arcs_at(t):
        live = [a for s, a in beats if s <= t]
        return arc(live[-1]) if live else None

    presses = run(arcs_at, 2000)
    entries = [round(p[1]) for p in presses]
    check("one press per beat, in order", entries == [150, 354, 198], presses)
    gaps = [b[0] - a[0] for a, b in zip(presses, presses[1:])]
    check("presses never closer than MIN_PRESS_GAP_MS",
          all(g >= MIN_PRESS_GAP_MS for g in gaps), gaps)


def test_a_poor_fit_does_not_press():
    a = arc(150.0)
    wild = lambda t: 40.0 if int(t / FRAME_MS) % 2 else -40.0
    presses = run(lambda t: a, 600, noise=wild)
    check("a needle read that will not fit is not pressed on", presses == [], presses)


def test_the_state_is_never_mutated():
    s0 = PerfState()
    s1 = observe(s0, 0.0, 10.0, arc(150.0))
    check("observe returns a new state and leaves the old one alone",
          s1 is not s0 and s0.samples == () and s0.arc is None, s0)
    s2 = pressed(s1, 5.0)
    check("pressed returns a new state", s2 is not s1 and s1.last_press_ms is None, s1)


def test_the_operator_taking_over_is_seen_and_the_bots_own_press_is_not():
    """The player hands a Performance back the moment the operator presses SPACE: the
    window is a fixed 16 s and can outlive a cancelled Performance, and two hands on one
    key is how a Performance gets an extra press. Injected presses read source 0."""
    space, other = 49, 3
    check("an operator SPACE press is a take-over",
          operator_pressed_space([{"keycode": space, "source": 1, "repeat": 0}], space))
    check("the bot's own injected press is not",
          not operator_pressed_space([{"keycode": space, "source": 0, "repeat": 0}], space))
    check("another key is not",
          not operator_pressed_space([{"keycode": other, "source": 1, "repeat": 0}], space))
    check("nothing pressed is not", not operator_pressed_space([], space))


BOUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "frames", "bout_20261125-152526")


def test_autoruns_own_step_plays_the_recorded_performance():
    """`autorun.play_performance_frame` itself, over the first whole Performance ever
    recorded (15:25:25, played by the operator, ~21 beats). Catches the wiring the pure
    tests cannot: seconds vs milliseconds, the ring located once, one press per beat.
    The clock is the frame clock and `fire` is recorded instead of pressing anything."""
    if not os.path.isdir(BOUT):
        # A FAILURE, not a skip: a suite that skips its only end-to-end check and still
        # prints "all ok" is the trap. If the bout is pruned, point this at another one.
        check("the recorded Performance is on disk", False, BOUT)
        return
    import json
    from types import SimpleNamespace
    import cv2
    import tools.autorun as autorun
    from dbd.utils.wide_capture import centre_slice, geometry_from_describe, to_model_size

    meta = json.load(open(os.path.join(BOUT, "bout.json")))
    geometry = geometry_from_describe(meta["geometry"])
    recs = [json.loads(l) for l in open(os.path.join(BOUT, "manifest.jsonl"))]
    recs = [r for r in recs if -1000 <= r["t_ms"] <= 15700]

    clock = {"t": 0.0}
    fired = []
    real = (autorun.monotonic, autorun.fire, autorun.log)
    autorun.monotonic = lambda: clock["t"]
    autorun.fire = lambda args, wait_ms: fired.append(clock["t"] + wait_ms / 1000.0) \
        or clock["t"] + wait_ms / 1000.0
    autorun.log = lambda *a, **k: None
    try:
        session = autorun.perf.Session()
        args = SimpleNamespace(dry_run=True)
        for r in recs:
            clock["t"] = r["t_ms"] / 1000.0
            frame = to_model_size(centre_slice(cv2.imread(os.path.join(BOUT, r["frame"])),
                                               geometry))
            # Every recorded Performance frame classified 7 or 0; 7 is the common one.
            session, _ = autorun.play_performance_frame(
                args, session, frame, clock["t"], 7, 70.0, 27.0, autorun.CENTRE_PRIOR)
    finally:
        autorun.monotonic, autorun.fire, autorun.log = real

    check("the ring was located", session.ring is not None, session.ring)
    check("one press per beat: 21 on the recorded Performance", len(fired) == 21, len(fired))
    gaps = [b - a for a, b in zip(fired, fired[1:])]
    check("no two presses closer than MIN_PRESS_GAP_MS",
          all(g * 1000.0 >= MIN_PRESS_GAP_MS for g in gaps), min(gaps) if gaps else None)


def test_a_vanished_ring_is_never_extrapolated_into_a_press():
    """Review H1: an arc left unpressed (skipped, or the Performance cut short) and then the
    ring disappears. The old state must not be projected a revolution forward into a press
    on an empty screen. Arc drawn too deep to take, then everything vanishes at 150 ms."""
    a = arc(0.0)
    presses = run(lambda t: a if t < 150 else None, 2500, start_deg=60.0,
                  needle_until_ms=150)
    check("no press in the 2.35 s after the ring vanished", presses == [], presses)


def test_a_lost_needle_is_never_extrapolated_while_the_arc_stays_drawn():
    """H1's other half: the arc is still read but the needle is not (too faint, occluded).
    The fit from before must not be projected forward into a press."""
    a = arc(200.0)
    presses = run(lambda t: a, 2500, needle_until_ms=150)
    check("no press once the needle stops being read", presses == [], presses)


def test_an_arc_that_stops_being_drawn_is_not_pressed():
    """The needle keeps sweeping but the arc is gone well before the needle gets there (a
    cancelled Performance, or a misread that lasted two frames). Nothing is pressed on an
    arc not read in the last ARC_STALE_MS."""
    a = arc(150.0)
    presses = run(lambda t: a if t < 60 else None, 1500)
    check("no press on an arc no longer drawn", presses == [], presses)


def test_a_new_arc_after_a_reversal_is_not_mistaken_for_the_one_just_pressed():
    """11-25 15:43:27: the needle swept ANTICLOCKWISE for one beat (arc 192-303, entered at
    303, so its far end was 192), then clockwise again onto a new arc 89-200, whose far end
    200 sat within tolerance of 192. Identified by its far end alone, the new arc read as
    already pressed and the beat was skipped — which fails the whole Performance. An arc is
    identified by BOTH drawn edges, whichever way the needle goes."""
    state = PerfState()
    t = 0.0
    angle = 330.0
    presses = []
    first, second = arc(192.0, 111.0), arc(89.0, 111.0)

    def step(state, t, angle, zone):
        state = observe(state, t, angle % 360.0, zone)
        p = plan(state, t, LEAD_MS)
        if p.press_at_ms is not None and p.press_at_ms - t <= FRAME_MS * 1.25:
            presses.append((round(t), p.entry_deg))
            state = pressed(state, max(t, p.press_at_ms))
        return state

    # anticlockwise from 330 down through the first arc's 303 end
    for _ in range(16):
        state = step(state, t, angle, first)
        t += FRAME_MS
        angle -= RATE * FRAME_MS / 1000.0
    # the arc relocates and the needle turns clockwise, the new arc well ahead of it
    angle = 250.0
    for _ in range(40):
        state = step(state, t, angle, second)
        t += FRAME_MS
        angle += RATE * FRAME_MS / 1000.0
    entries = [round(e) for _, e in presses]
    check("both beats pressed, the anticlockwise one at 303 and the next at 89",
          entries == [303, 89], presses)


def test_an_already_pressed_arc_is_not_pressed_again_a_revolution_later():
    """Review M3: the arc stays drawn for more than a revolution after its press. Only the
    retired-arc check can stop the second press — the gap and the band have long passed."""
    a = arc(150.0)
    presses = run(lambda t: a, 3000)
    check("one press, however long the pressed arc stays drawn", len(presses) == 1, presses)


def _frame():
    import numpy as np
    return np.zeros((224, 224, 3), dtype=np.uint8)


def _drive(preds, reads, ring=((112.0, 102.0), 65.0), recent=(), last_arc_s=None):
    """`session_step` with the frame readers stubbed: `reads(t)` -> (needle, arc)."""
    real = perf.read_frame
    perf.read_frame = lambda img, c, r: reads(_drive.t)
    try:
        session = perf.Session(ring=ring, recent=recent, last_arc_s=last_arc_s)
        presses, notes = [], []
        t = 0.0
        for pred in preds:
            _drive.t = t
            session, p, note = perf.session_step(session, _frame(), t / 1000.0, pred, t,
                                                 LEAD_MS, FRAME_MS)
            if note:
                notes.append(note)
            if p is not None:
                presses.append(t)
                session = perf.session_pressed(session, max(t, p.press_at_ms))
            t += FRAME_MS
        return presses, notes, session
    finally:
        perf.read_frame = real


def _beat(t):
    return needle_at(t), arc(150.0)


def test_nothing_is_pressed_until_the_screen_confirms_a_performance():
    """Review H2: an Active Ability press that started nothing still opens the window. With
    no frame classified as a Performance the player must never press."""
    presses, _, _ = _drive([0] * 40, _beat)
    check("no Performance-class frames: no press", presses == [], presses)
    presses, _, _ = _drive([perf.PERFORMANCE_PREDS[0]] * 40, _beat)
    check("...and with them, it plays", len(presses) == 1, presses)


def test_confirmation_is_recent_and_a_repair_frame_clears_it():
    """Review round 2: a cumulative count let any 5 class-7 frames anywhere in the window
    confirm it for good. Five 7s long ago, then class-0 frames: not confirmed. And a single
    repair frame inside the window voids it."""
    stale = [7] * 5 + [0] * 20
    presses, _, _ = _drive(stale, _beat)
    check("five 7s long ago do not confirm", presses == [], presses)
    presses, _, _ = _drive([7] * 10 + [3] + [7, 0, 0, 0] * 10, _beat)
    check("a repair frame clears it until 5 of the last 8 are 7 again", presses == [],
          presses)


def test_presses_only_on_the_allowlisted_classes():
    for cls in (4, 5, 6):
        presses, _, _ = _drive([cls] * 40, _beat, recent=(7,) * perf.CONFIRM_WINDOW)
        check(f"class {cls} frames are never pressed on, however confirmed", presses == [],
              presses)


def test_never_a_press_on_a_repair_or_wiggle_frame():
    """The negative control: outside the windows the player fired 5 times on Storm and repair
    checks. Even confirmed, a frame the classifier calls repair-heal is never pressed on."""
    repair = perf.NEVER_PRESS_PREDS[0]
    presses, _, _ = _drive([repair] * 40, _beat, recent=(7,) * perf.CONFIRM_WINDOW)
    check("confirmed, but every frame repair-heal: no press", presses == [], presses)


def test_no_arc_for_the_lapse_ends_playing_for_the_window():
    silent = lambda t: (needle_at(t), None)
    n = int((perf.LAPSE_S * 1000 + 200) / FRAME_MS)
    _, notes, session = _drive([perf.PERFORMANCE_PREDS[0]] * n, silent,
                               recent=(7,) * perf.CONFIRM_WINDOW, last_arc_s=0.0)
    check("a lapse ends the session", session.done == "lapsed", session.done)
    _, _, fresh = _drive([perf.PERFORMANCE_PREDS[0]] * n, silent,
                         recent=(7,) * perf.CONFIRM_WINDOW)
    check("...but the clock never starts before the first arc", fresh.done is None,
          fresh.done)


def test_no_ring_means_no_reading_at_all():
    """`refine_centre` falls back to the prior when there is no ring; the session must keep
    looking rather than read arcs about an assumed centre."""
    import numpy as np
    blank = np.zeros((224, 224, 3), dtype=np.uint8)
    session = perf.Session()
    for i in range(perf.LOCATE_FRAMES + 4):
        session, p, _ = perf.session_step(session, blank, i * 0.027,
                                          perf.PERFORMANCE_PREDS[0], i * 27.0, LEAD_MS,
                                          FRAME_MS)
        check_p = p
    check("a blank screen locates no ring", session.ring is None, session.ring)
    check("...and plans nothing", check_p is None, check_p)


def test_without_ordinary_checks_the_ring_is_the_fixed_prior_not_located():
    """2026-11-26 16:03: no ordinary check had fired, the session located the ring from the
    Performance's own frames at (113, 101) r=64, and `read_frame` then missed beat 3's arc
    on every frame — the needle ran through it unpressed. The prior (112, 102) r=65 presses
    all three in replay, and matches the measured ring on all four 11-25 Performances."""
    ring, source = perf.ring_for_session(())
    check("no ordinary checks -> the fixed prior ring", ring == perf.PRIOR_RING, ring)
    check("...and the log says so", "prior" in source, source)
    two = ((111.0, 101.0, 66.0), (111.0, 101.0, 66.0))
    ring, _ = perf.ring_for_session(two)
    check("fewer than KNOWN_RING_MIN checks -> still the prior", ring == perf.PRIOR_RING, ring)


def test_with_ordinary_checks_the_ring_is_theirs():
    centres = ((111.5, 101.5, 65.0), (111.5, 102.0, 65.0), (111.5, 101.75, 65.0))
    ring, source = perf.ring_for_session(centres)
    check("enough ordinary checks -> their median",
          ring == ((111.5, 101.75), 65.0), ring)
    check("...and the log says so", "ordinary checks" in source, source)


def test_a_given_ring_is_used_from_the_first_frame_and_named_in_the_note():
    import numpy as np
    blank = np.zeros((224, 224, 3), dtype=np.uint8)
    session, _, note = perf.session_step(perf.Session(), blank, 0.0, perf.PERFORMANCE_PREDS[0],
                                         0.0, LEAD_MS, FRAME_MS, ring=perf.PRIOR_RING,
                                         ring_source="the fixed prior")
    check("ring set on the first frame, no locating", session.ring == perf.PRIOR_RING
          and session.locate == (), (session.ring, len(session.locate)))
    check("note names where the ring came from", note is not None
          and "the fixed prior" in note, note)


def main():
    print("performance player")
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
