// Drives tools/blink-opencode.js the way OpenCode does, without OpenCode.
//
//   node tests/ci/opencode_plugin_driver.mjs check     every scenario, asserted
//   node tests/ci/opencode_plugin_driver.mjs exit      one session, then exit
//   node tests/ci/opencode_plugin_driver.mjs fixture   a known desk, held open
//
// Run with HOME pointed at a throwaway directory: the plugin writes into
// ~/.blink/state-opencode and nowhere else, and this checks that it does.
// It writes there only while that directory EXISTS -- `blink install`
// creates it, the plugin never does (see THE DIRECTORY IS THE INSTALLER'S
// in the plugin's header) -- so `check` and `fixture` create it first, as
// the installer would, and `exit` leaves that to its caller: the sh check
// runs it both with the directory and without it.
//
// The events are shaped exactly as captured from OpenCode 1.19.3 (the lab
// capture behind docs/plans/opencode.md, section "What was measured"): an
// `event` hook handed {event: {type, properties}}, and the tool hooks handed
// {tool, sessionID, callID} plus an output object carrying the arguments.
// Every payload below carries fields the plugin must be seen NOT to keep --
// a command, a question, an error body -- and `check` greps for them.
//
// `check` prints nothing on success. tests/ci/check_opencode_plugin.sh holds
// it to that, because the plugin's own silence is one of the things checked:
// stdout and stderr are trapped in-process while the plugin runs, and any
// byte it writes is a failure.
//
// `fixture` builds one desk of every state, prints "ready" and waits for
// stdin to close, so that tests/pc/test_opencode_plugin.py can point
// claude_state at the result WHILE this process -- whose pid is in every
// slot -- is still alive, and then again after it has exited.

import * as fs from "node:fs"
import * as os from "node:os"
import * as path from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

const HERE = path.dirname(fileURLToPath(import.meta.url))
// Overridable so a test can hand this a deliberately broken copy and see the
// check fail: a checker nobody has seen fail is not known to check anything.
const PLUGIN = process.env.BLINK_OPENCODE_PLUGIN || path.join(HERE, "..", "..", "tools", "blink-opencode.js")
const DIR = path.join(os.homedir(), ".blink", "state-opencode")
const SECRETS = ["swordfish", "rm -rf", "Which colour", "secret-body", "/x/y/"]

// --- trap every byte the plugin might print --------------------------------

const realOut = process.stdout.write.bind(process.stdout)
const realErr = process.stderr.write.bind(process.stderr)
const printed = []
function trap() {
  process.stdout.write = (chunk, ...rest) => {
    printed.push(["stdout", String(chunk)])
    return true
  }
  process.stderr.write = (chunk, ...rest) => {
    printed.push(["stderr", String(chunk)])
    return true
  }
}
function untrap() {
  process.stdout.write = realOut
  process.stderr.write = realErr
}

let failures = []
function fail(msg) {
  failures.push(msg)
}

// --- the plugin, driven ----------------------------------------------------

const mod = await import(pathToFileURL(PLUGIN).href)
const exported = Object.keys(mod)
if (exported.length !== 1 || typeof mod[exported[0]] !== "function") {
  // OpenCode calls EVERY exported function as a plugin factory.
  untrap()
  realErr(`FAIL the plugin must export exactly one function, got ${JSON.stringify(exported)}\n`)
  process.exit(1)
}
const factory = mod[exported[0]]

async function plugin(directory = "/x/y/MyProj") {
  return factory({
    directory,
    worktree: directory,
    project: { id: "p", worktree: directory },
    serverUrl: new URL("http://localhost:4096/"),
    client: {},
    $: () => {},
  })
}

function ev(h, type, properties) {
  return h.event({ event: { id: "evt_x", type, properties } })
}
const created = (h, id, extra = {}) =>
  ev(h, "session.created", { sessionID: id, info: { id, directory: "/x/y/MyProj", title: "swordfish", ...extra } })
const updated = (h, id, extra = {}) =>
  ev(h, "session.updated", { sessionID: id, info: { id, directory: "/x/y/MyProj", title: "swordfish", ...extra } })
const status = (h, sid, s) => ev(h, "session.status", { sessionID: sid, status: s })
const busy = (h, sid) => status(h, sid, { type: "busy" })
const idle = (h, sid) => status(h, sid, { type: "idle" })
const retry = (h, sid, inMs) =>
  status(h, sid, { type: "retry", attempt: 1, message: "Usage limit reached, secret-body", next: Date.now() + inMs })
const error = (h, sid, name) =>
  ev(h, "session.error", { sessionID: sid, error: { name, data: { message: "secret-body", responseBody: "swordfish" } } })
const asked = (h, sid, id, kind = "permission") =>
  ev(h, `${kind}.asked`, kind === "permission"
    ? { id, sessionID: sid, permission: "bash", patterns: ["rm -rf /"], metadata: { command: "rm -rf /" }, always: [] }
    : { id, sessionID: sid, questions: [{ question: "Which colour?", header: "c", options: [] }] })
const replied = (h, sid, requestID, type = "permission.replied") =>
  ev(h, type, { sessionID: sid, requestID, reply: "once", answers: [["swordfish"]] })
const deleted = (h, id) => ev(h, "session.deleted", { sessionID: id, info: { id, directory: "/x/y/MyProj" } })
const toolBefore = (h, sid) =>
  h["tool.execute.before"]({ tool: "bash", sessionID: sid, callID: "call_1" }, { args: { command: "rm -rf /" } })
const toolAfter = (h, sid) =>
  h["tool.execute.after"]({ tool: "bash", sessionID: sid, callID: "call_1", args: { command: "rm -rf /" } },
    { title: "swordfish", output: "swordfish", metadata: {} })

// --- what is on disk -------------------------------------------------------

function p(...parts) {
  return path.join(DIR, ...parts)
}
function exists(rel) {
  return fs.existsSync(p(rel))
}
function slot(sid) {
  try {
    return fs.readFileSync(p(`${sid}.state`), "utf8")
  } catch {
    return null
  }
}
function ino(sid) {
  try {
    return fs.statSync(p(`${sid}.state`)).ino
  } catch {
    return null
  }
}

// The slot, held to the shim's exact shape: the same keys in the same order,
// a bare ten-digit epoch, this process's pid, and the name only when expected.
function expectSlot(label, sid, event, name = "MyProj") {
  const text = slot(sid)
  if (text === null) return fail(`${label}: no slot for ${sid}`)
  const nameval = name ? `,"name":"${name}"` : ""
  const re = new RegExp(`^\\{"event":"${event}","t":(\\d{10}),"pid":${process.pid}${nameval.replace(/[.]/g, "\\.")}\\}$`)
  const m = re.exec(text)
  if (!m) return fail(`${label}: slot for ${sid} is ${text}, wanted event ${event} name ${name}`)
  const age = Date.now() / 1000 - Number(m[1])
  if (age < -2 || age > 30) fail(`${label}: slot clock is ${age}s off`)
}
function expectFile(label, rel, want = true) {
  if (exists(rel) !== want) fail(`${label}: ${rel} should ${want ? "" : "not "}exist`)
}

// What `blink install` does before the plugin is ever loaded
// (pc/cli._install_opencode_plugin): the state directory, 0700.
function makeDir() {
  fs.mkdirSync(DIR, { recursive: true, mode: 0o700 })
  if (process.platform !== "win32") fs.chmodSync(DIR, 0o700)
}

function walk(dir, out = [], base = dir) {
  let names = []
  try {
    names = fs.readdirSync(dir)
  } catch {
    return out
  }
  for (const n of names) {
    const full = path.join(dir, n)
    out.push(path.relative(base, full))
    if (fs.statSync(full).isDirectory()) walk(full, out, base)
  }
  return out
}

// --- check -----------------------------------------------------------------

// Every kind of event the plugin writes for, against one session id: used
// where the point is that NONE of them may write anything.
async function everyEvent(h, sid) {
  await created(h, sid)
  await busy(h, sid)
  await toolBefore(h, sid)
  await asked(h, sid, `${sid}_p1`)
  await asked(h, sid, `${sid}_q1`, "question")
  await replied(h, sid, `${sid}_p1`)
  await created(h, `${sid}_kid`, { parentID: sid })
  await busy(h, `${sid}_kid`)
  await retry(h, `${sid}_kid`, 3600 * 1000)
  await retry(h, sid, 3600 * 1000)
  await error(h, sid, "APIError")
  await idle(h, sid)
  await busy(h, sid)
  await error(h, sid, "MessageAbortedError")
  await idle(h, sid)
  await deleted(h, sid)
}

async function check() {
  // 0. No state directory: what `blink uninstall` leaves behind for an
  //    OpenCode that loaded the plugin before it and is still running.
  //    Found in review, 2026-10-19: the plugin used to mkdir -p on every
  //    write, and its next event put ~/.blink/state-opencode -- and ~/.blink
  //    itself, where that had gone too -- back on a desk Blink had been
  //    removed from. Nothing may be created, and nothing may throw.
  {
    const home = os.homedir()
    const h = await plugin()
    try {
      await everyEvent(h, "ses_nodir")
    } catch (e) {
      fail(`events with no state directory threw: ${e}`)
    }
    if (fs.existsSync(path.join(home, ".blink"))) {
      fail(`with no state directory the plugin created ${walk(home).join(", ")}`)
    }
    // The uninstall landing under a plugin that has already written: the
    // directory goes, the same process keeps receiving events.
    makeDir()
    const h2 = await plugin()
    await created(h2, "ses_uninst")
    await busy(h2, "ses_uninst")
    expectSlot("before the uninstall", "ses_uninst", "UserPromptSubmit")
    fs.rmSync(DIR, { recursive: true, force: true })
    try {
      await everyEvent(h2, "ses_uninst")
      await everyEvent(h2, "ses_uninst2")
    } catch (e) {
      fail(`events after the state directory went threw: ${e}`)
    }
    if (fs.existsSync(DIR)) fail(`the plugin recreated the state directory: ${walk(DIR).join(", ")}`)
  }
  // From here on, the directory the installer would have made.
  makeDir()

  // 1. A normal turn: busy repeated per step, a tool, then idle (twice, as
  //    captured). Repeats inside REFRESH_MS must not rewrite the slot.
  {
    const h = await plugin()
    await created(h, "ses_norm")
    expectFile("created only", "ses_norm.state", false)
    await busy(h, "ses_norm")
    expectSlot("busy", "ses_norm", "UserPromptSubmit")
    const before = ino("ses_norm")
    await busy(h, "ses_norm")
    await toolBefore(h, "ses_norm")
    await toolAfter(h, "ses_norm")
    if (ino("ses_norm") !== before) fail("running events inside the refresh window rewrote the slot")
    await idle(h, "ses_norm")
    expectSlot("idle", "ses_norm", "Stop")
    const stopped = ino("ses_norm")
    await idle(h, "ses_norm")
    if (ino("ses_norm") !== stopped) fail("a repeated idle rewrote the slot")
    // A tool hook after the turn finished must not repaint it as running.
    await toolAfter(h, "ses_norm")
    expectSlot("tool after stop", "ses_norm", "Stop")
    if (process.platform !== "win32") {
      const fmode = fs.statSync(p("ses_norm.state")).mode & 0o777
      const dmode = fs.statSync(DIR).mode & 0o777
      if (fmode !== 0o600) fail(`slot mode is ${fmode.toString(8)}, wanted 600`)
      if (dmode !== 0o700) fail(`state dir mode is ${dmode.toString(8)}, wanted 700`)
    }
  }

  // 2. One permission, asked and answered.
  {
    const h = await plugin()
    await created(h, "ses_perm")
    await busy(h, "ses_perm")
    await toolBefore(h, "ses_perm")
    await asked(h, "ses_perm", "per_1")
    expectSlot("asked", "ses_perm", "PermissionRequest")
    expectFile("asked", "ses_perm.waiting")
    if (process.platform !== "win32" && (fs.statSync(p("ses_perm.waiting")).mode & 0o777) !== 0o600) {
      fail("waiting marker is not 0600")
    }
    // Busy while the prompt is open is a model step, not an answer.
    await busy(h, "ses_perm")
    expectFile("busy while asked", "ses_perm.waiting")
    await replied(h, "ses_perm", "per_1")
    // Running either way. Which running word is on the slot depends on the
    // refresh throttle -- that busy wrote UserPromptSubmit a moment ago --
    // and claude_state reads all three the same.
    const ev2 = JSON.parse(slot("ses_perm")).event
    if (!["UserPromptSubmit", "PreToolUse", "PostToolUse"].includes(ev2)) fail(`replied wrote ${ev2}`)
    expectFile("replied", "ses_perm.waiting", false)
    // Without a busy in between, the answer itself moves the slot off
    // PermissionRequest.
    await asked(h, "ses_perm", "per_2")
    await replied(h, "ses_perm", "per_2")
    expectSlot("replied", "ses_perm", "PostToolUse")
    expectFile("replied", "ses_perm.waiting", false)
  }

  // 3. Two overlapping prompts: the marker lasts until the LAST is answered.
  {
    const h = await plugin()
    await busy(h, "ses_two")
    await asked(h, "ses_two", "per_a")
    await asked(h, "ses_two", "per_b")
    await replied(h, "ses_two", "per_a")
    expectFile("one of two answered", "ses_two.waiting")
    expectSlot("one of two answered", "ses_two", "PostToolUse")
    await replied(h, "ses_two", "per_b")
    expectFile("both answered", "ses_two.waiting", false)
    // An answer to a request this process never saw opened.
    await asked(h, "ses_two", "per_c")
    await replied(h, "ses_two", "per_unknown")
    expectFile("stray answer", "ses_two.waiting")
    // A turn that finishes is asking nobody anything.
    await idle(h, "ses_two")
    expectFile("idle with one open", "ses_two.waiting", false)
    expectSlot("idle with one open", "ses_two", "Stop")
  }

  // 4. A question, rejected; and one replied.
  {
    const h = await plugin()
    await busy(h, "ses_q")
    await asked(h, "ses_q", "que_1", "question")
    expectSlot("question", "ses_q", "PermissionRequest")
    expectFile("question", "ses_q.waiting")
    await replied(h, "ses_q", "que_1", "question.rejected")
    expectFile("question rejected", "ses_q.waiting", false)
    expectSlot("question rejected", "ses_q", "PostToolUse")
    await asked(h, "ses_q", "que_2", "question")
    await replied(h, "ses_q", "que_2", "question.replied")
    expectFile("question replied", "ses_q.waiting", false)
  }

  // 5. A short retry is running; 6. a long one is failed until busy returns.
  {
    const h = await plugin()
    await busy(h, "ses_retry")
    await retry(h, "ses_retry", 1000)
    const t = JSON.parse(slot("ses_retry"))
    if (!["UserPromptSubmit", "PreToolUse"].includes(t.event)) fail(`short retry wrote ${t.event}`)
    await retry(h, "ses_retry", 3600 * 1000)
    expectSlot("long retry", "ses_retry", "StopFailure")
    await busy(h, "ses_retry")
    expectSlot("busy after long retry", "ses_retry", "UserPromptSubmit")
    // A long retry, then the five attempts run out: error, then idle twice.
    await retry(h, "ses_retry", 3600 * 1000)
    await busy(h, "ses_retry")
    await error(h, "ses_retry", "APIError")
    await idle(h, "ses_retry")
    await idle(h, "ses_retry")
    expectSlot("retries exhausted", "ses_retry", "StopFailure")
  }

  // 7. Abort: Interrupt, and the idles and the aborted tool's after-hook
  //    that follow it change nothing.
  {
    const h = await plugin()
    await busy(h, "ses_abort")
    await asked(h, "ses_abort", "per_x")
    await error(h, "ses_abort", "MessageAbortedError")
    expectSlot("abort", "ses_abort", "Interrupt")
    expectFile("abort", "ses_abort.waiting", false)
    await idle(h, "ses_abort")
    await idle(h, "ses_abort")
    await toolAfter(h, "ses_abort")
    expectSlot("abort then idle", "ses_abort", "Interrupt")
  }

  // 7b. Answers that arrive after their turn is over. Found in review,
  //     2026-10-19: every answer wrote PostToolUse, so a question rejected
  //     after an abort, or a reply to a request this process never saw
  //     arriving after Stop, repainted a finished session as running -- and
  //     nothing moved it back until the next busy, up to an hour later.
  {
    const h = await plugin()
    // busy, question asked, Esc: abort, idle, then the late rejection.
    await busy(h, "ses_late_rej")
    await asked(h, "ses_late_rej", "que_late", "question")
    await error(h, "ses_late_rej", "MessageAbortedError")
    await idle(h, "ses_late_rej")
    await replied(h, "ses_late_rej", "que_late", "question.rejected")
    expectSlot("question rejected after the abort", "ses_late_rej", "Interrupt")
    expectFile("question rejected after the abort", "ses_late_rej.waiting", false)
    // A reply naming a request this process never saw, after Stop.
    await busy(h, "ses_late_rep")
    await idle(h, "ses_late_rep")
    await replied(h, "ses_late_rep", "per_never_seen")
    expectSlot("unknown reply after stop", "ses_late_rep", "Stop")
    expectFile("unknown reply after stop", "ses_late_rep.waiting", false)
    // A request that WAS seen, answered only after its turn idled.
    await busy(h, "ses_late_rep")
    await asked(h, "ses_late_rep", "per_seen")
    await idle(h, "ses_late_rep")
    await replied(h, "ses_late_rep", "per_seen")
    expectSlot("known reply after stop", "ses_late_rep", "Stop")
    // A failed turn stays failed through a late answer too.
    await busy(h, "ses_late_err")
    await asked(h, "ses_late_err", "per_e")
    await error(h, "ses_late_err", "APIError")
    await idle(h, "ses_late_err")
    await replied(h, "ses_late_err", "per_e")
    expectSlot("reply after a failure", "ses_late_err", "StopFailure")
  }

  // 8. An API error is StopFailure, and the idle after it does not hide it.
  {
    const h = await plugin()
    await busy(h, "ses_err")
    await error(h, "ses_err", "APIError")
    await idle(h, "ses_err")
    await idle(h, "ses_err")
    expectSlot("api error", "ses_err", "StopFailure")
    await busy(h, "ses_err")
    expectSlot("next turn", "ses_err", "UserPromptSubmit")
    await idle(h, "ses_err")
    expectSlot("next turn idle", "ses_err", "Stop")
  }

  // 9. Subagents: a file under the parent, never a slot of their own.
  {
    const h = await plugin()
    await created(h, "ses_par")
    await busy(h, "ses_par")
    await created(h, "ses_kid", { parentID: "ses_par" })
    expectFile("child created", "ses_par/ses_kid")
    await busy(h, "ses_kid")
    await toolBefore(h, "ses_kid")
    expectFile("child busy", "ses_kid.state", false)
    await asked(h, "ses_kid", "per_k")
    expectFile("child asks", "ses_par.waiting")
    expectFile("child asks", "ses_kid.waiting", false)
    expectSlot("child asks", "ses_par", "PermissionRequest")
    await replied(h, "ses_kid", "per_k")
    expectFile("child answered", "ses_par.waiting", false)
    // A grandchild counts under the top-level session.
    await created(h, "ses_gk", { parentID: "ses_kid" })
    expectFile("grandchild", "ses_par/ses_gk")
    await idle(h, "ses_gk")
    expectFile("grandchild idle", "ses_par/ses_gk", false)
    // session.updated after the child's idle (captured every run) must not
    // put the agent file back.
    await idle(h, "ses_kid")
    await updated(h, "ses_kid", { parentID: "ses_par" })
    expectFile("child idle", "ses_par/ses_kid", false)
    // A child that errors stops too, and its long retry fails the parent.
    await created(h, "ses_kid2", { parentID: "ses_par" })
    await retry(h, "ses_kid2", 3600 * 1000)
    expectSlot("child long retry", "ses_par", "StopFailure")
    await busy(h, "ses_kid2")
    expectSlot("child busy again", "ses_par", "PreToolUse")
    await error(h, "ses_kid2", "APIError")
    expectFile("child error", "ses_par/ses_kid2", false)
    expectSlot("child error", "ses_par", "PreToolUse")
    for (const f of walk(DIR)) {
      if (/ses_(kid|gk)[^/\\]*\.(state|waiting|ended)$/.test(f)) fail(`a subagent got a session file: ${f}`)
    }

    // 10. Deletion: the slot, marker and agent dir go, a tombstone stays.
    await created(h, "ses_kid3", { parentID: "ses_par" })
    await asked(h, "ses_par", "per_d")
    await deleted(h, "ses_par")
    expectFile("deleted", "ses_par.state", false)
    expectFile("deleted", "ses_par.waiting", false)
    expectFile("deleted", "ses_par", false)
    expectFile("deleted", "ses_par.ended")
    // Resumed under the same id: the tombstone goes.
    await busy(h, "ses_par")
    expectFile("resumed", "ses_par.ended", false)
    expectSlot("resumed", "ses_par", "UserPromptSubmit")
    // Deleting a subagent's session stops the agent and nothing else.
    await created(h, "ses_kid4", { parentID: "ses_par" })
    await deleted(h, "ses_kid4")
    expectFile("child deleted", "ses_par/ses_kid4", false)
    expectFile("child deleted", "ses_kid4.ended", false)
    expectSlot("child deleted", "ses_par", "UserPromptSubmit")
  }

  // 11. A status that beats its session's creation: top-level until the info
  //     says otherwise, then the slot is taken back without a tombstone.
  {
    const h = await plugin()
    await busy(h, "ses_late")
    expectSlot("status first", "ses_late", "UserPromptSubmit")
    await updated(h, "ses_late", { parentID: "ses_norm" })
    expectFile("turned out a child", "ses_late.state", false)
    expectFile("turned out a child", "ses_late.ended", false)
  }

  // 12. Hostile ids: nothing is written for any of them, anywhere.
  {
    const h = await plugin()
    const bad = ["../evil", "..", ".", "/evil", "a/b", "a\\b", 'a"b', "", " ses", "_ses", "-ses", "ses x",
      "x".repeat(65), "ses\u0000x", "évil", null, 123, {}, ["ses_arr"], true]
    const before = walk(DIR).sort().join("\n")
    for (const id of bad) {
      await busy(h, id)
      await asked(h, id, "per_bad")
      await replied(h, id, "per_bad")
      await error(h, id, "APIError")
      await idle(h, id)
      await toolBefore(h, id)
      await created(h, id)
      await created(h, "ses_okchild", { parentID: id })
      await deleted(h, id)
    }
    await created(h, "ses_okchild2", { parentID: "../evil" })
    await busy(h, "ses_okchild2")
    const after = walk(DIR).filter((f) => !/ses_okchild2?/.test(f)).sort().join("\n")
    if (after !== before) fail(`hostile ids changed the state dir:\n${after}\nvs\n${before}`)
    if (exists("ses_okchild2.state") !== true) fail("a session whose parentID is invalid should stay top-level")
    for (const f of walk(path.dirname(path.dirname(DIR)))) {
      if (/evil/.test(f)) fail(`a hostile id reached the filesystem: ${f}`)
    }
    // The 64-character limit is inclusive.
    const max = "s" + "x".repeat(63)
    await busy(h, max)
    expectSlot("64-char id", max, "UserPromptSubmit")
  }

  // 13. Directory names: only a final segment the shim's pattern accepts.
  {
    const cases = [
      ["/x/y/MyProj", "MyProj"],
      ["C:\\Users\\a\\Proj", "Proj"],
      ["C:/Users/a/Proj", "Proj"],
      ["/x/we\"ird/Proj", "Proj"],
      ["/x/y/Pro\"j", null],
      ["/x/y/back\\slash", "slash"],
      ["/x/y/sp ace", null],
      ["/x/y/.hidden", null],
      ["/x/y/..", null],
      ["/x/y/Proj/", null],
      ["Proj", null],
      ["/x/y/" + "a".repeat(24), "a".repeat(24)],
      ["/x/y/" + "a".repeat(25), null],
      ["/x/y/naïve", null],
      // Not a string: the directory OpenCode was started in stands in.
      [42, "MyProj"],
    ]
    const h = await plugin()
    let i = 0
    for (const [dir, want] of cases) {
      const sid = `ses_dir${i++}`
      await ev(h, "session.created", { info: { id: sid, directory: dir } })
      await busy(h, sid)
      expectSlot(`directory ${JSON.stringify(dir)}`, sid, "UserPromptSubmit", want)
    }
    // No session info at all: the directory OpenCode was started in.
    const h2 = await plugin("C:\\work\\Fallback")
    await busy(h2, "ses_nodir")
    expectSlot("fallback directory", "ses_nodir", "UserPromptSubmit", "Fallback")
    const h3 = await factory({})
    await busy(h3, "ses_noinit")
    expectSlot("no directory at all", "ses_noinit", "UserPromptSubmit", null)
  }

  // 14. Garbage: nothing escapes, nothing is written.
  {
    const before = walk(DIR).sort().join("\n")
    const h = await plugin()
    const junk = [undefined, null, {}, { event: null }, { event: {} }, { event: { type: 5 } },
      { event: { type: "session.status" } }, { event: { type: "session.status", properties: null } },
      { event: { type: "session.status", properties: { sessionID: "ses_j" } } },
      { event: { type: "session.status", properties: { sessionID: "ses_j", status: "busy" } } },
      { event: { type: "session.status", properties: { sessionID: "ses_j", status: { type: "nonsense" } } } },
      { event: { type: "session.created", properties: { info: "ses_j" } } },
      { event: { type: "session.deleted", properties: { info: null } } },
      { event: { type: "session.error", properties: { sessionID: "ses_j2", error: null } } },
      { event: { type: "permission.asked", properties: { sessionID: null, id: "per" } } },
      { event: { type: "plugin.added", properties: { id: "x" } } },
      { event: { type: "message.part.delta", properties: { sessionID: "ses_j", delta: "swordfish" } } }]
    for (const j of junk) {
      try {
        await h.event(j)
      } catch (e) {
        fail(`event ${JSON.stringify(j)} threw ${e}`)
      }
    }
    for (const j of [undefined, null, {}, "ses_j", { sessionID: 7 }]) {
      for (const k of ["tool.execute.before", "tool.execute.after"]) {
        try {
          await h[k](j, undefined)
        } catch (e) {
          fail(`${k} ${JSON.stringify(j)} threw ${e}`)
        }
      }
    }
    try {
      await plugin(null)
      await factory(undefined)
      await factory(null)
    } catch (e) {
      fail(`the factory threw on a bad input: ${e}`)
    }
    const after = walk(DIR).filter((f) => f !== "ses_j2.state").sort().join("\n")
    if (after !== before) fail(`garbage changed the state dir:\n${after}\nvs\n${before}`)
    // A session.error with no error object is still an error.
    expectSlot("error with no body", "ses_j2", "StopFailure")
  }

  // 15. An unwritable state directory: silent, and no throw.
  if (process.platform !== "win32" && process.getuid && process.getuid() !== 0) {
    const h = await plugin()
    fs.chmodSync(DIR, 0o500)
    try {
      await busy(h, "ses_ro")
      await asked(h, "ses_ro", "per_ro")
      await deleted(h, "ses_ro")
    } catch (e) {
      fail(`a read-only state dir threw: ${e}`)
    } finally {
      fs.chmodSync(DIR, 0o700)
    }
  }

  // Nothing the events carried, beyond ids and the directory's last segment.
  for (const f of walk(DIR)) {
    const full = p(f)
    if (fs.statSync(full).isDirectory()) continue
    const text = fs.readFileSync(full, "utf8")
    for (const s of SECRETS) if (text.includes(s) || f.includes(s)) fail(`${f} carries payload content: ${s}`)
    if (/\.tmp$/.test(f)) fail(`a temp file was left behind: ${f}`)
  }
}

// --- fixture ---------------------------------------------------------------

async function fixture() {
  makeDir()
  const h = await plugin()
  for (const sid of ["ses_run", "ses_wait", "ses_idle", "ses_fail", "ses_abort", "ses_long", "ses_par", "ses_gone"]) {
    await created(h, sid)
    await busy(h, sid)
  }
  await asked(h, "ses_wait", "per_1")
  await idle(h, "ses_idle")
  await error(h, "ses_fail", "APIError")
  await idle(h, "ses_fail")
  await idle(h, "ses_fail")
  await error(h, "ses_abort", "MessageAbortedError")
  await idle(h, "ses_abort")
  await retry(h, "ses_long", 3600 * 1000)
  await created(h, "ses_kid1", { parentID: "ses_par" })
  await busy(h, "ses_kid1")
  await created(h, "ses_kid2", { parentID: "ses_par" })
  await busy(h, "ses_kid2")
  await idle(h, "ses_kid2")
  await deleted(h, "ses_gone")
}

// --- main ------------------------------------------------------------------

const mode = process.argv[2] || "check"
trap()
let crashed = null
try {
  if (mode === "check") await check()
  else if (mode === "exit") {
    const h = await plugin()
    await created(h, "ses_exit")
    await busy(h, "ses_exit")
    await idle(h, "ses_exit")
    await created(h, "ses_exitkid", { parentID: "ses_exit" })
  } else if (mode === "fixture") await fixture()
  else crashed = `unknown mode ${mode}`
} catch (e) {
  crashed = `the driver crashed: ${e && e.stack ? e.stack : e}`
}
untrap()
for (const [stream, text] of printed) failures.push(`the plugin wrote to ${stream}: ${JSON.stringify(text)}`)
if (crashed) failures.push(crashed)
if (failures.length) {
  realErr(failures.map((f) => `FAIL ${f}\n`).join(""))
  process.exit(1)
}
if (mode === "fixture") {
  realOut("ready\n")
  process.stdin.resume()
  process.stdin.on("end", () => process.exit(0))
}
