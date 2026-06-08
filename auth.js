// OAuth (Authorization Code + PKCE) for the Claude Code OAuth client.
//
// The Claude Code client only permits the hosted copy/paste callback, so this
// is a manual-code flow: build an authorize URL, the user logs in on
// Anthropic's page and is shown a code, then we exchange that code for tokens.
import crypto from "node:crypto";

const CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e";
const AUTHORIZE_URL = "https://claude.ai/oauth/authorize";
const TOKEN_URL = "https://console.anthropic.com/v1/oauth/token";
const REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback";
const SCOPE = "org:create_api_key user:profile user:inference";

function base64url(buf) {
  return buf.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * Begin a login: returns { url, verifier }.
 * Caller must keep `verifier` to complete the exchange.
 * Per the Claude Code flow, `state` equals the PKCE verifier.
 */
export function beginLogin() {
  const verifier = base64url(crypto.randomBytes(32));
  const challenge = base64url(crypto.createHash("sha256").update(verifier).digest());
  const params = new URLSearchParams({
    code: "true",
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: REDIRECT_URI,
    scope: SCOPE,
    code_challenge: challenge,
    code_challenge_method: "S256",
    state: verifier,
  });
  return { url: `${AUTHORIZE_URL}?${params}`, verifier };
}

function toTokenSet(json) {
  return {
    accessToken: json.access_token,
    refreshToken: json.refresh_token,
    expiresAt: Date.now() + Number(json.expires_in ?? 3600) * 1000,
  };
}

/**
 * Exchange the pasted authorization code for tokens.
 * The copy/paste code is often of the form "<code>#<state>"; split it.
 */
export async function exchangeCode(rawCode, verifier) {
  const [code, state] = String(rawCode).trim().split("#");
  const res = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      grant_type: "authorization_code",
      code,
      code_verifier: verifier,
      client_id: CLIENT_ID,
      redirect_uri: REDIRECT_URI,
      state: state ?? verifier,
    }),
  });
  if (!res.ok) {
    throw new Error(`Token exchange failed: HTTP ${res.status} ${await res.text().catch(() => "")}`);
  }
  return toTokenSet(await res.json());
}

/** Refresh an access token using the refresh token. */
export async function refreshTokens(refreshToken) {
  const res = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      grant_type: "refresh_token",
      refresh_token: refreshToken,
      client_id: CLIENT_ID,
    }),
  });
  if (!res.ok) {
    throw new Error(`Token refresh failed: HTTP ${res.status}`);
  }
  const json = await res.json();
  const next = toTokenSet(json);
  // Anthropic may not return a new refresh token; keep the old one if so.
  if (!next.refreshToken) next.refreshToken = refreshToken;
  return next;
}
