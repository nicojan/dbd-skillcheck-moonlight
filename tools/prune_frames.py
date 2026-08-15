"""Delete recorded frames that are far from any detected skill check.

A full-frame session is ~350 KB/frame at 30 fps, so an evening of play is tens of GB, and
most of it is lobbies, menus and running around. Only the frames near a skill check carry
information worth keeping.

The important constraint: **prune on full-grid evidence, never on the centre crop alone.**
An off-centre check (the Doctor's Madness effect places checks uniformly at random) does
not fire the centre crop at all, so pruning by centre detection would silently delete
exactly the frames that prove off-centre checks exist. This tool therefore consumes the
log of a `scan_frames.py` run, which tiles the whole frame.

    .venv/bin/python tools/prune_frames.py --scan-log scan.out            # dry run
    .venv/bin/python tools/prune_frames.py --scan-log scan.out --apply    # actually delete

Dry run by default. Deletion is irreversible and the frames cannot be regenerated without
replaying the games, so the default has to be the safe one.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict

SESSION_HEADER = re.compile(r"=+\s+(\S*frames/session_\S+?)\s*\(")
DETECTION_LINE = re.compile(r"(\d{6}\.jpg)\s+tile=\(")
BYTES_PER_GB = 1024 ** 3


def parse_args():
    p = argparse.ArgumentParser(description="Prune frames far from any detected skill check")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--scan-log",
                     help="output of scan_frames.py. Only safe if the log is COMPLETE — a log "
                          "truncated by `tail` silently under-reports detections and would "
                          "delete frames that should be kept")
    src.add_argument("--sessions", nargs="+",
                     help="session dirs; detections are read from each <session>/hits/*.png, "
                          "which scan_frames.py writes one per detected frame. More reliable "
                          "than a log, since nothing can truncate it")
    src.add_argument("--centre-detections", nargs="+", metavar="SESSION",
                     help="session dirs pruned against <session>/centre_detections.json, the "
                          "cache sweep_rates.py writes. ONLY safe once the session is known to "
                          "contain no off-centre (Doctor/Madness) checks: the centre crop "
                          "cannot see those, so pruning on it would delete exactly the frames "
                          "worth keeping. When in doubt run scan_frames.py and use --sessions")
    p.add_argument("--every", type=int, default=1,
                   help="the --every used for the scan, needed to map hit indices back to "
                        "frame numbers (hits are indexed into the SAMPLED list)")
    p.add_argument("--window", type=int, default=30,
                   help="frames to keep either side of a detection")
    p.add_argument("--apply", action="store_true",
                   help="actually delete; without this it only reports")
    return p.parse_args()


def log(message):
    print(message, flush=True)


def parse_scan_log(path):
    """{session dir: set of frame filenames that fired}, in log order."""

    detections = defaultdict(set)
    current = None
    with open(path) as f:
        for line in f:
            header = SESSION_HEADER.search(line)
            if header:
                current = header.group(1)
                detections.setdefault(current, set())
                continue
            hit = DETECTION_LINE.search(line)
            if hit and current:
                detections[current].add(hit.group(1))
    return detections


def frames_in(session):
    return sorted(n for n in os.listdir(session) if n.endswith(".jpg"))


def detections_from_hits(session, every):
    """Frame filenames that fired, recovered from <session>/hits/hit_NNNNNN.png.

    scan_frames.py names each annotated frame by its index into the SAMPLED frame list,
    so with --every 2 the hit index must be doubled to get back to the real frame.
    """

    hits_dir = os.path.join(session, "hits")
    if not os.path.isdir(hits_dir):
        return set()

    names = frames_in(session)
    sampled = names[::max(every, 1)]
    detected = set()
    for entry in os.listdir(hits_dir):
        match = re.fullmatch(r"hit_(\d+)\.png", entry)
        if not match:
            continue
        index = int(match.group(1))
        if index < len(sampled):
            detected.add(sampled[index])
    return detected


def detections_from_centre(session):
    """Frame filenames that fired, from <session>/centre_detections.json.

    That cache stores real frame names rather than sampled indices, so unlike the hits/
    route there is no --every mapping to get wrong.
    """

    path = os.path.join(session, "centre_detections.json")
    if not os.path.exists(path):
        return set()

    with open(path) as f:
        cache = json.load(f)
    return {rec["frame"] for rec in cache.get("detections", [])}


def plan(session, detected, window):
    """(keep, drop) filename lists for one session."""

    names = frames_in(session)
    index = {name: i for i, name in enumerate(names)}
    keep_idx = set()
    for name in detected:
        i = index.get(name)
        if i is None:
            continue
        keep_idx.update(range(max(i - window, 0), min(i + window + 1, len(names))))

    keep = [names[i] for i in sorted(keep_idx)]
    drop = [n for i, n in enumerate(names) if i not in keep_idx]
    return keep, drop


def size_of(session, names):
    total = 0
    for n in names:
        try:
            total += os.path.getsize(os.path.join(session, n))
        except OSError:
            pass
    return total


def main():
    args = parse_args()
    if args.scan_log:
        detections = parse_scan_log(args.scan_log)
        if not detections:
            sys.exit(f"no session sections found in {args.scan_log}")
    elif args.centre_detections:
        detections = {s.rstrip("/"): detections_from_centre(s.rstrip("/"))
                      for s in args.centre_detections}
        log("pruning against CENTRE-CROP detections — valid only for sessions with no "
            "off-centre (Doctor/Madness) checks")
    else:
        detections = {s.rstrip("/"): detections_from_hits(s.rstrip("/"), args.every)
                      for s in args.sessions}

    grand_keep = grand_drop = 0
    plans = []

    for session in sorted(detections):
        if not os.path.isdir(session):
            log(f"SKIP {session}: not a directory")
            continue
        detected = detections[session]
        keep, drop = plan(session, detected, args.window)

        # A session with no detections at all would prune to nothing. That is much more
        # likely to mean the scan log was misparsed than that the session is worthless.
        if not detected:
            log(f"SKIP {session}: scan log lists no detections — refusing to delete everything")
            continue

        keep_b, drop_b = size_of(session, keep), size_of(session, drop)
        grand_keep += keep_b
        grand_drop += drop_b
        plans.append((session, keep, drop))
        log(f"{session}")
        log(f"  {len(detected)} frames fired, {len(keep)} kept (+/-{args.window}), "
            f"{len(drop)} to delete")
        log(f"  keep {keep_b / BYTES_PER_GB:.2f} GB, free {drop_b / BYTES_PER_GB:.2f} GB")

    log("")
    log(f"TOTAL: keep {grand_keep / BYTES_PER_GB:.2f} GB, "
        f"free {grand_drop / BYTES_PER_GB:.2f} GB")

    if not args.apply:
        log("\nDRY RUN — nothing deleted. Re-run with --apply to delete.")
        return

    deleted = 0
    for session, _keep, drop in plans:
        for name in drop:
            try:
                os.remove(os.path.join(session, name))
                deleted += 1
            except OSError as err:
                log(f"  failed to delete {name}: {err}")
    log(f"\ndeleted {deleted} frames, freed {grand_drop / BYTES_PER_GB:.2f} GB")
    log("manifest.jsonl and session.json are left intact; they still describe every "
        "frame originally captured, including the deleted ones")


if __name__ == "__main__":
    main()
