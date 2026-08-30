"""The `dbd` shell function's shutdown path, driven by a real Ctrl-C in a real pty.

This is the one load-bearing piece of the setup that lived outside the test suite, and it
is where the 2026-08-29 match was lost: the run ended with four bouts on disk, no review
prompt, and a log that stops mid-sentence. Ctrl-C was costing both halves of the shutdown
at once. An untrapped SIGINT aborts the whole zsh function, so the review TUI after the
armed run never ran; and `tee` sits in the same foreground process group, so it took the
same signal and died FIRST, leaving autorun's shutdown block — the landing tally and the
recorder's frame and drop counts — writing into a broken pipe. Neither failure is visible
from inside Python, and neither can be reproduced by reading the function.

So this extracts the tail of the real `dbd` out of ~/.zshrc and runs THAT, rather than a
copy that can drift away from what actually runs at 23:00. Stubs stand in for autorun and
the review tool, because what is under test is the shell's signal handling, not theirs.
"""

import os
import re
import select
import shutil
import sys
import tempfile
import time

FAILED = []

# Overridable so the suite can be pointed at a mutated copy — that is how the negative
# control below was run: strip the traps, watch these tests fail, put them back.
ZSHRC = os.environ.get("DBD_ZSHRC", os.path.expanduser("~/.zshrc"))
TAIL_START = '  cd "$repo" || return 1'

STUB_PYTHON = '''#!/usr/bin/env python3
"""Stands in for both .venv/bin/python entry points, told apart by the script argument."""

import os
import sys
import time

script = sys.argv[1] if len(sys.argv) > 1 else ""
if "autorun" in script:
    print("armed, waiting", flush=True)
    try:
        time.sleep(0.2 if os.environ.get("STUB_EXIT_FAST") else 30)
    except KeyboardInterrupt:
        print("stopping", flush=True)
    finally:
        # Stands for the whole `finally` block in autorun.run(): the landing tally, the
        # recorder's frame and drop counts. All of it is written AFTER the signal lands.
        print("SUMMARY: 0 dropped", flush=True)
elif "review_recordings" in script:
    print("REVIEW RAN", flush=True)
'''


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def extract_tail(path=ZSHRC):
    """The armed-run block of `dbd`, verbatim: from the `cd` to the function's close.

    Anchored on text rather than line numbers so an edit above it does not silently
    shift the window and leave this testing the wrong lines.
    """

    with open(path) as f:
        lines = f.read().splitlines()
    try:
        start = lines.index(TAIL_START)
    except ValueError:
        raise SystemExit(f"{path}: no `dbd` tail found — anchor {TAIL_START!r} is gone")
    for i in range(start, len(lines)):
        if lines[i] == "}":
            return "\n".join(lines[start:i])
    raise SystemExit(f"{path}: `dbd` tail never closes")


def make_repo():
    """A throwaway repo whose .venv/bin/python is the stub, laid out as `dbd` expects."""

    repo = tempfile.mkdtemp(prefix="dbd-shell-test-")
    os.makedirs(os.path.join(repo, ".venv/bin"))
    os.makedirs(os.path.join(repo, "tools"))
    stub = os.path.join(repo, ".venv/bin/python")
    with open(stub, "w") as f:
        f.write(STUB_PYTHON)
    os.chmod(stub, 0o755)
    for name in ("autorun.py", "review_recordings.py"):
        open(os.path.join(repo, "tools", name), "w").close()
    return repo


def run_dbd_tail(repo, interrupt=True, env=None, timeout=15.0):
    """Run the real tail in a pty, optionally Ctrl-C it, return everything it printed.

    A pty and not a pipe: SIGINT from Ctrl-C is delivered to the terminal's foreground
    process GROUP, and that grouping is the whole subject here. Sent to the process alone
    it would never reach `tee`, and the bug would not reproduce.
    """

    import pty

    script = os.path.join(repo, "run.zsh")
    with open(script, "w") as f:
        f.write("dbd_tail() {\n  local repo=\"$1\"\n" + extract_tail() + "\n}\n"
                f'dbd_tail "{repo}"\n')

    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(env or {})
        os.environ["TERM"] = "dumb"
        os.execvp("zsh", ["zsh", "-f", script])

    out, sent, deadline = b"", not interrupt, time.time() + timeout
    while time.time() < deadline:
        if select.select([fd], [], [], 0.2)[0]:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            out += data
        if not sent and b"armed, waiting" in out:
            time.sleep(0.4)          # let the stub reach its sleep before signalling
            os.write(fd, b"\x03")    # the literal Ctrl-C, delivered by the tty driver
            sent = True
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    return out.decode(errors="replace")


def armed_log(repo):
    """(filename, contents) of the log the run wrote, or (None, '') if it wrote none."""

    names = [n for n in os.listdir(repo) if n.startswith("armed-") and n.endswith(".log")]
    if len(names) != 1:
        return None, ""
    with open(os.path.join(repo, names[0])) as f:
        return names[0], f.read()


def test_ctrl_c_reaches_the_review_step():
    """The bug: an untrapped SIGINT aborted the function and the TUI never opened."""

    repo = make_repo()
    try:
        out = run_dbd_tail(repo)
        check("Ctrl-C still opens the review TUI", "REVIEW RAN" in out, repr(out[-200:]))
    finally:
        shutil.rmtree(repo)


def test_ctrl_c_keeps_the_shutdown_summary():
    """The other half: `tee` died on the same signal and the summary hit a broken pipe."""

    repo = make_repo()
    try:
        out = run_dbd_tail(repo)
        name, log = armed_log(repo)
        check("shutdown summary survives in the log",
              "stopping" in log and "SUMMARY: 0 dropped" in log, f"{name}: {log!r}")
        check("shutdown summary survives on the terminal",
              "SUMMARY: 0 dropped" in out, repr(out[-200:]))
    finally:
        shutil.rmtree(repo)


def test_log_name_carries_the_date():
    """armed-2338.log existed twice, from two different days. The second wins."""

    repo = make_repo()
    try:
        run_dbd_tail(repo, interrupt=False, env={"STUB_EXIT_FAST": "1"})
        name, _ = armed_log(repo)
        check("log name is dated, not just HHMM",
              bool(name) and bool(re.fullmatch(r"armed-\d{8}-\d{4}\.log", name)), str(name))
    finally:
        shutil.rmtree(repo)


def test_clean_exit_also_opens_the_review():
    """Ctrl-C is the usual exit, not the only one."""

    repo = make_repo()
    try:
        out = run_dbd_tail(repo, interrupt=False, env={"STUB_EXIT_FAST": "1"})
        check("a clean exit opens the review TUI too",
              "REVIEW RAN" in out, repr(out[-200:]))
    finally:
        shutil.rmtree(repo)


def test_no_review_env_skips_the_tui():
    repo = make_repo()
    try:
        out = run_dbd_tail(repo, env={"DBD_NO_REVIEW": "1"})
        _, log = armed_log(repo)
        check("DBD_NO_REVIEW=1 skips the TUI", "REVIEW RAN" not in out, repr(out[-200:]))
        check("...and the log is still complete", "SUMMARY: 0 dropped" in log, repr(log))
    finally:
        shutil.rmtree(repo)


def main():
    print("dbd shell function")
    if not shutil.which("zsh"):
        print("  SKIP  no zsh on PATH")
        return 0
    if not os.path.exists(ZSHRC):
        print(f"  SKIP  no {ZSHRC}")
        return 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if FAILED:
        print(f"\n{len(FAILED)} FAILED: {', '.join(FAILED)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
