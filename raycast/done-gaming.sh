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

# Since 2026-11-03 Switchboard owns game mode, and its own generated Raycast folder has
# this command too; this copy stays for rollback. game-mode.sh only when Switchboard is
# absent: the two keep separate lists of what to reopen, so never mix them in a session.
sb="${DBD_SWITCHBOARD:-$HOME/.local/bin/switchboard}"
if [[ -x "$sb" ]]; then
	exec "$sb" run done-gaming
fi
exec "$(dirname "$0")/../bin/game-mode.sh" off
