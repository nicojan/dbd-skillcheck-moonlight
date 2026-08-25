"""Re-score aim and lead policies against the landings this bot actually recorded.

    .venv/bin/python tools/rescore_policy.py landings-*.jsonl

`AIM_BIAS_DEG`, `BURST_TRIP_MS` and `LEVEL_WINDOW` are all set from numbers produced this way, and
`AIM_BIAS_DEG`'s own comment ends with "Re-score the `.jsonl` landings instead" — but
nothing in the repo did it, so every claim in those comment blocks had to be rebuilt by
hand each time it was questioned. Twice the aim bias was moved on a replay sweep, which
measures the wrong distribution: replay error is ~1.0 deg sigma against 3.8 live.

Why re-scoring is legitimate here and a replay sweep is not. Both policies only TRANSLATE
the landing: the aim bias shifts the target along the sweep, and the lead shifts when the
press registers. Neither changes the link, the needle or the zone. So a recorded landing
can be re-graded under a different policy exactly, from

    error_ms = round_trip_ms - lead_ms + OVERSHOOT_MS

and the real `score_freeze` against the real recorded zone. Every re-grade starts from the
error the fire ACTUALLY recorded and the lead it was ACTUALLY aimed with, never from a
reconstruction: 214 of 377 logged fires (57%) ran with `--adapt-lead` on and were aimed at
something other than 60 ms, so assuming a 60 ms baseline mis-scores the majority of the
record. That mistake is what this docstring is here to stop being made a third time.

What it CANNOT model is a
policy that changes whether a check fires at all — a later target is a later deadline, so
a large aim bias trades misses for no-fires that this arithmetic cannot see. Treat a bias
past `great_width / 4` as unmeasured rather than good.

"""

import glob
import json
import os
import sys
from statistics import mean, median, stdev

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autorun
from dbd.utils.needle_tracker import AIM_BIAS_DEG, Zone, score_freeze

MAX_PLAUSIBLE_TRIP_MS = 200.0   # past this it is a wrapped revolution, not a latency
HISTORIC_BIAS_DEG = 1.0         # the aim used before `aim_bias_deg` was logged per fire


def load_sessions(paths):
    """One list of (record, zone) per file, keeping only fires that can be re-graded.

    Grouped BY FILE and never pooled, because a lead policy carries state from one check to
    the next and that state must not leak across a session boundary — a bot restart forgets
    everything, and a re-score that pretends otherwise flatters any policy with memory.
    """

    out = []
    for path in sorted(paths):
        fires = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                if record.get("round_trip_ms") is None or not record.get("zone"):
                    continue          # a no-press or an unmeasured landing: nothing to grade
                if record.get("error_deg") is None or record.get("lead_ms") is None:
                    continue          # pre-dates the fields the translation needs
                zone = Zone(**record["zone"])
                if not zone.great_measured:
                    continue          # full-white zone; every landing in it would score Great
                if not 0.0 < record["round_trip_ms"] < MAX_PLAUSIBLE_TRIP_MS:
                    continue
                fires.append((record, zone))
        if fires:
            out.append((os.path.basename(path), fires))
    return out


def regrade(sessions, lead_of, bias_deg=AIM_BIAS_DEG):
    """Grade every fire under `lead_of(last_trip_ms, trips)` and an aim bias, via score_freeze.

    Translation only, and measured from what the fire recorded. Leading by `d` ms MORE means
    pressing `d` ms earlier, so the landing moves `d` ms back along the sweep; the aim bias
    moves it forward. Both are applied as DIFFERENCES from what this fire already used,
    which is why no overshoot term appears — it is already inside the recorded error.
    """

    verdicts, errors = [], []
    for _, fires in sessions:
        last_trip, trips = None, ()
        for record, zone in fires:
            rate = record["rate_deg_s"]
            sign = 1.0 if rate > 0 else -1.0
            per_ms = abs(rate) / 1000.0

            lead_delta = record["lead_ms"] - lead_of(last_trip, trips)
            # From the aim this fire was ACTUALLY taken with, never the live constant. Read
            # from the constant, every recorded landing silently re-baselines the moment
            # someone edits it, and the tool reports the new value as already shipped — it
            # did exactly that when the bias moved off 1.0. `HISTORIC_BIAS_DEG` covers the
            # fires that pre-date the field; git says the constant was 1.0 from 08-15 17:01,
            # before the earliest landings file, so every one of them was aimed at 1.0.
            was = record.get("aim_bias_deg", HISTORIC_BIAS_DEG)
            bias_delta = (min(bias_deg, zone.great_width / 4.0)
                          - min(was, zone.great_width / 4.0))
            # into along-sweep degrees, where negative is early and the Great band's leading
            # edge sits at -great_width/2 with no margin behind it
            along = record["error_deg"] * sign + lead_delta * per_ms + bias_delta

            verdict, _ = score_freeze(zone, (zone.great_mid + along * sign) % 360.0)
            verdicts.append(verdict)
            errors.append(along)
            last_trip = record["round_trip_ms"]
            trips = (trips + (last_trip,))[-autorun.LEVEL_WINDOW:]
    return verdicts, errors


def shipped(last_trip_ms, trips):
    """The policy autorun actually runs: the link's level, then the burst rule on top."""

    return autorun.lead_for_check(
        autorun.lead_level_ms(autorun.ROUND_TRIP_MS, trips), last_trip_ms)


def line(label, verdicts, errors):
    n = len(verdicts)
    return ("  %-40s %3d GREAT (%4.1f%%)  %2d good  %2d MISS   mean %+5.2f  sd %4.2f deg"
            % (label, verdicts.count("GREAT"), 100.0 * verdicts.count("GREAT") / n,
               verdicts.count("good"), verdicts.count("MISS"), mean(errors), stdev(errors)))


def main(argv):
    paths = argv[1:] or glob.glob("landings-*.jsonl")
    sessions = load_sessions(paths)
    if not sessions:
        raise SystemExit("no gradeable fires found — pass some landings-*.jsonl")
    total = sum(len(f) for _, f in sessions)
    print("%d gradeable fires across %d sessions\n" % (total, len(sessions)))

    base = autorun.ROUND_TRIP_MS
    print("=== lead policy (aim bias held at the shipped %.1f deg) ===" % AIM_BIAS_DEG)
    fixed = regrade(sessions, lambda _t, _h: base)
    print(line("fixed %.0f ms (the floor to beat)" % base, *fixed))
    print(line("burst rule alone, <%.0f -> %.0f"
               % (autorun.BURST_TRIP_MS, autorun.BURST_LEAD_MS),
               *regrade(sessions, lambda t, _h: autorun.lead_for_check(base, t))))
    print(line("SHIPPED: link level (med-%d) + burst" % autorun.LEVEL_WINDOW,
               *regrade(sessions, shipped)))
    for lead in (56, 58, 62, 64):
        print(line("fixed %d ms" % lead, *regrade(sessions, lambda _t, _h, l=lead: float(l))))

    print("\n=== aim bias (lead held at the shipped level + burst rule) ===")
    rule = shipped
    for bias in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        mark = "  <-- shipped" if bias == AIM_BIAS_DEG else ""
        print(line("bias %.1f deg%s" % (bias, mark), *regrade(sessions, rule, bias_deg=bias)))

    print("\n=== per session, shipped policy against a fixed lead ===")
    print("  a policy that wins overall by losing a session is not an improvement")
    for name, fires in sessions:
        one = [(name, fires)]
        fv, _ = regrade(one, lambda _t, _h: base)
        bv, _ = regrade(one, rule)
        moved = ("" if (fv.count("GREAT"), fv.count("MISS")) ==
                 (bv.count("GREAT"), bv.count("MISS")) else
                 ("  BETTER" if bv.count("MISS") < fv.count("MISS") or
                  (bv.count("MISS") == fv.count("MISS") and
                   bv.count("GREAT") > fv.count("GREAT")) else "  WORSE"))
        print("  %-34s n=%3d  fixed %3dG/%2dM -> shipped %3dG/%2dM%s"
              % (name, len(fires), fv.count("GREAT"), fv.count("MISS"),
                 bv.count("GREAT"), bv.count("MISS"), moved))

    print("\n=== where the link has been sitting ===")
    print("  a level shift and a burst look identical inside one session; this is what")
    print("  separates them, and the 60 ms constant went stale because nothing printed it")
    for name, fires in sessions:
        per = [r["round_trip_ms"] for r, _ in fires]
        print("  %-34s n=%3d  median %5.1f  %5.1f to %5.1f ms"
              % (name, len(per), median(per), min(per), max(per)))

    trips = [r["round_trip_ms"] for _, f in sessions for r, _ in f]
    below = [t for t in trips if t < autorun.BURST_TRIP_MS]
    print("\n=== the gap the threshold sits in ===")
    print("  round trip: median %.1f, %.1f to %.1f ms" % (median(trips), min(trips), max(trips)))
    print("  %d of %d fires (%.1f%%) are under the %.0f ms threshold; they reach %.1f ms"
          % (len(below), len(trips), 100.0 * len(below) / len(trips),
             autorun.BURST_TRIP_MS, max(below) if below else float("nan")))
    above = [t for t in trips if t >= autorun.BURST_TRIP_MS]
    print("  the normal population floors at %.1f ms — the threshold splits an empty gap"
          % min(above))


if __name__ == "__main__":
    main(sys.argv)
