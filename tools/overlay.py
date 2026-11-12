"""A click-through status badge over the Moonlight stream: is the bot pressing right now?

    .venv/bin/python tools/overlay.py                 # reads state/overlay.json
    .venv/bin/python tools/overlay.py --corner top-left

`autorun.py` starts this itself (pass `--no-overlay` to skip) and stops it on exit, so
there is normally nothing to run by hand. It reads the file the armed loop writes — see
`dbd/utils/overlay_state.py` for the contract and the two failure directions it designs
out — and never talks to the loop any other way.

What keeps it out of the bot's way:

* FRAMES. The bot screenshots the screen, so the panel is placed by `overlay_state.place`,
  which refuses any rect near the capture box. A refused position hides the panel rather
  than drawing it somewhere unproven.
* FOCUS. A non-activating, click-through panel in an accessory app never becomes the
  frontmost app, and it sits above layer 0, which `focus_watcher` ignores when it decides
  whether Moonlight has the keyboard.
* SPACES. A fullscreen Moonlight lives on its own Space; the panel joins every Space and is
  allowed beside fullscreen windows, at a level above them.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import objc
import Quartz
from AppKit import (
    NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered,
    NSColor, NSFont, NSLineBreakByTruncatingTail, NSMakeRect, NSPanel, NSScreen,
    NSScreenSaverWindowLevel, NSTextField, NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle, NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSObject, NSTimer
from time import time

from dbd.utils import overlay_state as ov

PANEL_SIZE = (380, 58)
POLL_SECONDS = 0.25

# Tone -> (r, g, b). Solid fills so the state reads in peripheral vision, white text on top.
TONES = {
    "go": (0.13, 0.62, 0.30),
    "hold": (0.90, 0.55, 0.05),
    "stop": (0.80, 0.15, 0.15),
    "idle": (0.35, 0.35, 0.38),
}
ALPHA = 0.88


def _label(frame, size, bold):
    field = NSTextField.alloc().initWithFrame_(frame)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setTextColor_(NSColor.whiteColor())
    field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                   else NSFont.systemFontOfSize_(size))
    field.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
    return field


def _make_panel():
    w, h = PANEL_SIZE
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, w, h),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered, False)
    panel.setLevel_(NSScreenSaverWindowLevel)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorFullScreenAuxiliary
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorIgnoresCycle)
    panel.setIgnoresMouseEvents_(True)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(False)
    panel.setHidesOnDeactivate_(False)
    panel.setFloatingPanel_(True)
    panel.setBecomesKeyOnlyIfNeeded_(True)

    root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    root.setWantsLayer_(True)
    root.layer().setCornerRadius_(10.0)
    panel.setContentView_(root)

    headline = _label(NSMakeRect(12, 24, w - 24, 28), 20, True)
    detail = _label(NSMakeRect(12, 6, w - 24, 18), 11, False)
    root.addSubview_(headline)
    root.addSubview_(detail)
    return panel, root, headline, detail


class Overlay(NSObject):
    """Polls the state file and redraws. Holds no state the file does not."""

    def initWithArgs_(self, args):
        self = objc.super(Overlay, self).init()
        if self is None:
            return None
        self.args = args
        self.panel, self.root, self.headline, self.detail = _make_panel()
        self.last_rect = None
        self.refused_said = False
        return self

    def tick_(self, _timer):
        if self.args.parent_pid and not _alive(self.args.parent_pid):
            NSApplication.sharedApplication().terminate_(None)
            return
        snap = ov.read_state(self.args.state)
        b = ov.badge(snap, time())
        rect = None
        if snap is not None:
            rect = ov.place(snap.get("content"), snap.get("capture"), PANEL_SIZE,
                            corner=self.args.corner, margin=self.args.margin)
            if rect is None and snap.get("capture") is not None and not self.refused_said:
                print(f"overlay: {self.args.corner} corner would touch the capture box; "
                      f"hidden. Try --corner.", file=sys.stderr)
                self.refused_said = True
        if rect is None:
            self.panel.orderOut_(None)
            self.last_rect = None
            return
        if rect != self.last_rect:
            primary_h = NSScreen.screens()[0].frame().size.height
            x, y, w, h = ov.to_appkit(rect, primary_h)
            self.panel.setFrame_display_(NSMakeRect(x, y, w, h), True)
            self.last_rect = rect
        r, g, bl = TONES[b.tone]
        self.root.layer().setBackgroundColor_(Quartz.CGColorCreateSRGB(r, g, bl, ALPHA))
        self.headline.setStringValue_(b.headline)
        self.detail.setStringValue_(b.detail)
        self.panel.orderFrontRegardless()


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", default=ov.DEFAULT_PATH)
    p.add_argument("--corner", default=ov.DEFAULT_CORNER, choices=ov.CORNERS)
    p.add_argument("--margin", type=int, default=ov.DEFAULT_MARGIN)
    p.add_argument("--parent-pid", type=int, default=None,
                   help="exit when this process does (autorun passes its own pid)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    overlay = Overlay.alloc().initWithArgs_(args)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        POLL_SECONDS, overlay, "tick:", None, True)
    overlay.tick_(None)
    app.run()


if __name__ == "__main__":
    main()
