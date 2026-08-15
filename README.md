# dbd-skillcheck-moonlight

A macOS fork of [Manuteaa/dbd_autoSkillCheck](https://github.com/Manuteaa/dbd_autoSkillCheck), rebuilt to watch a Dead by Daylight session streamed from a Windows host over [Moonlight](https://moonlight-stream.org/) rather than one running on the same machine.

Upstream is Windows-only, and it assumes the game and the watcher share a computer. Both assumptions break here. Almost everything in this fork follows from that.

This is closer to a measurement rig than a finished bot. It reliably hits wiggle checks and reliably misses Great on repair and heal, for a reason that turns out to be arithmetic.

## Status

Working: window targeting, focus gating, detection, key delivery through the stream, wiggle skill checks.

Broken: repair and heal checks land in **Good**, never **Great**.

We measured the Great zone off the drawn pixels of 13 deliberately missed checks. It is **10.5 degrees wide**. At our median sweep rate of 320 deg/s the needle crosses that band in 33 ms, and across every rate we have recorded, up to the 406 deg/s ceiling Hyperfocus can reach, the window runs 26 to 37 ms. Our keypress-to-pixel round trip is **72 ms**, median of six trials that spanned 8 ms.

The needle has therefore left the zone before the key arrives. No setting recovers those milliseconds. Upstream ships a delay dial (the hit-ante option). Raising it made results worse, which is how we learned that the Great band hugs the *leading* edge of the success zone. The dial was already at the end of its travel.

Pressing early against a predicted position is the only thing left that can work. That tracker is the next piece of work. Everything it rests on has been checked against more than a hundred recorded checks, and the notes below say where those recordings live.

## What differs from upstream

| Area | Upstream | Here |
|---|---|---|
| Platform | Windows, `win32 SendInput` | macOS, pyautogui behind a dispatcher |
| Windows path | the only path | kept intact in `directkeys_win.py` |
| Capture | fixed display region | the Moonlight window, letterbox stripped |
| Crop | fixed pixels | scaled from *content* height |
| Firing | on any Great classification | gated on the stream holding focus |
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
.venv/bin/python tools/autorun.py                  # armed
.venv/bin/python tools/autorun.py --pin-geometry   # freeze the box after the first lock
```

Pin the geometry if the window has ever moved mid-session. Moonlight registers about sixteen windows, one of them a 1280x628 decoy that clears the size floor, and a refresh firing mid Space-transition once latched a menu-bar-inset window that gave a 218 pixel crop instead of 224.

Upstream's Gradio web UI still runs through `python app.py`, with a `moonlight` capture backend added.

Read this twice: EAC can treat injected input as an unfair advantage, and accounts get banned for it. Upstream restricts the tool to private games. This fork does nothing to improve those odds.

## Tools

Thirteen small programs, all of them offline and replayable against recorded frames. None of them need the game running.

| Tool | What it does |
|---|---|
| `autorun.py` | the detect and fire loop, focus-gated |
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
| Needle sweep rate | 291 to 328 deg/s, median 320 (standard play); 214 to 440 on a faster build |
| Angle fit residual | 2.1 to 5.3 deg RMS on our captures, 0.44 to 2.7 on native 60 fps footage |
| Great zone, from pixels | 10.5 deg, range 10.0 to 10.5 across 13 checks |
| Success zone, from pixels | 49.5 deg total, Great at the leading edge in 13 of 13 |

The syscall dominates capture cost, which barely moves with region size. Shrinking the capture region cannot buy frame rate; widening it is nearly free, which is what makes off-centre Doctor checks tractable.

`NOTES-local.md` holds the full record: every measurement, what was ruled out, and the failure modes that produced a confident wrong answer before anyone noticed. Read its "Resume here" section first.

## Things that will bite you

These each cost a day.

**`NSWorkspace.frontmostApplication()` caches and never refreshes** without a Cocoa run loop. It reported iTerm2 for sixty straight seconds while Moonlight was fullscreen and focused. It looks correct in short-lived test scripts and fails in the long-running loop that matters. `focus_watcher.py` queries `CGWindowList` instead.

**A fullscreen app lives on its own macOS Space.** `kCGWindowListOptionOnScreenOnly` only reports the active Space, so fullscreen Moonlight is invisible to that query from another desktop. Window lookup has to use `kCGWindowListOptionAll`.

**The 224 pixel crop is not arbitrary.** It is `224/1080 * content_height`, because the model was trained on checks occupying a fixed fraction of a 1080p frame. Feed it the whole window and the check shrinks to a fifth of its trained height and distorts into an ellipse.

**A hit freezes the needle, and frame-equality will not detect it.** The game stops the needle dead on a successful hit, but the stream encoder keeps jittering pixels, so consecutive frames differ while the angle is bit-for-bit identical. An early attempt tested frame content for equality, never fired, and left frozen tails in the velocity fit; four of thirteen checks then looked like they had non-constant angular velocity. Detect the freeze by the needle failing to advance instead.

**A confident class is not a check detector.** The model returns `repair-heal (out)` at 1.000 confidence on a red-ringed perk icon in the loadout menu, and labels the check-free frames either side of a real check `full black (out)` rather than `None`. Confidence is therefore useless as a filter. The class the model assigns is the only thing standing between a stray perk icon and a wrong keypress, and that is thinner protection than it looks.

**Never measure game geometry through the classifier.** The span of frames labelled `great` gives about 40 degrees, three times the drawn zone, which is wide enough to have wrongly overturned the conclusion in the Status section. That label is a hand-annotated "press about here" cue with margin baked in. Measure pixels.

## Licence and credit

GPL-3.0, inherited from upstream and unchanged. If you redistribute this, in source or compiled form, you carry the same obligations: state that it derives from [Manuteaa/dbd_autoSkillCheck](https://github.com/Manuteaa/dbd_autoSkillCheck), keep the copyright notice, ship the licence, and make the source available under GPL-3.0.

The model, the dataset design, the training code, and the original tool are [Manuteaa's](https://github.com/Manuteaa). This fork only moves it to a different machine and takes a lot of measurements. Their [Discord](https://discord.gg/3mewehHHpZ) is where the upstream project is discussed; questions about this fork belong in this repository's issues.
