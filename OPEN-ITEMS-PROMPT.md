Four items were left open after the 2026-09-14 sweep. **Two are done** — the degenerate zone read (`1f598e9`) and Merciless Storm (`9980fd7`), both 2026-09-15, both written up at the top of NOTES-local.md "Resume here". **What is left needs a match played**, and one match answers all of it.

Read NOTES-local.md "Resume here" first. This repo's rule is that a visible mechanism is not evidence, and that a tool reporting success can still be answering the wrong question.

BACKGROUND (verified, don't re-derive it):
The off-centre question is CLOSED — every session in the repo has been swept. `frames/` is
21 G. The degenerate zone read was an AMBER success zone against a whiteness test, and is
fixed and replayed. Merciless Storm lands 16 of 17 inside its drawn zone and needs no
gating. HEAD is `e85cc91` on `macos-moonlight`, clean and pushed.

================================================================================
ONE MATCH ANSWERS FOUR THINGS. It has been blocked for days.
================================================================================

`LoadTally`, the `load_1min` stamp, the `o` key and the folded `game-mode.sh on` have all
never run for real. **They do not need four matches.** The `dbd` shell function passes
`--record` before "$@" (autorun.py:365-372), so an armed match records its own bouts — the
08-29 note that "armed and recorded cannot run in the same session" is STALE.

BEFORE THE MATCH:

  * **`source ~/.zshrc`.** The `dbd` function changed on 2026-09-15 and a stale shell is
    exactly why the `--seed-lead` flag missed on 09-01.
  * **`uptime`.** Never score a match under external load — that has already cost four
    matches. The shell guard reads once at launch, which is what missed the three matches
    that armed clean and went bad mid-match; that is why LoadTally exists.

THEN TYPE `dbd` — nothing else. **It now quits the background apps itself**, so there is no
Raycast step before the match any more. Read the line it prints:

    Game mode: closed 11 — Bartender BetterTouchTool Quip ... (load 1m: 8.27)

**That line has never been printed by a real run.** Verified separately: the dry run, a
live quit/reopen round trip on Rocket, the whole `off` path against a scratch manifest, and
detection agreeing with `lsappinfo` on 8 apps. If it names something it should not have
closed, that is a finding, not noise. If it prints nothing at all, check whether the shell
was sourced before blaming the script.

PLAY ONE MATCH, then read the shutdown block. Three things, in this order:

  1. **`LoadTally` fired at all.** Look for a line beginning `load:` printed BEFORE the
     landings figures (autorun.py:1444 — deliberately before, a verdict after the figures
     is read too late). Three shapes are possible and they are not interchangeable:

         load: peak 3.41, last 2.90 over 37 samples (gate 6) — SCORABLE
         load: peak 7.02, last 6.80 over 37 samples (gate 6) — DO NOT SCORE: 9 of 37 ...
         load: not measured — run too short to sample

     The third means the tally never got a sample, NOT that the machine was idle. A quiet
     run says SCORABLE out loud precisely so that silence cannot be mistaken for evidence.

  2. **`load_1min` was actually written per fire** (autorun.py:1346). The landings file is
     `landings-<timestamp>.jsonl` at the repo root:

         grep -c load_1min landings-<the new one>.jsonl

     Compare that count against the number of fires the summary reports. A count of 0 with
     a healthy `load:` line means the stamp is wired to the summary and not to the fires.

  3. **The `o` key on a real bout.** Run `tools/review_recordings.py` after the match,
     press `o` on a bout (it cycles unknown -> seen -> none -> unknown), then read it back:

         python -c "import json;print(json.load(open('frames/<bout>/bout.json'))['off_centre'])"

     `unknown` must stay distinguishable from `none` — a bout you did not answer is not a
     bout with no off-centre checks, and collapsing the two manufactures evidence from
     every unmarked bout. **This does not work retroactively**: all 27 existing bouts are
     already marked reviewed and read as `unknown`, so the TUI will not offer them. It has
     to be a bout from this match.

AFTERWARDS: run **`Done Gaming`** in Raycast (or `bin/game-mode.sh off`) to put back exactly
what was closed. This is deliberately NOT automatic — a Ctrl-C between matches must not
reopen eleven apps that are about to be closed again. If Raycast has never been pointed at
`~/dev/dbd_autoSkillCheck/raycast`, add it in Settings → Extensions → Scripts; the shell
path works either way.

TRAP: Ctrl-C is the supported way to end a run and it takes two traps in `dbd` to survive.
If the shutdown block does not appear at all, that is the `tee` failure from 08-29, not a
missing tally — check `tools/test_dbd_shell.py` before concluding anything about LoadTally.

================================================================================
IF THE MATCH IS SCORABLE, ONE MORE QUESTION FALLS OUT
================================================================================

Aim bias 3.0 with `--seed-lead` is **still unadjudicated after five attempts**, four of them
lost to load. The standing target is 25 GREAT / 11 good / 2 MISS. `armed-20260913-1741.log`
scored 42 G / 9 good / 2 MISS over 60 fires, and **one of those two misses was the amber
zone read fixed on 09-15**, so it is 1 real miss — but no `uptime` was taken during it, so
it cannot be scored. A `SCORABLE` verdict from LoadTally on this match is the first time
that question can be answered cleanly.

Do NOT re-litigate 3.0 from the 08-31 table — it was scored against a seed that had never
executed. **Never score the lead constant on an unseeded re-score.**

================================================================================
WHEN IT IS DONE
================================================================================

Append a short entry to the top of "Resume here" in NOTES-local.md saying what was found,
**including the items that came back clean** — an unverified item and a verified-fine item
look identical in a handoff otherwise. Commit per item, not in one lump. Then delete this
file: it is spent.

Do NOT reopen: the 855 "ungraded" reactive presses are 845 wiggle, ungradeable by
construction — do not build a reactive freeze watch. `ROUND_TRIP_MS` stays 60 and the aim
bias stays 3.0. The off-centre sweep question is closed. Merciless Storm needs no gating.
The zone read is fixed.
