# dbd-skillcheck-moonlight

A macOS fork of [Manuteaa/dbd_autoSkillCheck](https://github.com/Manuteaa/dbd_autoSkillCheck), rebuilt to watch a Dead by Daylight session streamed from a Windows host over [Moonlight](https://moonlight-stream.org/) rather than one running on the same machine.

Upstream is Windows-only, and it assumes the game and the watcher share a computer. Both assumptions break here. Almost everything in this fork follows from that.

It began as a measurement rig, because the reason repair and heal checks never landed Great turned out to be arithmetic rather than tuning. It now fires predictively, which is the only thing that can work.

## Status

Working: window targeting, focus gating, detection, key delivery through the stream, wiggle checks, and **predictive firing on sweeping checks**.

Not handled: Merciless Storm, where the tracker deliberately abstains. Off-centre Doctor checks are still outside the capture crop.

We measured the Great zone off the drawn pixels of 13 deliberately missed checks. It is **10.5 degrees wide**, and the same measurement on the 13 hit checks agrees to within half a degree. At our median sweep rate of 320 deg/s the needle crosses that band in 33 ms, and across every rate we have recorded, up to the 406 deg/s ceiling Hyperfocus can reach, the window runs 26 to 37 ms. Our keypress-to-pixel round trip is **72 ms**, median of six trials that spanned 8 ms.

The needle has therefore left the zone before the key arrives. No setting recovers those milliseconds. Upstream ships a delay dial (the hit-ante option). Raising it made results worse, which is how we learned that the Great band hugs the *leading* edge of the success zone. The dial was already at the end of its travel.

So the bot no longer presses when it sees a Great. It locates the ring, reads the Great band out of the drawn pixels, fits the sweep as a straight line, and schedules the key so the needle *arrives* in the band 72 ms later. `dbd/utils/needle_tracker.py` is the whole of it.

### What it scores

`tools/replay_tracker.py` runs the live tracker frame by frame over every recorded check on disk and scores the press against ground truth measured from the whole check — the needle's fitted position when the key lands, versus the Great band read off the pixels. Nothing in the replay can see the future.

| set | checks | result | worst error |
|---|---|---|---|
| `recordings_missed` (unhit) | 15 | 15 Great | 2.4 deg |
| `recordings` (hit) | 13 | 13 Great | 3.3 deg |
| `oppression.mp4` (native 4K, 363 deg/s) | 1 | 1 Great | 1.9 deg |
| Merciless Storm, two clips | 29 | abstains on all 29 | — |

The Great band is ±5.25 deg about its centre, so that is the error budget. Drop two frames in three and it still scores 15 and 12; the approach is not short of frames.

For scale, the same code can grade the *player's* presses, because a hit freezes the needle and that frozen angle is where the press landed:

```bash
.venv/bin/python tools/replay_tracker.py recordings --human
```

Ten scorable human presses: **4 Great, 6 good, and every single one late**, median 9.0 deg past the centre of the band. That is the arriving-late diagnosis measured directly, without reference to any latency figure.

What it *is* sensitive to is the 72 ms constant. Mis-state it by 10 ms and Greats fall to 11 of 15; by 20 ms and they fall to 2. Re-run `tools/measure_latency.py` after any change to the network path, the host, or Moonlight's settings, and pass the result as `--round-trip-ms`. The two directions are not symmetric — Great sits at the leading edge of the success zone, so firing late spills into Good while firing early misses the zone entirely — so the aim sits one degree late by design.

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

Every predictive shot logs the fitted rate, the fit residual, the frame count behind it, and the angle it aimed at. It then reads where the needle froze and grades itself Great, good or miss on the spot, so accuracy is a measurement rather than an inference from the end-of-match tally.

Pin the geometry if the window has ever moved mid-session. Moonlight registers about sixteen windows, one of them a 1280x628 decoy that clears the size floor, and a refresh firing mid Space-transition once latched a menu-bar-inset window that gave a 218 pixel crop instead of 224.

### The armed run

Everything above is verified offline and in dry-run. The one claim no amount of replay can settle is whether a self-reported `GREAT` matches what the game actually awarded, and that needs an armed match. Private games only — see the warning below.

**Before starting.** Re-measure the latency, because it is the one input the whole thing is sensitive to and it moves with the network, the host and Moonlight's settings:

```bash
.venv/bin/python tools/measure_latency.py
```

If the median is not close to 72 ms, pass the real figure as `--round-trip-ms`. A 10 ms error costs about a quarter of the Greats; 20 ms costs nearly all of them.

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

The class on the first line is normally `(out)`, not `(great)`. That is the design working: it commits about 72 ms before the needle reaches the band, while the model still calls it out.

**What to compare.** The bot's verdict against the game's own feedback, check by check. The offline replay says 29/29 Great; anything much below that armed means the latency figure is wrong, and the *direction* says which way — `good` verdicts with a positive error mean it is arriving late, so the true round trip is longer than the number it was given.

**When to stop and re-measure rather than push on:**

- `landing: needle still sweeping after the press` on repair or heal checks. The press is not connecting at all — check the Accessibility grant for the terminal you launched from, since keys go nowhere silently without it.
- `HIT reactive: … tracker stood down` appearing often. The tracker is not finding a zone; the framing is likely wrong.
- `WARNING geometry changed`. Restart with `--pin-geometry`.
- Verdicts that disagree with the game. Stop and re-run `measure_latency.py` before collecting more.

Read this twice: EAC can treat injected input as an unfair advantage, and accounts get banned for it. Upstream restricts the tool to private games. This fork does nothing to improve those odds.

Upstream's Gradio web UI still runs through `python app.py`, with a `moonlight` capture backend added.

Read this twice: EAC can treat injected input as an unfair advantage, and accounts get banned for it. Upstream restricts the tool to private games. This fork does nothing to improve those odds.

## Tools

Seventeen small programs, all but the runner offline and replayable against recorded frames. None of them need the game running.

| Tool | What it does |
|---|---|
| `autorun.py` | the detect and fire loop, focus-gated, predictive |
| `replay_tracker.py` | runs the tracker over recorded checks and scores where each press lands |
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
| Keypress to pixel | 72 ms median |
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
