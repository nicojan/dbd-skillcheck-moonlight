"""Run the detector only while the stream is focused; pause automatically when it is not.

    python tools/autorun.py --dry-run     # detect and log, never press a key
    python tools/autorun.py               # armed
    python tools/autorun.py --no-predict  # upstream's reactive behaviour only

Sweeping checks are fired PREDICTIVELY by default. Reacting to a Great classification
cannot land one here: the Great band is 10.5 deg, so it lasts 26-37 ms, against a 72 ms
keypress-to-pixel round trip through Moonlight. The needle has left before the key
arrives, which is why repair and heal checks used to land in Good every time and why
`--hit-ante` could not fix it. `dbd/utils/needle_tracker.py` fits the sweep and schedules
the press to LAND in Great; see its docstring and NOTES-local.md.

Wiggle keeps the reactive path — it oscillates rather than sweeping, so a linear fit is
wrong by construction, and it is the one check type that already worked.

Why the gate matters beyond convenience: synthetic keystrokes go to the *focused*
application. An ungated loop that is still running after you switch away will type
SPACE into whatever you switched to. Pausing on focus loss is the correctness fix, and
resuming on focus gain is what makes it usable.

State transitions are logged with timestamps so you can confirm the pause/resume edges
are firing where you expect.
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass
from re import sub
from time import monotonic, sleep, strftime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.AI_model import AI_model
from dbd.utils.directkeys import PressKey, ReleaseKey, SPACE
from dbd.utils.focus_watcher import FocusWatcher
from dbd.utils.monitoring_window import Monitoring_window, WindowNotFoundError
from dbd.utils.needle_tracker import (
    ROUND_TRIP_MS, Reading, TrackerState, decide, mark_fired, needle_angle, observe,
    read_watch, score_freeze, strength_reference, time_to_angle,
)

IDLE_POLL_SECONDS = 0.20
HIT_COOLDOWN_SECONDS = 0.5
ANTE_FRONTIER_PRED = 2

# Every non-wiggle class that means a check is on screen. `full black (out)` is included
# because Merciless Storm classifies as that, and because the model labels the frames
# either side of a real check with it too — the tracker's own gates (a drawn Great band,
# a plausible rate, a clean fit) are what reject those, not the class.
TRACKED_PREDS = (1, 2, 3, 4, 5, 6, 7)
WIGGLE_PREDS = (8, 9, 10)

DEFAULT_FRAME_MS = 30.0   # seed for the frame-period estimate, before any is measured
FRAME_MS_DECAY = 0.8      # EMA weight on the previous estimate
TRACK_DROP_FRAMES = 3     # consecutive check-free frames that end a track
# How long to watch for the needle to freeze after a press. This is a measurement window,
# so it has to be wider than the delay it is measuring: the freeze cannot appear in our
# capture until a full round trip after key-down, and a window shorter than that reports
# `needle gone before it could be read` for a press that landed perfectly well. 0.30 was
# sized when the loop was believed to be 72 ms. The armed run of 2026-08-15 landed 112 deg
# late (~343 ms of unexplained delay on top of the 130 it led by), which would fall outside
# a 300 ms window entirely — and `needle gone` is exactly what the earlier armed attempts
# printed. Keep this well clear of the worst round trip until that delay is understood.
FREEZE_WATCH_SECONDS = 0.80

# Trailing grabs below the needle floor that mean the check has left the screen. Once it
# has, no further grab can add anything, and the watch used to spend the rest of its 800 ms
# staring at an empty crop with the detector stopped — blind to a second check the whole
# time. Four is two grabs past the three a freeze needs, so a single dark frame mid-check
# cannot end the watch early.
CLEARED_DARK_READS = 4

# How much of a landing error is taken into the lead, WHEN --adapt-lead is passed. It is
# off by default now: simulated over the 212 scored fires on record, no gain and no median
# window beat holding the lead at 60 ms. Every one of them widened the landing spread
# (5.75 deg fixed, against 5.77 at gain 0.1, 5.88 at 0.3, 5.86 through a 7-wide median),
# and the Great counts they produced — 165 to 176 of 212 — all sit inside the +/-5.7 that
# counting noise alone gives at an 81% rate.
#
# The reason is in the signal, not the filter. Over 236 armed fires the fit-derived round
# trip has sd 12.6 ms while the tail read — a direct clock on the same link, key-down to
# seeing the freeze — has sd 5.3, and the two correlate at only 0.44. Most of the swing
# the loop was following was its own measurement error. Within a burst of checks seconds
# apart, where the link cannot have moved, the landing spread is still 3.74 deg (11 ms):
# that is aim noise, and no lead policy reaches it. Between sessions there is nothing left
# to chase either — mean error at a fixed 60 ms runs -0.74 to +2.52 deg across all seven
# logged sessions, every one of them inside a Great half-width.
#
# 2026-08-17 19:23 is what it cost: two 34 ms readings in a row against a tail that never
# left 77, the lead walked 61 -> 47, and the check missed 8 deg early.
#
# The flag and the clamps stay. A link that genuinely drifts would show up in the measured
# round trips first, and this is the lever to reach for when it does.
LEAD_GAIN = 0.3
LEAD_MIN_MS = 25.0
LEAD_MAX_MS = 160.0

# How long SPACE is held down. This is NOT cosmetic: the press was 5 ms, which a desktop
# text field registers fine (key events are queued, so duration is irrelevant) but a game
# polling input once per rendered frame can miss entirely — 16.7 ms between polls at 60 fps,
# and Moonlight batches the press and release over the network so the host re-injects them
# back to back. That is why measure_latency.py could fill a text field while the armed run
# never landed a single check. 50 ms matches test_keypress.py, the tool that verifies
# delivery, and is within the range of a normal human tap. Only key-DOWN decides when the
# hit registers, so a longer hold costs nothing in timing accuracy.
PRESS_HOLD_SECONDS = 0.05

# The last stretch of the lead is spun rather than slept. sleep() overshot by 2-5 ms on
# every armed fire — requested 9 slept 12, requested 31 slept 36 — a systematic late bias
# of about a degree at 325 deg/s, quietly absorbed into the round-trip constant instead of
# removed. Measured here, sleep returns about 50% late and deterministically so: 2 ms takes
# 3.0, 5 takes 7.5, 14 takes 20.7, 34 takes 44.0, with the maximum over 30 trials within
# 0.4 ms of the median. A single sleep of the whole gap therefore cannot be corrected by a
# fixed margin — see _wait_until. The spin costs one core for a few ms, once per check.
SPIN_MS = 3.0

# A Space transition takes time to settle, and Moonlight registers ~16 windows while
# it does. Resolving geometry mid-transition picked a menu-bar-inset window once
# (218px crop instead of 224), so wait for the dust to settle before re-resolving.
RESUME_SETTLE_SECONDS = 0.75


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Focus-gated skill check detector")
    p.add_argument("--window", default="Moonlight", help="substring of the window owner/title")
    p.add_argument("--model", default="models/model.onnx")
    p.add_argument("--aspect", type=str, default="16:9", help="stream aspect ratio, or 'fill'")
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--hit-ante", type=int, default=0, help="ms delay on ante-frontier hits")
    p.add_argument("--no-predict", dest="predict", action="store_false",
                   help="disable predictive firing; react to the classifier as upstream does")
    p.add_argument("--round-trip-ms", type=float, default=ROUND_TRIP_MS,
                   help="measured keypress->pixel latency the prediction leads by")
    p.add_argument("--adapt-lead", dest="adapt_lead", action="store_true",
                   help="follow the measured round trip instead of holding the lead fixed "
                        "(off by default — it cost more Greats than it won; see adapt_lead)")
    p.add_argument("--landing-log", default=None,
                   help="JSONL of every freeze watch (default: landings-<timestamp>.jsonl)")
    p.add_argument("--no-landing-log", dest="landing_log_enabled", action="store_false",
                   help="do not record the freeze watch readings")
    p.add_argument("--dry-run", action="store_true", help="log detections without pressing keys")
    p.add_argument("--no-require-on-screen", action="store_true",
                   help="gate on focus alone, skipping the window-list confirmation")
    p.add_argument("--pin-geometry", action="store_true",
                   help="lock the startup capture region instead of re-resolving on resume")
    return p.parse_args(argv)


def parse_aspect(text):
    if text.lower() in ("fill", "none"):
        return None
    if ":" in text:
        w, h = text.split(":")
        return float(w) / float(h)
    return float(text)


def log(message):
    print(f"[{strftime('%H:%M:%S')}] {message}", flush=True)


def _wait_until(deadline):
    """Wait out a deadline accurately: halve the gap with sleep, then spin the last bit.

    sleep() returns about 50% late here, so sleeping the whole remaining gap overshoots by
    half of it and no fixed margin fixes that — the error scales with the wait. Sleeping
    HALF the gap lands at ~75% of it even with a 50% overrun, so the loop converges on the
    deadline from below in two or three passes and the spin covers what is left. A press
    that goes out late by a few milliseconds is a press aimed a degree or two late.
    """

    while True:
        remaining = deadline - monotonic()
        if remaining <= SPIN_MS * 0.001:
            break
        sleep(remaining * 0.5)
    while monotonic() < deadline:
        pass


def fire(args, wait_ms):
    """Sleep out the remaining lead, then tap SPACE. The sleep is the whole point.

    Takes MILLISECONDS. The last SPIN_MS of the wait is spun rather than slept, because
    sleep() consistently returned late by 2-5 ms and that lands straight in the press.

    It used to take seconds while the only caller that passed a
    non-zero value handed it `decision.press_at_ms - now_ms`, so every predictive press
    slept 1000x too long: a 20 ms lead became a 20 second one. The reactive path passes 0
    and was unaffected, which is why reactive pressed the game and predictive appeared
    never to press at all. The unit now lives in the parameter name, and every value on
    both sides of this boundary is suffixed `_ms`.

    Returns the monotonic timestamp of key-DOWN. That instant, not the return of this
    function, is when the press is committed and when the round trip starts, so it is what
    `report_landing` measures the freeze against. The release is 50 ms later and irrelevant
    to timing — see PRESS_HOLD_SECONDS.
    """

    _wait_until(monotonic() + wait_ms * 0.001)
    pressed_at = monotonic()
    if args.dry_run:
        return pressed_at
    PressKey(SPACE)
    sleep(PRESS_HOLD_SECONDS)
    ReleaseKey(SPACE)
    return pressed_at


@dataclass(frozen=True)
class Landing:
    """What one predictive fire turned into. `outcome` names why, when there is no number."""

    outcome: str
    round_trip_ms: Optional[float] = None
    verdict: Optional[str] = None
    error_deg: Optional[float] = None


def plausible_round_trip(round_trip_ms, fit):
    """Is this a latency, or a wrapped revolution wearing one's clothes?

    `time_to_angle` returns the NEXT time the needle reaches the settled angle. A landing a
    degree or two BEHIND the extrapolated press position — which fit error alone produces,
    at 1.7-2.0 deg RMS — therefore comes back as a whole revolution rather than a value near
    zero. Half a revolution is 552 ms at the median 326 deg/s and 295 at the Hyperfocus
    ceiling, both far above any latency this link has shown, so anything past it is the wrap
    rather than a slow press. Flagged and excluded from the median, never silently averaged
    in: one bogus 1100 would move a ten-sample median more than the jitter being measured.
    """

    if fit is None or abs(fit.rate_deg_s) < 1e-6:
        return False
    return 0.0 <= round_trip_ms <= 180.0 / abs(fit.rate_deg_s) * 1000.0


def adapt_lead(lead_ms, measured_ms, gain=LEAD_GAIN, lo=LEAD_MIN_MS, hi=LEAD_MAX_MS):
    """Pull the lead a fraction of the way onto the round trip this check actually took.

    The measured round trip is `lead - overshoot + landing_error/rate` — the same
    measurement as the landing error, restated in milliseconds, and the only latency
    figure this project has that was taken armed against the game. Following it fully
    would put the whole of a 46-98 ms spread into the next aim; following a third of it
    converges on the link's median in a handful of checks and shrugs off one bad one.

    This removes the CONSTANT part of the error. It cannot touch the jitter, which at
    +/-17 ms is +/-5.6 deg against a Great half-width of 5.25 and is the ceiling on this
    whole approach.
    """

    return min(hi, max(lo, (1.0 - gain) * lead_ms + gain * measured_ms))


class LandingLog:
    """Append-only JSONL, one object per predictive fire, readings included.

    The freeze watch used to collect thirty-odd grabs, print a single line and throw the
    rest away. That is why a quarter of fires spent four sessions filed under "the press
    never arrived" with no way to tell that from "the watch could not read the landing" —
    the raw series distinguishes them instantly and nothing was keeping it.
    """

    def __init__(self, path):
        self.path = path
        self.handle = None
        self.written = 0

    def write(self, record):
        if self.handle is None:
            self.handle = open(self.path, "a", encoding="utf-8")
        self.handle.write(json.dumps(record) + "\n")
        self.handle.flush()   # a match that ends in a crash must still leave its evidence
        self.written += 1

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if not ordered:
        return None
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def summarise_landings(landings):
    """Lines summarising a run's landings. Pure, so the arithmetic can be tested.

    Reports the checks that produced NO reading alongside those that did. The sample is
    censored — a press that never registers leaves no freeze to measure — and censored in
    the direction that flatters the result, since the checks it drops are the ones that went
    worst. A spread quoted without the count it excluded reads tighter than the link is.
    """

    if not landings:
        return ["landings: none — no predictive fire reached a landing"]

    read = [l for l in landings if l.round_trip_ms is not None]
    verdicts = [l.verdict for l in landings if l.verdict]
    trips = [l.round_trip_ms for l in read]
    errors = [l.error_deg for l in landings if l.error_deg is not None]

    # `ungraded` must be in this list, not just in the count: a full-white check has no
    # measurable Great band, so it is scored as a hit but never as a Great. Leaving it out
    # made the header claim more fires than the tally accounted for.
    tally = ", ".join(f"{v} {verdicts.count(v)}"
                      for v in ("GREAT", "good", "MISS", "ungraded")
                      if verdicts.count(v))
    lines = [f"landings: {len(verdicts)} of {len(landings)} fires scored"
             + (f" — {tally}" if tally else "")]

    if trips:
        lines.append(f"  round trip: median {median(trips):.0f} ms, {min(trips):.0f}-"
                     f"{max(trips):.0f} (spread {max(trips) - min(trips):.0f}), n={len(trips)}")
    if errors:
        lines.append(f"  landing error: median {median(errors):+.1f} deg, "
                     f"{min(errors):+.1f} to {max(errors):+.1f} — negative is early, "
                     f"the expensive direction")

    missed = [l.outcome for l in landings if l.round_trip_ms is None]
    if missed:
        lines.append("  no round trip from: "
                     + ", ".join(f"{missed.count(o)} {o}" for o in sorted(set(missed))))
    return lines


def no_press_note(desc, tracker, decision, tracked_ms):
    """One line explaining a check that ended without a press, or None if there is none.

    The silent failure this closes: a tracked check whose `decide` never schedules and
    never sets `may_react` — too few samples, a fit that never tightened, a rate out of
    range — simply falls out of the loop. It writes nothing at all, so a match that missed
    six checks and one that saw six is the same log. Every miss so far has been diagnosed
    off a fire that DID happen, which is a survivorship bias baked into the instrument.
    """

    if tracker is None or not tracker.samples or tracker.fired_at_ms is not None:
        return None

    reason = "never decided" if decision is None else decision.reason
    note = (f"NO PRESS: {desc} — {reason}; {len(tracker.samples)} samples over "
            f"{tracked_ms:.0f} ms")

    fit = None if decision is None else decision.fit
    if fit is not None:
        note += f", fit {fit.rms_deg:.1f} deg RMS at {fit.rate_deg_s:+.0f} deg/s"
    if tracker.zone is None:
        note += ", no zone found"
    else:
        note += (f", Great {tracker.zone.great_start:.0f}-{tracker.zone.great_end:.0f} deg")
    return note


def report_landing(model, tracker, track_t0, args, pressed_at, fit=None,
                   lead_ms=None, record=None, context=None):
    """Read where the press landed and how long it took to get there.

    A successful hit stops the needle dead at the hit position, so the frozen angle is the
    landing — measured in the same frames, about the same centre, against the same drawn
    zone the press was aimed at. Without this the bot's accuracy could only be inferred
    from a Great/Good tally at the end of a match, which is how a 3x measurement error
    went unnoticed here before.

    The round trip comes from WHERE the needle stopped, not from when we noticed: the fit
    says when our capture showed that angle, and the gap from key-down to that instant is
    the closed-loop latency. Timing the freeze off the tail reads directly was the first
    attempt and it is not robust — a single stray read during the failed-check animation
    truncates the search and inflates the figure, which it did on 2026-08-15, reporting
    409 ms for a freeze the needle's own position puts at 88. The tail-read figure is kept
    as a cross-check, because the two agreeing is worth something and the two disagreeing
    is worth more.

    This is the only measurement of the round trip taken under real armed-run load;
    `measure_latency.py` takes it idle, in its own process, against a host text field.

    Every grab is kept and handed to `record`, because the line this function prints is a
    conclusion and the readings are the evidence for it. Four sessions were spent guessing
    at a 25% loss that this loop had the data to explain and was discarding.
    """

    if args.dry_run or tracker.zone is None:
        return Landing("not scored")

    # The floor is relative to THIS check's own needle. An absolute 20 admits the stray red
    # left behind once the check clears, which scores 20-45 with a meaningless angle.
    reference = strength_reference([s.strength for s in tracker.samples])
    deadline = pressed_at + FREEZE_WATCH_SECONDS
    readings, watch = [], read_watch(())
    while monotonic() < deadline:
        t = monotonic()
        bgr = model.grab_screenshot()[:, :, ::-1]
        angle, strength = needle_angle(bgr, tracker.centre)
        readings.append(Reading(t, angle, strength))
        watch = read_watch(readings, reference)
        if watch.outcome == "frozen":
            break                       # three agreeing reads; nothing more to learn
        if watch.lit >= 3 and watch.dark_tail >= CLEARED_DARK_READS:
            break                       # the check has left the screen

    def finish(landing, **extra):
        if record is not None:
            entry = dict(context or {})
            entry.update({
                "outcome": watch.outcome,
                "landing": landing.outcome,
                "reads": watch.reads,
                "lit": watch.lit,
                "dark_tail": watch.dark_tail,
                "lit_floor": round(watch.floor, 1),
                "reference_strength": None if reference is None else round(reference, 1),
                "lead_ms": lead_ms,
                "watch_ms": round((monotonic() - pressed_at) * 1000.0, 1),
                "rate_deg_s": None if fit is None else round(fit.rate_deg_s, 1),
                "fit_rms_deg": None if fit is None else round(fit.rms_deg, 2),
                "fit_n": None if fit is None else fit.n,
                "zone": {"great_start": tracker.zone.great_start,
                         "great_end": tracker.zone.great_end,
                         "zone_start": tracker.zone.zone_start,
                         "zone_end": tracker.zone.zone_end},
                "readings": [[round((r.t - pressed_at) * 1000.0, 1), round(r.angle, 1),
                              round(r.strength, 1)] for r in readings],
            })
            entry.update(extra)
            record(entry)
        return landing

    if watch.outcome in ("no reads", "dark"):
        log(f"  landing: needle gone before it could be read ({watch.lit} lit of "
            f"{watch.reads} reads, floor {watch.floor:.0f})")
        return finish(Landing("needle gone"))

    if watch.outcome == "sweeping":
        if watch.dark_tail >= CLEARED_DARK_READS:
            # The needle swept to the end of its arc and the check vanished unhit. A press
            # that connects freezes it; a press that misses ends the check outright. Either
            # way something would have stopped. Nothing did, so nothing arrived.
            log(f"  landing: the check swept to its end and cleared — the press did not "
                f"reach the game ({watch.lit} lit reads, then {watch.dark_tail} dark)")
            return finish(Landing("check cleared"))
        log(f"  landing: needle still sweeping {FREEZE_WATCH_SECONDS * 1000:.0f} ms after "
            f"the press — it did not connect, or this is Merciless Storm, which never stops")
        return finish(Landing("still sweeping"))

    settled = watch.angle
    verdict, err = score_freeze(tracker.zone, settled)
    log(f"  landed {settled:.1f} deg — {verdict}"
        + (f", {err:+.1f} deg from Great centre" if err is not None else ""))

    press_ms = (pressed_at - track_t0) * 1000.0
    measured = time_to_angle(fit, settled, press_ms) if fit is not None else None
    if measured is None:
        return finish(Landing("no fit", verdict=verdict, error_deg=err),
                      settled_deg=round(settled, 1), verdict=verdict,
                      error_deg=None if err is None else round(err, 2))

    # The correction is signed and directly actionable: pass it as --round-trip-ms. Landing
    # early means we led by more than the loop actually costs, and early is the expensive
    # error — Great sits at the leading edge of the success zone, so a late press spills
    # into Good while an early one misses outright.
    round_trip_ms = measured - press_ms
    onset = watch.onset

    # The tail read is quantised by the grab interval: the watch starts at key-down and
    # grabs every ~25 ms, so the freeze can only be seen at the next multiple of that. Nine
    # of ten armed checks reported 77-81 ms and one 103 — which is one bucket and its
    # neighbour, not a link holding 79 +/- 2, and it is consistent with the 46-98 ms the
    # fit gives. Quote the interval alongside the reading so the two are never again read
    # as a disagreement.
    if onset is not None:
        gaps = [(b.t - a.t) * 1000.0 for a, b in zip(readings, readings[1:])]
        interval = median(gaps) or 0.0
        cross = (f", tail read says {(onset - pressed_at) * 1000:.0f} "
                 f"+/-{interval:.0f} (grab interval)")
    else:
        cross = ""

    scored = dict(settled_deg=round(settled, 1), verdict=verdict,
                  error_deg=None if err is None else round(err, 2),
                  round_trip_ms=round(round_trip_ms, 1),
                  tail_read_ms=None if onset is None else round((onset - pressed_at) * 1000, 1))

    if not plausible_round_trip(round_trip_ms, fit):
        log(f"  round trip {round_trip_ms:.0f} ms — IMPLAUSIBLE, past half a revolution; "
            f"reading it as a wrap and excluding it{cross}")
        return finish(Landing("implausible", verdict=verdict, error_deg=err), **scored)

    assumed = lead_ms if lead_ms is not None else getattr(args, "round_trip_ms", 0.0)
    log(f"  round trip {round_trip_ms:.0f} ms measured, against "
        f"{assumed:.0f} ms assumed{cross}")
    return finish(Landing("measured", round_trip_ms=round_trip_ms, verdict=verdict,
                          error_deg=err), **scored)


def run(args):
    monitoring = Monitoring_window(
        window_query=args.window,
        crop_size=224,
        stream_aspect=parse_aspect(args.aspect),
    )
    log(f"window: {monitoring.describe()}")

    watcher = FocusWatcher(
        query=args.window,
        require_on_screen=not args.no_require_on_screen,
    )

    model = AI_model(
        model_path=args.model,
        use_gpu=False,
        nb_cpu_threads=args.threads,
        monitoring=monitoring,
    )
    log(f"provider: {model.check_provider()}   dry_run: {args.dry_run}")
    log(f"firing: {'predictive' if args.predict else 'reactive only'}"
        + (f", leading by {args.round_trip_ms:.0f} ms"
           f"{' (adaptive)' if args.adapt_lead else ' (fixed)'}" if args.predict else ""))

    landing_log = None
    if args.landing_log_enabled and not args.dry_run:
        path = args.landing_log or f"landings-{strftime('%Y%m%d-%H%M%S')}.jsonl"
        landing_log = LandingLog(path)
        log(f"freeze watch readings -> {path}")
    log(f"waiting for '{args.window}' to become active (ctrl-c to quit)")

    lead_ms = args.round_trip_ms

    active = False
    frames = 0
    hits = 0
    landings = []          # one Landing per predictive fire; summarised at exit
    window_start = monotonic()
    seen_frontmost = None

    tracker = None          # TrackerState for the check in progress
    track_t0 = None         # monotonic origin for that check's timestamps
    decision = None         # the last thing `decide` said about it, for the post-mortem
    tracked_desc = ""       # what the classifier called it, for the same reason
    no_press = []           # reasons tracked checks ended without a press

    def stand_down(tracker):
        """Drop the tracker, saying why if the check never got a press. Returns None."""

        note = no_press_note(tracked_desc, tracker, decision,
                             0.0 if track_t0 is None else (monotonic() - track_t0) * 1000.0)
        if note is not None:
            log(note)
            # The counts are per reason, and `decide` builds its reasons with the numbers
            # in them ("only 4 samples"), so the digits come out or every check is its own
            # category and the tally says nothing.
            no_press.append(sub(r"[-+]?[0-9.]+", "N",
                                "never decided" if decision is None else decision.reason))
        return None

    dropped = 0             # consecutive check-free frames since the last tracked one
    last_capture = None
    frame_ms = DEFAULT_FRAME_MS

    try:
        while True:
            now_active = watcher.is_active()

            # Log focus changes using the cached answer from the check above. Without
            # this, a run with no PAUSED/RESUMED lines is indistinguishable from a run
            # where the gate silently stopped tracking.
            current_frontmost = watcher.last_frontmost
            if current_frontmost != seen_frontmost:
                log(f"  focus: {seen_frontmost!r} -> {current_frontmost!r}")
                seen_frontmost = current_frontmost

            # --- edge: active -> paused -------------------------------------------
            if active and not now_active:
                # Never leave SPACE held down in whatever app just took focus.
                if not args.dry_run:
                    ReleaseKey(SPACE)
                elapsed = monotonic() - window_start
                fps = frames / elapsed if elapsed > 0 else 0
                log(f"PAUSED  (focus lost) — {frames} frames, {hits} hits, {fps:.0f} fps")
                active = False
                tracker, last_capture = stand_down(tracker), None  # the check is long
                                                    # gone by the time focus comes back

            # --- edge: paused -> active -------------------------------------------
            elif not active and now_active:
                sleep(RESUME_SETTLE_SECONDS)  # let the Space transition finish
                previous_region = dict(monitoring.region)

                if args.pin_geometry:
                    log(f"RESUMED (focus gained) — pinned to {monitoring.region}")
                    active = True
                    frames = 0
                    hits = 0
                    window_start = monotonic()
                    continue

                try:
                    # The window may have moved or resized while we were away.
                    monitoring.refresh()
                except WindowNotFoundError as e:
                    log(f"active but window lookup failed: {e}")
                    sleep(IDLE_POLL_SECONDS)
                    continue

                # Silent geometry drift is the dangerous failure: the model keeps
                # running against a mis-scaled crop and simply stops detecting.
                if dict(monitoring.region) != previous_region:
                    log(f"WARNING geometry changed: {previous_region} -> {monitoring.region}")
                    log("  if detection degrades, re-run with --pin-geometry")

                log(f"RESUMED (focus gained) — capturing {monitoring.region}")
                active = True
                frames = 0
                hits = 0
                window_start = monotonic()

            if not active:
                sleep(IDLE_POLL_SECONDS)
                continue

            # --- armed loop --------------------------------------------------------
            captured = monotonic()
            frame = model.grab_screenshot()
            frames += 1
            pred, desc, probs, should_hit = model.predict(frame)

            if last_capture is not None:
                dt = (captured - last_capture) * 1000.0
                if dt < 200.0:  # ignore the gap across a pause or a cooldown
                    frame_ms = FRAME_MS_DECAY * frame_ms + (1 - FRAME_MS_DECAY) * dt
            last_capture = captured

            # --- predictive path: sweeping checks ----------------------------------
            if args.predict and pred in TRACKED_PREDS:
                if tracker is None:
                    tracker, track_t0 = TrackerState(), captured
                dropped = 0
                tracked_desc = desc

                # grab_screenshot returns RGB; the needle test is R - max(G,B) on a BGR
                # array, so an unconverted frame silently measures blueness instead.
                tracker = observe(tracker, frame[:, :, ::-1], (captured - track_t0) * 1000.0)
                decision = decide(tracker, (monotonic() - track_t0) * 1000.0, lead_ms)

                if decision.press_at_ms is not None:
                    now_ms = (monotonic() - track_t0) * 1000.0
                    # Hold only while another frame would land in time to sharpen the fit.
                    # The margin is on the safe side of the frame-period estimate: waiting
                    # for a frame that arrives late costs the whole check, whereas firing
                    # a frame early costs a few frames of fit quality.
                    if decision.press_at_ms - now_ms > frame_ms * 1.25:
                        continue

                    hits += 1
                    tracker = mark_fired(tracker, now_ms)
                    # Age of the freshest frame the fit was built on. This is OUR share of
                    # the delay — capture plus inference — and it is the half we can fix in
                    # this file. Whatever the measured round trip exceeds it by belongs to
                    # the stream, and no constant in here will move it.
                    frame_age_ms = now_ms - tracker.samples[-1].t_ms if tracker.samples else 0.0
                    requested_ms = decision.press_at_ms - now_ms
                    pressed_at = fire(args, requested_ms)

                    fit = decision.fit
                    log(f"{'WOULD FIRE' if args.dry_run else 'FIRE'} predictive: {desc} — "
                        f"{fit.rate_deg_s:+.0f} deg/s, fit {fit.rms_deg:.1f} deg RMS over "
                        f"{fit.n} frames, aiming {decision.target_deg:.1f} deg")
                    log(f"  timing: frame age {frame_age_ms:.0f} ms at decide, lead "
                        f"{requested_ms:.0f} ms requested / "
                        f"{(pressed_at - track_t0) * 1000 - now_ms:.0f} ms slept")
                    landing = report_landing(
                        model, tracker, track_t0, args, pressed_at, decision.fit,
                        lead_ms=lead_ms,
                        record=None if landing_log is None else landing_log.write,
                        context={"fire": len(landings) + 1, "at": strftime("%H:%M:%S"),
                                 "desc": desc, "target_deg": decision.target_deg,
                                 "lead_requested_ms": round(requested_ms, 1),
                                 "lead_slept_ms": round(
                                     (pressed_at - track_t0) * 1000 - now_ms, 1),
                                 "frame_age_ms": round(frame_age_ms, 1)})
                    if landing is not None:
                        landings.append(landing)

                    # Follow the link rather than a constant measured on one match. The
                    # correction is applied AFTER the landing is recorded, so the log shows
                    # what each check was actually aimed with.
                    if args.adapt_lead and landing.round_trip_ms is not None:
                        adapted = adapt_lead(lead_ms, landing.round_trip_ms)
                        if abs(adapted - lead_ms) >= 1.0:
                            log(f"  lead: {lead_ms:.0f} -> {adapted:.0f} ms")
                        lead_ms = adapted
                    tracker = None
                    sleep(HIT_COOLDOWN_SECONDS)
                    window_start, last_capture = monotonic(), None
                    frames = 0

                # A fitted check with nowhere to aim (no Great band drawn, or the band
                # already passed) is exactly the reactive case: pressing on the model's
                # cue lands in Good, which beats not pressing at all.
                elif should_hit and decision.may_react:
                    hits += 1
                    fire(args, 0.0)
                    log(f"{'WOULD HIT' if args.dry_run else 'HIT'} reactive: {desc} — "
                        f"tracker stood down ({decision.reason})")
                    tracker = None
                    sleep(HIT_COOLDOWN_SECONDS)
                    window_start, last_capture = monotonic(), None
                    frames = 0
                continue

            if pred in WIGGLE_PREDS:
                # wiggle oscillates; a linear fit is wrong by construction
                tracker = stand_down(tracker)
            elif tracker is not None:
                dropped += 1
                if dropped >= TRACK_DROP_FRAMES:
                    tracker = stand_down(tracker)

            # --- reactive path: wiggle, and everything when --no-predict ------------
            if should_hit:
                if pred == ANTE_FRONTIER_PRED and args.hit_ante > 0:
                    sleep(args.hit_ante * 0.001)

                hits += 1
                confidence = float(max(probs.values()))
                log(f"{'WOULD HIT' if args.dry_run else 'HIT'}: {desc} ({confidence:.3f})")
                fire(args, 0.0)

                sleep(HIT_COOLDOWN_SECONDS)  # don't re-trigger on the same skill check
                window_start, last_capture = monotonic(), None
                frames = 0

    except KeyboardInterrupt:
        log("stopping")
    finally:
        if not args.dry_run:
            ReleaseKey(SPACE)  # belt and braces: never exit with the key held
        # The per-check lines scroll past during a match and the log is read afterwards, so
        # the run has to state its own result rather than leaving it to be greped out.
        for line in summarise_landings(landings):
            log(line)
        if no_press:
            # Recall, not precision: these are the checks the aim numbers above never saw.
            tally = ", ".join(f"{no_press.count(r)} {r}" for r in sorted(set(no_press)))
            log(f"  no press: {len(no_press)} tracked checks never got one — {tally}")
        if args.predict and args.adapt_lead:
            log(f"  lead: started at {args.round_trip_ms:.0f} ms, ended at {lead_ms:.0f}")
        if landing_log is not None:
            log(f"  {landing_log.written} freeze watches recorded in {landing_log.path}")
            landing_log.close()
        model.cleanup()


if __name__ == "__main__":
    run(parse_args())
