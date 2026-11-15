"""The ping sidecar: what each fire record carries, and the ways it could read calm wrongly.

The field that matters is `ping_worst_2s_ms`. A spike that resolved before the press still
says the link was moving, and a record that only carried the latest reply would read that
fire as calm. The second failure is silence read as quiet: nothing heard must stamp None.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbd.utils.link_ping import LinkPinger, parse_line

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


REPLY = "64 bytes from 100.67.246.5: icmp_seq={n} ttl=64 time={ms} ms"


def test_parses_macos_ping_output():
    check("a reply parses to its time",
          parse_line(REPLY.format(n=3, ms="1.157")) == ("reply", 1.157))
    check("a timeout parses as a loss",
          parse_line("Request timeout for icmp_seq 12") == ("lost", None))
    check("the header line is ignored",
          parse_line("PING compute (100.67.246.5): 56 data bytes") is None)
    check("the statistics footer is ignored",
          parse_line("round-trip min/avg/max/stddev = 1.157/1.991/3.738/0.899 ms") is None)


def test_nothing_heard_is_unknown_not_quiet():
    p = LinkPinger(clock=lambda: 0.0)
    snap = p.snapshot()
    check("every field is None before a reply", all(v is None for v in snap.values()), snap)
    check("summary says not measured", "not measured" in p.summary(), p.summary())


def test_worst_holds_a_spike_the_latest_forgot():
    p = LinkPinger(clock=lambda: 0.0)
    p.feed(REPLY.format(n=1, ms="2.0"), now=10.0)
    p.feed(REPLY.format(n=2, ms="38.5"), now=10.2)
    p.feed(REPLY.format(n=3, ms="2.1"), now=10.4)
    snap = p.snapshot(now=10.5)
    check("latest is the calm reply", snap["ping_ms"] == 2.1, snap)
    check("worst in 2 s still carries the spike", snap["ping_worst_2s_ms"] == 38.5, snap)
    check("age is measured from the latest reply", snap["ping_age_ms"] == 100, snap)


def test_window_forgets_after_two_seconds():
    p = LinkPinger(clock=lambda: 0.0)
    p.feed(REPLY.format(n=1, ms="40.0"), now=10.0)
    p.feed(REPLY.format(n=2, ms="2.0"), now=11.0)
    snap = p.snapshot(now=12.5)
    check("a spike older than the window drops out", snap["ping_worst_2s_ms"] == 2.0, snap)


def test_losses_are_counted_in_window():
    p = LinkPinger(clock=lambda: 0.0)
    p.feed(REPLY.format(n=1, ms="2.0"), now=10.0)
    p.feed("Request timeout for icmp_seq 2", now=10.2)
    p.feed("Request timeout for icmp_seq 3", now=10.4)
    snap = p.snapshot(now=10.5)
    check("two losses in the window", snap["ping_lost_2s"] == 2, snap)
    check("a loss does not overwrite the last reply", snap["ping_ms"] == 2.0, snap)
    check("summary reports the losses", "2 lost" in p.summary(), p.summary())


def test_stale_link_shows_its_age():
    # The process died or the host vanished: the last reply is still there, but its age
    # says how much to trust it, and the window is empty rather than falsely calm.
    p = LinkPinger(clock=lambda: 0.0)
    p.feed(REPLY.format(n=1, ms="2.0"), now=10.0)
    snap = p.snapshot(now=40.0)
    check("age grows when replies stop", snap["ping_age_ms"] == 30000, snap)
    check("worst is None once the window is empty", snap["ping_worst_2s_ms"] is None, snap)
    check("lost is None, not 0, once the window is empty", snap["ping_lost_2s"] is None, snap)


def test_start_failure_never_raises():
    import dbd.utils.link_ping as lp

    def no_binary(*a, **k):
        raise FileNotFoundError("/sbin/ping")

    real, lp.subprocess.Popen = lp.subprocess.Popen, no_binary
    try:
        p = LinkPinger(host="compute")
        started = p.start()
    finally:
        lp.subprocess.Popen = real
    check("start returns False when ping cannot spawn", started is False)
    check("the reason reaches the summary", "/sbin/ping" in p.summary(), p.summary())
    p.stop()
    check("stop after a failed start is safe", True)


def test_summary_figures():
    p = LinkPinger(clock=lambda: 0.0)
    for i, ms in enumerate([1.0, 2.0, 3.0, 4.0, 50.0]):
        p.feed(REPLY.format(n=i, ms=ms), now=float(i))
    s = p.summary()
    check("summary has the median", "median 3.0 ms" in s, s)
    check("summary has the max", "max 50.0" in s, s)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            print(fn.__doc__ or name)
            fn()
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
