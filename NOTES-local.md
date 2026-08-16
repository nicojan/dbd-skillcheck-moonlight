# Local macOS / Moonlight fork — working notes

Fork of [Manuteaa/dbd_autoSkillCheck](https://github.com/Manuteaa/dbd_autoSkillCheck) adapted to run on macOS against a game streamed from a Windows host over Moonlight. Local, offline testing build.

Upstream is Windows-only and assumes the game runs on the same machine it is watching. Both assumptions are false here, which is what most of the work below addresses.

## Setup

```bash
cd /Users/nicojan/dev/dbd_autoSkillCheck
.venv/bin/python tools/autorun.py --dry-run     # detect and log, press nothing
```

- Python 3.12 venv at `.venv` (system Python is 3.14, too new for the torch path).
- Deps: `numpy mss onnxruntime pyautogui IPython pillow gradio opencv-python` plus `pyobjc-framework-ApplicationServices`.
- `git-lfs` installed via Homebrew. **`models/model.onnx` is an LFS file** — a fresh clone gets a 132-byte pointer and fails with `INVALID_PROTOBUF`. Fix: `git lfs pull`.
- Training deps (torch, torchvision, pytorch-lightning, torchmetrics, ~2.5 GB) are **not** installed. Nothing in the capture → inference → keypress path needs them; they are only required to retrain.
- Accessibility permission is granted to iTerm2, which is what allows synthetic key injection. A different terminal would need its own grant.

## Status

Working: window targeting, focus gating, detection, key delivery through Moonlight, wiggle skill checks, and — as of 2026-08-15 23:20 — **predictive firing armed, in a real match**: 5 GREAT, 1 good, 0 MISS across six scored landings at `--round-trip-ms 60`. That is the claim no amount of replay could settle, and it is now settled.

Two things remain open, in order of what they cost. **A quarter of presses never register** — 2 of 8 that session, 2 of 6 the session before, all reported as `still sweeping`, meaning the key did not reach the game at all. That is now the largest single loss, larger than any timing error. And **the two round-trip estimates disagree**: the fit-derived figure spread 46-91 ms while the tail read sat at 79 +/- 2 over the same six checks. The fit-derived one tracks the landing error at 3.1 ms/deg, which is exactly 1/rate — so it is the landing error restated, not an independent measurement. Which of the two is real decides whether the link jitters by 45 ms (near the ceiling, stop tuning) or by 4 (the scatter is ours, and there is headroom).

Not working: Merciless Storm (deliberate abstention), off-centre Doctor checks (outside the capture crop).

**The reason is now settled, and it is not a tuning problem.** The Great band is 10-20 degrees wide and the needle sweeps at 327-609 deg/s, so the window is **16-61 ms** — always *narrower* than the ~72 ms keypress-to-pixel round trip. Reacting to a Great cannot work, in any configuration, because the needle has already left by the time the key lands. `--hit-ante` was never going to fix it. The only path to Great is to **predict** where the needle will be and press early.

Everything predictive firing depends on has been verified against **75 real skill checks across three builds** (48 as of 2026-08-11, plus 27 Hyperfocus checks on 2026-08-12). It is a build task with no research risk remaining.

## Resume here

Read this section first; it is the state as of 2026-08-15.

**2026-08-15 (evening): the predictive tracker is built, validated offline and confirmed firing live.** `dbd/utils/needle_tracker.py` holds the logic, `tools/replay_tracker.py` scores it against every recorded check, `tools/test_needle_tracker.py` covers what the recordings cannot. `autorun.py` fires predictively by default; `--no-predict` restores upstream's reactive behaviour.

Replayed frame by frame, with ground truth measured separately from the whole check:

| set | checks | result | worst error |
|-----|--------|--------|-------------|
| `recordings_missed` | 15 | **15 Great** | 2.4 deg |
| `recordings` | 13 | **13 Great** | 3.3 deg |
| `recordings_video/oppression` (native 4K, 363 deg/s) | 1 | **Great** | 1.9 deg |
| Merciless Storm, both clips | 29 | **abstains on all 29** | — |

Live dry-run against a real match fired on two checks: +327 deg/s at 2.5 deg RMS over 32 frames, +352 at 2.1 over 17. The live loop gets *more* frames than the recordings do, so the fit is not frame-starved.

**Four bugs were found and fixed on the way, three of them in code that already existed.** Each produced a plausible number rather than an error:

- **A circular run search inside a non-circular slice.** Looking for the solid Great band within the zone's own samples wrapped from the trailing end round to the leading end and joined them into one phantom band, reporting Great about a zone-width late. It landed the press ~50 deg early on 3 of 13 checks. `measure_zone.py` has the same construction and is now fixed too — it does not fire at that tool's 0.5 deg sampling, only at the 1.0 deg the tracker uses, which is exactly the kind of latency that survives a review.
- **The frozen-tail trim was defeated by quantisation.** It required two consecutive frames of zero advance, and a frozen needle wobbles half a degree either way, which resets the counter. A 200 ms frozen tail therefore reached the fit and pulled `recordings_missed/check_005` from 325 to 293 deg/s. The bar is now a fraction of the check's own median step. **`analyse_needle.py` has the same weakness and its published rate spreads are affected** — the 290-347 deg/s figure for standard play is inflated at the bottom end; the trimmed set sits at 314-327.
- **Two of the 22 "deliberately unhit" checks were in fact hit.** `check_005` and `check_006` freeze at 211 and 145 deg, inside Good. They are still usable for zone geometry, which is why this went unnoticed, but they are not clean unhit sweeps.
- **Live frames are RGB and recorded frames are BGR.** `grab_screenshot()` returns RGB; `cv2.imread` returns BGR. The needle test is `R - max(G,B)`, so feeding it a live frame unconverted measures blueness and finds nothing. Nothing had exercised that path before, because every needle measurement to date ran offline.

**`check_009/010/019` failing the ring-centre fit is explained, and the handoff's hypothesis was wrong.** They are not the short recordings: `check_011` and `check_020` are the same 51 frames and fit fine. They are the only three checks in the set whose recording is *entirely* check — 51 lit frames out of 51, no needle-free frames at either end. The static-UI median is taken with the needle masked out, so a recording with no needle-free frames leaves parts of the base ring permanently masked, the ring stops being a full circle, and the centre peak drops to 61-82 against the 145 a good fit gives. All three are wiggle, which takes the reactive path regardless.

**`measure_zone.py` on the hit set: no problem.** The handoff flagged this as unknown — frozen tails might have broken the median-over-frames assumption. They do not. The 13 repair-heal checks in `recordings` give Great 10.0-11.0, median **10.5**, matching `recordings_missed` exactly. The load-bearing constant now has two independent datasets under it.

**What the tracker is sensitive to is the 72 ms round trip, not the frame rate.** Dropping two frames in three still scores 15 and 12 Greats. Mis-stating the latency by 10 ms drops the unhit set to 11 of 15; by 20 ms, to 2 of 15. Re-measure with `measure_latency.py` after any change to the network, the host or Moonlight's settings and pass `--round-trip-ms`. The two directions are not symmetric: Great is at the leading edge of the success zone, so firing late spills into Good while firing early misses the zone outright — hence `AIM_BIAS_DEG = 1.0`, which aims one degree late on purpose. The sweep behind that value is in the constant's comment.

**2026-08-15 (later): the freeze-trim fix was ported to `analyse_needle.py`, and it moves published numbers.** `sweep_rates.py` imports the trim from there, so both tools move together — corrected figures below. Every change is in the same direction: the old numbers were inflated by frozen tails that the strict stall test let through.

| set | was | now |
|-----|-----|-----|
| `recordings` rate spread | 290-347 deg/s, RMS 2.8 median | **294-328**, RMS **2.2** |
| `recordings_missed` rate spread | — | **319-329**, RMS ~1.9 |
| Hyperfocus session rate spread | 219-362, median 318, RMS 3.9 | **293-368**, median **325**, RMS **2.0** |
| fit error as % of required lead | 16% median | **9-10%** median |
| implied timing error | ~12 ms | **~6 ms** |

Two documented claims change as a result:

- **"One fit in 27 is junk (check 26, RMS 11.84) and should be excluded by an RMS threshold" is obsolete.** That fit was a frozen tail. No check in the session now exceeds 2.84 RMS, and the outlier-exclusion advice is no longer needed.
- **The Hyperfocus staircase is confirmed and got *cleaner*.** Checks 5-8 (gaps 5.9, 5.9, 4.0 s) now read 328.6 → 340.1 → 353.9 → 367.7, i.e. +3.5%, +4.1%, +3.9% against a predicted +4% — errors of -0.5%, +0.1%, -0.1%, against the -1.1% to -2.3% previously recorded. A correction that tightens an independent prediction it was not aimed at is worth more than the measurement itself.

Note the direction of the old error: it made the sweep look *more* variable and *less* linear than it is. Both were arguments for the tracker, so nothing built on them was wrong — but "rate varies between checks" rests on the Hyperfocus staircase and the fast build, not on the spread within a single standard-play set, which is now tight enough (319-329) that the tool says so itself.

**2026-08-15 (later still): the armed-only path is hardened, and the human baseline is measured.** Five things fixed that dry-run could never have exposed, because none of them are on the dry-run path:

- **Unbounded frame retention.** `TrackerState` kept every frame of a check. A discrete check is a second, but Merciless Storm is one continuous check running 20 s that the tracker abstains on — so nothing ever cleared the buffer. 680 frames at 224x224x3 is 100 MB, climbing for the length of the match. Frames are now capped at 24 and samples at 60, with a separate `seen` counter so the retry cadence does not break when the caps bite.
- **The reactive fallback could fire on a static needle.** It required only "a fit exists", and a fit exists for a needle that is not moving. A menu with a confident class is exactly that — the loadout perk icon classifies `repair-heal (out)` at 1.000. The fallback now requires `may_react`, which `decide` sets only when a real sweep is present and the press merely cannot be scheduled.
- **`report_landing` assumed the last angle it saw was a freeze.** The needle only stops if the press connected; on a miss it sweeps straight on, and the function would have reported a confident verdict about a position the press had nothing to do with. Extracted as `freeze_angle`, which requires the last three reads to agree to within the wobble.
- **`fired_at_ms` was a dead field.** `decide` checked it and nothing set it, so the no-double-fire property rested entirely on the caller remembering to drop the tracker. `mark_fired` now sets it.
- **A hardcoded strength floor** in `report_landing` duplicated `MIN_NEEDLE_STRENGTH`.

**The first version of the test for this passed while being wrong, which is worth recording.** It drove `report_landing` through a stub monitor. The function samples against a wall clock; the stub ran out of frames inside 300 ms and repeated its last one; three identical reads look exactly like a freeze. So the sweeping case — the one the test existed for — passed by accident. The fix was to split the pure decision out and test it against angles measured off real recordings, and to test the wall-clock wrapper only for what a stub can honestly answer. **A stub that runs out of data does not fail, it fabricates.**

**The human baseline: `tools/replay_tracker.py recordings --human`.** A hit freezes the needle, so the frozen angle is where the player's press landed, scored against the same drawn zone by the same code. Ten scorable presses: **4 Great, 6 good, 0 miss, and every single one late** — median 9.0 deg past the band's centre, bias +10.3, worst +21.5. That is the arriving-late diagnosis measured directly, with no latency number involved. The tracker's replay over the same recordings is 13/13 Great at median 1.0 deg and a bias of -0.3.

Also corrected while doing this: **only 10 of the 14 hit recordings actually capture the freeze.** The other four end while the needle is still moving. "All were hit, so every one has a frozen tail" is true of the checks, not of the recordings.

**2026-08-15 (evening, last): the Merciless Storm clip closed the two gaps the recordings could not.** `tools/test_continuous_check.py` replays `videos/merciless-storm.mp4` frames 25-658 as **one unbroken 21.1 s check** — which is what it is; `ingest_video.py` cuts it per revolution, so the ingested `check_NNN` directories cannot exercise this and the source video has to be re-decoded.

- **The frame and sample caps now have evidence.** 633 frames in, 24 retained, **3.61 MB peak** against the 95 MB the uncapped version would have held, and `seen` still counts all 633 so the zone-retry cadence is unaffected.
- **Abstention holds across the whole continuous check**, not merely per revolution. Zero presses scheduled in 21 s. The per-revolution replay could only ever show the former.
- **The wall-clock loop in `report_landing` has now executed.** Nine grabs inside the 300 ms window at a 30 fps pacing, refusing a verdict on the never-stopping Merciless Storm needle and producing one (`landed 213.0 deg — GREAT, +4.5 deg`) on the genuinely frozen tail of `recordings/check_001`.

**On stubbing, since this is the second time it mattered.** Repeating frames is honest only when the frames genuinely show a still needle. Cycling six frames of a frozen tail reproduces what the capture really sees; clamping on the last frame of a *sweep* invents a stillness that was never there, which is how the first version of this test passed its most important case for the wrong reason. The stub also has to be *paced*: `report_landing` samples against a wall clock, so an instant-return stub compresses its entire 300 ms window into microseconds and tests nothing about the loop.

**Still open:** Merciless Storm's Great geometry (the tracker abstains rather than guessing), off-centre Doctor checks (still outside the 224 crop), and validation of the counter-clockwise path against real footage — every reversed check we have is Merciless Storm, which has no measurable Great band, so the direction handling is covered only by unit tests.

**2026-08-15 (afternoon): third-party recordings closed the two gaps that five live sessions could not.** Downloaded clips of other players are now ingested by `tools/ingest_video.py` into the `recordings/` format, so every analysis tool runs on them unchanged. That gave the first real Doctor/Madness checks and the first Merciless Storm — see *What third-party recordings settled*. It also exposed **two silent bugs in `analyse_needle.py`**, both now fixed, both of which had been producing confident wrong answers about the project's load-bearing assumption:

- It measured the needle angle about the **crop centre (112, 112)** rather than the ring, which sits at **(112, 102)**. On clean native footage that inflated the residual from 0.5 deg to 5.8 deg and made the tool print *"velocity is NOT reliably constant; predictive firing needs a better model"* about a needle holding 300.0 deg/s to within half a degree. It now refines the ring per check.
- `trim_frozen_tail` treated "did not advance clockwise" as a stall, so **every counter-clockwise check was discarded on its second frame** — silently, and precisely the Madness checks the tool exists to characterise.

Our own numbers moved as a result: `recordings/` fit error is **2.1-5.3 deg RMS, median 2.8** (13 sweeping checks), against the 2.6-4.7 previously recorded, and 13/13 now sit under 25% of the required lead. All 14 checks refine to (111.5, 101.5-102.0), so the prior is right and the residual scatter is not centre error. `check_003` is an unexplained outlier at 5.28 RMS — a fixed centre of (106, 98) fits it at 2.7, so the ring-peak criterion and the fit-residual criterion disagree on that one check. Worth understanding before the tracker trusts a per-check centre blindly.

**The next piece of work is still the predictive tracker** (step 3 below). Nothing blocks it, and after 2026-08-14 nothing about the timing budget is assumed any more. The design is settled, the constants are **measured**, and there is a labelled dataset to develop and validate against without needing the game running.

The 2026-08-12 session removed the last research doubt about it: linearity now holds across **four independent sessions and three builds**, and **nothing in the game accelerates a needle mid-sweep** (researched against both the simulator bundle and the wiki — see *No mechanic accelerates a needle mid-sweep*). Any non-linear fit from here on is an instrument fault, not a mechanic.

The 2026-08-14 session closed the last open constant: **the Great zone is 10.5 deg, measured from the drawn pixels** rather than taken from the simulator (`tools/measure_zone.py` against 22 deliberately-unhit checks). The Great window is therefore **26-37 ms** across every rate we have observed, against a 72 ms round trip — so reactive firing is confirmed impossible on a measurement, not on a fan recreation. Build the tracker against 10.5 deg, not the simulator default of 15.

Two requirements that were **not** previously written down anywhere, both cheap to honour and silent if missed:

- **Madness checks can rotate counter-clockwise.** Take the *sign* of the fitted slope; assuming clockwise mis-times a reversed check by twice the lead.
- **Absolute rate cannot be inferred from build or perk state, only measured per check.** Our absolute scale disagrees with the simulator's base by up to ~10-16% in both directions, while within-chain ratios are clean. Fit the rate live; never derive it from a token count or a build.

```bash
cd /Users/nicojan/dev/dbd_autoSkillCheck
.venv/bin/python tools/analyse_needle.py recordings          # 14 checks, needle fit quality
.venv/bin/python tools/sweep_rates.py --frames frames/session_20260812_193737   # cached: seconds, no inference
.venv/bin/python tools/ingest_video.py videos/merciless-storm-madness2.mp4      # any downloaded clip -> recordings_video/
.venv/bin/python tools/analyse_needle.py recordings_video/merciless-storm-madness2
```

What exists to build against, all offline and replayable:

| Data | What it is |
|------|------------|
| `recordings/check_001..014` | 14 checks at ~40 fps, 224 centre crops, per-frame class + confidence in `manifest.json`. All **hit**, so all have frozen tails. The primary development set. |
| `recordings_missed/check_001..022` | 22 deliberately **unhit** checks, same format. No frozen tails, both zone edges drawn. The zone-geometry set. |
| `frames/session_20260812_193737/` | Hyperfocus session: 28800 full frames, 911 detections, 27 checks, **detection-cached**. Shows rate changing *between* adjacent checks — the case per-check estimation exists for. |
| `frames/session_20260811_160620/` | Fast/small-window build: 3851 full frames, 559 detections, 35 measurable checks. The hard case. |
| `frames/session_2026081{1_142741,1_153049,1_160232}/` | Three standard-play sessions, pruned to skill-check neighbourhoods. |

**~~Two~~ One thing still needs live play:**

1. ~~**The true Great window.**~~ **DONE 2026-08-14.** 22 unhit checks recorded to `recordings_missed/`, and the zone measured straight from the drawn pixels by `tools/measure_zone.py`: **Great = 10.5 deg**, whole success zone 49.5 deg, Great at the leading edge in 13/13. See *Zone geometry* below. `greatZoneSize` is no longer taken on trust, and the reactive-infeasibility conclusion now rests on a measurement rather than on a fan recreation.

2. ~~**A Doctor match.**~~ **Obtained 2026-08-15 without playing one** — from a downloaded recording of another player, ingested by `tools/ingest_video.py`. Four Madness checks and four counter-clockwise ones, see *What third-party recordings settled*. Still worth capturing our own if a Doctor turns up (n=4 is one match), but it is no longer a blocker, and **downloading footage is the cheaper route to any rare game state** — it needs no matchmaking luck and gives cleaner data than our stream does. If capturing live, **record with `record_frames.py`, never `record_checks.py`** — the latter triggers on the centre crop, so an off-centre check never fires it and is silently never recorded.

## What was built

| File | Purpose |
|------|---------|
| `dbd/utils/directkeys.py` | Platform dispatcher; upstream Windows version preserved as `directkeys_win.py`, new `directkeys_posix.py` uses pyautogui |
| `dbd/utils/monitoring_window.py` | Targets the Moonlight window instead of a fixed display region; strips letterbox/pillarbox, scales the crop from *content* height |
| `dbd/utils/focus_watcher.py` | Gates firing on the stream being focused |
| `tools/autorun.py` | Main runner: focus-gated detect/fire loop, `--dry-run`, `--pin-geometry`, `--hit-ante` |
| `tools/calibrate_window.py` | Dumps the capture box drawn on the frame, to verify framing |
| `tools/test_keypress.py` | Isolates whether synthetic keys reach the host |
| `tools/measure_latency.py` | Measures keypress → pixel round trip |
| `tools/wide_scan.py` | Diagnostic: slides a 224 window over the whole frame to find where checks appear |
| `tools/record_checks.py` | Records clean frame sequences of skill checks for building the tracker |
| `tools/analyse_needle.py` | Extracts needle angle per frame and measures how constant the sweep rate is |
| `tools/measure_zone.py` | Measures the Great/Good zone widths from the drawn pixels of **unhit** checks; refines the ring centre, then splits fill from outline by radial thickness |
| `tools/record_frames.py` | Records full frames during play at 30 fps with **no inference** (threaded JPEG writers), for offline analysis |
| `tools/scan_frames.py` | Offline 224-tile sweep over recorded frames; the off-centre hunt without a live frame-rate budget |
| `tools/sweep_rates.py` | Per-check sweep rate and fit quality from a full-frame session (centre crop only, so ~1 inference/frame) |
| `tools/ingest_video.py` | Turns any downloaded gameplay mp4 into `recordings/`-format checks: rescales to 1080p, finds rings by Hough + needle confirmation, keeps only clusters with coherent rotation, cuts continuous sweeps per revolution, refines each ring centre, and writes 224 crops with model-classified manifests |
| `tools/prune_frames.py` | Deletes frames far from any detected check; dry run by default. Three detection sources: `--scan-log`, `--sessions` (tile-sweep `hits/`), `--centre-detections` (the `sweep_rates.py` cache — **only** for sessions known to have no off-centre checks) |

Modified upstream: `app.py` (added `moonlight` capture backend + focus gate), `dbd/AI_model.py` (made `cleanup()` idempotent — it crashed on every shutdown).

Two changes to `sweep_rates.py` on 2026-08-12:

- **It caches detections.** Inference over a 28800-frame session is ~8 minutes and was being repeated for every question asked of the same footage. It now writes `<session>/centre_detections.json` (frame name, index, `t_ms`, class — metadata only, no pixels) and reuses it unless `--rescan` is passed. The cache is keyed on crop geometry, so a cache taken with a different crop is refused rather than silently reused. Re-analysis is now seconds: only the frames that fired get re-read.
- **It no longer crashes on pruned sessions.** Geometry was probed from `frame_list[0]`, which does not exist in any pruned session — the manifest deliberately keeps every originally-captured frame while the files are gone. It now probes the first frame that actually reads. **The tool had been unusable on all three pruned sessions since they were pruned**, which is worth remembering before concluding a session contains nothing.
- It also reports each check's **start time and gap since the previous check**, which is what makes token chains legible. Per-check rates in isolation cannot show a staircase.

## Measurements

All on the 2560x1080 ultrawide, Moonlight fullscreen, stream pillarboxed to 1920x1080.

| Thing | Value |
|-------|-------|
| Capture (`mss`) | 21.9 ms/frame — **90% of the frame budget** |
| Inference | 2.0–2.5 ms/frame |
| Focus gate | 2.45 ms/frame |
| Live throughput | **34 fps** (README wants ~120); measured **39 fps** on 2026-08-15 over 1015 frames / 26 s. Replay says 20 fps costs nothing and 13 fps costs a third of the Greats, so the loop rate is not currently a constraint. Beware the `PAUSED` line's fps field — it covers only since the last fire, so a short window right before focus loss can read as low as 16 |
| Keypress → pixel round trip | **~72 ms** median when first measured; **re-measured 126.5 ms on 2026-08-15** (9 clean trials, spread 119–135 plus a 192 outlier). Same convention both times — raw from `measure_latency.py`, sampling interval included — so the stream leg roughly doubled, ~45–55 → ~97.5 ms. **Every "72 ms" elsewhere in this file is the historical figure; re-measure before trusting it.** Pass `--round-trip-ms 130` (not 126.5: late spills to Good, early is nearly free, so biasing long is worth 14/15 vs 11/15 Great across the jitter band) |
| Capture cost vs region size | 224px 22.2 ms vs full-frame 26.9 ms — **+4.7 ms for 41x the pixels** |
| Full-frame JPEG encode + write | 12.3 ms at q92 (q75 is 11.6 ms — quality is nearly free, so keep it high) |
| Full-frame record loop ceiling | **41.5 fps** with 2 writer threads (34.2 with 1, 42.6 with 3 — capture is then the wall). Serialized it was ~24 fps |
| Full-frame record disk cost | ~350 KB/frame at q92 → ~630 MB/min at the 30 fps default |
| Offline tile sweep | ~2.5 fps at 153 tiles/frame — 8191 frames takes ~26 min at `--every 2` |
| Needle sweep rate | **290–347 deg/s** median 329 (standard, 13 checks); **214–440** median 326 (fast build, 35 checks); **219–362** median 318 (Hyperfocus, 27 checks) |
| Needle angle fit error | RMS **2.1–5.3 deg**, median **2.8** (standard, re-measured 2026-08-15 after the centre fix), **4.1 deg** median (fast build), **3.9 deg** median (Hyperfocus), vs ~23 deg of lead for the 72 ms round trip → **~9 ms** implied timing error |
| Needle angle fit error, **native capture** | RMS **0.44–2.7 deg** on third-party 60 fps recordings; **0.76 deg over 17 continuous revolutions**. Our residual is the Moonlight encoder and our frame rate, not the game |
| Merciless Storm sweep rate | **300.0 deg/s** (1200 ms/rev), constant across a 20.6 s continuous check |
| Hyperfocus per-token rate step | **+4%**, confirmed to within **0.1–2.3%** across two 4-check chains — but only ~2–3 tokens ever banked, because tokens require *Greats* |
| Great window (from simulator constants) | **16–61 ms** — always *narrower* than the 72 ms round trip |
| Great zone, **measured from pixels** | **10.5 deg** (10.0–10.5 across 13 unhit repair-heal checks; threshold-independent) → **26–37 ms** at observed sweep rates |
| Success zone, **measured from pixels** | **49.5 deg** total (Great 10.5 + Good 39.0), Great at the leading edge in 13/13 |
| Ring prefilter (2x downscale + Hough) | **6 ms**, located the ring in 5/5 known check frames; full-res Hough is 229 ms |

Capture cost is dominated by the syscall, not the pixel count. Shrinking the capture region cannot buy frame rate; widening it is nearly free.

## Findings worth not rediscovering

**A milliseconds value slept as seconds cost four armed matches (2026-08-15).** `fire()` took `wait_seconds` and slept it directly; the predictive call site passed `decision.press_at_ms - now_ms`, in milliseconds. Every predictive press therefore slept 1000x too long — the guard above the call only fires when the remaining lead is under ~1.25 frame periods, so leads were 0-32 ms and became 0-32 **seconds**. Nothing in the test estate could see it: `--dry-run` returns before the sleep, the offline replay never sleeps at all, and the reactive path passes 0 — which is why `--no-predict` pressed the game correctly the whole time and predictive appeared never to press. Armed, the symptom was `needle gone before it could be read`, which reads like a *detection* fault and sent four sessions of diagnosis at the capture layer, the press hold, and the latency constant. The single readable landing we ever got (112 deg late) was the check whose remaining lead happened to be 0.343 ms. **Three lessons, in order of how much time each would have saved:** a boundary that carries a unit must carry it in the parameter name, not the caller's memory; a code path no test can reach is a code path that is wrong; and when a failure message names a *layer* (`needle gone`), check that the layer is where the fault is before believing it.

**The armed latency is 60 ms, and every idle measurement of it has been wrong.** Measured armed, against the game, under load: median **59 ms** over six scored landings (2026-08-15 23:20-23:25, 5 GREAT / 1 good / 0 MISS). `measure_latency.py` said 72 originally and **126.5 the same evening**, both taken idle against a host text field — so the idle figure was out by more than a factor of two in the direction that fires early, which is the direction that misses outright. `ROUND_TRIP_MS` now defaults to 60. **Re-measure armed, from the `round trip NNN ms measured` lines, not from the standalone tool.**

Changing the default from 72 to 60 costs one check in the offline replay, and the cause is worth knowing rather than chasing: `recordings_missed/check_005` drops to `NO FIRE (fit too poor, 15.5 deg RMS)`. A shorter lead makes the tracker wait longer before firing, which pulls that recording's frozen tail into the fit — and check_005 is one of the two "deliberately unhit" checks that were in fact hit (see above). The gate refusing a 15.5 deg fit is correct behaviour on a contaminated recording, not a regression. 14/14 of the clean checks still score GREAT.

**A latency figure measured idle is not the latency the run experiences.** `measure_latency.py` presses a key and watches a text field on the host, in its own process, with the detector not running. It reported 126.5 ms while the armed loop was ~1000x out. The armed path now times itself — `report_landing` dates the freeze against key-down, so `round trip NNN ms measured` is the real closed-loop number per check, under real load, against the game rather than a text box. **Prefer it over the standalone tool whenever they disagree**, and treat a swing check-to-check as a stream fault that no `--round-trip-ms` value can absorb.

**A measurement window has to be wider than the delay it is measuring.** `FREEZE_WATCH_SECONDS` was 0.30, sized when the loop was believed to be 72 ms. Any press landing later than that reports `needle gone before it could be read` — indistinguishable from a press that never arrived, and the exact message the broken predictive path produced. It is now 0.80. **A diagnostic that can time out reports the timeout as a finding**, which is how a 1000x error looked like a detection problem for four sessions.

**The ring is at (112, 102) in a 224 crop, not at (112, 112).** `analyse_needle.py` measured the needle angle about the crop's geometric centre for four days. The angle is measured *about* that point, so the 10 px error lands straight in the angle and then in the velocity fit, as a per-revolution sinusoid that a straight-line fit reads as non-constant velocity. On our own footage it inflated the median RMS from 2.8 to 3.7 deg; on clean 60 fps third-party footage it inflated 0.5 deg to 5.8 and flipped the tool's printed verdict to *"velocity is NOT reliably constant; predictive firing needs a better model"* — a confident wrong answer about the single assumption the whole project rests on. Fixed by refining the ring per check the way `measure_zone.py` does. **Any measurement taken about an assumed centre is suspect; refine first.**

**A direction assumption will silently delete your rarest data.** `trim_frozen_tail` ended a check after two frames that "failed to advance", where advance meant *clockwise*. A counter-clockwise Madness check therefore stalls on its second frame and is thrown away entirely — no error, just a `SKIP — only 2 usable frames`. Every reversed check in the first Doctor footage we ever obtained was discarded this way. Advance is now signed by the check's own median step.

**A confident class is not evidence a check is on screen.** In the ingested clips the model labels the frames immediately before and after a real check `full black (out)` — not `None` — so they pass the `desc == "None"` pre-roll filter, and the brightest stray red pixel in them supplies a meaningless angle that drifts the *opposite* way to the real sweep. That was enough to reverse the inferred direction and trigger the bug above. Filter by needle response relative to the check's own peak (a drawn needle scores 70-150; these strays reach 20-45), and keep only the contiguous lit block. This is the same failure family as the loadout-menu detection below: **the model's class assignment is not a check detector.**

**`cv2.HoughCircles` is not accurate enough to anchor radial measurements.** It located the skill-check ring centre only to about +/-3 px, wandering between (109.5, 101.5) and (114.5, 102.5) across checks that are in fact identically framed. Sampling an annulus from a centre that is 3 px off shifts the apparent radius by 3 px at some angles, which moved the zone arc clean out of any fixed radial band — the zone "vanished" on 9 of 22 checks and the surviving numbers were quietly wrong on the rest. Refining the centre by maximising the angle-median radial peak (the base ring is a full circle, so at the true centre it lands at one radius for every angle) pins it to a quarter pixel and gives `ring_r = 65.00` on **every** check. `measure_zone.py` does this; anything else measuring radially should too.

**The "SPACE" prompt box mimics the Good zone.** It is static, bright, and has the same radial-thickness signature as the zone outline, so it reads as a phantom ~45 deg arc at 65-105 deg in every check. It sits near r=45 while the ring is at r=65, so a radial window around the ring excludes it. Widening that window to be safe silently re-admits it.

**Do not use `NSWorkspace.frontmostApplication()`.** It caches and never refreshes without a Cocoa run loop. It reported `iTerm2` for 60 straight seconds while Moonlight was fullscreen and focused. It looks correct in short-lived processes, so it passes casual testing and fails in the long-running loop that matters. `focus_watcher.py` uses `CGWindowList` instead, which re-queries the window server every call. Verified across seven live transitions.

**A fullscreen app lives on its own macOS Space.** `kCGWindowListOptionOnScreenOnly` only reports the *active* Space, so a fullscreen Moonlight is invisible to that query when you are looking at another desktop. Window *lookup* must use `kCGWindowListOptionAll`; the *focus gate* deliberately uses on-screen-only, because that is the question it is asking.

**The 224x224 centre crop is not arbitrary.** `crop = 224/1080 * content_height` — the model was trained on skill checks occupying a fixed fraction of a 1080p frame. Feeding it the whole window squashes the check to ~1/5 its trained height and distorts the circle into an ellipse. On this setup the correct crop is `left=1168, top=428, 224x224`, identical to what stock `Monitoring_mss(monitor_id=1)` computes.

**Geometry drifts on resume.** `refresh()` fired mid-Space-transition once and latched a menu-bar-inset window, giving a 218px crop instead of 224. Moonlight registers ~16 windows including a 1280x628 decoy that clears the size floor. Mitigated with a 0.75 s settle delay, a loud warning on change, and `--pin-geometry`.

**The needle freezes on a successful hit, and the freeze is invisible to a frame-equality test.** When a check is hit the game stops the needle dead at the hit position, but the stream encoder keeps jittering the pixels, so consecutive frames are *not* identical even though the angle is bit-for-bit the same. A first attempt at trimming these post-hit frames tested frame content for equality, never fired, and left the frozen tail in the velocity fit. That single bug made four of thirteen checks look like they had non-constant angular velocity (RMS residual 9-15 deg) and inflated the apparent spread of sweep rates to 221-347 deg/s. Detecting the freeze by the needle *failing to advance* for two consecutive frames instead fixed all four: 13/13 now fit cleanly and the real rate spread is 290-347 deg/s. **Anything that fits needle motion must exclude post-hit frames, or it will silently conclude the needle is unpredictable.**

**The model fires at 1.00 confidence on the loadout menu.** A red-ringed circular perk icon in the perk selection screen classifies as `repair-heal (out)` with confidence 1.000. The focus gate does not help, because Moonlight is focused in menus too. It happened to land on a non-hit class here, so `autorun.py` would not have pressed — but the thing protecting us is *class assignment*, not confidence, which is thinner than it looks. Any off-centre analysis must exclude menu frames, and a static-UI detection persists for minutes, so it looks like the most solid detection in a session unless events are capped by duration.

**Model coverage and capture coverage are independent.** The model may recognise every skill check type, but a check rendering outside the crop is never captured, so it is never classified — a silent miss with no error. Training coverage cannot fix a cropping problem.

**Frame-mean is the wrong statistic for detecting small changes.** In `measure_latency.py` the first attempt averaged over the whole frame: diffuse encoder noise moved the mean 160x more than a typed character did. Counting pixels that changed by >30 grey levels separates a hard-edged caret from soft video noise. Synthetic check: noise 0 changed px vs caret 300.

## The Great-vs-Good problem

Armed run at `--hit-ante 0`:

```
4 x wiggle (great)              -> worked perfectly
2 x repair-heal (ante-frontier) -> landed mid-Good, not Great
```

Every repair/heal detection comes back as class 2, `ante-frontier` — the model deliberately recognises the needle *just before* the Great band so there is time to absorb latency. The compensating delay is gated behind `hit_ante > 0`.

Raising `--hit-ante` to 20 (the author's default) made it **worse**, which settles the geometry: the Great band is at the *leading* edge of the zone, and we are arriving **late**, not early. The README's "Good instead of Great" entry says to *decrease* toward 0 — already there. **The dial is out of travel.**

Error budget: ~72 ms round trip, of which ~20–29 ms is our own sampling interval and ~45–55 ms is the Moonlight stream. Even instantaneous capture recovers only ~25 ms of ~80 ms total. The stream is the larger half and is not locally optimisable.

## The skill check simulator settles most of the open questions

[dbd.lucaservers.com](https://dbd.lucaservers.com) is a browser skill check simulator whose client bundle (`/js/app.75a81068.js`) contains the mechanics as explicit constants. It is a **fan recreation, not the game's source**, so its numbers are somebody's model of DBD rather than ground truth — but three of them can be checked against our own 48 measured checks, and they agree closely enough to take the rest seriously.

**Rotation.** The needle animation is:

```js
easing: "linear", rotate: [0, 360], duration: 1100
```

- `duration: 1100` ms per revolution = **327.3 deg/s**. Measured independently: median **329** deg/s (standard build, 13 checks) and **326** deg/s (fast build, 35 checks). Agreement within 1%.
- `easing: "linear"` is the constant-angular-velocity assumption stated outright, matching the 3.7-4.1 deg RMS linear fits.
- The glyph variant rotates `[0, 2160]` over `6600` ms — six revolutions at the same 327.3 deg/s.

**Zone geometry**, which resolves the "Great is at the leading edge" question the notes previously inferred only from `--hit-ante` behaviour:

```js
u = 20
d = 240 - u - successZoneSize + greatZoneSize
m = random(u, d)                                  // zone start angle
great.start = m
great.end   = good.start = m + greatZoneSize
good.end    = m + greatZoneSize + successZoneSize
```

Great occupies the *leading* edge and Good follows it — confirming why raising `--hit-ante` made results worse rather than better. `greatZoneSize` is 10-20 (default 15); `successZoneSize` is 50 (0 for wiggle).

**Measured against real pixels on 2026-08-14 (`tools/measure_zone.py`, 13 unhit repair-heal checks):**

| quantity | simulator | measured | verdict |
|----------|-----------|----------|---------|
| Great zone | 10-20, default 15 | **10.5 deg** (range 10.0-10.5) | corroborated, at the *bottom* of the stated range — use 10.5, not the default 15 |
| Whole success zone | 65 if additive (`greatZoneSize + successZoneSize`) | **49.5 deg** (range 49.0-50.0) | **the formula above reads wrong** |
| Great at leading edge | yes | yes, 13/13 | confirmed independently of `--hit-ante` behaviour |

The total is 49.5 deg, not 65. That matches `successZoneSize` (50) on its own, which says Great is **carved out of the leading edge of the success zone rather than added in front of it** — i.e. `good.end = m + successZoneSize`, with `good.start = m + greatZoneSize` as written. Treat this as a correction to the transcription above, not to the simulator; the bundle itself has not been re-read to confirm which is the transcription error. Nothing downstream depends on the total — only `greatZoneSize` sits in the hit-window numerator — but the ~15 deg discrepancy should be resolved before the total is used for anything.

Two check types are **not** comparable and are excluded from those figures: wiggle (32.0 great / 67.5 total, all four checks identical — it oscillates and has its own constants) and full-white (59.5 great / 60.0 total — that type appears to render the whole zone filled).

**The Great window is always smaller than our round trip.** At `greatZoneSize / rate`:

| scenario | deg/s | great window |
|----------|-------|--------------|
| base | 327 | 46 ms |
| Hyperfocus x6 (+4%/token) | 406 | 37 ms |
| Coulrophobia (+50%) | 491 | 31 ms |
| Coulrophobia + Hyperfocus | 609 | 25 ms |
| worst case, `greatZoneSize` 10 | 609 | 16 ms |

Against a **~72 ms** keypress-to-pixel round trip, the Great band is **never** wide enough to react to. This is not "reactive firing usually misses" — it is arithmetically impossible, for every configuration. Predictive firing is mandatory, not an optimisation. Our own truncated measurement (>= 52 ms median, 26-99 ms range) sits consistently inside this band.

**Now measured directly rather than assumed.** At the measured 10.5 deg and our own fitted per-check sweep rates, the Great window is **31 ms** at the median 337 deg/s, **37 ms** at the slowest observed 287 deg/s, and **26 ms** at the Hyperfocus ceiling of 406 deg/s. Every value is well under the 72 ms round trip, so the conclusion holds with a real measurement underneath it instead of a fan-recreation constant — and the true window is at the *narrow* end of the previously assumed 16-61 ms band.

**A classifier-label measurement of this would have been wrong by 3x, in the direction that overturns the conclusion.** Taking the span of frames the model labels `great` gives ~40 deg (~120 ms), which is *wider* than the round trip and would have said reactive firing works. The model's `great` label is a hand-annotated "press about here" cue with deliberate margin, not the drawn zone. Never measure game geometry through the classifier.

**Off-centre skill checks are the Madness effect (The Doctor).** This is the whole of the position logic:

```js
if (effects.includes("madness")) {
    inset = 0.15 * window.innerHeight
    element.style.top  = random(inset, innerHeight - inset) + "px"
    element.style.left = random(inset, innerWidth  - inset) + "px"
} else {
    element.style.top = "50%"; element.style.left = "50%"
}
```

Position is **uniform random** over the screen inset by 15% of the window height; every other check is hard-coded dead centre. This explains three sessions and ~55k frames with zero off-centre sightings: **no Doctor was faced.** The phenomenon is real and reproducible on demand, but only against that killer — playing more games against anyone else can never produce one, which is why the empirical hunt was doomed regardless of tooling quality.

On a 1920x1080 stream the madness region is 1596x756 px, needing 128 tiles at 50% overlap (~256 ms/frame, ~4 fps) — far too slow live. A **ring-detection prefilter is ~25x cheaper**: downscale 2x, `medianBlur(3)`, `HoughCircles(param1=100, param2=30, minRadius=27, maxRadius=57)` costs **6 ms** and located the ring in 5/5 known check frames. It proposes candidate positions which the model then classifies at ~2 ms each, so a spurious proposal costs one inference rather than a wrong answer. Full-resolution Hough is 229 ms and pointless by comparison; the 2x downscale is what makes it viable.

## What third-party recordings settled

Three downloaded clips of other players (`videos/`, gitignored, 31 MB) were ingested on 2026-08-15 by `tools/ingest_video.py` into `recordings_video/`, 31 checks in total. They are other people's captures at 720p, 1080p and 4K — everything is rescaled to 1080p first, after which the refined ring radius came out **64.61-65.19 px on all 31 checks**, against `ring_r = 65.00` on our own. Same UI scale, so the data is directly comparable despite the different sources.

They are also *better* data than ours in the one way that matters for kinematics: no Moonlight encoder in the path, and 60 fps. Fit residuals are **0.44-2.7 deg RMS** against our 2.1-5.3.

**Madness positions are far tighter than the simulator claims.** Four off-centre checks, measured from the refined ring centre on a 1920x1080 frame whose centre is (959.5, 539.5):

| check | centre | offset | radial |
|-------|--------|--------|--------|
| 001 | (1086.0, 620.5) | (+125.5, +79.5) | 149 px |
| 002 | (1011.5, 457.5) | (+51.5, -78.5) | 94 px |
| 006 | (1042.8, 528.8) | (+83.5, -10.5) | 84 px |
| 009 | (834.8, 668.0) | (-124.5, +127.5) | 178 px |

The other nine sit at (959.3, 539.8) with under 4 px of scatter — dead centre, no drift. So the displacement is a real, bounded, discrete effect, **not** the simulator's `inset = 0.15 * innerHeight` uniform-over-screen model, which would scatter checks up to ~800 px out. **This changes the production fix for off-centre checks.** The farthest ring's outer edge is 243 px from centre; feeding the model a 224 crop centred on it needs the capture to span centre +/- 290 px, i.e. a 580x580 region. That is one capture and a handful of candidate crops — not 128 tiles, and arguably not even a Hough prefilter. Caveat that matters: **n = 4, from a single match.** Treat 180 px as a measured lower bound on the range, not as the limit.

**Counter-clockwise is confirmed on real footage, and it is not exclusive with off-centre.** 4 of 13 checks in that clip rotate counter-clockwise. Three of them are also off-centre. The wiki's "equal chance of being placed off-centre *or* turning counter-clockwise" is wrong read as an exclusive or — a Madness check can be both, and one of the four reversed checks was dead centre. A tracker must take the sign of the fitted slope and must not infer direction from position.

**Merciless Storm is a distinct motion type: one continuous rotation, no reset, no freeze on a hit.** `merciless-storm.mp4` is a single check running **20.6 s, 17 revolutions**, fitted as one straight line:

```
rate = 300.0 deg/s    RMS = 0.76 deg over the whole 6090 deg    period = 1200 ms
```

Per-revolution the rates are 299.2-303.9 with 0.44-0.65 deg RMS. Three consequences for the tracker, all of which the current design gets wrong:

- **"Abort if the needle stops advancing" would abort on every Merciless Storm hit.** The needle does not stop when the check is hit; the zone simply moves and the sweep continues.
- **The rate can be fitted once and reused for the whole 20 s**, instead of re-estimated per check. There is no per-check boundary to re-estimate at.
- **The zone relocates each revolution**, so anything that medians over a check's frames (`measure_zone.py` does) must work on one revolution at a time. `ingest_video.py` cuts continuous events at each full turn for exactly this reason.

**The classifier calls Merciless Storm checks `full black`, not `repair-heal`.** 870 of 890 frames in that clip, and 387 of 513 in the other. The zone renders as a thin unfilled outline arc with no solid Great band, which is why `measure_zone.py` reports `great = 0.0` on all of them — that is the tool behaving correctly on a type it was not built for, the same exclusion already applied to `full white`. **The Great/Good geometry of a Merciless Storm check is still unmeasured.**

**Rates in these clips cluster near 300 deg/s (1200 ms/rev), not the simulator's 327 (1100 ms).** Across the two Merciless Storm clips: 289-324, median 301. The single non-Merciless-Storm check, in `oppression.mp4`, fits at **363 deg/s**. Our own standard play is 291-328, median 320. Do **not** read 1200 ms as a new base constant — n is far too small to separate "Merciless Storm rotates slower" from "that player's build". It is one more reason the rate must be estimated live per check.

## Hyperfocus is visible as a token staircase, and it confirms +4%/token from our own data

`frames/session_20260812_193737` (28800 frames, 8.5 GB, 2026-08-12, player running Hyperfocus) gives 27 measurable checks: **219-362 deg/s, median 318, RMS 3.9 deg** — linearity holds for a third independent session, on a third build.

The interesting part is not the spread but the *ordering*. Grouping checks into chains separated by <= 15 s (one interaction, tokens persist) turns two chains into clean staircases:

| chain | rates (deg/s) | error vs +4%/token |
|-------|---------------|--------------------|
| t=250s | 321.7 -> 336.8 -> 345.2 -> 362.3 | +0.7%, -0.8%, +0.1% |
| t=296s | 314.2 -> 323.2 -> 332.0 -> 345.5 | -1.1%, -2.3%, -2.2% |

Four consecutive checks each, 2-6 s apart, rising monotonically at ~1.04 per step. Two independent 4-long monotonic runs is roughly `(1/24)^2` by chance, so this is signal. **It is the first independent corroboration of a *perk* constant** — previously only the base 327 deg/s had been checked against our own measurements. 4 of 12 within-chain steps match +4% to within 1%; the rest go flat or *decline* (-3.7% to -7.5%), which is what a token reset on a non-Great looks like.

**Tokens require Greats, so the rate distribution is partly a readout of hit quality.** Hyperfocus grants a token only on *succeeding a Great*, capped at 6 (+24% = 406 deg/s). This session peaked at **362** (~2-3 tokens) because Greats are the open bug — the perk never got to stack. Do not treat "the player ran Hyperfocus" as equivalent to "the data contains fast checks".

**Absolute rate is not a readout of token count; only within-chain ratios are trustworthy.** The lowest good repair fits are 275-306 deg/s, up to **16% below** the simulator's 327 base, which is impossible if 327 is really the floor. The same dip is present in the older standard-play range (290-347), so it is a pre-existing absolute discrepancy — either the fan recreation's 1100 ms is not the game's, or ~10% of our absolute scale is measurement. Ratios within a chain cancel any constant scale error, which is why the staircase holds while "this check has N tokens" does not. This does not change the tracker's design; it is another reason the rate must be **estimated live per check** rather than derived from build or perk state.

One fit in 27 is junk (check 26, RMS 11.84 against a 3.9 median) and should be excluded by an RMS threshold, not by hand. Check 15 is wiggle, where a single linear fit is wrong by construction: the bundle gives wiggle `[now, 2160*direction+now]` over 8400 ms = **257 deg/s**, against the 222 the linear fit reports. Wiggle rows in this table are not rates.

## No mechanic accelerates a needle mid-sweep

Checked 2026-08-12 against both the simulator bundle and the wiki, because a needle that accelerated mid-sweep would break the linear tracker on exactly the checks that most need prediction.

Every rotation path in `app.75a81068.js` is `easing: "linear"` with the duration fixed *before* the tween is created — standard `[0,360]`/1100 ms, glyph `[0,2160]`/6600 ms, wiggle `[now, 2160*direction+now]`/8400 ms. Every named effect writes `speed` pre-animation: Coulrophobia sets `speed: 600` outright, Hyperfocus subtracts `speed*(4*tokens/100)`, Unnerving Presence touches only `successZoneSize`, Madness only repositions. No code retimes, re-eases or restacks a running animation; the only mid-animation call is a pause/reset *after* completion.

The wiki agrees on the mechanics: the only two things that change pointer speed at all are **Coulrophobia** (+50%, healing checks only, while in the terror radius) and **Hyperfocus** (+4%/token, max 6 tokens = +24%). 327 x 1.24 = **406 deg/s**, matching the figure already in these notes.

**Madness checks can be reversed — confirmed on real footage 2026-08-15.** The wiki states a Madness check has an equal chance of being placed off-centre *or* turning **counter-clockwise**; 4 of 13 checks in the Doctor clip run counter-clockwise, and three of those are *also* off-centre, so read it as "and/or", not exclusive. A tracker that assumes clockwise mis-times a reversed check by twice the lead, silently — take the **sign** of the fitted slope, and never infer direction from position.

## The hit window, and why it is not yet measured

The Great-vs-Good problem is really a question about **how many milliseconds the needle spends inside the Great band**, because that is the budget the ~72 ms round trip has to fit inside. Measuring it from the 14 recordings gives a median of **>= 52 ms**, with individual checks from 26 to 99 ms.

That number is a **lower bound, not the answer**. Every one of those checks was hit by the player, which freezes the needle partway through the Great band, so the observed span runs from the first Great frame to the moment of the hit rather than to the far edge of the zone. A window measured this way is truncated by construction.

To measure it properly the next session needs skill checks that are **deliberately let pass** — no key press, needle sweeps through Great and out the other side. A handful of missed checks per build is enough, and it is the only way to get the true width. Builds that shrink the success window (rather than speeding the needle up) change exactly this quantity while leaving the sweep rate alone, so the two effects have to be measured separately.

**Locate the check's ring once per check, not once per frame.** `cv2.HoughCircles` wobbles by a few pixels between frames, and because the needle angle is measured *about* that centre, the wobble lands straight in the angle. Measured per frame, the fast-build checks fitted at 7.9 deg RMS median; using one median ring per check, the same data fits at **3.9 deg** — matching the standard-build recordings (3.7 deg) almost exactly. The 216-425 deg/s build initially looked like it might be genuinely non-linear, i.e. accelerating mid-sweep, which would have broken the linear tracker for exactly the checks that need prediction most. It was not: it was jitter in the measuring instrument. **Faster checks are as linear as normal ones.** In the units that matter, a 3.9 deg fit error at ~314 deg/s is roughly **12 ms** of timing error, against the ~72 ms the reactive path currently misses by.

**The estimation budget is large enough for live per-check fitting.** If the sweep rate has to be estimated per check rather than hardcoded, the tracker needs enough of the sweep *before* the Great band to fit a rate from. Measured across the recordings, a check spends a median of **545 ms** (21 frames at the 40 fps recording rate) between first detection and first Great. Even a check sweeping twice as fast leaves ~272 ms, which is ~9 frames at the 34 fps the live loop actually achieves — enough to fit a slope, with margin left to schedule the press. Per-check live estimation is therefore viable, not just desirable. Confirmed by the player that rate varies between checks *within a single build* ("sometimes faster, sometimes normal"), which rules out per-build calibration as a shortcut.

The relationship worth keeping in mind: `hit window (ms) = Great band angular width / sweep rate`. A build can make a check harder by shrinking the numerator or growing the denominator, and those two need different responses from the bot. Only a *faster* needle is compensated by better prediction; a *narrower* window tightens the tolerance on every part of the chain, including the 45-55 ms of Moonlight latency that is not locally optimisable.

## Next steps

1. **Moonlight/host latency settings** (user, in progress) — wired connection, host game V-Sync off, Moonlight frame-pacing/V-Sync off, GPU low-latency mode. Highest value per effort. Re-run `tools/measure_latency.py` afterwards to quantify.

2. ~~**Record real skill checks**~~ — **done 2026-08-11.** 14 checks in `recordings/`, 60 frames each spanning ~1.48 s at ~40 fps, each covering the approach, the zone crossing and the aftermath, with per-frame model predictions as ground truth in `manifest.json`. Re-run with `.venv/bin/python tools/record_checks.py --seconds 1500` to collect more.

3. ~~**Predictive firing**~~ — **BUILT 2026-08-15**, see *Resume here*. 29/29 Great across every scorable recorded check, confirmed firing live in dry-run. The design below is what was built, with two additions the design did not anticipate: the aim sits one degree late because the error directions are not symmetric, and the tracker abstains outright when no solid Great band is drawn rather than guessing at one. The original notes follow.

   Needs no retraining. Latency is *consistent* (six clean trials spanned 8 ms), and a stable delay is compensable. Track the needle angle across frames with classical CV, estimate angular velocity, and schedule the press to *land* in Great using ~72 ms of lead.

   The algorithm, now that the constants are known:

   - **Detect** a check the usual way (the model on the centre crop).
   - **Locate the ring once** with `cv2.HoughCircles` and reuse that centre for the whole check. Do not re-locate per frame — see the ring-jitter finding below.
   - **Track** the needle angle each frame via `R - max(G,B)` along an annulus about that centre.
   - **Fit** angle against time as a straight line, refreshing as frames arrive. There are ~545 ms of sweep before Great (~272 ms even at double speed), which is 9-21 frames at live rates — enough for a stable slope.
   - **Schedule** the press for when the needle will reach the Great band: Great starts at the *leading* edge of the zone and is 10-20 degrees wide. Fire `round_trip_ms` early.
   - **Abort** if the needle stops advancing *in its own direction* — but only for discrete checks. A Merciless Storm needle never stops, not even on a hit, so a stop-based abort would fire on every one of them. See *What third-party recordings settled*.

   Expected accuracy from the measured fit quality: ~4 deg RMS at ~330 deg/s is **~12 ms** of timing error, against a 16-61 ms window. Tight but workable — and it is the only approach that can work at all.

   Reusable pieces already written and validated: `tools/analyse_needle.py` (`needle_angle`, `trim_frozen_tail`) and `tools/scan_frames.py` (`locate_ring`, `needle_angle_about`).

   **All three questions are now answered** (`tools/analyse_needle.py`, 14 recordings from 2026-08-11):

   - *Is the needle cleanly extractable?* **Yes.** It is a red radial line; scoring pixels by `R - max(G,B)` isolates it and cancels the white success-zone arc. Peak response is 80-150 against background across every recording. Stream compression is not a problem.
   - *Is angular velocity constant within one check?* **Yes.** A straight-line fit of angle against time gives RMS residual 2.6-4.7 deg on 13/13 sweeping checks. Covering the ~72 ms round trip needs ~23 deg of lead, so the fit error is 11-20% of the lead (median 16%). This is the load-bearing assumption and it holds.
   - *Does speed vary between checks?* **Yes, and a second build widened it considerably.** Standard play: 290-347 deg/s, median 329. A build the player described as having faster needles and a smaller press window: **214-440 deg/s, median 326** across 35 checks. Note the median barely moved — the build *widens the distribution* rather than shifting it, which is exactly the player's description of "sometimes faster, sometimes at normal speeds". Rate therefore varies between checks *within a single build*, which rules out per-build calibration as a shortcut and makes live per-check estimation mandatory.

     **The mechanism behind that widening is now known: it is token stacking, not randomness.** A third session with Hyperfocus (219-362 deg/s, median 318, 27 checks) shows rate climbing ~4% per check along chains of consecutive checks and resetting when the chain breaks — see *Hyperfocus is visible as a token staircase*. A "widened distribution with an unmoved median" is exactly what a mixture of token states looks like. This strengthens the case for live per-check estimation rather than weakening it: the rate genuinely differs between two checks seconds apart on the same generator.

   Critically, the fast build's checks are **just as linear**: 4.1 deg RMS median, 17% of the required lead — statistically indistinguishable from the standard build's 3.7 deg. Faster does not mean less predictable.

   Wiggle is a **separate motion type** — the needle oscillates rather than sweeping, so a single linear fit is wrong by construction. It is also the one case that already works reactively, so it can keep the existing path.

   Build it to log prediction error per check, so it reports honestly whether it works instead of us inferring from Great/Good counts. Two things it must do, both learned the hard way above: exclude post-hit frozen frames from the velocity fit, and ignore detections while a menu is up.

4. ~~**Off-centre skill checks**~~ — **explained 2026-08-11, and it is not a distribution to be discovered by playing.** They are the **Madness effect (The Doctor)** and nothing else: the simulator positions the check uniformly at random inset 15% of window height, and hard-codes dead centre in every other case. That is why ~55k frames across four sessions and three killers produced zero — the phenomenon was never reachable without a Doctor.

   **Measured 2026-08-15 from real Doctor footage, and it is much easier than the simulator implied.** Four Madness checks sit 84-178 px from centre, not the ~800 px a uniform-over-screen model predicts — see *What third-party recordings settled*. The production fix is therefore **neither tiling nor a prefilter**: capture a 580x580 region about centre (which covers the farthest observed check plus its 224 crop) and classify a handful of candidate positions. Capture cost is nearly flat with region size (+4.7 ms for 41x the pixels), so this is close to free. n = 4 from one match, so treat 180 px as a lower bound and log any check that lands near the edge.

   Kept for reference, in case the range turns out to be wider than four checks suggest: covering the simulator's madness region takes 128 tiles at 50% overlap (~256 ms/frame, ~4 fps), unusable live. A ring prefilter is ~25x cheaper — downscale 2x, `medianBlur(3)`, `HoughCircles(param1=100, param2=30, minRadius=27, maxRadius=57)` costs **6 ms** and located the ring in 5/5 known check frames. Full-resolution Hough is 229 ms and pointless by comparison. This is worth building only if Doctor games matter; a centre-only bot is correct for every other killer.

   The historical traps below are kept because they are about *verifying a rare-event claim*, and every one of them produced a confident wrong answer first.

   Four traps this tooling had to grow past. The first three each manufactured a false positive; the fourth would have hidden a real one:
   - At 50% tile overlap **a single centred check lights up four or more tiles**, and the neighbours sit far enough out to be labelled OFF-CENTRE. Counting tiles instead of checks invents exactly the finding the tool exists to test for. Fixed with non-max suppression.
   - **A real check is transient** (~1 s), so a detection lasting minutes is static UI. But **duration alone does not work**: detection on static UI *flickers*, chopping minutes of bloodweb into many short runs that each look check-like. A first pass using only duration reported "7 multi-frame off-centre checks"; every one was menu chrome (bloodweb nodes, the end-of-match score screen).
   - **Motion is the honest discriminator** — a real needle sweeps ~330 deg/s while UI sits still — but total motion is not enough either. Animated backgrounds (the score screen's fire) make the brightest-red ray jump around, faking ~29 deg of "motion". Requiring *directional coherence* (net rotation / path length >= 0.8, over at least 3 frames) separates a needle, which never reverses, from jitter.
   - **The angle must be measured about the CHECK's centre, not the tile's.** `analyse_needle.py` measures about the tile centre, which is correct for `record_checks.py` frames because the check is centred by construction. In `scan_frames.py` a tile is a fixed grid position, so the check sits off-centre inside it by up to half a tile, and an annulus about the tile centre samples pure background — reporting *no needle* for genuine checks. Verified against real `full white` checks that scored zero motion until the ring was located first (`cv2.HoughCircles`, radius 60-110 in a 224 tile) and the angle measured about it. **This failure mode is the dangerous direction: it rejects real off-centre checks silently**, which is precisely the thing the tool exists to find. Independent of all timing work: no amount of timing fixes a check whose pixels are never captured. Full-frame coverage at 50% overlap is 153 tiles ≈ 3 fps, far too slow for live play, so a production fix likely means tiling only the positions that actually occur — cheap, since wide capture costs +4.7 ms. If positions turn out to be arbitrary, the ONNX batch dimension is pinned at `[1,3,224,224]` and would need re-exporting as dynamic to batch tiles efficiently.

5. **Frame rate** — only if the above falls short. `mss` is 90% of the budget and its cost barely varies with region size. Quartz `CGWindowListCreateImage` measured 18 ms vs 22 ms, a marginal win. The real fix is ScreenCaptureKit, which has no clean Python binding and is a genuine piece of work.

   Partly addressed for *recording*: capture and JPEG encode are independent and OpenCV releases the GIL while encoding, so moving writes to worker threads took `record_frames.py` from ~12 to **41.5 fps**. That trick does not help the live bot, whose cost is inference, not encoding — but it is a reminder to check whether a measured ceiling is physics or just serialization. It was serialization.

6. ~~**Measure the true Great window**~~ — **DONE 2026-08-14: 10.5 deg**, see *Zone geometry*. `greatZoneSize` is no longer taken on trust.

   Still worth doing, and needs no new play: **a hit freezes the needle, and `trim_frozen_tail` already detects that freeze.** The freeze angle relative to the located ring is where the press landed, so the 14 hit recordings can be mined for *hit position* — which would answer "Great or Good?" per check directly, instead of inferring it from Great/Good counts at the end of a match. That machinery is needed by the tracker anyway. `measure_zone.py` now supplies the other half of that comparison: with the zone located in the same frame, a freeze angle can be scored against it directly.

7. **Housekeeping.** Disk is at **95% (56 GB free)** and `session_20260812_193737` is the largest unpruned asset at 8.5 GB. Pruning it needs `scan_frames.py` to run first, since `prune_frames.py` reads the `hits/` that the tile sweep writes — ~20 minutes of compute, and there is no Doctor in that session to hunt, so it is purely a disk decision. Do **not** prune it against centre-crop detections.

## Open data

- `recordings/check_001` … `check_014` — 14 real skill checks, 60 frames each, unannotated, with `manifest.json` giving per-frame timestamp, predicted class and confidence. All were **hit**, so every one has a frozen tail. This is the dataset the predictive tracker is built and validated against. 57 MB.
- `recordings_missed/check_001` … `check_022` — 22 **deliberately unhit** checks recorded 2026-08-14, 51-75 frames each at ~43 fps, same manifest format. 97 MB. 15 sweeping (13 repair-heal usable for zone geometry, 2 full-white), 7 wiggle. No frozen tails, so the needle sweeps clear past the zone and both zone edges are drawn — this is what `measure_zone.py` needs and what `recordings/` cannot provide. `check_009`, `check_010` and `check_019` are short (truncated by the next check triggering) and their ring-centre fit fails; they are skipped rather than silently mismeasured.
- `videos/` — three downloaded recordings of other players, 31 MB: `oppression.mp4` (3840x2160 @60, **only 81 frames / 1.35 s** despite the container claiming 366), `merciless-storm.mp4` (1280x720 @30, 23 s), `merciless-storm-madness2.mp4` (1920x1080 @60, 25 s). The source of every Doctor and Merciless Storm measurement we have.
- `recordings_video/<clip>/check_NNN` — 31 checks ingested from the above, same format and same tooling as `recordings/`, plus an `events.json` per clip carrying refined centre, ring radius, offset from frame centre, rate and direction. **4 Madness (off-centre), 4 counter-clockwise, 17 revolutions of one continuous Merciless Storm check.** Frames are 224 crops placed so the ring lands at (112, 102), matching `record_checks.py`.
- `frames/session_20260812_193737` — 28800 frames / 8.5 GB, ~23 min of play with **Hyperfocus**, 911 detections in 27 checks, **not yet pruned**. The token-staircase evidence. Carries `centre_detections.json`, so re-analysis needs no inference. No Doctor and no Merciless Storm in this session, and no checks were deliberately let pass, so it does not advance the Great-window measurement.
- `frames/session_20260814_234847` — public match, 2026-08-15. 25591 frames captured at 21.3 fps effective (below the 30 target; the rate decayed from 24.4 as the disk filled), 777 detections in **19 checks**, all centred. **Pruned** against `centre_detections.json` to 1917 frames / 0.61 GB, freeing 6.79 GB — safe here only because the match was confirmed Doctor-free. Repair-heal rates 293-335 deg/s, RMS 4.2 deg, so linearity now holds across **four** sessions. Hyperfocus was equipped but the session shows **no rate staircase** — consecutive checks wander (334.7 → 322.9 → 323.4) rather than climbing, which is what the "tokens require Greats, so 2-3 bank at most" note predicts. Do not treat this session as Hyperfocus evidence.
- `frames/session_*/` — four full-frame sessions, **pruned** to the frames within +/-30 of a detected skill check: 8776 frames / 2.4 GB kept, 46384 frames / 11.9 GB deleted. `session_20260811_160620` is the fast/small-window build (3851 frames, 559 detections) and is the richest. Each session keeps its `manifest.jsonl` and `session.json`, which still describe every frame *originally* captured including the deleted ones, so the timeline stays interpretable.
- `frames/session_*/hits/` — one annotated full frame per detection (1060 PNGs, ~2 GB), green box on the production centre crop, red on each detection. This is the audit trail for the off-centre result and the record `prune_frames.py` reads; delete it once the conclusion is no longer in question.
- `scan_hits/hit_225753_001.png` — one real skill check, but **annotations are drawn over the pixels of interest**. Bug now fixed; the tool saves a `_clean.png` alongside. This frame is not usable for CV development.

All three are gitignored — they are GBs of local capture data, not source.

## Note on use

Upstream README: injected input "can be considered as an 'unfair advantage' by EAC, potentially leading to a ban. For this reason, the script should only be used in private games."
