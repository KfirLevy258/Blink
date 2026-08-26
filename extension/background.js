// Clauge web bridge.
//
// Reports claude.ai usage numbers to the Clauge daemon on this machine, so the
// desk gauge reflects usage that happened in a browser tab -- the one blind
// spot the CLI hook and the desktop cache both admit to.
//
// What this does NOT do, deliberately:
//
//   - it never issues a request of its own. Everything here is observation of
//     responses the page was already going to receive, which is what keeps the
//     product's "no outbound polling" promise true with the extension
//     installed. There is no timer and no fetch against claude.ai anywhere in
//     this file.
//   - it never reads a body, a cookie, a token or any page content. Response
//     HEADERS only, and only the numeric ones.
//   - it sends to 127.0.0.1 and nowhere else.
//
// A caveat worth stating in the code rather than only in a README: the header
// names below are NOT a documented contract. They are matched by shape, and if
// claude.ai stops emitting anything that matches, this extension reports
// nothing at all. That is the intended failure -- silence, and the panel falls
// back to the other sources -- rather than a guess.

const ENDPOINT = "http://127.0.0.1:9877/usage";
const DIAG_ENDPOINT = "http://127.0.0.1:9877/diag";

// How often to tell the daemon what we can see, whether or not we found
// anything. This is the whole answer to "is the extension working?", and it
// has to be reportable WITHOUT finding usage numbers -- a silent extension
// that matched nothing looks exactly like one that is not installed.
const DIAG_INTERVAL_MS = 30000;

let responsesSeen = 0;      // responses from claude.ai we looked at
let headersMatched = 0;     // ...of which carried something rate-limit shaped
let lastDiagAt = 0;

// Never report more often than this. A busy tab fires many requests per turn
// and the panel updates once a minute; posting per request would be pure noise.
const MIN_INTERVAL_MS = 5000;

let lastPostAt = 0;
let lastPayloadKey = "";

// Header shapes, not header names. Anything carrying a rate-limit figure tends
// to spell it one of these ways; matching the shape means a rename that keeps
// the convention keeps working, and a rename that abandons it goes silent
// rather than wrong.
const RE_LIMIT = /rate[-_]?limit.*limit$|^x?-?ratelimit-limit/i;
const RE_REMAINING = /rate[-_]?limit.*remaining$|^x?-?ratelimit-remaining/i;
const RE_RESET = /rate[-_]?limit.*reset$|^x?-?ratelimit-reset/i;
const RE_USED_PCT = /rate[-_]?limit.*(used|utilization).*(pct|percent)/i;

// Which window a header is about. Headers that distinguish them usually say so
// in the name; one that does not is treated as the session window, which is
// the one the large dial shows.
function windowOf(name) {
  if (/week|7d|seven/i.test(name)) return "weekly";
  return "session";
}

function numeric(value) {
  const n = Number(String(value).trim());
  return Number.isFinite(n) ? n : null;
}

// Pull whatever numbers are present out of a response's headers.
function readHeaders(headers) {
  const acc = {
    session: { limit: null, remaining: null, reset: null, pct: null },
    weekly: { limit: null, remaining: null, reset: null, pct: null },
  };
  let sawAnything = false;

  for (const h of headers || []) {
    const name = h.name || "";
    const n = numeric(h.value);
    if (n === null) continue;
    const w = acc[windowOf(name)];

    if (RE_USED_PCT.test(name)) {
      w.pct = n;
      sawAnything = true;
    } else if (RE_LIMIT.test(name)) {
      w.limit = n;
      sawAnything = true;
    } else if (RE_REMAINING.test(name)) {
      w.remaining = n;
      sawAnything = true;
    } else if (RE_RESET.test(name)) {
      w.reset = n;
      sawAnything = true;
    }
  }
  return sawAnything ? acc : null;
}

// used% from limit/remaining, or a percentage that was given directly.
function usedPct(w) {
  if (w.pct !== null && w.pct >= 0 && w.pct <= 100) return w.pct;
  if (w.limit !== null && w.remaining !== null && w.limit > 0) {
    const pct = ((w.limit - w.remaining) / w.limit) * 100;
    // Clamped only against arithmetic noise at the edges. A figure far
    // outside the range means the two headers were not the pair assumed here,
    // and that is reported as unknown rather than as a saturated dial.
    if (pct < -1 || pct > 101) return null;
    return Math.min(100, Math.max(0, pct));
  }
  return null;
}

// A reset header may be an absolute epoch or seconds-from-now. Both appear in
// the wild; tell them apart by magnitude rather than by trusting one form.
function resetEpoch(value) {
  if (value === null) return null;
  const nowS = Math.floor(Date.now() / 1000);
  if (value > 1000000000) return value;        // already an epoch
  if (value > 0) return nowS + value;          // a duration
  return null;
}

function buildPayload(acc) {
  const s = usedPct(acc.session);
  const w = usedPct(acc.weekly);
  if (s === null && w === null) return null;

  const payload = {};
  if (s !== null) payload.session_pct = s;
  if (w !== null) payload.weekly_pct = w;

  const sr = resetEpoch(acc.session.reset);
  const wr = resetEpoch(acc.weekly.reset);
  if (sr !== null) payload.session_resets_at = sr;
  if (wr !== null) payload.weekly_resets_at = wr;
  return payload;
}

// Report what we have observed, found or not. Deliberately carries no page
// data of any kind: two counters, so the daemon can say "the extension is
// running and claude.ai sends nothing that looks like a rate limit" instead
// of the user having to open a service-worker console to find that out.
async function reportDiagnostics() {
  const now = Date.now();
  if (now - lastDiagAt < DIAG_INTERVAL_MS) return;
  lastDiagAt = now;
  try {
    await fetch(DIAG_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ responses: responsesSeen, matched: headersMatched }),
    });
  } catch (e) {
    // The daemon is not running. Ordinary, and not worth a console error on
    // every response.
  }
}

async function report(payload) {
  // Deduplicate before throttling. Identical numbers are worth nothing to the
  // panel however much time has passed, and the daemon treats a repeat as a
  // fresh reading -- which would keep a stale figure looking live.
  const key = JSON.stringify(payload);
  if (key === lastPayloadKey) return;

  const now = Date.now();
  if (now - lastPostAt < MIN_INTERVAL_MS) return;

  lastPostAt = now;
  lastPayloadKey = key;
  try {
    await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: key,
    });
  } catch (e) {
    // The daemon is not running, or the port is taken. Both are ordinary --
    // most people will have the browser open without the gauge plugged in --
    // and neither is worth a console error on every turn. Forget the payload
    // so the next identical one is retried once the daemon is back.
    lastPayloadKey = "";
  }
}

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    responsesSeen++;
    const acc = readHeaders(details.responseHeaders);
    if (acc) {
      headersMatched++;
      const payload = buildPayload(acc);
      if (payload) report(payload);
    }
    // Always, matched or not. The absence of a match is the finding.
    reportDiagnostics();
  },
  { urls: ["https://claude.ai/*"] },
  ["responseHeaders"]
);

// --- testability -----------------------------------------------------------
//
// The header matching is the part of this extension most likely to be wrong,
// and the only way to find out in a browser is to have claude.ai actually send
// something. That is not a reason to leave it unchecked: the parsing is pure
// functions over a header array, and tests/extension/test_background.mjs runs
// them under Node against fixtures.
//
// Guarded so Chrome never sees it -- a service worker has no `module`.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { windowOf, numeric, readHeaders, usedPct, resetEpoch,
                     buildPayload, RE_LIMIT, RE_REMAINING, RE_RESET,
                     RE_USED_PCT };
}
