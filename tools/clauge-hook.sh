#!/bin/sh
# Clauge execution-state hook.
#
# Claude Code runs this on lifecycle events and pipes the event JSON to stdin.
# We record which event fired, for which session, and when.
#
# WHAT IS CAPTURED, exactly: an event name, a session id, an agent id, and a
# clock reading. Nothing else is read from the payload -- not the prompt, not
# the tool arguments, not the transcript path, not the cwd, not the assistant's
# message. The two ids are opaque identifiers Claude Code generates; they are
# used as filenames so that concurrent sessions can be told apart, and for
# nothing else.
#
# This is a real widening from the first version, which captured an event name
# and a timestamp and nothing else. That made the metadata-only promise
# structural -- there was nothing there to leak. It is now a policy instead,
# which is weaker, and the reason for accepting that is that a single global
# slot silently misreports the moment a second session exists: two terminals
# overwrite each other and the panel confidently shows the wrong one.
#
# The event name arrives as $1, from the settings entry we wrote, rather than
# being parsed out of the payload. POSIX sh has no JSON parser and this runs on
# every tool call, so what parsing there is stays to one sed per invocation.
event=${1:-unknown}
input=$(cat)

# Extract the session id, and SANITISE IT IN THE PATTERN rather than after.
#
# This value ends up in a filename, so it is the one piece of attacker-shaped
# input on this path. The character class is the sanitiser: a value containing
# a slash, a quote, a space or a NUL simply fails to match and falls through to
# "unknown" -- there is no separate validation step that can be forgotten or
# reordered. The {1,64} bound stops a pathological payload producing a filename
# the filesystem rejects.
sid=$(printf '%s' "$input" |
	sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([0-9A-Za-z._-]\{1,64\}\)".*/\1/p' |
	head -1)
[ -n "$sid" ] || sid=unknown

DIR="$HOME/.clauge/state"
[ -d "$DIR" ] || mkdir -p "$DIR" 2>/dev/null

case $event in
SessionEnd)
	# The session is over: take its whole directory with it, agents and all.
	# rm -rf on a path built from the sanitised id above; a traversal cannot
	# reach here because the pattern would not have matched.
	rm -rf "$DIR/$sid" 2>/dev/null
	rm -f "$DIR/$sid.state" 2>/dev/null
	;;
SubagentStart|SubagentStop)
	# One file per agent, named by the id Claude Code assigns. This is why
	# the count is exact and lock-free: every agent owns its own file, so
	# two agents starting at once cannot race on a shared counter, and a
	# stop removes precisely the agent that stopped rather than
	# decrementing something and hoping.
	aid=$(printf '%s' "$input" |
		sed -n 's/.*"agent_id"[[:space:]]*:[[:space:]]*"\([0-9A-Za-z._-]\{1,64\}\)".*/\1/p' |
		head -1)
	[ -n "$aid" ] || aid=unknown
	[ -d "$DIR/$sid" ] || mkdir -p "$DIR/$sid" 2>/dev/null
	if [ "$event" = "SubagentStart" ]; then
		: > "$DIR/$sid/$aid" 2>/dev/null
	else
		rm -f "$DIR/$sid/$aid" 2>/dev/null
	fi
	;;
*)
	# Atomic write, same reason as the statusline shim: the daemon may read
	# at any moment and a half-written file parses as malformed. The
	# 2>/dev/null must come BEFORE the '>' target -- if opening the target
	# itself fails, the shell reports it using whatever stderr was in effect
	# when the '>' was processed, so a trailing redirect looks like it
	# suppresses the error and does not.
	printf '{"event":"%s","t":%s}' "$event" "$(date +%s)" 2>/dev/null \
		> "$DIR/$sid.state.tmp" &&
		mv -f "$DIR/$sid.state.tmp" "$DIR/$sid.state" 2>/dev/null
	;;
esac

# Never fail a hook. A non-zero exit is a signal to Claude Code, and Clauge
# having a bad day must not become the user's bad day.
exit 0
