# Clauge web bridge (optional)

Usage that happens in a browser tab is the one thing neither the CLI hook nor
the desktop cache can see. This extension closes that gap by forwarding the
numbers claude.ai already sends back to your browser.

It is **optional**. Clauge works without it; installing it only makes the
weekly dial more accurate for people who also use claude.ai in a browser.

## What it sends, and where

To `http://127.0.0.1:9877/usage` on your own machine, and nowhere else:

```json
{"session_pct": 25, "weekly_pct": 42, "session_resets_at": 1787320800}
```

That is the whole payload. The extension reads **response headers only** — no
page content, no message text, no cookies, no tokens — and it never issues a
request of its own. Everything it reports is observed from responses the page
was already receiving, which is what keeps Clauge's "no outbound polling"
property true with the extension installed.

## Installing it

1. Open `chrome://extensions`.
2. Turn on **Developer mode**.
3. Choose **Load unpacked** and select this `extension/` directory.

The daemon picks it up on its own — there is nothing to configure. If Clauge
is not running, the extension silently does nothing.

## Is it working?

```
clauge status
```

The `Browser` line answers it directly, and it distinguishes the three cases
that matter:

| Line | What it means |
|---|---|
| `extension not seen` | Not installed, or the daemon is not running |
| `extension running, but none of N responses carried rate-limit headers` | Installed and working; claude.ai simply does not send what this needs |
| `extension running, N rate-limit headers seen but none usable` | Headers exist but do not yield a percentage — worth reporting |
| `extension working (N usage reports)` | Numbers are reaching the panel |

The extension reports these counts every 30 seconds whether or not it found
anything, because a silent extension that matched nothing looks exactly like
one that was never installed. The report carries two integers and nothing else.

## What it actually reports today

**Nothing, and that is now measured rather than assumed.** Driven against the
real site on 2026-08-27 — a page load, three reloads and a complete message
turn — it observed **178 responses from claude.ai and matched zero of them**:

```
Browser  extension running, but none of 178 responses carried rate-limit headers
```

So claude.ai does not send its usage figures in response headers, and this
extension has nothing to forward. Install it if you like; it will tell you the
same thing, on your own traffic, in one line. The panel keeps its numbers from
the status line and the desktop cache either way.

`docs/next-steps.md` section A has the reasoning, including why a page-context
probe cannot answer this and why `extraHeaders` would not change the result.

## If it reports nothing

That is the designed failure, not a crash. The rate-limit header names are not
a documented contract, so this matches them by shape rather than by exact name.
If claude.ai emits nothing that matches, the extension says so through the
status line above, the panel falls back to the CLI hook and the desktop cache,
and nothing guesses.
