"""Read a landings JSONL back and say what the match actually did.

    .venv/bin/python tools/read_landings.py landings-20260816-004500.jsonl
    .venv/bin/python tools/read_landings.py landings-*.jsonl --unscored

`autorun.py` writes one object per predictive fire: the aim, the fit, the lead it used,
the verdict, and the raw `(ms since press, angle, strength)` series the freeze watch
collected. This turns that into the two numbers that decide what to do next.

**The landing error is the measurement; the round trip is the same measurement in
milliseconds.** `round_trip = lead - overshoot + error/rate`, so quoting both as if they
were independent evidence double-counts one observation. What matters is the SPREAD:
under 3 deg and the remaining loss is not timing, over 5 and the link jitter is the whole
problem and only Moonlight settings or host-side input injection can move it.

The `--unscored` dump is the point of keeping the readings at all. A fire that produced no
verdict is either a press that never reached the game or a landing the watch could not
read, and those call for opposite work. The series separates them: a needle that sweeps
cleanly at the fitted rate straight through the aim and off the screen was never
interrupted, so nothing arrived. One that freezes and is then lost to stray red is a
measurement fault.
"""

import argparse
import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root


def parse_args():
    p = argparse.ArgumentParser(description="Summarise autorun landing logs")
    p.add_argument("paths", nargs="+", help="landings-*.jsonl (globs allowed)")
    p.add_argument("--unscored", action="store_true",
                   help="dump the reading series for every fire that produced no verdict")
    p.add_argument("--all", action="store_true", help="dump the reading series for every fire")
    return p.parse_args()


def load(paths):
    records = []
    for pattern in paths:
        for path in sorted(glob.glob(pattern)) or [pattern]:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
    return records


def median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def stdev(values):
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def tally(values):
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return ", ".join(f"{k} {counts[k]}" for k in sorted(counts, key=lambda k: -counts[k]))


MAX_GREAT_DEG = 20.0  # kept in step with needle_tracker.MAX_GREAT_DEG


def gradeable(record):
    """Was this fire's Great band actually measured, or was the whole zone lit?

    Read from the recorded zone rather than the verdict, so logs written BEFORE the
    2026-08-16 fix are re-judged honestly instead of being taken at their word. A
    `full white` check draws one solid block: its band reads 33-59 deg and fills its own
    zone, so every landing inside scored GREAT and the tally flattered itself.
    """

    zone = record.get("zone")
    if not zone:
        return True
    great = (zone["great_end"] - zone["great_start"]) % 360.0
    span = (zone["zone_end"] - zone["zone_start"]) % 360.0
    return great <= MAX_GREAT_DEG and great < 0.9 * span


def summarise(records):
    """Lines describing a set of fires. Pure, so the arithmetic can be tested."""

    if not records:
        return ["no fires recorded"]

    ungraded = [r for r in records if not gradeable(r)]
    records = [r for r in records if gradeable(r)]
    if not records:
        return [f"{len(ungraded)} fires, none with a measured Great band — nothing to grade"]

    verdicts = [r["verdict"] for r in records if r.get("verdict")]
    errors = [r["error_deg"] for r in records if r.get("error_deg") is not None]
    trips = [r["round_trip_ms"] for r in records if r.get("round_trip_ms") is not None]

    lines = [f"{len(records)} fires — {tally(r['landing'] for r in records)}"]
    if verdicts:
        lines.append(f"  verdicts: {tally(verdicts)}"
                     f"  ({100.0 * verdicts.count('GREAT') / len(verdicts):.0f}% Great"
                     f" of {len(verdicts)} scored)")

    if errors:
        spread = stdev(errors)
        lines.append(f"  landing error: median {median(errors):+.1f} deg, bias "
                     f"{sum(errors) / len(errors):+.1f}, {min(errors):+.1f} to "
                     f"{max(errors):+.1f}"
                     + (f", sigma {spread:.1f} deg" if spread is not None else ""))
        if spread is not None:
            # The Great band is +/-5.25 deg about its centre, so a sigma near that IS the
            # hit rate. Saying so stops the spread being read as a tuning problem.
            lines.append(f"  that sigma is {spread / 5.25:.1f}x the Great half-width — "
                         + ("timing is not the remaining loss" if spread < 3.0
                            else "the link jitter is the ceiling, not the aim"))

    if trips:
        spread = stdev(trips)
        lines.append(f"  round trip: median {median(trips):.0f} ms, {min(trips):.0f}-"
                     f"{max(trips):.0f}"
                     + (f", sigma {spread:.0f} ms" if spread is not None else "")
                     + f", n={len(trips)}")
        lines.append("  (the same measurement as the landing error, in ms — not "
                     "independent evidence)")

    leads = [r["lead_ms"] for r in records if r.get("lead_ms") is not None]
    if leads:
        lines.append(f"  lead used: {leads[0]:.0f} -> {leads[-1]:.0f} ms")

    if ungraded:
        kinds = tally(r.get("desc", "?") for r in ungraded)
        landed = [r["error_deg"] for r in ungraded if r.get("error_deg") is not None]
        lines.append(f"  + {len(ungraded)} fires excluded — no measurable Great band "
                     f"({kinds}); they pressed and "
                     + (f"{sum(1 for r in ungraded if r.get('verdict') != 'MISS')} landed "
                        f"in the zone" if landed else "were not read")
                     + ", but the band is unmeasured so they cannot be graded")

    unscored = [r for r in records if not r.get("verdict")]
    if unscored:
        lines.append(f"  {len(unscored)} of {len(records)} produced no verdict: "
                     + tally(r["landing"] for r in unscored))
        lines.append("  run with --unscored to see what their readings show")
    return lines


def describe_watch(record):
    """Lines for one fire's raw readings, with the sweep-vs-freeze question answered."""

    readings = record.get("readings", [])
    head = (f"fire {record.get('fire', '?')} at {record.get('at', '?')} — "
            f"{record.get('landing')} ({record.get('outcome')}), "
            f"aimed {record.get('target_deg')}, {record.get('rate_deg_s')} deg/s, "
            f"lead {record.get('lead_ms')} ms")
    lines = [head, f"  {record.get('lit')} lit of {record.get('reads')} reads, "
                   f"{record.get('dark_tail')} dark at the end, floor "
                   f"{record.get('lit_floor')}"]

    floor = record.get("lit_floor", 0) or 0
    lit = [(t, a) for t, a, s in readings if s >= floor]
    if len(lit) >= 2 and record.get("rate_deg_s"):
        # Did the needle keep turning at the rate the fit measured, right to the end? If it
        # did, nothing interrupted the check, so no press arrived.
        span_ms = lit[-1][0] - lit[0][0]
        travelled = (lit[-1][1] - lit[0][1]) % 360.0
        rate = record["rate_deg_s"]
        if rate < 0:
            travelled = (lit[0][1] - lit[-1][1]) % 360.0
        expected = abs(rate) * span_ms / 1000.0
        lines.append(f"  lit block swept {travelled:.0f} deg in {span_ms:.0f} ms; an "
                     f"uninterrupted needle would have swept {expected:.0f}")
        if expected > 20.0:
            ratio = travelled / expected
            lines.append("  -> the needle never stopped: the press did not reach the game"
                         if ratio > 0.8 else
                         "  -> the needle stopped part way: something interrupted the check")

    for t, a, s in readings:
        mark = " " if s >= floor else "."
        lines.append(f"   {mark} {t:7.1f} ms  {a:6.1f} deg  strength {s:5.1f}")
    return lines


def main():
    args = parse_args()
    records = load(args.paths)
    for line in summarise(records):
        print(line)

    wanted = records if args.all else [r for r in records if not r.get("verdict")]
    if args.unscored or args.all:
        for record in wanted:
            print()
            for line in describe_watch(record):
                print(line)


if __name__ == "__main__":
    main()
