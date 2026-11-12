"""The on-stream status overlay: what the bot publishes, and what the badge says about it.

WHY THIS IS TESTED. The overlay exists to answer one question mid-match — is the bot
pressing right now? A badge that says ARMED while the bot is disarmed, crashed or frozen
is worse than no badge, because the operator stops checking the log. So the two failure
directions that matter are pinned here: every non-pressing state must render as
non-pressing, and a state file nobody is updating must stop claiming anything.

The other thing pinned is placement. The bot reads the screen, not the video stream, so a
panel drawn over the capture box would be IN the frames the classifier reads. The overlay
must refuse any position that touches that box.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbd.utils import overlay_state as ov

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


# --- which state the bot is in ---------------------------------------------------------

def test_mode_precedence():
    check("focus lost wins over everything",
          ov.bot_mode(active=False, armed=True, performance_until=99.0, now=1.0) == "paused")
    check("disarmed wins over a Performance",
          ov.bot_mode(active=True, armed=False, performance_until=99.0, now=1.0) == "disarmed")
    check("inside a Performance is stand-down",
          ov.bot_mode(active=True, armed=True, performance_until=99.0, now=1.0) == "standdown")
    check("Performance over -> armed",
          ov.bot_mode(active=True, armed=True, performance_until=1.0, now=1.0) == "armed")


# --- the snapshot ----------------------------------------------------------------------

class _L:
    def __init__(self, verdict):
        self.verdict = verdict


def test_snapshot_tallies_verdicts():
    snap = ov.snapshot(mode="armed", wall=100.0, landings=[_L("GREAT"), _L("GREAT"),
                       _L("good"), _L("MISS"), _L(None)], reactive=3)
    check("fires count every predictive fire", snap["fires"] == 5, snap)
    check("GREAT/good/MISS counted",
          (snap["great"], snap["good"], snap["miss"]) == (2, 1, 1), snap)
    check("reactive presses carried", snap["reactive"] == 3, snap)
    check("schema version stamped", snap["v"] == ov.SCHEMA_VERSION, snap)


def test_snapshot_rejects_unknown_mode():
    try:
        ov.snapshot(mode="sleepy", wall=1.0)
    except ValueError:
        check("an unknown mode is refused, not rendered", True)
    else:
        check("an unknown mode is refused, not rendered", False)


def test_snapshot_is_json_and_rounded():
    snap = ov.snapshot(mode="standdown", wall=5.0, standdown_left=3.4567, load=4.21987,
                       fps=35.678, capture={"left": 1, "top": 2, "width": 3, "height": 4},
                       content={"left": 0, "top": 0, "width": 10, "height": 10})
    text = json.dumps(snap)
    check("round-trips through JSON", json.loads(text) == snap)
    check("seconds left rounded", snap["standdown_left"] == 3.5, snap["standdown_left"])
    check("load rounded", snap["load"] == 4.22, snap["load"])


# --- the file ------------------------------------------------------------------------

def test_write_then_read():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "overlay.json")
        snap = ov.snapshot(mode="armed", wall=1.0)
        check("write succeeds and creates the directory", ov.write_state(snap, path))
        check("read returns what was written", ov.read_state(path) == snap)
        check("no temp file left behind", os.listdir(os.path.dirname(path)) == ["overlay.json"])


def test_read_bad_file():
    with tempfile.TemporaryDirectory() as d:
        missing = os.path.join(d, "nope.json")
        check("missing file -> None", ov.read_state(missing) is None)
        broken = os.path.join(d, "broken.json")
        with open(broken, "w") as f:
            f.write("{not json")
        check("unparsable file -> None", ov.read_state(broken) is None)
        wrong = os.path.join(d, "wrong.json")
        with open(wrong, "w") as f:
            json.dump({"v": 999, "mode": "armed", "t": 1}, f)
        check("another schema version -> None", ov.read_state(wrong) is None)


def test_write_never_raises():
    check("unwritable path -> False, no exception",
          ov.write_state(ov.snapshot(mode="armed", wall=1.0), "/dev/null/x/overlay.json")
          is False)


def test_writer_keeps_only_the_latest_and_writes_final_last():
    import threading
    gate = threading.Event()
    seen = []

    def slow_write(snap, path):
        gate.wait(2.0)            # hold the first write so later offers pile up
        seen.append(snap["mode"])
        return True

    w = ov.StateWriter("unused", write=slow_write)
    w.offer(ov.snapshot(mode="paused", wall=1.0))
    for mode in ("armed", "standdown", "disarmed"):
        w.offer(ov.snapshot(mode=mode, wall=2.0))
    gate.set()
    w.close(final=ov.snapshot(mode="stopped", wall=3.0))
    check("the final snapshot is written last", seen[-1:] == ["stopped"], seen)
    check("superseded snapshots are dropped, not queued", len(seen) <= 3, seen)
    check("the thread has exited", not w._thread.is_alive())


def test_writer_counts_failures_and_never_raises():
    w = ov.StateWriter("/dev/null/x/overlay.json")
    w.offer(ov.snapshot(mode="armed", wall=1.0))
    w.close()
    check("a failed write is counted", w.failures == 1, w.failures)


# --- what the badge says --------------------------------------------------------------

def test_badge_states():
    now = 1000.0
    for mode, word in (("armed", "ARMED"), ("disarmed", "DISARMED"),
                       ("standdown", "STAND DOWN"), ("paused", "PAUSED")):
        b = ov.badge(ov.snapshot(mode=mode, wall=now, standdown_left=7.0), now)
        check(f"{mode} -> {word}", b.headline.startswith(word), b.headline)
    b = ov.badge(ov.snapshot(mode="armed", wall=now), now)
    check("only armed is green", b.tone == "go", b.tone)
    for mode in ("disarmed", "standdown", "paused"):
        b = ov.badge(ov.snapshot(mode=mode, wall=now), now)
        check(f"{mode} is not green", b.tone != "go", b.tone)
    b = ov.badge(ov.snapshot(mode="standdown", wall=now, standdown_left=7.2), now)
    check("stand-down shows seconds left", "7s" in b.headline, b.headline)
    check("stand-down says the bot presses nothing", "presses nothing" in b.detail,
          b.detail)


def test_badge_stale_and_missing():
    snap = ov.snapshot(mode="armed", wall=100.0)
    b = ov.badge(snap, 100.0 + ov.STALE_SECONDS + 0.1)
    check("a stale ARMED file does not say ARMED", "ARMED" not in b.headline, b.headline)
    check("...and is not green", b.tone != "go", b.tone)
    b = ov.badge(None, 100.0)
    check("no file -> not running, not green",
          b.tone != "go" and "NOT RUNNING" in b.headline, b.headline)
    b = ov.badge(ov.snapshot(mode="stopped", wall=100.0), 100.5)
    check("stopped -> not green", b.tone != "go" and "STOPPED" in b.headline, b.headline)


def test_badge_never_green_when_nothing_reaches_the_game():
    now = 50.0
    for mode in ("armed", "standdown"):
        b = ov.badge(ov.snapshot(mode=mode, wall=now, dry_run=True), now)
        check(f"dry run {mode} is not green and says so",
              b.tone != "go" and "DRY RUN" in b.headline, b)
        b = ov.badge(ov.snapshot(mode=mode, wall=now, can_press=False), now)
        check(f"no Accessibility {mode} is not green and says so",
              b.tone != "go" and "NO KEY ACCESS" in b.headline, b)
    b = ov.badge(ov.snapshot(mode="armed", wall=now, can_press=None), now)
    check("unknown access does not block the green", b.tone == "go", b)
    b = ov.badge(ov.snapshot(mode="armed", wall=now, guard=False), now)
    check("armed without the key tap is amber, not green", b.tone == "hold", b)
    b = ov.badge(ov.snapshot(mode="paused", wall=now, dry_run=True), now)
    check("paused still reads PAUSED in a dry run", b.headline == "PAUSED", b)


def test_backwards_clock_is_stale():
    b = ov.badge(ov.snapshot(mode="armed", wall=100.0), 90.0)
    check("a snapshot from the future is not believed", b.tone != "go", b)


def test_poller():
    import time
    calls = []
    p = ov.Poller(lambda: calls.append(1) or True, interval=0.01)
    deadline = time.time() + 1.0
    while p.value is None and time.time() < deadline:
        time.sleep(0.005)
    p.close()
    check("poller publishes the read", p.value is True, p.value)

    def boom():
        raise RuntimeError("x")
    q = ov.Poller(boom, interval=0.01)
    time.sleep(0.05)
    q.close()
    check("a raising read reads as unknown", q.value is None, q.value)


def test_default_path_is_off_the_external_volume():
    check("state file defaults to the internal disk",
          ov.DEFAULT_PATH.startswith(os.path.expanduser("~")), ov.DEFAULT_PATH)


def test_badge_detail_line():
    snap = ov.snapshot(mode="armed", wall=1.0, landings=[_L("GREAT"), _L("MISS")],
                       reactive=2, load=4.2, fps=35.0)
    detail = ov.badge(snap, 1.0).detail
    for part in ("2 fires", "1 great", "1 miss", "2 reactive", "load 4.2", "35 fps"):
        check(f"detail shows {part!r}", part in detail, detail)


# --- placement ------------------------------------------------------------------------

CONTENT = {"left": 0, "top": 25, "width": 1512, "height": 850}
CAPTURE = {"left": 492, "top": 157, "width": 529, "height": 529}


def test_overlaps():
    a = {"left": 0, "top": 0, "width": 10, "height": 10}
    check("disjoint", not ov.overlaps(a, {"left": 20, "top": 0, "width": 5, "height": 5}))
    check("touching within pad overlaps",
          ov.overlaps(a, {"left": 12, "top": 0, "width": 5, "height": 5}, pad=4))
    check("intersecting", ov.overlaps(a, {"left": 5, "top": 5, "width": 10, "height": 10}))


def test_place_corner_and_guard():
    rect = ov.place(CONTENT, CAPTURE, size=(260, 64), corner="top-right", margin=16)
    check("top-right sits inside the content's top-right",
          rect == {"left": 1512 - 16 - 260, "top": 25 + 16, "width": 260, "height": 64}, rect)
    check("...and clear of the capture box", not ov.overlaps(rect, CAPTURE, pad=ov.CAPTURE_PAD))
    check("top-left too",
          ov.place(CONTENT, CAPTURE, size=(260, 64), corner="top-left")["left"]
          == CONTENT["left"] + ov.DEFAULT_MARGIN)
    # A panel big enough to reach the box must be refused, not drawn into the frames.
    huge = ov.place(CONTENT, CAPTURE, size=(1400, 300), corner="top-right")
    check("a position touching the capture box is refused (None)", huge is None, huge)
    check("no capture box known -> refused", ov.place(CONTENT, None, size=(10, 10)) is None)
    try:
        ov.place(CONTENT, CAPTURE, size=(10, 10), corner="middle")
    except ValueError:
        check("unknown corner is an error", True)
    else:
        check("unknown corner is an error", False)


def test_to_appkit_flip():
    rect = {"left": 100, "top": 40, "width": 200, "height": 60}
    check("top-left origin -> bottom-left origin",
          ov.to_appkit(rect, primary_height=982) == (100, 982 - 40 - 60, 200, 60))


def main():
    print("overlay state")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if FAILED:
        print(f"\n{len(FAILED)} FAILED: {', '.join(FAILED)}")
        return 1
    print("\nall ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
