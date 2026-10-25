#!/bin/bash

# Raycast script command. Add this repo's raycast/ folder as a script
# directory in Raycast Settings → Extensions → Scripts.
#
# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Done Gaming
# @raycast.mode compact
#
# Optional parameters:
# @raycast.icon 🔁
# @raycast.packageName DBD
# @raycast.description Reopen exactly the apps "dbd" closed for this gaming session — nothing else.

set -uo pipefail

"$(dirname "$0")/../bin/game-mode.sh" off
