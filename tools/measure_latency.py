"""Measure the closed-loop latency that makes hits land late.

The detector fires on the earliest class the model has (ante-frontier), and the
ante-frontier delay is already at 0. If hits still land past the Great band, the
remaining error is latency in the loop, and there is no dial for that until we know
where the time goes.

This measures the whole round trip the way the detector experiences it: press a key,
then watch our own captured frames until the screen changes.

    us -> CGEvent -> Moonlight -> network -> host input -> host renders
       -> host encodes -> network -> Moonlight decodes -> screen -> our capture

That total is exactly how far the needle travels between "we decide to press" and
"the press takes effect on the host", so it is the compensation the model would need
to fire earlier by.

Setup: focus Moonlight with a text field on the host that visibly changes when a key
is typed (the code redemption box works). Then:

    python tools/measure_latency.py --trials 10

Interpretation:
    a large number  -> stream round trip dominates; tuning hit_ante cannot fix it
    a small number  -> our ~29 ms sampling interval dominates; frame rate is the lever
"""

import argparse
import os
import sys
from time import monotonic, sleep, strftime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.utils.directkeys import PressKey, ReleaseKey, SPACE
from dbd.utils.focus_watcher import FocusWatcher
from dbd.utils.monitoring_window import Monitoring_window

SETTLE_SECONDS = 1.0
RESPONSE_TIMEOUT = 1.5


def parse_args():
    p = argparse.ArgumentParser(description="Measure end-to-end keypress->pixel latency")
    p.add_argument("--window", default="Moonlight")
    p.add_argument("--aspect", default="16:9")
    p.add_argument("--trials", type=int, default=10)
    p.add_argument("--gap", type=float, default=1.5, help="seconds between trials")
    p.add_argument("--noise-factor", type=float, default=3.0,
                   help="change threshold as a multiple of measured idle noise")
    p.add_argument("--pixel-delta", type=float, default=30.0,
                   help="per-pixel brightness change counted as 'changed' (0-255)")
    p.add_argument("--min-pixels", type=int, default=80,
                   help="floor on changed-pixel count, so noise can never trigger")
    p.add_argument("--roi", default=None,
                   help="watch only x,y,w,h (content coords) — e.g. the text field")
    return p.parse_args()


def parse_roi(text):
    if not text:
        return None
    parts = [int(v) for v in text.split(",")]
    if len(parts) != 4:
        raise ValueError("--roi must be x,y,w,h")
    return tuple(parts)


def parse_aspect(text):
    if text.lower() in ("fill", "none"):
        return None
    if ":" in text:
        w, h = text.split(":")
        return float(w) / float(h)
    return float(text)


def log(message):
    print(f"[{strftime('%H:%M:%S')}] {message}", flush=True)


def grab_gray(monitor, roi=None):
    frame = np.array(monitor.get_raw_frame(), dtype=np.uint8)[:, :, :3]
    gray = frame.astype(np.float32).mean(axis=2)
    if roi is not None:
        x, y, w, h = roi
        gray = gray[y:y + h, x:x + w]
    return gray


def changed_pixels(current, baseline, pixel_delta):
    """Count of strongly-changed pixels.

    Frame-mean is the wrong statistic here: one typed character moves a few hundred
    pixels out of two million, shifting the mean by ~0.001, while diffuse encoder
    noise moves the mean far more than that. Counting pixels that changed by a large
    amount separates a hard-edged caret from soft video noise.
    """

    return int((np.abs(current - baseline) > pixel_delta).sum())


def measure_noise(monitor, pixel_delta, roi=None, seconds=SETTLE_SECONDS):
    """Worst idle frame-to-frame change count, as a floor for the trigger threshold."""

    previous = grab_gray(monitor, roi)
    worst = 0
    deadline = monotonic() + seconds
    samples = 0

    while monotonic() < deadline:
        current = grab_gray(monitor, roi)
        worst = max(worst, changed_pixels(current, previous, pixel_delta))
        previous = current
        samples += 1

    return worst, samples


def main():
    args = parse_args()

    monitor = Monitoring_window(
        window_query=args.window,
        crop_size=224,
        stream_aspect=parse_aspect(args.aspect),
        full_window=True,   # watch the whole frame; the change can be anywhere
    )
    watcher = FocusWatcher(query=args.window)
    monitor.start()

    log(f"waiting for '{args.window}' to take focus (ctrl-c to quit)...")
    while not watcher.is_active():
        sleep(0.2)

    roi = parse_roi(args.roi)
    log(f"focused. measuring idle noise floor{' in roi ' + str(roi) if roi else ''} — do not touch anything")
    noise, samples = measure_noise(monitor, args.pixel_delta, roi)
    threshold = max(noise * args.noise_factor, args.min_pixels)
    log(f"idle noise {noise} changed-px over {samples} frames -> trigger threshold {threshold:.0f} px")

    # A genuinely still screen idles in the hundreds of changed pixels. Anything in the
    # thousands means motion, and the threshold then rides so high that only ambient
    # change trips it — which produced a set of unusable 200-995 ms outliers once.
    if noise > 2000:
        log(f"WARNING: screen is not static ({noise} changed px while idle).")
        log("  Trials may trigger on ambient motion rather than the keypress.")
        log("  Go to a still screen, or pass --roi x,y,w,h around the text field.")

    log(f"running {args.trials} trials; watch the text field fill up")

    latencies = []

    try:
        for trial in range(1, args.trials + 1):
            if not watcher.is_active():
                log("focus lost — stopping")
                break

            baseline = grab_gray(monitor, roi)

            sent_at = monotonic()
            PressKey(SPACE)
            sleep(0.005)
            ReleaseKey(SPACE)

            detected_at = None
            peak = 0
            while monotonic() - sent_at < RESPONSE_TIMEOUT:
                current = grab_gray(monitor, roi)
                count = changed_pixels(current, baseline, args.pixel_delta)
                peak = max(peak, count)
                if count > threshold:
                    detected_at = monotonic()
                    break

            if detected_at is None:
                log(f"  trial {trial:2d}: no change within {RESPONSE_TIMEOUT}s "
                    f"(peak {peak} px vs threshold {threshold:.0f})")
            else:
                ms = (detected_at - sent_at) * 1000
                latencies.append(ms)
                log(f"  trial {trial:2d}: {ms:6.1f} ms  ({peak} px changed)")

            sleep(args.gap)

    except KeyboardInterrupt:
        log("interrupted")
    finally:
        monitor.stop()

    if not latencies:
        log("no measurements — was a text field focused on the host?")
        return 1

    arr = np.array(latencies)
    log("")
    log(f"trials {len(arr)}  min {arr.min():.1f}  median {np.median(arr):.1f}  "
        f"max {arr.max():.1f}  mean {arr.mean():.1f} ms")
    log("")
    log("note: this includes one capture interval (~29 ms at 34 fps), since a change")
    log("cannot be seen before the next frame is grabbed. Subtract that for the")
    log("stream's own contribution.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
