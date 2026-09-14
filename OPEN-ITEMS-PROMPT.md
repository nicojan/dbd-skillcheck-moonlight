Work the four items left open after the 2026-09-14 sweep and delete. Two need a match played; two are desk work. Do them in the order below and report each one's verdict separately.

Read NOTES-local.md "Resume here" first. This repo's rule is that a visible mechanism is not evidence, and that a tool reporting success can still be answering the wrong question.

BACKGROUND (verified 2026-09-14 against the code, don't re-derive it):
The off-centre question is CLOSED — every session in the repo has been swept. `frames/` is
21 G: `session_20260829_154535` (14 G, the Doctor replay corpus, kept on purpose) and 27
`bout_*` (6.4 G). `frames/_metadata/` holds the deleted sessions' manifests. Volume at 91%,
89 G free. HEAD is `7e74afb` on `macos-moonlight`, clean and pushed.

================================================================================
ITEM 1+2 — ONE MATCH ANSWERS BOTH. Play it first; it has been blocked for days.
================================================================================

`LoadTally` and the `o` key have both never run for real. **They do not need two matches.**
The `dbd` shell function passes `--record` before "$@" (autorun.py:365-372), so an armed
match records its own bouts — the 08-29 note that "armed and recorded cannot run in the same
session" is STALE and predates that flag. One match, then one review.

BEFORE THE MATCH:

  * Run the **`dbd` Raycast command** (new, 2026-09-14, `bin/game-mode.sh`). It quits twelve
    background apps and remembers which were actually running; `Done Gaming` puts back
    exactly those. **Its `on` path has never run for real** — dry-run, a single-app round
    trip and the whole `off` path are verified, the twelve-app quit is not. Read what it
    says it closed. If it names something it should not have, that is a finding, not noise.
  * Then `uptime`. **Never score a match under external load** — that has already cost four
    matches. The shell guard reads once at launch, which is exactly what missed the three
    matches that armed clean and went bad mid-match; that is why LoadTally exists.

THEN PLAY ONE MATCH and read the shutdown block. Three things to check, in this order:

  1. **`LoadTally` fired at all.** Look for a line beginning `load:` printed BEFORE the
     landings figures (autorun.py:1444 — deliberately before, a verdict after the figures is
     read too late). Three shapes are possible and they are not interchangeable:

         load: peak 3.41, last 2.90 over 37 samples (gate 6) — SCORABLE
         load: peak 7.02, last 6.80 over 37 samples (gate 6) — DO NOT SCORE: 9 of 37 ...
         load: not measured — run too short to sample

     The third means the tally never got a sample, NOT that the machine was idle. A quiet
     run says SCORABLE out loud precisely so that silence cannot be mistaken for evidence.

  2. **`load_1min` was actually written per fire** (autorun.py:1346). The landings file is
     `landings-<timestamp>.jsonl` at the repo root:

         grep -c load_1min landings-<the new one>.jsonl

     Compare that count against the number of fires the summary reports. A count of 0 with a
     healthy `load:` line means the stamp is wired to the summary and not to the fires.

  3. **The `o` key on a real bout.** Run `tools/review_recordings.py` after the match, press
     `o` on a bout (it cycles unknown -> seen -> none -> unknown), then read it back:

         python -c "import json;print(json.load(open('frames/<bout>/bout.json'))['off_centre'])"

     `unknown` must stay distinguishable from `none` — a bout you did not answer is not a
     bout with no off-centre checks, and collapsing the two manufactures evidence from every
     unmarked bout. **This does not work retroactively**: all 27 existing bouts are already
     marked reviewed and read as `unknown`, so the TUI will not offer them. It has to be a
     bout from this match.

TRAP: Ctrl-C is the supported way to end a run and it takes two traps in `dbd` to survive.
If the shutdown block does not appear at all, that is the `tee` failure from 08-29, not a
missing tally — check `tools/test_dbd_shell.py` before concluding anything about LoadTally.

================================================================================
ITEM 3 — the degenerate zone READ. Only the REPORTING was fixed.
================================================================================

`19521f1` made a MISS on a zone with no Great band countable apart from a real one. The READ
that produces those zones is untouched: `find_zone` in `dbd/utils/needle_tracker.py:630`.

The failure, from the record: at 18:08:22 `great_start == zone_start == 201` and
`great_end == zone_end == 210` — a 9 degree zone on a `repair-heal` check that draws ~50.
`find_zone` located a 9 degree run of zone thickness, found a Great band filling all of it,
and returned a Zone in which Great and the success zone are the same 9 degrees. Downstream
this cost two things: `aim_bias_for` returned 0.0, so the shot went in ~3 deg early, and the
run summary counted the result beside a real miss.

Rare and real: **7 of 2374 fires, 0.3%**, arriving in pairs seconds apart.

WHAT TO DO — and the order matters, because this repo has three times designed against an
aggregate that turned out to be a reporting artefact:

  1. **Characterise before changing anything.** Pull the 7 from the landings logs and the
     check queue and look at what `find_zone` actually saw. `great_width == zone_width` on a
     check class that draws ~50 deg is the signature; confirm that is what all 7 are, and not
     several different faults sharing one symptom.
  2. **Then decide whether the answer is a refusal or a better read.** A zone whose Great
     band fills 100% of it, on a class that never draws one that narrow, is more likely a
     failed read than a real zone — `find_zone` already returns None for Merciless Storm
     rather than guessing, so refusing is an established behaviour here, not a new one.
  3. Whatever the fix, it must not touch the centred path that has ~2400 fires behind it.
     `tools/test_needle_tracker.py` and `tools/test_wide_capture.py` both pin that.

================================================================================
ITEM 4 — Merciless Storm. MEASURE before abstaining. Fix the geometry blocker first.
================================================================================

The abstain recommendation is WITHDRAWN and must not be acted on. The ~25 deg late figure
behind it came from the same style of proxy measurement that was proved wrong on wiggle
hours later — that reasoning predicted 33% failures and the correct method found 10 of 10
landing centrally. **Storm having no Great tier raises the stakes of a miss; it is not
evidence one occurs.**

THE BLOCKER IS CONFIRMED AND MUST BE FIXED FIRST. Verified 2026-09-14:

    recordings_video/merciless-storm/events.json   "size": [1920, 1080]
    videos/merciless-storm.mp4  (ffprobe)           1280x720

Exactly 1.5x. Every crop origin, the centre (959.5, 539.75) and `ring_r` (64.99) in that
file assume 1080p, so any measurement taken against the 720p mp4 is reading the wrong pixels
— and will do so silently, producing plausible numbers. Settle which of the two is right
before measuring anything. Note the sibling `merciless-storm-madness2` clip is the
Doctor-plus-Storm footage and is a separate file; check its geometry too rather than assuming
it shares the fault.

THEN re-measure Storm's landing with the **wiggle method** — cue frame + local rate x trip,
scored against the drawn zone — not against a recorded window's endpoint. **A recorded window
endpoint is not a landing**; that is exactly the error that produced the false 33% wiggle
figure, 200-250 ms after the cue against a 43 ms trip. The method is preserved at
`.claude/wtrig.py` (bands at `.claude/bands.py`), both gitignored, and both read
`frames/bout_20260907-162322` and `frames/bout_20260907-165119` — **kept through the delete
for this reason**, so they are still there.

Only if the measurement shows a real problem does the behaviour question open: `decide`
returns `may_react`, so `autorun.py` fires on the classifier's cue. Suppressing it means
gating the REACTIVE path, not the tracker. The outline path that was tested against Storm
both ways was removed in `8a1c62d`, so Storm is back to the reactive fallback alone.

================================================================================
WHEN EACH ITEM IS DONE
================================================================================

Append a short entry to the top of "Resume here" in NOTES-local.md saying what was found,
including the items that came back clean — an unverified item and a verified-fine item look
identical in a handoff otherwise. Commit per item, not in one lump.

Do NOT reopen: the 855 "ungraded" reactive presses are 845 wiggle, ungradeable by
construction — do not build a reactive freeze watch. `ROUND_TRIP_MS` stays 60 and the aim
bias stays 3.0; never score the lead constant on an unseeded re-score. The off-centre sweep
question is closed.
