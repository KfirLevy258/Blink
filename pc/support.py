"""`blink support`: one zip a non-technical owner can send us (issue #20).

The Support section used to ask the owner to find ~/.blink/bridge.log and
paste `blink status` by hand, which is where a support exchange usually
stalled. This collects both, plus the versions, into one file on the Desktop
and says what it collected and where, so the owner can open it and look
before sending it.

What goes in is cut by an allowlist, not by a list of things to remove.
bridge.log is written through the redacting paths today (protocol.log_name,
logbook.console_line), but until 2026-10-30 both printed whole messages,
ids and usage figures into it, and a log rotated before that date can still
be on a customer's disk. A filter that removes the shapes we can think of
(UUIDs, hex, braces) passes the shapes we did not: an SSID printed bare, a
session name, "session 42%". So the rule is the other way round.

The allowlist is the shape of our own printed lines: TEMPLATES, one per
string literal that prints a log line or a `blink status` row, each its
constant pieces in order with a hole (None) wherever the f-string fills
something in. A line is kept as far as a template matches it from its first
character, and every hole is masked whatever it holds. Earlier rounds of
this change matched words, then loose two-word pieces anywhere in a line,
and each review (2026-11-24) found a value that came through: a Codex
project called "Build Release" was made of words our code prints, and a WSL
distribution called "not running" was itself one of our pieces, so it was
kept inside the very row that names it. A hole is now never searched for a
piece of ours. A template starts only at the start of a line. The one
exception is JOINS: the `+` joins in our code whose right-hand side is
itself our own text, each listed by a person who read it. After any other
template, whatever follows is masked, even when it is a template of ours:
round 6 let one follow any template that ended on its constant text, and
"[bridge] logo: not running" (a `+` join in pc/bridge.py whose right-hand
side carries an error string) kept its whole value.

When several templates read the same line, a character is ours only where
all of them agree (_cover_at). Round 7 took the one that covered the most,
and review (2026-11-25) found that "[usage] session" + a legacy value of
"applied" came through, because '[usage] session applied' is a longer
template of ours. In bridge.log a template that ends before the line does
also counts as a reading, of our text with a value after it, because a log
can be older than this code. A template that is a bare tag and one value
(_generic, `f"[bridge] {err}"`) votes too in bridge.log, so a line under
such a tag keeps its tag and nothing else: round 8 left it out, and review
found a value that was word for word one of our [bridge] lines shown whole.
A filled-in run is shown only if it is spacing; round 7 showed any run
without an ASCII letter or digit, so "[wifi] joined 東京" kept the network's
name, and round 8 any run of punctuation, so a network called "!!!" did.

The two version numbers in status.txt (the App row and the Claude Code row)
are the only filled-in values kept there, and each only when it is a whole
version straight after the word that names it; the firmware's is in
system.txt, from board.json. In the log, a message's type after "->" or
"<-" is masked like any other value under [bridge]: an error printed as
"[bridge] " + err reads the same when err is "-> usage". Inside our own text, a
word that is the account's or the computer's name is masked too
(private_words), labels and tags aside.

A log line is kept only if it starts with one of our own tags (LOG_TAGS)
and holds no brace; a line that is not ours (a raw message dump, a legacy
echo of the board's console, a traceback's message) is dropped and counted.
A tagged line that no template matches keeps its tag and nothing else.
bridge.log keeps no filled-in value at all, versions included.

`blink status` is cut by row: only the rows it is known to print are kept
(STATUS_LABELS; never the Wire row, which is the usage message itself), a
continuation line only under the few rows that print one (CONTINUED), and
each line is matched as above.

TEMPLATES is written out below, not read from our own code at run time.
Round 3 of this change built a list from every string constant in pc/,
docstrings included, at run time: names were among its 4,793 words, and it
needed the frozen build to hand back its own bytecode, where an empty answer
would have masked everything. The list here is fixed, and
tests/pc/test_support_cli.py derives it again from the source with ast and
fails when the two differ, so a line added to the code is either added here
by a person who looked at it, or masked.

The bundle never opens a serial port and never touches the service: it reads
files and runs `blink status`, which itself only reads. Nothing here logs
what it read, and nothing resolves home at import (tests/test_sandbox.py).
Standard library only, like everything in pc/.
"""
import getpass
import os
import platform
import re
import sys
import time
import zipfile

# The tags our own lines start with: every "[tag]" that opens a string
# literal in claude_usage_bridge.py, blink_main.py and pc/, and the board's
# console tags (logbook.CONSOLE_TAGS) that console_line can leave bare.
# tests/pc/test_support_cli.py derives the same set from the source and
# fails when they differ, so a tag added later is not silently dropped from
# every bundle. A line under any other tag is not one we can vouch for.
LOG_TAGS = frozenset({
    "ask", "bl", "boot", "bridge", "burn", "cfg", "claude", "desktop",
    "fatal", "focus", "ingest", "log", "logo", "mem", "ota", "plan",
    "proto", "settings", "sleep", "slide", "spend", "status", "swipe",
    "tap", "test", "theme", "time", "touch", "update", "usage", "watchdog",
    "wifi", "wsl",
})

# How much of the log goes in: the newest 256 KB, which is about ten hours
# of a busy desk at the 24 KB/hour logbook.CAP_BYTES measured. Support reads
# the last few hours; the whole 20 MB of history is too big to email.
LOG_CAP_BYTES = 256 * 1024
LOG_FILES = ("bridge.log.1", "bridge.log")    # oldest first
# logbook.BOARD_LINE: what bridge.log says for a console line of the board
# that is not one of our printks. A constant, so it is kept whole.
BOARD_LINE = "(board line)"

MASK = "<x>"
PATH_MASK = "<path>"

# The shapes of the string literals that print our log lines (a literal
# opening with one of LOG_TAGS) and the rows of `blink status`
# (STATUS_SOURCES in the test), split at newlines: constant pieces, and None
# where the f-string fills something in. A shape that starts with a hole, or
# whose first piece has no letter or digit ("            " before a filled-in
# continuation), is left out: it would vouch for nothing. Generated by
# tests/pc/test_support_cli.py::test_templates_are_the_shapes_our_lines_print;
# paste its output here when it fails.
TEMPLATES = (
    ('            (if that finds nothing, this board is ahead of the published release)',),
    ('            Claude Code on Windows itself is not read',),
    ('            Claude Desktop fills it in after its first reading; if it stays empty while',),
    ('            Codex asks once whether to trust a hook. If that',),
    ('            Store build marker found but its folder is gone',),
    ('            also looked at ', None),
    ('            also: ', None),
    ('            could not be read: ', None),
    ('            data found in the Store package folder: ', None),
    ('            data moved by the Store build: ', None),
    ('            folder lookup failed', None, ', Roaming used as a fallback'),
    ('            last seen on ', None),
    ('            looked at ', None),
    ('            looked under ', None),
    ('            never runs -- open Codex and accept it.',),
    ('            newer app files, no usage cache yet: ', None),
    ('            no Store package folder under ', None, ' (looked for Claude_*)'),
    ('            other copy not read: ', None),
    ('            other serial devices: ', None, ' -- asked once, not BLINK, left alone'),
    ('            prompt was declined the hook stays in the file and',),
    ('            renamed from ', None, ' -- run `', None, ' install`'),
    ('            run `', None, ' install --wsl ', None, '` to set Blink up there'),
    ('            run: ', None, ' update'),
    ('            the app shows usage, please report it',),
    ('            the board is on ', None, ' and this app is ', None, ' -- they ship together'),
    ('            the one Blink was set up for is gone; another distribution is called ', None, ' now --'),
    ('            this is a checkout: bring it up to date with git',),
    (" (Claude Desktop hasn't written a reading yet; it does within a few minutes)",),
    (' (last seen on ', None, ')'),
    (' (no Claude page)',),
    (' (sign in to Claude Desktop for the Claude page)',),
    (' (the panel also reads ', None, ')'),
    (' and Codex',),
    ('--quiet',),
    ('--user',),
    ('/query',),
    ('/tn',),
    ('Activity    PARTIAL -- ', None, '/', None, ' hooks present; run `', None, ' install` to restore them'),
    ('Activity    hooks installed (', None, '/', None, ' events', None, ')'),
    ('Activity    hooks installed (', None, '/', None, ' events), but settings.json sets disableAllHooks -- Claude Code runs none of them'),
    ('Activity    hooks not installed -- the busy/idle pip will stay dark',),
    ('Activity    not checked -- ', None, ' ', None),
    ('Activity    unknown -- settings.json could not be read',),
    ('Activity    unknown -- settings.json does not parse',),
    ('App         ', None),
    ('Board       ', None),
    ('Board       ', None, ' (', None, ')', None),
    ('Board       none identified yet -- looking at ', None),
    ('Board       not plugged in',),
    ('Bridge      ',),
    ('Bridge      not checked (', None, ')'),
    ('Claude Code ', None),
    ('Claude Code ', None, ' -- TOO OLD, needs ', None, '+ (panel will stay blank)'),
    ('Claude Code ', None, ' -- could not read a version from that'),
    ('Claude Code WSL (', None, '), '),
    ('Claude Code not found -- Claude Desktop is installed but has recorded no usage yet; open it and sign in',),
    ("Claude Code not found -- Claude Desktop is signed in but hasn't written a reading yet; it does within a few minutes",),
    ('Claude Code not found -- and no Claude Desktop cache or Codex log either, so nothing is reporting usage',),
    ('Claude Code not found -- running on ', None, ' (', None, ')', None),
    ('Claude Code not found -- running on Codex alone',),
    ('Claude Code on PATH, but `claude --version` did not answer',),
    ('Claude Desktop',),
    ('Claude Desktop alone',),
    ('Claude Desktop and Codex',),
    ('Claude Desktop refreshes this only while you are at the machine,',),
    ('Click the Claude icon ', None, ' to refresh it now.'),
    ('Codex',),
    ('Codex       ', None, ' recent session log(s), none with a rate-limit line -- finish one turn in Codex; if that does not help, its log format may have changed; please report it'),
    ('Codex       no session logs (Codex not installed, or never run)',),
    ('Codex       session log parsed, reading ', None, ' old'),
    ('Codex hook  firing, ', None, ' live session', None),
    ('Codex hook  not installed -- run `', None, ' install` on a machine with Codex'),
    ('Codex hook  registered, but it has never written anything',),
    ('Codex hook  registered, but its slots could not be read (', None, ')'),
    ('Desktop     no usage cache (Claude Desktop installed but never signed in)',),
    ('Desktop     no usage cache (Claude Desktop not installed, or never signed in)',),
    ('Desktop     usage cache parsed, reading ', None, ' old'),
    ('Desktop     usage cache present but could not be read',),
    ("Desktop     usage cache present but did not parse -- the app's file format may have changed; please report it",),
    ('Desktop     usage cache present but holds no usage figures yet',),
    ('Link        USB cable',),
    ('Link        WiFi set up, board not found',),
    ('Link        WiFi, board at ',),
    ('Link        no board connected',),
    ('Link        unknown (app not reporting)',),
    ('Session     no five-hour window',),
    ('Session     no five-hour window on Codex (Codex reported a weekly limit only)',),
    ('Status line ',),
    ('Status line inside ', None, ', not checked: it ', None),
    ('Status line installed inside ', None),
    ('Status line not installed inside ', None),
    ('Usage data  ', None, 's old -- open Claude Code in ', None, ' to refresh it'),
    ('Usage data  ', None, 's old -- open Claude Code to refresh it'),
    ('Usage data  fresh (', None, 's old)'),
    ("Usage data  from Claude Desktop's own files (no Claude Code status line on this machine)",),
    ('Usage data  from the Codex session log (no Claude Code status line yet)',),
    ("Usage data  none yet -- Claude Desktop hasn't written a reading; it does within a few minutes",),
    ('Usage data  none yet -- Claude Desktop is installed; open it and sign in',),
    ('Usage data  none yet -- open Claude Code in ', None, ' once so it renders its status line'),
    ('Usage data  none yet -- open Claude Code once so it renders its status line',),
    ('Usage refresh ',),
    ('[ask] answered',),
    ('[ask] closed unanswered',),
    ('[ask] dropped: not shown in time',),
    ('[ask] shown',),
    ('[ask] withdrawn: the board is dozing',),
    ('[ask] withdrawn: the computer went away',),
    ('[boot] edition',),
    ('[boot] logo: cannot map partition',),
    ('[boot] logo: company',),
    ('[boot] logo: frame failed to decode',),
    ('[boot] logo: none',),
    ('[boot] unknown edition',),
    ('[bridge]',),
    ('[bridge] ', None),
    ('[bridge] ', None, ' answered; not resetting it'),
    ('[bridge] ', None, ' burn line working again'),
    ('[bridge] ', None, ' did not answer; trying ', None),
    ('[bridge] ', None, ' unchanged ', None, ' not logged'),
    ('[bridge] ', None, 'burn not sent: ', None),
    ('[bridge] -> ', None),
    ('[bridge] <- ', None),
    ("[bridge] Blink's config.json could not be read; showing this computer's own readings until it can",),
    ('[bridge] NOT SENT: ', None),
    ('[bridge] another Blink daemon is already running and has the board; waiting for it to exit. Two of them on one cable would interleave on the wire.',),
    ('[bridge] board found at ', None),
    ('[bridge] builder state unknown: ', None),
    ('[bridge] connected on ', None),
    ('[bridge] could not check the login service file: ', None),
    ('[bridge] could not record the pid: ', None),
    ('[bridge] link ok: ', None, ' keepalives in the last ', None, 's'),
    ('[bridge] log rotated; the previous one is ', None, '.1'),
    ('[bridge] login service file brought up to date; it takes effect at the next login or service reload',),
    ('[bridge] logo: ',),
    ('[bridge] logo: ', None, '%'),
    ('[bridge] main source: ', None),
    ('[bridge] no usage data yet -- open Claude Code once so it renders its status line',),
    ('[bridge] nothing answered quickly; listening longer before giving up',),
    ('[bridge] nothing here answers; waiting for a board to be plugged in',),
    ('[bridge] ota: ', None),
    ('[bridge] ota: ', None, ' needs protocol ', None, ' and this app speaks ', None, ' -- update the app first'),
    ('[bridge] ota: ', None, ' publishes no app for this platform; the board will run ahead of the app'),
    ('[bridge] ota: ', None, '%'),
    ('[bridge] ota: FAILED: ', None),
    ('[bridge] ota: FAILED: connection lost',),
    ('[bridge] ota: app update failed; installing the firmware with the current app',),
    ('[bridge] ota: asset is ', None, ' bytes, manifest says ', None),
    ('[bridge] ota: board has ', None, ', release has ', None, ' -- nothing to do'),
    ('[bridge] ota: board speaks ', None, ', this app speaks ', None, ' -- not offering an update'),
    ('[bridge] ota: closing port to flash ', None),
    ('[bridge] ota: could not resume ', None, ' after ', None, ' attempts; giving up'),
    ('[bridge] ota: download failed: ', None),
    ('[bridge] ota: flashing ', None, ' (', None, ' bytes)'),
    ('[bridge] ota: no manifest yet; ', None, ' stays approved (attempt ', None, ' of ', None, ')'),
    ('[bridge] ota: not offering ', None, ' -- ', None),
    ('[bridge] ota: offering ', None, ' (', None, ' bytes)'),
    ('[bridge] ota: resuming the approved install of ', None),
    ('[bridge] ota: sent; waiting for the board to restart',),
    ('[bridge] ota: sha256 ', None, ' != manifest ', None),
    ('[bridge] ota: the release moved on; not resuming',),
    ('[bridge] ota: updating this app to ', None, ' first'),
    ('[bridge] probe not sent: ', None),
    ('[bridge] serial lost: ', None, '; looking for the board on every port'),
    ('[bridge] spend burn line working again',),
    ('[bridge] spend burn not sent: ', None),
    ('[bridge] stopping once the ', None, ' has finished'),
    ('[bridge] the board speaks ', None, ' and this app speaks ', None, ' -- update the app on this computer; firmware updates are held until you do'),
    ('[bridge] the login service file has been edited, so it was left as it is; run `blink install` to give it the ', None, ' s stop grace a firmware write needs'),
    ('[bridge] the other daemon exited; taking over',),
    ('[bridge] theme builder did not open: ', None),
    ('[bridge] theme builder: no browser to open here; the board says to run blink theme',),
    ('[bridge] theme not sent: ', None),
    ('[bridge] upkeep failed while waiting: ', None),
    ('[bridge] waiting for the board (', None, ')...'),
    ('[burn] a Codex sessions directory cannot be listed; the burn view goes on without it',),
    ('[burn] a project directory cannot be listed; the burn view goes on without it',),
    ('[burn] every Codex sessions directory lists again',),
    ('[burn] every project directory lists again',),
    ('[burn] the Claude Code projects folder cannot be listed; no burn view until it can',),
    ('[burn] the Claude Code projects folder lists again',),
    ('[burn] the Codex sessions folder cannot be listed; no burn view until it can',),
    ('[burn] the Codex sessions folder lists again',),
    ('[cfg] ',),
    ('[cfg] could not store the edition',),
    ('[cfg] edition',),
    ('[cfg] edition already stamped as',),
    ('[cfg] edition set to',),
    ('[cfg] migrated 1.3.4 record',),
    ('[cfg] migrated pre-OTA record',),
    ('[cfg] migrated pre-edition record',),
    ('[cfg] migrated pre-lock record',),
    ('[cfg] migrated pre-main_src record',),
    ('[cfg] persist failed',),
    ('[cfg] storage open failed',),
    ('[cfg] theme and focus taken from the older slot',),
    ('[cfg] unknown edition',),
    ('[claude] ', None, ' was written ', None, 's ago but its pid ', None, " is already gone: that pid may not name the session's own process on this machine, so pid liveness is DISABLED until one proves live -- until then, sessions that end without SessionEnd drop out after ", None, 's as before'),
    ('[desktop] ', None, ' did not parse as a usage cache; ignoring this source'),
    ('[fatal] SELFTEST BUILD: will fault at',),
    ('[fatal] reason',),
    ('[fatal] selftest: writing through NULL at',),
    ('[fatal] this boot follows',),
    ('[focus] could not store the choice',),
    ('[ingest] ', None, ' failed and will be skipped (', None, ')'),
    ('[ingest] ', None, ' is working again'),
    ('[ingest] Desktop data folder: ', None),
    ('[ingest] Desktop folder lookup failed (', None, '); reading Roaming'),
    ('[ingest] the board asked for a provider that is not reporting; keeping ', None),
    ('[log] last ', None, ' lines repeated ', None, ' times'),
    ('[log] last message repeated ', None, ' times'),
    ('[logo] rejected',),
    ('[logo] remove failed',),
    ('[logo] removed',),
    ('[logo] written',),
    ('[ota] confirm failed',),
    ("[ota] could not read the chip's eFuses on ", None, '; not flashing. ', None),
    ('[ota] could not read the release feed: ', None),
    ('[ota] downloading',),
    ('[ota] image confirmed',),
    ('[ota] install confirm withdrawn: the offer it named is no longer available',),
    ('[ota] install not started: the offer changed under the confirm',),
    ('[ota] install not started: the offer changed under the prompt',),
    ('[ota] not healthy within 90 s -- rebooting to revert',),
    ('[ota] offer shown',),
    ('[ota] offer withdrawn: a different version is on offer',),
    ('[ota] offer withdrawn: state is no longer available',),
    ('[ota] offer withdrawn: the board is dozing',),
    ('[ota] sha256 mismatch',),
    ('[ota] slot1 write failed',),
    ('[ota] stream aborted',),
    ('[ota] test boot of',),
    ('[ota] truncated',),
    ('[ota] update REVERTED',),
    ('[ota] update landed',),
    ('[ota] verified and marked pending',),
    ('[plan]',),
    ('[proto] burn line refused',),
    ('[proto] daemon offers',),
    ('[proto] daemon resuming install of',),
    ('[proto] host connected',),
    ('[proto] host says bye',),
    ('[proto] host status',),
    ('[proto] host time synced',),
    ('[proto] host went away',),
    ('[proto] ota failed',),
    ('[proto] ota: writing',),
    ('[proto] pref_set refused',),
    ('[proto] rx ring overflow, dropped',),
    ('[proto] sburn line refused',),
    ('[settings] logo encoder bug',),
    ('[settings] logo preview could not be read',),
    ('[settings] logo preview not kept',),
    ('[settings] request failed',),
    ('[settings] request file refused',),
    ('[settings] request served',),
    ('[settings] the board still reports its logo after remove',),
    ('[sleep] dozing',),
    ('[sleep] waking',),
    ('[slide] VSCRDEF refused; transitions will not scroll',),
    ('[spend] count not saved (', None, ')'),
    ('[spend] reading failed',),
    ('[status]',),
    ('[status] ', None, ' ', None),
    ('[status] ', None, ' page unreadable (', None, ')'),
    ('[status] ', None, ' unknown (no answer for ', None, ' min)'),
    ('[status] check failed: ', None),
    ('[tap] cannot write ', None, ': ', None, '. The transcript is incomplete.'),
    ('[test] confirm fails',),
    ('[test] hang after boot',),
    ('[test] never healthy',),
    ('[theme] builder could not start: ', None),
    ('[theme] builder ended: ', None, ' after ', None, ' s (pings=', None, ' previews=', None, ' saves=', None, ' cancels=', None, ' closes=', None, ' tabs=', None, ')'),
    ("[theme] could not keep the board's choice: ", None),
    ('[theme] could not record ', None, ': ', None),
    ("[theme] could not record the board's theme: ", None),
    ('[theme] could not store the theme',),
    ('[theme] line refused',),
    ('[theme] preview cancelled',),
    ('[theme] preview timed out; back to the stored theme',),
    ('[theme] saved',),
    ('[theme] the builder ended; told the board',),
    ("[theme] the theme was changed on the board while this app was away; keeping the board's choice",),
    ('[time] manual time',),
    ('[touch] INJECT BUILD: the PC can drive this panel -- bench only, never ship this',),
    ('[update] ', None),
    ('[update] ', None, ' is available; automatic updates are turned off on this machine'),
    ('[update] ', None, ' is available; run `blink update` to install it'),
    ('[update] cannot read ', None, ': ', None, '; using the built-in key'),
    ('[update] check failed: ', None),
    ('[update] could not read the release feed: ', None),
    ('[update] could not record the pending update: ', None),
    ('[update] could not start the new program: ', None),
    ('[update] download failed: ', None),
    ('[update] installing ', None),
    ('[update] manifest signature did not verify -- ignoring the feed',),
    ('[update] restarting into the new version',),
    ('[update] restored the previous program at ', None),
    ('[update] running from a checkout: updates come from git',),
    ('[usage] FATAL: mbedTLS allocator hookup failed',),
    ('[usage] cleared a stored refresh token: no WiFi in this build',),
    ('[usage] display not ready',),
    ('[usage] firmware boot OK',),
    ('[usage] mode: USB bridge',),
    ('[usage] mode: USB bridge (no host yet)',),
    ('[usage] no app after 60 s; dozing until one speaks',),
    ('[usage] panel: DBI bus not ready, MADCTL uncorrected',),
    ('[usage] panel: MADCTL write failed',),
    ('[usage] session',),
    ('[usage] session applied',),
    ('[watchdog] ', None),
    ('[wifi] ',),
    ('[wifi] connect refused',),
    ('[wifi] could not remove the pairing file; delete the private folder in .blink by hand',),
    ('[wifi] could not save the pairing',),
    ('[wifi] discovery socket failed',),
    ('[wifi] forget failed',),
    ('[wifi] forgotten',),
    ('[wifi] handshake refused',),
    ('[wifi] heap free',),
    ('[wifi] heap low, restarting',),
    ('[wifi] join failed',),
    ('[wifi] joined',),
    ('[wifi] link lost (', None, ')'),
    ('[wifi] listen failed',),
    ('[wifi] lost the network',),
    ('[wifi] paired',),
    ('[wifi] radio up',),
    ('[wifi] scan failed',),
    ('[wifi] selftest failed',),
    ('[wifi] session closed',),
    ('[wifi] session open',),
    ('[wifi] the board could not erase its WiFi settings',),
    ('[wifi] the board joined but did not answer over WiFi (', None, ')'),
    ('[wifi] the board was set up from another computer or forgot this one; plug it in and tap Open on PC on the board',),
    ('[wifi] the pairing file cannot be read, so the board is treated as unpaired',),
    ('[wifi] the pairing file is not owner-only, so the board is treated as unpaired',),
    ('[wifi] verified',),
    ('[wsl] check failed: ', None),
    ("[wsl] could not check the distribution's name: ", None),
    ('[wsl] the WSL distribution Blink was set up for is gone; not reading or writing another one in its place',),
    ('a board is',),
    ('activity light from Codex only: Claude hooks not working',),
    ('activity light from the Code tab',),
    ('and stops once it has been idle a while -- so a reading this old',),
    ('blink',),
    ('blink-bridge.service',),
    ('blink-hook.sh',),
    ('blink-statusline.sh',),
    ('board_id',),
    ('cost',),
    ('could not be checked',),
    ("could not check -- Windows did not list WSL's distributions",),
    ('failing',),
    ('firmware ', None),
    ('fw',),
    ('gone',),
    ('gui/', None, '/', None),
    ('id ', None),
    ('in the notification area (system tray)',),
    ('in your menu bar',),
    ('installed at ', None),
    ('installed but not loaded -- run `', None, ' install` to start it'),
    ('ip',),
    ('is not on this computer',),
    ('is not running',),
    ('is-active',),
    ('last exit code',),
    ('launchctl',),
    ('link',),
    ('no activity light: Claude hooks not working',),
    ('no systemd here; not something this can check',),
    ('no weekly countdown',),
    ('none',),
    ('not available for WSL yet',),
    ('not found -- removed or reinstalled?',),
    ('not installed',),
    ('not running',),
    ('not set up (turn on with: blink config usage-refresh on)',),
    ('off',),
    ('off: /usage kept failing; run claude and /login, then turn it on again',),
    ("off: Claude Code's /usage used the model",),
    ('on',),
    ('on, but ', None, ' failed; run claude and /login if it keeps failing'),
    ("on, but Claude Code wasn't found",),
    ('on, but this Claude Code has no /usage yet; update Claude Code',),
    ('on, last ran ', None, ' min ago'),
    ('open Device Manager to see what it says about that device',),
    ('pid',),
    ('port',),
    ('print',),
    ('registered as a Scheduled Task',),
    ('registered but FAILING -- exits with code ', None, ', ', None, ' attempts so far. See ', None),
    ('registered to start when you log in',),
    ('registered with launchd',),
    ('run `', None, ' driver` to install it'),
    ('running',),
    ('running (launchd, pid ', None, ')'),
    ('runs',),
    ('schtasks',),
    ('stopped',),
    ('systemctl',),
    ('the activity hook shim is missing, so the hooks run nothing',),
    ('the activity hook shim is out of date',),
    ('the last ', None, ' runs'),
    ('the last run',),
    ('the status line shim is out of date',),
    ('unchecked',),
    ('unknown on ', None),
    ('unreadable',),
    ('usb',),
    ('usually means nobody was here, not that anything is broken.',),
    ('wifi',),
    ('win32',),
    ('wire',),
)

# platform.system() and platform.machine() answer from short fixed lists;
# anything else is written as "unknown".
SYSTEMS = frozenset({"Darwin", "Linux", "Windows"})
MACHINES = frozenset({"arm64", "x86_64", "AMD64", "ARM64", "aarch64",
                      "i386", "i686", "x86", "armv7l"})

_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ")
_TAG = re.compile(r"^\[([a-z_-]+)\](?=\s|$)")
# A dotted version, three or four parts: app, firmware, Claude Code, Python.
# Two parts are not allowed -- "1.50" is as likely to be a cost.
_VERSION = re.compile(r"^v?\d{1,4}(?:\.\d{1,4}){2,3}$")
_TOKEN = re.compile(r"[A-Za-z0-9]+|\s|[^A-Za-z0-9\s]")
_ALNUM = re.compile(r"[A-Za-z0-9]")


def is_version(text: str) -> bool:
    """A three- or four-part version number and nothing else."""
    return bool(_VERSION.match(text))


def tokens(text: str):
    """Runs of letters and digits, and every other character alone."""
    return _TOKEN.findall(text)


_HEAD = 4
_INDEX = None


def _index():
    """TEMPLATES by the first characters of their first piece, so a line is
    not tried against every one of them."""
    global _INDEX
    if _INDEX is None:
        idx = {}
        for t in TEMPLATES:
            idx.setdefault(t[0][:_HEAD], []).append(t)
        _INDEX = idx
    return _INDEX


def _candidates(text, p):
    idx = _index()
    for k in range(min(_HEAD, len(text) - p), 0, -1):
        key = text[p:p + k]
        for t in idx.get(key, ()):
            if k == _HEAD or len(t[0]) == k:
                yield t


def _match(text, p, tpl):
    """`tpl` laid over `text` from `p`: its pieces in order, each found
    after the one before, and the holes between them. Returns (segments,
    end, open, whole): segments as (start, end, ours); `open` when the line
    is used up by a hole (one at the template's end, or a piece that is not
    there), so no other template may follow; `whole` when every piece was
    found. None if the first piece is not at `p`."""
    head = tpl[0]
    if not text.startswith(head, p):
        return None
    segs = [(p, p + len(head), True)]
    q = p + len(head)
    i = 1
    while i < len(tpl):
        if i + 1 >= len(tpl):
            segs.append((q, len(text), False))
            return segs, len(text), True, True
        piece = tpl[i + 1]
        at = text.find(piece, q)
        if at < 0:
            segs.append((q, len(text), False))
            return segs, len(text), True, False
        segs.append((q, at, False))
        segs.append((at, at + len(piece), True))
        q = at + len(piece)
        i += 2
    return segs, q, False, True


def _match_greedy(text, p, tpl):
    """`tpl` laid like _match, but every hole as long as it can be: each
    piece at its last place that still leaves room for the pieces after
    it. Where a value holds one of our pieces, _match ends the hole at the
    first one and this at the last, so a piece is called ours only where
    both agree (_readings). Review (2026-11-25) found a WSL distribution
    named "x), not running": `f"Claude Code WSL ({wsl_name}), "` read up
    to the first "), " and called "not running", the name's own text, the
    state that our code printed."""
    m = _match(text, p, tpl)
    if m is None or not m[3]:
        return m
    lazy_at = [s for s, e, ours in m[0][1:] if ours]
    pieces = [tpl[i] for i in range(2, len(tpl), 2)]
    at = [0] * len(pieces)
    limit = len(text)
    for i in reversed(range(len(pieces))):
        at[i] = text.rfind(pieces[i], lazy_at[i], limit)
        limit = at[i]
    segs = [(p, p + len(tpl[0]), True)]
    q = p + len(tpl[0])
    for piece, a in zip(pieces, at):
        segs.append((q, a, False))
        segs.append((a, a + len(piece), True))
        q = a + len(piece)
    if len(tpl) % 2 == 0:
        segs.append((q, len(text), False))
        return segs, len(text), True, True
    return segs, q, False, True


def _matches(text, p, tpl):
    """The lazy and the greedy laying of `tpl` from `p`, once each when
    they are the same; nothing when the head is not there."""
    lazy = _match(text, p, tpl)
    if lazy is None:
        return []
    greedy = _match_greedy(text, p, tpl)
    return [lazy] if greedy == lazy else [lazy, greedy]


# The `+` joins whose right-hand side is our own text, so a second template
# may follow the first. All five are in pc/cli.py cmd_status (read
# 2026-11-25): `"Bridge      " + backend().status()`, whose strings are the
# service backends' literals; `"Status line " + ("installed at ..." or "not
# installed")`; `"Usage refresh " + _refresh_status(...)`, whose returns are
# literals; and `"Claude Code not found -- running on Codex alone" + why`,
# where `why` is one of three literals; and `f"Claude Code WSL ({wsl_name}), "
# + {...}[wsl_state]`, a dict of four literals. The second template's holes are
# masked as anywhere else. Any other template that ends on its constant
# text is followed by a mask, whatever comes after it. Round 6 of this change
# let any such template be followed by another, and review found
# `"[bridge] logo: " + (... "FAILED: " + res[1])` in pc/bridge.py: a legacy
# line "[bridge] logo: not running" kept its whole value, because the value
# was itself one of our templates. One join, never two, so nothing here
# recurses: a line of thousands of "running" pieces is one template and a
# mask (round 6's recursive search hit Python's recursion limit on it).
JOINS = frozenset({
    ("Bridge      ",),
    ("Status line ",),
    ("Usage refresh ",),
    ("Claude Code not found -- running on Codex alone",),
    ("Claude Code WSL (", None, "), "),
})


def _best(text, p):
    """The single template that covers most of `text` from `p`: (width,
    segments, tpl, end, open), or None when none starts there."""
    result = None
    for tpl in _candidates(text, p):
        m = _match(text, p, tpl)
        if m is None:
            continue
        segs, q, is_open, _ = m
        width = sum(e - s for s, e, ours in segs if ours)
        if result is None or width > result[0]:
            result = (width, segs, tpl, q, is_open)
    return result


def _cover(text, appended=False):
    """The segments of `text` that are ours, laid from its first character.
    What no template reaches is one segment that is not ours."""
    if not text:
        return []
    return _cover_at(text, 0, True, appended)


def _cover_at(text, p, join_ok, appended=False):
    """The segments of `text[p:]` that are ours.

    Every template whose pieces are all there, laid from `p`, and that
    accounts for the line to its end (its last hole runs to the end, it
    ends where the line does, or it is one of JOINS and the rest is
    covered once more) is a reading of the line. A character is ours only
    where every reading agrees that it is.

    With `appended` (bridge.log), a template that ends on its constant
    text before the line does is a reading too, of our text with a value
    put after it, so the rest is masked. A log may be older than this
    code, and an old line is anything at all after a tag of ours. Round 7
    kept the one reading that covered the most, and review found the
    constant `'[usage] session'` (a board line that legacy logs carry with
    its figures after it) beside the longer `'[usage] session applied'`: a
    legacy line whose value was "applied" kept it, because the longer
    template called it ours. The shorter reading now says that "applied"
    may be a value. The cost is a few words of ours masked where one log
    template is the start of another, the safe side. `blink status` is
    printed by this very code, which puts text after a constant only at
    JOINS, so its rows are read without `appended` (with it, "running
    (launchd, pid 7)" lost everything after "running").

    Where there is no reading, the template that covers the most from `p`
    is laid and the rest is masked, as before. After a join only one more
    template is laid, never a chain, so this recurses once at most."""
    readings = []
    for tpl in _candidates(text, p):
        for segs, q, is_open, whole in _matches(text, p, tpl):
            # A bare tag and one value reads every line under its tag. In
            # `blink status` (printed by this code) it is left out, or
            # every row would be one value; in bridge.log it votes, so the
            # rest of a line under such a tag is masked (see _generic).
            if not whole or (_generic(tpl) and not appended):
                continue
            if is_open or q >= len(text):
                readings.append(segs)
            elif join_ok and tpl in JOINS:
                readings.append(segs + _cover_at(text, q, False, appended))
            elif appended:
                readings.append(segs + [(q, len(text), False)])
    if readings:
        return _agreed(readings, p, len(text))
    best = _best(text, p)
    if best is None:
        return [(p, len(text), False)]
    tpl = best[2]
    laid = []
    for segs, q, is_open, _ in _matches(text, p, tpl):
        if is_open or q >= len(text):
            laid.append(segs)
        elif join_ok and tpl in JOINS:
            laid.append(segs + _cover_at(text, q, False, appended))
        else:
            laid.append(segs + [(q, len(text), False)])
    return _agreed(laid, p, len(text))


def _generic(tpl):
    """A template that is a tag or a row label and then at most one value:
    `"[bridge] " + msg`, `f"Board       {state}"`, `"[wifi] "`. As a
    reading it says that every line under that tag may be one value.

    In bridge.log that reading counts. Round 8 left it out of the vote, so
    a line under [bridge] that read as one of our longer lines kept its
    text, and review (2026-11-25) pointed out that `f"[bridge] {err}"` (in
    claude_usage_bridge.py) and the logs from before 2026-10-30 can put a
    value there that is word for word one of those lines: "[bridge] board
    found at" was a private value shown whole. Nothing in the line can
    tell the two apart, so every line under a tag that has one of these
    (bridge, update, watchdog, wifi, cfg, status, plan) keeps its time and
    its tag and nothing else. The other tags (ota, usage, proto, ingest,
    theme...) are the board's and the app's fixed lines, and keep theirs.

    In `blink status` it is left out: its rows are printed by this very
    code, which fills a label's one value only where the label's template
    says so, and the vote would mask every row whole. That is the residual
    for status.txt: a row printed as a label and one value whose value is
    itself one of our longer rows under the same label shows that value."""
    if len(tpl) > 2:
        return False
    head = tpl[0]
    t = _TAG.match(head)
    if t:
        head = head[t.end():]
    else:
        label = next((l for l in STATUS_LABELS if head.startswith(l)), None)
        if label is not None:
            head = head[len(label):]
    return not _ALNUM.search(head)


def _agreed(readings, start, end):
    """The characters from `start` to `end` that every one of `readings`
    calls ours, as segments; the rest as runs that are not ours."""
    ours = [True] * (end - start)
    for segs in readings:
        mine = [False] * (end - start)
        for s, e, is_ours in segs:
            if is_ours:
                mine[s - start:e - start] = [True] * (e - s)
        ours = [a and b for a, b in zip(ours, mine)]
    out = []
    for i, flag in enumerate(ours):
        if out and out[-1][2] == flag:
            out[-1] = (out[-1][0], start + i + 1, flag)
        else:
            out.append((start + i, start + i + 1, flag))
    return out


def _filled(run: str, version_ok: bool):
    """A stretch that is not ours: what our code filled in. Its spacing is
    kept; anything else in it is masked, but for one whole version where
    `version_ok`. Returns (text, whether a version was kept)."""
    lead = run[:len(run) - len(run.lstrip())]
    core = run.strip()
    trail = run[len(lead) + len(core):]
    # Only spacing is shown as it is. Round 7 kept a run with no ASCII
    # letter or digit in it, and review found that `[wifi] joined 東京`
    # kept the network's name whole; round 8 still kept bare punctuation,
    # and review (2026-11-25) found `[wifi] joined !!!`, a network named
    # "!!!", shown whole. A filled-in value of punctuation is a value.
    if not core:
        return run, False
    if version_ok:
        first, _, rest = core.partition(" ")
        if is_version(first):
            tail = ""
            if rest:
                tail = " " + _filled(rest, False)[0]
            return lead + first + tail + trail, True
    if "/" in core or "\\" in core or core.startswith("~"):
        return lead + PATH_MASK + trail, False
    return lead + MASK + trail, False


def scrub_text(text: str, private=frozenset(), version_after=None,
               keep=0, appended=False) -> str:
    """`text` with everything our code did not write itself masked.

    What TEMPLATES lays over the line from its first character is kept
    (`appended` as in _cover_at: on for bridge.log, whose lines may be
    older than this code); a
    word in it that is in `private` (the account's name, the computer's) is
    masked, but for the first `keep` characters (a row's label, a log
    line's tag). When no template starts the line, the first `keep`
    characters are kept and the rest is masked. Every hole is masked whole,
    except one version straight after the word `version_after` (case
    aside). Spacing is kept, so the columns of `blink status` still line
    up."""
    segs = _cover(text, appended)
    if keep and not (segs and segs[0][2]):
        segs = [(0, keep, True), (keep, len(text), False)]
    out, last, spent = [], None, version_after is None
    for s, e, ours in segs:
        run = text[s:e]
        if not run:
            continue
        if ours:
            pos = s
            for t in tokens(run):
                if pos >= keep and t.lower() in private:
                    out.append(MASK)
                else:
                    out.append(t)
                if _ALNUM.match(t):
                    last = t.lower()
                pos += len(t)
            continue
        shown, kept = _filled(run, not spent and last == version_after)
        spent = spent or kept
        out.append(shown)
    return "".join(out)


def system_field(label: str, value: str, private=frozenset(),
                 version=False, allowed=None) -> str:
    """One line of system.txt: the label, padded, and the value. A
    `version` field keeps its value only when the whole of it is a version,
    an `allowed` field only when it is one of those; anything else is
    "unknown" for them, and goes through scrub_text for the rest."""
    value = str(value)
    if version:
        shown = value if is_version(value) else "unknown"
    elif allowed is not None:
        shown = value if value in allowed else "unknown"
    else:
        shown = scrub_text(value, private)
    return f"{label:<12}{shown}"


# The rows `blink status` prints (pc/cli.py cmd_status and the helpers it
# calls), by the label in their first twelve columns. A row under any other
# label, and the Wire row above all (it is the usage message itself), is
# left out.
STATUS_LABELS = (
    "Bridge", "Link", "App", "Board", "Claude Code", "Status line",
    "Activity", "Usage data", "Usage refresh", "Session", "Desktop",
    "Codex hook", "Codex",
)
# The rows whose continuation lines (twelve spaces) are kept, because these
# are the rows that print one (pc/cli.py, counted 2026-11-24): where the
# board was last seen, the other serial devices, where Desktop's and Codex's
# files were looked for, the Codex hook's trust prompt. A continuation line
# under any other row is dropped, and none of them keeps a version.
CONTINUED = frozenset({"Board", "Claude Code", "Desktop", "Codex hook",
                       "Codex"})
# Where a status row may hold a version: the word before it, on the row's
# first line only. "App         1.5.1", "Claude Code 2.1.318". The board's
# firmware version is in system.txt instead, from board.json.
VERSION_AT = {"App": "app", "Claude Code": "code"}


def status_rows(text: str, private=frozenset()):
    """`blink status` output cut to the rows support reads, each under
    scrub_text. Returns (text, dropped)."""
    kept, dropped, keeping = [], 0, False
    for line in text.splitlines():
        if not line.strip():
            continue
        if "{" in line or "}" in line:
            dropped += 1
            keeping = False
            continue
        if line.startswith(" " * 12):
            if keeping:
                kept.append(scrub_text(line, private))
            else:
                dropped += 1
            continue
        label = next((l for l in STATUS_LABELS
                      if line == l or line.startswith(l + " ")), None)
        if label is None:
            dropped += 1
            keeping = False
            continue
        keeping = label in CONTINUED
        kept.append(scrub_text(line, private, VERSION_AT.get(label),
                               keep=len(label)))
    return "".join(l + "\n" for l in kept), dropped


def redact_log_line(line: str, private=frozenset()):
    """One line of bridge.log as the bundle may hold it, or None to drop it.

    Kept: an optional logbook timestamp, then one of LOG_TAGS, then the
    line under scrub_text with no filled-in value kept, versions included;
    or the constant BOARD_LINE. Dropped: everything else, which is a line
    that is not ours to vouch for -- untagged output, a raw message, a
    traceback, a line glued together from two writers.
    """
    text = line.rstrip("\r\n")
    stamp = ""
    m = _STAMP.match(text)
    if m:
        stamp, text = m.group(1) + " ", text[m.end():]
    if text == BOARD_LINE:
        return stamp + text
    t = _TAG.match(text)
    if not t or t.group(1) not in LOG_TAGS:
        return None
    # A tag with a message glued behind it ("[bridge] <- {'t': ...") is a
    # legacy dump: the shape of the message itself is not ours to send.
    if "{" in text or "}" in text:
        return None
    # "[bridge] -> usage" (a message's direction and its type, from
    # protocol.log_name) is masked like any other line under [bridge].
    # Round 9 kept it whole when the type was one of protocol.WIRE_TYPES,
    # and review (2026-11-25) pointed out that `f"[bridge] {err}"` prints
    # the same line when an error's private text is "-> usage": nothing in
    # the line tells the two apart. The timestamps still show the traffic.
    return stamp + scrub_text(text, private, keep=t.end(), appended=True)


_ARROW_TYPE = re.compile(r"^ (->|<-) ([a-z_]+|\(unknown type\))$")


def _read_tail(path, budget):
    """The last `budget` bytes of `path`, from a line boundary, as text."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > budget:
                f.seek(size - budget)
                data = f.read()
                cut = data.find(b"\n")
                data = data[cut + 1:] if cut >= 0 else b""
            else:
                data = f.read()
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def log_tail(blink_dir, private=frozenset(), cap_bytes=LOG_CAP_BYTES):
    """The redacted tail of bridge.log and the generation before it.

    Returns (lines, dropped, found): the kept lines oldest first, how many
    lines were dropped, and whether any log file was there at all. The
    newest file gets the budget first; the older one has what is left.
    """
    texts = []
    budget = cap_bytes
    found = False
    for name in reversed(LOG_FILES):
        if budget <= 0:
            break
        text = _read_tail(os.path.join(blink_dir, name), budget)
        if text is None:
            continue
        found = True
        texts.append(text)
        budget -= len(text.encode("utf-8", errors="replace"))
    kept, dropped = [], 0
    for text in reversed(texts):
        for line in text.splitlines():
            if not line.strip():
                continue
            out = redact_log_line(line, private)
            if out is None:
                dropped += 1
            else:
                kept.append(out)
    return kept, dropped, found


def private_words(home):
    """Words that name this person or this machine: the account name, the
    home folder's name, the computer's name and its parts. Lower-cased,
    three letters or more (shorter would mask ordinary words).

    None is let off for being common. Round 4 let off "desktop", "pro",
    "home" and a few more so a Mac called "MacBook-Pro" kept them, and the
    review (2026-11-24) pointed out that an account called "desktop" then
    went through whole. A Windows computer named "DESKTOP-7Q2K" now masks
    the word "desktop" in text our code wrote; the row labels and log tags
    are kept whatever this holds (scrub_text's `keep`)."""
    names = [os.path.basename(os.path.normpath(home))]
    try:
        names.append(getpass.getuser())
    except Exception:
        pass
    node = platform.node()
    names.append(node)
    words = set()
    for n in names:
        words.update(w for w in re.split(r"[^a-z0-9]+", n.lower())
                     if len(w) >= 3)
    # "name.local" and "name.lan" are the network's suffix, not a name.
    return frozenset(words - {"local", "lan"})


def _inside(path, home):
    home = os.path.normpath(home)
    p = os.path.normpath(path)
    return p == home or p.startswith(home + os.sep)


def _windows_desktop():
    """The Desktop folder Explorer shows, from the registry. With OneDrive
    backing up the Desktop it is ~\\OneDrive\\Desktop (or "OneDrive -
    <company>"), not ~\\Desktop, and a zip put in ~\\Desktop then is in a
    folder the owner never opens (owner's review, 2026-11-24)."""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\User Shell Folders") as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
        return os.path.expandvars(value)
    except Exception:
        return None


def _xdg_desktop(home):
    """XDG_DESKTOP_DIR from ~/.config/user-dirs.dirs: a Linux desktop in
    another language names the folder for it ("Schreibtisch", "Bureau")."""
    try:
        with open(os.path.join(home, ".config", "user-dirs.dirs"),
                  encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r'\s*XDG_DESKTOP_DIR\s*=\s*"([^"]*)"', line)
                if m:
                    value = m.group(1)
                    if value.startswith("$HOME"):
                        value = home + value[len("$HOME"):]
                    return value
    except OSError:
        pass
    return None


def desktop_dir(home):
    """The owner's Desktop folder, or None if there is none to be found.

    Only a folder inside `home` is taken. The registry and user-dirs.dirs
    answer for the real account whatever HOME says, and a Desktop outside
    the home being looked at is not one this run was asked about (under
    the test sandbox it would be the real desk's Desktop)."""
    candidates = []
    if sys.platform == "win32":
        candidates.append(_windows_desktop())
    elif not sys.platform.startswith("darwin"):
        candidates.append(_xdg_desktop(home))
    candidates.append(os.path.join(home, "Desktop"))
    if sys.platform == "win32":
        try:
            names = sorted(n for n in os.listdir(home)
                           if n.startswith("OneDrive"))
        except OSError:
            names = []
        candidates += [os.path.join(home, n, "Desktop") for n in names]
    for c in candidates:
        if c and _inside(c, home) and os.path.isdir(c):
            return c
    return None


def default_out_dir(home):
    """The Desktop, where an owner will find it; ~/.blink if there is none
    (a server, a desktop whose folder could not be found)."""
    return desktop_dir(home) or os.path.join(home, ".blink")


def show_path(path, home):
    """`path` with the home folder shown as ~, for what the owner reads."""
    home = os.path.normpath(home)
    p = os.path.normpath(path)
    if _inside(p, home):
        return "~" + p[len(home):]
    return p


MANIFEST = """\
Blink support bundle, made by `blink support`.

  status.txt   the rows of `blink status` support reads
  system.txt   the app's and the board's versions, the operating system
               and the service state
  bridge.log   the newest part of the app's log

Every line was cut down before it was written here: the text the app
writes itself is kept, and anything it filled in -- a name, a folder, a
number, an id -- is shown as {mask} or {path}, but for two version
numbers in status.txt. Log lines and status rows that are not the app's
own are left out ({dropped} of them this time).
This is a filter, not a proof: read the files before you send them.
"""


def build_bundle(out_dir, status_text, system_text, log_lines, dropped=0,
                 today=None):
    """Write the zip and return its path.

    Named for the day, with -2, -3 ... after it rather than over an earlier
    one: an owner who ran it twice may already have sent the first, or be
    looking at it. Created with O_EXCL so a race cannot overwrite either.
    """
    os.makedirs(out_dir, exist_ok=True)
    day = today or time.strftime("%Y-%m-%d")
    n = 1
    while True:
        name = f"blink-support-{day}.zip" if n == 1 else \
            f"blink-support-{day}-{n}.zip"
        path = os.path.join(out_dir, name)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_BINARY", 0), 0o644)
        except FileExistsError:
            n += 1
            continue
        f = os.fdopen(fd, "wb")
        break
    with f, zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("MANIFEST.txt", MANIFEST.format(
            mask=MASK, path=PATH_MASK, dropped=dropped))
        z.writestr("status.txt", status_text)
        z.writestr("system.txt", system_text)
        z.writestr("bridge.log", "".join(l + "\n" for l in log_lines))
    return path
