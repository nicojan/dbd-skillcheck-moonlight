"""The gap rotation and the drain, which are the two things here that can lose data."""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_log import CheckLog, reactive_record

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


class FakeClock:
    """A clock the test drives, so a 60 s gap does not take 60 s to test."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def read_all(directory):
    out = {}
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            out[name] = [json.loads(line) for line in handle if line.strip()]
    return out


def test_a_quiet_minute_starts_a_new_file():
    print("\ntest_a_quiet_minute_starts_a_new_file")
    tmp = tempfile.mkdtemp()
    try:
        clock = FakeClock()
        log = CheckLog(directory=tmp, gap_seconds=60.0, clock=clock)

        log.write({"at": "20:00:00"}, "predictive")
        clock.advance(30.0)                     # inside the gap: same bout
        _, rotated_mid = log.write({"at": "20:00:30"}, "predictive")
        check("a 30 s gap does not rotate", rotated_mid is False)

        clock.advance(61.0)                     # past the gap: new bout
        _, rotated = log.write({"at": "20:01:31"}, "reactive")
        check("a 61 s gap does rotate", rotated is True)
        log.close()

        files = read_all(tmp)
        check("two files were written", len(files) == 2, f"got {list(files)}")
        counts = sorted(len(v) for v in files.values())
        check("split 2 and 1, not 3 and 0", counts == [1, 2], f"got {counts}")
        check("every record was kept", log.total == 3, f"got {log.total}")
        check("and the file count agrees", log.files == 2, f"got {log.files}")

        # The boundary itself: exactly the gap is still the same bout. Nothing depends on
        # which way this falls, but it must not be accidental.
        clock2 = FakeClock()
        log2 = CheckLog(directory=tempfile.mkdtemp(), gap_seconds=60.0, clock=clock2)
        log2.write({"at": "a"}, "predictive")
        clock2.advance(60.0)
        _, exact = log2.write({"at": "b"}, "predictive")
        check("exactly 60 s is not yet a rotation", exact is False)
        log2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_path_is_recorded_and_the_record_is_not_mutated():
    print("\ntest_the_path_is_recorded_and_the_record_is_not_mutated")
    tmp = tempfile.mkdtemp()
    try:
        log = CheckLog(directory=tmp, clock=FakeClock())
        original = {"at": "20:00:00", "verdict": "GREAT"}
        log.write(original, "predictive")
        log.close()

        written = list(read_all(tmp).values())[0]
        check("the path is on the record", written[0]["path"] == "predictive",
              f"got {written[0].get('path')}")
        check("the original fields survive", written[0]["verdict"] == "GREAT")
        # The caller hands the SAME dict to LandingLog. Stamping `path` onto it in place
        # would put a check-queue field into the permanent archive, and the archive's
        # readers do not expect one.
        check("the caller's dict is not mutated", "path" not in original,
              f"got {original}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_reactive_press_is_recorded_as_ungraded():
    print("\ntest_a_reactive_press_is_recorded_as_ungraded")
    record = reactive_record("wiggle (great)", 0.988, at="20:51:16")

    check("it carries what the classifier saw", record["desc"] == "wiggle (great)"
          and record["confidence"] == 0.988, f"got {record}")
    # These must be present and None, not absent. A reader doing `r.get("verdict")` sees
    # the same thing either way, but one doing `r["round_trip_ms"] or 0` turns a press
    # nobody watched into a 0 ms round trip, which would be the fastest link on record.
    for field in ("verdict", "error_deg", "round_trip_ms"):
        check(f"{field} is present and None", field in record and record[field] is None,
              f"got {record.get(field, '<absent>')}")
    check("and it says why there is no landing",
          record["landing"] == "not watched", f"got {record['landing']}")


def test_the_drain_moves_and_never_clobbers():
    print("\ntest_the_drain_moves_and_never_clobbers")
    tmp = tempfile.mkdtemp()
    try:
        import pull_check_stats
        checks = os.path.join(tmp, "checks")
        archive = os.path.join(checks, "archive")
        os.makedirs(archive)

        name = "checks-20260824-204402.jsonl"
        with open(os.path.join(checks, name), "w", encoding="utf-8") as h:
            h.write(json.dumps({"at": "20:44:02", "path": "predictive"}) + "\n")
        # An archived file of the same name already exists — the restart-in-the-same-
        # second case. The older one is evidence and must survive.
        with open(os.path.join(archive, name), "w", encoding="utf-8") as h:
            h.write(json.dumps({"at": "OLD", "path": "predictive"}) + "\n")

        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            pull_check_stats.main(["pull_check_stats.py"])
        finally:
            os.chdir(cwd)

        left = [f for f in os.listdir(checks) if f.endswith(".jsonl")]
        check("the queue is empty afterwards", left == [], f"got {left}")
        archived = sorted(os.listdir(archive))
        check("both files are in the archive", len(archived) == 2, f"got {archived}")
        with open(os.path.join(archive, name), encoding="utf-8") as h:
            check("the pre-existing archive file was not overwritten",
                  json.loads(h.readline())["at"] == "OLD")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_peek_leaves_the_queue_alone():
    print("\ntest_peek_leaves_the_queue_alone")
    tmp = tempfile.mkdtemp()
    try:
        import pull_check_stats
        checks = os.path.join(tmp, "checks")
        os.makedirs(checks)
        with open(os.path.join(checks, "checks-1.jsonl"), "w", encoding="utf-8") as h:
            h.write(json.dumps({"at": "1", "path": "reactive"}) + "\n")

        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            pull_check_stats.main(["pull_check_stats.py", "--peek"])
        finally:
            os.chdir(cwd)

        check("the file is still queued",
              os.path.exists(os.path.join(checks, "checks-1.jsonl")))
        check("and nothing was archived",
              not os.path.exists(os.path.join(checks, "archive")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_a_quiet_minute_starts_a_new_file()
    test_the_path_is_recorded_and_the_record_is_not_mutated()
    test_a_reactive_press_is_recorded_as_ungraded()
    test_the_drain_moves_and_never_clobbers()
    test_peek_leaves_the_queue_alone()
    print("\n" + ("all passing" if not FAILED
                  else f"{len(FAILED)} failing: " + ", ".join(FAILED)))
    raise SystemExit(1 if FAILED else 0)
