#!/bin/bash

# Raycast script command. Add ~/dev/dbd_autoSkillCheck/raycast as a script
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

"$HOME/dev/dbd_autoSkillCheck/bin/game-mode.sh" off
