"""One JSONL record per skill check the bot saw, in a folder that gets drained.

WHY THIS EXISTS ALONGSIDE `LandingLog`, which writes the same records. The two answer
different questions and must not be merged:

  * `landings-*.jsonl` is the ARCHIVE. One file per session, never deleted, and the
    substrate every policy constant in this repo is derived from — `rescore_policy.py`
    re-reads all of it on every run, and the answers only mean anything pooled across
    the whole record. Twice a constant was moved on one session's evidence and was
    wrong both times.
  * `checks/*.jsonl` is a QUEUE of checks not yet looked at. `pull_check_stats.py`
    reports on it and then drains it into `checks/archive/`, so the next pull cannot
    re-report checks that have already been acted on.

Deleting the queue is therefore safe and deleting the archive is not, which is exactly
why they are separate files rather than one file with a cursor. A cursor is one bad
offset away from silently re-reporting or silently skipping.

TWO THINGS THIS RECORDS THAT THE ARCHIVE DOES NOT.

First, `path`: which of the bot's three routes the check took. A predictive fire, a
reactive press, or no press at all.

Second, and this is the gap that motivated the whole file: **reactive presses used to
leave no trace anywhere.** `autorun.py` fires reactively on wiggle and on a fitted check
with nowhere to aim, printing a `HIT:` line to a console log nobody parses, and writing
nothing. Eight of them in the 2026-08-24 20:44 session were invisible to every stats
tool in the repo, so "how many skill checks did we see" had no answer — only "how many
did we grade", which is a different and much smaller number.

Rotation is by GAP, not by clock or size: a new file starts when nothing has been
recorded for `ROTATE_GAP_SECONDS`. That makes each file one continuous bout of skill
checks — roughly a generator, a chase, a hook — which is the unit worth reading a rate
off. A wall-clock rotation would cut a bout in half and a size rotation would join two.
"""

import json
import os
from time import monotonic, strftime

CHECK_DIR = "checks"
ARCHIVE_DIR = os.path.join(CHECK_DIR, "archive")

# A minute of quiet ends a bout. Chosen to sit well clear of the longest gap WITHIN one
# — consecutive checks on a single generator run 5-30 s apart in the logged sessions —
# without being so long that two unrelated bouts merge. Nothing depends on the exact
# value: it only decides where a file boundary falls, never what is recorded.
ROTATE_GAP_SECONDS = 60.0


class CheckLog:
    """Append-only JSONL, one object per skill check, rotating on a gap of quiet.

    Stateful in the same way `LandingLog` is, and for the same reason: the handle has to
    outlive the call. Every write flushes, because a match that ends in a crash must
    still leave its evidence — that lesson cost four sessions here already.
    """

    def __init__(self, directory=CHECK_DIR, gap_seconds=ROTATE_GAP_SECONDS, clock=monotonic):
        self.directory = directory
        self.gap_seconds = gap_seconds
        self.clock = clock
        self.path = None
        self.handle = None
        self.written = 0        # this file
        self.total = 0          # this session, across rotations
        self.files = 0
        self._last_write = None

    def _rotate_due(self, now):
        return self._last_write is not None and (now - self._last_write) > self.gap_seconds

    def _open(self):
        os.makedirs(self.directory, exist_ok=True)
        # Named for the first record in it, not for the session, so a file's name says
        # when its bout started even when several share one run.
        stamp = strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.directory, f"checks-{stamp}.jsonl")
        # A restart inside the same second would otherwise append to the previous bout's
        # file and silently join two that the gap rule had just separated.
        suffix = 1
        while os.path.exists(path):
            suffix += 1
            path = os.path.join(self.directory, f"checks-{stamp}-{suffix}.jsonl")
        self.path = path
        self.handle = open(path, "a", encoding="utf-8")
        self.written = 0
        self.files += 1
        return path

    def write(self, record, path_taken):
        """Record one skill check. `path_taken` is 'predictive', 'reactive' or 'no press'.

        Returns the file it landed in, so the caller can say when a new bout started.
        """

        now = self.clock()
        rotated = self.handle is not None and self._rotate_due(now)
        if rotated:
            self.close()
        if self.handle is None:
            self._open()

        entry = dict(record)
        entry["path"] = path_taken
        self.handle.write(json.dumps(entry) + "\n")
        self.handle.flush()
        self.written += 1
        self.total += 1
        self._last_write = now
        return self.path, rotated

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def reactive_record(desc, confidence, reason=None, **context):
    """The record for a press that had no aim behind it, and so no landing to grade.

    Deliberately shaped like a landing record with the graded fields absent rather than
    invented: `verdict` and `round_trip_ms` are None because a reactive press does not
    watch for the freeze, so nothing here knows where it landed. A reader that treats
    missing as zero would report a reactive hit as a perfect one.
    """

    entry = dict(context)
    entry.update({
        "desc": desc,
        "confidence": None if confidence is None else round(float(confidence), 3),
        "reason": reason,
        "outcome": "reactive press",
        "landing": "not watched",
        "verdict": None,
        "error_deg": None,
        "round_trip_ms": None,
    })
    return entry
