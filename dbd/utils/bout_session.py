"""The on-disk shape of a recorded bout, and the one marker that keeps it honest.

WHY THIS IS A FILE AND NOT A DICT LITERAL IN THE RECORDER.

`record_frames.py` writes whole content rects. `ClipRecorder` writes 672 px wide boxes —
the frames the armed loop actually decided on, already cropped. Both are directories of
`%06d.jpg` plus a `manifest.jsonl`, and from the outside they are indistinguishable.

That matters because the two readers in this repo both infer geometry from the image:

  * `replay_centre_crop.session_geometry` takes the first frame's shape AS the content
    rect and derives the wide box from it.
  * `scan_frames` sizes its tile as `224 * height / 1080`.

Hand either one a 672 box and it computes a plausible, wrong answer and says nothing —
a 672-tall "content rect" yields a 139 px tile and a wide box that is not where any
check is. Silent wrong answers are this repo's most expensive failure mode, so a bout
declares itself: `kind: "wide_bout"`, plus the content rect and `WideGeometry` it was
actually cropped with. Readers branch on the marker rather than guessing from a shape.

A directory with no `bout.json` is a `record_frames.py` session and reads exactly as it
always did.
"""

import json
import os

BOUT_FILE = "bout.json"
MANIFEST_FILE = "manifest.jsonl"
WIDE_BOUT = "wide_bout"
DISCARD_DIR = "discard"


def bout_path(directory):
    return os.path.join(directory, BOUT_FILE)


def load(directory):
    """The bout metadata for a directory, or None if it is not a bout.

    None is the answer for a `record_frames.py` session and for anything unreadable —
    the caller's fallback is the old infer-from-the-image path, which is correct there.
    """

    path = bout_path(directory)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return meta if meta.get("kind") == WIDE_BOUT else None


def save(directory, meta):
    """Write metadata atomically, so a crash mid-write cannot leave an unreadable bout.

    The recorder rewrites this file on every check, and a bout that loses its marker
    reads back as a full-frame session — the exact silent misread this module exists to
    prevent. A temp file plus `os.replace` makes that impossible.
    """

    os.makedirs(directory, exist_ok=True)
    tmp = bout_path(directory) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    os.replace(tmp, bout_path(directory))


def new_meta(content, geometry, started, gap_seconds, quality):
    return {
        "kind": WIDE_BOUT,
        "started": started,
        "content": dict(content),
        "geometry": dict(geometry),
        "gap_seconds": gap_seconds,
        "quality": quality,
        "reviewed": False,
        # Set while the recorder is still filling this directory, cleared by `_close_bout`.
        # `find_bouts` hides an active bout so the review tool cannot `shutil.move` a
        # directory out from under the writer threads. `writer_pid` is what makes the flag
        # safe to trust — see `writer_alive`.
        "active": True,
        "writer_pid": os.getpid(),
        "frames": 0,
        "checks": [],
    }


def writer_alive(meta):
    """Whether the process that opened this bout is still running.

    The `active` flag alone cannot be trusted: a `kill -9`, a panic, or a pulled power
    cord leaves it set with nobody writing, and a bout that is hidden forever is exactly
    the silent loss the pending notice exists to end — four bouts and 993 MB went
    unnoticed on 2026-08-30 that way. So a bout is only live if its writer answers.

    Signal 0 checks for existence without delivering anything. A recycled pid can keep one
    orphan hidden until the next run, which is the safe direction to be wrong in: the
    alternative is moving a directory a live recorder holds open.
    """

    if not meta.get("active"):
        return False
    pid = meta.get("writer_pid")
    if not isinstance(pid, int) or pid <= 0:
        return False        # active with no usable pid — treat as orphaned, not as live
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True         # alive, owned by someone else
    except OSError:
        return False
    return True


def mark_closed(meta):
    """Clear the live marker. Returns `meta` so a caller can save in one step."""

    meta["active"] = False
    return meta


def mark_reviewed(directory):
    meta = load(directory)
    if meta is None:
        return False
    meta["reviewed"] = True
    save(directory, meta)
    return True


def find_bouts(root, include_reviewed=False, include_active=False):
    """Every bout under `root`, oldest first.

    Skips `discard/`, reviewed bouts, and bouts a live recorder is still writing. That
    last one is what makes the review tool safe to run while a match is in progress.
    """

    if not os.path.isdir(root):
        return []
    found = []
    for name in sorted(os.listdir(root)):
        if name == DISCARD_DIR:
            continue
        directory = os.path.join(root, name)
        if not os.path.isdir(directory):
            continue
        meta = load(directory)
        if meta is None:
            continue
        if meta.get("reviewed") and not include_reviewed:
            continue
        if writer_alive(meta) and not include_active:
            continue
        found.append((directory, meta))
    return found


def disk_bytes(directory):
    total = 0
    for entry in os.scandir(directory):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total
