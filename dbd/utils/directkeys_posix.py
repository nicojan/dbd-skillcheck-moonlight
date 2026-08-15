# directkeys_posix.py
# macOS / Linux key injection, used when the Windows SendInput path is unavailable.
# Key codes are pyautogui key names rather than Windows virtual-key codes.
#
# macOS note: the process running this needs Accessibility permission
# (System Settings > Privacy & Security > Accessibility) or the key events
# are silently dropped.

import pyautogui

UP = "up"
DOWN = "down"
A = "a"
SPACE = "space"

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.0


def PressKey(key_name):
    pyautogui.keyDown(key_name)


def ReleaseKey(key_name):
    pyautogui.keyUp(key_name)


if __name__ == "__main__":
    import time

    PressKey(A)
    time.sleep(0.5)
    ReleaseKey(A)
    print("Pressed")
