# Spirit audio and stream tuning — macOS/Moonlight

Companion to `NOTES-local.md`. Covers the presentation layer only: what the operator hears and sees, and what the bot's capture path can tolerate. Nothing here changes detection, aiming or lead.

## The constraint that governs everything below

`needle_tracker.py` thresholds are **absolute** 0-255 values, not relative to frame statistics:

| Constant | Value | Reads |
|---|---|---|
| `NEEDLE_REDNESS` | 25.0 | per-pixel red dominance (chroma) |
| `MIN_NEEDLE_STRENGTH` | 20.0 | absolute red floor |
| `HOT` | 60.0 | residual whiteness counted as drawn |
| `MIN_CENTRE_PEAK` | 60.0 | centre-fit quality floor |

The constraint is **no pixel transform anywhere in the chain**, which is a wider category than "no third-party software". Any gamma, saturation, contrast or sharpening pass shifts all four thresholds at once and invalidates the 878-fire calibration behind `AIM_BIAS_DEG`. It catches:

- ReShade and any host-side injector
- any client-side video filter
- **DBD's own in-game brightness / gamma slider** — whatever it is set to now is baked into the calibration; leave it
- HDR (`hdr = 0` in Moonlight; keep it there)

`CENTRE_PRIOR = (112.0, 102.0)` is likewise geometry-bound: keep the stream at 1920x1080.

Audio is not in the control loop. Audio changes are free.

## Safe now — host-side, in-game (zero pixel impact)

- [ ] **FOV slider to default (87).** OPEN — not found in Settings on 2026-09-01; searched Graphics and Accessibility. Killer-only setting, shipped in the Beta tab in 2023 and relocated since. Value also lives in `GameUserSettings.ini` on the host if the menu route stays unavailable. The slider offsets the audio pan, so a widened FOV degrades directional tracking while phasing. Does not move the skill-check HUD, which is centred UI. Note: the **Shadowborn** perk widens FOV and reintroduces the same offset.
- [x] **Music sliders to 0** (done 2026-09-01) — menu, in-game, chase. Terror-radius music is the main mask over breathing and footsteps.
- [x] **In-game "Headphones" option — set OFF 2026-09-01.** It is a stereo-widening pass, not true HRTF, and it smears distance cues. Many Spirit players track better with it off.

## Safe now — client-side, macOS (zero pixel impact)

- [ ] **Disable Windows Sonic / Dolby Atmos — this is a HOST WINDOWS setting, not in-game.** Tray speaker icon -> Spatial sound -> Off. Sunshine's WASAPI loopback capture sits downstream of the Windows audio engine, so a spatialiser there is baked into the stream before it reaches the client.
- [x] **Keep Moonlight `audiocfg = 0` (stereo).** Requesting 5.1 means a 5.1 -> headphone downmix on macOS, which is worse than the game's own stereo image.
- [x] **SoundSource audio latency** — lowered 2026-09-01. Was `latencyMode = high`. Larger buffers delay game audio relative to video, which is the wrong direction for a killer whose tracking is audio-timed. Set via Settings -> Advanced -> Audio Latency (exact buffer mapping per value not confirmed; use the UI labels).
- [ ] **SoundSource EQ on `Moonlight.app`.** Per-application, applied after decode, entirely on the client. Never touches the host.

### SoundSource EQ — cut, do not boost

SoundSource 6 hosts Apple's **AUGraphicEQ**; set it to **10 Bands**. Centres are powers of two (32 / 64 / 128 / 256 / 512 / 1k / 2k / 4k / 8k / 16k), not the ISO series. Prefer cuts and make up the loss with the app's volume slider: boosting clips against near-full-scale Opus, and a net-negative curve preserves the quiet-to-loud range that distance estimation depends on. Spirit is a distance-estimation problem, so compressing dynamics costs accuracy.

| Band | Move | Why | Confidence |
|---|---|---|---|
| 32 Hz | -6 | sub rumble, no positional information | solid |
| 64 Hz | -5 | phase-sound body, the main mask while phasing | **unmeasured** |
| 128 Hz | -4 | ambience and map drone | **unmeasured** |
| 256 Hz | -3 | mud, masks up into the breathing band | plausible |
| 512 Hz | -1 | light touch | plausible |
| 1 kHz | 0 | breathing, grunts of pain | leave |
| 2 kHz | 0 | | leave |
| 4 kHz | +1 | footstep transients, grass rustle | plausible |
| 8 kHz | 0 | | leave |
| 16 kHz | -3 | little Opus content, mostly hiss | solid |

**Make-up gain is required.** The curve is net-negative, so raise Moonlight's volume in SoundSource until overall loudness matches flat. Without it you are comparing level, not clarity. Save the curve as a named preset so it can be A/B'd against flat.

Applied and verified 2026-09-01: curve matches the table, `16kHz Band: -3 dB` confirmed in the UI.

Latency cost lands on audio only; the bot's loop is video, so round-trip is unaffected.

### Measure the phase band before trusting the two unmeasured rows

The 64/128 Hz placement is from listening, not analysis, and the phase sound is the dominant mask now that music is off. Audio Hijack settles it with a known-good / known-bad pair:

1. Session -> **Application** block on Moonlight -> **Recorder** + spectrum analyser inline.
2. Capture (a) a phase with no survivor nearby, (b) a phase with a survivor running close.
3. Difference the spectra. Overlap = the masking band to cut. Energy present only in (b) = the band to protect.
4. Move the cuts onto the measured overlap; flatten the rest.

## Needs a validation match — do not flip blind

One change per match, then `tools/rescore_policy.py`, per established methodology.

1. **`yuv444 = 1`.** The needle is red and `NEEDLE_REDNESS` is a chroma measurement, but 4:2:0 carries chroma at quarter resolution. 4:4:4 should sharpen exactly the channel detection depends on, and `MIN_RUN_DEG = 3.0` / `CLOSE_DEG = 4.0` exist to paper over the resulting speckle. Requires HEVC or AV1 and host NVENC 4:4:4 support. Expect thresholds to need a re-check even if sigma improves.
2. **`bitrate` 28000 -> 40000.** Low for 1080p120. Same speckle argument, weaker lever than (1).
3. **Host game capped at 120 fps** (distinct from Moonlight's `fps = 120`, which is the stream rate). Still open as discriminator #1 in `NOTES-local.md`: if the game samples input once per rendered frame, 60 Hz contributes `16.7/sqrt(12)` = 4.8 ms, the largest single identified jitter term.

Ranked by expected effect on sigma: (1) > (3) > (2).

## Ruled out

- **Shaders, decided 2026-09-01: not running any.** ReShade is EAC-whitelisted and BHVR has stated nobody is banned for it, so this is **not** a ban decision — it is a calibration one. Shaders are a visibility tool for the operator's eyes; the bot reads the same pixels and only one of the two is calibrated.

  Two paths exist if this is ever revisited, neither taken:
  1. Run a preset and disable it for armed matches. Free, but the failure mode is forgetting — detection degrades silently and only surfaces downstream as a sigma or miss spike. Mitigable with a startup luminance probe against a stored reference on a static UI region, reusing the `static_image` median machinery (~30 lines), which would catch any pixel-transform drift regardless of cause.
  2. Make the tracker thresholds relative to per-frame statistics rather than absolute. Filters then stop mattering, but it needs revalidation against the full landings history. Only worth it for permanent shader use.

- **Any client-side video filter** (sharpening, gamma, saturation), same reason.
- **HDR** (`hdr = 0`). Changes the whole luminance mapping.
- **Engine.ini clarity configs** (no-grass, no-fog, brighter red stain). Disabled engine-side by BHVR; the widely-copied presets are placebo now.

## Stream judder is external CPU load — not the stream, the recorder, or V-Sync (2026-09-03)

**Symptom.** Panning and mouse movement go choppy at random during play. Not periodic, not correlated with skill checks, and — the detail that cracked it — **still present on the Steam screen after quitting DBD**, which rules out the game, the bot's decisions and anything scene-dependent in one observation.

**Cause: an unrelated project's `next build` saturating the Mac.** Caught live at **123.8% CPU** with a twelve-worker fan-out (60-85% each), WindowServer starved at 58-70%, and a **1-minute load average of 43.8 on a 14-core M3 Max** — roughly 3x oversubscription. It ran at least twice in twenty minutes (23:58 and 00:11, `.next/BUILD_ID` mtime), launched from a Claude session sitting in `~/dev/human`. Nothing in this repo, this stream or this game was involved. Under 3x oversubscription the compositor cannot present on time, so every streamed frame arrives late regardless of what the link is doing.

**Three explanations that were checked and are wrong.** Each is worth keeping, because each is the obvious first guess:

1. **Not the recorder.** Measured on `frames/bout_20260902-233144` (2827 frames, 7 min). Every burst contains both halves of a natural experiment — the pre-roll frames were ring-buffered while nothing was being written, the tail frames while both encoder threads were saturated:

   | | median | p90 | worst | >60 ms |
   |---|---|---|---|---|
   | pre-roll (not writing) | 28.0 ms | 34.3 | **479.0** | 19 |
   | hot window (writing) | 27.9 ms | 36.4 | 127.2 | 23 |

   Identical medians, and the worst stall of the match happened while the recorder wrote nothing. **Caveat, and it is a real one: this tests encoding and disk I/O only.** `ClipRecorder.offer` appends to the ring on *every* frame, so the ring is running in both columns and this experiment cannot see its cost. On 36 GB the few hundred MB of retained frames is not a plausible cause, but it is untested, not excluded.

2. **Not V-Sync,** and the reasoning that got there is the transferable part. `vsync = 0` and `framepacing = 0` are set deliberately, to buy back the frame of latency the 40-43 ms round trip is calibrated against, and V-Sync off genuinely does let arrival jitter reach the eye unsmoothed. But **it has been off the whole time, and a constant cannot explain an onset.** It is a modifier — the machine has no buffer to absorb a stall — not a cause. Do not "fix" it: turning V-Sync on shifts the round trip and invalidates the lead constant and the seed.

3. **Not the link.** No packet loss was needed to explain anything once the load was visible.

**The one-step diagnostic, before any stream theory.** Run `uptime`. Above a load of ~6 on this machine the compositor is contended, and *anything* observed about the stream is a statement about the load rather than about the stream. Only below that is Moonlight's overlay (Settings -> "Show performance stats while streaming") worth reading, and it splits the remainder: network dropped frames means the link; a clean network with spiking decode/render means local contention.

**Why this is a data-integrity finding and not just an operator annoyance.** Every constant in this project — `ROUND_TRIP_MS`, the seed, `AIM_BIAS_DEG` — is fitted to measured round trips. A match played under 3x oversubscription inflates those trips and adds jitter that is the scheduler's, not the link's, and it lands in the landings log looking exactly like link data. **A match played under load must not be scored.** The 23:31-23:38 bout on 2026-09-02 overlaps the window when builds were firing and carries a 479 ms stall in a stretch where the recorder wrote nothing; whether its trip distribution is shifted off the 40-43 ms baseline is **unchecked** — settle that before counting it toward the bias-3.0 evidence.

**Cheap guard worth building:** the `dbd` launch already skips on preconditions. A load-average check there — refuse to arm, or warn loudly, above ~6 — would make it impossible to collect calibration data under a contended scheduler again. Not built.

## Status at close (2026-09-01)

Done: music off, in-game Headphones off, SoundSource latency lowered, AUGraphicEQ curve applied to `Moonlight.app`, stereo confirmed.

Open, in payoff order:
1. Windows spatial sound -> Off on the host (one click, tray icon).
2. EQ make-up gain, and save the curve as a named preset for A/B.
3. FOV to 87 — slider not locatable in Settings on 2026-09-01.
4. Measure the phase band to firm up the 64/128 Hz rows.

Deferred to their own validation matches, one change each: `yuv444`, bitrate 28000 -> 40000, host game 120 fps cap.

**Attribution caveat:** four things changed in one sitting. Do not attribute a change in feel to any single one of them, and note that the latency-mode fix is plausibly a larger effect than the EQ curve.

## Rollback

Moonlight prefs snapshot: `docs/moonlight-prefs-backup-20260901.plist`

```bash
defaults import com.moonlight-stream.Moonlight docs/moonlight-prefs-backup-20260901.plist
```
