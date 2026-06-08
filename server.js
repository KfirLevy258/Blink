// Local web server: serves the dashboard, runs the OAuth login flow, and polls
// the usage endpoint on a background timer (>=180s to avoid rate limiting).
import http from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join, extname } from "node:path";

import { loadTokens, saveTokens, isExpiring } from "./tokens.js";
import { beginLogin, exchangeCode, refreshTokens } from "./auth.js";
import { fetchUsage } from "./usage.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const PUBLIC = join(__dirname, "public");

const PORT = Number(process.env.PORT) || 4317;
const HOST = "127.0.0.1";
const POLL_INTERVAL_MS = 180_000; // endpoint is safe at >=180s

// ---- in-memory state -------------------------------------------------------
let tokens = null; // current token set
let pendingVerifier = null; // PKCE verifier between /auth/login and /auth/exchange
let lastData = null; // last successful usage payload
let connState = "logged_out"; // logged_out | connected | rate_limited | stale | error
let lastError = null;

// ---- token helpers ---------------------------------------------------------
async function ensureFreshToken() {
  if (!tokens) return false;
  if (!isExpiring(tokens)) return true;
  try {
    tokens = await refreshTokens(tokens.refreshToken);
    await saveTokens(tokens);
    return true;
  } catch (e) {
    // Refresh token revoked/expired — force re-login.
    tokens = null;
    connState = "logged_out";
    lastError = "Session expired — please log in again.";
    return false;
  }
}

// ---- the poll --------------------------------------------------------------
async function poll() {
  if (!tokens) {
    connState = "logged_out";
    return;
  }
  if (!(await ensureFreshToken())) return;

  let result = await fetchUsage(tokens.accessToken);

  // One retry path: 401 means the access token is stale — refresh and retry.
  if (!result.ok && result.status === "unauthorized") {
    try {
      tokens = await refreshTokens(tokens.refreshToken);
      await saveTokens(tokens);
      result = await fetchUsage(tokens.accessToken);
    } catch {
      tokens = null;
      connState = "logged_out";
      lastError = "Session expired — please log in again.";
      return;
    }
  }

  if (result.ok) {
    lastData = result.data;
    connState = "connected";
    lastError = null;
  } else if (result.status === "rate_limited") {
    connState = "rate_limited"; // keep lastData
  } else if (result.status === "unauthorized") {
    tokens = null;
    connState = "logged_out";
    lastError = "Authorization rejected — please log in again.";
  } else {
    connState = "stale"; // network/other — keep lastData
    lastError = result.message || "Could not reach Anthropic.";
  }
}

// ---- http helpers ----------------------------------------------------------
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };

function sendJSON(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(body) });
  res.end(body);
}

async function serveStatic(res, file) {
  try {
    const buf = await readFile(join(PUBLIC, file));
    res.writeHead(200, { "content-type": MIME[extname(file)] || "application/octet-stream" });
    res.end(buf);
  } catch {
    res.writeHead(404).end("Not found");
  }
}

function readBody(req) {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => {
      try {
        resolve(data ? JSON.parse(data) : {});
      } catch {
        resolve({});
      }
    });
  });
}

// ---- routes ----------------------------------------------------------------
const server = http.createServer(async (req, res) => {
  const { method } = req;
  const url = new URL(req.url, `http://${HOST}:${PORT}`);
  const path = url.pathname;

  try {
    if (method === "GET" && path === "/") return serveStatic(res, "index.html");
    if (method === "GET" && (path === "/app.js" || path === "/styles.css")) {
      return serveStatic(res, path.slice(1));
    }

    if (method === "GET" && path === "/api/usage") {
      return sendJSON(res, 200, {
        authed: !!tokens,
        state: connState,
        error: lastError,
        data: lastData,
        pollIntervalMs: POLL_INTERVAL_MS,
      });
    }

    if (method === "POST" && path === "/auth/login") {
      const { url: authUrl, verifier } = beginLogin();
      pendingVerifier = verifier;
      return sendJSON(res, 200, { url: authUrl });
    }

    if (method === "POST" && path === "/auth/exchange") {
      const { code } = await readBody(req);
      if (!code || !pendingVerifier) {
        return sendJSON(res, 400, { ok: false, error: "Missing code or no login in progress." });
      }
      try {
        tokens = await exchangeCode(code, pendingVerifier);
        pendingVerifier = null;
        await saveTokens(tokens);
        await poll(); // populate immediately
        return sendJSON(res, 200, { ok: true });
      } catch (e) {
        return sendJSON(res, 400, { ok: false, error: e.message });
      }
    }

    if (method === "POST" && path === "/auth/logout") {
      tokens = null;
      lastData = null;
      connState = "logged_out";
      lastError = null;
      return sendJSON(res, 200, { ok: true });
    }

    res.writeHead(404).end("Not found");
  } catch (e) {
    sendJSON(res, 500, { error: e.message });
  }
});

// ---- boot ------------------------------------------------------------------
(async () => {
  tokens = await loadTokens();
  if (tokens) await poll();
  setInterval(poll, POLL_INTERVAL_MS);
  server.listen(PORT, HOST, () => {
    console.log(`\n  Live Claude UI  →  http://${HOST}:${PORT}\n`);
    if (!tokens) console.log("  Not logged in yet — open the page and click “Log in”.\n");
  });
})();
