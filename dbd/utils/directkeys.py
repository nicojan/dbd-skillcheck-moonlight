# directkeys.py
# Platform dispatcher. Callers keep importing PressKey / ReleaseKey / SPACE from here;
# the concrete implementation depends on the OS.
#
#   Windows -> directkeys_win  (ctypes SendInput, Windows virtual-key codes)
#   others  -> directkeys_posix (pyautogui, pyautogui key-name strings)
#
# The key constants are NOT interchangeable between the two backends, so always
# import them from this module rather than hardcoding values.

import sys

if sys.platform == "win32":
    from dbd.utils.directkeys_win import PressKey, ReleaseKey, UP, DOWN, A, SPACE
else:
    from dbd.utils.directkeys_posix import PressKey, ReleaseKey, UP, DOWN, A, SPACE

__all__ = ["PressKey", "ReleaseKey", "UP", "DOWN", "A", "SPACE"]
