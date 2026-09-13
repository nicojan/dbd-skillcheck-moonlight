# Testing the `1234!` outline check

The tracker used to abstain on this check entirely — four of them on 2026-09-11 produced four `NO PRESS` lines and nothing else. As of `1a76e29` it presses on them. Offline it puts 4 of 4 presses inside the drawn arc, but **nothing has been played**, and the one question the recordings physically cannot answer is whether a press inside the arc passes the check. That is what this session is for.

Full mechanism and the measurements behind it: `NOTES-local.md`, top of *Resume here*. Constants and reasoning: the `--- the relocating outline check ---` block in `dbd/utils/needle_tracker.py`.

## The one thing only you can report

**Did the check succeed or fail in the game?** The logs can tell me where the key landed relative to the drawn arc. They cannot tell me what the game did with it — nothing in the capture reads the outcome, and in all four recordings nobody pressed, so there is no recorded success or failure anywhere to calibrate against.

So for each `1234!` check, note whether it **succeeded** (action continues, no explosion, no loud fail) or **failed** (generator explodes / the loud fail sound / progress lost). Even three or four observations settles it. If that is all you bring back, it is enough.

Second question, if you can watch for it: **does the check END when the bot presses, or keep running?** The check lasts 2.90 s and draws four or five arcs. The bot presses once. If one press finishes it, the check clears right after the fire. If it does not, the check runs its full 2.90 s with a single fire in it. I cannot tell these apart from the recordings.

## Before arming

```bash
source ~/.zshrc                 # the stale-shell trap: an old `dbd` silently drops flags
functions dbd | grep -c seed-lead   # must print 1. NOT `type dbd`: see below
cd ~/dev/dbd_autoSkillCheck && git log --oneline -1    # must be 1a76e29 or later
uptime                          # note the 1-minute load, before AND after the match
```

`functions dbd`, not `type dbd`. In zsh `type` prints only where a function lives — `dbd is a shell function from /Users/nicojan/.zshrc` — and never its body, so `type dbd | grep -c seed-lead` returns 0 whether the flag is present or not. That broken form sat in `NOTES-local.md` from 2026-09-01 until it was run here on 09-12 and answered 0 on a shell that had the flag.

The load matters for the *rates*, not for this test. A match above the gate still answers the success/fail question fine; it just cannot be scored against the Great-rate record.

## Step 1 — one dry run first (recommended)

```bash
dbd --dry-run
```

Nothing is pressed. You play the checks yourself as usual, and the log prints `WOULD FIRE` where the bot would have pressed. This confirms detection, the arc read, and the aim at zero risk to your match — and it is the fastest way to find out whether the perk ever draws something the new path does *not* recognise.

## Step 2 — armed

```bash
dbd
```

Recording is already on (`--record`), so every check is captured whether it fires or not. Play normally.

**Keep your hands off the space bar during a `1234!` check.** If you press too, the log cannot tell your press from the bot's, and the landing record becomes unattributable. This is the one place where helping ruins the measurement.

## Reading the log

The armed log is `armed-YYYYMMDD-HHMM.log` in the repo root. A fire is a block of three or four lines starting at the `FIRE`. On an ordinary check the verdict is the `landed` line below it; on this one there is no verdict, for the reason in the table. These are the exact strings, generated from the code rather than typed from memory:

```
FIRE predictive: full black (out) [outline arc 130-241 deg] — +300 deg/s, fit 1.8 deg RMS over 25 frames, aiming 147.9 deg
  timing: frame age 22 ms at decide, lead 0 ms requested / 0 ms slept
  landing: not watched — a relocating arc never freezes, so the tracker stays live for the next one

NO PRESS: full black (out) — needle 88 deg short of the arc; 60 samples over 1500 ms, fit 1.8 deg RMS at +300 deg/s, outline arc 130-241 deg
```

**Several `FIRE` lines in a row on one check is now correct, not a fault.** The check draws a fresh arc every ~650 ms and runs for as long as it runs; each arc is its own press. Since 2026-09-12 the tracker stays live across a fire instead of standing down, so expect roughly one fire per needle revolution rather than one per check.

| What you see | What it means |
|---|---|
| `[outline arc 130-241 deg]` in a `FIRE` line | The new path fired. The bracket is the arc it aimed into, and it appears on nothing else. |
| `landing: not watched` | Expected on every outline fire, and not a complaint. The freeze watch is the instrument that reads where a press landed, and it works by waiting for the needle to stop — which this check never does. Watching anyway cost 800 ms of a check whose arcs live 650 ms, so it is skipped. **The consequence is that the log cannot tell you whether a press landed inside its arc. Only you can.** |
| Several `FIRE` lines seconds apart | One press per arc, which is the intended behaviour. What would be wrong is two fires into the *same* arc — the bracketed range repeating back to back. |
| `NO PRESS: ... already pressed this arc` | The guard that prevents exactly that. Harmless, and expected on the frames right after a fire while the needle is still crossing the arc it was aimed at. |
| `NO PRESS: ... , outline arc 130-241 deg` | It saw the arc and never pressed. The reason field says why — most likely the needle never got inside one in time, which is expected on some checks. |
| `NO PRESS: ... , no zone found` | It never recognised an arc at all — the old behaviour. If that happens on a `1234!` check the arc fell outside the 90-140 deg width the reader accepts, and I will need the frames. |

`full black (out)` in those lines is just what the classifier calls this check; it has no class of its own and does not need one. An ordinary check still says `Great 200-210 deg` in the same slot, so the two are never ambiguous.

## After the match

```bash
cd ~/dev/dbd_autoSkillCheck
uptime

L=$(command ls -t armed-*.log | head -1); echo "$L"
command grep -an -B1 -A4 'outline arc' "$L"   # every outline fire and no-press, with context
command grep -ac 'outline arc' "$L"           # how many the new path saw at all
tail -12 "$L"                                 # the shutdown summary
```

`command ls` and `command grep` are not fussiness. On this Mac `ls` is eza, which reads `-t` as `--time FIELD` and errors out on the glob; and `grep` is ugrep, which skips files listed in `.gitignore` — `armed-*.log` is one, so a bare `grep` can silently find nothing and read as "it never fired". See `~/.claude/docs/machine-truths.md`.

Then send me:

1. **Your success/fail note per check**, with rough timestamps if you have them.
2. The output of the block above.
3. The name of the armed log and the landings file (`landings-*.jsonl`) — I will read them from disk.
4. Both `uptime` readings.

Do **not** run `tools/pull_check_stats.py` without `--peek`: it has no argparse, treats anything that is not `--peek` as a full drain, and there are 654 checks queued behind it.

## What I will not conclude from this

- A single failed press does not condemn `OUTLINE_AIM_DEG`. The arc expires on a ~679 ms timer of its own, so roughly 7% of presses are expected to arrive just after the arc has gone, no matter where they are aimed. That figure is the round trip and no constant in the tracker can reduce it.
- Great-rate numbers from a loaded machine do not go into the record. Outline checks never enter the Great tally in any case — since 2026-09-12 they are not scored at all, and appear in the end-of-match summary only under `no round trip from: N not watched`.
- If the arc is never detected, the answer is frames, not a wider width bound. `frames/bout_*` for that match will have them, and `OUTLINE_MIN_DEG`'s 34 deg of daylight over Merciless Storm is what stops the tracker pressing on a check it is supposed to abstain on.
