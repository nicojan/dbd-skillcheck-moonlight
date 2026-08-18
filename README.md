# dbd-skillcheck-moonlight

A macOS fork of [Manuteaa/dbd_autoSkillCheck](https://github.com/Manuteaa/dbd_autoSkillCheck), rebuilt to watch a Dead by Daylight session streamed from a Windows host over [Moonlight](https://moonlight-stream.org/) rather than one running on the same machine.

Upstream is Windows-only, and it assumes the game and the watcher share a computer. Both assumptions break here. Almost everything in this fork follows from that.

It began as a measurement rig, because the reason repair and heal checks never landed Great turned out to be arithmetic rather than tuning. It now fires predictively, which is the only thing that can work.

## Status

Working: window targeting, focus gating, detection, key delivery through the stream, wiggle checks, and **predictive firing on sweeping checks — verified armed, over seven real matches on 2026-08-16 and 2026-08-17**: 202 gradeable fires, **170 Great (84%)**, 24 good, 8 miss, and every press accounted for.

**No presses are being lost.** An earlier version of this section claimed about a quarter never reached the game. That was wrong, and it was our own instrument: the freeze watch judged the tail of everything it saw against an absolute brightness floor, while the stray red left behind by a cleared check clears that floor. A landing that froze and then cleared inside the watch window therefore reported `still sweeping` — the same line a genuinely lost press produces. Once the watch was fixed to judge the contiguous lit block against the check's own peak, **32 of 32 fires in the next match produced a readable landing**, and 33 more since. There was never a delivery problem.

**The remaining loss is link jitter, and it is not ours to fix.** Round trip over 56 armed fires runs 39-120 ms against a median of 56, which is about 3.7 degrees of scatter at the needle — against a Great half-band of 5.25. That alone sets the ceiling near 90%, which is roughly where it lands. The fit contributes under a degree, so better prediction cannot buy another Great; only a quieter link can.

**That percentage is precision, not recall.** Until 2026-08-17 the bot could only count checks it fired on: a tracked check that `decide` gave up on — too few samples, a fit that never tightened, a rate out of range — fell out of the loop writing nothing, so a match that lost six checks logged the same as one that saw six. It now prints a `NO PRESS` line per dropped check with the reason behind it, and tallies them at exit. A check the classifier never labels at all is still invisible, and no logging change fixes that without ground truth.

**Where the misses actually are: short tracks.** Split by how many frames the fit had, over 181 landings with two corrupt-zone outliers removed — 15 frames or fewer scores 70% Great and holds four of the five misses; 16-25 frames scores 79% with none; over 25 frames scores 81% at 3.66 degrees of scatter. At the ~37 fps the loop captures at, a full sweep is about 40 frames, so an 11-frame fit means the check was picked up some 700 ms late. That is a detection-latency problem, and it is the open one.

Not handled: Merciless Storm, where the tracker deliberately abstains. Off-centre Doctor checks are still outside the capture crop. `full white` checks are fired at but **not graded** — that type draws its whole success zone as one solid block, so there is no Great band to measure and any landing inside would score Great by construction.

We measured the Great zone off the drawn pixels of 13 deliberately missed checks. It is **10.5 degrees wide**, and the same measurement on the 13 hit checks agrees to within half a degree. At our median sweep rate of 320 deg/s the needle crosses that band in 33 ms, and across every rate we have recorded, up to the 406 deg/s ceiling Hyperfocus can reach, the window runs 26 to 37 ms. Our keypress-to-pixel round trip is **about 56 ms**, median over 56 armed fires, ranging 39 to 120.

The needle has therefore left the zone before the key arrives. No setting recovers those milliseconds. Upstream ships a delay dial (the hit-ante option). Raising it made results worse, which is how we learned that the Great band hugs the *leading* edge of the success zone. The dial was already at the end of its travel.

So the bot no longer presses when it sees a Great. It locates the ring, reads the Great band out of the drawn pixels, fits the sweep as a straight line, and schedules the key so the needle *arrives* in the band a round trip later. `dbd/utils/needle_tracker.py` is the whole of it.

### What it scores

`tools/replay_tracker.py` runs the live tracker frame by frame over every recorded check on disk and scores the press against ground truth measured from the whole check — the needle's fitted position when the key lands, versus the Great band read off the pixels. Nothing in the replay can see the future.

| set | checks | result | worst error |
|---|---|---|---|
| `recordings_missed` (unhit) | 15 | 12 Great, 2 ungraded, 1 no fire | 1.6 deg |
| `recordings` (hit) | 13 | 12 Great, 1 no fire | 2.9 deg |
| `oppression.mp4` (native 4K, 363 deg/s) | 1 | 1 Great | 1.9 deg |
| Merciless Storm, two clips | 29 | abstains on all 29 | — |

The Great band is ±5.25 deg about its centre, so that is the error budget. Drop two frames in three and it barely moves; the approach is not short of frames.

An earlier version of this table read 15/15 and 13/13. **Neither number is reproducible, for two separate reasons, and both are worth stating rather than quietly restating.** The two `ungraded` entries are `full white` checks that were never gradeable — that type draws its zone as one solid block, so the measured "Great band" comes back 58 degrees and every landing inside it scored Great automatically. The two `no fire` entries appeared when the aim bias was removed (see below): an earlier target is an earlier deadline, so on those two checks the tracker has to commit before its fit has settled, and the fit-quality gate declines. That trade was accepted deliberately — it costs two checks offline and, across 33 armed fires, cost none.

For scale, the same code can grade the *player's* presses, because a hit freezes the needle and that frozen angle is where the press landed:

```bash
.venv/bin/python tools/replay_tracker.py recordings --human
```

Ten scorable human presses: **4 Great, 6 good, and every single one late**, median 9.0 deg past the centre of the band. That is the arriving-late diagnosis measured directly, without reference to any latency figure.

What it *is* sensitive to is the round-trip constant. Mis-state it by 10 ms and Greats fall to 11 of 15; by 20 ms and they fall to 2. The default is now **60 ms**, and the armed loop measures its own round trip per check and adapts the lead toward it, so the constant matters mainly at the start of a session.

**Do not trust `measure_latency.py` over the armed number.** It presses at a text field on the host, in its own process, with the detector not running — it reported 126.5 ms on the same evening the armed loop was landing at 59. Every predictive fire logs `round trip NNN ms measured`, which is the closed-loop figure under real load against the game itself. Prefer it whenever the two disagree.

The two directions of error are not symmetric — Great sits at the leading edge of the success zone, so firing late spills into Good while firing early misses the zone entirely. The aim therefore used to sit one degree late on purpose. **It no longer does**: that bias was swept against zero-jitter replays, and the first match to record per-check landings came back a mean +1.3 degrees *late* already, so the margin was being bought on the side that was not failing. At zero the aim is centred — mean landing error **−0.41 deg ±0.88 over 33 fires**, against +1.30 ±0.95 before.

## What differs from upstream

| Area | Upstream | Here |
|---|---|---|
| Platform | Windows, `win32 SendInput` | macOS, pyautogui behind a dispatcher |
| Windows path | the only path | kept intact in `directkeys_win.py` |
| Capture | fixed display region | the Moonlight window, letterbox stripped |
| Crop | fixed pixels | scaled from *content* height |
| Firing | on any Great classification | predicted, scheduled to land in Great; gated on focus |
| Purpose | play the game | measure it, then play it |

The trained model, its eleven-class taxonomy, and the whole training pipeline under `dbd/` are upstream's work, untouched. Three upstream Python files carry edits. The other 3,600 lines are new.

## Setup

You need Python 3.12. System Python on recent macOS is 3.14, which is too new for the torch path.

```bash
git clone https://github.com/nicojan/dbd-skillcheck-moonlight
cd dbd-skillcheck-moonlight
git lfs pull
python3.12 -m venv .venv
.venv/bin/pip install numpy mss onnxruntime pyautogui IPython pillow gradio opencv-python pyobjc-framework-ApplicationServices
```

Run `git lfs pull` even if cloning seemed to work. `models/model.onnx` is a Git LFS object, and a plain clone leaves you with a 132-byte pointer file that fails at load time with `INVALID_PROTOBUF`. This catches everyone once.

Grant Accessibility permission to whichever terminal you launch from; that is what lets the process inject keys. Without it they go nowhere, and nothing reports an error. Switch terminals and you need a fresh grant.

Training dependencies (torch, torchvision, pytorch-lightning, torchmetrics, about 2.5 GB) are deliberately absent. Nothing in the capture, inference, or keypress path touches them.

## Running it

Start Moonlight and make it fullscreen. Then:

```bash
.venv/bin/python tools/autorun.py --dry-run        # detect and log, press nothing
.venv/bin/python tools/autorun.py                  # armed, predictive
.venv/bin/python tools/autorun.py --pin-geometry   # freeze the box after the first lock
.venv/bin/python tools/autorun.py --no-predict     # upstream's reactive behaviour
```

Every predictive shot logs the fitted rate, the fit residual, the frame count behind it, and the angle it aimed at. It then reads where the needle froze and grades itself Great, good or miss on the spot, so accuracy is a measurement rather than an inference from the end-of-match tally. A check that is tracked and then given up on — too few samples, a fit that never tightened — prints a `NO PRESS` line saying so, so the log carries the checks it lost as well as the ones it hit. Each fire is also written to `landings-<timestamp>.jsonl` with its raw readings; read a match back with:

```bash
.venv/bin/python tools/read_landings.py landings-20260816-214940.jsonl
```

**A shell shortcut, if you run this often.** Drop this in `~/.zshrc` — it starts the stream, waits for it to settle, launches the game on the host, then runs the bot in the foreground of that terminal, which is where the Accessibility grant lives:

```zsh
dbd() {
  local repo="$HOME/dev/dbd_autoSkillCheck" host="${DBD_HOST:-<host>}"
  nohup moonlight stream "$host" "Steam Big Picture" >/dev/null 2>&1 & disown
  sleep "${DBD_WAIT:-30}"
  ssh -n "$host" 'export DISPLAY=:0 XDG_RUNTIME_DIR=/run/user/1000 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
    XAUTHORITY=$(ls /run/user/1000/.mutter-Xwaylandauth.* 2>/dev/null | head -1); \
    setsid steam "steam://rungameid/381210" >/tmp/dbd-launch.log 2>&1 </dev/null &'
  cd "$repo" || return 1
  .venv/bin/python tools/autorun.py "$@" 2>&1 | tee "armed-$(date +%H%M).log"
}
```

Three legs, in that order: stream the host's Big Picture session to a window here, launch the game on the host so it renders into that stream, then run the bot. The game leg goes over ssh because the host's Sunshine exposes no app entry for the game — only Desktop and Big Picture — so it cannot be streamed directly; the env block is what makes the already-running Steam client findable from a non-login shell. Do **not** guard the stream behind `pgrep -qx Moonlight`: any already-open Moonlight window, including a plain GUI one, then makes the function skip the stream and sit there doing nothing. A second `moonlight stream` hands off to the running instance rather than opening a duplicate, so the guard buys nothing.

Foreground and *that* terminal both matter: a detached or backgrounded run injects nothing and reports no error. Do not add `--pin-geometry` here — startup resolves before Moonlight is fullscreen, and the resume path self-corrects.

Pin the geometry if the window has ever moved mid-session. Moonlight registers about sixteen windows, one of them a 1280x628 decoy that clears the size floor, and a refresh firing mid Space-transition once latched a menu-bar-inset window that gave a 218 pixel crop instead of 224.

### The armed run

Everything above is verified offline and in dry-run. The one claim no amount of replay can settle is whether a self-reported `GREAT` matches what the game actually awarded, and that needs an armed match. Private games only — see the warning below.

**Before starting.** The lead now adapts to the round trip the loop measures for itself, so there is no longer a calibration step to run first. `measure_latency.py` still exists, but treat it as a rough sanity check rather than the number to pass — it measures an idle process against a host text field, and has been wrong by a factor of two against the armed loop. If you do want to seed the starting lead, pass `--round-trip-ms`; it will drift toward the truth within a few checks anyway.

Then confirm the framing is right before arming, and keep the log:

```bash
.venv/bin/python tools/autorun.py --dry-run                       # ~1 min, confirm detections
.venv/bin/python tools/autorun.py 2>&1 | tee armed-$(date +%H%M).log
```

Add `--pin-geometry` if the geometry warning has ever appeared.

**What a good shot looks like.** Two lines per check — what it decided, then what actually happened:

```
FIRE predictive: repair-heal (out) — +327 deg/s, fit 2.5 deg RMS over 32 frames, aiming 300.0 deg
  landed 301.4 deg — GREAT, +1.4 deg from Great centre
```

The class on the first line is normally `(out)`, not `(great)`. That is the design working: it commits a full round trip before the needle reaches the band, while the model still calls it out.

**What to compare.** The bot's verdict against the game's own feedback, check by check. The offline replay scores 24 Great with nothing worse than a no-fire; a shortfall made up of `good` verdicts means the latency figure is wrong, and the *direction* says which way — a positive error means it is arriving late, so the true round trip is longer than the number it was given. A shortfall made up of *misses*, or of checks with no readable landing at all, is a different failure and is not about latency: see "When nothing lands at all" below.

**When to stop and re-measure rather than push on:**

- `landing: needle still sweeping after the press`, or `landing: needle gone before it could be read`. Neither message distinguishes "pressed and missed" from "never pressed", so do not read either as a timing problem. See "When nothing lands at all" below.
- `HIT reactive: … tracker stood down` appearing often. The tracker is not finding a zone; the framing is likely wrong.
- `WARNING geometry changed`. Restart with `--pin-geometry`.
- Verdicts that disagree with the game. Stop and re-run `measure_latency.py` before collecting more.

**When nothing lands at all.** If no check ever produces a readable `landed NNN deg` line, the press is not reaching the game *when it was aimed to*, and no amount of `--round-trip-ms` tuning will help. A latency error cannot produce outright misses anyway: replay at believed 130 ms gives Good, not misses, until true latency reaches ~250 ms, and all-misses needs ~300 ms. Work down this ladder instead:

1. **Read the `timing:` line.** Every predictive fire now logs the age of the frame the fit was built on, the lead it asked for, and the lead it actually slept. If requested and slept disagree, the fault is in this process and nothing further down the ladder matters. This is what caught the unit bug below.
2. **Read the `round trip NNN ms measured` line.** `report_landing` times how long the freeze took to appear in our own capture, which is the closed-loop latency under real armed-run load. `measure_latency.py` measures the same quantity idle, in its own process, against a host text field — when the two disagree, the armed number is the one describing the run. If the measured round trip is far above what was assumed, re-measure; if it swings check to check, the fault is the stream and no constant will fix it.
3. **Run it from a plain terminal window.** Not through Claude Code — its Bash tool is sandboxed, so CGEvent injection is silently dropped, and its `!` prompt is killed at 120 s, mid-match.
4. **Check the press hold.** `PRESS_HOLD_SECONDS` must stay well above one host frame period. It was 5 ms until 2026-08-15, which a desktop text field registers fine — key events are queued, so duration is irrelevant — while a game polling input once per rendered frame (16.7 ms at 60 fps) can miss it entirely, especially as Moonlight batches the press and release over the network. **No dry run can catch this**, because `fire()` returns before pressing when `--dry-run` is set.
5. **Isolate delivery** with `test_keypress.py --presses 5`, with the host on something a spacebar visibly changes. It holds SPACE for 50 ms, so it can pass where a shorter hold fails.
6. **Compare against reactive.** `autorun.py --no-predict` presses with no lead and should land in Good. If reactive scores and predictive does not, the problem is in the predictive path alone — as it turned out to be.

**The bug this ladder was built to find, for the record.** `fire()` took its wait in *seconds* while the predictive call site passed `decision.press_at_ms - now_ms` in *milliseconds*. Every predictive press therefore slept 1000x too long — a 20 ms lead became 20 seconds. It survived four armed matches because nothing else could see it: `--dry-run` returns before the sleep, the offline replay never sleeps, the reactive path passes 0, and the symptom it produced armed was `needle gone before it could be read`, which reads like a *detection* fault. The one check that ever produced a landing was the one whose remaining lead happened to be a fraction of a millisecond. `fire()` now takes milliseconds, says so in the parameter name, and `test_landing_report.py` pins the unit.

Upstream's Gradio web UI still runs through `python app.py`, with a `moonlight` capture backend added.

Read this twice: EAC can treat injected input as an unfair advantage, and accounts get banned for it. Upstream restricts the tool to private games. This fork does nothing to improve those odds.

## Tools

Eighteen small programs, all but the runner offline and replayable against recorded frames. None of them need the game running.

| Tool | What it does |
|---|---|
| `autorun.py` | the detect and fire loop, focus-gated, predictive |
| `replay_tracker.py` | runs the tracker over recorded checks and scores where each press lands |
| `read_landings.py` | reads a match's `landings-*.jsonl` back: Great rate, landing bias and spread, round trip, and the raw readings behind any fire that produced no verdict |
| `test_needle_tracker.py` | unit tests for the tracker's logic, including the reversed-check path |
| `test_landing_report.py` | tests the armed-only self-scoring path, which no dry-run reaches |
| `test_continuous_check.py` | replays a 21 s Merciless Storm check unbroken; covers the buffer caps and the freeze loop |
| `calibrate_window.py` | draws the capture box on a frame so you can see the framing |
| `measure_latency.py` | keypress to pixel round trip |
| `test_keypress.py` | isolates whether synthetic keys reach the host at all |
| `record_checks.py` | clean 224-pixel frame sequences of individual checks |
| `record_frames.py` | full frames at 30 fps with no inference, for offline work |
| `analyse_needle.py` | needle angle per frame, and how constant the sweep rate is |
| `measure_zone.py` | Great and Good widths straight from the drawn pixels |
| `sweep_rates.py` | per-check rate and fit quality across a whole session |
| `scan_frames.py` | offline tile sweep, for finding checks outside the centre crop |
| `ingest_video.py` | turns a downloaded gameplay clip into the same dataset format |
| `wide_scan.py` | slides a 224 window over a frame to find where checks appear |
| `prune_frames.py` | deletes frames far from any detected check |

`ingest_video.py` earns its place. Five live sessions produced no Doctor and no Merciless Storm, so those game states stayed unmeasured for a week. One downloaded clip of another player, rescaled and ingested, produced four Madness checks and four counter-clockwise ones on the first run. Downloaded footage also fits far cleaner than our own, 0.44 to 2.7 deg RMS against 2.1 to 5.3, because no stream encoder sits in the path.

## What we measured

Numbers are from a 2560x1080 ultrawide, Moonlight fullscreen, the stream pillarboxed to 1920x1080.

| Quantity | Value |
|---|---|
| Capture (`mss`) | 21.9 ms per frame, about 90% of the frame budget |
| Inference | 2.0 to 2.5 ms per frame |
| Live throughput | 34 fps (upstream targets 120) |
| Keypress to pixel | **56 ms median** armed (39-120, sigma 12-15); an idle standalone measurement of the same thing said 126.5 and was wrong |
| Capture cost, 224 px vs full frame | 22.2 ms vs 26.9 ms, so 4.7 ms buys 41 times the pixels |
| Needle sweep rate | 294 to 328 deg/s, median 325 (standard play); 214 to 440 on a faster build |
| Angle fit residual | 1.9 to 2.8 deg RMS on our captures, 0.44 to 2.7 on native 60 fps footage |
| Great zone, from pixels | 10.5 deg, range 10.0 to 10.5 across 13 checks |
| Success zone, from pixels | 49.5 deg total, Great at the leading edge in 13 of 13 |

The syscall dominates capture cost, which barely moves with region size. Shrinking the capture region cannot buy frame rate; widening it is nearly free, which is what makes off-centre Doctor checks tractable.

`NOTES-local.md` holds the full record: every measurement, what was ruled out, and the failure modes that produced a confident wrong answer before anyone noticed. Read its "Resume here" section first.

## Things that will bite you

These each cost a day.

**`NSWorkspace.frontmostApplication()` caches and never refreshes** without a Cocoa run loop. It reported iTerm2 for sixty straight seconds while Moonlight was fullscreen and focused. It looks correct in short-lived test scripts and fails in the long-running loop that matters. `focus_watcher.py` queries `CGWindowList` instead.

**A fullscreen app lives on its own macOS Space.** `kCGWindowListOptionOnScreenOnly` only reports the active Space, so fullscreen Moonlight is invisible to that query from another desktop. Window lookup has to use `kCGWindowListOptionAll`.

**The 224 pixel crop is not arbitrary.** It is `224/1080 * content_height`, because the model was trained on checks occupying a fixed fraction of a 1080p frame. Feed it the whole window and the check shrinks to a fifth of its trained height and distorts into an ellipse.

**A hit freezes the needle, and frame-equality will not detect it.** The game stops the needle dead on a successful hit, but the stream encoder keeps jittering pixels, so consecutive frames differ while the angle is bit-for-bit identical. An early attempt tested frame content for equality, never fired, and left frozen tails in the velocity fit; four of thirteen checks then looked like they had non-constant angular velocity. Detect the freeze by the needle failing to advance instead — and not by "did not move at all", because a frozen needle wobbles half a degree with quantisation, which resets a strict stall counter and lets a 200 ms tail through anyway. The bar has to be a fraction of the check's own median step. Fixing that alone tightened the measured rate spread from 290-347 to 294-328 deg/s, halved the fit residual, and removed the one fit in the Hyperfocus session that had been dismissed as junk.

**A confident class is not a check detector.** The model returns `repair-heal (out)` at 1.000 confidence on a red-ringed perk icon in the loadout menu, and labels the check-free frames either side of a real check `full black (out)` rather than `None`. Confidence is therefore useless as a filter. The class the model assigns is the only thing standing between a stray perk icon and a wrong keypress, and that is thinner protection than it looks.

**Never measure game geometry through the classifier.** The span of frames labelled `great` gives about 40 degrees, three times the drawn zone, which is wide enough to have wrongly overturned the conclusion in the Status section. That label is a hand-annotated "press about here" cue with margin baked in. Measure pixels.

## Licence and credit

GPL-3.0, inherited from upstream and unchanged. If you redistribute this, in source or compiled form, you carry the same obligations: state that it derives from [Manuteaa/dbd_autoSkillCheck](https://github.com/Manuteaa/dbd_autoSkillCheck), keep the copyright notice, ship the licence, and make the source available under GPL-3.0.

The model, the dataset design, the training code, and the original tool are [Manuteaa's](https://github.com/Manuteaa). This fork only moves it to a different machine and takes a lot of measurements. Their [Discord](https://discord.gg/3mewehHHpZ) is where the upstream project is discussed; questions about this fork belong in this repository's issues.
