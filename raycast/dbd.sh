#!/bin/bash

# Raycast script command. Add this repo's raycast/ folder as a script
# directory in Raycast Settings → Extensions → Scripts.
#
# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title dbd
# @raycast.mode compact
#
# Optional parameters:
# @raycast.icon 🎮
# @raycast.packageName DBD
# @raycast.description Quit the background apps, remembering which were actually running; "Done Gaming" puts them back. The `dbd` SHELL function now does this itself before it arms, so this is for the times you want the apps gone without playing.

set -uo pipefail

"$(dirname "$0")/../bin/game-mode.sh" on
