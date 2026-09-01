"""The part of the review tool that moves data, plus the keys that drive it.

Everything that can lose a bout lives in `scan` / `apply_selection` / `empty_discard`, so
those run headless against temp directories. The defaults matter as much as the moves: a
bout that arrives pre-checked would be kept by a careless ENTER, which is the one outcome
this tool exists to prevent.

The last three tests run the tool in a real pty, because the headless ones cannot see the
class of bug that actually bit: they hand `loop` integer key codes, while a terminal sends
escape SEQUENCES. ESC used to mean quit, so backing out discarded the selection instead of
applying it. A stub screen passes that every time.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dbd.utils import bout_session
from review_recordings import apply_selection, empty_discard, pending_notice, scan

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def make_bout(root, name, checks=2, reviewed=False, kind=bout_session.WIDE_BOUT,
              active=None, writer_pid=None):
    directory = os.path.join(root, name)
    os.makedirs(directory, exist_ok=True)
    for i in range(3):
        with open(os.path.join(directory, f"{i:06d}.jpg"), "wb") as f:
            f.write(b"x" * 1024)
    meta = {
        "kind": kind,
        "started": "20260829-2201" + "00",
        "content": {"left": 0, "top": 0, "width": 1920, "height": 1080},
        "geometry": {"side": 672, "centre_in_box": [161, 202]},
        "gap_seconds": 300.0,
        "quality": 92,
        "reviewed": reviewed,
        "frames": 3,
        "checks": [{"at": "22:0%d:00" % i, "desc": "great", "path": "predictive"}
                   for i in range(checks)],
    }
    if active is not None:
        meta["active"] = active
    if writer_pid is not None:
        meta["writer_pid"] = writer_pid
    with open(os.path.join(directory, "bout.json"), "w") as f:
        json.dump(meta, f)
    return directory


def test_everything_starts_unchecked():
    root = tempfile.mkdtemp()
    try:
        make_bout(root, "bout_a")
        make_bout(root, "bout_b")
        bouts = scan(root)
        check("both bouts found", len(bouts) == 2, f"{len(bouts)} found")
        check("nothing is checked by default", all(not b.keep for b in bouts),
              "a bout arrived pre-checked")
    finally:
        shutil.rmtree(root)


def test_reviewed_bouts_do_not_come_back():
    root = tempfile.mkdtemp()
    try:
        make_bout(root, "bout_a", reviewed=True)
        make_bout(root, "bout_b")
        names = [b.name for b in scan(root)]
        check("reviewed bout is skipped", names == ["bout_b"], str(names))
    finally:
        shutil.rmtree(root)


def test_record_frames_sessions_are_ignored():
    root = tempfile.mkdtemp()
    try:
        plain = os.path.join(root, "session_20260829_154535")
        os.makedirs(plain)
        open(os.path.join(plain, "000000.jpg"), "wb").write(b"x")
        make_bout(root, "bout_a")
        names = [b.name for b in scan(root)]
        check("a record_frames session is not a bout", names == ["bout_a"], str(names))
    finally:
        shutil.rmtree(root)


def test_apply_keeps_checked_and_moves_the_rest():
    root = tempfile.mkdtemp()
    try:
        make_bout(root, "bout_keep")
        make_bout(root, "bout_drop")
        bouts = scan(root)
        for b in bouts:
            b.keep = b.name == "bout_keep"
        kept, discarded, failures = apply_selection(bouts, root)

        check("one kept, one discarded", (kept, discarded) == (1, 1), f"{kept}/{discarded}")
        check("no failures", failures == [], str(failures))
        check("kept bout stays put", os.path.isdir(os.path.join(root, "bout_keep")))
        check("discarded bout left the root",
              not os.path.isdir(os.path.join(root, "bout_drop")))
        check("discarded bout is in discard/, not deleted",
              os.path.isdir(os.path.join(root, "discard", "bout_drop")),
              "a discard must be reversible")
        check("kept bout is now marked reviewed",
              bout_session.load(os.path.join(root, "bout_keep"))["reviewed"] is True)
        check("kept bout does not reappear on rescan", scan(root) == [] or
              [b.name for b in scan(root)] == [], str([b.name for b in scan(root)]))
    finally:
        shutil.rmtree(root)


def test_discard_never_collides():
    root = tempfile.mkdtemp()
    try:
        make_bout(root, "bout_a")
        bouts = scan(root)
        apply_selection(bouts, root)
        make_bout(root, "bout_a")           # same name recorded again
        apply_selection(scan(root), root)
        inside = sorted(os.listdir(os.path.join(root, "discard")))
        check("a repeat name does not overwrite the first discard", len(inside) == 2,
              str(inside))
    finally:
        shutil.rmtree(root)


def test_discard_pile_is_not_rescanned():
    root = tempfile.mkdtemp()
    try:
        make_bout(root, "bout_a")
        apply_selection(scan(root), root)
        check("discarded bouts do not reappear", scan(root) == [],
              str([b.name for b in scan(root)]))
    finally:
        shutil.rmtree(root)


def test_empty_discard_deletes_only_the_pile():
    root = tempfile.mkdtemp()
    try:
        make_bout(root, "bout_keep")
        make_bout(root, "bout_drop")
        bouts = scan(root)
        for b in bouts:
            b.keep = b.name == "bout_keep"
        apply_selection(bouts, root)
        count, freed = empty_discard(root)
        check("one bout deleted", count == 1, str(count))
        check("bytes reported", freed > 0, str(freed))
        check("the kept bout survives", os.path.isdir(os.path.join(root, "bout_keep")))
        check("the pile is empty",
              os.listdir(os.path.join(root, "discard")) == [],
              str(os.listdir(os.path.join(root, "discard"))))
    finally:
        shutil.rmtree(root)


# --- the real terminal --------------------------------------------------------------
#
# The tests above drive `loop` with integer key codes, which is exactly why they could not
# see the bug that mattered: they bypass terminal byte parsing entirely. Under ncurses a
# key arrives as an escape SEQUENCE, and `27` on its own used to mean quit — so ESC threw
# the whole selection away without applying it. These run the tool in a real pty instead.
#
# Note the arrow bytes: once ncurses enables keypad it puts the terminal in APPLICATION
# cursor mode, where Down is \x1bOB, not the normal-mode \x1b[B. Sending the wrong one
# tests nothing and looks like a navigation bug.

KEY_DOWN_APP = b"\x1bOB"



def test_pending_is_silent_when_nothing_is_waiting():
    """Silence is the contract: this runs at the start of every `dbd`, and a line printed
    every evening is a line nobody reads by the third one."""

    root = tempfile.mkdtemp()
    try:
        check("an empty root says nothing", pending_notice(scan(root)) == "",
              repr(pending_notice(scan(root))))
        make_bout(root, "bout_00", checks=2, reviewed=True)
        check("a reviewed bout says nothing either",
              pending_notice(scan(root)) == "", repr(pending_notice(scan(root))))
    finally:
        shutil.rmtree(root)


def test_pending_names_what_is_waiting():
    root = tempfile.mkdtemp()
    try:
        for i in range(3):
            make_bout(root, f"bout_{i:02d}", checks=i + 1)
        notice = pending_notice(scan(root))
        check("it counts the bouts", notice.startswith("3 unreviewed bouts"), notice)
        check("and sizes them", "MB)" in notice or "GB)" in notice, notice)
        make_bout(root, "bout_03", checks=1, reviewed=True)
        check("a reviewed bout is not counted",
              pending_notice(scan(root)).startswith("3 unreviewed bouts"),
              pending_notice(scan(root)))
    finally:
        shutil.rmtree(root)


def drive_tui(root, keys, settle=1.2, per_key=0.5):
    """Run the tool in a pty, send `keys`, return the directories still in `root`."""

    import fcntl
    import pty
    import select
    import struct
    import termios
    import time

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.execv(os.path.join(repo, ".venv/bin/python"),
                 ["python", os.path.join(repo, "tools/review_recordings.py"),
                  "--root", root])
    # curses cannot lay out a screen of unknown size, and a 0x0 pty makes it fail on start.
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))

    def drain(seconds):
        end = time.time() + seconds
        while time.time() < end:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    if not os.read(fd, 65536):
                        return False
                except OSError:
                    return False
        return True

    drain(settle)
    for key in keys:
        try:
            os.write(fd, key)
        except OSError:
            break
        if not drain(per_key):
            break
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    return sorted(n for n in os.listdir(root)
                  if os.path.isdir(os.path.join(root, n)) and n.startswith("bout_"))


def test_pty_arrow_then_space_keeps_the_second_bout():
    root = tempfile.mkdtemp()
    try:
        for i in range(3):
            make_bout(root, f"bout_{i:02d}", checks=i + 1)
        left = drive_tui(root, [KEY_DOWN_APP, b" ", b"\r", b"y"])
        check("arrow moves, space keeps, enter+y applies", left == ["bout_01"], str(left))
    finally:
        shutil.rmtree(root)


def test_pty_esc_does_not_discard_the_session():
    """ESC used to mean quit, which threw the selection away. It must be inert now."""

    root = tempfile.mkdtemp()
    try:
        for i in range(3):
            make_bout(root, f"bout_{i:02d}", checks=i + 1)
        left = drive_tui(root, [b"\x1b", b" ", b"\r", b"y"])
        check("ESC is inert and the selection survives it", left == ["bout_00"], str(left))
    finally:
        shutil.rmtree(root)


def test_pty_q_quits_and_changes_nothing():
    root = tempfile.mkdtemp()
    try:
        for i in range(3):
            make_bout(root, f"bout_{i:02d}", checks=i + 1)
        left = drive_tui(root, [b" ", b"q"])
        check("q quits with nothing moved",
              left == ["bout_00", "bout_01", "bout_02"], str(left))
    finally:
        shutil.rmtree(root)


# --- the live-bout guard ---------------------------------------------------------------
# A bout the recorder is still filling must never reach the review list. `apply_selection`
# moves a discarded bout with `shutil.move`, so offering a live one lets a keystroke pull
# the directory out from under the writer threads mid-match. The flag is only half of it:
# a `kill -9` leaves `active` set forever, and a bout that can never be reviewed is the
# same silent loss the pending notice exists to end — so liveness is the writer's pid,
# not the flag alone.

def test_active_bout_with_live_writer_is_hidden():
    root = tempfile.mkdtemp()
    try:
        make_bout(root, "bout_done")
        make_bout(root, "bout_live", active=True, writer_pid=os.getpid())
        names = [b.name for b in scan(root)]
        check("a bout being written is not offered", names == ["bout_done"], str(names))
    finally:
        shutil.rmtree(root)


def test_active_bout_with_dead_writer_is_offered():
    root = tempfile.mkdtemp()
    try:
        # A pid that cannot be running: claimed, then reaped.
        dead = os.fork()
        if dead == 0:
            os._exit(0)
        os.waitpid(dead, 0)
        make_bout(root, "bout_orphan", active=True, writer_pid=dead)
        names = [b.name for b in scan(root)]
        check("an orphaned bout survives its writer", names == ["bout_orphan"], str(names))
    finally:
        shutil.rmtree(root)


def test_bout_without_active_key_is_offered():
    root = tempfile.mkdtemp()
    try:
        make_bout(root, "bout_old")
        names = [b.name for b in scan(root)]
        check("a pre-flag bout still reviews", names == ["bout_old"], str(names))
    finally:
        shutil.rmtree(root)


def test_closed_bout_is_offered():
    root = tempfile.mkdtemp()
    try:
        make_bout(root, "bout_closed", active=False, writer_pid=os.getpid())
        names = [b.name for b in scan(root)]
        check("active=False reviews even with a live pid", names == ["bout_closed"], str(names))
    finally:
        shutil.rmtree(root)


def main():
    print("review_recordings")
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
