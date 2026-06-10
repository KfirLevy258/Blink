"""Read the Claude OAuth token and fetch/map usage. Token-read logic mirrors
claude_usage_test.py; mapping converts the Anthropic JSON to a flat usage message."""
import json
import os
import subprocess
import urllib.request

from pc import protocol

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def _window(raw, key):
    w = raw.get(key) or {}
    return float(w.get("utilization", 0.0)), w.get("resets_at", "")


def map_usage(raw: dict) -> dict:
    """Convert a /api/oauth/usage JSON dict to a 'usage' protocol message."""
    session_pct, session_reset = _window(raw, "five_hour")
    weekly_pct, weekly_reset = _window(raw, "seven_day")
    models = []
    for key, name in (("seven_day_sonnet", "sonnet"), ("seven_day_opus", "opus")):
        w = raw.get(key)
        if isinstance(w, dict) and "utilization" in w:
            models.append({"name": name, "weekly_pct": float(w["utilization"])})
    return protocol.usage(session_pct, session_reset, weekly_pct, weekly_reset, models)


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
