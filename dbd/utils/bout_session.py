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
# Operator key presses, stamped on the same clock as `manifest.jsonl`. Absent unless the
# run was started with --record-keys; a bout without one is not malformed.
KEYS_FILE = "keys.jsonl"
WIDE_BOUT = "wide_bout"
DISCARD_DIR = "discard"

# Did the OPERATOR see a skill check drawn away from the centre of the screen during this
# bout? Three states, never two.
#
# The bot cannot answer this about itself: an off-centre check is one whose pixels the
# production 224 crop never captured, so from inside the run it is indistinguishable from
# a check that did not happen. The only offline answer is `tools/scan_frames.py`, a tile
# sweep that costs ~3 HOURS per session at 2.6-3.3 fps. The operator watching the stream
# sees the displaced check plainly and can say so in a keystroke — which is why this is an
# operator field and not a derived one.
#
# UNKNOWN is the default and must stay distinguishable from NONE. A checkbox defaulting to
# "no off-centre checks" would manufacture evidence out of every bout nobody got round to
# marking, and "we have never seen one" is exactly the conclusion that would then be read
# off a pile of unset flags. The Madness gap went unmeasured for weeks on that shape of
# reasoning. Absent on every bout recorded before 2026-09-13, which reads as UNKNOWN.
OFF_CENTRE_UNKNOWN = "unknown"
OFF_CENTRE_SEEN = "seen"
OFF_CENTRE_NONE = "none"
OFF_CENTRE_STATES = (OFF_CENTRE_UNKNOWN, OFF_CENTRE_SEEN, OFF_CENTRE_NONE)


def off_centre_of(meta):
    """The bout's off-centre state, defaulting to UNKNOWN for anything unset or bogus."""

    state = (meta or {}).get("off_centre")
    return state if state in OFF_CENTRE_STATES else OFF_CENTRE_UNKNOWN


def next_off_centre(state):
    """The next state in the cycle. Pure, so the TUI's key handler can be tested."""

    order = OFF_CENTRE_STATES
    try:
        return order[(order.index(state) + 1) % len(order)]
    except ValueError:
        return OFF_CENTRE_SEEN      # cycling from a bogus value lands somewhere useful


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
        # `keys_watched` is what makes `keys: 0` readable: without it, a bout with no
        # presses and a bout where nothing was listening look identical, and "the operator
        # never hit that beat" is exactly the wrong conclusion to draw from a missing
        # grant. See KEYS_FILE.
        "keys_watched": False,
        "keys": 0,
        # See OFF_CENTRE_* above. Written at record time so the field always exists on a
        # new bout; the operator sets it in `tools/review_recordings.py` afterwards, which
        # is the only moment they still remember the match.
        "off_centre": OFF_CENTRE_UNKNOWN,
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


def mark_reviewed(directory, off_centre=None):
    meta = load(directory)
    if meta is None:
        return False
    meta["reviewed"] = True
    if off_centre is not None:
        meta["off_centre"] = (off_centre if off_centre in OFF_CENTRE_STATES
                              else OFF_CENTRE_UNKNOWN)
    save(directory, meta)
    return True


def set_off_centre(directory, state):
    """Record the operator's off-centre answer without marking the bout reviewed.

    Separate from `mark_reviewed` because a DISCARDED bout needs its answer written too —
    the flag is the reason a bout might be worth rescuing, so it has to be on disk before
    the directory moves, not after.
    """

    meta = load(directory)
    if meta is None:
        return False
    meta["off_centre"] = state if state in OFF_CENTRE_STATES else OFF_CENTRE_UNKNOWN
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
