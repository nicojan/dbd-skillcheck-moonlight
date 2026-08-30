"""The part of the review tool that moves data. The curses rendering is not tested here.

Everything that can lose a bout lives in `scan` / `apply_selection` / `empty_discard`, so
those run headless against temp directories. The defaults matter as much as the moves: a
bout that arrives pre-checked would be kept by a careless ENTER, which is the one outcome
this tool exists to prevent.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dbd.utils import bout_session
from review_recordings import apply_selection, empty_discard, scan

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def make_bout(root, name, checks=2, reviewed=False, kind=bout_session.WIDE_BOUT):
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
