"""See skill checks the 224 centre crop never contains — the Doctor's Madness checks.

The gap this closes
-------------------
Madness displaces a skill check from dead centre. The production path grabs a fixed 224
centre crop, so a displaced check is never captured and therefore never classified: a
silent miss that no lead, aim or model change can reach. Measured on one recorded Doctor
match (`frames/session_20260829_154535`, 34330 frames, `tools/scan_frames.py`): **nine
distinct off-centre checks, all nine confirmed by eye, zero false positives — and six of
them are SNAP OUT OF IT**, the Madness-cure action that exists only against a Doctor and
that the bot has never once seen.

How it works
------------
1. Grab a 672x672 box instead of the 224 crop, positioned at the crop's origin plus
   `WIDE_OFFSET`. That box contains the production crop exactly, so the centre 224 slice
   of the wide grab IS today's frame, bit for bit, from the same instant.
2. If the centre slice shows no check, sweep the box for the check's ring with a
   half-scale Hough pass.
3. Re-crop 224 from the wide box about the ring it found, placing the ring where a
   centred check puts it. Everything downstream — classifier and tracker — then sees the
   framing it was built for.

Why a ring sweep rather than tiling
-----------------------------------
Tiling the box classifies N fixed positions per frame. Measured on this machine at
2.35 ms per 224 inference, the eight observed positions cost **18.8 ms on every frame**,
against a live loop that runs at ~29 ms — it would take the loop to ~21 fps and degrade
the velocity fit for every check, including the centred ones that already work.

The ring sweep costs **0.75 ms**, found the ring on **9 of 9** confirmed off-centre
checks, and fires on 14% of quiet frames — each false fire costing one extra inference, so
1.11 inferences per frame amortised. End to end the frame goes from 2.59 ms to **4.67 ms**
of work, and the wider grab adds ~0.4 ms on top (mss is nearly flat in region size once
warm: 224 px and 672 px both measure 18-19 ms per grab under sustained load). About
2.5 ms on a ~29 ms loop, against tiling's 18.8.

It also covers the whole box rather than eight positions observed in a single match, so it
does not inherit that n=1.

The risk is asymmetric in our favour: the centre path never consults this module, so a
ring the sweep misses is a check that would have been missed anyway. The floor is the
status quo — but only because `look` also DROPS a lock the moment its own window goes
empty and the centre does not. Without that the floor does not hold, and Merciless Storm
plus Doctor is what proved it; see the rule in `look`.

Why the crop is re-centred rather than handed over as a tile
------------------------------------------------------------
A fixed grid tile puts the check off-centre *within the tile* by up to half a stride.
Measured over the nine, the ring sits **8 to 88 px** from (112, 102) inside its own tile.
`needle_tracker.refine_centre` searches +-3 px about that prior, so it fails, the angle is
then measured about a point in the background, and the check is rejected **silently** —
the failure mode NOTES-local.md warns about at *tile centre vs check centre*. Re-centring
on the located ring removes the problem by construction instead of tuning around it.

Merciless Storm plus Doctor is the one state where this can make things worse rather than
just better, because Storm draws no solid Great band, so `decide` stands down and
`autorun.py` presses on the classifier's cue instead — and a reactive press lands ~25 deg
late against a ~23 deg zone. Wide capture makes off-centre Storm revolutions visible, so
it enlarges the population of those presses. Measured on `merciless-storm-madness2`, the
only Doctor-plus-Storm footage here, with `--framing wide`: the wide path's verdict on
every one of the 9 CENTRED revolutions is identical to the centre crop's, and of the 4
off-centre ones it adds a single press — a predictive fire on a real repair check with a
measured band, not a Storm outline. So the enlargement is real in principle and measured
at zero here, on n = 4. The open question it presses on is `NOTES-local.md`'s, not this
module's: whether to suppress the reactive fallback when no zone is drawn.

Measured both ways over the nine, with `tools/replay_centre_crop.py --frames`: the
production centre crop presses **1 of 9**, and that one is a single-frame reactive false
positive rather than an aimed press. A fixed grid tile classifies the check on 30-40 frames
and still presses **0 of 4** tried — the classifier sees it, the tracker rejects it,
nothing is logged. The re-centred crop presses **9 of 9**, all predictive, all aimed inside
a measured Great band. On the 58 centred checks of the same match the new path's decisions
are byte-identical to the old one's.

Geometry note
-------------
Everything here scales with content height the way `centre_crop_region` does, and the box
origin is derived FROM that function rather than recomputed, so the two cannot drift
apart. A centred check's ring sits at (112, 102) in the 224 crop, not (112, 112) —
confirmed here over 1731 ring locations in one recorded match, at exactly 10.00 px above
the crop centre with 1.8 px of scatter.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np

from dbd.utils.needle_tracker import CENTRE_PRIOR as RING_IN_CROP
from dbd.utils.monitoring_window import (
    TRAINING_REFERENCE_CROP,
    TRAINING_REFERENCE_HEIGHT,
    Monitoring_window,
    centre_crop_region,
)

# The box is sized and placed by where the RING can sit, not by where a tile origin can.
# A check is only usable once a whole 224 crop can be centred on it, so a box of side S
# covers only S-224 px of ring positions — 560 covers 336. The nine confirmed off-centre
# checks put their rings across 294 px in x and 200 in y, which 336 does contain, but with
# 21 px to spare on each side in x. Measured rather than reasoned: at 560 x (-64, -204) the
# check at frame 18475 sits 19 px past the left edge of what can be centred, its crop is
# clamped, the tracker's prior is 19 px wrong, and the check is dropped — 8 of 9 rather
# than 9 of 9. Sizing the box off tile origins is what hid that: a tile origin only has to
# CONTAIN the check, and containing it is not enough.
#
# 672 covers 448 px of ring positions and is centred on the observed spread, so every one
# of the nine sits 77 px or more inside the edge. The extra 112 px costs ~0.3 ms of sweep
# and nothing measurable in capture (a full 1920x1080 grab is only +4.7 ms over a 224 one).
# n = 1 match; a second Doctor match re-scanned is what would confirm the spread.
WIDE_BOX = 672.0                  # box side at training scale (1080p content)
WIDE_OFFSET = (-161.0, -202.0)    # box origin relative to the 224 crop's origin

# Where a centred check's ring sits in the 224 crop — not the crop's centre. Taken from
# `needle_tracker` rather than restated, because a crop placed to one constant and an
# angle measured about another is a 10 px error that reads as non-constant velocity.

# Ring sweep. Radii are quoted at training scale for a full-size box and halved by the
# downscale; a check ring measures 62-66 px across every frame inspected here.
SWEEP_DOWNSCALE = 2
SWEEP_BLUR = 3
SWEEP_MIN_RADIUS = 27
SWEEP_MAX_RADIUS = 57
SWEEP_PARAM1 = 100
SWEEP_PARAM2 = 30
SWEEP_MIN_DIST = 50



@dataclass(frozen=True)
class WideGeometry:
    """Resolved capture geometry for one window. Screen coordinates, plus box-local ones."""

    region: dict                  # mss region for the wide box
    crop_region: dict             # mss region the production path would have used
    centre: Tuple[int, int]       # the production crop's origin INSIDE the box
    side: int                     # box side, screen px
    crop_side: int                # production crop side, screen px
    scale: float                  # content height / 1080
    clamped: bool                 # box hit the content edge and had to be moved

    def describe(self):
        return {
            "wide_region": dict(self.region),
            "crop_region": dict(self.crop_region),
            "centre_in_box": list(self.centre),
            "side": self.side,
            "crop_side": self.crop_side,
            "scale": round(self.scale, 3),
            "clamped": self.clamped,
        }


def wide_geometry(content, crop_size=TRAINING_REFERENCE_CROP, zoom=1.0,
                  box=WIDE_BOX, offset=WIDE_OFFSET):
    """The wide box for a content rect, and where the production crop sits inside it.

    The crop region comes from `centre_crop_region`, never from a second computation of
    the same thing — the whole design rests on the centre slice of this box being the
    frame the production path would have grabbed, so the two origins must be one origin.

    The box is clamped into the content rect. Clamping moves the box but never the crop,
    so `centre` still points at the production crop wherever the box ends up.
    """

    crop = centre_crop_region(content, crop_size, zoom)
    scale = content["height"] / TRAINING_REFERENCE_HEIGHT
    side = max(int(round(box * scale)), crop["width"])

    left = crop["left"] + int(round(offset[0] * scale))
    top = crop["top"] + int(round(offset[1] * scale))

    # Never grab outside the game: past the content edge is a letterbox bar or another
    # application, and either one is background the sweep would have to reject.
    lo_x, hi_x = content["left"], content["left"] + content["width"] - side
    lo_y, hi_y = content["top"], content["top"] + content["height"] - side
    clamped_left = min(max(left, lo_x), hi_x) if hi_x >= lo_x else lo_x
    clamped_top = min(max(top, lo_y), hi_y) if hi_y >= lo_y else lo_y
    clamped = (clamped_left, clamped_top) != (left, top)

    return WideGeometry(
        region={"left": clamped_left, "top": clamped_top, "width": side, "height": side},
        crop_region=dict(crop),
        centre=(crop["left"] - clamped_left, crop["top"] - clamped_top),
        side=side,
        crop_side=crop["width"],
        scale=scale,
        clamped=clamped,
    )


def centre_slice(wide, geometry):
    """The production 224 crop, sliced out of the wide grab. Same pixels, same instant."""

    x, y = geometry.centre
    return wide[y:y + geometry.crop_side, x:x + geometry.crop_side]


def sweep_rings(wide_bgr, scale=1.0):
    """Every check-sized ring in the wide box, as (cx, cy, r) in box pixels.

    Half scale on purpose: the ring is tens of pixels across, so the downscale costs no
    recall and buys a 4x smaller Hough accumulator. Full-resolution Hough is 229 ms and
    finds the same circles.
    """

    small = cv2.resize(wide_bgr, None, fx=1.0 / SWEEP_DOWNSCALE, fy=1.0 / SWEEP_DOWNSCALE,
                       interpolation=cv2.INTER_AREA)
    grey = cv2.medianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), SWEEP_BLUR)
    found = cv2.HoughCircles(
        grey, cv2.HOUGH_GRADIENT, dp=1, minDist=int(SWEEP_MIN_DIST * scale),
        param1=SWEEP_PARAM1, param2=SWEEP_PARAM2,
        minRadius=int(SWEEP_MIN_RADIUS * scale), maxRadius=int(SWEEP_MAX_RADIUS * scale))
    if found is None:
        return ()
    d = float(SWEEP_DOWNSCALE)
    return tuple((float(c[0]) * d, float(c[1]) * d, float(c[2]) * d) for c in found[0])


def pick_ring(rings, geometry, ring_in_crop=RING_IN_CROP):
    """The ring most worth a second inference, or None.

    Nearest to where a centred check would sit, because Madness displacement is small —
    8 to 160 px over the nine confirmed checks — so a circle far out in the box is more
    likely to be UI than a check.

    Rings the centre crop already covers are deliberately NOT excluded. The sweep only
    runs on a frame the centre crop called `None`, so there is no check there to
    double-count, and a check displaced just far enough to be cut in half by the crop is
    exactly the case a distance filter would throw away.
    """

    if not rings:
        return None
    cx0 = geometry.centre[0] + ring_in_crop[0] * geometry.scale
    cy0 = geometry.centre[1] + ring_in_crop[1] * geometry.scale
    return min(rings, key=lambda r: float(np.hypot(r[0] - cx0, r[1] - cy0)))


@dataclass(frozen=True)
class OffCentreCrop:
    """A window into the wide box, held for the whole of one off-centre check.

    Located ONCE, then reused. Re-locating per frame puts the locator's own jitter into
    the crop origin and from there into the angle — the same reason `needle_tracker` fixes
    its ring centre once per check rather than every frame (7.9 deg RMS per frame against
    3.9 with one centre).
    """

    origin: Tuple[int, int]         # crop origin inside the wide box
    prior: Tuple[float, float]      # where the ring landed, in MODEL-scale crop pixels
    ring_r: float                   # ring radius in box pixels, for sanity checks


def crop_at(wide, origin, geometry):
    """The held window, at the size the model's input is pinned to. BGR in, BGR out."""

    x, y = origin
    return to_model_size(wide[y:y + geometry.crop_side, x:x + geometry.crop_side])


def locate_offcentre(wide_bgr, geometry, ring_in_crop=RING_IN_CROP):
    """Find a check's ring in the wide box and fix a crop window on it, or None."""

    ring = pick_ring(sweep_rings(wide_bgr, geometry.scale), geometry, ring_in_crop)
    if ring is None:
        return None

    side = geometry.crop_side
    x = int(round(ring[0] - ring_in_crop[0] * geometry.scale))
    y = int(round(ring[1] - ring_in_crop[1] * geometry.scale))
    x = min(max(x, 0), max(wide_bgr.shape[1] - side, 0))
    y = min(max(y, 0), max(wide_bgr.shape[0] - side, 0))

    return OffCentreCrop(
        origin=(x, y),
        prior=scale_point((ring[0] - x, ring[1] - y), geometry),
        ring_r=ring[2],
    )


NONE_CLASS = 0   # the classifier's "no skill check here"


@dataclass(frozen=True)
class Look:
    """One frame's worth of looking: what was seen, in which crop, and what it cost."""

    pred: int
    desc: str
    probs: dict
    should_hit: bool
    crop: np.ndarray                       # BGR at model size — what the tracker observes
    origin: Tuple[int, int]                # where that crop was taken from, in the box
    held: Optional[OffCentreCrop]          # set once an off-centre check is locked on
    inferences: int                        # classifier calls made, for the frame budget

    @property
    def prior(self):
        """The tracker's ring prior for this crop: the located ring, or the constant."""

        return RING_IN_CROP if self.held is None else self.held.prior


def look(predict, wide, geometry, held=None):
    """Look at one wide grab, centre first. Returns a `Look`.

    `predict` takes a BGR crop at model size and returns `AI_model.predict`'s tuple,
    `(pred, desc, probs, should_hit)` — the same four values, so nothing is dropped on the
    way through and the caller still has the confidence it logs.

    The centre crop is always tried first and is never gated on the sweep, so the path
    that already works pays one extra Hough pass and nothing else. Only when the centre is
    empty does the box get swept.

    **The crop is locked only once it CLASSIFIES as a check, never on the ring alone.**
    The sweep fires on 9.3% of quiet frames, and locking on the first circle it likes
    hands the tracker a window on background for the rest of the check — measured: it cost
    frame 21925 of the reference session, which fires once the lock waits for the model.
    Once locked the window is held, because re-locating per frame feeds the locator's own
    jitter into the angle.

    **Callers must restart the tracker whenever `origin` changes.** A displaced check is
    often visible at the edge of the centre crop for its first few frames, so the centre
    path claims it, measures the angle about a point the ring is nowhere near, and only
    then does the sweep take over. Carrying those samples across the switch is what cost
    frame 18475 of the reference session: 5 junk angles held the fit at 55 deg RMS for the
    whole check, and it was never pressed despite being tracked for 37 frames.

    The sweep is consulted only when the centre crop is empty, deliberately. Choosing the
    crop from the ring instead would divert a genuinely centred check whenever Hough
    prefers a decoy circle, and over the reference match the picked ring is more than
    10 px from the centred position on about a quarter of flagged frames. Centre-first has
    no such failure: its floor is exactly today's behaviour.
    """

    if held is not None:
        crop = crop_at(wide, held.origin, geometry)
        pred, desc, probs, should_hit = predict(crop)
        if pred != NONE_CLASS:
            return Look(pred, desc, probs, should_hit, crop, held.origin, held, 1)

        # The held window went empty. Before spending another frame on it, make sure the
        # centre is not showing a check — a LOCK MUST NEVER HIDE THE CENTRE PATH, which is
        # the guarantee the whole design rests on. Without this the lock is unconditional
        # for the life of the track, and a false ring that classifies once suppresses the
        # working path entirely: measured on `merciless-storm-madness2`, check_004 is a
        # dead-centre check that the sweep stole on its first frame with a ring at the
        # box's left edge, then sat on for all 32 remaining frames.
        centre = to_model_size(centre_slice(wide, geometry))
        c_pred, c_desc, c_probs, c_hit = predict(centre)
        if c_pred != NONE_CLASS:
            return Look(c_pred, c_desc, c_probs, c_hit, centre, geometry.centre, None, 2)

        # Both empty. Keep the lock: a one or two frame gap mid-check is normal, and
        # re-sweeping would move the crop and restart the track for nothing. The caller's
        # drop rule ends the track if the gap turns out to be the end of the check.
        return Look(pred, desc, probs, should_hit, crop, held.origin, held, 2)

    centre = to_model_size(centre_slice(wide, geometry))
    pred, desc, probs, should_hit = predict(centre)
    if pred != NONE_CLASS:
        return Look(pred, desc, probs, should_hit, centre, geometry.centre, None, 1)

    found = locate_offcentre(wide, geometry)
    if found is None:
        return Look(pred, desc, probs, should_hit, centre, geometry.centre, None, 1)

    crop = crop_at(wide, found.origin, geometry)
    off_pred, off_desc, off_probs, off_hit = predict(crop)
    if off_pred == NONE_CLASS:
        return Look(pred, desc, probs, should_hit, centre, geometry.centre, None, 2)

    return Look(off_pred, off_desc, off_probs, off_hit, crop, found.origin, found, 2)


def to_model_size(crop, crop_size=int(TRAINING_REFERENCE_CROP)):
    """Resize a screen-scale crop to the size the ONNX input is pinned to."""

    if crop.shape[:2] == (crop_size, crop_size):
        return crop
    return cv2.resize(crop, (crop_size, crop_size), interpolation=cv2.INTER_CUBIC)


def scale_point(point, geometry, crop_size=int(TRAINING_REFERENCE_CROP)):
    """A point in screen-scale crop pixels, expressed in model-scale (224) pixels."""

    factor = crop_size / float(geometry.crop_side)
    return (point[0] * factor, point[1] * factor)


class Monitoring_wide(Monitoring_window):
    """`Monitoring_window` that grabs the wide box and hands back its centre slice.

    `get_frame_np` returns the same 224 RGB frame as before, so every existing caller —
    `AI_model.grab_screenshot` included — is unaffected. The wide grab it was sliced from
    is kept on `last_wide` (BGR, screen scale) for the off-centre sweep, so the two views
    are always the same instant. Grabbing twice would sample two different moments and put
    that difference straight into the velocity fit.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_wide: Optional[np.ndarray] = None

    def refresh(self):
        region = super().refresh()
        self.geometry = wide_geometry(self.content, self.crop_size, self.zoom)
        if not self.full_window:
            self.region = dict(self.geometry.region)
        return region

    def describe(self):
        described = super().describe()
        described["wide"] = self.geometry.describe()
        return described

    def grab_wide(self) -> np.ndarray:
        """The whole box, BGR at screen scale. One grab; `last_wide` keeps it."""

        raw = np.array(self.get_raw_frame(), dtype=np.uint8)[:, :, :3]  # BGRA -> BGR
        self.last_wide = raw
        return raw

    def get_frame_np(self) -> np.ndarray:
        if self.full_window:
            return super().get_frame_np()

        crop = centre_slice(self.grab_wide(), self.geometry)
        return np.flip(to_model_size(crop), 2)  # BGR -> RGB, at model size

    def get_frame_pil(self):
        from PIL import Image

        return Image.fromarray(self.get_frame_np())


def geometry_from_describe(meta):
    """Rebuild a `WideGeometry` from its own `describe()` output.

    A recorded bout stores the geometry it was cropped with rather than letting a reader
    re-derive one from the image — the frames ARE the wide box, so there is nothing left
    in them to derive it from. See `dbd/utils/bout_session.py`.
    """

    return WideGeometry(
        region=dict(meta["wide_region"]),
        crop_region=dict(meta["crop_region"]),
        centre=tuple(meta["centre_in_box"]),
        side=int(meta["side"]),
        crop_side=int(meta["crop_side"]),
        scale=float(meta["scale"]),
        clamped=bool(meta["clamped"]),
    )
