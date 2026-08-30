"""Pick which recorded bouts to keep. Everything starts unchecked.

`autorun.py --record` writes one directory per bout — a stretch of play with no five
minute gap in it, which is about one match. Most of them are ordinary and worth nothing;
the reason to record at all is the rare one, and you cannot know which that was until
afterwards. So this lists them, keeps nothing by default, and makes keeping the deliberate
act rather than the automatic one. A recorder that keeps everything fills 70 GB in two
evenings and the rare bout is lost in the pile either way.

    .venv/bin/python tools/review_recordings.py            # the TUI
    .venv/bin/python tools/review_recordings.py --list     # print and exit, no changes
    .venv/bin/python tools/review_recordings.py --empty-discard

Keys: up/down or j/k move, SPACE toggles, `a` toggles all, `p` opens the middle frame of
the bout so you can see what it was, ENTER applies, `q` quits changing nothing.

Applying moves every UNCHECKED bout to `frames/discard/` and marks every checked one
reviewed so it does not come back next time. Discard is a move, not a delete: these frames
are unrepeatable, and the volume this runs on sits at 93% full precisely because deleting
in haste is how you lose the one match that mattered. Empty it yourself, once you are sure.
"""

import argparse
import curses
import os
import shutil
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from dbd.utils import bout_session

DEFAULT_ROOT = "frames"
BYTES_PER_MB = 1024 ** 2
BYTES_PER_GB = 1024 ** 3


def human_size(count):
    """MB below a gigabyte, GB above. A bout is tens of megabytes; "0.00 GB" says nothing."""

    if count < BYTES_PER_GB:
        return f"{count / BYTES_PER_MB:.0f} MB"
    return f"{count / BYTES_PER_GB:.2f} GB"


def plural(count, word):
    return f"{count} {word}" + ("" if count == 1 else "s")


class Bout:
    """One recorded bout and the tally that decides whether it is worth keeping."""

    def __init__(self, directory, meta):
        self.directory = directory
        self.meta = meta
        self.name = os.path.basename(directory)
        self.checks = list(meta.get("checks", []))
        self.frames = int(meta.get("frames", 0))
        self.bytes = bout_session.disk_bytes(directory)
        self.keep = False           # unchecked by default — the whole point

    @property
    def started(self):
        stamp = str(self.meta.get("started", ""))
        return stamp[9:11] + ":" + stamp[11:13] if len(stamp) >= 13 else "??:??"

    @property
    def ended(self):
        times = [c.get("at") for c in self.checks if c.get("at")]
        return times[-1][:5] if times else self.started

    @property
    def paths(self):
        return Counter(c.get("path", "?") for c in self.checks)

    def summary(self):
        by_path = self.paths
        parts = ", ".join(f"{n} {name}" for name, n in sorted(by_path.items()))
        return (f"{self.started}–{self.ended}   {len(self.checks):3d} checks"
                f"  ({parts or 'none'})".ljust(64)
                + f"{self.frames:5d} frames  {human_size(self.bytes):>8}")

    def middle_frame(self):
        names = sorted(n for n in os.listdir(self.directory) if n.endswith(".jpg"))
        return os.path.join(self.directory, names[len(names) // 2]) if names else None


def scan(root=DEFAULT_ROOT):
    return [Bout(d, m) for d, m in bout_session.find_bouts(root)]


def apply_selection(bouts, root=DEFAULT_ROOT):
    """Keep the checked, discard the rest. Returns (kept, discarded, failures).

    A move that fails must not be reported as a discard — losing track of where a bout
    went is worse than leaving it in place, so failures come back and the caller says so.
    """

    discard_root = os.path.join(root, bout_session.DISCARD_DIR)
    kept, discarded, failures = 0, 0, []
    for bout in bouts:
        if bout.keep:
            bout_session.mark_reviewed(bout.directory)
            kept += 1
            continue
        os.makedirs(discard_root, exist_ok=True)
        target = os.path.join(discard_root, bout.name)
        suffix = 1
        while os.path.exists(target):
            suffix += 1
            target = os.path.join(discard_root, f"{bout.name}-{suffix}")
        try:
            shutil.move(bout.directory, target)
            discarded += 1
        except OSError as e:
            failures.append((bout.name, str(e)))
    return kept, discarded, failures


def empty_discard(root=DEFAULT_ROOT):
    """Delete the discard pile. The only place in this tool that destroys anything."""

    discard_root = os.path.join(root, bout_session.DISCARD_DIR)
    if not os.path.isdir(discard_root):
        return 0, 0
    names = [n for n in sorted(os.listdir(discard_root))
             if os.path.isdir(os.path.join(discard_root, n))]
    freed = sum(bout_session.disk_bytes(os.path.join(discard_root, n)) for n in names)
    for n in names:
        shutil.rmtree(os.path.join(discard_root, n), ignore_errors=True)
    return len(names), freed


def print_list(bouts):
    if not bouts:
        print("no unreviewed bouts")
        return
    total = sum(b.bytes for b in bouts)
    for bout in bouts:
        print(f"  [ ] {bout.summary()}")
    print(f"\n{plural(len(bouts), 'bout')}, {human_size(total)}")


# --- the TUI ------------------------------------------------------------------------
#
# curses rather than a dependency: it is in the stdlib, this is a checklist, and the repo
# has no TUI framework in the venv. Rendering is kept apart from `scan`/`apply_selection`
# above so the part that moves data can be tested without a terminal.

HELP = " up/down move   SPACE keep   a all   p preview   ENTER apply   q quit "


def draw(screen, bouts, cursor, offset, message):
    screen.erase()
    height, width = screen.getmaxyx()
    keeping = [b for b in bouts if b.keep]
    header = (f" {plural(len(bouts), 'bout')}, keeping {len(keeping)} "
              f"({human_size(sum(b.bytes for b in keeping))} of "
              f"{human_size(sum(b.bytes for b in bouts))})")
    screen.addnstr(0, 0, header.ljust(width - 1), width - 1, curses.A_REVERSE)

    rows = max(height - 4, 1)
    for row, bout in enumerate(bouts[offset:offset + rows]):
        index = offset + row
        mark = "x" if bout.keep else " "
        line = f" [{mark}] {bout.summary()}"
        attr = curses.A_BOLD if index == cursor else curses.A_NORMAL
        if index == cursor:
            line = ">" + line[1:]
        screen.addnstr(row + 2, 0, line.ljust(width - 1), width - 1, attr)

    screen.addnstr(height - 2, 0, (message or HELP).ljust(width - 1), width - 1,
                   curses.A_REVERSE if message else curses.A_DIM)
    screen.refresh()


def preview(bout):
    """Open the bout's middle frame in whatever the OS uses. Never fatal."""

    path = bout.middle_frame()
    if path is None:
        return "no frames in that bout"
    try:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, path], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return f"opened {os.path.basename(path)}"
    except OSError as e:
        return f"could not open preview: {e}"


def loop(screen, bouts):
    """Returns True if the user chose to apply, False if they quit."""

    curses.curs_set(0)
    # Both of these are load-bearing, and the second was a live bug.
    #
    # keypad: without it an arrow key arrives as its raw bytes (ESC, '[', 'B') instead of
    # one KEY_DOWN. `curses.wrapper` already sets it, but `loop` is called directly by the
    # tests too, and a navigation key that silently does nothing is not worth the risk.
    #
    # ESC is NOT a quit key. It used to be, and that made the FIRST arrow press quit and
    # throw the session away — the ESC that opens the escape sequence was read as a bare
    # ESC. Verified in a pty: one KEY_DOWN produced "quit — nothing changed". `q` quits;
    # nothing else does.
    screen.keypad(True)
    try:
        curses.set_escdelay(25)     # shrink the window where a lone ESC can be mistaken
    except (AttributeError, curses.error):
        pass                        # older curses; ESC is unbound anyway
    cursor, offset, message = 0, 0, None
    while True:
        height = screen.getmaxyx()[0]
        rows = max(height - 4, 1)
        offset = min(max(offset, cursor - rows + 1), cursor)
        draw(screen, bouts, cursor, offset, message)
        message = None

        key = screen.getch()
        if key == ord("q"):
            return False
        if key in (curses.KEY_DOWN, ord("j")):
            cursor = min(cursor + 1, len(bouts) - 1)
        elif key in (curses.KEY_UP, ord("k")):
            cursor = max(cursor - 1, 0)
        elif key == ord(" "):
            bouts[cursor].keep = not bouts[cursor].keep
        elif key == ord("a"):
            target = not all(b.keep for b in bouts)
            for b in bouts:
                b.keep = target
        elif key == ord("p"):
            message = preview(bouts[cursor])
        elif key in (curses.KEY_ENTER, 10, 13):
            discarding = sum(1 for b in bouts if not b.keep)
            if discarding == 0:
                return True
            message = (f"move {discarding} bout(s) to discard/ and keep "
                       f"{len(bouts) - discarding}? y/n")
            draw(screen, bouts, cursor, offset, message)
            if screen.getch() in (ord("y"), ord("Y")):
                return True
            message = "cancelled"


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=DEFAULT_ROOT, help="where bouts were recorded")
    p.add_argument("--list", action="store_true", help="print the bouts and exit")
    p.add_argument("--empty-discard", action="store_true",
                   help="permanently delete everything already discarded")
    args = p.parse_args()

    if args.empty_discard:
        count, freed = empty_discard(args.root)
        print(f"deleted {plural(count, 'discarded bout')}, freed {human_size(freed)}")
        return 0

    bouts = scan(args.root)
    if args.list:
        print_list(bouts)
        return 0
    if not bouts:
        print(f"no unreviewed bouts in {args.root}/ — "
              f"record some with tools/autorun.py --record")
        return 0

    if not curses.wrapper(loop, bouts):
        print("quit — nothing changed")
        return 0

    kept, discarded, failures = apply_selection(bouts, args.root)
    print(f"kept {plural(kept, 'bout')}; moved {discarded} to "
          f"{os.path.join(args.root, bout_session.DISCARD_DIR)}/")
    for name, error in failures:
        print(f"  FAILED to discard {name}: {error}")
    if discarded:
        print("delete them for good with --empty-discard once you are sure")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
