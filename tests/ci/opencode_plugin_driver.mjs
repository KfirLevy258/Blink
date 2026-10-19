// Drives tools/blink-opencode.js the way OpenCode does, without OpenCode.
//
//   node tests/ci/opencode_plugin_driver.mjs check     every scenario, asserted
//   node tests/ci/opencode_plugin_driver.mjs exit      one session, then exit
//   node tests/ci/opencode_plugin_driver.mjs fixture   a known desk, held open
//   node tests/ci/opencode_plugin_driver.mjs instances several plugin instances,
//                                                      then exit
//   node tests/ci/opencode_plugin_driver.mjs nodir     two instances with no
//                                                      directory, then exit
//   node tests/ci/opencode_plugin_driver.mjs exit2     one OpenCode 2.0 turn,
//                                                      then exit
//   node tests/ci/opencode_plugin_driver.mjs fixture2  `fixture`'s desk, built
//                                                      from 2.0 events
//   node tests/ci/opencode_plugin_driver.mjs leak2     the resubscribe leak
//                                                      alone (also in `check`)
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
// OpenCode 2.0 drives the same file through its default export instead: one
// `setup(ctx)` per project location, each handed the whole service's event
// stream as `ctx.event.subscribe({signal})`, an async iterable of
// {id, created, type, location?, data}. The 2.0 half of this driver fakes that
// stream with channel() below; see THE 2.0 SHAPE in the plugin.
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
import { syncBuiltinESMExports } from "node:module"

const HERE = path.dirname(fileURLToPath(import.meta.url))
// Overridable so a test can hand this a deliberately broken copy and see the
// check fail: a checker nobody has seen fail is not known to check anything.
const PLUGIN = process.env.BLINK_OPENCODE_PLUGIN || path.join(HERE, "..", "..", "tools", "blink-opencode.js")
const DIR = path.join(os.homedir(), ".blink", "state-opencode")
const SECRETS = ["swordfish", "rm -rf", "Which colour", "secret-body", "hunter2", "/x/y/"]

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

// The 2.0 entry point runs a loop setup never awaits; a rejection escaping it
// would be printed by node into whatever the service logs to, and under a
// stricter runtime would end the process. Recorded as a failure rather than
// left to node's default, so it is reported with the rest.
process.on("unhandledRejection", (e) => fail(`unhandled rejection: ${e && e.stack ? e.stack : e}`))

// --- the plugin, driven ----------------------------------------------------

// The exports, held to exactly what both OpenCode lines load, because each
// way of getting them wrong has a cost nobody sees:
//   - Exactly two. 1.x releases that fall back to calling every export would
//     call any third one as a plugin factory.
//   - `default` a plain object. 2.0's module schema rejects anything whose
//     typeof is not "object" (effect 4.0.1, SchemaAST.ts), so a function
//     carrying the same keys is not loaded at all.
//   - `id` and `setup` on it. 2.0 skips a module without {id, setup} with
//     nothing but a log warning in a service log nobody reads -- which is how
//     2.0.0 left the light dark for five weeks.
//   - `server` the 1.x factory itself: 1.x from 1.3.4 calls that in
//     preference to the named export.
//   - No `effect`: 2.0 tries that variant first, and it would win over setup.
//   - No `tui`: a default carrying both `tui` and `server` throws on 1.x.
const mod = await import(pathToFileURL(PLUGIN).href)
{
  const exported = Object.keys(mod).sort()
  const d = mod.default
  const problems = []
  if (JSON.stringify(exported) !== JSON.stringify(["BlinkStatePlugin", "default"])) {
    problems.push(`the plugin must export exactly BlinkStatePlugin and default, got ${JSON.stringify(exported)}`)
  } else {
    if (typeof mod.BlinkStatePlugin !== "function") problems.push("BlinkStatePlugin is not a function")
    if (d === null || typeof d !== "object") {
      problems.push(`the default export must be a plain object, got ${d === null ? "null" : typeof d}`)
    } else {
      if (d.id !== "blink") problems.push(`default.id is ${JSON.stringify(d.id)}, wanted "blink"`)
      if (d.server !== mod.BlinkStatePlugin) problems.push("default.server is not BlinkStatePlugin")
      if (typeof d.setup !== "function") problems.push("default.setup is not a function")
      if ("effect" in d) problems.push("the default export has an effect key, which 2.0 would take over setup")
      if ("tui" in d) problems.push("the default export has a tui key, which throws beside server on 1.x")
    }
  }
  if (problems.length) {
    untrap()
    realErr(problems.map((m) => `FAIL ${m}\n`).join(""))
    process.exit(1)
  }
}
const factory = mod.BlinkStatePlugin

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

// --- the plugin, driven as OpenCode 2.0 drives it ---------------------------

// A fake of ctx.event.subscribe: every subscribe() opens one async iterator,
// and push() delivers to every iterator open at that moment, as the 2.0
// service fans its one stream out to each subscriber. An abort ends the
// iterator -- a pending next() resolves done -- which is how the plugin's
// cleanup is seen to work. end() and fail() close every open iterator without
// an abort: a stream that simply ends, and the one encode failure that ends
// the stream for every copy at once (packages/plugin/src/promise/adapter.ts),
// which is what the plugin's resubscribe loop exists for.
function channel() {
  const subs = new Set()
  const ch = {
    calls: 0,
    signals: [],
    subscribe({ signal } = {}) {
      ch.calls++
      ch.signals.push(signal)
      const queue = []
      const waiting = []
      let done = false
      let error = null
      const onAbort = () => sub.finish(null)
      const sub = {
        deliver(v) {
          if (done) return
          if (waiting.length) waiting.shift().resolve({ value: v, done: false })
          else queue.push(v)
        },
        finish(err) {
          if (done) return
          done = true
          error = err
          subs.delete(sub)
          // Our own listener goes with the iterator, so that what is left on
          // the signal afterwards is the plugin's alone.
          if (signal) signal.removeEventListener("abort", onAbort)
          for (const w of waiting.splice(0)) {
            if (err) w.reject(err)
            else w.resolve({ value: undefined, done: true })
          }
        },
      }
      subs.add(sub)
      if (signal) {
        if (signal.aborted) sub.finish(null)
        else signal.addEventListener("abort", onAbort)
      }
      return {
        [Symbol.asyncIterator]() {
          return this
        },
        next() {
          if (queue.length) return Promise.resolve({ value: queue.shift(), done: false })
          if (done) return error ? Promise.reject(error) : Promise.resolve({ value: undefined, done: true })
          return new Promise((resolve, reject) => waiting.push({ resolve, reject }))
        },
        return() {
          sub.finish(null)
          return Promise.resolve({ value: undefined, done: true })
        },
      }
    },
    push(ev) {
      for (const s of [...subs]) s.deliver(ev)
    },
    fail(err = new Error("encode failed")) {
      for (const s of [...subs]) s.finish(err)
    },
    end() {
      for (const s of [...subs]) s.finish(null)
    },
    get closed() {
      return subs.size === 0
    },
  }
  return ch
}

// Every cleanup setup2 handed back, run at the end of a mode. A stream left
// open holds nothing that keeps node alive, but a setup whose subscribe
// threw sits in the plugin's backoff timer, which does.
const cleanups = []

// One 2.0 location: setup called as 2.0's plugin host calls it.
async function setup2(ch, dir = "/x/y/MyProj", m = mod) {
  const cleanup = await m.default.setup({ location: { directory: dir }, event: { subscribe: ch.subscribe } })
  if (typeof cleanup === "function") cleanups.push(cleanup)
  return cleanup
}

// The plugin's loop handles a delivered event in microtasks; two turns of
// setImmediate are past all of them.
const flush = () => new Promise((r) => setImmediate(() => setImmediate(r)))

async function send(ch, ...events) {
  for (const e of events) ch.push(e)
  await flush()
}

// Event ids are unique for the whole run, not per case: the tracker is one
// per process and remembers every handled id on globalThis, so a reused id
// would be dropped as a duplicate and the case would test nothing.
let evtN = 0
function v2(type, data, loc) {
  return { id: `evt_v2_${++evtN}`, created: Date.now(), type, data, ...(loc ? { location: { directory: loc } } : {}) }
}
const LONG = 3600 * 1000
const e2 = {
  created: (sid, extra = {}) =>
    v2("session.created", { sessionID: sid, title: "swordfish", location: { directory: "/x/y/MyProj" }, ...extra }),
  started: (sid, loc) => v2("session.execution.started", { sessionID: sid }, loc),
  step: (sid) => v2("session.step.started", { sessionID: sid }),
  tool: (sid) => v2("session.tool.called", { sessionID: sid, tool: "bash", input: { command: "rm -rf /" } }),
  succeeded: (sid) => v2("session.execution.succeeded", { sessionID: sid }),
  failed: (sid) => v2("session.execution.failed", { sessionID: sid, error: { message: "secret-body" } }),
  interrupted: (sid, reason) => v2("session.execution.interrupted", { sessionID: sid, reason }),
  retry: (sid, inMs) =>
    v2("session.retry.scheduled", { sessionID: sid, attempt: 1, at: Date.now() + inMs, error: { message: "secret-body" } }),
  asked: (sid, id) =>
    v2("permission.asked", { id, sessionID: sid, permission: "bash", resources: ["rm -rf /"], message: "Which colour?" }),
  replied: (sid, requestID) => v2("permission.replied", { sessionID: sid, requestID, reply: "once" }),
  form: (sid, id) => v2("form.created", { form: { id, sessionID: sid, title: "Which colour?" } }),
  formReplied: (sid, id) => v2("form.replied", { sessionID: sid, id, answer: "hunter2" }),
  formCancelled: (sid, id) => v2("form.cancelled", { sessionID: sid, id }),
  deleted: (sid) => v2("session.deleted", { sessionID: sid }),
}

// Every kind of 2.0 event the plugin writes for, against one session id.
function everyEvent2(sid) {
  const kid = `${sid}_kid`
  return [e2.created(sid), e2.started(sid), e2.step(sid), e2.tool(sid), e2.asked(sid, `${sid}_p1`),
    e2.form(sid, `${sid}_f1`), e2.replied(sid, `${sid}_p1`), e2.formReplied(sid, `${sid}_f1`),
    e2.created(kid, { parentID: sid }), e2.started(kid), e2.retry(kid, LONG), e2.retry(sid, LONG),
    e2.failed(sid), e2.started(sid), e2.interrupted(sid, "user"), e2.started(sid), e2.succeeded(sid),
    e2.deleted(sid)]
}

async function until(pred, ms) {
  const end = Date.now() + ms
  while (Date.now() < end) {
    if (pred()) return true
    await new Promise((r) => setTimeout(r, 25))
  }
  return pred()
}

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
    // 2.0 as well: a service left running across the uninstall is the 2.0
    // shape of the same thing, and it outlives every terminal.
    const ch = channel()
    await setup2(ch)
    try {
      await everyEvent(h, "ses_nodir")
      await send(ch, ...everyEvent2("ses_v2nodir"))
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
    await send(ch, e2.created("ses_v2uninst"), e2.started("ses_v2uninst"))
    expectSlot("2.0 before the uninstall", "ses_v2uninst", "UserPromptSubmit")
    fs.rmSync(DIR, { recursive: true, force: true })
    try {
      await everyEvent(h2, "ses_uninst")
      await everyEvent(h2, "ses_uninst2")
      await send(ch, ...everyEvent2("ses_v2uninst"))
      await send(ch, ...everyEvent2("ses_v2uninst2"))
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

  // 16. A rename the platform refuses for a moment. Found in review,
  //     2026-10-19: on Windows, renaming onto a file another process has open
  //     fails with EPERM/EBUSY/EACCES, and the daemon opens every slot on a
  //     two-second poll -- so the event was silently lost. The plugin's
  //     `import * as fs` sees a patched builtin through
  //     syncBuiltinESMExports, which is how this reaches it.
  {
    const real = fs.default.renameSync
    let calls = 0
    const refuse = (code) => {
      calls++
      throw Object.assign(new Error(code), { code })
    }
    const swap = (fn) => {
      fs.default.renameSync = fn
      syncBuiltinESMExports()
    }
    try {
      const h = await plugin()
      // Busy for three tries, then free: the write lands, late but whole.
      swap((...a) => (calls < 3 ? refuse("EPERM") : (calls++, real(...a))))
      await busy(h, "ses_ren")
      if (calls !== 4) fail(`a briefly busy rename was tried ${calls} times, wanted 4`)
      expectSlot("rename retried", "ses_ren", "UserPromptSubmit")
      // Busy for good: given up after five tries, quietly, nothing thrown.
      calls = 0
      swap(() => refuse("EBUSY"))
      await idle(h, "ses_ren")
      if (calls !== 5) fail(`a rename that never frees was tried ${calls} times, wanted 5`)
      expectSlot("rename given up", "ses_ren", "UserPromptSubmit")
      calls = 0
      swap(() => refuse("EACCES"))
      await busy(h, "ses_ren_b")
      if (calls !== 5) fail(`EACCES was tried ${calls} times, wanted 5`)
      // Not a busy file: no retry, a failure is a failure.
      calls = 0
      swap(() => refuse("ENOSPC"))
      await busy(h, "ses_ren_c")
      if (calls !== 1) fail(`ENOSPC was retried: ${calls} tries`)
      expectFile("rename refused", "ses_ren_b.state", false)
      expectFile("rename refused", "ses_ren_c.state", false)
    } catch (e) {
      fail(`a refused rename threw: ${e}`)
    } finally {
      swap(real)
    }
  }

  await check2()

  // Nothing the events carried, beyond ids and the directory's last segment.
  for (const f of walk(DIR)) {
    const full = p(f)
    if (fs.statSync(full).isDirectory()) continue
    const text = fs.readFileSync(full, "utf8")
    for (const s of SECRETS) if (text.includes(s) || f.includes(s)) fail(`${f} carries payload content: ${s}`)
    if (/\.tmp$/.test(f)) fail(`a temp file was left behind: ${f}`)
  }
}

// --- check, OpenCode 2.0 ---------------------------------------------------

// The same file loaded through its default export. One tracker serves every
// setup in this process, so every case below uses session ids of its own
// (ses_v2...), and every event id is fresh (see v2()).
async function check2() {
  const RUN = ["UserPromptSubmit", "PreToolUse", "PostToolUse"]
  const running = (label, sid) => {
    const t = slot(sid)
    const evn = t === null ? null : JSON.parse(t).event
    if (!RUN.includes(evn)) fail(`${label}: ${sid} is ${evn}, wanted a running event`)
  }

  // 17. A normal turn. execution.started fires once per turn and the step and
  //     tool events keep the clock fresh -- throttled like 1.x's busy, so a
  //     step inside REFRESH_MS must not rewrite the slot.
  {
    const ch = channel()
    await setup2(ch)
    await send(ch, e2.created("ses_v2norm"))
    expectFile("2.0 created only", "ses_v2norm.state", false)
    await send(ch, e2.started("ses_v2norm"))
    expectSlot("2.0 started", "ses_v2norm", "UserPromptSubmit")
    const before = ino("ses_v2norm")
    await send(ch, e2.step("ses_v2norm"), e2.tool("ses_v2norm"))
    if (ino("ses_v2norm") !== before) fail("2.0 running events inside the refresh window rewrote the slot")
    await send(ch, e2.succeeded("ses_v2norm"))
    expectSlot("2.0 succeeded", "ses_v2norm", "Stop")
    // A tool event landing after the turn finished must not repaint it.
    await send(ch, e2.tool("ses_v2norm"), e2.step("ses_v2norm"))
    expectSlot("2.0 tool after stop", "ses_v2norm", "Stop")
  }

  // 18. Permissions: the marker follows the open requests, as in 1.x.
  {
    const ch = channel()
    await setup2(ch)
    await send(ch, e2.created("ses_v2perm"), e2.started("ses_v2perm"))
    await send(ch, e2.asked("ses_v2perm", "per_v2_1"))
    expectSlot("2.0 asked", "ses_v2perm", "PermissionRequest")
    expectFile("2.0 asked", "ses_v2perm.waiting")
    await send(ch, e2.replied("ses_v2perm", "per_v2_1"))
    expectSlot("2.0 replied", "ses_v2perm", "PostToolUse")
    expectFile("2.0 replied", "ses_v2perm.waiting", false)
    // Two overlapping: the marker lasts until the LAST is answered.
    await send(ch, e2.asked("ses_v2perm", "per_v2_a"), e2.asked("ses_v2perm", "per_v2_b"))
    await send(ch, e2.replied("ses_v2perm", "per_v2_a"))
    expectFile("2.0 one of two answered", "ses_v2perm.waiting")
    await send(ch, e2.replied("ses_v2perm", "per_v2_b"))
    expectFile("2.0 both answered", "ses_v2perm.waiting", false)
    // An answer to a request this process never saw asked.
    await send(ch, e2.asked("ses_v2perm", "per_v2_c"), e2.replied("ses_v2perm", "per_v2_unknown"))
    expectFile("2.0 stray answer", "ses_v2perm.waiting")
    // The turn finishing with one open: asking nobody anything, and the late
    // answer to it does not repaint the finished turn as running.
    await send(ch, e2.succeeded("ses_v2perm"))
    expectFile("2.0 succeeded with one open", "ses_v2perm.waiting", false)
    await send(ch, e2.replied("ses_v2perm", "per_v2_c"))
    expectSlot("2.0 reply after succeeded", "ses_v2perm", "Stop")
  }

  // 19. Forms: 2.0's questions. The session is named inside data.form, and
  //     "global" is MCP elicitation's owner for a form that belongs to no
  //     session -- it passes the id pattern, so only the plugin's own check
  //     keeps a global.state off the panel.
  {
    const ch = channel()
    await setup2(ch)
    await send(ch, e2.created("ses_v2form"), e2.started("ses_v2form"))
    await send(ch, e2.form("ses_v2form", "frm_v2_1"))
    expectSlot("2.0 form", "ses_v2form", "PermissionRequest")
    expectFile("2.0 form", "ses_v2form.waiting")
    await send(ch, e2.formReplied("ses_v2form", "frm_v2_1"))
    expectFile("2.0 form replied", "ses_v2form.waiting", false)
    expectSlot("2.0 form replied", "ses_v2form", "PostToolUse")
    await send(ch, e2.form("ses_v2form", "frm_v2_2"))
    await send(ch, e2.formCancelled("ses_v2form", "frm_v2_2"))
    expectFile("2.0 form cancelled", "ses_v2form.waiting", false)
    await send(ch, e2.form("global", "frm_v2_g"), e2.formReplied("global", "frm_v2_g"),
      e2.started("global"), e2.asked("global", "per_v2_g"))
    expectFile("2.0 global form", "global.state", false)
    expectFile("2.0 global form", "global.waiting", false)
  }

  // 20. Retries. data.at is the epoch ms the retry fires at; one more than a
  //     minute away is the quota case and reads as failed.
  {
    const ch = channel()
    await setup2(ch)
    await send(ch, e2.created("ses_v2retry"), e2.started("ses_v2retry"))
    await send(ch, e2.retry("ses_v2retry", 1000))
    running("2.0 short retry", "ses_v2retry")
    await send(ch, e2.retry("ses_v2retry", LONG))
    expectSlot("2.0 long retry", "ses_v2retry", "StopFailure")
    // The retry fired and a step began: the failure is lifted.
    await send(ch, e2.step("ses_v2retry"))
    expectSlot("2.0 step after long retry", "ses_v2retry", "PreToolUse")
    // A long retry, then the attempts run out.
    await send(ch, e2.retry("ses_v2retry", LONG))
    await send(ch, e2.failed("ses_v2retry"))
    expectSlot("2.0 retries exhausted", "ses_v2retry", "StopFailure")
    // A real failure is not lifted by a straggling step.
    await send(ch, e2.step("ses_v2retry"), e2.tool("ses_v2retry"))
    expectSlot("2.0 step after failure", "ses_v2retry", "StopFailure")
    // A long retry arriving after the turn's Stop changes nothing.
    await send(ch, e2.started("ses_v2retry"), e2.succeeded("ses_v2retry"))
    await send(ch, e2.retry("ses_v2retry", LONG))
    expectSlot("2.0 long retry after stop", "ses_v2retry", "Stop")
  }

  // 21. Interrupted, by reason. user (Esc) and inactivity end the turn;
  //     shutdown keeps it claimed to resume, and superseded is followed by the
  //     replacing prompt's own execution.started.
  {
    const ch = channel()
    await setup2(ch)
    for (const reason of ["user", "inactivity"]) {
      const sid = `ses_v2int_${reason}`
      await send(ch, e2.created(sid), e2.started(sid), e2.asked(sid, `per_v2_int_${reason}`))
      await send(ch, e2.interrupted(sid, reason))
      expectSlot(`2.0 interrupted ${reason}`, sid, "Interrupt")
      expectFile(`2.0 interrupted ${reason}`, `${sid}.waiting`, false)
    }
    await send(ch, e2.created("ses_v2int_shut"), e2.started("ses_v2int_shut"))
    await send(ch, e2.interrupted("ses_v2int_shut", "shutdown"))
    expectSlot("2.0 interrupted shutdown", "ses_v2int_shut", "UserPromptSubmit")
    await send(ch, e2.created("ses_v2int_sup"), e2.started("ses_v2int_sup"))
    await send(ch, e2.interrupted("ses_v2int_sup", "superseded"))
    await send(ch, e2.started("ses_v2int_sup"))
    expectSlot("2.0 superseded then started", "ses_v2int_sup", "UserPromptSubmit")
  }

  // 22. Subagents: a file under the parent, never a slot of their own.
  {
    const ch = channel()
    await setup2(ch)
    await send(ch, e2.created("ses_v2par"), e2.started("ses_v2par"))
    await send(ch, e2.created("ses_v2kid1", { parentID: "ses_v2par" }))
    expectFile("2.0 child created", "ses_v2par/ses_v2kid1")
    await send(ch, e2.started("ses_v2kid1"), e2.step("ses_v2kid1"), e2.tool("ses_v2kid1"))
    expectFile("2.0 child started", "ses_v2kid1.state", false)
    await send(ch, e2.asked("ses_v2kid1", "per_v2_kid"))
    expectFile("2.0 child asks", "ses_v2par.waiting")
    expectFile("2.0 child asks", "ses_v2kid1.waiting", false)
    expectSlot("2.0 child asks", "ses_v2par", "PermissionRequest")
    await send(ch, e2.replied("ses_v2kid1", "per_v2_kid"))
    expectFile("2.0 child answered", "ses_v2par.waiting", false)
    // A subagent waiting out a quota is its whole session waiting.
    await send(ch, e2.retry("ses_v2kid1", LONG))
    expectSlot("2.0 child long retry", "ses_v2par", "StopFailure")
    await send(ch, e2.step("ses_v2kid1"))
    expectSlot("2.0 child step after long retry", "ses_v2par", "PreToolUse")
    await send(ch, e2.succeeded("ses_v2kid1"))
    expectFile("2.0 child succeeded", "ses_v2par/ses_v2kid1", false)
    // A child interrupted by the service shutting down is still claimed.
    await send(ch, e2.created("ses_v2kid2", { parentID: "ses_v2par" }), e2.started("ses_v2kid2"))
    await send(ch, e2.interrupted("ses_v2kid2", "shutdown"))
    expectFile("2.0 child interrupted shutdown", "ses_v2par/ses_v2kid2")
    await send(ch, e2.interrupted("ses_v2kid2", "user"))
    expectFile("2.0 child interrupted user", "ses_v2par/ses_v2kid2", false)
    for (const f of walk(DIR)) {
      if (/ses_v2kid[^/\\]*\.(state|waiting|ended)$/.test(f)) fail(`a 2.0 subagent got a session file: ${f}`)
    }

    // 23. Deletion: the slot, marker and agent dir go, a tombstone stays,
    //     and the session speaking again takes the tombstone back.
    await send(ch, e2.created("ses_v2kid3", { parentID: "ses_v2par" }), e2.asked("ses_v2par", "per_v2_del"))
    await send(ch, e2.deleted("ses_v2par"))
    expectFile("2.0 deleted", "ses_v2par.state", false)
    expectFile("2.0 deleted", "ses_v2par.waiting", false)
    expectFile("2.0 deleted", "ses_v2par", false)
    expectFile("2.0 deleted", "ses_v2par.ended")
    await send(ch, e2.started("ses_v2par"))
    expectFile("2.0 resumed", "ses_v2par.ended", false)
  }

  // 24. Names. Measured in a real 2.0.32 service: execution.* events carry no
  //     `location`, so the name has to come from session.created's own
  //     location and be remembered. An event envelope's location names a
  //     session nothing else has; with neither, the key is left out.
  {
    const ch = channel()
    await setup2(ch)
    await send(ch, e2.created("ses_v2name"), e2.started("ses_v2name"))
    expectSlot("2.0 name from created", "ses_v2name", "UserPromptSubmit", "MyProj")
    await send(ch, e2.started("ses_v2loc", "/x/y/Loc"))
    expectSlot("2.0 name from the envelope", "ses_v2loc", "UserPromptSubmit", "Loc")
    await send(ch, e2.started("ses_v2anon"))
    expectSlot("2.0 no name at all", "ses_v2anon", "UserPromptSubmit", null)
  }

  // 25. Every location's setup is handed the WHOLE service's stream, so the
  //     same event reaches the tracker once per open project. It must be
  //     handled once: written once, counted once.
  {
    const a = channel()
    const b = channel()
    await setup2(a, "/x/y/A")
    await setup2(b, "/x/y/B")
    await send(a, e2.created("ses_v2dup"))
    const st = e2.started("ses_v2dup")
    a.push(st)
    await flush()
    const first = ino("ses_v2dup")
    b.push(st)
    await flush()
    if (ino("ses_v2dup") !== first) fail("2.0: the same event through two setups was written twice")
    // A lagging copy replaying an event already handled -- the review's case
    // (2026-10-20): execution.started arriving at the second copy after the
    // first had already seen the turn finish.
    const st2 = e2.started("ses_v2dup")
    await send(a, st2, e2.succeeded("ses_v2dup"))
    await send(b, st2)
    expectSlot("2.0 lagging duplicate started", "ses_v2dup", "Stop")
    // The same asked through both, then one reply: a double count would
    // leave the marker behind.
    await send(a, e2.started("ses_v2dup"))
    const ask = e2.asked("ses_v2dup", "per_v2_dup")
    await send(a, ask)
    await send(b, ask)
    await send(a, e2.replied("ses_v2dup", "per_v2_dup"))
    expectFile("2.0 duplicate asked, one reply", "ses_v2dup.waiting", false)
    // ... and an asked replayed after its reply does not bring it back.
    const ask2 = e2.asked("ses_v2dup", "per_v2_dup2")
    await send(a, ask2, e2.replied("ses_v2dup", "per_v2_dup2"))
    await send(b, ask2)
    expectFile("2.0 lagging duplicate asked", "ses_v2dup.waiting", false)

    // 26. Asked through one location, answered through another: one tracker,
    //     so the marker clears. With a tracker per setup it stayed for good.
    await send(a, e2.created("ses_v2cross"), e2.started("ses_v2cross"))
    await send(a, e2.asked("ses_v2cross", "per_v2_cross"))
    expectFile("2.0 asked via A", "ses_v2cross.waiting")
    await send(b, e2.replied("ses_v2cross", "per_v2_cross"))
    expectFile("2.0 replied via B", "ses_v2cross.waiting", false)
    expectSlot("2.0 replied via B", "ses_v2cross", "PostToolUse")
  }

  // 27. A second evaluation of the same file -- 2.0 imports it afresh per
  //     location -- finds the first one's tracker on globalThis rather than
  //     making a second writer, and adds no 'exit' listener of its own.
  {
    const KEY = Symbol.for("blink.opencode.tracker")
    const held = globalThis[KEY]
    if (!held || typeof held.revision !== "string" || !held.tracker) {
      fail(`2.0: no tracker on globalThis, got ${JSON.stringify(held && Object.keys(held))}`)
    }
    const exits = process.listenerCount("exit")
    const mod2 = await import(pathToFileURL(PLUGIN).href + "?copy=2")
    if (mod2 === mod) fail("2.0: the second import was not a second evaluation")
    const a = channel()
    const b = channel()
    await setup2(a, "/x/y/A", mod)
    await setup2(b, "/x/y/B", mod2)
    const now = globalThis[KEY]
    if (now !== held || (now && held && (now.tracker !== held.tracker || now.revision !== held.revision))) {
      fail("2.0: a second copy of the same file made a tracker of its own")
    }
    if (process.listenerCount("exit") !== exits) {
      fail(`2.0: a second copy added 'exit' listeners: ${exits} -> ${process.listenerCount("exit")}`)
    }
    await send(a, e2.created("ses_v2copy"), e2.started("ses_v2copy"), e2.asked("ses_v2copy", "per_v2_copy"))
    await send(b, e2.replied("ses_v2copy", "per_v2_copy"))
    expectFile("2.0 asked in one copy, answered in the other", "ses_v2copy.waiting", false)
  }

  // 28. Cleanup aborts the subscription, and a stopped copy writes nothing.
  {
    const ch = channel()
    const cleanup = await setup2(ch)
    if (typeof cleanup !== "function") {
      fail(`2.0 setup returned ${typeof cleanup}, wanted a cleanup function`)
    } else {
      cleanup()
      const sig = ch.signals[0]
      if (!sig || !sig.aborted) fail("2.0 cleanup did not abort the subscription's signal")
      if (!ch.closed) fail("2.0 cleanup left the stream open")
      await send(ch, e2.created("ses_v2gone"), e2.started("ses_v2gone"))
      expectFile("2.0 after cleanup", "ses_v2gone.state", false)
      try {
        cleanup()
      } catch (e) {
        fail(`2.0 cleanup twice threw: ${e}`)
      }
      await new Promise((r) => setTimeout(r, 50))
      if (ch.calls !== 1) fail(`2.0 resubscribed after cleanup: ${ch.calls} subscribes`)
    }
  }

  // 29. A stream that ends, or fails, without our abort is resubscribed. One
  //     encode failure ends the stream for every copy at once, and without
  //     this the light stayed dark until something reloaded the plugin
  //     (review, 2026-10-20). Real timers: the first wait is a second.
  {
    const ch = channel()
    const cleanup = await setup2(ch)
    ch.end()
    if (!(await until(() => ch.calls >= 2, 1500))) fail(`2.0: an ended stream was not resubscribed (${ch.calls})`)
    await send(ch, e2.created("ses_v2resub"), e2.started("ses_v2resub"))
    expectSlot("2.0 after a resubscribe", "ses_v2resub", "UserPromptSubmit")
    // Events flowed again, so the wait is back to a second.
    ch.fail()
    if (!(await until(() => ch.calls >= 3, 1500))) fail(`2.0: a failed stream was not resubscribed (${ch.calls})`)
    await send(ch, e2.succeeded("ses_v2resub"))
    expectSlot("2.0 after a failed stream", "ses_v2resub", "Stop")
    cleanup()
  }

  // 30. Bad contexts: setup must return, never throw, whatever it is handed.
  //     OpenCode is waiting on it to finish loading the plugin.
  for (const [label, ctx] of [["undefined", undefined], ["{}", {}], ["{event:{}}", { event: {} }],
    ["a throwing subscribe", { event: { subscribe() { throw new Error("no stream") } } }],
    ["a subscribe returning nothing", { event: { subscribe() { return undefined } } }]]) {
    try {
      const cleanup = await mod.default.setup(ctx)
      if (typeof cleanup !== "function") fail(`2.0 setup(${label}) returned ${typeof cleanup}`)
      else cleanups.push(cleanup)
    } catch (e) {
      fail(`2.0 setup(${label}) threw: ${e}`)
    }
  }

  // 31. Garbage and hostile ids: nothing escapes, nothing is written.
  {
    const ch = channel()
    await setup2(ch)
    const before = walk(DIR).sort().join("\n")
    const bad = ["../x", "..", ".", "", "a/b", "a\\b", 'a"b', " ses", "x".repeat(65), null, 5, {}, ["ses_arr"]]
    const junk = [null, undefined, 42, "session.execution.started", {}, { type: 5, data: { sessionID: "ses_v2j" } },
      { type: "session.execution.started" }, { type: "session.execution.started", data: null },
      { type: "session.execution.started", data: "ses_v2j" },
      { id: 7, type: "session.execution.started", data: { sessionID: null } },
      v2("form.created", { form: null }), v2("form.created", { form: "ses_v2j" }), v2("form.created", {}),
      v2("session.created", { sessionID: "ses_v2junkkid", parentID: "../x" }),
      v2("message.part.delta", { sessionID: "ses_v2j", delta: "swordfish" }),
      v2("session.status", { sessionID: "ses_v2j", status: { type: "busy" } })]
    for (const id of bad) {
      junk.push(e2.created(id), e2.started(id), e2.step(id), e2.tool(id), e2.retry(id, LONG), e2.asked(id, "per_v2_bad"),
        e2.replied(id, "per_v2_bad"), e2.form(id, "frm_v2_bad"), e2.formReplied(id, "frm_v2_bad"),
        e2.failed(id), e2.interrupted(id, "user"), e2.succeeded(id), e2.deleted(id),
        e2.created("ses_v2junkkid2", { parentID: id }))
    }
    await send(ch, ...junk)
    const after = walk(DIR).sort().join("\n")
    if (after !== before) fail(`2.0 garbage changed the state dir:\n${after}\nvs\n${before}`)
    // "../x" from the state directory is ~/.blink/x*: the shim's traversal.
    const blink = fs.readdirSync(path.dirname(DIR))
    if (blink.join() !== "state-opencode") fail(`a 2.0 hostile id reached ~/.blink: ${blink.join(", ")}`)
  }

  for (const c of cleanups.splice(0)) c()
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

// `fixture`'s desk, state for state, built from 2.0 events: waiting is a
// form, idle a succeeded turn, failed an execution.failed, the abort an
// interrupt by the user. The same session ids, so the Python side holds both
// to one expectation.
async function fixture2() {
  makeDir()
  const ch = channel()
  await setup2(ch)
  for (const sid of ["ses_run", "ses_wait", "ses_idle", "ses_fail", "ses_abort", "ses_long", "ses_par", "ses_gone"]) {
    await send(ch, e2.created(sid), e2.started(sid))
  }
  await send(ch,
    e2.form("ses_wait", "frm_1"),
    e2.succeeded("ses_idle"),
    e2.failed("ses_fail"),
    e2.interrupted("ses_abort", "user"),
    e2.retry("ses_long", LONG),
    e2.created("ses_kid1", { parentID: "ses_par" }), e2.started("ses_kid1"),
    e2.created("ses_kid2", { parentID: "ses_par" }), e2.started("ses_kid2"), e2.succeeded("ses_kid2"),
    e2.deleted("ses_gone"))
}

// One 2.0 turn, then the process exits: the service going away is every
// session it held ending, from the one 2.0 ender on globalThis.
async function exit2() {
  const ch = channel()
  await setup2(ch)
  await send(ch, e2.created("ses_v2exit"), e2.started("ses_v2exit"), e2.succeeded("ses_v2exit"),
    e2.created("ses_v2exitkid", { parentID: "ses_v2exit" }), e2.started("ses_v2exitkid"))
}

// Found while writing these tests, 2026-10-20, and fixed in the plugin the same
// day: blinkV2's pause() added an "abort" listener to the subscription's
// signal on every resubscribe and removed it only on abort, so a timer that
// simply fired left its listener -- and its closure -- on the signal for the
// life of the setup. Measured: 13 resubscribes, 13 listeners. A service whose
// stream fails now and then for weeks grew without bound. Node prints no
// MaxListeners warning for an AbortSignal (checked under 26.8.1), so this is
// memory, not noise, and only a count can catch it. Run at the end of
// `check`; takes about three seconds.
async function leak2() {
  const { getEventListeners } = await import("node:events")
  const ch = channel()
  const cleanup = await setup2(ch)
  for (let i = 0; i < 2; i++) {
    const want = ch.calls + 1
    ch.end()
    if (!(await until(() => ch.calls >= want, 1500))) fail(`stream ${i} was not resubscribed`)
    // An event resets the plugin's wait to its one-second minimum.
    await send(ch, e2.step(`ses_v2leak${i}`))
  }
  const sig = ch.signals[ch.signals.length - 1]
  // The channel's own listener is the one for the open iterator; anything
  // past it is the plugin's, and with no pause in progress there should be none.
  const n = getEventListeners(sig, "abort").length
  if (n > 1) fail(`after 2 resubscribes the signal holds ${n} abort listeners, wanted 1 (pause() never removes its own)`)
  cleanup()
}

// --- instances -------------------------------------------------------------

// Found in review, 2026-10-19: every factory call left its exit handler, and
// with it its sessions and requests maps, in a module-level Set for the life
// of the process; at exit the stale instances ran sessionEnd as well. Now one
// handler per directory, the newest replacing its predecessor, at most 64.
//   - ses_stale: written by an instance for /x/y/Same that a later instance
//     for the same directory replaced. Its slot must survive the exit.
//   - ses_live, ses_other: the current instances for /x/y/Same and
//     /x/y/Other. Tombstoned.
//   - ses_d0 .. ses_d64: one instance each for 65 more directories. The
//     oldest, ses_d0 -- evicted along with /x/y/Same's and /x/y/Other's
//     instances, which are older still -- keeps its slot; the 64 newest are
//     tombstoned.
async function instances() {
  makeDir()
  const turn = async (h, sid) => {
    await busy(h, sid)
    await idle(h, sid)
  }
  await turn(await plugin("/x/y/Same"), "ses_stale")
  await turn(await plugin("/x/y/Same"), "ses_live")
  await turn(await plugin("/x/y/Other"), "ses_other")
  if (process.argv[3] === "many") {
    for (let i = 0; i <= 64; i++) await turn(await plugin(`/x/y/D${i}`), `ses_d${i}`)
  }
}

// --- nodir -----------------------------------------------------------------

// Third review, 2026-10-19: an instance whose input carries no `directory`
// was keyed "" in the exit-handler map, so every such instance shared one
// key and each evicted the last -- at exit only the newest ended anything.
// Two of them here, ses_nodir1 and ses_nodir2: both must be tombstoned.
async function nodir() {
  makeDir()
  for (const sid of ["ses_nodir1", "ses_nodir2"]) {
    const h = await factory({ client: {}, $: () => {} })
    await busy(h, sid)
    await idle(h, sid)
  }
}

// --- main ------------------------------------------------------------------

const mode = process.argv[2] || "check"
trap()
let crashed = null
try {
  if (mode === "check") {
    await check()
    await leak2()
  }
  else if (mode === "exit") {
    const h = await plugin()
    await created(h, "ses_exit")
    await busy(h, "ses_exit")
    await idle(h, "ses_exit")
    await created(h, "ses_exitkid", { parentID: "ses_exit" })
  } else if (mode === "fixture") await fixture()
  else if (mode === "fixture2") await fixture2()
  else if (mode === "exit2") await exit2()
  else if (mode === "leak2") await leak2()
  else if (mode === "instances") await instances()
  else if (mode === "nodir") await nodir()
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
if (mode === "fixture" || mode === "fixture2") {
  realOut("ready\n")
  process.stdin.resume()
  process.stdin.on("end", () => process.exit(0))
}
