"""Predict where the skill-check needle will be, so the key can be pressed early.

Reacting to a Great classification cannot work here. The Great band is 10.5 deg wide
(measured from the drawn pixels, `tools/measure_zone.py`), the needle sweeps at roughly
300-400 deg/s, so the band lasts 26-37 ms — against a keypress-to-pixel round trip of
about 72 ms through Moonlight. The needle has left before the key lands, in every
configuration. See NOTES-local.md, "The Great-vs-Good problem".

The way out is to press at a predicted position instead of a seen one:

  1. locate the ring once per check, because the angle is measured ABOUT that point,
  2. read the drawn Great band out of the static pixels,
  3. fit needle angle against time as a straight line (velocity is constant within a
     check — 2.1-5.3 deg RMS across 13 checks, and 0.44-2.7 on native footage),
  4. schedule the press so the needle arrives at the middle of the Great band
     `round_trip_ms` after the key goes down.

Everything here is pure: frames and timestamps in, a decision out. `TrackerState` is
immutable — `observe()` returns a new state — so a live loop and an offline replay run
exactly the same code, which is what makes the replay evidence worth anything.

Three failure modes this module exists to avoid, each of which has already cost a day
somewhere in this project:

  * Assuming the ring is at the crop's centre. It is at (112, 102) in a 224 crop, and the
    10 px error lands straight in the angle, then in the velocity fit, as a per-revolution
    sinusoid that reads as non-constant velocity.
  * Assuming clockwise. The Doctor's Madness reverses a check, and a hardcoded direction
    mis-times a reversed check by twice the lead. Direction comes from the sign of the fit.
  * Measuring the zone through the classifier. Its `great` label is a hand-annotated
    "press about here" cue with margin, about 3x the drawn band. Measure pixels.
"""

from dataclasses import dataclass, replace
from typing import Optional, Tuple

import numpy as np

# --- geometry ------------------------------------------------------------------------
CENTRE_PRIOR = (112.0, 102.0)   # the ring in a 224 crop, NOT the crop's centre
CENTRE_SPAN = 3.0               # px searched around the prior; every check so far
CENTRE_STEP = 0.5               # refines to within half a pixel of the prior
RING_SEARCH = (45.0, 85.0)
RING_STEP = 0.5
SEARCH_ANGLE_STEP = 4.0         # rays used while searching for the centre; see refine_centre
SEARCH_RING_STEP = 1.0
STATIC_FRAMES = 8               # frames the static-UI median is taken over; see static_image
ANGLE_STEP = 1.0                # coarser than the offline tools; this runs per check live
NEEDLE_IN, NEEDLE_OUT = 55, 100  # annulus holding the needle, outside the SPACE prompt
NEEDLE_REDNESS = 25.0           # per-pixel red dominance that marks needle, not arc
MIN_NEEDLE_STRENGTH = 20.0      # absolute floor; below this nothing red is drawn at all
RELATIVE_FLOOR = 0.5            # of the check's own peak response; see lit_span()
MIN_CENTRE_PEAK = 60.0          # a good fit peaks well above this; below it, keep the prior

# --- zone extraction -----------------------------------------------------------------
WINDOW_IN, WINDOW_OUT = -6.0, 10.0   # radial window about the ring; excludes the prompt
RADIUS_STEP = 0.25
HOT = 60.0            # residual whiteness counted as drawn
FILL_RUN_PX = 4.0     # contiguous lit radius meaning a solid fill, not an outline
ZONE_THICK_PX = 1.5   # total lit radius meaning the arc is present at all
MIN_RUN_DEG = 3.0     # shorter angular runs are compression speckle
MIN_GREAT_DEG = 6.0   # a real Great band measures 10-13 deg (58 on full-white); anything
                      # narrower is speckle. This is what keeps the tracker off Merciless
                      # Storm, which draws an unfilled outline with no solid band at all —
                      # its Great geometry is unmeasured, so a press there is a guess.
MAX_GREAT_DEG = 20.0  # ...and the other end of that same fact. A `full white` check draws
                      # the whole zone as one solid block, so the fill run spans it and the
                      # band reads 33-59 deg. Firing there is fine — nine such fires on
                      # 2026-08-16 all landed inside the zone — but SCORING there is not:
                      # every one came back GREAT by construction and pulled the match
                      # tally from 78% to 84%. Grade only a band we actually measured.
CLOSE_DEG = 4.0       # bridge dropouts this short; antialiasing punches 1-2 deg holes

# --- fitting and firing --------------------------------------------------------------
MIN_ZONE_FRAMES = 5      # frames of static UI needed before the zone median is trustworthy
ZONE_RETRY_EVERY = 3     # frames between retries while no zone has been found

# Bounds on what a track retains. A discrete check lasts about a second, but Merciless
# Storm is ONE continuous check running 20 s with no reset and no freeze — 680 frames at
# live rates, and the tracker abstains on it, so nothing ever clears the buffer. At
# 224x224x3 that is 100 MB of retained frames climbing towards the length of the match.
# Keeping the most recent few is enough: the zone is static, so any window sees it.
MAX_FRAMES = 24
MAX_SAMPLES = 60         # ~2 s of sweep; a longer fit window buys nothing and drifts
FREEZE_TOLERANCE_DEG = 3.0   # wobble a genuinely frozen needle still shows; see freeze_angle
MIN_FIT_FRAMES = 5       # samples needed before a slope is worth acting on
MAX_FIT_RMS_DEG = 8.0    # a fit worse than this is an instrument fault; do not fire on it
MIN_RATE_DEG_S = 120.0   # below this it is not a sweeping check
MAX_RATE_DEG_S = 800.0   # above the Coulrophobia + Hyperfocus ceiling of 609
# Keypress -> pixel. 60.0 is the first value measured ARMED, against the game, under load:
# median 59 ms over six scored landings on 2026-08-15 23:20-23:25, which returned 5 GREAT,
# 1 good, 0 MISS. Every earlier figure came from measure_latency.py, which presses at a
# text field on the host with the detector not running — 72 originally, then 126.5 when
# re-measured idle the same evening the armed run was landing at 59. Prefer the armed
# number; `report_landing` prints one per check, so a session that disagrees says so.
ROUND_TRIP_MS = 60.0
GREAT_FALLBACK_DEG = 10.5  # measured; the simulator's default of 15 is 40% too wide

# Aim this far PAST the middle of the Great band, in the needle's own direction. The two
# ways of being wrong are not symmetric: Great sits at the leading edge of the success
# zone, so overshooting spills into Good — a worse hit, but still a hit — while
# undershooting lands outside the zone entirely and fails the check. Replaying the
# recordings with the round trip deliberately mis-stated by 20 ms shows it: 20 ms late
# cost 13 Greats and zero misses, 20 ms early cost 9 outright misses. A small late bias
# buys margin against the expensive error at the price of margin against the cheap one.
#
# 1.0 deg is where that trade stops paying. Swept over the 15-check unhit set, every value
# from 0 to 2 keeps 15/15 Great at the measured latency; what changes is the tails. At
# +10 ms of latency error, 0 deg keeps 14 Greats, 1.0 keeps 11, 2.0 keeps 6 — so the cost
# of the bias climbs fast, while the benefit against the -20 ms tail is largely bought by
# the first degree (9 misses at 0 deg, 3 at 1.5).
#
# **Set to 0 on 2026-08-16, and VALIDATED over 33 armed fires the same evening.** That
# sweep ran against zero-jitter replays, where the only error is the one you dial in. The
# first real match to record per-check landings said the empirical distribution was already
# late: mean +1.30 deg +/-0.95 over 23 gradeable fires. At 0, two further matches give mean
# **-0.41 +/-0.88 over 33** — indistinguishable from centred — and 30/33 Great against
# 18/23. The shift is -1.71 +/-1.29 against the -1.00 this change predicts: right
# direction, right size. (The Great rate alone is NOT significant at these counts, p ~ 0.16;
# it is the agreement with the predicted shift that carries the result.)
#
# It is NOT free, and the replays say so: at 0 the offline error centres (bias +1.3 -> +0.5
# on the unhit set, worst 2.6 -> 1.6) but `recordings/check_009` stops firing at all —
# "fit too poor (17.4 deg RMS)". An earlier target means an earlier deadline, so the
# tracker must commit a frame or two sooner, on a fit that has not settled. That is the
# same short-fit failure the live match showed, reached from the other side: aim and fit
# quality are coupled, and one no-fire is worse than one good. 12 GREAT + 1 no fire against
# a baseline of 13 GREAT is the price of this test.
#
# That cost did NOT show up live: zero no-fires across the 33 fires at 0, including four
# short fits that all still fired. The offline no-fire looks specific to check_009's
# geometry rather than general.
#
# **Back to 1.0 on 2026-08-17, on 136 pooled armed fires — and the point is that the
# optimum is a plateau, not a value.** Bias only translates the landing distribution, so
# every value can be re-scored against the four matches' measured errors without playing
# again. 0.0 and +1.0 give the *same* tally, 117 Great (86%) and 4 MISS; +1.5 trades two
# Greats for one miss, -1.0 trades three Greats for eight misses. The 33-fire sample that
# argued for 0 was never able to separate 0 from 1.0, and this one says they are equal on
# the two outcomes that matter.
#
# 1.0 wins the tie on the two things the tally cannot see. First, the asymmetry is now
# measured rather than argued from replays: the Great band sits a median 1.0 deg from the
# leading edge of the success zone and 38.0 deg from the trailing edge, and all four misses
# in 136 fires are EARLY (-7.5 to -9.5 deg). Nothing has ever missed late. Second, a later
# target is a later deadline, which is the no-fire risk the paragraph above describes; 1.0
# relieves it instead of spending it. Same Great rate, margin on both failure modes.
#
# Do not re-sweep this against replays. Replay error is ~1.0 deg sigma against 3.8 live, so
# a replay sweep is measuring the wrong distribution — that is how the value got moved on
# thin evidence twice. Re-score the `.jsonl` landings instead.
#
# **2.5 on 2026-08-20, on 654 pooled armed fires, because Nico stated the preference the
# earlier tallies were silently guessing at: a good beats a miss.** Every value above was
# picked by maximising Great and treating good and MISS as the same kind of not-Great. They
# are not. A good costs the bonus; a MISS costs 10% progress and screams. Once misses are
# the thing being minimised the plateau argument above stops applying, because bias trades
# along exactly the axis that separates them.
#
# Re-scored with `tools/rescore_policy.py` over every landings file, lead on the shipped
# burst rule:
#
#     bias 1.0 (was shipped)  531 GREAT (81.2%)   98 good  25 MISS
#     bias 2.0                506 GREAT (77.4%)  132 good  16 MISS
#     bias 2.5                500 GREAT (76.5%)  142 good  12 MISS
#
# Misses halve for ~5 points of Great, and no session in the 22 gets worse on misses — 10
# get better. The 2026-08-20 22:45 match that prompted this went 27G/3M -> 28G/1M, better on
# both, and its three misses were the three shortest round trips of the night (35.7, 22.3,
# 38.1 ms against a 55.2 ms median). Nothing has ever missed late, in 654 fires.
#
# 2.5 and not more because of the `great_width / 4` clamp in `decide`: on the usual 10 deg
# Great that IS 2.5, so this is the largest bias that is actually applied rather than
# clipped, and `rescore_policy.py`'s docstring is right that past the clamp the arithmetic
# stops being able to grade itself. The residual unmeasured cost is the no-fire risk named
# above — 1.5 deg later is ~4.6 ms later at 325 deg/s, well inside one 20-30 ms frame, but
# a re-score cannot see a fire it turns into a no-fire. Watch `NO PRESS` counts.
AIM_BIAS_DEG = 2.5


@dataclass(frozen=True)
class Zone:
    """The drawn success zone, in degrees about the ring. 0 = up, clockwise."""

    great_start: float
    great_end: float
    zone_start: float
    zone_end: float

    @property
    def great_width(self) -> float:
        return (self.great_end - self.great_start) % 360.0

    @property
    def great_mid(self) -> float:
        return (self.great_start + self.great_width / 2.0) % 360.0

    @property
    def zone_width(self) -> float:
        return (self.zone_end - self.zone_start) % 360.0

    @property
    def great_measured(self) -> bool:
        """Did we find a Great band, or just the whole zone lit?

        Two independent tells, because either alone can be fooled: an absolute width no
        real band reaches, and a band filling its own zone. A graded check sits at
        10-11 deg inside a 49 deg zone; the `full white` degenerate case sits at 33-59
        inside 33-60, a ratio of ~1.0 that no drawn check has.
        """

        return (self.great_width <= MAX_GREAT_DEG
                and self.great_width < 0.9 * self.zone_width)


@dataclass(frozen=True)
class Fit:
    """A straight line through needle angle against time."""

    rate_deg_s: float   # signed; negative is counter-clockwise (Madness)
    intercept: float    # unwrapped angle at t = 0
    rms_deg: float
    n: int

    def angle_at(self, t_ms: float) -> float:
        return self.rate_deg_s * t_ms / 1000.0 + self.intercept


@dataclass(frozen=True)
class Sample:
    t_ms: float
    angle: float
    strength: float


@dataclass(frozen=True)
class TrackerState:
    """Everything known about the check in progress. Immutable; `observe` returns a new one."""

    samples: Tuple[Sample, ...] = ()
    frames: Tuple[np.ndarray, ...] = ()
    seen: int = 0           # frames observed in total; `frames` is capped, this is not
    centre: Tuple[float, float] = CENTRE_PRIOR
    ring_r: float = 65.0
    centre_fixed: bool = False
    zone: Optional[Zone] = None
    fired_at_ms: Optional[float] = None


# --- primitives ----------------------------------------------------------------------

def sample_rays(img, cx, cy, radii, angle_step=ANGLE_STEP):
    """Sample `img` along rays from (cx, cy). Angle 0 is up and increases clockwise."""

    angles = np.arange(0.0, 360.0, angle_step)
    theta = np.deg2rad(angles)[:, None]
    xs = np.clip(np.round(cx + radii[None, :] * np.sin(theta)).astype(int), 0, img.shape[1] - 1)
    ys = np.clip(np.round(cy - radii[None, :] * np.cos(theta)).astype(int), 0, img.shape[0] - 1)
    return angles, img[ys, xs]


def needle_angle(bgr, centre=CENTRE_PRIOR):
    """(angle_deg, strength) of the strongest red radial line.

    The needle is red and the success-zone arc is white, so scoring each pixel by
    `R - max(G, B)` isolates one and cancels the other.
    """

    b, g, r = (bgr[:, :, i].astype(np.float32) for i in range(3))
    redness = r - np.maximum(g, b)
    angles, rays = sample_rays(redness, centre[0], centre[1], np.arange(NEEDLE_IN, NEEDLE_OUT),
                               angle_step=0.5)
    profile = rays.mean(axis=1)
    return float(angles[int(np.argmax(profile))]), float(profile.max())


def strength_reference(strengths):
    """The check's own peak needle response, or None if there is nothing to measure.

    The median of the top quartile rather than the maximum: one bright frame should not
    set the bar for the whole check.
    """

    arr = np.asarray(list(strengths), dtype=np.float64)
    if not len(arr):
        return None
    return float(np.median(arr[arr >= np.percentile(arr, 75)]))


def lit_floor(reference, relative_floor=RELATIVE_FLOOR):
    """The strength above which a reading is a drawn needle rather than a leftover.

    A fixed floor is not enough, and neither is the classifier: it labels the check-free
    frames either side of a real check `full black (out)` rather than `None`, so they
    survive a class filter, and the brightest stray red pixel in them supplies a
    meaningless angle. A drawn needle scores 70-150; those strays reach 20-45, which
    clears the absolute floor of 20 outright. Judging against the check's own peak is
    what separates them.
    """

    if reference is None:
        return MIN_NEEDLE_STRENGTH
    return max(MIN_NEEDLE_STRENGTH, relative_floor * reference)


def _longest_true(mask):
    """(start, stop) of the longest contiguous run of True. Never wraps."""

    best, run, start = (0, 0), 0, 0
    for i, on in enumerate([*mask, False]):
        if on:
            start = i if run == 0 else start
            run += 1
        else:
            best, run = max(best, (run, start)), 0
    length, start = best
    return start, start + length


def lit_span(samples, relative_floor=RELATIVE_FLOOR):
    """(start, stop) of the contiguous block where a needle is actually drawn.

    Stray red outside a check drifts the OPPOSITE way to the real sweep, which was once
    enough to reverse an inferred direction, and here it dragged a whole-check rate from
    325 to 293 deg/s. See `lit_floor` for why the threshold is relative.
    """

    strengths = [s.strength for s in samples]
    if not strengths:
        return 0, 0
    floor = lit_floor(strength_reference(strengths), relative_floor)
    return _longest_true([s >= floor for s in strengths])


def trim_frozen_tail(samples):
    """Drop samples after the needle stops dead, and return the kept prefix.

    A successful hit freezes the needle at the hit position. Those frames are not part of
    the sweep and drag a straight-line fit badly.

    Two things this must get right, both learned from wrong answers:

      * The freeze is invisible to a frame-equality test. The stream encoder keeps
        jittering pixels while the angle sits at exactly the same value, so equality never
        fires. Detect it by the needle failing to ADVANCE instead.
      * "Failing to advance" cannot mean "did not move at all". Angular quantisation makes
        a frozen needle wobble by half a degree either way, which resets a strict stall
        counter and lets a 200 ms frozen tail through — that alone pulled one check's
        fitted rate from 325 to 293 deg/s and turned a good press into a scored miss.
        The bar is a fraction of the check's own median step.

    Advance is signed by the check's own direction: the Doctor's Madness reverses a check,
    and against a hardcoded clockwise assumption every frame of one reads as stalled, so
    the whole check is discarded on its second frame — silently, and precisely the rarest
    data there is.
    """

    if len(samples) < 3:
        return samples
    steps = [(b.angle - a.angle + 540) % 360 - 180 for a, b in zip(samples, samples[1:])]
    sign = 1.0 if float(np.median(steps)) >= 0 else -1.0
    bar = max(2.0, 0.4 * float(np.median(np.abs(steps))))

    kept, stalled = [samples[0]], 0
    for prev, cur in zip(samples, samples[1:]):
        advanced = sign * ((cur.angle - prev.angle + 540) % 360 - 180)
        stalled = stalled + 1 if advanced < bar else 0
        if stalled >= 2:
            return tuple(kept[:-1])  # the first stalled frame is already frozen too
        kept.append(cur)
    return tuple(kept)


def static_image(frames, max_frames=STATIC_FRAMES):
    """Per-pixel median whiteness with the needle masked out — the check's static UI.

    The needle moves and the zone does not, so a median over frames erases the needle
    where it has moved on and the redness mask erases it where it has not.

    Only `max_frames` evenly spaced frames are used. The median over eight is already
    clean, and the cost is linear: forty frames is 41 ms, which is a frame and a half of
    the live budget spent to sharpen an image that was already sharp.
    """

    if len(frames) > max_frames:
        picks = np.linspace(0, len(frames) - 1, max_frames).round().astype(int)
        frames = [frames[i] for i in picks]

    stack = np.stack(frames).astype(np.float32)
    b, g, r = stack[..., 0], stack[..., 1], stack[..., 2]
    whiteness = np.minimum(np.minimum(r, g), b)
    masked = np.where(r - np.maximum(g, b) > NEEDLE_REDNESS, np.nan, whiteness)
    masked[:, np.all(np.isnan(masked), axis=0)] = 0.0  # pixels the needle never left
    return np.nanmedian(masked, axis=0)


def refine_centre(static, prior=CENTRE_PRIOR, span=CENTRE_SPAN, step=CENTRE_STEP,
                  search_angle_step=SEARCH_ANGLE_STEP, search_ring_step=SEARCH_RING_STEP):
    """Centre that makes the base ring land at one radius for every angle.

    `cv2.HoughCircles` is only accurate to about +/-3 px here, and an origin that far off
    shifts apparent radius enough to move the zone clean out of the radial window — it
    made the arc vanish on 9 of 22 checks while leaving plausible numbers on the rest.
    The ring is a full circle, so at the true centre its angle-median radial profile is a
    tall narrow spike; off centre it smears across radii and the peak drops. Maximising
    that peak pins the centre to a quarter pixel.

    The grid search deliberately samples coarsely — 90 rays at 1 px of radius rather than
    360 at a quarter pixel. What is being maximised is a median over a full circle, which
    a quarter of the rays estimates just as well, and the fine sampling cost 60 ms per
    check: two dropped frames in the middle of the sweep, to move the answer by nothing.
    The winning centre is then profiled finely to read the ring radius off.
    """

    coarse = np.arange(RING_SEARCH[0], RING_SEARCH[1], search_ring_step)
    grid = np.arange(-span, span + 1e-9, step)
    best = (-1.0, prior[0], prior[1])
    for dx in grid:
        for dy in grid:
            cx, cy = prior[0] + dx, prior[1] + dy
            rays = sample_rays(static, cx, cy, coarse, angle_step=search_angle_step)[1]
            peak = float(np.median(rays, axis=0).max())
            if peak > best[0]:
                best = (peak, cx, cy)
    peak, cx, cy = best
    if peak < MIN_CENTRE_PEAK:
        return prior[0], prior[1], 65.0, peak

    fine = np.arange(RING_SEARCH[0], RING_SEARCH[1], RING_STEP)
    profile = np.median(sample_rays(static, cx, cy, fine)[1], axis=0)
    return cx, cy, float(fine[int(np.argmax(profile))]), peak


def _close_gaps(mask, angle_step, width_deg=CLOSE_DEG):
    """Fill False gaps shorter than width_deg, wrapping around 360."""

    n = len(mask)
    span = int(round(width_deg / angle_step))
    out = mask.copy()
    i = 0
    while i < 2 * n:  # two laps so a gap straddling 0 deg is seen whole
        if not out[i % n]:
            j = i
            while j < 2 * n and not out[j % n]:
                j += 1
            if j - i <= span and j < 2 * n:
                for k in range(i, j):
                    out[k % n] = True
            i = j
        else:
            i += 1
    return out


def _longest_run(mask, angle_step, min_deg=MIN_RUN_DEG, circular=True):
    """Longest run of True as (start_index, length), or None if too short.

    `circular` must be False when the mask is a slice of a larger circle rather than the
    whole of it. Searching the zone's own samples circularly lets a few lit samples at the
    trailing end join the solid band at the leading end into one phantom run, which
    reports the Great band roughly a zone-width late — landing the press ~50 deg early on
    3 of 13 checks here. Only the full-circle angular profile may wrap.
    """

    n = len(mask)
    if not mask.any():
        return None
    if mask.all():
        return (0, n)

    best_len = best_start = cur_len = cur_start = 0
    # Begin at a False so no run is split by the wrap; irrelevant when not circular.
    start = int(np.argmax(~mask)) if circular else 0
    for k in range(n):
        i = (start + k) % n if circular else k
        if mask[i]:
            if cur_len == 0:
                cur_start = i
            cur_len += 1
            if cur_len > best_len:
                best_len, best_start = cur_len, cur_start
        else:
            cur_len = 0

    if best_len * angle_step < min_deg:
        return None
    return (best_start, best_len)


def _max_contiguous(row):
    """Longest contiguous True run within one angle's radial samples, in pixels."""

    best = cur = 0
    for v in row:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best * RADIUS_STEP


def find_zone(static, centre, ring_r, angle_step=ANGLE_STEP):
    """Locate the drawn success zone and the solid Great band inside it.

    Great and Good differ in RADIAL THICKNESS rather than brightness: Good is two thin
    rails with a dark gap between them (~2.2 px of lit radius), Great is a filled band
    (~8.5 px). Subtracting the per-radius median over angle removes the base ring, which
    is present at every angle.

    Returns None when no zone is drawn — Merciless Storm renders an unfilled outline with
    no solid band at all, and firing on a guessed position there is worse than not firing.
    """

    radii = np.arange(ring_r + WINDOW_IN, ring_r + WINDOW_OUT, RADIUS_STEP)
    angles, polar = sample_rays(static, centre[0], centre[1], radii, angle_step=angle_step)
    resid = polar - np.median(polar, axis=0, keepdims=True)
    hot = resid > HOT

    thickness = hot.sum(axis=1) * RADIUS_STEP
    runs = np.array([_max_contiguous(hot[i]) for i in range(hot.shape[0])])

    found = _longest_run(_close_gaps(thickness >= ZONE_THICK_PX, angle_step), angle_step)
    if found is None:
        return None
    z_start, z_len = found
    idx = [(z_start + k) % len(angles) for k in range(z_len)]

    great = _longest_run(runs[idx] >= FILL_RUN_PX, angle_step, circular=False)
    if great is None or great[1] * angle_step < MIN_GREAT_DEG:
        return None
    g_start, g_len = great

    return Zone(
        great_start=float(angles[idx[g_start]]),
        great_end=float((angles[idx[g_start]] + g_len * angle_step) % 360.0),
        zone_start=float(angles[idx[0]]),
        zone_end=float((angles[idx[0]] + z_len * angle_step) % 360.0),
    )


def fit_sweep(samples):
    """Straight-line fit of angle against time, or None if the samples cannot support one.

    Angles are unwrapped before fitting so a sweep through 360 does not read as a jump
    back to zero. The slope keeps its sign: negative is a counter-clockwise Madness check.
    """

    if len(samples) < MIN_FIT_FRAMES:
        return None
    t = np.array([s.t_ms for s in samples])
    angles = np.rad2deg(np.unwrap(np.deg2rad([s.angle for s in samples])))
    slope, intercept = np.polyfit(t, angles, 1)
    rms = float(np.sqrt(np.mean((angles - (slope * t + intercept)) ** 2)))
    return Fit(rate_deg_s=float(slope * 1000.0), intercept=float(intercept),
               rms_deg=rms, n=len(samples))


def time_to_angle(fit, target_deg, now_ms):
    """When the fitted needle next reaches `target_deg`, in ms. None if it never will.

    Both directions are handled by measuring the remaining travel in the needle's own
    direction of rotation and dividing by the rate.
    """

    if fit is None or abs(fit.rate_deg_s) < 1e-6:
        return None
    here = fit.angle_at(now_ms)
    if fit.rate_deg_s > 0:
        remaining = (target_deg - here) % 360.0
    else:
        remaining = (here - target_deg) % 360.0
    return now_ms + remaining / abs(fit.rate_deg_s) * 1000.0


# --- the tracker ---------------------------------------------------------------------

def observe(state, frame, t_ms):
    """Fold one frame into the state. Returns a new TrackerState; never mutates."""

    angle, strength = needle_angle(frame, state.centre)
    new = replace(
        state,
        samples=(state.samples + (Sample(t_ms, angle, strength),))[-MAX_SAMPLES:],
        frames=(state.frames + (frame,))[-MAX_FRAMES:],
        seen=state.seen + 1,
    )

    # The centre is refined ONCE, from the first frames that show the static UI, and then
    # reused. Re-locating per frame puts the locator's own jitter straight into the angle:
    # measured per frame the fast-build checks fit at 7.9 deg RMS, and at 3.9 with one
    # centre per check. It is the same data.
    if not new.centre_fixed and new.seen >= MIN_ZONE_FRAMES:
        static = static_image(new.frames)
        cx, cy, ring_r, _ = refine_centre(static)
        new = replace(new, centre=(cx, cy), ring_r=ring_r, centre_fixed=True,
                      zone=find_zone(static, (cx, cy), ring_r))
        # The angles so far were measured about the prior, up to 3 px away. Re-measure
        # them about the refined centre rather than fitting a line through two conventions.
        # Pair from the END: `samples` and `frames` are capped at different lengths, so
        # zipping from the front would silently pair a sample with the wrong frame once
        # either cap bites.
        redone = tuple(Sample(s.t_ms, *needle_angle(f, new.centre))
                       for s, f in zip(new.samples[-len(new.frames):], new.frames))
        new = replace(new, samples=new.samples[:-len(redone)] + redone)
    elif new.zone is None and new.seen % ZONE_RETRY_EVERY == 0:
        # The zone can be missing early — the needle sitting across it, a dropped frame —
        # and appear once more static frames accumulate. Retrying costs ~9 ms of a ~29 ms
        # frame, so retry periodically rather than every frame.
        new = replace(new, zone=find_zone(static_image(new.frames), new.centre, new.ring_r))

    return new


@dataclass(frozen=True)
class Decision:
    """What the tracker wants done, and why — the 'why' so it can be logged and audited."""

    press_at_ms: Optional[float] = None
    reason: str = "waiting"
    fit: Optional[Fit] = None
    target_deg: Optional[float] = None
    lands_at_ms: Optional[float] = None

    # True when a real needle is sweeping but the press cannot be scheduled — no Great
    # band drawn, or the band already passed. Those are the cases where reacting to the
    # classifier is the right floor. It is deliberately FALSE when the fit itself is
    # unconvincing, because "the model is confident and the needle is not moving" is what
    # a menu looks like: a red-ringed perk icon in the loadout classifies at 1.000.
    may_react: bool = False


def decide(state, now_ms, round_trip_ms=ROUND_TRIP_MS):
    """Should we press, and when? Pure — the caller owns the clock and the keyboard."""

    if state.fired_at_ms is not None:
        return Decision(reason="already fired")

    fit = fit_sweep(state.samples)
    if fit is None:
        return Decision(reason=f"only {len(state.samples)} samples")
    if fit.rms_deg > MAX_FIT_RMS_DEG:
        return Decision(reason=f"fit too poor ({fit.rms_deg:.1f} deg RMS)", fit=fit)

    rate = abs(fit.rate_deg_s)
    if not (MIN_RATE_DEG_S <= rate <= MAX_RATE_DEG_S):
        return Decision(reason=f"rate {fit.rate_deg_s:.0f} deg/s out of range", fit=fit)
    if state.zone is None:
        return Decision(reason="no zone drawn yet", fit=fit, may_react=True)

    # Never bias past the band's own trailing edge: on a narrow Great — Unnerving Presence
    # and Overcharge both shrink the zone — a fixed offset would aim clean out of it.
    bias = min(AIM_BIAS_DEG, state.zone.great_width / 4.0)
    target = (state.zone.great_mid + bias * (1.0 if fit.rate_deg_s > 0 else -1.0)) % 360.0
    lands = time_to_angle(fit, target, now_ms)
    press_at = lands - round_trip_ms

    # A whole revolution of waiting means the needle has already passed Great. Discrete
    # checks end after one sweep, so there is no second crossing to aim at.
    if (lands - now_ms) > 360.0 / rate * 1000.0 * 0.95:
        return Decision(reason="Great already passed", fit=fit, target_deg=target,
                        may_react=True)
    if press_at < now_ms:
        return Decision(reason=f"too late by {now_ms - press_at:.0f} ms", fit=fit,
                        target_deg=target, lands_at_ms=lands, may_react=True)

    return Decision(press_at_ms=press_at, reason="scheduled", fit=fit,
                    target_deg=target, lands_at_ms=lands)


def mark_fired(state, t_ms):
    """Record that the key has gone down, so `decide` cannot schedule a second press.

    The live loop drops the tracker after firing, which makes this redundant there — but
    a safety property that depends on every caller remembering to do something is not a
    safety property. Firing twice into one check would land the second press outside the
    zone and fail a check the first press had already won.
    """

    return replace(state, fired_at_ms=t_ms)


def freeze_angle(angles, tolerance=FREEZE_TOLERANCE_DEG, window=3):
    """The angle a needle has settled at, or None if it is still sweeping.

    A press that connects stops the needle dead; a press that misses does not stop it at
    all. Reading the last angle either way would report a confident landing for a position
    the press had nothing to do with, so the last few reads have to agree before any
    verdict is allowed. The tolerance is the wobble a genuinely frozen needle still shows
    under angular quantisation, not zero.
    """

    if len(angles) < window:
        return None
    tail = angles[-window:]
    spread = max(abs((b - a + 540.0) % 360.0 - 180.0) for a in tail for b in tail)
    return tail[-1] if spread <= tolerance else None


def freeze_onset(readings, tolerance=FREEZE_TOLERANCE_DEG, window=3):
    """When the freeze first became visible, from (timestamp, angle) reads. None if sweeping.

    `freeze_angle` answers *where* the needle stopped; this answers *when* we could see it
    stop. The gap between the press going out and that moment is the closed-loop round
    trip, measured through the real pipeline, under real armed-run load, once per check.
    `measure_latency.py` reports the same quantity but idle, in its own process, against a
    text field on the host desktop — so it has never been able to see load or the game.

    Walks BACKWARDS from the settled angle. Walking forward would stop at the first read
    matching that angle, and a sweeping needle passes through its eventual resting place
    on an earlier revolution — timing the fly-past instead of the freeze, and reporting a
    round trip shorter than the truth. Short is the flattering direction, which is exactly
    how this project's previous measurement errors survived.

    Resolution is the grab interval, and the freeze can only be seen on the next grab
    after it happens, so the result is late by up to one interval.
    """

    settled = freeze_angle([a for _, a in readings], tolerance=tolerance, window=window)
    if settled is None:
        return None

    onset = None
    for t, angle in reversed(readings):
        if abs((angle - settled + 540.0) % 360.0 - 180.0) > tolerance:
            break
        onset = t
    return onset


def score_freeze(zone, freeze_deg):
    """Grade a press from where the needle stopped: 'GREAT', 'good' or 'MISS'.

    A successful hit freezes the needle at the hit position, so the frozen angle IS the
    landing, read off the same frame as the zone it is scored against. That turns the bot
    into its own instrument: it can report per check whether it actually hit Great,
    instead of the result being inferred from a Great/Good tally at the end of a match.
    """

    if zone is None or freeze_deg is None:
        return "unknown", None
    in_zone = (freeze_deg - zone.zone_start) % 360.0 <= zone.zone_width
    if not zone.great_measured:
        # The press landed, and we can say whether it landed in the zone — but the Great
        # band was never measured on this check, so calling it Great would be inventing
        # the result. `ungraded` keeps it out of the tally and in the error spread.
        verdict = "ungraded" if in_zone else "MISS"
    elif (freeze_deg - zone.great_start) % 360.0 <= zone.great_width:
        verdict = "GREAT"
    elif in_zone:
        verdict = "good"
    else:
        verdict = "MISS"
    return verdict, (freeze_deg - zone.great_mid + 540.0) % 360.0 - 180.0


# --- reading the freeze watch ---------------------------------------------------------

@dataclass(frozen=True)
class Reading:
    """One grab taken while watching for the needle to stop. `t` is monotonic seconds."""

    t: float
    angle: float
    strength: float


@dataclass(frozen=True)
class Watch:
    """What the freeze watch saw, and how much of it was a drawn needle.

    `outcome` is one of:

      * `frozen`   — the needle stopped; `angle` is where and `onset` is when we first saw it.
      * `sweeping` — a needle was drawn and never stopped. With a dark tail that means the
        check ran out its sweep and vanished, i.e. the press never reached the game;
        without one, the watch simply ended first, or this is Merciless Storm.
      * `dark`     — nothing above the floor was ever drawn. The check was already gone.
      * `no reads` — too few grabs to judge at all.

    `dark_tail` is what makes the first two of those distinguishable, and distinguishing
    them is the whole point: both used to print the same line.
    """

    outcome: str
    angle: Optional[float] = None
    onset: Optional[float] = None
    lit: int = 0
    reads: int = 0
    dark_tail: int = 0
    floor: float = MIN_NEEDLE_STRENGTH


def read_watch(readings, reference=None, tolerance=FREEZE_TOLERANCE_DEG, window=3):
    """Decide what a series of post-press grabs shows. Pure; the caller owns the clock.

    Judge the CONTIGUOUS LIT BLOCK, not the tail of everything read. A press that connects
    freezes the needle and the check then leaves the screen well inside the watch window,
    so the last few grabs are stray red at 20-45 strength with meaningless, jittering
    angles. Reading the tail of all of them therefore refused a verdict for a landing that
    was perfect, and printed the same message a press that never arrived produces — six of
    nine armed fires on 2026-08-15 came back that way, and were counted as lost presses.
    """

    readings = tuple(readings)
    floor = lit_floor(reference)
    if len(readings) < window:
        return Watch("no reads", reads=len(readings), floor=floor)

    lit = [r.strength >= floor for r in readings]
    dark_tail = next((i for i, on in enumerate(reversed(lit)) if on), len(lit))
    start, stop = _longest_true(lit)
    block = readings[start:stop]
    if len(block) < window:
        return Watch("dark", lit=len(block), reads=len(readings), dark_tail=dark_tail,
                     floor=floor)

    settled = freeze_angle([r.angle for r in block], tolerance=tolerance, window=window)
    if settled is None:
        return Watch("sweeping", lit=len(block), reads=len(readings), dark_tail=dark_tail,
                     floor=floor)

    onset = freeze_onset([(r.t, r.angle) for r in block], tolerance=tolerance, window=window)
    return Watch("frozen", angle=settled, onset=onset, lit=len(block),
                 reads=len(readings), dark_tail=dark_tail, floor=floor)
