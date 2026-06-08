# Live Claude UI

A tiny local web page showing your **Claude Code session limit (5-hour window)**
and **weekly limit (7-day window)** live — the same numbers as the `/usage`
command, as two gauges with reset countdowns.

![two gauges: Session 5h and Weekly 7d](#)

## Run

```bash
npm start            # or: node server.js
# → open http://127.0.0.1:4317   (set PORT to change)
```

Click **Log in with Anthropic** → a real Anthropic login page opens in your
browser → after authorizing, copy the code it shows you and paste it into the
page. The app stores its own access + refresh token and refreshes it
automatically. You only log in once.

## How it works

- A zero-dependency Node server (built-in `http`/`crypto`/`fetch`) polls
  `GET https://api.anthropic.com/api/oauth/usage` every **180 seconds** with the
  required headers (`Authorization`, `anthropic-beta: oauth-2025-04-20`,
  `User-Agent: claude-code/<version>`).
- It caches the result; the browser page polls that local cache every ~15s and
  ticks the reset countdowns every second.
- Response fields used: `five_hour` → Session gauge, `seven_day` → Weekly gauge
  (plus `seven_day_sonnet` / `seven_day_opus` shown when present).

Tokens are stored at `~/.config/live-claude-ui/tokens.json` with `0600`
permissions and are never logged or committed.

## Caveats (please read)

- This uses an **undocumented** Anthropic OAuth endpoint and reuses Claude
  Code's public OAuth client. Anthropic may change the flow or revoke
  third-party refresh tokens at any time. If that happens the page shows a
  "log in again" prompt rather than breaking.
- The usage endpoint is **aggressively rate-limited**; do not lower the 180s
  poll interval, or you will get persistent `429`s.
- This is a personal, single-account, single-machine tool. Treat the stored
  token like a password.

## Status indicators

| Dot | Meaning |
|-----|---------|
| green  | Connected — fresh data |
| amber  | Rate-limited or stale — showing last good values |
| red    | Error |
| grey   | Logged out |
