"""The opt-in `1-2-3-4!` player: one press per beat, timed like the operator's own.

OFF by default (`--play-performance`), and live only inside the PERFORMANCE_SECONDS window
the operator's own Active Ability press opens. Outside that window, or without the flag,
autorun stands down exactly as it did before this module existed.

What the check is, from the first whole Performance ever recorded
(`frames/bout_20261125-152526`, 15:25:23-41) and the operator's keyed presses of 09-13
(`frames/bout_20260913-130005`, read by `tools/pair_keys.py`):

  * The needle NEVER stops and is visible the whole time, sweeping ~300 deg/s.
  * An unfilled ~120 deg arc is drawn 0.5-0.7 s ahead of the needle (sometimes only 7-13 deg
    ahead), and it relocates ~20 deg after the needle enters it — 6-36 deg over 25 arcs.
    The arc is a METRONOME: the needle reaches its leading edge on the beat.
  * The operator, who passes every time, presses with the needle 2-12 deg past the leading
    edge as displayed, so after a round trip the press registers ~15-30 deg in.

So the target is NOT "anywhere inside the arc". The 09-12 attempt landed presses 15 and 28
deg inside drawn arcs and the Performance still failed. The target is the operator's band:
land LAND_DEG past the leading edge, never past MAX_LAND_DEG, once per arc.

Pure. Every function takes a state and returns a new one; the caller owns the clock, the
frames and the keyboard. The arc reader `find_outline_arc` lives here too, recovered from
`8a1c62d^`. `tools/pair_keys.py` keeps its own analysis copy (width gate 140, not 145).

WHAT TURNS IT ON. The operator's Active Ability press only ALLOWS the player; the screen has
to confirm a Performance before a single press (`session_step`). The window is designed to
err long — an ability press that started nothing (15:38:13 on 11-25) still opens one — and
over the whole 11-25 bout the same player run outside the windows fired 5 times on Storm and
repair checks whose outline it misread. So: the ring must locate with a real peak,
CONFIRM_FRAMES frames must classify as a Performance (`full black (out)` on 88-92% of
Performance frames, `repair-heal` on Storm), no press ever goes out on a repair or wiggle
frame, and LAPSE_S with no arc ends playing for the rest of the window.
"""

from dataclasses import dataclass, replace
from typing import Optional, Tuple

import numpy as np

from dbd.utils.needle_tracker import (
    ANGLE_STEP, CENTRE_PRIOR, FILL_RUN_PX, HOT, MIN_CENTRE_PEAK, MIN_GREAT_DEG,
    MIN_NEEDLE_STRENGTH,
    RADIUS_STEP, WINDOW_IN, WINDOW_OUT, ZONE_THICK_PX, Zone, _close_gaps, _longest_run,
    _max_contiguous, needle_angle, refine_centre, sample_rays, static_image,
)

# 1-2-3-4! never reads below 106 deg; Merciless Storm draws the same unfilled outline at 39-40
# and Madness Storm at 37-56, so 90 keeps Storm out with 34 deg of daylight. 11-25 read up to
# 140 with the needle's glow merged in.
OUTLINE_MIN_DEG = 90.0
OUTLINE_MAX_DEG = 145.0

# Where the press should REGISTER, measured from the arc's leading edge along the sweep. The
# operator's presses go down 2-12 deg in and register one round trip later; the arc on 11-25
# relocated a median ~20 deg in. MAX_LAND_DEG is the far end of what the operator's own
# presses have been seen at (relocations up to 36 deg on a Performance the operator played).
LAND_DEG = 20.0
MAX_LAND_DEG = 35.0

FIT_FRAMES = 8              # newest needle reads in the rate fit
FIT_MAX_AGE_MS = 350.0      # older reads are dropped — the fit spans ~8 frames at ~35 fps
FIT_MIN_SAMPLES = 4
MAX_FIT_RMS_DEG = 6.0
MIN_RATE_DEG_S = 150.0
# Measured 271-336 on 11-25. Capped well below the ~530 at which a 680 ms beat would put
# consecutive arcs in the same place and SAME_ARC_TOL_DEG would merge them.
MAX_RATE_DEG_S = 450.0
MIN_PRESS_GAP_MS = 300.0    # beats were 540-810 ms apart; nothing legitimate is closer
# Two reads are one arc when BOTH edges agree within this — the edge the needle approaches
# reads up to ~15 deg late as its glow eats it (108 -> 119, 89 -> 104 on 11-25), and a far
# edge read 13 deg apart between frames (351 vs 338). Consecutive arcs sit beat x rate = 160-245 deg apart at the rates
# MAX_RATE_DEG_S admits, so 20 cannot merge two.
SAME_ARC_TOL_DEG = 20.0

LOCATE_FRAMES = 8           # frames the ring is located from, once per Performance

# The arc read is only as good as the centre. Located from the Performance's own frames
# ((111.5, 102.0) on 11-25) one arc reads a steady 63-174, 111 deg — the width measured on
# 09-12. A hand-typed (112, 102) reads the same arc 48-56 to 174-177 and flickers: the base
# ring bleeds into the read 1 px off-centre. So never read arcs about an assumed centre.

# Nothing is planned from a read this old: a vanished ring must not be extrapolated into a
# press a revolution later (the H1 review finding). Three frames at ~35 fps.
STALE_READ_MS = 100.0
ARC_STALE_MS = 200.0        # the arc read drops out for a frame or two near the needle

# The session gates — see the module docstring, WHAT TURNS IT ON.
PERFORMANCE_PREDS = (7,)    # `full black (out)`: what a Performance frame classifies as
# An ALLOWLIST: every 11-25 Performance frame classified 7 or 0. A denylist of the classes a
# Performance is not would leave 4, 5 and 6 pressable for no reason.
PRESS_PREDS = (0, 7)
NEVER_PRESS_PREDS = (1, 2, 3, 8, 9, 10)   # repair-heal and wiggle: clears the confirmation
# Confirmation is RECENT, never cumulative: CONFIRM_FRAMES of the last CONFIRM_WINDOW frames
# classed 7, with none of them repair or wiggle. Class 7 alone is NOT a Performance —
# NOTES records Merciless Storm classifying `full black` (870 of 890 frames in one clip) —
# so this gate only keeps the player off the clearly-wrong; the arc reader (an unfilled
# 90-145 deg outline) and the negative-control replay are what the rest rests on.
CONFIRM_FRAMES = 5
CONFIRM_WINDOW = 8
LOCATE_RETRY_FRAMES = 4     # a failed ring search waits this many frames before the next
KNOWN_RING_MIN = 3          # ordinary-check centres needed before `known_ring` is trusted
# A Performance RESTS between phrases: 2.3 s with no arc mid-Performance on 11-25 (15:43:17),
# and the operator's presses on 09-13 had a 3.75 s gap. The lapse only has to catch a
# cancelled Performance — the staleness guards already stop stale presses — so err long.
LAPSE_S = 4.0


@dataclass(frozen=True)
class Arc:
    """One drawn arc, as its two drawn edges in clockwise order, read when it was first seen
    — before the needle's own red glow eats 6-12 deg of the edge it approaches.

    Identity is BOTH edges and never depends on which way the needle sweeps: on 11-25 the
    needle reversed for one beat, and an identity keyed on the far end (which flips with
    the direction) took the next, different arc for the one just pressed and skipped it."""

    start: float
    end: float

    def entry(self, sign):
        """The edge a needle sweeping this way reaches first."""
        return self.start if sign > 0 else self.end

    def same_as(self, other):
        return (other is not None
                and abs(_wrap(self.start - other.start)) <= SAME_ARC_TOL_DEG
                and abs(_wrap(self.end - other.end)) <= SAME_ARC_TOL_DEG)


@dataclass(frozen=True)
class PerfState:
    samples: Tuple[Tuple[float, float], ...] = ()   # (t_ms, unwrapped needle deg)
    arc: Optional[Arc] = None
    arc_seen_ms: Optional[float] = None             # last frame the arc was READ on
    retired: Optional[Arc] = None                   # the last arc pressed
    last_press_ms: Optional[float] = None


@dataclass(frozen=True)
class Plan:
    press_at_ms: Optional[float]
    reason: str
    entry_deg: Optional[float] = None
    rate_deg_s: Optional[float] = None
    land_deg: Optional[float] = None    # where along the arc this press should register


def find_outline_arc(static, centre, ring_r, angle_step=ANGLE_STEP):
    """The drawn arc of a relocating outline check, as a Zone, or None.

    Recovered from `8a1c62d^:dbd/utils/needle_tracker.py`. Read from ONE frame: the arc
    relocates every beat, and a median over frames smears two positions into one.
    """

    radii = np.arange(ring_r + WINDOW_IN, ring_r + WINDOW_OUT, RADIUS_STEP)
    angles, polar = sample_rays(static, centre[0], centre[1], radii, angle_step=angle_step)
    hot = (polar - np.median(polar, axis=0, keepdims=True)) > HOT

    # Any solid fill at all and this is not our check — the complement of `find_zone`'s
    # Great-band test.
    runs = np.array([_max_contiguous(hot[i]) for i in range(hot.shape[0])])
    band = _longest_run(runs >= FILL_RUN_PX, angle_step)
    if band is not None and band[1] * angle_step >= MIN_GREAT_DEG:
        return None

    thickness = hot.sum(axis=1) * RADIUS_STEP
    found = _longest_run(_close_gaps(thickness >= ZONE_THICK_PX, angle_step), angle_step)
    if found is None:
        return None
    start, length = found
    width = length * angle_step
    if not (OUTLINE_MIN_DEG <= width <= OUTLINE_MAX_DEG):
        return None
    edge = float(angles[start])
    return Zone(great_start=edge, great_end=(edge + width) % 360.0,
                zone_start=edge, zone_end=(edge + width) % 360.0)


def _wrap(deg):
    """Into (-180, 180]."""
    d = deg % 360.0
    return d - 360.0 if d > 180.0 else d


def _fit(samples):
    """(rate deg/s, angle at the newest sample's time, rms) or None."""

    if len(samples) < FIT_MIN_SAMPLES:
        return None
    t = np.array([s[0] for s in samples])
    a = np.array([s[1] for s in samples])
    if t[-1] - t[0] <= 0:
        return None
    slope, icept = np.polyfit(t, a, 1)
    rms = float(np.sqrt(np.mean((a - (slope * t + icept)) ** 2)))
    return float(slope * 1000.0), float(slope * t[-1] + icept), rms


def _rate_sign(samples):
    fit = _fit(samples)
    return None if fit is None else (1.0 if fit[0] >= 0 else -1.0)


def observe(state, t_ms, needle_deg, zone):
    """A new state with this frame's needle read and arc read folded in."""

    samples = state.samples
    if needle_deg is not None:
        unwrapped = needle_deg
        if samples:
            prev = samples[-1][1]
            unwrapped = prev + _wrap(needle_deg - prev)
        samples = tuple(s for s in samples + ((t_ms, unwrapped),)
                        if t_ms - s[0] <= FIT_MAX_AGE_MS)[-FIT_FRAMES:]

    arc = state.arc
    if zone is not None:
        read = Arc(start=zone.zone_start, end=zone.zone_end)
        if not read.same_as(arc):
            arc = read
        return replace(state, samples=samples, arc=arc, arc_seen_ms=t_ms)
    return replace(state, samples=samples, arc=arc)


def plan(state, now_ms, lead_ms):
    """When to press for the current arc, so the press registers LAND_DEG into it."""

    if state.arc is None:
        return Plan(None, "no arc drawn")
    if state.arc_seen_ms is None or now_ms - state.arc_seen_ms > ARC_STALE_MS:
        return Plan(None, "arc not read recently")
    if not state.samples or now_ms - state.samples[-1][0] > STALE_READ_MS:
        return Plan(None, "needle not read recently")
    if state.arc.same_as(state.retired):
        return Plan(None, "already pressed this arc")
    fit = _fit(state.samples)
    if fit is None:
        return Plan(None, f"only {len(state.samples)} needle reads")
    rate, angle_newest, rms = fit
    if rms > MAX_FIT_RMS_DEG:
        return Plan(None, f"fit too poor ({rms:.1f} deg RMS)", rate_deg_s=rate)
    if not MIN_RATE_DEG_S <= abs(rate) <= MAX_RATE_DEG_S:
        return Plan(None, f"rate {rate:.0f} deg/s out of range", rate_deg_s=rate)

    sign = 1.0 if rate > 0 else -1.0
    entry = state.arc.entry(sign)
    speed = abs(rate) / 1000.0
    t_newest = state.samples[-1][0]
    angle_now = angle_newest + rate * (now_ms - t_newest) / 1000.0
    # Where along the sweep, from the entry edge, a press sent NOW would register.
    offset_now = _wrap((angle_now + rate * lead_ms / 1000.0 - entry) * sign)

    earliest = now_ms
    if state.last_press_ms is not None:
        earliest = max(earliest, state.last_press_ms + MIN_PRESS_GAP_MS)

    if offset_now < LAND_DEG:
        press_at, land = now_ms + (LAND_DEG - offset_now) / speed, LAND_DEG
    elif offset_now <= MAX_LAND_DEG:
        press_at, land = now_ms, offset_now
    else:
        return Plan(None, f"too late, would land {offset_now:.0f} deg in",
                    entry_deg=entry, rate_deg_s=rate)

    if press_at < earliest:
        landing = offset_now + (earliest - now_ms) * speed
        if landing > MAX_LAND_DEG:
            return Plan(None, "too soon after the last press",
                        entry_deg=entry, rate_deg_s=rate)
        press_at, land = earliest, max(land, landing)
    return Plan(press_at, "scheduled", entry_deg=entry, rate_deg_s=rate,
                land_deg=land)


def pressed(state, t_ms):
    """A new state with the current arc retired, so it is never pressed twice."""

    return replace(state, retired=state.arc,
                   last_press_ms=t_ms)


def locate_ring(frames, prior=CENTRE_PRIOR):
    """(centre, ring_r, peak) from the first frames of a Performance, located ONCE — the ring
    does not move within a Performance, and the arc read is only as good as this centre.
    `peak` under MIN_CENTRE_PEAK means `refine_centre` fell back to the prior: no ring."""

    cx, cy, ring_r, peak = refine_centre(static_image(list(frames)), prior=prior)
    return (cx, cy), ring_r, peak


def read_frame(frame_bgr, centre, ring_r):
    """(needle angle or None, arc Zone or None) for one 224 BGR crop. The needle read is
    dropped below MIN_NEEDLE_STRENGTH, where nothing red is drawn at all."""

    angle, strength = needle_angle(frame_bgr, centre)
    arc = find_outline_arc(static_image([frame_bgr]), centre, ring_r)
    return (angle if strength >= MIN_NEEDLE_STRENGTH else None), arc


def operator_pressed_space(events, space_keycode):
    """Did the OPERATOR press SPACE in these drained key events? The bot's own presses are
    injected and read `source` 0 (`key_watcher.py`); hardware reads non-zero. Any operator
    press hands the rest of the Performance back to the operator."""

    return any(e.get("keycode") == space_keycode and e.get("source", 0) != 0
               for e in events)


@dataclass(frozen=True)
class Session:
    """The player across ONE Performance window. `done` set means hands off until it ends."""

    state: PerfState = PerfState()
    ring: Optional[tuple] = None          # (centre, ring_r)
    locate: tuple = ()                    # frames the ring is being located from
    recent: tuple = ()                    # the last CONFIRM_WINDOW classifier preds
    last_arc_s: Optional[float] = None
    done: Optional[str] = None


def known_ring(centres, min_count=KNOWN_RING_MIN):
    """The ring as ordinary checks this run located it: per-coordinate median of the
    tracker's refined (cx, cy, r) for centre-crop checks, or None with too few.

    Why not locate it from the Performance's own first frames: on 11-25 that landed 1-2 px
    off on two of four Performances (peaks 64 and 72, no better than the good ones), and 1 px
    off the arc reads on 20-35% of frames instead of 85-90%, so the lapse ended the session
    after 9 and 2 beats. The 30 ordinary checks of the same match refined to (111.5,
    101.5-102.0) r=65 every time — the reading that works. The ring is a fixed UI element."""

    if len(centres) < min_count:
        return None
    xs, ys, rs = zip(*centres)
    return (float(np.median(xs)), float(np.median(ys))), float(np.median(rs))


def session_step(session, frame_bgr, t_s, pred, now_ms, lead_ms, frame_ms,
                 prior=CENTRE_PRIOR, ring=None):
    """One frame. Returns (session, plan or None, note or None).

    A plan is returned only when a press is DUE within 1.25 frames and every gate passes;
    the caller fires it and calls `session_pressed`. `note` is a one-off line worth logging.
    Pure: the caller owns the clock and the keyboard.
    """

    if session.done is not None:
        return session, None, None
    recent = (session.recent + (pred,))[-CONFIRM_WINDOW:]
    session = replace(session, recent=recent)
    confirmed = (sum(p in PERFORMANCE_PREDS for p in recent) >= CONFIRM_FRAMES
                 and not any(p in NEVER_PRESS_PREDS for p in recent))

    note = None
    if session.ring is None and ring is not None:
        session = replace(session, ring=ring)
        note = (f"ring at ({ring[0][0]:.1f}, {ring[0][1]:.1f}) r={ring[1]:.0f}, from this "
                f"run's ordinary checks")
    if session.ring is None:
        locate = (session.locate + (frame_bgr,))[-LOCATE_FRAMES:]
        if len(locate) < LOCATE_FRAMES:
            return replace(session, locate=locate), None, None
        centre, ring_r, peak = locate_ring(locate, prior=prior)
        if peak < MIN_CENTRE_PEAK:
            # No ring yet. Keep looking, but every LOCATE_RETRY_FRAMES rather than every
            # frame: a full `refine_centre` per frame for a 16 s window that started nothing
            # is cost for no purpose.
            return replace(session, locate=locate[LOCATE_RETRY_FRAMES:]), None, None
        session = replace(session, ring=(centre, ring_r), locate=())
        note = f"ring at ({centre[0]:.0f}, {centre[1]:.0f}) r={ring_r:.0f}, peak {peak:.0f}"

    angle, arc = read_frame(frame_bgr, session.ring[0], session.ring[1])
    state = observe(session.state, t_s * 1000.0, angle, arc)
    last_arc = t_s if arc is not None else session.last_arc_s
    session = replace(session, state=state, last_arc_s=last_arc)

    # The lapse clock starts at the first arc, never at the ring: the first beat can come a
    # second or more after the ring is drawn.
    if last_arc is not None and t_s - last_arc > LAPSE_S:
        return (replace(session, done="lapsed"), None,
                f"no arc for {LAPSE_S:.1f}s — the Performance looks over; hands off")
    if not confirmed or pred not in PRESS_PREDS:
        return session, None, note
    p = plan(state, now_ms, lead_ms)
    if p.press_at_ms is None or p.press_at_ms - now_ms > frame_ms * 1.25:
        return session, None, note
    return session, p, note


def is_playing(session):
    """Is the player actually in charge right now? False before the screen confirms a
    Performance and after a lapse — the caller logs STAND DOWN as before in both."""

    recent = session.recent
    return (session.done is None and session.ring is not None
            and sum(p in PERFORMANCE_PREDS for p in recent) >= CONFIRM_FRAMES
            and not any(p in NEVER_PRESS_PREDS for p in recent))


def session_pressed(session, t_ms):
    return replace(session, state=pressed(session.state, t_ms))
