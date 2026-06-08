// Token storage: persists the OAuth token set to a 0600 file in the user's
// config dir. Tokens are secrets — never logged.
import { homedir } from "node:os";
import { join } from "node:path";
import { mkdir, readFile, writeFile, chmod } from "node:fs/promises";

const DIR = join(homedir(), ".config", "live-claude-ui");
const FILE = join(DIR, "tokens.json");

// Refresh when fewer than 5 minutes of validity remain.
const EXPIRY_SKEW_MS = 5 * 60 * 1000;

/** @typedef {{accessToken:string, refreshToken:string, expiresAt:number}} Tokens */

/** Load tokens, or null if none stored yet. */
export async function loadTokens() {
  try {
    const raw = await readFile(FILE, "utf8");
    const t = JSON.parse(raw);
    if (t && t.accessToken && t.refreshToken) return t;
    return null;
  } catch {
    return null;
  }
}

/** Persist tokens with owner-only permissions. */
export async function saveTokens(tokens) {
  await mkdir(DIR, { recursive: true, mode: 0o700 });
  await writeFile(FILE, JSON.stringify(tokens, null, 2), { mode: 0o600 });
  await chmod(FILE, 0o600);
}

/** True when the access token is missing or about to expire. */
export function isExpiring(tokens) {
  if (!tokens || !tokens.expiresAt) return true;
  return Date.now() + EXPIRY_SKEW_MS >= tokens.expiresAt;
}

export const TOKENS_PATH = FILE;
