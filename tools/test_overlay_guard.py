"""The bot's own check that the status overlay is not inside the frames it reads.

The overlay places itself clear of the capture box (`overlay_state.place`), but that is
the overlay's promise about itself, made from a file that can lag a geometry change by a
poll. This is the end-state check from the other side: the focus watcher already walks the
on-screen window list every frame, and now also reports whether any window owned by the
overlay's pid intersects the capture box. The loop refuses those frames.

`scan_windows` is pure over a list of CGWindowList-shaped dicts, so it is tested here
without a window server.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbd.utils.focus_watcher import FocusWatcher, scan_windows

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def win(owner, pid, x, y, w, h, layer=0, title=""):
    return {"kCGWindowOwnerName": owner, "kCGWindowOwnerPID": pid, "kCGWindowName": title,
            "kCGWindowLayer": layer,
            "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h}}


MOONLIGHT = win("Moonlight", 100, 0, 0, 1512, 982)
BOX = {"left": 492, "top": 157, "width": 529, "height": 529}
OVERLAY_PID = 4242


def test_overlay_in_the_corner_is_fine():
    windows = [win("Python", OVERLAY_PID, 1100, 16, 380, 58, layer=1000), MOONLIGHT]
    front, present, obstructed = scan_windows(windows, "Moonlight",
                                              guard_pid=OVERLAY_PID, guard_box=BOX)
    check("the overlay does not become the frontmost app", front == "Moonlight", front)
    check("the stream is still present", present)
    check("a corner overlay does not obstruct", obstructed is False)


def test_overlay_inside_the_box_is_reported():
    windows = [win("Python", OVERLAY_PID, 600, 300, 380, 58, layer=1000), MOONLIGHT]
    _, _, obstructed = scan_windows(windows, "Moonlight",
                                    guard_pid=OVERLAY_PID, guard_box=BOX)
    check("an overlay window inside the capture box is reported", obstructed is True)


def test_only_the_overlay_pid_counts():
    windows = [win("Notification", 77, 600, 300, 380, 58, layer=1000), MOONLIGHT]
    _, _, obstructed = scan_windows(windows, "Moonlight",
                                    guard_pid=OVERLAY_PID, guard_box=BOX)
    check("someone else's window is not the overlay's problem", obstructed is False)


def test_no_guard_no_report():
    windows = [win("Python", OVERLAY_PID, 600, 300, 380, 58, layer=1000), MOONLIGHT]
    _, _, obstructed = scan_windows(windows, "Moonlight")
    check("without a guard nothing is reported", obstructed is False)


def test_watcher_carries_the_guard():
    watcher = FocusWatcher(query="Moonlight")
    check("a fresh watcher is never obstructed", watcher.last_obstructed is False)
    watcher.guard_pid = OVERLAY_PID
    watcher.guard_box = lambda: BOX
    check("the guard is settable after construction",
          watcher.guard_pid == OVERLAY_PID and watcher.guard_box() == BOX)


def main():
    print("overlay guard")
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
