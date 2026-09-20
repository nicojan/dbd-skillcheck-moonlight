#!/bin/bash
# game-mode.sh — take the background apps off the machine before an armed match,
# and put back exactly the ones that were taken off.
#
#   game-mode.sh on             quit the listed apps, remembering which were running
#   game-mode.sh off            relaunch only what `on` actually closed
#   game-mode.sh on  --dry-run  print what would happen, touch nothing
#   game-mode.sh off --dry-run  same
#
# Why "exactly the ones": a fixed relaunch list would start apps that were not
# running before the session, so the machine would come back dirtier than it
# started every time. `on` writes a manifest; `off` reads it and deletes it.
#
# This matters for this repo specifically. A match played above load ~6 is not
# scorable evidence (see dbd/utils/load_tally.py), and four matches have already
# been thrown out for it.

set -uo pipefail

readonly STATE_DIR="${DBD_GAME_MODE_STATE_DIR:-$HOME/Library/Application Support/dbd-game-mode}"
readonly MANIFEST="$STATE_DIR/closed.tsv"

readonly OSASCRIPT=/usr/bin/osascript
readonly OPEN=/usr/bin/open
readonly PKILL=/usr/bin/pkill
readonly PGREP=/usr/bin/pgrep
readonly MDLS=/usr/bin/mdls

# One line per app to close: <label>|<bundle to relaunch>|<every bundle to quit...>
#
# Adobe is one group rather than one app on purpose. Quitting "Creative Cloud"
# alone leaves Core Sync, CCXProcess, Adobe Desktop Service and AdobeIPCBroker
# running, and those are the processes that actually hold CPU and disk — the
# main app was not even running when this list was taken. Relaunching just
# Creative Cloud brings its own helpers back.
readonly TARGETS=(
	"Bartender|/Applications/Bartender 6.app"
	"BetterTouchTool|/Applications/BetterTouchTool.app"
	"Quip|/Applications/Quip.app"
	"Chorus|/Applications/Chorus.app"
	"WeChat|/Applications/WeChat.app"
	"iMessage|/System/Applications/Messages.app"
	"Default Folder X|/Applications/Default Folder X.app"
	"Unclutter|/Applications/Unclutter 2.app"
	"Google Drive|/Applications/Google Drive.app"
	"Rocket|/Applications/Rocket.app"
	"Velja|/Applications/Velja.app"
	"TextWarden|/Applications/TextWarden.app"
	"MeetingBar|/Applications/MeetingBar.app"
	"Adobe Creative Cloud|/Applications/Utilities/Adobe Creative Cloud/ACC/Creative Cloud.app|/Applications/Utilities/Adobe Creative Cloud/ACC/Creative Cloud Helper.app|/Applications/Utilities/Adobe Creative Cloud Experience/CCXProcess/CCXProcess.app|/Applications/Utilities/Adobe Sync/CoreSync/Core Sync.app|/Library/Application Support/Adobe/Adobe Desktop Common/ADS/Adobe Desktop Service.app|/Library/Application Support/Adobe/Adobe Desktop Common/IPCBox/AdobeIPCBroker.app"
)

DRY_RUN=0

# ---------------------------------------------------------------- helpers

# Bound a command without `timeout`: a background helper that never answers an
# Apple Event would otherwise hang the whole sequence with Raycast's spinner up.
# Forking from bash also keeps the TCC-responsible process at a stable path.
#
# bounded <seconds> <command...>
bounded() {
	local budget="$1"
	shift
	"$@" &
	local pid=$! waited=0
	while /bin/kill -0 "$pid" 2>/dev/null; do
		if ((waited >= budget)); then
			/bin/kill -KILL "$pid" 2>/dev/null
			wait "$pid" 2>/dev/null
			return 124
		fi
		/bin/sleep 1
		((waited++))
	done
	wait "$pid"
}

# An app is running if something is executing out of its bundle. Matching the
# bundle path rather than a process name is what makes "Creative Cloud Helper"
# and "Unclutter 2" answerable at all — pgrep -x sees a truncated, renamed or
# shared executable name and says no.
running() {
	"$PGREP" -f "^$1/Contents/MacOS/" >/dev/null 2>&1
}

quit_bundle() {
	local app="$1" id
	running "$app" || return 0

	id=$("$MDLS" -name kMDItemCFBundleIdentifier -raw "$app" 2>/dev/null)
	if [[ -n "$id" && "$id" != "(null)" ]]; then
		bounded 8 "$OSASCRIPT" -e "tell application id \"$id\" to quit" >/dev/null 2>&1
	fi

	# Give it a real chance to close on its own terms before insisting.
	for _ in 1 2 3 4 5 6; do
		running "$app" || return 0
		/bin/sleep 1
	done
	"$PKILL" -f "^$app/Contents/MacOS/" >/dev/null 2>&1
	/bin/sleep 1
	running "$app" && return 1
	return 0
}

# ---------------------------------------------------------------- on

mode_on() {
	local -a closed=() already=() missing=() kept=()
	local line label restore rest app group_running entry

	# Read any manifest already on disk. Running `on` twice must not forget the
	# first run — by the second run the apps are already quit, so a fresh
	# manifest would be empty and `off` would restore nothing.
	#
	# A plain array of tab-separated lines, not an associative array: Raycast
	# runs this under /bin/bash, which is 3.2 and has no `declare -A`.
	if [[ -f "$MANIFEST" ]]; then
		while IFS= read -r entry; do
			[[ -n "$entry" ]] && kept+=("$entry")
		done <"$MANIFEST"
	fi

	for line in "${TARGETS[@]}"; do
		IFS='|' read -r label restore rest <<<"$line"
		local -a bundles=("$restore") extra=()
		if [[ -n "${rest:-}" ]]; then
			IFS='|' read -r -a extra <<<"$rest"
			bundles+=("${extra[@]}")
		fi

		if [[ ! -d "$restore" ]]; then
			missing+=("$label")
			continue
		fi

		group_running=0
		for app in "${bundles[@]}"; do
			running "$app" && group_running=1
		done

		if ((!group_running)); then
			already_remembered "$label" "${kept[@]:-}" || already+=("$label")
			continue
		fi

		if ((DRY_RUN)); then
			echo "would quit: $label"
			closed+=("$label")
			continue
		fi

		for app in "${bundles[@]}"; do
			quit_bundle "$app" || echo "  note: $label would not quit ($(basename "$app"))" >&2
		done
		already_remembered "$label" "${kept[@]:-}" || kept+=("$(printf '%s\t%s' "$label" "$restore")")
		closed+=("$label")
	done

	if ((!DRY_RUN)); then
		/bin/mkdir -p "$STATE_DIR"
		: >"$MANIFEST"
		for entry in "${kept[@]:-}"; do
			[[ -n "$entry" ]] && printf '%s\n' "$entry" >>"$MANIFEST"
		done
	fi

	((${#missing[@]})) && echo "not installed, skipped: ${missing[*]}" >&2
	((${#already[@]})) && echo "already closed: ${already[*]}" >&2

	if ((${#closed[@]})); then
		echo "Game mode: closed ${#closed[@]} — ${closed[*]}$(load_note)"
	else
		echo "Game mode: nothing left to close$(load_note)"
	fi
}

# already_remembered <label> <manifest line...>
#
# True when this label is already in the manifest, so a second `on` neither
# duplicates it nor reports it as an app that was never running.
already_remembered() {
	local label="$1" entry
	shift
	for entry in "$@"; do
		[[ "${entry%%$'\t'*}" == "$label" ]] && return 0
	done
	return 1
}

# ---------------------------------------------------------------- off

mode_off() {
	if [[ ! -f "$MANIFEST" ]]; then
		echo "Nothing to restore — no gaming session was started from here."
		return 0
	fi

	local -a restored=() failed=()
	local label restore

	while IFS=$'\t' read -r label restore; do
		[[ -n "$label" ]] || continue
		if ((DRY_RUN)); then
			echo "would relaunch: $label"
			restored+=("$label")
			continue
		fi
		if [[ -d "$restore" ]] && "$OPEN" -g -a "$restore" >/dev/null 2>&1; then
			restored+=("$label")
		else
			failed+=("$label")
		fi
	done <"$MANIFEST"

	((DRY_RUN)) || /bin/rm -f "$MANIFEST"

	((${#failed[@]})) && echo "failed to relaunch: ${failed[*]}" >&2

	if ((${#restored[@]})); then
		echo "Back to normal: reopened ${#restored[@]} — ${restored[*]}"
	else
		echo "Back to normal: nothing needed reopening."
	fi
}

# The load average is the number this whole exercise is about, so print it. It
# lags by design — a one-minute average seconds after a quit still carries the
# apps that just died — so it is a sanity read, not a verdict.
load_note() {
	local one
	one=$(/usr/bin/uptime | /usr/bin/sed 's/.*averages*: *//' | /usr/bin/awk '{print $1}' | /usr/bin/tr -d ',')
	[[ -n "$one" ]] && echo " (load 1m: $one)"
}

# ---------------------------------------------------------------- main

main() {
	local mode="${1:-}"
	[[ "${2:-}" == "--dry-run" ]] && DRY_RUN=1
	readonly DRY_RUN

	case "$mode" in
	on) mode_on ;;
	off) mode_off ;;
	*)
		echo "usage: game-mode.sh {on|off} [--dry-run]" >&2
		return 2
		;;
	esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
	main "$@"
fi
