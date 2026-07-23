"""Read the Claude OAuth token and fetch/map usage: the mapping converts the
Anthropic usage JSON into the flat usage message the board renders."""
import json
from datetime import datetime, timezone
import os
import subprocess
import urllib.request

from pc import protocol

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def _window(raw, key):
    w = raw.get(key) or {}
    return float(w.get("utilization", 0.0)), w.get("resets_at", "")


def _secs_until(iso: str, now: datetime) -> int:
    """Seconds from `now` until the ISO-8601 instant `iso`. -1 if unknown.

    -1 rather than 0 for missing/malformed input: 0 would render as "resets
    now", which is a confident lie. -1 lets the display say "--".
    """
    if not iso:
        return -1
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return -1
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0, int((t - now).total_seconds()))


def map_usage(raw: dict, now: datetime = None) -> dict:
    """Convert a /api/oauth/usage JSON dict to a 'usage' protocol message."""
    now = now or datetime.now(timezone.utc)
    session_pct, session_reset = _window(raw, "five_hour")
    weekly_pct, weekly_reset = _window(raw, "seven_day")
    models = []

    def add_model(name, pct):
        if name and not any(m["name"] == name for m in models):
            models.append({"name": name, "weekly_pct": float(pct)})

    # Current accounts report per-model weekly usage inside limits[] as
    # weekly_scoped entries (scope.model.display_name, e.g. "Fable") --
    # verified against the live endpoint 2026-07-17; the flat
    # seven_day_<model> windows are all null there.
    for lim in raw.get("limits") or []:
        if (isinstance(lim, dict) and lim.get("kind") == "weekly_scoped"
                and "percent" in lim):
            model = (lim.get("scope") or {}).get("model") or {}
            add_model((model.get("display_name") or "").lower(), lim["percent"])
    # Older accounts: flat per-model windows.
    for key, name in (("seven_day_fable", "fable"),
                      ("seven_day_sonnet", "sonnet"),
                      ("seven_day_opus", "opus")):
        w = raw.get(key)
        if isinstance(w, dict) and "utilization" in w:
            add_model(name, w["utilization"])
    return protocol.usage(
        session_pct, session_reset, weekly_pct, weekly_reset, models,
        session_resets_in_s=_secs_until(session_reset, now),
        weekly_resets_in_s=_secs_until(weekly_reset, now),
    )


def read_token():
    """OAuth access token from macOS Keychain or ~/.claude/.credentials.json."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, check=True).stdout.strip()
        tok = json.loads(out).get("claudeAiOauth", {}).get("accessToken")
        if tok:
            return tok
    except Exception:
        pass
    try:
        with open(os.path.expanduser("~/.claude/.credentials.json")) as f:
            return json.load(f).get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def fetch_usage_raw(token: str, timeout=15) -> dict:
    """GET /api/oauth/usage. Raises urllib.error.HTTPError on non-2xx."""
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "User-Agent": "claude-usage-display/0.2",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)
