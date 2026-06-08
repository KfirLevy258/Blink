// Fetch and normalize the authoritative usage windows from Anthropic's
// undocumented OAuth usage endpoint (the same data the /usage command shows).
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const USAGE_URL = "https://api.anthropic.com/api/oauth/usage";
const FALLBACK_VERSION = "2.1.168";

let cachedUA = null;

/**
 * Build the required User-Agent. Without `claude-code/<version>` the endpoint
 * lands in an aggressively rate-limited bucket, so this header is mandatory.
 */
async function userAgent() {
  if (cachedUA) return cachedUA;
  let version = FALLBACK_VERSION;
  try {
    const { stdout } = await execFileAsync("claude", ["--version"], { timeout: 5000 });
    const m = stdout.match(/(\d+\.\d+\.\d+)/);
    if (m) version = m[1];
  } catch {
    // claude not on PATH — fall back to a recent version string.
  }
  cachedUA = `claude-code/${version}`;
  return cachedUA;
}

function windowOf(node) {
  if (!node || typeof node.utilization !== "number") return null;
  return { pct: node.utilization, resetsAt: node.resets_at ?? null };
}

/**
 * Fetch usage. Returns:
 *  - { ok:true, data:{...} }
 *  - { ok:false, status:"unauthorized" }  (401 — caller should refresh)
 *  - { ok:false, status:"rate_limited" }  (429)
 *  - { ok:false, status:"error", message } (network / other)
 */
export async function fetchUsage(accessToken) {
  let res;
  try {
    res = await fetch(USAGE_URL, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": await userAgent(),
        "Content-Type": "application/json",
      },
    });
  } catch (e) {
    return { ok: false, status: "error", message: e.message };
  }

  if (res.status === 401) return { ok: false, status: "unauthorized" };
  if (res.status === 429) return { ok: false, status: "rate_limited" };
  if (!res.ok) return { ok: false, status: "error", message: `HTTP ${res.status}` };

  let json;
  try {
    json = await res.json();
  } catch (e) {
    return { ok: false, status: "error", message: "bad JSON" };
  }

  return {
    ok: true,
    data: {
      session: windowOf(json.five_hour),
      weekly: windowOf(json.seven_day),
      weeklyOpus: windowOf(json.seven_day_opus),
      weeklySonnet: windowOf(json.seven_day_sonnet),
      extraUsage: json.extra_usage ?? null,
      fetchedAt: Date.now(),
    },
  };
}
