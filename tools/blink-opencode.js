// blink-opencode-plugin: written by Blink's installer; `blink uninstall` removes it.
//
// Blink's execution-state plugin for OpenCode.
//
// The line above is the OWNERSHIP MARKER, and it has to stay the first line of
// this file, byte for byte. pc/install_opencode_plugin.py refuses to overwrite,
// refresh or delete a <config>/opencode/plugin/blink.js whose first line is
// anything else: that directory is OpenCode's, it is documented as the place
// people put their own plugins, and a file called blink.js that somebody else
// wrote is theirs.
//
// WHAT THIS IS. tools/blink-hook.sh, translated. Claude Code and Codex both run
// a shell command on each lifecycle event, and that shim writes one small file
// per session into ~/.blink/state (or state-codex) which
// pc/providers/claude_state.py reads. OpenCode has no shell-hook config at all
// (measured on 1.19.3: no `hook` key anywhere in its config schema), so the
// only in-process hook it offers is a plugin -- and its SQLite database cannot
// stand in for one, because session status (busy / idle / retry), permission
// prompts and pending retries are held in memory and never reach the DB. A
// permission prompt looks exactly like a running tool there, and a session
// killed with -9 stays "running" in it forever. See docs/plans/opencode.md,
// "What the DB cannot say".
//
// So this writes EXACTLY the files the shim writes, in exactly its format, into
// ~/.blink/state-opencode, and claude_state reads OpenCode unchanged -- its
// state machine, the waiting marker, the tombstone, pid liveness and the
// abandonment sweep -- the same way it already reads Codex:
//
//   <sid>.state     {"event":"<Name>","t":<epoch s>,"pid":<pid>,"name":"<dir>"}
//   <sid>.waiting   present while a person is being asked something
//   <sid>.ended     left where the slot was when the session ends
//   <sid>/<child>   one empty file per live subagent session
//
// THE MAPPING (docs/plans/opencode.md, the table under "Live state"):
//
//   session.status busy             -> UserPromptSubmit          running
//   tool.execute.before / .after    -> PreToolUse (clock only)   running
//   permission.asked, question.asked-> PermissionRequest + .waiting  waiting
//   permission.replied,
//   question.replied / .rejected    -> PostToolUse, .waiting removed once no
//                                      request is left open      running
//                                      (only for a request still open in a
//                                      turn still running; see onAnswered)
//   session.status idle             -> Stop                      your turn
//   session.status retry, next>60s  -> StopFailure               failed
//   session.status retry, sooner    -> PreToolUse                running
//   session.error MessageAbortedError -> Interrupt               your turn
//   session.error anything else     -> StopFailure               failed
//   session.deleted                 -> SessionEnd                gone
//   a session with info.parentID    -> SubagentStart/Stop under its parent
//
// Three things the table does not say, each because the measured event stream
// disagreed with a naive reading of it:
//
//   1. AN ERROR IS FOLLOWED BY IDLE. Every captured failure -- the 401, and the
//      quota run that exhausted its five retries -- went session.error, then
//      session.status idle, then idle AGAIN 35-75 ms later. Mapped literally,
//      the Stop overwrites the StopFailure within a tenth of a second and the
//      one state this product exists to show is never on disk long enough for
//      a two-second poll to see it. So an error latches the turn: the idles
//      that follow it change nothing, and only the next busy -- a new turn, or
//      a retry firing -- clears it. This is also what the shim sees from
//      Claude Code, where StopFailure is fired INSTEAD of Stop, not before it.
//
//   2. BUSY IS NOT A NEW PROMPT. OpenCode re-announces busy on every model step
//      (three times in a plain one-reply run), so it does not mean what the
//      shim's UserPromptSubmit means -- "the person typed something" -- and it
//      must not clear the waiting marker the way that event does in the shim.
//      The marker here is driven by the open requests themselves; busy only
//      ever clears it when none is left, which is the same thing said safely.
//
//   3. THE PROCESS EXITING IS THE SESSION ENDING. `opencode run` and the TUI
//      exit without deleting the session -- it stays in the DB to be resumed --
//      so session.deleted almost never fires in real use. Claude Code fires
//      SessionEnd exactly then, when the terminal closes, and this does the
//      same from process 'exit'. Without it every finished `opencode run`
//      left a slot well under a second old whose pid had just died (measured:
//      the Stop and the exit 50-60 ms apart) -- a run that short may never
//      have been seen alive by a two-second poll, so the pid check alone
//      would have left it on the panel for the hour.
//
//      Measured on 1.19.3, 'exit' fires for `opencode run` and for nothing
//      else that ends a process: not when the TUI quits on ctrl-c (its slot
//      stayed, so the plugin evidently runs where 'exit' is never delivered),
//      and not for `opencode serve` on SIGTERM. Those keep their last slot,
//      and the pid check drops them on the next poll: OpenCode's directory
//      trusts a pid the daemon has once seen alive, whatever the slot's age
//      (claude_state.SeenAlivePids). It used to share Claude's process-wide
//      suspension, and a ctrl-c within FRESH_SLOT_S of the last event --
//      one second after the answer, measured with the real TUI, 2026-10-19
//      -- switched pid liveness off for the whole daemon and held the closed
//      session on "your turn" for an hour. A handler for the signals
//      themselves is NOT the fix: adding a SIGTERM or SIGINT listener
//      replaces the runtime's default of exiting, so it would change how
//      OpenCode dies.
//
// PRIVACY. The shim's rules, kept:
//   - What is written: an event name, a session id, a child session id, the
//     project directory's LAST SEGMENT, a clock reading and OpenCode's pid.
//     Nothing else is read out of any event: not the prompt, not a tool's
//     arguments or output, not a permission's patterns or command, not a
//     question's text or answers, not an error's message or response body.
//     The tool hooks are handed `output` (the arguments); it is never touched.
//   - Session ids pass the shim's own pattern, alphanumeric first character
//     and at most 64 characters, before they become a filename.
//   - The directory name passes the shim's 24-byte pattern, or is omitted.
//   - Files are 0600 in a 0700 directory.
//   - Writes go to a temp file named with the pid and are renamed into place.
//
// THE DIRECTORY IS THE INSTALLER'S. `blink install` creates
// ~/.blink/state-opencode (0700) when it installs this file, and this file
// writes into it only while it exists. It never creates ~/.blink or the state
// directory, and when either is missing every write is skipped, silently.
// Found in review, 2026-10-19: this used to mkdir -p the directory on every
// write, and `blink uninstall` cannot reach an OpenCode that is already
// running -- a TUI left open across the uninstall, or a `serve` that runs
// for days, keeps this plugin loaded in memory after its file is deleted. Its
// next busy put ~/.blink/state-opencode straight back, slots and all, on a
// desk where nothing would ever read or sweep it again, and on a machine
// where ~/.blink itself had gone it recreated that too. The subdirectory a
// session's agents live in is still made here, but only inside a state
// directory that already exists, and without `recursive`.
//
// SILENCE. OpenCode's TUI owns the terminal, so a stray byte on stdout or
// stderr lands in the middle of somebody's session -- the same reason the shim
// redirects every command it runs. Nothing here prints, and every entry point
// is wrapped so that nothing here can throw into OpenCode either: Blink having
// a bad day must not become the user's bad day.
//
// No dependency but node: builtins. OpenCode loads a plain .js file from its
// plugin directory with no npm install (measured on 1.19.3, under Bun), and a
// plugin that needed one would hand our install to OpenCode's background npm
// run in the config directory -- network, a registry, a lockfile -- for the
// sake of three file writes.
//
// EXACTLY TWO EXPORTS: the 1.x factory by name, and the default object both
// lines read (see the end of the file). OpenCode releases that fall back to
// calling every export load the named one, so a helper exported for a test
// would be called with the plugin input and its return value taken as a set
// of hooks. The tests reach everything through these two.

import * as crypto from "node:crypto"
import * as fs from "node:fs"
import * as os from "node:os"
import * as path from "node:path"
import { fileURLToPath } from "node:url"

// The shim's session-id pattern, verbatim: tools/blink-hook.sh, _ident. The
// first character is alphanumeric because `.` and `..` are otherwise valid
// matches, and "$DIR/.." is ~/.blink itself -- reproduced against the signing
// key before the shim was tightened. OpenCode's own ids are "ses_" plus 26
// base62 characters (packages/schema/src/session-id.ts), well inside this.
const ID_RE = /^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$/

// The shim's project-name pattern: a first character plus 23, the daemon's cap.
// It excludes `"` and `\`, which matters there because the value is pasted into
// JSON by hand; here JSON.stringify would escape them, but a name the shim
// would refuse is a name the daemon has never had to render, and the two
// writers must not disagree about what reaches the panel.
const NAME_RE = /^[0-9A-Za-z][0-9A-Za-z._-]{0,23}$/

// A retry scheduled further out than this is the quota case, and is painted
// as failed; a sooner one is OpenCode's "high concurrency" blip, which cleared
// in one second in every captured run, and painting that red would cry wolf.
const LONG_RETRY_MS = 60 * 1000

// A running session's slot is rewritten at most this often when nothing but
// the clock has changed. The timestamp matters -- a slot that stops changing
// for ABANDONED_AFTER_S (an hour) drops out of the census, and a long tool
// call must not -- but OpenCode announces busy on every model step and the
// tool hooks fire on every call, so writing on each would be a rename per
// event for a clock that only needs to be fresh to the nearest hour.
const REFRESH_MS = 5 * 1000

// How long a finished OpenCode 2.0 session stays on the panel with nothing
// happening before the plugin ends it (see scheduleIdleEnd). Long enough to
// read a reply and type the next prompt. BLINK_OPENCODE_IDLE_END_MS shortens
// it for the test driver, which cannot wait a quarter of an hour; nothing in
// a customer's environment sets it.
const IDLE_END_MS = (() => {
  const v = Number(process.env.BLINK_OPENCODE_IDLE_END_MS)
  return Number.isInteger(v) && v >= 1 ? v : 15 * 60 * 1000
})()

// The events claude_state files as running. Moving between them is only a
// clock refresh as far as the panel is concerned, so it is throttled like one.
const RUNNING = new Set(["UserPromptSubmit", "PreToolUse", "PostToolUse"])

// A turn that has finished. Nothing but a new busy may move a session out of
// these: a tool hook that arrives after an abort (the aborted tool's
// tool.execute.after) must not repaint a stopped session as working.
const FINISHED = new Set(["Stop", "Interrupt", "StopFailure"])

// Bounds on what this process remembers. `opencode serve` is long-lived and
// sees every session anyone opens through it; sessions are forgotten when they
// end, but one that is simply never touched again would otherwise be held for
// the life of the server. Far above any real desk, and evicting the oldest
// costs nothing worse than a session being treated as new on its next event.
const MAX_SESSIONS = 512
const MAX_REQUESTS = 256

function stateDir() {
  // os.homedir() and not process.env.HOME: on Windows it is USERPROFILE, which
  // is what the daemon's os.path.expanduser("~/.blink/state-opencode") means
  // too. Resolved per write rather than once at load, for the same reason
  // pc/ never computes a home path at module scope -- the tests move HOME.
  return path.join(os.homedir(), ".blink", "state-opencode")
}

function validId(v) {
  return typeof v === "string" && ID_RE.test(v) ? v : null
}

// The final path segment, and only that: the path above it is split off and
// dropped here, so the full path is never stored or written. Both separators,
// because OpenCode normalises Windows paths to forward slashes in its DB
// (database/path.ts) but nothing promises the event carries the same form.
//
// A separator is required, as the shim's `[^"]*[/\]` requires one: a bare
// "Proj" is not a path this could have come from. A trailing separator leaves
// an empty final segment and therefore no name, again as the shim does.
function projectName(dir) {
  if (typeof dir !== "string") return null
  const cut = Math.max(dir.lastIndexOf("/"), dir.lastIndexOf("\\"))
  if (cut < 0) return null
  const seg = dir.slice(cut + 1)
  return NAME_RE.test(seg) ? seg : null
}

// Each of these swallows its own failure. A state directory that has turned
// unwritable costs us the light, never the person their session.
function quietly(fn) {
  try {
    fn()
  } catch {
    // Deliberately empty. See SILENCE above.
  }
}

// The state directory when it exists, else null -- and null means write
// nothing. See THE DIRECTORY IS THE INSTALLER'S above: an absent directory is
// what an uninstall leaves, and an OpenCode still running from before it must
// not undo that. Checked on every write rather than once at load, because the
// uninstall happens while this process lives. A directory removed between
// this check and the write makes the write fail, and quietly() takes it.
function presentStateDir() {
  const dir = stateDir()
  try {
    return fs.statSync(dir).isDirectory() ? dir : null
  } catch {
    return null
  }
}

// One level, inside a state directory that exists: a session's agent
// directory. Not recursive, so a state directory that vanished cannot be
// recreated through it. 0700 because these names say which sessions are
// open; mkdir's mode applies only to what it creates, so an existing
// directory keeps the mode it has.
function ensureSubdir(p) {
  try {
    fs.mkdirSync(p, { mode: 0o700 })
  } catch (e) {
    if (!e || e.code !== "EEXIST") throw e
  }
}

function touchEmpty(p) {
  fs.writeFileSync(p, "", { mode: 0o600 })
}

function removeFile(p) {
  quietly(() => fs.rmSync(p, { force: true }))
}

// RENAME ON WINDOWS. A rename onto a file another process has open fails
// there -- EPERM, EBUSY or EACCES, depending on how it was opened -- where
// POSIX simply swaps the name. The daemon opens every slot on a two-second
// poll, so on a Windows desk some writes land in that window, and the event
// they carried was dropped without a word: a Stop lost that way leaves the
// panel saying "running" until the next event, which after a finished turn
// may be an hour away (found in review, 2026-10-19). The daemon's read is a
// single small file and is over in well under a millisecond, so a few short
// retries get past it; past those, the write is given up exactly as before.
//
// Synchronous, because every write here is (see the 'exit' listener below),
// and bounded: at most RENAME_TRIES - 1 waits of RENAME_WAIT_MS, 40 ms in
// all, on an event handler OpenCode is waiting for.
const RENAME_TRIES = 5
const RENAME_WAIT_MS = 10
const RENAME_BUSY = new Set(["EPERM", "EBUSY", "EACCES"])

// A synchronous sleep: Atomics.wait on a buffer nobody will ever notify,
// the standard way to block for a few ms without an event-loop turn. If the
// runtime refuses it (some refuse Atomics.wait on the main thread), a short
// spin on the clock does the same job for these few milliseconds.
function sleepSync(ms) {
  try {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms)
  } catch {
    const end = Date.now() + ms
    while (Date.now() < end) {
      // Deliberately empty.
    }
  }
}

function renameRetrying(from, to) {
  for (let attempt = 1; ; attempt++) {
    try {
      fs.renameSync(from, to)
      return
    } catch (e) {
      if (attempt >= RENAME_TRIES || !e || !RENAME_BUSY.has(e.code)) throw e
      sleepSync(RENAME_WAIT_MS)
    }
  }
}

// One 'exit' listener for the whole module, however many times OpenCode calls
// the factory -- it builds one plugin instance per project directory a server
// is asked about, and the tests build dozens. A listener per instance would
// pass Node's default limit of ten, and Node reports that with a warning
// printed to stderr: into the TUI's terminal, from a plugin whose whole
// promise is to print nothing.
//
// Synchronous by necessity: 'exit' runs no further turns of the event loop,
// which is also why everything in this file uses the sync fs calls.
//
// KEYED BY DIRECTORY, newest instance wins. Found in review, 2026-10-19: this
// was a Set that every factory call added a closure to and nothing removed,
// and each closure holds its instance's whole `sessions` and `requests` maps.
// `serve` builds an instance per directory and builds it again whenever
// OpenCode reloads that directory's instance, so a server that ran for weeks
// kept every generation alive -- and at exit the stale ones ran sessionEnd
// over sessions they had last seen days ago, tombstoning ids that a live
// instance (or another OpenCode process entirely) now owned. One ender per
// directory: a new instance for a directory replaces its predecessor, whose
// sessions, if any are still open, are left to the daemon's pid check --
// which ends them only if a poll saw this process alive (SeenAlivePids). A
// process shorter than a poll that also lost its handler here keeps its
// slots for the hour. `opencode run` builds one instance for one directory,
// so in practice that is a `serve`'s problem, and a `serve` is seen alive.
// Bounded as the maps are, oldest directory first.
//
// An instance with no directory gets a key of its own (a fresh Symbol), not
// a shared "": third review, 2026-10-19 -- every such instance landed on the
// one key and each new one evicted the last, so at exit only the newest
// ended its sessions and the rest held theirs on the panel.
//
// ONE PER PROCESS, not per module. OpenCode 2.0 evaluates this file afresh for
// every project location, every reload and every location that comes back
// after its idle eviction (core/src/plugin/supervisor.ts, a supervisor per
// location; config/plugin/source.ts, the file's mtime in the revision). A
// listener per evaluation would reach Node's limit of ten in a service that
// has seen ten projects -- the stderr warning above -- and would pin every
// old evaluation in memory (review, 2026-10-20). So the map and its one
// listener live on globalThis, and a fresh evaluation finds them there.
const MAX_INSTANCES = 64
const ENDERS_KEY = Symbol.for("blink.opencode.enders")
const enders = (() => {
  let m = globalThis[ENDERS_KEY]
  if (!(m instanceof Map)) {
    m = new Map()
    globalThis[ENDERS_KEY] = m
    quietly(() => {
      process.once("exit", () => {
        for (const end of m.values()) quietly(end)
      })
    })
  }
  return m
})()

// Everything one plugin instance knows and writes, for either OpenCode line:
// the 1.x factory below makes one per instance, and 2.0 shares one per
// process (see THE 2.0 SHAPE further down). `fallbackDir` is the name of last
// resort for a session whose own directory has not been seen.
function makeTracker(fallbackDir) {
  // sid -> what this process knows about the session. Insertion-ordered, which
  // is what makes evicting the oldest a matter of taking the first key.
  const sessions = new Map()
  // open request id -> { sid, root }: who was asked, and whose slot shows it.
  // Request ids are never written anywhere; they are keys and nothing more.
  const requests = new Map()
  // sid -> the idle-end timer of a finished 2.0 session. See scheduleIdleEnd.
  const idleEnds = new Map()
  // sid -> the directory of a session this tracker has ended, so that one
  // picked up again keeps its name from its first slot: under 2.0 the event
  // that resumes it, execution.started, carries no location, and the name
  // otherwise came back only with the next step (found by the idle2 test,
  // 2026-10-20). Bounded like the maps.
  const endedDirs = new Map()

  function rec(sid) {
    let r = sessions.get(sid)
    if (!r) {
      if (sessions.size >= MAX_SESSIONS) {
        sessions.delete(sessions.keys().next().value)
      }
      r = {
        parent: null, // the parent session id, for a subagent's session
        dir: endedDirs.get(sid) || null, // info.directory, never written: projectName() takes the tail
        last: null, // the last event written to this session's slot
        lastWrite: 0, // when, in ms
        errored: false, // a session.error latched this turn; see note 1 above
        retryFailed: false, // the StopFailure on the slot came from a long retry
        slot: false, // whether this process has written a slot for it
        agentLive: false, // a subagent's file exists under its root
      }
      sessions.set(sid, r)
    }
    return r
  }

  // The top-level session a subagent belongs to, walking up through nested
  // subagents. Bounded, so a parent cycle in a malformed stream cannot hang.
  function rootOf(sid) {
    let cur = sid
    for (let i = 0; i < 16; i++) {
      const r = sessions.get(cur)
      if (!r || !r.parent) return cur
      cur = r.parent
    }
    return cur
  }

  function isChild(sid) {
    const r = sessions.get(sid)
    return !!(r && r.parent)
  }

  // --- the slot ------------------------------------------------------------

  function writeSlot(sid, event) {
    const r = rec(sid)
    const now = Date.now()
    if (r.last === event && !RUNNING.has(event)) return
    if (RUNNING.has(event) && RUNNING.has(r.last) && now - r.lastWrite < REFRESH_MS) {
      return
    }
    const dir = presentStateDir()
    if (!dir) return
    // Same key order and shape the shim prints: event, t, then pid and name
    // only when they are usable. An absent key already means unknown to the
    // reader, so neither is ever written empty -- `"pid":` with no value would
    // fail to parse and take the whole session off the panel.
    let body = `{"event":${JSON.stringify(event)},"t":${Math.floor(now / 1000)}`
    const pid = process.pid
    if (Number.isInteger(pid) && pid > 0) body += `,"pid":${pid}`
    const name = projectName(r.dir || fallbackDir)
    if (name) body += `,"name":${JSON.stringify(name)}`
    body += "}"
    const slot = path.join(dir, `${sid}.state`)
    // A temp name carrying the pid, then a rename: the daemon may read at any
    // moment and a half-written file parses as malformed. Two OpenCode
    // processes can hold the same session (a TUI and a `serve`), and with one
    // shared temp name the second would truncate the first's half-written file
    // -- the bug the shim's $$ suffix fixed for parallel tool calls.
    const tmp = `${slot}.${pid}.tmp`
    try {
      fs.writeFileSync(tmp, body, { mode: 0o600 })
      // writeFileSync's mode applies only when it creates the file; a temp left
      // by a killed predecessor would keep whatever mode it had.
      quietly(() => fs.chmodSync(tmp, 0o600))
      renameRetrying(tmp, slot)
    } catch (e) {
      removeFile(tmp)
      throw e
    }
    r.last = event
    r.lastWrite = now
    r.slot = true
    // Anything but a turn ending on the person's move takes the session out
    // of its idle countdown (see scheduleIdleEnd); finishTurn starts a fresh
    // one after writing Stop or Interrupt.
    if (event !== "Stop" && event !== "Interrupt") clearIdleEnd(sid)
    // A session that speaks again is not over: OpenCode resumes a session under
    // its own id, and an hour-old tombstone would otherwise go on hiding a
    // session somebody is sitting in front of. As the shim does, on every
    // event that writes a slot.
    removeFile(path.join(dir, `${sid}.ended`))
  }

  // A clock refresh that may not resurrect a finished turn. Used for every
  // running signal that is not a busy: the tool hooks, a short retry, and
  // anything a subagent does on its root's behalf.
  function stillRunning(sid) {
    const r = rec(sid)
    if (FINISHED.has(r.last) && !(r.last === "StopFailure" && r.retryFailed)) return
    r.retryFailed = false
    writeSlot(sid, "PreToolUse")
  }

  // --- waiting -------------------------------------------------------------

  // The marker exists exactly while a request is open for this root. Driven by
  // the requests rather than by event names, so two overlapping prompts --
  // OpenCode runs tool calls in parallel and each may ask -- keep it until the
  // LAST one is answered. The shim cannot do this; it removes the marker on
  // the first answer. It does not need to, because Claude Code asks one thing
  // at a time.
  function syncMarker(root) {
    let open = false
    for (const v of requests.values()) {
      if (v.root === root) {
        open = true
        break
      }
    }
    if (open) {
      const dir = presentStateDir()
      if (dir) touchEmpty(path.join(dir, `${root}.waiting`))
    } else {
      removeFile(path.join(stateDir(), `${root}.waiting`))
    }
  }

  function dropRequests(pred) {
    for (const [id, v] of requests) {
      if (pred(v)) requests.delete(id)
    }
  }

  // --- subagents -----------------------------------------------------------

  function agentStart(child) {
    const r = rec(child)
    if (r.agentLive) return
    const root = rootOf(child)
    if (root === child) return
    const base = presentStateDir()
    if (!base) return
    const dir = path.join(base, root)
    ensureSubdir(dir)
    touchEmpty(path.join(dir, child))
    r.agentLive = true
  }

  function agentStop(child) {
    const r = sessions.get(child)
    const root = rootOf(child)
    if (root !== child) removeFile(path.join(stateDir(), root, child))
    if (r) r.agentLive = false
    // A subagent that has stopped is asking nobody anything.
    dropRequests((v) => v.sid === child)
    if (root !== child) syncMarker(root)
  }

  // --- session lifetime ----------------------------------------------------

  function sessionEnd(sid) {
    clearIdleEnd(sid)
    const ending = sessions.get(sid)
    if (ending && ending.dir) {
      endedDirs.delete(sid)
      if (endedDirs.size >= MAX_SESSIONS) endedDirs.delete(endedDirs.keys().next().value)
      endedDirs.set(sid, ending.dir)
    }
    const dir = stateDir()
    // The whole session, agents and all. The path is built from an id that
    // passed ID_RE, so a traversal cannot reach here.
    quietly(() => fs.rmSync(path.join(dir, sid), { recursive: true, force: true }))
    removeFile(path.join(dir, `${sid}.state`))
    removeFile(path.join(dir, `${sid}.waiting`))
    // The tombstone. Absence cannot say "this session ended", because absence
    // is also what a session nobody recorded looks like; the shim leaves this
    // file for the same reason, and claude_state sweeps it after an hour.
    quietly(() => {
      if (presentStateDir()) touchEmpty(path.join(dir, `${sid}.ended`))
    })
    dropRequests((v) => v.root === sid)
    // Forget its subagents with it. Their files went with the directory above.
    for (const id of [...sessions.keys()]) {
      if (id !== sid && rootOf(id) === sid) sessions.delete(id)
    }
    sessions.delete(sid)
  }

  // --- the events ----------------------------------------------------------

  function onInfo(info, created) {
    if (!info || typeof info !== "object") return
    const sid = validId(info.id)
    if (!sid) return
    const r = rec(sid)
    if (typeof info.directory === "string") r.dir = info.directory
    const parent = validId(info.parentID)
    if (!parent || parent === sid) return
    r.parent = parent
    if (r.slot) {
      // Seen as a top-level session first -- a status that beat its own
      // creation event -- and now known to be a subagent. Take its slot back
      // rather than leave a second session on the panel. No tombstone: it was
      // never a session of its own.
      const dir = stateDir()
      removeFile(path.join(dir, `${sid}.state`))
      removeFile(path.join(dir, `${sid}.waiting`))
      r.slot = false
      r.last = null
    }
    // On creation only. session.updated also fires AFTER a session's final
    // idle (captured on every run), so starting the agent from it would put
    // back the file the idle had just removed.
    if (created) agentStart(sid)
  }

  function onStatus(sid, status) {
    if (!status || typeof status !== "object") return
    const type = status.type
    if (isChild(sid)) {
      const root = rootOf(sid)
      if (type === "busy") {
        agentStart(sid)
        stillRunning(root)
      } else if (type === "retry") {
        agentStart(sid)
        if (isLongRetry(status)) {
          // A subagent waiting out a quota is its whole session waiting out a
          // quota: the parent is blocked on the task tool for as long.
          retryFail(root)
        } else {
          stillRunning(root)
        }
      } else if (type === "idle") {
        agentStop(sid)
      }
      return
    }
    // A status for a session this process never saw created is treated as a
    // top-level session. Subagent sessions are created DURING their parent's
    // turn, by this same process, so their creation event is always seen
    // before their first status; an id with no history is a session resumed
    // after this process started, and those are top-level. Guessing "child"
    // instead would hide a real session from the panel, and if the guess is
    // wrong the other way, onInfo takes the slot back the moment the
    // session's own info names its parent.
    const r = rec(sid)
    if (type === "busy") {
      r.errored = false
      r.retryFailed = false
      writeSlot(sid, "UserPromptSubmit")
      syncMarker(sid)
    } else if (type === "retry") {
      r.errored = false
      if (isLongRetry(status)) retryFail(sid)
      else stillRunning(sid)
    } else if (type === "idle") {
      // A finished turn is asking nobody anything, whatever was left open: the
      // shim removes the marker on Stop for the same reason.
      dropRequests((v) => v.root === sid)
      syncMarker(sid)
      if (!r.errored) writeSlot(sid, "Stop")
    }
  }

  function isLongRetry(status) {
    const next = status.next
    return typeof next === "number" && Number.isFinite(next) && next - Date.now() > LONG_RETRY_MS
  }

  function retryFail(root) {
    const r = rec(root)
    if (r.last === "Stop" || r.last === "Interrupt") return
    writeSlot(root, "StopFailure")
    r.retryFailed = true
  }

  function onError(sid, error) {
    // Only the name is read. The message and the response body carry the
    // provider's text and sometimes the request's, and none of it is ours.
    const name = error && typeof error === "object" ? error.name : null
    if (isChild(sid)) {
      // The subagent's turn is over; whether that fails the parent is the
      // parent's own business, and it says so with its own events.
      agentStop(sid)
      return
    }
    const r = rec(sid)
    r.errored = true
    r.retryFailed = false
    dropRequests((v) => v.root === sid)
    syncMarker(sid)
    writeSlot(sid, name === "MessageAbortedError" ? "Interrupt" : "StopFailure")
  }

  function onAsked(sid, id) {
    const root = rootOf(sid)
    if (typeof id === "string" && id) {
      if (requests.size >= MAX_REQUESTS) requests.delete(requests.keys().next().value)
      requests.set(id, { sid, root })
    }
    // The marker first: it is the witness the reader trusts, and the slot write
    // below is the one that can be lost to a concurrent writer.
    const dir = presentStateDir()
    if (dir) touchEmpty(path.join(dir, `${root}.waiting`))
    writeSlot(root, "PermissionRequest")
  }

  // The marker is always brought into line with what is still open; the slot
  // moves back to running ONLY for an answer to a request this process saw
  // asked and still holds open, in a turn that has not finished.
  //
  // Found in review, 2026-10-19: this wrote PostToolUse for every answer. An
  // answer can arrive after its turn is over -- busy, question.asked, the
  // person presses Esc (MessageAbortedError, then idle), and OpenCode then
  // sends question.rejected for the question the abort tore down; or a
  // permission.replied names a request this process never saw (asked before
  // it loaded, or evicted at MAX_REQUESTS) after the session's Stop. Each
  // repainted a finished session as running, and nothing would move it again
  // until the next busy: the panel said "running" over an idle terminal for
  // up to ABANDONED_AFTER_S, an hour. idle and session.error already drop
  // every open request for the root, so an answer that is no longer in
  // `requests` is exactly an answer to something the turn has finished with.
  function onAnswered(sid, requestID) {
    const known = typeof requestID === "string" ? requests.get(requestID) : undefined
    const root = known ? known.root : rootOf(sid)
    if (known) requests.delete(requestID)
    syncMarker(root)
    const r = sessions.get(root)
    if (!known || !r || FINISHED.has(r.last)) return
    writeSlot(root, "PostToolUse")
  }

  function onDeleted(info) {
    if (!info || typeof info !== "object") return
    const sid = validId(info.id)
    if (!sid) return
    if (isChild(sid)) {
      agentStop(sid)
      sessions.delete(sid)
      return
    }
    sessionEnd(sid)
  }

  function onEvent(event) {
    if (!event || typeof event !== "object") return
    const type = event.type
    const p = event.properties
    if (typeof type !== "string" || !p || typeof p !== "object") return
    switch (type) {
      case "session.created":
      case "session.updated":
        onInfo(p.info, type === "session.created")
        return
      case "session.deleted":
        onDeleted(p.info)
        return
    }
    // Every other event names its session in properties.sessionID. A value
    // that fails the pattern is SKIPPED, where the shim falls back to the
    // literal "unknown". The shim's fallback is one slot per bad payload from
    // one terminal; here one process carries every session `opencode serve`
    // hosts, and folding all of them into a single "unknown" slot is the exact
    // misreport per-session slots were introduced to end -- two sessions
    // overwriting each other while the panel confidently shows one. OpenCode
    // generates these ids itself, so a mismatch means its format changed, and
    // tests/ci/check_opencode_contract.sh is where that should surface, not
    // the panel.
    const sid = validId(p.sessionID)
    if (!sid) return
    switch (type) {
      case "session.status":
        onStatus(sid, p.status)
        return
      case "session.error":
        onError(sid, p.error)
        return
      case "permission.asked":
      case "question.asked":
        onAsked(sid, p.id)
        return
      case "permission.replied":
      case "question.replied":
      case "question.rejected":
        onAnswered(sid, p.requestID)
        return
    }
  }

  function onTool(hookInput) {
    // Only the session id is read from the hook's input; `output` -- the
    // tool's arguments, or its result -- is not even accepted as a parameter.
    if (!hookInput || typeof hookInput !== "object") return
    const sid = validId(hookInput.sessionID)
    if (!sid) return
    stillRunning(rootOf(sid))
  }

  // --- OpenCode 2.0 ----------------------------------------------------------

  // Event ids already handled. Every location's copy of the plugin receives
  // every event (see THE 2.0 SHAPE), and they all feed this one tracker, so
  // the first copy to deliver an event handles it and the rest are dropped
  // here. Only the types below are remembered: the stream also carries every
  // text and reasoning delta, which would push the window through in seconds
  // and let a lagging copy replay an execution.started over a Stop (review,
  // 2026-10-20). Bounded like the maps; at a few events per turn, an id
  // evicted after this many has long since been delivered to every copy.
  const seen = new Set()
  const MAX_SEEN = 4096
  const V2_TYPES = new Set([
    "session.created",
    "session.deleted",
    "session.execution.started",
    "session.execution.succeeded",
    "session.execution.failed",
    "session.execution.interrupted",
    "session.step.started",
    "session.tool.called",
    "session.retry.scheduled",
    "permission.asked",
    "permission.replied",
    "form.created",
    "form.replied",
    "form.cancelled",
  ])

  // The directory a 2.0 event happened in, kept for the slot's name. Taken
  // from session.created's own location first, then from any event's
  // envelope, and never overwritten once known: a session does not move
  // between directories except by session.moved, which nothing here reads.
  //
  // A slot already written without a name is let through the refresh
  // throttle on the next running event, so the name lands at once rather
  // than up to REFRESH_MS later. Measured in a real 2.0.32 service: the first
  // session in a project boots that project's location -- and this plugin
  // with it -- so session.created has already gone by, execution.started
  // carries no location, and the name arrived only with the next step's
  // envelope, eight seconds after the slot.
  function noteDir(sid, dir) {
    const r = rec(sid)
    if (r.dir || typeof dir !== "string") return
    r.dir = dir
    if (r.slot) r.lastWrite = 0
  }

  // The mapping is in docs/plans/opencode-2-light.md, "A. One plugin file,
  // both shapes". It differs from 1.x's in two ways that matter here:
  //   - one terminal event per turn (execution.succeeded / failed /
  //     interrupted), so there is no error-then-idle latch to inherit;
  //   - execution.started fires once per turn, not per model step, so the
  //     running clock is kept fresh by the step and tool events instead.
  function onV2Event(event) {
    if (!event || typeof event !== "object") return
    const type = event.type
    const d = event.data
    if (!V2_TYPES.has(type) || !d || typeof d !== "object") return
    if (typeof event.id === "string") {
      if (seen.has(event.id)) return
      if (seen.size >= MAX_SEEN) seen.delete(seen.keys().next().value)
      seen.add(event.id)
    }
    const where = event.location && typeof event.location === "object" ? event.location.directory : null

    if (type === "form.created") {
      // A form names its session inside `form`. "global" is the sentinel
      // owner MCP elicitation uses for a form that belongs to no session
      // (packages/schema/src/form.ts); it passes ID_RE, and a slot called
      // global.state would be a session that does not exist.
      const f = d.form
      const sid = f && typeof f === "object" ? validId(f.sessionID) : null
      if (!sid || sid === "global") return
      noteDir(sid, where)
      onAsked(sid, f.id)
      return
    }
    const sid = validId(d.sessionID)
    if (!sid || sid === "global") return
    switch (type) {
      case "session.created": {
        const loc = d.location && typeof d.location === "object" ? d.location.directory : null
        noteDir(sid, loc || where)
        onInfo({ id: sid, parentID: d.parentID }, true)
        return
      }
      case "session.deleted":
        onDeleted({ id: sid })
        return
    }
    noteDir(sid, where)
    if (isChild(sid)) {
      const root = rootOf(sid)
      switch (type) {
        case "session.execution.started":
        case "session.step.started":
        case "session.tool.called":
          agentStart(sid)
          stillRunning(root)
          return
        case "session.retry.scheduled":
          agentStart(sid)
          if (isLongRetryAt(d.at)) retryFail(root)
          else stillRunning(root)
          return
        case "session.execution.succeeded":
        case "session.execution.failed":
          agentStop(sid)
          return
        case "session.execution.interrupted":
          if (endsTurn(d.reason)) agentStop(sid)
          return
      }
    } else {
      const r = rec(sid)
      switch (type) {
        case "session.execution.started":
          r.retryFailed = false
          writeSlot(sid, "UserPromptSubmit")
          syncMarker(sid)
          return
        case "session.step.started":
        case "session.tool.called":
          // stillRunning() will not repaint a finished turn, and lifts a
          // long-retry failure once the retry has fired and a step begins.
          stillRunning(sid)
          return
        case "session.retry.scheduled":
          if (isLongRetryAt(d.at)) retryFail(sid)
          else stillRunning(sid)
          return
        case "session.execution.succeeded":
          finishTurn(sid, "Stop")
          return
        case "session.execution.failed":
          finishTurn(sid, "StopFailure")
          return
        case "session.execution.interrupted":
          if (endsTurn(d.reason)) finishTurn(sid, "Interrupt")
          return
      }
    }
    switch (type) {
      case "permission.asked":
        onAsked(sid, d.id)
        return
      case "permission.replied":
        onAnswered(sid, d.requestID)
        return
      case "form.replied":
      case "form.cancelled":
        onAnswered(sid, d.id)
        return
    }
  }

  // A finished turn is asking nobody anything, whatever was left open.
  function finishTurn(sid, event) {
    rec(sid).retryFailed = false
    dropRequests((v) => v.root === sid)
    syncMarker(sid)
    writeSlot(sid, event)
    if (event === "Stop" || event === "Interrupt") scheduleIdleEnd(sid)
  }

  // A FINISHED 2.0 SESSION ENDS AFTER IDLE_END_MS. Found on the owner's desk,
  // 2026-10-20: ten sessions on the panel while three were open. 2.0 keeps
  // every session in one service that outlives every window, and nothing it
  // publishes says a session was closed -- session.viewed is a read receipt,
  // location eviction is an hour-long idle timer, and the TUI's tabs are
  // client-side (packages/tui/src/context/session-tabs.tsx) -- so a finished
  // session stayed for ABANDONED_AFTER_S, the full hour. Claude's sessions
  // leave when their terminal's pid dies; 2.0's pid is the service's, so
  // the nearest honest rule is idleness: a turn that ended on the person's
  // move and saw nothing more for fifteen minutes is a session they have put
  // down. Its next prompt writes a slot and removes the tombstone, so a
  // session picked up again comes straight back. StopFailure is never ended
  // early: a red light must not vanish before anyone has seen it.
  //
  // The timer ends the session only if the slot on disk is still the very
  // one this tracker wrote. Another writer -- a newer tracker after a reload,
  // or a second OpenCode -- may have taken the session up since, and must not
  // have it tombstoned underneath it.
  function scheduleIdleEnd(sid) {
    clearIdleEnd(sid)
    const r = sessions.get(sid)
    if (!r || r.parent) return
    const wrote = Math.floor(r.lastWrite / 1000)
    const t = setTimeout(() => {
      idleEnds.delete(sid)
      quietly(() => {
        const cur = sessions.get(sid)
        if (!cur || (cur.last !== "Stop" && cur.last !== "Interrupt")) return
        for (const v of requests.values()) if (v.root === sid) return
        const dir = presentStateDir()
        if (!dir) return
        const onDisk = JSON.parse(fs.readFileSync(path.join(dir, `${sid}.state`), "utf8"))
        if (onDisk.event !== cur.last || onDisk.t !== wrote) return
        sessionEnd(sid)
      })
    }, IDLE_END_MS)
    // A timer must not keep a process alive: `opencode run` exits when its
    // turn is done, and the 'exit' listener already ends its sessions.
    if (t && typeof t.unref === "function") t.unref()
    idleEnds.set(sid, t)
  }

  function clearIdleEnd(sid) {
    const t = idleEnds.get(sid)
    if (t !== undefined) {
      clearTimeout(t)
      idleEnds.delete(sid)
    }
  }

  // "user" is Esc, "inactivity" OpenCode giving up on a turn nobody answered:
  // both leave the session waiting on the person. "shutdown" keeps the turn
  // claimed so it resumes when the service comes back, and "superseded" is a
  // new prompt replacing the running one -- its own execution.started
  // follows -- so neither ends anything the panel should show
  // (packages/core/src/session/execution.ts).
  function endsTurn(reason) {
    return reason === "user" || reason === "inactivity"
  }

  // session.retry.scheduled carries the epoch ms the retry fires at
  // (packages/core/src/session/runner/retry.ts), where 1.x carried `next`.
  function isLongRetryAt(at) {
    return typeof at === "number" && Number.isFinite(at) && at - Date.now() > LONG_RETRY_MS
  }

  // Every top-level session this tracker wrote a slot for, ended. For the
  // 'exit' listener; see note 3 at the top.
  function endAll() {
    for (const [sid, r] of [...sessions]) {
      if (!r.parent && r.slot) quietly(() => sessionEnd(sid))
    }
  }

  return { onEvent, onTool, onV2Event, endAll }
}

export const BlinkStatePlugin = async (input) => {
  // The directory OpenCode was started in. Used only as the fallback name for
  // a session whose own info has not been seen -- a session resumed in a
  // process that never saw it created. Under `opencode serve` this is the
  // server's directory, not necessarily the session's, so the session's own
  // info.directory always wins when there is one.
  let fallbackDir = null
  quietly(() => {
    if (input && typeof input.directory === "string") fallbackDir = input.directory
  })
  const tracker = makeTracker(fallbackDir)

  // The process exiting is the session ending -- see note 3 at the top. The
  // top-level sessions this instance wrote a slot for get the SessionEnd
  // treatment, from the one listener registered at module scope above --
  // replacing whatever an earlier instance for the same directory left there.
  // But only while this instance is the one registered for its directory
  // and among the newest MAX_INSTANCES: one replaced or evicted before the
  // exit ends nothing, and its sessions fall to the pid check (third review,
  // 2026-10-19: this said "every" session, which a short `opencode run` that
  // lost its handler would not get -- see the note on `enders`).
  quietly(() => {
    const key = fallbackDir === null ? Symbol("no directory") : fallbackDir
    enders.delete(key)
    while (enders.size >= MAX_INSTANCES) enders.delete(enders.keys().next().value)
    enders.set(key, tracker.endAll)
  })

  return {
    event: async (arg) => {
      quietly(() => tracker.onEvent(arg && arg.event))
    },
    "tool.execute.before": async (hookInput) => {
      quietly(() => tracker.onTool(hookInput))
    },
    "tool.execute.after": async (hookInput) => {
      quietly(() => tracker.onTool(hookInput))
    },
  }
}

// THE 2.0 SHAPE. OpenCode 2.0 runs one background service per user, shared by
// every TUI, `opencode run` and the Desktop app, and plugins run inside it.
// It loads this file only through a default export of {id, setup}
// (packages/core/src/plugin/module.ts), calls setup once per active project
// location, and hands each call the whole service's event stream -- every
// location's events, not just its own (core/src/plugin/host.ts). See
// docs/plans/opencode-2-light.md.
//
// ONE TRACKER PER PROCESS. With a tracker per call, N open projects would be N
// writers racing on the same slots, and a permission asked in one copy and
// answered in another would leave the waiting marker behind. Filtering each
// copy to its own location would fix that only for events that carry one,
// and an event published outside a location carries none
// (core/src/bus.ts, publish) -- and measured in a real 2.0.32 service, the
// execution.* events, the very ones that say running and finished, carry
// none. So every copy feeds the one tracker below, kept on globalThis because
// OpenCode imports the file afresh per location, and the tracker drops an
// event id it has already handled.
//
// THE TRACKER FOLLOWS THE FILE. `blink update` replaces blink.js under a
// running service, and 2.0 re-imports it (the file's mtime is part of the
// plugin's revision, config/plugin/source.ts). With a fixed key the new
// module found the old module's tracker and went on running the old code
// until the service restarted (review, 2026-10-20). The key is therefore the
// file's own digest: a new file gets a new tracker, the same file read again
// by another location gets the same one. A file that cannot be read for its
// digest -- not a case anyone has seen -- shares one fixed key instead,
// which is the old behaviour and still correct for a single version.
//
// The pid in a 2.0 slot is the service's, which outlives every TUI, and
// closing a TUI fires nothing. A 2.0 slot therefore ends on session.deleted,
// on the service exiting (the 'exit' listener, under one key for all 2.0
// trackers so the newest replaces its predecessors), when the daemon finds
// the service's pid dead, or by the one-hour abandonment sweep -- what 1.x
// already gets for a TUI killed with ctrl-c.
const TRACKER_KEY = Symbol.for("blink.opencode.tracker")
const V2_ENDER_KEY = Symbol.for("blink.opencode.tracker.ender")

function fileRevision() {
  try {
    const body = fs.readFileSync(fileURLToPath(import.meta.url))
    return crypto.createHash("sha256").update(body).digest("hex")
  } catch {
    return "unreadable"
  }
}

const REVISION = fileRevision()

function sharedTracker() {
  const held = globalThis[TRACKER_KEY]
  if (held && held.revision === REVISION && held.tracker) return held.tracker
  const tracker = makeTracker(null)
  globalThis[TRACKER_KEY] = { revision: REVISION, tracker }
  quietly(() => {
    enders.delete(V2_ENDER_KEY)
    while (enders.size >= MAX_INSTANCES) enders.delete(enders.keys().next().value)
    enders.set(V2_ENDER_KEY, tracker.endAll)
  })
  return tracker
}

// A stream that ends without our abort is resubscribed, after a wait that
// doubles from a second to half a minute and resets once events flow again.
// 2.0 encodes every event once per subscriber, and one encode failure ends the
// stream for every copy at once (packages/plugin/src/promise/adapter.ts,
// event.subscribe); without this the light went dark until something happened
// to reload the plugin (review, 2026-10-20).
const RESUBSCRIBE_MIN_MS = 1000
const RESUBSCRIBE_MAX_MS = 30 * 1000

async function blinkV2(ctx) {
  const controller = new AbortController()
  quietly(() => {
    const tracker = sharedTracker()
    const signal = controller.signal
    // The abort listener goes when the timer fires, not only on abort: one
    // left per resubscribe kept its closure for the life of the setup, and a
    // service whose stream failed now and then grew without bound (measured
    // by the test driver's leak2 mode, 2026-10-20: 13 resubscribes, 13
    // listeners).
    const pause = (ms) =>
      new Promise((resolve) => {
        const onAbort = () => {
          clearTimeout(t)
          resolve()
        }
        const t = setTimeout(() => {
          signal.removeEventListener("abort", onAbort)
          resolve()
        }, ms)
        signal.addEventListener("abort", onAbort, { once: true })
      })
    // Not awaited: setup must return for the plugin to finish loading, and the
    // loop lives until cleanup aborts it.
    ;(async () => {
      let wait = RESUBSCRIBE_MIN_MS
      while (!signal.aborted) {
        try {
          for await (const event of ctx.event.subscribe({ signal })) {
            wait = RESUBSCRIBE_MIN_MS
            quietly(() => tracker.onV2Event(event))
          }
        } catch {
          // Deliberately empty: resubscribed below. See SILENCE.
        }
        if (signal.aborted) return
        await pause(wait)
        wait = Math.min(wait * 2, RESUBSCRIBE_MAX_MS)
      }
    })().catch(() => {})
  })
  return () => {
    quietly(() => controller.abort())
  }
}

// What both lines load. 1.x from 1.3.4 detects a default export carrying
// `server` and calls that once, in preference to the named export
// (packages/opencode/src/plugin/index.ts, applyPlugin; first in v1.3.4); 2.0
// takes `setup` and ignores the extra key, because its module schema is an
// ordinary struct, which keeps only the properties it names
// (core/src/plugin/module.ts). There must be no `effect` key: 2.0 tries that
// variant first. And it must be a plain object, not a function carrying
// these keys: 2.0's schema rejects anything whose typeof is not "object"
// (effect 4.0.1, SchemaAST.ts).
//
// OpenCode 1.3.3 and older are NOT supported. They call every export as a
// function: from 1.2.11 the default object fails with a "Failed to load
// plugin" message beside a working light, and up to 1.2.10, with no catch
// around the loop, it breaks OpenCode's plugin hooks altogether. Decided
// 2026-10-20 rather than have the installer pick a file per version; the
// README states the minimum.
export default { id: "blink", server: BlinkStatePlugin, setup: blinkV2 }
