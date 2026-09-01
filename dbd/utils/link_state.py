"""What the last run measured the link to be, carried across the session boundary.

WHY THIS EXISTS.

`lead_level_ms` cannot move the lead until it has `LEVEL_MIN_SAMPLES` round trips, so the
first two or three fires of every session aim with the `ROUND_TRIP_MS` constant no matter
what the link is doing. That constant was calibrated on a ~61 ms link. On 2026-08-31 the
link ran at a 36 ms median and the first two fires landed 6.0 and 7.5 deg early — both
MISS, both before the tracker had a median to work from. The rest of the session, once the
tracker engaged, went 22 GREAT / 16 good / 0 MISS.

This file is the smallest thing that closes that gap: the previous run's measured level,
on disk, available at the first fire.

WHAT IT DELIBERATELY DOES NOT DO.

It does not touch the level tracker or the burst rule, and it is not a second lead policy.
It only supplies a starting value, and only for the fires before the tracker has its
samples — see `autorun.cold_start_base` for why that narrowness is load-bearing rather
than timid. Re-scored against every recorded landing with `tools/rescore_policy.py`,
widening it to the tracker's whole deadband costs 36 Greats in the recent link regime to
save the same two misses.
"""

import json
import os
import time

DEFAULT_PATH = os.path.join("state", "link_level.json")

# Older than this and the reading says nothing about tonight's link: the measured median
# has moved between 32.7 and 66.3 ms across the recorded sessions, so a week-old number is
# no better informed than the constant it would replace.
MAX_AGE_S = 3 * 24 * 3600

# Outside this, a stored value is a bug rather than a link — refuse it rather than aim
# with it. The recorded population spans 9 to 176 ms per fire and 32.7 to 66.3 ms per
# session median, so the bound is wide enough to admit any real link and narrow enough to
# catch a zero, a negative, or a garbled file.
MIN_MS, MAX_MS = 10.0, 120.0


def save(level_ms, path=DEFAULT_PATH, now=None):
    """Record the level this session settled at. Returns True if it was written.

    A failure here must never take the run down with it — this is a convenience for the
    NEXT session, and the current one has already finished its work by the time it runs.
    """

    if level_ms is None or not MIN_MS <= float(level_ms) <= MAX_MS:
        return False
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"level_ms": round(float(level_ms), 1),
                       "at": time.time() if now is None else now}, f)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def load(path=DEFAULT_PATH, now=None):
    """The stored level, or None if there isn't a usable one.

    None is the honest answer for missing, unreadable, stale and out-of-range alike: every
    one of them means "no better information than the constant", and the caller's fallback
    is the same in each case. Distinguishing them would only invite a caller to treat a
    corrupt file as a soft signal.
    """

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    level, at = data.get("level_ms"), data.get("at")
    if not isinstance(level, (int, float)) or not isinstance(at, (int, float)):
        return None
    if not MIN_MS <= level <= MAX_MS:
        return None
    if (time.time() if now is None else now) - at > MAX_AGE_S:
        return None
    return float(level)
