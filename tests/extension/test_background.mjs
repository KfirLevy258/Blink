/* Tests for the browser extension's header parsing.
 *
 *   node tests/extension/test_background.mjs
 *
 * This is the one component whose real input nobody has ever seen: claude.ai's
 * rate-limit headers are not a documented contract, the extension matches them
 * by SHAPE, and the only way to observe the real ones is to have the app make
 * an authenticated API call. A page-context probe cannot settle it either --
 * chrome.webRequest sees headers that fetch() hides.
 *
 * So the shape matching is tested against fixtures instead. That does not prove
 * the extension will find claude.ai's headers; it proves that IF a header of a
 * given shape arrives, the extension does the right thing with it -- and that
 * it stays silent rather than guessing when one does not.
 */
import { createRequire } from "node:module";
import assert from "node:assert/strict";

const require = createRequire(import.meta.url);
globalThis.chrome = { webRequest: { onHeadersReceived: { addListener() {} } } };
const bg = require("../../extension/background.js");

let pass = 0, fail = 0;
function test(name, fn) {
  try { fn(); pass++; console.log("PASS: " + name); }
  catch (e) { fail++; console.log("FAIL: " + name + "\n      " + e.message); }
}
const H = (o) => Object.entries(o).map(([name, value]) => ({ name, value }));

// --- the shapes it is meant to catch -------------------------------------

test("classic x-ratelimit triple is recognised", () => {
  const acc = bg.readHeaders(H({
    "x-ratelimit-limit": "100",
    "x-ratelimit-remaining": "25",
    "x-ratelimit-reset": "1787700000",
  }));
  assert.ok(acc, "nothing matched");
  assert.equal(bg.usedPct(acc.session), 75);
});

test("a percentage given directly is used as-is", () => {
  const acc = bg.readHeaders(H({ "ratelimit-used-percent": "42" }));
  assert.equal(bg.usedPct(acc.session), 42);
});

test("weekly headers land in the weekly window", () => {
  const acc = bg.readHeaders(H({
    "x-ratelimit-limit-week": "1000",
    "x-ratelimit-remaining-week": "100",
  }));
  assert.equal(bg.usedPct(acc.weekly), 90);
  assert.equal(bg.usedPct(acc.session), null);
});

test("underscored and unprefixed spellings both match", () => {
  for (const n of ["rate_limit_limit", "ratelimit-limit"]) {
    const acc = bg.readHeaders(H({ [n]: "10", [n.replace(/limit$/, "remaining")]: "5" }));
    assert.ok(acc, n + " did not match");
  }
});

// --- and, more importantly, what it must NOT do --------------------------

test("an ordinary response produces nothing at all", () => {
  assert.equal(bg.readHeaders(H({
    "content-type": "application/json", "date": "x", "server": "cloudflare",
  })), null);
});

test("a non-numeric value is ignored rather than guessed at", () => {
  assert.equal(bg.readHeaders(H({ "x-ratelimit-limit": "unlimited" })), null);
});

test("limit without remaining yields no percentage", () => {
  const acc = bg.readHeaders(H({ "x-ratelimit-limit": "100" }));
  assert.equal(bg.usedPct(acc.session), null);
});

test("a zero limit does not divide by zero", () => {
  const acc = bg.readHeaders(H({ "x-ratelimit-limit": "0", "x-ratelimit-remaining": "0" }));
  assert.equal(bg.usedPct(acc.session), null);
});

test("remaining above limit is refused, not clamped to a lie", () => {
  const acc = bg.readHeaders(H({ "x-ratelimit-limit": "10", "x-ratelimit-remaining": "999" }));
  assert.equal(bg.usedPct(acc.session), null);
});

test("a percentage outside 0-100 is refused", () => {
  const acc = bg.readHeaders(H({ "ratelimit-used-percent": "400" }));
  assert.equal(bg.usedPct(acc.session), null);
});

// --- reset times ----------------------------------------------------------

test("an absolute epoch passes through", () => {
  assert.equal(bg.resetEpoch(1787700000), 1787700000);
});

test("a duration is turned into an epoch", () => {
  const now = Math.floor(Date.now() / 1000);
  const got = bg.resetEpoch(3600);
  assert.ok(Math.abs(got - (now + 3600)) <= 2, "got " + got);
});

test("a nonsense reset is dropped", () => {
  assert.equal(bg.resetEpoch(0), null);
  assert.equal(bg.resetEpoch(null), null);
});

// --- the payload ----------------------------------------------------------

test("a payload carries only numbers, never page data", () => {
  const acc = bg.readHeaders(H({
    "x-ratelimit-limit": "100", "x-ratelimit-remaining": "25",
    "x-ratelimit-reset": "1787700000",
  }));
  const p = bg.buildPayload(acc);
  assert.deepEqual(Object.keys(p).sort(), ["session_pct", "session_resets_at"]);
  for (const v of Object.values(p)) assert.equal(typeof v, "number");
});

test("no usable window means no payload at all", () => {
  const acc = bg.readHeaders(H({ "x-ratelimit-reset": "1787700000" }));
  assert.equal(bg.buildPayload(acc), null);
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
