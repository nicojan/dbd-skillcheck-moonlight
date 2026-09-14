Run the overnight off-centre tile sweep on the one recorded session that has never had one, then tell me what it found.

BACKGROUND (verified 2026-09-13, don't re-derive it):
`frames/session_20260812_193737` is 8.5 GB, 28,882 frames, 100% intact. It has a
`centre_detections.json` (the centre-crop pass was run) but zero `hits/` — it has never
been swept with `tools/scan_frames.py`. That's chronology, not an oversight: `wide_capture.py`
and the whole off-centre question didn't exist until 08-29, two weeks after this was recorded.
It is the only complete session that has never been swept, and the answer decides whether
8.5 GB gets deleted or kept.

RUN EXACTLY THIS:

    cd ~/dev/dbd_autoSkillCheck && caffeinate -is .venv/bin/python tools/scan_frames.py \
      --frames frames/session_20260812_193737 \
      2>&1 | tee sweep-session_20260812_193737.log

Three flags NOT to add, each checked:
  * `caffeinate -is` is required. macOS idle-sleeps partway through a 3-hour job and you
    wake to a half-finished sweep with no marker for where it stopped.
  * Do NOT add `--hits-only`. Its help says "only count classes the bot would act on, not
    'out'/'frontier' sightings", which sounds like a noise filter. This session's centre
    pass is 423 `repair-heal (out)`, 216 `wiggle (out)`, 22 `ante-frontier`, 19
    `full black (out)` against 217 `(great)`. That flag would discard ~3/4 of the real
    detections, including every displaced check the sweep exists to find.
  * Leave `--every` at 1. `--every 2` halves the runtime but samples a ~0.7 s check at half
    rate. The 08-29 sweep found its nine checks at `--every 1`.

RUNTIME ~3 HOURS. Measured at 2.7 fps on this session's own frames, matching the 2.6-3.3 fps
NOTES-local.md records for the 08-29 sweep. 28,882 / 2.7 = ~2h58m. It saturates the CPU, so
DO NOT play a match while it runs — a match under load is unscorable and that has already
cost four matches on this project. Check `uptime` before concluding anything about timing.

HOW TO READ THE RESULT. The tool computes the answer itself — do not eyeball coordinates.
It classifies every hit tile as CENTRE or not and prints a one-line verdict, e.g.

    hit positions (tile origin -> frames / checks / multi-frame checks):
      (896, 448)  x20 frames / 1 runs / 1 check-like  offset=(48, 20)  CENTRE
    => no multi-frame detection landed outside the production centre crop.

The centre pass already found 27 distinct centred check events in this session (first at
frames 999-1018, last ending at frame 25,861), which is the floor a working sweep must clear.

  * Zero detections, or well under ~27 check-like runs -> THE SWEEP IS BROKEN, not the
    session empty. The tool says as much itself. Do not report "nothing found"; report that
    it failed. (A control over frames 0-1020 was run on 2026-09-13 and passed: it found the
    known check at frames 999-1018, 20 frames, 1 run, at tile (896, 448), labelled CENTRE.
    So a broken result means something changed, not that this was always so.)
  * Roughly 27+ runs and the verdict line says "no multi-frame detection landed outside the
    production centre crop" -> nothing off-centre here. The 8.5 GB is expendable and the
    question is closed.
  * Any tile listed WITHOUT the CENTRE label, with a multi-frame run -> a genuine off-centre
    check, like the nine in the 08-29 Doctor match. That is the find that justifies keeping
    the frames. Confirm each by eye in `hits/` before claiming it; the 08-29 sweep had zero
    false positives, but only because they were checked. Single-frame hits are not checks.


DISK: it writes annotated PNGs to `frames/session_20260812_193737/hits/`. The 08-29 sweep's
hits came to 4.7 GB for 2410 detections, so budget a few GB. The volume is at 92% with ~76 GB
free. Note the sweep makes the directory BIGGER before it tells you whether it can be deleted.

WHEN IT FINISHES: report the event count, the position histogram, and which of the three
outcomes above it is. Then append a short entry to the top of "Resume here" in NOTES-local.md
saying what was found, and stop — the deletion itself is my call, not yours.

Read NOTES-local.md "Resume here" first. This repo's rule is that a visible mechanism is not
evidence, and that a tool reporting success can still be answering the wrong question.
