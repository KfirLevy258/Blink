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

## If it reports nothing

That is the designed failure. The rate-limit header names are not a documented
contract, so this matches them by shape rather than by exact name. If claude.ai
stops emitting anything that matches, the extension goes quiet and the panel
falls back to the CLI hook and the desktop cache. It will not guess.
