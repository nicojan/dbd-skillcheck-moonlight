"""Isolate one question: does Moonlight forward *synthetic* keystrokes to the host?

Detection accuracy is irrelevant if injected keys never leave this machine. Moonlight
is an SDL app, and depending on how it reads input, CGEvent-posted keys may or may not
be picked up and forwarded over the stream.

Run this, switch to the stream, and watch the host: put the game somewhere a spacebar
has an obvious visible effect (a lobby, a text field on the host desktop, a menu).

    python tools/test_keypress.py --presses 5

It waits for the stream to take focus before sending anything, so it can never type
into your editor.
"""

import argparse
import os
import sys
from time import sleep, strftime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.utils.directkeys import PressKey, ReleaseKey, SPACE
from dbd.utils.focus_watcher import FocusWatcher, frontmost_app_name


def log(message):
    print(f"[{strftime('%H:%M:%S')}] {message}", flush=True)


def main():
    p = argparse.ArgumentParser(description="Test synthetic keypress delivery through a stream")
    p.add_argument("--window", default="Moonlight")
    p.add_argument("--presses", type=int, default=5)
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--timeout", type=float, default=120.0)
    args = p.parse_args()

    watcher = FocusWatcher(query=args.window)
    log(f"waiting up to {args.timeout:.0f}s for '{args.window}' to take focus...")

    # Log every focus change while waiting. Without this, "you never switched" and
    # "you switched but the gate missed it" produce an identical timeout message.
    waited = 0.0
    seen = frontmost_app_name()
    log(f"  focus is currently: {seen!r}")

    while not watcher.is_active():
        current = frontmost_app_name()
        if current != seen:
            log(f"  focus changed: {seen!r} -> {current!r}")
            seen = current
        sleep(0.25)
        waited += 0.25
        if waited >= args.timeout:
            log(f"timed out — nothing sent. Last focused app: {seen!r}")
            log(f"if {seen!r} was the stream, the gate's match on {args.window!r} is wrong")
            return 1

    log(f"'{args.window}' is focused. Sending {args.presses} SPACE presses.")
    log("watch the host for a reaction.")
    sleep(1.0)

    sent = 0
    try:
        for i in range(1, args.presses + 1):
            if not watcher.is_active():
                log(f"focus lost after {sent} presses — stopping")
                break
            PressKey(SPACE)
            sleep(0.05)
            ReleaseKey(SPACE)
            sent += 1
            log(f"  sent SPACE {i}/{args.presses}")
            sleep(args.interval)
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        ReleaseKey(SPACE)

    log(f"done — {sent} presses sent. Did the host react?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
