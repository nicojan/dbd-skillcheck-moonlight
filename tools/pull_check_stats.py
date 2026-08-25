"""Report on the skill checks not yet looked at, then drain the queue.

    .venv/bin/python tools/pull_check_stats.py            # report and drain
    .venv/bin/python tools/pull_check_stats.py --peek     # report, leave the queue alone

The queue is `checks/*.jsonl`, written by `check_log.py` and rotated on a minute of
quiet so each file is one continuous bout of checks. Draining MOVES those files to
`checks/archive/`; it never deletes them. The point of draining is that the next pull
cannot re-report checks already acted on — not that the checks stop existing. Every
policy constant in this repo comes from pooled evidence, and this tool is not allowed
to shrink that pool.

WHAT THIS IS NOT. It is not `rescore_policy.py`. That tool re-grades the whole archive
under a hypothetical policy and is how constants get changed; it needs every fire ever
recorded and would be actively misleading on one night's queue — reading a policy off
one session has produced the wrong answer here twice. This tool only reports what
happened, on the checks you have not seen yet. If a number here looks like it wants a
constant changed, take the question to `rescore_policy.py` over the full archive.
"""

import glob
import json
import os
import shutil
import sys
from collections import Counter
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_log import ARCHIVE_DIR, CHECK_DIR


def load(paths):
    """One (name, records) per file, in order. Files are bouts; never pool them blind."""

    out = []
    for path in sorted(paths):
        records = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    # A crash mid-write leaves a partial last line. Losing it is correct;
                    # refusing to report the 40 good records above it is not.
                    print("  (skipped a truncated line in %s)" % os.path.basename(path))
        if records:
            out.append((os.path.basename(path), records))
    return out


def summarise(records):
    """The tally for one bout, or for everything. Verdicts only from graded fires."""

    paths = Counter(r.get("path") for r in records)
    graded = [r for r in records if r.get("verdict") in ("GREAT", "good", "MISS")]
    verdicts = Counter(r["verdict"] for r in graded)
    trips = [r["round_trip_ms"] for r in records
             if isinstance(r.get("round_trip_ms"), (int, float))]
    return paths, verdicts, graded, trips


def bout_line(name, records):
    paths, verdicts, graded, trips = summarise(records)
    rate = ("%4.0f%%" % (100.0 * verdicts["GREAT"] / len(graded))) if graded else "   --"
    return ("  %-30s %3d checks %3d/%2d/%2d   %4dG %4dg %3dM   Great %s  trip %s"
            % (name, len(records), paths["predictive"], paths["reactive"],
               paths["no press"], verdicts["GREAT"], verdicts["good"], verdicts["MISS"],
               rate, ("%5.0f ms" % median(trips)) if trips else "     --"))


def main(argv):
    peek = "--peek" in argv[1:]
    files = load(glob.glob(os.path.join(CHECK_DIR, "checks-*.jsonl")))
    if not files:
        print("nothing queued in %s/ — either no checks since the last pull, or the bot "
              "ran with --no-check-log" % CHECK_DIR)
        return 0

    every = [r for _, records in files for r in records]
    paths, verdicts, graded, trips = summarise(every)

    print("%d skill checks across %d bout(s)\n" % (len(every), len(files)))
    print("=== per bout (a bout is checks with no minute-long gap in them) ===")
    print("  %-30s %-10s %-12s %-15s %s"
          % ("file", "seen", "pred/re/no", "GREAT good MISS", "of graded"))
    for name, records in files:
        print(bout_line(name, records))

    print("\n=== all of it ===")
    print("  acted on:   %d predictive, %d reactive" % (paths["predictive"], paths["reactive"]))
    print("  not acted:  %d tracked checks got no press" % paths["no press"])
    if paths["no press"]:
        reasons = Counter(r.get("reason") for r in every if r.get("path") == "no press")
        for reason, n in reasons.most_common(5):
            print("                %2d  %s" % (n, reason))

    if graded:
        print("  graded:     %d GREAT (%.1f%%), %d good, %d MISS"
              % (verdicts["GREAT"], 100.0 * verdicts["GREAT"] / len(graded),
                 verdicts["good"], verdicts["MISS"]))
        # Which edge a miss left through is the thing worth seeing at a glance since the
        # aim moved to 4.5 deg: early is a link drop, late is a spike carried over the
        # trailing edge by the bias, and only the second is new.
        misses = [r for r in graded if r["verdict"] == "MISS"]
        early = sum(1 for r in misses if (r.get("error_deg") or 0) <= 0)
        if misses:
            print("              %d early / %d late" % (early, len(misses) - early))
            for r in misses:
                print("                %s  %+.1f deg  %s  trip %s"
                      % (r.get("at", "--:--:--"), r.get("error_deg", 0.0),
                         r.get("desc", "?"),
                         ("%.0f ms" % r["round_trip_ms"])
                         if isinstance(r.get("round_trip_ms"), (int, float)) else "--"))
    else:
        print("  graded:     none — no predictive fire got a readable landing")

    # Reactive presses are counted but never graded: nothing watches where they land, so
    # a reader that treats them as hits is inventing a result. Say so rather than let the
    # Great percentage above be read as covering every check.
    if paths["reactive"]:
        print("  note:       the %d reactive press(es) are UNGRADED — the freeze watch "
              "does not run on them," % paths["reactive"])
        print("              so nothing knows where they landed. They are in the check "
              "count, not the Great rate.")
    if trips:
        print("  round trip: median %.0f ms over %d measured" % (median(trips), len(trips)))

    if peek:
        print("\n--peek: queue left in place. Drop --peek to drain it.")
        return 0

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    moved = 0
    for name, _ in files:
        src = os.path.join(CHECK_DIR, name)
        dst = os.path.join(ARCHIVE_DIR, name)
        # Never clobber: an archived bout is evidence, and a same-second name collision
        # after a restart must not silently overwrite the older one.
        suffix = 1
        while os.path.exists(dst):
            suffix += 1
            stem, ext = os.path.splitext(name)
            dst = os.path.join(ARCHIVE_DIR, "%s-%d%s" % (stem, suffix, ext))
        shutil.move(src, dst)
        moved += 1
    print("\ndrained: %d file(s) moved to %s/ — the next pull starts clean"
          % (moved, ARCHIVE_DIR))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
