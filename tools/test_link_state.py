"""The stored link level, and the narrowness of the seed it feeds.

Both halves exist because of the 2026-08-31 session: the first two fires aimed with the
60 ms constant against a 36 ms link and landed 6.0 and 7.5 deg early, both MISS, while the
38 fires after the level tracker engaged went 22 GREAT / 16 good / 0 MISS.

The seed is deliberately narrow, and that is the part worth pinning. `lead_level_ms` reads
its base twice — once as the pre-median fallback, once as the value its deadband measures
every later median against — so a seed that moves both pins the lead for the whole session.
Re-scored across every recorded landing that costs 36 Greats to save the same 2 misses.
`cold_start_base` must therefore hand the constant back the moment the tracker has samples.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autorun
from dbd.utils import link_state

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def tmp_path():
    return os.path.join(tempfile.mkdtemp(), "state", "link_level.json")


def test_a_level_survives_the_session_boundary():
    p = tmp_path()
    check("save reports success", link_state.save(36.0, p) is True)
    check("and reads back", link_state.load(p) == 36.0, str(link_state.load(p)))


def test_a_stale_level_says_nothing():
    p = tmp_path()
    link_state.save(36.0, p)
    late = time.time() + link_state.MAX_AGE_S + 1
    check("past MAX_AGE it is not offered", link_state.load(p, now=late) is None)
    check("just inside MAX_AGE it still is",
          link_state.load(p, now=time.time() + link_state.MAX_AGE_S - 60) == 36.0)


def test_an_impossible_level_is_refused():
    p = tmp_path()
    for bad in (0.0, -5.0, 500.0, None):
        check(f"refuses {bad}", link_state.save(bad, p) is False)
    check("and nothing was written", link_state.load(p) is None)


def test_a_corrupt_or_missing_file_reads_as_no_information():
    p = tmp_path()
    check("missing file", link_state.load(p) is None)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    for junk in ("", "{", '{"level_ms": "fast", "at": 1}', '{"at": 1}'):
        with open(p, "w") as f:
            f.write(junk)
        check(f"corrupt {junk[:14]!r}", link_state.load(p) is None)


def test_the_seed_reaches_only_the_cold_start():
    n = autorun.LEVEL_MIN_SAMPLES
    check("it applies with no trips yet",
          autorun.cold_start_base(60.0, (), 36.0) == 36.0)
    check("it still applies one short of a median",
          autorun.cold_start_base(60.0, tuple(range(n - 1)), 36.0) == 36.0)
    check("and is gone the moment the tracker has one",
          autorun.cold_start_base(60.0, tuple(range(n)), 36.0) == 60.0)
    check("and long after",
          autorun.cold_start_base(60.0, tuple(range(n + 20)), 36.0) == 60.0)


def test_no_seed_changes_nothing():
    check("None leaves the constant alone",
          autorun.cold_start_base(60.0, (), None) == 60.0)
    check("even mid-session", autorun.cold_start_base(60.0, (1, 2, 3, 4), None) == 60.0)


def test_a_fabricated_args_cannot_write_the_real_state_file():
    """The regression: `test_landing_report` drives autorun's shutdown block with stub
    landings, and an unguarded save stamped a 60.0 ms level into the repo's real state.
    The next real match would have cold-started from it."""

    import subprocess
    real = link_state.DEFAULT_PATH
    before = os.path.exists(real)
    here = os.path.dirname(os.path.abspath(__file__))
    subprocess.run([sys.executable, os.path.join(here, "test_landing_report.py")],
                   cwd=os.path.dirname(here), capture_output=True)
    check("a stub-args run leaves the real state file alone",
          os.path.exists(real) == before, f"exists={os.path.exists(real)} was={before}")


def main():
    print("link state / cold start")
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
