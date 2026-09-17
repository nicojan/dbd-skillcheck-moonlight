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
LOAD_START = "  # --- load guard (extracted by tools/test_dbd_shell.py) ---"
LOAD_END = "  # --- end load guard ---"
GAMEMODE_START = "  # --- game mode (extracted by tools/test_dbd_shell.py) ---"
GAMEMODE_END = "  # --- end game mode ---"

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



# --- the load guard --------------------------------------------------------------------
# A contended scheduler inflates round_trip_ms indistinguishably from the link. On
# 2026-09-02 two concurrent Claude sessions under ~/dev/human took the 1-min load average
# to 43.8 on 14 cores; the recorder and V-Sync were both suspected and cleared before
# anyone ran `uptime`. The guard is a NOTICE — the one property that must never regress is
# that it plays on regardless, so every case below asserts a zero exit as well as its text.
#
# It is a notice in the strong sense, and that is pinned here: it must NOT declare a match
# unscorable. It reads before game mode quits eleven apps and before the stream is up, so
# it both counts apps that are about to close and misses everything the armed match costs.
# The verdict belongs to `LoadTally`, which samples under the real workload; this guard's
# job is to name another session's build before a stream has been committed to.

def _run_load(loadavg, ncpu="14", env=None):
    """The load-guard block with `sysctl` stubbed. `loadavg` is its raw vm.loadavg text."""

    block = extract(LOAD_START, LOAD_END)
    tmp = tempfile.mkdtemp()
    try:
        stub = os.path.join(tmp, "sysctl")
        with open(stub, "w") as f:
            # -n vm.loadavg and -n hw.ncpu are the two reads; anything else is not ours.
            f.write('#!/bin/sh\n'
                    'case "$2" in\n'
                    f'  vm.loadavg) {"printf" if loadavg is not None else "false"}'
                    + (f' \'%s\\n\' \'{loadavg}\'' if loadavg is not None else "") + ' ;;\n'
                    f'  hw.ncpu) {"printf" if ncpu is not None else "false"}'
                    + (f' \'%s\\n\' \'{ncpu}\'' if ncpu is not None else "") + ' ;;\n'
                    '  *) exit 1 ;;\n'
                    'esac\n')
        os.chmod(stub, 0o755)
        script = f'PATH="{tmp}:$PATH"\ng() {{\n{block}\n}}\ng\n'
        e = dict(os.environ)
        e.pop("DBD_LOAD_GATE", None)
        e.update(env or {})
        out = subprocess.run(["zsh", "-f", "-c", script],
                             capture_output=True, text=True, env=e)
        return out.returncode, out.stdout + out.stderr
    finally:
        shutil.rmtree(tmp)


def test_high_load_warns_and_does_not_block():
    code, out = _run_load("{ 43.80 12.10 8.00 }")
    check("a saturated machine is named", "43.80" in out and "14 cores" in out, out.strip())
    check("and says the reading is the load, not the link", "LOAD, not the" in out, out.strip())
    check("and defers the verdict to the run's own load line",
          "load: line at shutdown" in out, out.strip())
    check("and says the background apps are still open",
          "before game mode quits" in out, out.strip())
    check("and does NOT pass a verdict of its own",
          "Do not score" not in out and "DO NOT SCORE" not in out, out.strip())
    check("and STILL plays on", code == 0, f"exit {code}")


def test_quiet_machine_says_nothing():
    # The pending-bouts notice set the precedent: silent when there is nothing to say.
    # A guard that prints every evening is a guard that stops being read.
    code, out = _run_load("{ 1.42 1.90 2.10 }")
    check("a quiet machine is silent", out.strip() == "", out.strip())
    check("and exits clean", code == 0, f"exit {code}")


def test_the_gate_is_overridable():
    code, out = _run_load("{ 8.00 8.00 8.00 }", env={"DBD_LOAD_GATE": "20"})
    check("a raised gate silences a load below it", out.strip() == "", out.strip())
    code, out = _run_load("{ 3.00 3.00 3.00 }", env={"DBD_LOAD_GATE": "2"})
    check("a lowered gate catches a load above it", "3.00" in out, out.strip())


def test_an_unreadable_load_plays_on():
    # Fail OPEN. A guard that blocks a launch over a parse slip has cost a match to
    # prevent a footnote, and `sysctl` missing is exactly the shape that slip takes.
    code, out = _run_load(None)
    check("an unreadable load average warns", "could not read the load" in out, out.strip())
    check("and does not block the launch", code == 0, f"exit {code}")


def test_a_malformed_load_does_not_crash_the_shell():
    # zsh arithmetic on a non-numeric string is a fatal error inside (( )), which would
    # abort the whole `dbd` function — the 2026-08-29 failure mode, from a new direction.
    for junk in ("{ n/a n/a n/a }", "{ }", "garbage"):
        code, out = _run_load(junk)
        check(f"junk load {junk!r} still plays on", code == 0, f"exit {code}: {out.strip()}")
        check(f"junk load {junk!r} says so", "could not read the load" in out, out.strip())


def test_the_guard_never_returns():
    # The property under test is structural, not behavioural: no exit path at all. A
    # `return` added here later would block a launch on a busy evening.
    block = extract(LOAD_START, LOAD_END)
    check("the load guard has no return/exit", 
          not re.search(r"^\s*(return|exit)\b", block, re.M), block)


# --- the game-mode block -------------------------------------------------------------
#
# `bin/game-mode.sh on` quits eleven background apps and records which were running, so
# `Done Gaming` can put back exactly those. It used to be a separate Raycast command with
# the same name as this function, which is the kind of collision that reads as "already
# done" six weeks later. Folded in here it has the load guard's contract: it warns, it
# never blocks, and a run that got this far must still arm even if the quit fails.

def _run_game_mode(args=(), script=None, env=None):
    """The block with `bin/game-mode.sh` stubbed. `script` None means the file is absent."""

    block = extract(GAMEMODE_START, GAMEMODE_END)
    repo = tempfile.mkdtemp(prefix="dbd-gamemode-")
    try:
        if script is not None:
            os.makedirs(os.path.join(repo, "bin"))
            path = os.path.join(repo, "bin", "game-mode.sh")
            with open(path, "w") as f:
                f.write(script)
            os.chmod(path, 0o755)
        # The block reads "$*", so the wrapper has to pass the function's own args
        # through — `as_function` would hand it the repo path as $1 and the --dry-run
        # case would then never be exercised.
        quoted = " ".join(f'"{a}"' for a in args)
        body = f'g() {{\n  local repo="$1"\n  shift\n{block}\n}}\ng "{repo}" {quoted}\n'
        e = dict(os.environ)
        e.pop("DBD_NO_GAME_MODE", None)
        e.update(env or {})
        out = subprocess.run(["zsh", "-f", "-c", body], capture_output=True, text=True, env=e)
        return out.returncode, out.stdout + out.stderr
    finally:
        shutil.rmtree(repo)


QUIT_STUB = '#!/bin/sh\necho "Game mode: closed 11 — Bartender Rocket ($1)"\n'


def test_game_mode_quits_the_apps_before_the_armed_run():
    code, out = _run_game_mode(script=QUIT_STUB)
    check("the quit runs", "closed 11" in out, out.strip())
    check("and is asked for `on`, not `off`", "(on)" in out, out.strip())
    check("and the function carries on", code == 0, f"exit {code}")


def test_a_dry_run_closes_nothing():
    """`dbd --dry-run` presses nothing, so it has no business quitting eleven apps."""

    code, out = _run_game_mode(args=("--dry-run",), script=QUIT_STUB)
    check("a dry run leaves the apps open", "closed 11" not in out, out.strip())
    check("and says nothing about it", out.strip() == "", out.strip())
    code, out = _run_game_mode(args=("--record-keys", "--dry-run"), script=QUIT_STUB)
    check("...even when --dry-run is not the first argument",
          "closed 11" not in out, out.strip())
    code, out = _run_game_mode(args=("--dry-runner",), script=QUIT_STUB)
    check("but a flag that merely starts the same way still closes them",
          "closed 11" in out, out.strip())


def test_the_off_switch_is_honoured():
    code, out = _run_game_mode(script=QUIT_STUB, env={"DBD_NO_GAME_MODE": "1"})
    check("DBD_NO_GAME_MODE=1 skips the quit", "closed 11" not in out, out.strip())
    check("and still arms", code == 0, f"exit {code}")


def test_a_failed_quit_never_costs_the_match():
    code, out = _run_game_mode(script='#!/bin/sh\nexit 3\n')
    check("a failing game-mode.sh is named", "playing anyway" in out, out.strip())
    check("and does not stop the run", code == 0, f"exit {code}")

    code, out = _run_game_mode(script=None)
    check("a missing game-mode.sh is named", "no bin/game-mode.sh" in out, out.strip())
    check("and does not stop the run either", code == 0, f"exit {code}")


def test_the_game_mode_block_cannot_abort_the_function():
    """The load guard's rule, and for the same reason: a busy machine is a fine evening to
    PLAY and only a bad one to SCORE. Closing apps is a convenience, not a precondition."""

    block = extract(GAMEMODE_START, GAMEMODE_END)
    check("the game-mode block has no return/exit",
          not re.search(r"^\s*(return|exit)\b", block, re.M), block)


# --- the path TO the game-mode block ---------------------------------------------------
#
# Everything above tests the game-mode block in ISOLATION: extracted, wrapped, run against
# a stub repo. That says the block works and says nothing at all about whether `dbd` ever
# gets there — and "did it get there" is the only question the operator actually asked, on
# 2026-09-16, when eleven apps were still open after a launch. Between the top of the
# function and the block sit seven `return 1` gates (no venv, no moonlight binary, an
# unreachable host, a failed `moonlight list`, an app the host does not offer, a Moonlight
# that exited on launch, a Moonlight that died during the settle) and one if/elif whose
# closing `fi` decides whether the block is inside the launch branch or after it. Any of
# them can skip the quit, and the only symptom is eleven apps that stayed open — no error,
# no line in armed-*.log, nothing to read back. The block's own output cannot be recovered
# from the log either: the tee wraps only the armed run, and this runs before it.
#
# So this runs the function head for real, from `dbd() {` through the end of the block,
# with the outside world stubbed, and asks whether game-mode.sh was invoked. HOME is
# redirected rather than the repo path parameterised, because `local repo=` is spelled
# "$HOME/dev/dbd_autoSkillCheck" in the function and a test that rewrites that line is
# testing its own rewrite.

HEAD_START = "dbd() {"

# `pgrep -f "Moonlight stream ..."` matching is what sets already_streaming, which skips
# the 4 s probe and the 30 s settle. That is not a shortcut around the code under test:
# the resumed-stream path is the one an operator takes all evening, and taking it keeps
# this test at well under a second instead of 34.
PATH_STUBS = {
    "pgrep": '#!/bin/sh\nexit 0\n',
    "open": '#!/bin/sh\nexit 0\n',
}


def _ssh_stub(reachable=True, game_up=False):
    """One stub for all three ssh calls in the head, told apart by the remote command."""

    return (
        '#!/bin/sh\n'
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        f'    *pgrep*DeadByDaylight*) exit {0 if game_up else 1} ;;\n'
        '    *rungameid*) echo "LAUNCHED" >&2; exit 0 ;;\n'
        '  esac\n'
        'done\n'
        f'exit {0 if reachable else 255}\n'
    )


def _run_to_game_mode(reachable=True, list_ok=True, offers_app=True, game_up=False,
                      args=(), env=None):
    """Run `dbd` from its first line to the end of the game-mode block.

    Returns (exit code, output, quit_ran) where quit_ran is whether the stubbed
    bin/game-mode.sh was actually invoked — the thing nothing else here checks.
    """

    block = extract(HEAD_START, GAMEMODE_END)
    home = tempfile.mkdtemp(prefix="dbd-reach-home-")
    try:
        repo = os.path.join(home, "dev", "dbd_autoSkillCheck")
        os.makedirs(os.path.join(repo, ".venv/bin"))
        os.makedirs(os.path.join(repo, "tools"))
        os.makedirs(os.path.join(repo, "bin"))
        stub = os.path.join(repo, ".venv/bin/python")
        with open(stub, "w") as f:
            f.write(STUB_PYTHON)
        os.chmod(stub, 0o755)
        for name in ("autorun.py", "review_recordings.py"):
            open(os.path.join(repo, "tools", name), "w").close()

        # The marker is a file and not a line of output because the question is whether
        # the script RAN. Output can be swallowed by a redirect; the file cannot.
        marker = os.path.join(home, "game-mode-ran")
        quit_stub = os.path.join(repo, "bin", "game-mode.sh")
        with open(quit_stub, "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$1" > "{marker}"\n'
                    'echo "Game mode: closed 11 — Bartender Rocket"\n')
        os.chmod(quit_stub, 0o755)

        shims = os.path.join(home, "shims")
        os.makedirs(shims)
        stubs = dict(PATH_STUBS)
        stubs["ssh"] = _ssh_stub(reachable=reachable, game_up=game_up)
        stubs["moonlight"] = (
            '#!/bin/sh\n'
            f'[ "$1" = list ] || exit 0\n'
            f'{"exit 1" if not list_ok else ""}\n'
            + (f'printf "%s\\n" "Steam Big Picture"\n' if offers_app
               else 'printf "%s\\n" "Desktop"\n')
        )
        for name, text in stubs.items():
            path = os.path.join(shims, name)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o755)

        quoted = " ".join(f'"{a}"' for a in args)
        script = (f'PATH="{shims}:$PATH"\n{block}\n}}\ndbd {quoted}\n')
        e = dict(os.environ)
        e.pop("DBD_NO_GAME_MODE", None)
        e.pop("DBD_NO_GAME", None)
        e["HOME"] = home
        e["DBD_MOONLIGHT"] = os.path.join(shims, "moonlight")
        e["DBD_WAIT"] = "0"
        e.update(env or {})
        out = subprocess.run(["zsh", "-f", "-c", script], capture_output=True,
                             text=True, env=e, timeout=60)
        return out.returncode, out.stdout + out.stderr, os.path.exists(marker)
    finally:
        shutil.rmtree(home)


def test_a_normal_launch_reaches_the_game_mode_block():
    """The 2026-09-16 report, as a test: the apps were still open after a launch."""

    code, out, quit_ran = _run_to_game_mode()
    check("a launch that gets past the preflight quits the apps", quit_ran, out.strip())
    check("and asks for `on`", "closed 11" in out, out.strip())
    check("and the head exits clean", code == 0, f"exit {code}: {out.strip()}")


def test_an_already_running_game_still_reaches_it():
    """The if/elif immediately above the block. If its `fi` ever moves inside the launch
    branch, THIS is the path that silently stops closing apps — and it is the common one,
    because restarting the bot after a Ctrl-C always takes it."""

    code, out, quit_ran = _run_to_game_mode(game_up=True)
    check("a game already up does not skip the quit", quit_ran, out.strip())
    check("and it is recognised as already running",
          "already running" in out, out.strip())


def test_the_no_game_switch_still_reaches_it():
    """DBD_NO_GAME suppresses the Steam launch, not the app quit."""

    code, out, quit_ran = _run_to_game_mode(env={"DBD_NO_GAME": "1"})
    check("DBD_NO_GAME still quits the apps", quit_ran, out.strip())


def test_a_preflight_failure_leaves_the_apps_open():
    """The other half of the contract, and the reason the block sits where it does: a run
    that never reaches a stream must not leave eleven apps closed and a manifest to undo
    by hand. Each of these is a `return 1` above the block."""

    for name, knobs in (("an unreachable host", dict(reachable=False)),
                        ("a failed `moonlight list`", dict(list_ok=False)),
                        ("an app the host does not offer", dict(offers_app=False))):
        code, out, quit_ran = _run_to_game_mode(**knobs)
        check(f"{name} closes nothing", not quit_ran, out.strip())
        check(f"{name} stops the launch", code != 0, f"exit {code}: {out.strip()}")


def test_a_dry_run_reaches_the_block_and_closes_nothing():
    """Distinguishes the two silences that look identical from outside: `--dry-run` must
    reach the block and decline, not be skipped by a preflight gate on the way."""

    code, out, quit_ran = _run_to_game_mode(args=("--dry-run",))
    check("a dry run closes nothing", not quit_ran, out.strip())
    check("but still gets all the way through the head",
          code == 0, f"exit {code}: {out.strip()}")


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
