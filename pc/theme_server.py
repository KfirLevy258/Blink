"""The theme builder, served to this computer's own browser and nobody else's.

`blink theme` runs it in the foreground; the daemon runs it on a thread when
somebody taps Settings > Theme > "Edit on computer" on the board. Either way
it is stdlib http.server on 127.0.0.1, an ephemeral port, and a page that is
the whole builder in one file (pc/theme_builder.html) with nothing fetched
from anywhere else -- no fonts, no CDN, so it works on a desk with no
network and leaks nothing to one.

Who may talk to it. A port on loopback is reachable by every program on the
machine and, through the browser, by every web page the owner has open: a
page on any site can POST to http://127.0.0.1:<port>/ by guessing the port,
and a page on a name its owner points at 127.0.0.1 (DNS rebinding) can even
read the answers. So:

  - the link that is opened carries a one-time token (`k`). It is exchanged,
    once, for a session cookie that is HttpOnly and SameSite=Strict, and the
    browser is redirected to "/" so the token is not left in the address bar
    or the history. A copy of the link used a second time is refused.
  - every path -- the page included -- needs that cookie. Without it the
    answer is 403 and nothing is read or written.
  - the Host header must be 127.0.0.1:<port> or localhost:<port>, which is
    what defeats rebinding: the attacker's page sends its own name.
  - a POST with an Origin header must come from this server's own origin.
  - nothing is logged. BaseHTTPRequestHandler prints every request line to
    stderr by default, and the first one carries the token; under the daemon
    that is a log file the README asks customers to send us.

When it stops. Save writes theme.json and stops. Cancel, closing the tab (the
page says so on pagehide, and a reload inside the grace takes it back --
CLOSE_GRACE_S, or RELOAD_GRACE_S when this page had only just been served),
or IDLE_S with no edit, write a cancel for the daemon -- the board goes back
to its stored theme -- and stop.
"""
import hmac
import http.client
import http.server
import json
import os
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser

from pc import (logo_encode, logo_preview, private_file, settings_ops,
                status_page, theme, update, usage_refresh, user_config,
                wifi_pair)

IDLE_S = 600            # 10 minutes with no edit (plan, revision 2026-10-25)
# A reload fires pagehide too, then loads the page again, and the builder
# must not end in between. It waited 5 s for every close, and the owner
# closed a tab and watched the board's slot stay on "Editing on your
# computer" for about 6 s (2026-10-31). The wait is now short for a close
# and long only where the close looks like a reload:
#
#   - A reload's navigation reaches this server (GET /) before the old page
#     is unloaded, because a browser unloads the page it leaves only once
#     the new one's response has arrived. So a /api/closed that comes within
#     RELOAD_WINDOW_S of a page being served is taken for a reload and given
#     RELOAD_GRACE_S, the old 5 s. Where a browser unloads first and asks
#     after, the GET / lands later than the close and touch() clears it.
#   - Any other close waits CLOSE_GRACE_S, which has to outlast the page
#     being unloaded and the next one asking, on a loopback socket: the
#     time from a pagehide to the new page's first request is milliseconds
#     of work and a scheduler wake-up. 1.5 s is that with a margin of two
#     orders for a machine that stalls (a Windows VM under load), and
#     short enough that the slot has changed by the time the owner looks.
CLOSE_GRACE_S = 1.5
RELOAD_GRACE_S = 5
RELOAD_WINDOW_S = 3
# How often the deadlines above are checked. The grace is measured in whole
# ticks of this, so at the old 1 s a 5 s grace ended after 5 to 6 s.
TICK_S = 0.25
# How long a one-time link may wait to be used. The CLI prints one for a
# person to click when no browser opened, so it is the builder's own idle
# limit: a link older than that belongs to a builder that would have
# stopped anyway.
LINK_LIFE_S = 600
# Routes that need the board; with no board_ops they answer 409.
PANELS = ("look", "connect", "board")
BOARD_PATHS = ("/api/wifi", "/api/device", "/api/logo", "/api/pref")
MAX_BODY = 4096         # the widest theme is about 200 bytes of JSON
# POST /api/logo only: a GIF as the owner picked it, or 307200 bytes of RGBA.
# Checked against Content-Length before a byte is read; every other route
# keeps MAX_BODY, so the bigger limit buys nothing to a request that is not
# a logo. 2 MiB is above any GIF the encoder would accept (180 frames at
# 320x240 compress far smaller) and below what a hostile page could use to
# hold the handler thread and memory.
MAX_LOGO_BODY = 2 * 1024 * 1024
# How long a closing connection may keep sending what was not read
# (_HTTPServer.shutdown_request).
_LINGER_S = 2.0
_ENCODE_LOCK = threading.Lock()
# What POST /api/pref takes, and the values the board's own checks allow
# (proto.c pref_set_line). view is an integer the board ignores until
# feat/ultra-burn gives it meaning; the page sends 0.
PREF_ENUMS = {"main_src": ("claude", "codex"), "focus": (0, 1),
              "bright": (20, 40, 60, 80, 100)}
# The page's tab id on a preview, save or cancel. A header and not a body
# field because those bodies are the theme itself, which theme.validate
# checks key by key; see _Handler.do_POST.
TAB_HEADER = "X-Blink-Tab"
# The requests BuilderServer.count tallies, for the line it logs when it
# ends. Paths only: nothing a request carries is kept.
_COUNTED = ("/api/ping", "/api/preview", "/api/save", "/api/cancel",
            "/api/closed")
# A builder heartbeat (/api/ping) arrives every 20 s from an open page. A page
# heard within this long is "still open": the Launcher does not put a second
# tab beside it. Chrome slows a hidden tab's timers to once a minute, hence
# the margin over 60 s.
PAGE_ALIVE_S = 75
# Asks for "Edit on computer" within this long of the last open (counted
# from when the open began) are folded into it: answered with that open's
# ok, and no second window. On the Launcher, not the Bridge: a Bridge is
# rebuilt on every reconnect, and a nudged cable must not reset it.
#
# It was 10 s, one browser per 10 s (plan, Answers 2026-10-25), matching the
# board's 10 s lock-out. The board now takes a tap at any time except while
# its ask is in flight, and reads "Edit on computer" again 3 s after "Opened
# on your computer" (owner, 2026-10-29); at 10 s the daemon would have
# answered the first press after that ok with no window at all. At 2 s, a
# press after the note has gone always opens again (the board's 3 s starts
# at the reply, after the open began). A press within 2 s of the open --
# a replayed ask, or a deliberate one on the "Opened" note itself -- is
# folded into it, which is benign: a window opened moments earlier.
#
# That 3 s comeback is now only what firmware from before the hold does.
# A board that hears this daemon's "edit_done" cap holds the slot from ok 1
# until the builder ends (owner, 2026-10-30) and sends no ask meanwhile, so
# for it this limit only meets a replayed ask; it stays 2 s for the older
# boards, which still press again after 3 s.
OPEN_EVERY_S = 2
# The longest a Ctrl-C in `blink theme` waits to be noticed; see
# BuilderServer.wait_interruptibly.
WAIT_SLICE_S = 0.5


def check_token(token):
    """A link token is a str of at least 32 characters. A blank one would
    let a bare /open redeem a session (review of 4.1)."""
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("link token too short")


def cookie_name(port):
    """Named for the port. Cookies are scoped by host, not port, so a second
    builder on 127.0.0.1 -- `blink theme` beside the daemon's own -- would
    otherwise overwrite this one's session in the same browser."""
    return f"blink_theme_{port}"


def _same(good, offered):
    """Constant-time compare, as bytes. hmac.compare_digest raises TypeError
    on a str with anything outside ASCII, and that was a traceback for any
    request carrying one (review, 2026-10-27); every real token is ASCII, so
    such input is simply wrong."""
    try:
        b = offered.encode("ascii")
    except (UnicodeEncodeError, AttributeError):
        return False
    return hmac.compare_digest(good.encode("ascii"), b)
PAGE_FILE = "theme_builder.html"


# Where the page takes its built-in themes. The page used to type its own
# copy of the presets, and the board's palettes changed under it for a whole
# firmware round: Colour-blind safe, Light, Light soft and Retro LCD all
# previewed colours the board never drew (audit, 2026-10-28). Now the server
# writes the list in, from pc/theme_builtins.py, which is generated from the
# firmware's own table, so there is no second copy left to drift.
BUILTINS_MARK = "/*@BUILTINS@*/null"


def builtins_json():
    """The built-ins as the page reads them: the board's tile name, the name
    `blink theme set` takes, and the custom-theme fields."""
    rows = [dict(b, cli=theme.display_name(b["id"]))
            for b in theme.theme_builtins.BUILTINS]
    # No "<" at all, as <, which JSON and the script read the same: so
    # nothing a name might one day hold can close the script element it
    # sits in ("</") or tip the HTML parser into its escaped states
    # ("<!--"). Escaping only "</" left the second open (review of
    # fp/audit-fixes, NICE 3). ensure_ascii, the default, does the rest.
    return json.dumps(rows, separators=(",", ":")).replace("<", "\\u003c")


def page():
    """The builder page. Read from beside this file, which is also where
    tools/build_binary.sh puts it in the frozen app (--add-data ...:pc),
    with the built-in themes written in."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           PAGE_FILE), encoding="utf-8") as f:
        text = f.read()
    if text.count(BUILTINS_MARK) != 1:
        # A page that cannot show the presets is a broken install, and the
        # caller already says so for a missing one.
        raise FileNotFoundError(f"{PAGE_FILE} has no place for the themes")
    return text.replace(BUILTINS_MARK, builtins_json())


# The two faces the board's screen is drawn in (Montserrat is LVGL's font),
# vendored so the page loads nothing from the network: it is served to a desk
# that may be offline and runs inside a frozen app. Latin subset of the
# upstream OFL release; the licence sits beside them.
FONT_NAMES = ("Montserrat-Medium-latin.woff2", "Montserrat-SemiBold-latin.woff2")


def font_path(name):
    """Where a vendored font is, found the way the page itself is: beside
    this file, which in the frozen app is also under sys._MEIPASS/pc (where
    tools/build_binary.sh --add-data puts pc/fonts). The bundle is asked
    first so a frozen build never reads a stray source tree."""
    here = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        bundled = os.path.join(sys._MEIPASS, "pc", "fonts", name)
        if os.path.isfile(bundled):
            return bundled
    return os.path.join(here, "fonts", name)


# ------------------------------------------------------------- opening a browser

# What a desktop session exports that a service started outside it may lack.
_SESSION_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
                 "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                 "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP")


def _run_text(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _linux_session_env(environ, runner):
    """The environment to open a browser in, or None when there is no
    display to open one on.

    Under `blink theme` in a desktop terminal DISPLAY or WAYLAND_DISPLAY is
    simply there. The daemon is a systemd --user unit, though, and a unit
    starts with the user manager's environment, which carries a display only
    once the desktop has imported it (GNOME and KDE do, at login, with
    `systemctl --user import-environment`). show-environment is where it
    then lives, so that is where it is read from.

    With neither, this is a headless machine -- a server, a container, an
    SSH-only box -- and the answer is no. webbrowser would not say no: with
    TERM set it falls back to lynx or w3m, which in a service would run a
    text browser nobody can see, and the board would be told "Opened on your
    computer".
    """
    env = dict(environ)
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return env
    out = runner(["systemctl", "--user", "show-environment"]) or ""
    for line in out.splitlines():
        key, sep, val = line.partition("=")
        if sep and key in _SESSION_VARS and val and not env.get(key):
            env[key] = val
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return env
    return None


def open_url(url, platform=None, environ=None, runner=None, popen=None,
             which=None, browser=None):
    """Open `url` in the logged-in user's browser. True if one was started.

    The same code for `blink theme` and for the daemon, and the daemon is
    what makes it more than webbrowser.open:
      - macOS: the daemon is a LaunchAgent, which runs inside the user's GUI
        session, so webbrowser (osascript `open location`) reaches it.
      - Windows: the daemon is started from the user's Run key, in their
        interactive session, so webbrowser (os.startfile) works there too.
      - Linux: see _linux_session_env. xdg-open is started detached, in its
        own session, so the browser it launches is not a child the service
        manager kills with the daemon on the next restart.
    Every collaborator is injectable; the tests never start a browser.

    Opened is not the same as in front. When "Edit on computer" reshows a
    builder whose tab is behind other windows (contract Addendum 6), the
    browser gets the URL and opens a tab, but whether its window is raised
    is the desktop's decision. Windows' foreground lock lets a process
    that is not in the foreground -- the daemon never is -- start a tab
    but usually only flash the browser on the taskbar; GNOME's focus-
    stealing prevention does the same ("... is ready"); macOS raises it.
    There is no standard-library way past that: AllowSetForegroundWindow
    can only hand on a right the caller holds, and the daemon holds none,
    and anything that could would be a new dependency (CLAUDE.md). So this
    stays the limit, written down, and docs/windows-check.md 10d records
    what each release's Windows pass sees (review of fp/audit-nice r2,
    SHOULD-2).
    """
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    runner = runner or _run_text
    popen = popen or subprocess.Popen
    which = which or shutil.which
    browser = browser or webbrowser.open
    if platform.startswith("linux"):
        env = _linux_session_env(environ, runner)
        if env is None:
            return False
        opener = which("xdg-open")
        if opener:
            try:
                popen([opener, url], env=env, stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      start_new_session=True)
                return True
            except OSError:
                return False
    try:
        return bool(browser(url))
    except Exception:
        return False


# ------------------------------------------------------------- the server

class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "blink"
    # A client that opens a connection and sends nothing would otherwise hold
    # a handler thread for ever.
    timeout = 10
    sys_version = ""

    def log_message(self, *args):        # see the module docstring
        pass

    def log_request(self, *args):
        pass

    @property
    def builder(self):
        return self.server.builder

    # -- answers

    def _send(self, status, body=b"", ctype="text/plain; charset=utf-8",
              headers=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not any(k.lower() == "cache-control" for k, _ in headers):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj).encode("utf-8"),
                   "application/json")

    def _refuse(self, status=403):
        # The prefix is what the page's running_builder matches; the command
        # is the one that started this server.
        cmd = "blink settings" if self.builder.persistent else "blink theme"
        self._send(status, f"Not allowed. Run {cmd} again.\n".encode())

    # -- checks

    def _host_ok(self):
        port = self.builder.port
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}",
                                                f"localhost:{port}")

    def _session_ok(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, val = part.strip().partition("=")
            if (name == cookie_name(self.builder.port)
                    and self.builder.session_valid(val)):
                return True
        return False

    def _origin_ok(self):
        # Required, not merely checked when present: a cross-site form post
        # can carry no Origin at all in some browsers, and `null` (a sandboxed
        # frame) is not ours either (spec 8.3, D35).
        origin = self.headers.get("Origin")
        port = self.builder.port
        return origin in (f"http://127.0.0.1:{port}",
                          f"http://localhost:{port}")

    def _active(self, path):
        """The settings page's idle clock follows any request: a person
        scanning for networks or flipping a switch is not idle, though they
        are editing no theme. Not the page's own heartbeat or its close,
        which say nothing about a person (a ping every 20 s would keep it up
        for good, and a touch would cancel a close's grace)."""
        if self.builder.persistent and path not in ("/api/ping",
                                                    "/api/closed"):
            self.builder.touch()

    # -- GET

    def do_GET(self):
        if not self._host_ok():
            return self._refuse()
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/open":
            k = urllib.parse.parse_qs(url.query).get("k", [""])[0]
            session = self.builder.redeem(k)
            if session is None:
                fate = self.builder.link_fate(k)
                if fate == "replaced":
                    return self._send(403, b"This link was replaced by a"
                                      b" newer one. Use the newest builder"
                                      b" tab.\n")
                if fate == "used":
                    return self._send(403, b"This link was opened already."
                                      b" Use the tab it opened.\n")
                return self._refuse()
            # ?panel= rides the one-time link through the redirect, only if
            # it names one of the three controls: it opens that control.
            panel = urllib.parse.parse_qs(url.query).get("panel", [""])[0]
            where = f"/?panel={panel}" if panel in PANELS else "/"
            return self._send(303, headers=(
                ("Location", where),
                ("Set-Cookie", f"{cookie_name(self.builder.port)}="
                               f"{session}; HttpOnly;"
                               " SameSite=Strict; Path=/")))
        if not self._session_ok():
            return self._refuse()
        self._active(url.path)
        if url.path == "/":
            self.builder.page_served()
            return self._send(200, self.builder.page_bytes,
                              "text/html; charset=utf-8", headers=(
                ("Content-Security-Policy",
                 "default-src 'none'; script-src 'unsafe-inline';"
                 " style-src 'unsafe-inline'; connect-src 'self';"
                 " font-src 'self'; img-src data:; frame-ancestors 'none';"
                 " form-action 'none'; base-uri 'none'"),
                ("X-Frame-Options", "DENY")))
        if url.path.startswith("/font/"):
            name = url.path[len("/font/"):]
            if name not in FONT_NAMES:
                return self._refuse(404)
            try:
                with open(font_path(name), "rb") as f:
                    data = f.read()
            except OSError:
                return self._refuse(404)
            return self._send(200, data, "font/woff2", headers=(
                ("Cache-Control", "max-age=86400"),))
        if url.path == "/api/state":
            self.builder.touch()
            # "draft": the last preview this builder took, so a tab opened
            # by another press of Edit on computer starts on the colours the
            # board is showing and not on the saved theme. Without it the
            # older tab was told to stop and the newer one opened on the
            # saved theme: the draft was on the board and in no tab that
            # could still send it (review of fp/edit-reset r2, SHOULD-1).
            #
            # "board" and "waiting_s": whether anything will show a preview.
            # The page said "Preview sent: ... is on your board" after every
            # change, to a 1.3.4 board that takes no theme line and with no
            # bridge running to take the file (review of 1.4.0, builder S3).
            takes, _ = theme.board_takes_themes(self.builder.home)
            return self._json(200, {"saved": theme.load_saved(
                self.builder.home), "draft": self.builder.draft,
                "idle_s": self.builder.idle_s,
                "persistent": self.builder.persistent,
                "board": {"themes": takes},
                "waiting_s": theme.preview_waiting_s(self.builder.home)})
        if self._theme_only(url.path):
            return
        ops = self.builder.board_ops
        if url.path == "/api/wifi":
            return self._json(200, ops.state())
        if url.path == "/api/wifi/nets":
            return self._json(200, {"nets": self.builder.nets})
        if url.path == "/api/device":
            return self._json(200, ops.device())
        if url.path == "/api/computer":
            return self._computer_state()
        if url.path == "/api/logo":
            return self._json(200, {"logo": ops.device().get("logo")})
        if url.path == "/api/logo/preview":
            # Only the copy of what this daemon sent, and only while the
            # board still reports that same logo (bytes and frames).
            png = logo_preview.load_matching(self.builder.home,
                                             ops.device().get("logo"))
            if png is None:
                return self._refuse(404)
            return self._send(200, png, "image/png", headers=(
                ("Cache-Control", "no-store"),))
        return self._refuse(404)

    # -- the settings endpoints

    def _theme_only(self, path):
        """True after answering 409 when this is a board-side route and the
        server was started without a way to the board (`blink theme`)."""
        if path.startswith(BOARD_PATHS) and self.builder.board_ops is None:
            self._json(409, {"why": "Open this with blink settings while"
                                    " the app is running."})
            return True
        return False

    def _computer_state(self):
        home = self.builder.home
        try:
            on = user_config.load(home).get(status_page.SETTING) is True
        except (user_config.Unreadable, OSError):
            return self._json(500, {"why": "This computer's settings file"
                                           " could not be read."})
        self._json(200, {"status_page": on,
                         "auto_update": update.auto_update_allowed(home),
                         # Keep figures fresh: develop's usage refresh,
                         # the same key `blink config usage-refresh` sets.
                         "usage_refresh": usage_refresh.enabled(home) is True})

    def _post_computer(self, want):
        home = self.builder.home
        if not (isinstance(want, dict) and want and all(
                k in ("status_page", "auto_update", "usage_refresh")
                and isinstance(v, bool) for k, v in want.items())):
            return self._json(400, {"why": "That was not a setting."})
        try:
            if "status_page" in want:
                user_config.put(home, status_page.SETTING,
                                want["status_page"])
            if "usage_refresh" in want:
                user_config.put(home, usage_refresh.SETTING,
                                want["usage_refresh"])
                if want["usage_refresh"]:
                    # As `blink config usage-refresh on` does: a fresh
                    # start, and where claude is now.
                    usage_refresh.switch_on(home, os.path.expanduser("~"))
            if "auto_update" in want:
                marker = os.path.join(home, "no-auto-update")
                if want["auto_update"]:
                    if os.path.exists(marker):
                        os.remove(marker)
                else:
                    os.makedirs(home, exist_ok=True)
                    open(marker, "ab").close()
        except (user_config.Unreadable, OSError):
            return self._json(500, {"why": "This computer could not save"
                                           " that setting."})
        self._computer_state()

    def _post_wifi(self, path, want):
        """`want` is the parsed body; the caller drops it on return. Nothing
        here logs, keeps or echoes it."""
        ops = self.builder.board_ops
        try:
            if path == "/api/wifi/scan":
                self.builder.nets = ops.scan()
                return self._json(200, {"nets": self.builder.nets})
            if path == "/api/wifi/forget":
                return self._json(200, ops.forget())
            if path != "/api/wifi/join":
                return self._refuse(404)
            ssid = pw = None
            if isinstance(want, dict):
                ssid, pw = want.get("s"), want.get("p")
            try:
                ssid = bytes.fromhex(ssid)
            except (TypeError, ValueError):
                ssid = None
            if ssid is None or not isinstance(pw, str):
                return self._json(400, {"why": "That was not a network."})
            # Before the board is touched, so a bad entry costs no wait.
            bad = (wifi_pair.ssid_problem(ssid)
                   or wifi_pair.password_problem(pw))
            if bad:
                return self._json(400, {"why": bad})
            return self._json(200, ops.join(ssid, pw))
        except settings_ops.BoardOpError as e:
            self._json(409, {"why": str(e)})

    def _pref_ok(self, want):
        if not (isinstance(want, dict) and want):
            return False
        for k, v in want.items():
            if k == "view":
                if isinstance(v, bool) or not isinstance(v, int) \
                        or not 0 <= v <= 255:
                    return False
            elif k not in PREF_ENUMS or isinstance(v, bool) \
                    or v not in PREF_ENUMS[k]:
                return False
        return True

    def _post_pref(self, want):
        if not self._pref_ok(want):
            return self._json(400, {"why": "That was not a setting."})
        try:
            self._json(200, {"pref": self.builder.board_ops.pref_set(**want)})
        except settings_ops.BoardOpError as e:
            self._json(409, {"why": str(e)})

    def _post_logo(self):
        """The logo as the page chose it, as raw bytes. Every refusal is one
        sentence and costs the board nothing: the image is encoded and
        checked here, and only a blob logo_encode.parse accepted goes on.
        Nothing about the body is logged or echoed."""
        h = self.headers

        def refuse(status, why):
            self.close_connection = True      # the body was not read
            self._json(status, {"why": why})

        if h.get("Content-Type", "").split(";")[0].strip().lower() \
                != "application/octet-stream":
            return refuse(415, "That was not a picture.")
        try:
            n = int(h.get("Content-Length", ""))
        except ValueError:
            return refuse(400, "That was not a picture.")
        if n < 0 or n > MAX_LOGO_BODY:
            return refuse(413, "That file is too big. Pick a smaller one.")
        kind = h.get("X-Blink-Logo")
        hold = h.get("X-Blink-Hold-Ms", "")
        fit = h.get("X-Blink-Fit")
        bg = h.get("X-Blink-Bg", "")
        if kind not in ("rgba", "gif"):
            return refuse(400, "That was not a picture.")
        # Compared as text: str.isdigit() is true for "²" and the like, and
        # int() of those raises past the handler.
        if hold not in {str(x) for x in logo_encode.HOLD_CHOICES}:
            return refuse(400, "Pick 3, 5 or 10 seconds.")
        if kind == "gif" and fit not in logo_encode.FIT_SCALE:
            return refuse(400, "Pick a size on the screen.")
        if len(bg) != 6 or any(c not in "0123456789abcdefABCDEF" for c in bg):
            return refuse(400, "That was not a background colour.")
        bgc = tuple(bytes.fromhex(bg))
        raw = self.rfile.read(n)
        try:
            # One encode at a time: a page that re-checks after every
            # change must not run several 2 MiB decodes at once.
            with _ENCODE_LOCK:
                if kind == "rgba":
                    blob = logo_encode.from_rgba(raw, bgc, int(hold))
                else:
                    blob = logo_encode.from_gif(raw, bgc, int(hold), fit)
        except ValueError as e:
            msg = str(e)
            return self._json(400, {"why": msg[:1].upper() + msg[1:] + "."})
        finally:
            raw = None
        # ?check=1: the same encoding and the same refusals, and then the
        # size alone. The page asks before it offers Send, so "About N KB"
        # is the encoder's number and a too-big logo is refused on the
        # page rather than after a minute on the cable.
        if "check=1" in urllib.parse.urlsplit(self.path).query.split("&"):
            return self._json(200, {"bytes": len(blob)})
        try:
            state = self.builder.board_ops.logo(blob)
        except settings_ops.BoardOpError as e:
            return self._json(409, {"why": str(e)})
        if isinstance(state, dict) and state.get("present") == 1:
            try:
                logo_preview.save(self.builder.home, blob, state)
            except (OSError, ValueError, private_file.NotPrivate):
                # The preview is a courtesy, the send is done. A programming
                # error (not these three) is allowed to surface instead.
                print("[settings] logo preview not kept", file=sys.stderr)
        self._json(200, {"logo": state, "bytes": len(blob)})

    def _post_logo_remove(self):
        try:
            state = self.builder.board_ops.logo_remove()
        except settings_ops.BoardOpError as e:
            return self._json(409, {"why": str(e)})
        # The board answers logo_state either way. A failed flash erase
        # leaves present:1, and a 200 then had the page drop the logo and
        # say nothing while the next boot showed it again.
        if (not isinstance(state, dict) or state.get("present") != 0
                or state.get("err") == 1):
            print("[settings] the board still reports its logo after remove",
                  file=sys.stderr)
            return self._json(409, {"why": (
                "The board could not remove the logo. Try again.")})
        logo_preview.remove(self.builder.home)
        self._json(200, {"logo": state})

    # -- POST

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, 400
        if n < 0 or n > MAX_BODY:
            return None, 413
        return self.rfile.read(n), None

    def do_POST(self):
        if not (self._host_ok() and self._session_ok()
                and self._origin_ok()):
            return self._refuse()
        path = urllib.parse.urlsplit(self.path).path
        self._active(path)
        self.builder.count(path)
        want = None
        settings = path.startswith(("/api/wifi/", "/api/computer",
                                    "/api/pref", "/api/logo/remove"))
        if self._theme_only(path):
            return
        if path == "/api/logo":
            return self._post_logo()
        if settings:
            if self.headers.get("Content-Type", "").split(";")[0].strip() \
                    .lower() != "application/json":
                return self._json(415, {"why": "That was not JSON."})
        raw, err = self._body()
        if settings and not err:
            try:
                want = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                err = 400
            raw = None
        if err:
            self.close_connection = True
            return self._refuse(err)
        # Preview, Save and Cancel each carry the page's tab id, and an
        # older tab's is refused here rather than on its next ping. The ping
        # comes every 20 s while a tab is visible and about once a minute
        # while it is hidden, and in that window an older tab's Save or
        # Cancel ended the builder under the newer one, whose next ping then
        # said the builder had stopped (review of fp/edit-reset r2,
        # SHOULD-2). 409 with "newer", so the page stops as a ping would
        # have told it to.
        if path in ("/api/preview", "/api/save", "/api/cancel",
                    "/api/done") and \
                self.builder.superseded(_tab_id(
                    self.headers.get(TAB_HEADER))):
            return self._json(409, {"newer": True, "error":
                                    "Continued in a newer tab."})
        if path.startswith("/api/wifi/"):
            return self._post_wifi(path, want)
        if path == "/api/computer":
            return self._post_computer(want)
        if path == "/api/pref":
            return self._post_pref(want)
        if path == "/api/logo/remove":
            return self._post_logo_remove()
        if path == "/api/cancel":
            self._json(200, {"ok": True})
            return self.builder.finish("cancelled")
        if path == "/api/done":
            # The settings page's Done button: the session ends, and an
            # unsaved preview is cancelled by finish() as on any non-save.
            self._json(200, {"ok": True})
            return self.builder.finish("done")
        if path == "/api/closed":
            self.builder.closing(_tab_of(raw))
            return self._json(200, {"ok": True})
        if path == "/api/ping":
            # Alive, not active: an open tab nobody touches still times out.
            # "newer": a later tab has taken over, so this one stops.
            newer = self.builder.seen(_tab_of(raw))
            return self._json(200, {"ok": True, "newer": bool(newer)})
        if path not in ("/api/preview", "/api/save"):
            return self._refuse(404)
        try:
            t = theme.validate(json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeDecodeError) as e:
            msg = str(e) if isinstance(e, theme.ThemeError) else \
                "that was not a theme"
            return self._json(400, {"error": msg})
        if path == "/api/preview":
            self.builder.touch()
            self.builder.write_preview(t)
            return self._json(200, {"ok": True})
        # Under the builder's file lock from the stored theme to the cancel,
        # so a preview still being written cannot land between them or after
        # (review of 1.4.0, builder N5).
        with self.builder.writing():
            keep = theme.saved_custom(self.builder.home) \
                if t["id"] != theme.CUSTOM else None
            try:
                theme.save(self.builder.home, t, keep_custom=keep)
            except theme.Unreadable as e:
                # theme.json is there and unreadable, so it is not written
                # over (contract Addendum 4a); the message says which file,
                # not what it holds.
                return self._json(500, {"error": f"Not saved: {e}."})
            except OSError as e:
                # Said to the page, and the builder stays up so Save can be
                # tried again. "Saved" over a failed write is a lie the
                # owner acts on.
                return self._json(500, {"error": "Not saved: this computer"
                                        f" could not write the theme file"
                                        f" ({e})."})
            # A cancel, not just dropping the preview file: a preview still
            # waiting would reach the board after the save and show
            # something other than what was just stored, and one already
            # sent stays on the board for 15 minutes when this Save picked
            # the theme already stored, because then no save line goes out
            # to end it (review NICE-4). ThemeSync.outgoing drops the cancel
            # when a save is owed.
            theme.write_cancel(self.builder.home)
            # The settings page keeps writing on: stop_writing() would make
            # every later preview return False, so the next theme clicked
            # would silently not reach the board. finish() ends it.
            if not self.builder.persistent:
                self.builder.stop_writing()
            else:
                # Nothing is unsaved now: a tab opened next must not start
                # on the preview this save replaced.
                self.builder.draft = None
        self._json(200, {"ok": True})
        if not self.builder.persistent:
            self.builder.finish("saved")


def _tab_of(raw):
    """The page's own id from a ping or close body, or None."""
    try:
        tab = json.loads(raw.decode("utf-8")).get("tab") if raw else None
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    return _tab_id(tab)


def _tab_id(tab):
    """Short and plain or nothing: it is only ever a key in
    BuilderServer._tabs and _order."""
    if isinstance(tab, str) and 0 < len(tab) <= 32 and tab.isalnum():
        return tab
    return None


def _linger(request):
    """Half-close `request` and drop what it still sends, until it closes,
    MAX_LOGO_BODY + 64 KiB have been read or _LINGER_S has passed. The
    number of bytes dropped, for the test of those bounds."""
    read = 0
    try:
        request.shutdown(socket.SHUT_WR)
        end, cap = time.monotonic() + _LINGER_S, MAX_LOGO_BODY + 65536
        while read < cap:
            wait = end - time.monotonic()
            if wait <= 0:
                break
            request.settimeout(wait)
            got = request.recv(min(cap - read, 65536))
            if not got:
                break
            read += len(got)
    except OSError:
        pass
    return read


class _HTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        # http.server's own server_bind asks socket.getfqdn() for a name to
        # keep in server_name, which nothing here reads -- every link is
        # http://127.0.0.1:<port>. getfqdn is a reverse lookup through
        # gethostbyaddr, which goes to the system resolver (the old value on
        # a stock Mac was '1.0.0.127.in-addr.arpa'), and it ran inside
        # launch() with the Launcher's lock held, while the board counts down
        # 5 s to its own "Run blink theme" fallback (bridge.py).
        #
        # Why it went, and how sure that is: the child in
        # tests/pc/test_daemon_exit.py that starts a builder never got past
        # launch() within 10 s on macos-latest, in both runs of fp/ci-fixes'
        # base (41093831896, 41094198669), both on runner image
        # macos-26-arm64 20261026.0455.2. The macOS system-Python job runs
        # the same test with the same lookup and passed both times, on the
        # older images 20261013.0424.1 and 20261021.0445.8. The child does
        # the job's first reverse lookup of 127.0.0.1; the second, in a
        # status-page test with a stock http.server, sat in a progress line
        # ~35 s slower than Ubuntu's, and later ones were fast. A resolver
        # made to take 30 s reproduces the CI failure exactly, and this bind
        # passes under it. That makes a slow first lookup on that image the
        # likeliest cause, but it is inferred, not measured on the runner: a
        # stall on the job's first listen() would leave the same timings.
        # The lookup buys nothing either way, so it is skipped: bind, and
        # name the address as given.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def shutdown_request(self, request):
        """Close the way RFC 9112 9.6 asks: stop writing, then read and drop
        what the client is still sending, then close.

        A refusal answers before the body is read (403 without a session,
        415, a bad logo header). Closing a socket that still has unread
        bytes sends a reset, and Windows then throws away the answer the
        client had not read yet: on the Windows VM (2026-11-07) a 300 KB
        logo POST refused with 403 or 400 came back as WinError 10053, not
        the sentence, while macOS and Linux happened to deliver it. Bounded
        in bytes and time (_linger), and on the handler's own thread, so a
        client that keeps sending costs at most one more logo body and two
        seconds.

        By design the bound is below what a hostile body can be: a POST
        declaring more than MAX_LOGO_BODY is refused with 413 before a byte
        is read, and if its sender keeps going past the drain this still
        ends in a reset, so a Windows client may never see that 413. The
        settings page checks a file's size before it sends, so only a
        client that ignores the limit meets it.
        """
        _linger(request)
        self.close_request(request)

    def handle_error(self, request, client_address):
        # socketserver prints a traceback to stderr here, and under the daemon
        # stderr is the log a customer sends us. A failed request is the
        # browser's problem, not the log's.
        pass


class BuilderServer:
    """One builder session: a server, its tokens, and its deadlines."""

    def __init__(self, home, idle_s=IDLE_S, now=time.monotonic,
                 tick_s=TICK_S, board_ops=None, panel="look", token=None,
                 persistent=False):
        self.home = home
        # True for the settings page, which stays up through a theme save
        # (Connect and Board go on working); `blink theme` is False and ends
        # on save as it always did.
        self.persistent = persistent
        self.board_ops = board_ops      # None: `blink theme`, no board routes
        self.nets = []                  # the last scan, for GET /api/wifi/nets
        self.idle_s = idle_s
        self._now = now
        self._tick_s = tick_s
        self._lock = threading.Lock()
        self._links = {}            # unspent link -> when it was made
        self._minted = set()        # the ones new_link made, not add_token
        if token is not None:
            check_token(token)
            self._links[token] = now()
        # Links that no longer work and why, "replaced" or "used", newest
        # last and kept few: only so the page the browser lands on can say
        # which (see link_fate).
        self._gone = {}
        self._sessions = set()
        self._last = now()
        self._seen = None
        self._closed_at = None
        self._close_grace = CLOSE_GRACE_S
        # When the page was last served (GET /); see CLOSE_GRACE_S.
        self._served_at = None
        # Each open page by the id it made for itself, and when it last
        # pinged. Two tabs can share the session -- "Edit on computer"
        # while one is open opens another -- and the older one's close
        # must not end the builder the newer one is showing.
        self._tabs = {}
        # Every tab id in the order it was first heard. Only the newest is
        # live; see seen().
        self._order = {}
        # The last theme a preview sent, or None; see /api/state.
        self.draft = None
        # Held by whoever writes the preview file or the theme, and what
        # makes the end final: once a save or a finish has written its
        # cancel, nothing later is written (write_preview, writing).
        self._files = threading.Lock()
        self._writing = True
        self.outcome = None
        # What the daemon's log says when this builder ends, or None. Set by
        # the Launcher, so `blink theme` prints nothing beyond its own words.
        self.say = None
        # What this builder was asked, counted by kind and never by content,
        # and when it started; see _account.
        self._born = now()
        self._counts = dict.fromkeys(_COUNTED, 0)
        self._done = threading.Event()
        self.page_bytes = page().encode("utf-8")
        self._httpd = _HTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.builder = self
        self.address = self._httpd.server_address[:2]
        self.port = self.address[1]

    @property
    def done(self):
        # Finished, not merely stopped: the Launcher must not hand out a link
        # to a server that is on its way down.
        return self.outcome is not None

    def writing(self):
        """The file lock, for a handler that writes the theme and then the
        cancel as one step. Call stop_writing() inside it once the cancel is
        written."""
        return self._files

    def stop_writing(self):
        self._writing = False

    def write_preview(self, t):
        """Write a preview for the daemon, unless the builder has ended.
        True when written.

        The preview and Save handlers run on separate threads and do_POST
        never asked whether the builder was over. A preview whose write was
        slow (a scanner holding theme_preview.json on Windows) landed after
        Save's cancel when the theme saved was the one already stored, so
        the daemon sent only op=preview and the board showed it for 15
        minutes under "Saved" (review of 1.4.0, builder N5)."""
        with self._files:
            if not self._writing:
                return False
            theme.write_preview(self.home, t)
            self.draft = t
            return True

    def listening_socket(self):
        return self._httpd.socket

    def start(self):
        # Recorded so that `blink theme` does not start a second builder on
        # this home: two write the same preview file, and one's cancel,
        # close or idle end took the other's live preview off the board while
        # its tab went on saying "Preview sent" (review of 1.4.0, builder
        # N1). A failure to write it costs only that protection.
        try:
            theme.write_builder(self.home, self.port)
        except OSError:
            pass
        threading.Thread(target=self._httpd.serve_forever,
                         kwargs={"poll_interval": 0.2},
                         name="blink-theme-http", daemon=True).start()
        threading.Thread(target=self._watch, name="blink-theme-idle",
                         daemon=True).start()

    def _watch(self):
        while not self._done.wait(self._tick_s):
            self.tick()

    # -- tokens

    def new_link(self, panel=None):
        """A one-time link. It replaces any link not yet used, and lives
        LINK_LIFE_S: "Edit on computer" mints one on every reshow (contract
        Addendum 6), and each used to stay valid, unspent, for the server's
        whole life (review of fp/audit-nice r2, NICE 1). A spent link was
        always refused; now so is a superseded or stale one."""
        k = secrets.token_urlsafe(32)
        with self._lock:
            # Only links this method minted: the token a `blink settings`
            # run printed is spoken for, and a press of Open on PC on the
            # board must not turn it into "replaced" (final review, page
            # minor 8).
            for old in [o for o in self._links if o in self._minted]:
                self._retire_link(old, "replaced")
                del self._links[old]
            self._minted = {k}
            self._links[k] = self._now()
        extra = f"&panel={panel}" if panel in PANELS else ""
        return f"http://127.0.0.1:{self.port}/open?k={k}{extra}"

    def add_token(self, token):
        """Another one-time link token, beside any unspent one."""
        check_token(token)
        with self._lock:
            self._links[token] = self._now()

    def _retire_link(self, k, why):
        """Called with the lock held."""
        self._gone[k] = why
        while len(self._gone) > 16:
            del self._gone[next(iter(self._gone))]

    def link_fate(self, k):
        """"replaced" or "used" for a link that once worked, else None. A
        slow browser still loading the first link when the owner pressed
        Edit on computer again was told "Not allowed. Run blink theme
        again.", which reads as an instruction to start a second builder,
        while the newer tab worked (review of 1.4.0, builder N4)."""
        with self._lock:
            for gone, why in self._gone.items():
                if _same(gone, k):
                    return why
        return None

    def redeem(self, k):
        with self._lock:
            now = self._now()
            for good, made in list(self._links.items()):
                if _same(good, k):
                    del self._links[good]
                    if now - made >= LINK_LIFE_S:
                        return None
                    self._retire_link(good, "used")
                    s = secrets.token_urlsafe(32)
                    self._sessions.add(s)
                    self._last = self._seen = now
                    return s
        return None

    def session_valid(self, s):
        with self._lock:
            return any(_same(good, s) for good in self._sessions)

    # -- liveness

    def touch(self):
        with self._lock:
            self._last = self._seen = self._now()
            self._closed_at = None

    def page_served(self):
        """The page itself was asked for: a touch, and the time kept so a
        close that follows at once is read as a reload (CLOSE_GRACE_S)."""
        with self._lock:
            self._last = self._seen = self._served_at = self._now()
            self._closed_at = None

    def _prune_tabs(self, now):
        # A tab unheard from for PAGE_ALIVE_S no longer counts (it crashed,
        # or its machine slept); dropped rather than kept for the server's
        # life (review r2, NICE 2).
        self._tabs = {k: t for k, t in self._tabs.items()
                      if now - t < PAGE_ALIVE_S}

    def seen(self, tab=None):
        """A ping. True when `tab` has been superseded by a newer one.

        One live tab per builder. Every press of "Edit on computer" while
        the builder is open opens another tab on it, and on Windows that
        tab often opens behind other windows, so the owner presses again:
        with the button no longer locked out, that made a pile of tabs, all
        able to send previews (review of fp/edit-reset, SHOULD-1). Now the
        newest tab -- the last id first heard -- is the live one, and an
        older tab is told so on its next ping, or by a 409 on its next
        preview, save or cancel (superseded()), and stops. The page closes
        itself only when it holds nothing unsaved and the browser allows;
        otherwise it keeps its colours on screen, its controls off, and
        its status line says the builder continued in a newer tab and that
        the share code of its changes can still be copied (retire() in the
        page), and the newer tab opens on the last
        preview anyway (/api/state "draft"). A superseded tab's ping keeps
        nothing alive: it does not count as seen, and it leaves the tab
        count, so the newest tab's close still ends the builder."""
        with self._lock:
            now = self._now()
            if tab is not None:
                seq = self._order.setdefault(tab, len(self._order))
                if seq < len(self._order) - 1:
                    self._tabs.pop(tab, None)
                    return True
            self._seen = now
            self._closed_at = None
            self._prune_tabs(now)
            if tab is not None:
                self._tabs[tab] = now
            return False

    def superseded(self, tab):
        """Has a newer tab taken over from `tab`? Only a tab already heard
        is ever older: an id not yet pinged is a page that has just loaded,
        which is the newest there is, and no id at all is a client from
        before tab ids (or a test), which is let through as it always was."""
        with self._lock:
            seq = self._order.get(tab) if tab is not None else None
            return seq is not None and seq < len(self._order) - 1

    def page_alive(self):
        """Is a builder page open on this server right now?"""
        with self._lock:
            return (not self.done and self._seen is not None
                    and self._closed_at is None
                    and self._now() - self._seen < PAGE_ALIVE_S)

    def closing(self, tab=None):
        with self._lock:
            now = self._now()
            self._tabs.pop(tab, None)
            self._prune_tabs(now)
            # Only the newest tab is live (seen()). An older one that has
            # not yet pinged since the newer arrived is still in the count,
            # but it is not showing this builder: its next ping says it is
            # superseded and drops it, and a retired tab sends no
            # /api/closed, so nothing would ever set the close again. The
            # builder then ran with no live tab until IDLE_S, 600 s after
            # the last edit, and the board kept the closed tab's last
            # preview all that time (review of 1.4.0, builder S1).
            newest = len(self._order) - 1
            if any(self._order.get(k) == newest for k in self._tabs):
                return      # the live tab is still showing this builder
            self._closed_at = now
            reload = (self._served_at is not None
                      and now - self._served_at < RELOAD_WINDOW_S)
            self._close_grace = RELOAD_GRACE_S if reload else CLOSE_GRACE_S

    def tick(self):
        with self._lock:
            now = self._now()
            closed = (self._closed_at is not None
                      and now - self._closed_at >= self._close_grace)
            idle = now - self._last >= self.idle_s
        if closed:
            self.finish("closed")
        elif idle:
            self.finish("idle")

    def count(self, path):
        """One more request of this kind, for the line finish() says. Only
        the paths named in _COUNTED are kept: a tally by kind can hold no
        theme, no link and no cookie."""
        if path in self._counts:
            with self._lock:
                self._counts[path] += 1

    def _account(self, outcome):
        """How this builder ended, as one line. The outcome is one of the
        fixed words finish() is called with; the rest are whole numbers."""
        with self._lock:
            n = dict(self._counts)
            tabs = len(self._order)
            secs = int(self._now() - self._born)
        return (f"[theme] builder ended: {outcome} after {secs} s"
                f" (pings={n['/api/ping']} previews={n['/api/preview']}"
                f" saves={n['/api/save']} cancels={n['/api/cancel']}"
                f" closes={n['/api/closed']} tabs={tabs})")

    def finish(self, outcome, cancel=True):
        """Stop. Anything but a save also tells the board to drop the
        preview, through the same file every preview went through."""
        with self._lock:
            if self.outcome is not None:
                return
            self.outcome = outcome
        # Said first, before anything below can fail: it is the one record
        # of why the builder ended. On 2026-10-31 a builder ended 6 s after
        # the board opened it and the log could not say a Save from a closed
        # tab. A log that cannot be written must not stop the builder.
        if self.say is not None:
            try:
                self.say(self._account(outcome))
            except Exception:
                pass
        try:
            theme.remove_builder(self.home, self.port)
        except OSError:
            pass
        with self._files:
            self._writing = False
            if cancel and outcome != "saved":
                try:
                    theme.write_cancel(self.home)
                except OSError:
                    pass

        def stop():
            self._httpd.shutdown()
            self._httpd.server_close()
            self._done.set()
        # Not inline: finish() is called from a request thread, and shutdown()
        # waits for serve_forever to notice, which is fine from there -- but
        # the response to that request has to get out first.
        threading.Thread(target=stop, name="blink-theme-stop",
                         daemon=True).start()

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self.outcome

    def wait_interruptibly(self):
        """wait() with no end, for a foreground command that Ctrl-C must
        be able to stop. In slices, because a lock acquire with no timeout
        is not interruptible on Windows in CPython 3.11, the Python the
        Windows binary is built with: Python/thread_nt.h says "Fow now,
        intr_flag does nothing on Windows, and lock acquires are
        uninterruptible", and Event.wait(None) is exactly such an acquire.
        `blink theme` then ignored Ctrl-C until the builder ended by itself
        -- Save, Cancel, the tab closed, ten minutes idle -- while telling
        the owner that Ctrl-C cancels it (review of 1.4.0, builder S2). A
        timed wait is interrupted on every platform. Half a second is the
        longest a Ctrl-C can wait. Measured on Windows 11 with Python
        3.11.9: a SIGINT raised at 2 s reached Event.wait(None) at 8 s,
        when the event was set, and a loop of wait(0.5) at 2.0 s, by both
        signal.raise_signal and _thread.interrupt_main."""
        while not self._done.wait(WAIT_SLICE_S):
            pass
        return self.outcome


def running_builder(home):
    """The port of a builder running on `home`, or None.

    From the record every builder keeps (theme.BUILDER_FILE), then asked: a
    builder killed before it could remove its record leaves one behind, and
    its port may since have gone to some other program. So the port must
    answer the way this server does to a request with no session, 403 and
    "Not allowed", and a record that fails that is stale and is taken
    away."""
    port = theme.read_builder(home)
    if port is None:
        return None
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            c.request("GET", "/api/state", headers={
                "Host": f"127.0.0.1:{port}"})
            r = c.getresponse()
            alive = r.status == 403 and r.read(64).startswith(b"Not allowed")
        finally:
            c.close()
    except (OSError, http.client.HTTPException):
        alive = False
    if alive:
        return port
    try:
        theme.remove_builder(home, port)
    except OSError:
        pass
    return None


def _say_ending(line):
    # The daemon's stderr is bridge.log. The line is built by
    # BuilderServer._account: a fixed outcome word and counts.
    print(line, file=sys.stderr)


class Launcher:
    """The daemon's builder: at most one running, reopened on request."""

    def __init__(self, home, opener=None, make_server=BuilderServer,
                 now=time.monotonic, board_ops=None, persistent=False):
        self._home = home
        # The settings page; see BuilderServer. A way to the board makes it
        # one: the page sends no pref_set and loads no Board panel unless
        # /api/state says persistent, and the daemon built this with
        # board_ops alone, so the page the board's Open on PC opened
        # dropped every brightness click without a request and ended on its
        # first Save (owner, 2026-11-07, board on WiFi: no pref_set logged).
        self.persistent = persistent or board_ops is not None
        self.board_ops = board_ops      # the settings page's way to the board
        # `is None`, not `or`: a stub that was falsy (a list subclass, empty
        # until its first call) became the real open_url, which is
        # webbrowser.open on macOS -- a review probe opened two tabs in the
        # owner's own browser (review of 1.4.0, builder N8).
        self._opener = opener if opener is not None else open_url
        self._make = make_server
        self._now = now
        self._server = None
        self._opened_at = None
        self._last_ok = False
        # The builder a board's ask opened, until its end has been told to
        # the board once (ended(), contract Addendum 6).
        self._told = None
        self._lock = threading.Lock()

    def launch(self):
        """True when a builder page is open, or has just been opened."""
        with self._lock:
            now = self._now()
            server = self._server
            live = server is not None and not server.done
            if live and self._opened_at is not None and (
                    now - self._opened_at < OPEN_EVERY_S):
                # Answered with what the open under OPEN_EVERY_S ago
                # achieved, and no new window: the one narrow case of an ok
                # with nothing newly shown. Any ask within 2 s of the open
                # reaches it -- a replayed one, or a press on the board's
                # "Opened" note -- moments after a window opened (review
                # r2, NICE 4; review of fp/edit-reset, SHOULD-2).
                return self._last_ok
            # A page already open in a tab used to be answered ok 1 with
            # nothing shown, on the idea that a second tab would fight the
            # first; but a tab behind other windows is one the owner cannot
            # find, and "Opened on your computer" over no window is the
            # silent ok the contract now forbids (Addendum 6). So a fresh
            # one-time link is opened on the running builder; the tabs
            # share one session and the server counts them
            # (BuilderServer.seen), so closing the older one does not end
            # the builder under the newer. Whether the browser comes to the
            # FRONT is the desktop's call, not ours: see open_url.
            self._opened_at = now
            ok, server = self._open(server if live else None)
            if ok and server.done:
                # It ended while the browser was opening: its tab had just
                # closed and its grace ran out, or it went idle. The board
                # would be told "opened" over a dead builder, the done
                # having been ignored while its ask was in flight, and a
                # press on its note would be folded into this dead open
                # (review r2, SHOULD-1). So once more, on a builder of its
                # own.
                ok, server = self._open(None)
                if ok and server.done:
                    ok = False
            self._last_ok = ok
            if ok:
                self._told = server
            return ok

    def serve_headless(self, panel, token, request_id):
        """`blink settings`: start or re-use the page server with `token`
        registered, then record its port with the request id (D37, D50).
        Never calls the opener: the CLI opens the browser, and launch()
        holds this lock across an opener measured stuck for 28.7 s."""
        # A running server needs no Launcher lock: launch() holds it across a
        # hung opener (28.7 s measured), and the request file is already
        # consumed, so waiting here made the CLI say "not running" of a
        # healthy daemon.
        server = self._server
        if server is not None and not server.done:
            server.persistent = True    # the settings page; see BuilderServer
            server.add_token(token)
            server.touch()
            theme.write_builder(self._home, server.port, ack=request_id)
            return
        with self._lock:
            server = self._server
            if server is not None and not server.done:
                server.persistent = True
                server.add_token(token)
                server.touch()
            else:
                kw = {} if self.board_ops is None \
                    else {"board_ops": self.board_ops}
                server = self._make(self._home, panel=panel, token=token,
                                    persistent=True, **kw)
                server.say = _say_ending
                server.start()
                self._server = server
            # After the token: the record is what the CLI waits for.
            theme.write_builder(self._home, server.port, ack=request_id)

    def _open(self, server):
        """Open a browser on `server`, or on a new builder when it is None.
        (ok, the server it was opened on). Called with the lock held."""
        fresh = server is None
        if fresh:
            try:
                more = {"persistent": True} if self.persistent else {}
                server = self._make(self._home, **more) \
                    if self.board_ops is None else self._make(
                        self._home, board_ops=self.board_ops, **more)
            except OSError as e:
                # The page missing from a broken install, or no port to
                # bind. One line, and the board's own fallback.
                print(f"[theme] builder could not start: {e}",
                      file=sys.stderr)
                self._server = None
                return False, None
            server.say = _say_ending
            server.start()
            self._server = server
        else:
            # A tap is an intent to edit: it holds off this builder's idle
            # limit and the close grace of a tab that has just gone, which
            # would otherwise end it before the new tab loads (review r2,
            # SHOULD-1). redeem() alone does not clear the grace.
            server.touch()
        # Held across the opener on purpose, so two taps cannot open two
        # tabs; close() and ended() do not wait for it (see there).
        ok = bool(self._opener(server.new_link()))
        if not ok and fresh:
            # Nothing was ever shown, so there is nothing to cancel; the
            # board puts up its own "Run blink theme" fallback.
            server.finish("no browser", cancel=False)
            if self._server is server:
                self._server = None
        return ok, server

    def ended(self):
        """True, once, when a builder opened for the board has ended --
        saved, cancelled, its tab closed, idle, or stopped -- so the daemon
        can send theme_edit done:1 (contract Addendum 6).

        Polled from the serial loop on every pass, so it must never wait:
        launch() holds the lock across the browser opener, which has been
        measured stuck for 28.7 s (close(), below), and the first version
        of this blocked the serial thread that long -- no reads, no pong,
        HOST LOST on the board at 30 s (review r2, MUST-1). When launch()
        has the lock this answers False and is asked again next pass."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            server = self._told
            if server is not None and server.done:
                self._told = None
                return True
            return False
        finally:
            self._lock.release()

    def editing(self):
        """Is the builder a board's ask opened still open? True, False, or
        None when that cannot be said without waiting. For the welcome
        greet() writes ("editing", protocol.welcome), which is how a board
        that rebooted or was unplugged meanwhile learns whether to hold its
        "Edit on computer" slot.

        Never waits, for the reason ended() does: greet() runs on the serial
        thread, and launch() holds the lock across a browser opener measured
        stuck for 28.7 s. A launch in progress answers None, which leaves
        the board as it is; the ok 1 that launch is about to put on the
        queue holds it if a builder opens (the board takes a late ok).

        `_told` is the builder until its end has been told to the board, so
        an ended builder whose done is still waiting for the board's caps
        reads False here and the done that follows is a no-op."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            server = self._told
            return server is not None and not server.done
        finally:
            self._lock.release()

    def close(self):
        """On daemon exit: a live builder writes its cancel and stops.

        Waits at most a second for the lock. launch() holds it across the
        browser opener, and an opener stuck starting a browser kept a close
        queued behind it for 28.7 s -- far past the 5 s burn.sh waits for
        the port after a stop, which is the bound that matters now
        the plist allows 180 s (cli.STOP_GRACE_S) (review, 2026-10-27).
        Without the
        lock the server is still finished: finish() is idempotent and locks
        for itself.
        """
        got = self._lock.acquire(timeout=1)
        try:
            server, self._server = self._server, None
        finally:
            if got:
                self._lock.release()
        if server is not None:
            server.finish("daemon stopping")
