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
import subprocess
import sys
import tempfile
import time

FAILED = []

# Overridable so the suite can be pointed at a mutated copy — that is how the negative
# control below was run: strip the traps, watch these tests fail, put them back.
ZSHRC = os.environ.get("DBD_ZSHRC", os.path.expanduser("~/.zshrc"))
TAIL_START = '  cd "$repo" || return 1'
PENDING_START = "  # --- pending bouts (extracted by tools/test_dbd_shell.py) ---"
PENDING_END = "  # --- end pending bouts ---"
GAMEUP_START = "  # --- game already running (extracted by tools/test_dbd_shell.py) ---"
GAMEUP_END = "  # --- end game already running ---"

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
    if "--pending" in sys.argv:
        root = sys.argv[sys.argv.index("--root") + 1] if "--root" in sys.argv else "frames"
        # Silent unless something is waiting. That contract is what the shell block is
        # built on, so the stub honours it rather than always printing.
        if os.path.isdir(root) and os.listdir(root):
            print("2 unreviewed bouts (40 MB) left from an earlier run", flush=True)
    else:
        print("REVIEW RAN", flush=True)
'''


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def extract(start, end, path=ZSHRC):
    """Lines of `dbd` from `start` up to (not including) `end`, verbatim.

    Anchored on text rather than line numbers so an edit elsewhere in the file cannot
    silently shift the window and leave this testing the wrong lines. A missing anchor is
    fatal rather than an empty match, because a test that quietly runs nothing passes.
    """

    with open(path) as f:
        lines = f.read().splitlines()
    try:
        first = lines.index(start)
    except ValueError:
        raise SystemExit(f"{path}: anchor {start!r} is gone from the `dbd` function")
    for i in range(first + 1, len(lines)):
        if lines[i] == end:
            return "\n".join(lines[first:i])
    raise SystemExit(f"{path}: {start!r} is never closed by {end!r}")


def extract_tail(path=ZSHRC):
    """The armed-run block: from the `cd` to the function's own closing brace."""

    return extract(TAIL_START, "}", path)


def as_function(body, repo, name="block"):
    """`body` wrapped as a zsh function taking $repo — these blocks declare `local`."""

    return f'{name}() {{\n  local repo="$1"\n{body}\n}}\n{name} "{repo}"\n'


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
        f.write(as_function(extract_tail(), repo, "dbd_tail"))

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


def run_pending_block(repo):
    """Run the launch-time notice block. No pty: no signal is involved in this one."""

    script = os.path.join(repo, "pending.zsh")
    with open(script, "w") as f:
        f.write(as_function(extract(PENDING_START, PENDING_END), repo, "pending_block"))
    return subprocess.run(["zsh", "-f", script], capture_output=True, text=True,
                          timeout=30)


def test_the_launch_notice_names_what_is_waiting():
    """The after-run review is the only chance those frames get, and a run that never
    reaches it leaves them on a full volume with nothing ever mentioning them again."""

    repo = make_repo()
    try:
        os.makedirs(os.path.join(repo, "frames", "bout_00"))
        done = run_pending_block(repo)
        check("the notice appears before the stream starts",
              "2 unreviewed bouts (40 MB)" in done.stdout, repr(done.stdout))
        check("and says how to act on it",
              "tools/review_recordings.py" in done.stdout, repr(done.stdout))
    finally:
        shutil.rmtree(repo)


def test_the_launch_notice_is_silent_when_nothing_is_waiting():
    """The normal evening. A line printed every time is a line nobody reads."""

    repo = make_repo()
    try:
        os.makedirs(os.path.join(repo, "frames"))       # present, empty
        done = run_pending_block(repo)
        check("an empty frames/ prints nothing", done.stdout == "", repr(done.stdout))
        shutil.rmtree(os.path.join(repo, "frames"))
        done = run_pending_block(repo)
        check("and no frames/ at all prints nothing too",
              done.stdout == "", repr(done.stdout))
    finally:
        shutil.rmtree(repo)


def test_a_broken_check_warns_and_plays_on():
    """A notice that fails SILENTLY is worse than none — it reads as "nothing is waiting"
    forever, which is the exact failure it was added to end. And it must never block a
    launch: whatever is wrong with the review tool, the match still gets to happen."""

    repo = make_repo()
    try:
        with open(os.path.join(repo, ".venv/bin/python"), "w") as f:
            f.write("#!/bin/sh\necho 'ModuleNotFoundError: dbd' >&2\nexit 1\n")
        done = run_pending_block(repo)
        check("the failure is reported", "could not check for unreviewed bouts"
              in done.stderr, repr(done.stderr))
        check("with the error itself, not just a shrug",
              "ModuleNotFoundError" in done.stderr, repr(done.stderr))
        check("nothing is claimed on stdout", done.stdout == "", repr(done.stdout))
        check("and the launch is not blocked", done.returncode == 0, done.returncode)
    finally:
        shutil.rmtree(repo)


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


# --- the launch skips ------------------------------------------------------------------
# `dbd` used to relaunch the game and sit through the settle wait unconditionally, so
# restarting the bot after a Ctrl-C cost 34 s and a redundant Steam URL. Both skips are
# probes against the outside world, which is exactly what a stub can stand in for.

def _run_gameup(stub_ssh_exit, env=None):
    """The game-already-running block, with `ssh` stubbed to a fixed exit code."""

    block = extract(GAMEUP_START, GAMEUP_END)
    tmp = tempfile.mkdtemp()
    try:
        stub = os.path.join(tmp, "ssh")
        with open(stub, "w") as f:
            f.write(f"#!/bin/sh\nexit {stub_ssh_exit}\n")
        os.chmod(stub, 0o755)
        script = (f'PATH="{tmp}:$PATH"\n'
                  'host=compute\n'
                  f'{block}\n'
                  'fi\n'
                  'print "GAME_UP=[$game_up]"\n')
        e = dict(os.environ)
        e.update(env or {})
        out = subprocess.run(["zsh", "-c", script], capture_output=True, text=True, env=e)
        return out.stdout
    finally:
        shutil.rmtree(tmp)


def test_running_game_is_not_relaunched():
    out = _run_gameup(0)
    check("a running game is detected", "GAME_UP=[1]" in out, out.strip())
    check("and says so instead of launching",
          "already running" in out, out.strip())


def test_absent_game_falls_through_to_launch():
    out = _run_gameup(1)
    check("no game means no skip", "GAME_UP=[]" in out, out.strip())
    check("and nothing claims it is running",
          "already running" not in out, out.strip())


def test_unreachable_host_falls_through_to_launch():
    # 255 is ssh's own "could not connect". The safe direction is to attempt the launch:
    # a duplicate Steam URL is free, a silently skipped launch leaves you staring at a
    # desktop for the length of a match.
    out = _run_gameup(255)
    check("an unreachable host does not read as 'running'",
          "GAME_UP=[]" in out, out.strip())


def test_gameup_probe_does_not_match_rungameid():
    # The trap this check exists to avoid: steam.sh keeps `steam://rungameid/381210` in
    # its argv for the life of the client, so a `rungameid` probe reads "running" every
    # evening. Pin the pattern that actually goes over the wire.
    block = extract(GAMEUP_START, GAMEUP_END)
    check("the probe matches the game, not the steam URL",
          "DeadByDaylight" in block and "rungameid" not in block, block)
    check("and the probe cannot hang forever",
          "ConnectTimeout" in block, block)


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
