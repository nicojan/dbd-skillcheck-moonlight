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

What it CANNOT model is a policy that changes whether a check fires at all. For the aim
bias specifically that limit turned out to be vacuous, and this docstring asserted the
opposite for four days: a LATER target makes `lands` later, so `decide`'s
`press_at = lands - round_trip` is later too and there is MORE time to make it. The sign
was inverted. Of 77 no-presses in 1114 recorded decisions not one is "too late by N ms" or
"Great already passed" — every one is too-few-samples, poor fit, or rate out of range, and
the aim reaches none of those. So a bias past `great_width / 4` is measurable after all,
and the sweep below runs to 10 deg. See `aim_bias_for` for the clamp that replaced it.

"""

import glob
import json
import os
import sys
from statistics import mean, median, stdev

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autorun
from dbd.utils import needle_tracker
from dbd.utils.needle_tracker import AIM_BIAS_DEG, Zone, aim_bias_for, score_freeze

MAX_PLAUSIBLE_TRIP_MS = 200.0   # past this it is a wrapped revolution, not a latency

# A SCHEDULER STALL, which is a different animal from a wrapped revolution and needs its
# own gate. `round_trip_ms` cannot see one: it is derived from the settled angle (see
# autorun.py's `time_to_angle` call), so a stalled press and a slow link produce the same
# number and reconstructing one from the other is exact to 0.01 deg over all 546 recorded
# fires. `tail_read_ms` is the only independent reading — wall clock from key-down to the
# freeze onset — and it is tightly bounded: the freeze watch grabs every ~27 ms and gives
# up after three, so a legitimate tail read cannot exceed ~87 ms and p99 across the record
# is 81.7. One fire in 546 breaks that ceiling (2026-09-02 23:37:28, tail 184.4 ms against
# a trip of 181.0), and at 181 it slid under MAX_PLAUSIBLE_TRIP_MS and was being scored as
# a real +43 deg landing. That is the whole content of the 2026-09-02 contamination
# scare: not a poisoned session, one poisoned fire that nothing could name.
MAX_TAIL_READ_MS = 120.0        # 1.4x the worst legitimate tail read on record
HISTORIC_BIAS_DEG = 1.0         # the aim used before `aim_bias_deg` was logged per fire


def clamp_bias(bias_deg, zone):
    """`aim_bias_for` for an arbitrary candidate bias, not just the shipped constant."""

    room = zone.zone_width - zone.great_width / 2.0 - needle_tracker.ZONE_KEEP_DEG
    return max(0.0, min(bias_deg, room))


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
                tail = record.get("tail_read_ms")
                if tail is not None and tail > MAX_TAIL_READ_MS:
                    continue      # the machine stalled under this press, not the link
                fires.append((record, zone))
        if fires:
            out.append((os.path.basename(path), fires))
    return out


def regrade(sessions, lead_of, bias_deg=AIM_BIAS_DEG, base_of=None):
    """Grade every fire under `lead_of(last_trip_ms, trips)` and an aim bias, via score_freeze.

    Translation only, and measured from what the fire recorded. Leading by `d` ms MORE means
    pressing `d` ms earlier, so the landing moves `d` ms back along the sweep; the aim bias
    moves it forward. Both are applied as DIFFERENCES from what this fire already used,
    which is why no overshoot term appears — it is already inside the recorded error.
    """

    verdicts, errors = [], []
    for name, fires in sessions:
        # `base_of` is what a cold-start policy varies: the value the level tracker falls
        # back to before it has LEVEL_MIN_SAMPLES trips to form a median from. Passed per
        # session because a seed comes from the session BEFORE it, and pooling would hand
        # every session a number drawn partly from its own future.
        base = None if base_of is None else base_of(name)
        last_trip, trips = None, ()
        for record, zone in fires:
            rate = record["rate_deg_s"]
            sign = 1.0 if rate > 0 else -1.0
            per_ms = abs(rate) / 1000.0

            aimed = (lead_of(last_trip, trips) if base is None
                     else lead_of(last_trip, trips, base))
            lead_delta = record["lead_ms"] - aimed
            # From the aim this fire was ACTUALLY taken with, never the live constant. Read
            # from the constant, every recorded landing silently re-baselines the moment
            # someone edits it, and the tool reports the new value as already shipped — it
            # did exactly that when the bias moved off 1.0. `HISTORIC_BIAS_DEG` covers the
            # fires that pre-date the field; git says the constant was 1.0 from 08-15 17:01,
            # before the earliest landings file, so every one of them was aimed at 1.0.
            # What this fire was ACTUALLY aimed with, under the clamp in force when it was
            # written: `aim_bias_effective_deg` where the run logged it, and the old
            # `great_width / 4` bound for every record that pre-dates the zone clamp.
            # Re-clamping a historic record with today's rule would credit it with an aim
            # it never took, which is the same class of error `aim_bias_deg` was added to
            # stop — one level further in.
            was = record.get("aim_bias_effective_deg")
            if was is None:
                was = min(record.get("aim_bias_deg", HISTORIC_BIAS_DEG),
                          zone.great_width / 4.0)
            bias_delta = clamp_bias(bias_deg, zone) - was
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


def seeded(last_trip_ms, trips, base_ms):
    """The shipped policy with the 60 ms constant replaced by a per-session seed.

    Nothing else moves: the level tracker and the burst rule are untouched, so this only
    changes WHERE the lead starts, not how it adapts. That is the whole point — the level
    tracker already fixes the lead within LEVEL_MIN_SAMPLES trips, and the fires it cannot
    reach are the ones before it has them.
    """

    return autorun.lead_for_check(autorun.lead_level_ms(base_ms, trips), last_trip_ms)


def seeded_cold_only(last_trip_ms, trips, base_ms):
    """Seed the COLD START alone, then hand the base back to the shipped constant.

    `lead_level_ms` uses `base_lead_ms` twice: as the value to fall back to before it has
    `LEVEL_MIN_SAMPLES` trips, and as the value its deadband measures the median against
    for the whole rest of the session. `seeded` moves both, which is why it buys a smaller
    cold-start miss count and pays for it in Greats hours later — a base near the link's
    level puts every later median inside the deadband, pinning the lead to the seed.

    Only the first use is the cold start. This swaps the base back the moment the tracker
    can stand on its own, so the seed reaches the fires it was meant for and nothing else.
    """

    base = autorun.cold_start_base(autorun.ROUND_TRIP_MS, trips, base_ms)
    return autorun.lead_for_check(autorun.lead_level_ms(base, trips), last_trip_ms)


def session_medians(sessions):
    """Median round trip per session, in the chronological order the filenames give."""

    out = {}
    for name, fires in sessions:
        trips = sorted(r["round_trip_ms"] for r, _ in fires)
        out[name] = trips[len(trips) // 2] if trips else None
    return out


def previous_seed(sessions, fallback):
    """Seed each session from the one before it — the policy as it would actually run.

    An oracle that seeds a session from its OWN median measures nothing you could ship:
    at the first fire that number does not exist yet. The honest test is the value the
    previous run would have written to disk, which is what this builds. The earliest
    session has no predecessor and keeps the constant, exactly as a first-ever run would.
    """

    medians = session_medians(sessions)
    order = [name for name, _ in sessions]
    seeds, prev = {}, None
    for name in order:
        seeds[name] = fallback if prev is None else prev
        if medians[name] is not None:
            prev = medians[name]
    return seeds


def first_fires(sessions, n):
    """Each session truncated to its first `n` fires — where a cold start can still bite.

    A cold-start fix touches two or three fires in a session of forty, so an overall row
    buries it: the same two converted misses read as a rounding change. This is the view
    that can actually see the thing being changed.
    """

    return [(name, fires[:n]) for name, fires in sessions if fires[:n]]


def line(label, verdicts, errors):
    """One row. Misses are split by WHICH EDGE they left through, because the two are
    different failure modes with different causes: an early miss is a link drop the lead
    did not catch, a late miss is a link spike carried over the zone's trailing edge by
    the aim bias. Raising the bias trades the first for the second, and a total alone
    hides the moment that trade starts costing."""

    n = len(verdicts)
    early = sum(1 for v, e in zip(verdicts, errors) if v == "MISS" and e <= 0)
    late = sum(1 for v, e in zip(verdicts, errors) if v == "MISS" and e > 0)
    return ("  %-40s %3d GREAT (%4.1f%%)  %3d good  %2d MISS (%2de/%2dl)  mean %+5.2f  sd %4.2f"
            % (label, verdicts.count("GREAT"), 100.0 * verdicts.count("GREAT") / n,
               verdicts.count("good"), verdicts.count("MISS"), early, late,
               mean(errors), stdev(errors)))


USAGE = """re-score aim and lead policies against recorded landings

    .venv/bin/python tools/rescore_policy.py [landings-*.jsonl ...]

With no arguments it reads every landings-*.jsonl in the working directory. Pass a subset
to score one link regime on its own — the constant went stale because the link drifted, so
a whole-record number can hide a policy that only works on the sessions you no longer play.
"""


def main(argv):
    # A real -h, for the reason the sibling tool's absence of one is a documented trap:
    # `pull_check_stats.py` reads `"--peek" in argv[1:]` and treats everything else, --help
    # included, as a full drain — which is how the check log got drained by a --help call on
    # 2026-08-30. This tool is read-only, so the same slip only produced a confusing
    # FileNotFoundError for '--help', but the fix is one line and the surprise is the same.
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(USAGE)
        return 0
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
    for bias in (0.0, 1.0, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 8.0, 10.0):
        mark = "  <-- shipped" if bias == AIM_BIAS_DEG else ""
        print(line("bias %.1f deg%s" % (bias, mark), *regrade(sessions, rule, bias_deg=bias)))

    print("\n=== cold start: the fires before the level tracker has a median ===")
    print("  the shipped policy needs %d trips before it can move the lead; until then it"
          % autorun.LEVEL_MIN_SAMPLES)
    print("  aims with the %.0f ms constant, whatever the link is actually doing tonight."
          % base)
    seeds = previous_seed(sessions, base)
    seed_of = seeds.get
    for n in (3, 5):
        head = first_fires(sessions, n)
        if not head:
            continue
        shown = sum(len(f) for _, f in head)
        print("  -- first %d fires per session (%d fires) --" % (n, shown))
        print(line("SHIPPED (base %.0f ms)" % base, *regrade(head, shipped)))
        print(line("seeded from the previous session",
                   *regrade(head, seeded, base_of=seed_of)))
        print(line("seeded from the previous session, COLD START ONLY",
                   *regrade(head, seeded_cold_only, base_of=seed_of)))
        for seed in (30, 33, 36, 40):
            print(line("seeded at a fixed %d ms" % seed,
                       *regrade(head, seeded, base_of=lambda _n, s=seed: float(s))))
    print("  -- every fire, to confirm the fix costs nothing later --")
    print(line("SHIPPED (base %.0f ms)" % base, *regrade(sessions, shipped)))
    print(line("seeded from the previous session",
               *regrade(sessions, seeded, base_of=seed_of)))
    print(line("seeded from the previous session, COLD START ONLY",
               *regrade(sessions, seeded_cold_only, base_of=seed_of)))

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
    sys.exit(main(sys.argv) or 0)
