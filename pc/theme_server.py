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
page says so on pagehide, and a reload inside CLOSE_GRACE_S takes it back),
or IDLE_S with no edit, write a cancel for the daemon -- the board goes back
to its stored theme -- and stop.
"""
import hmac
import http.server
import json
import os
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser

from pc import theme

IDLE_S = 600            # 10 minutes with no edit (plan, revision 2026-10-25)
CLOSE_GRACE_S = 5       # a reload fires pagehide too, then loads again
# How long a one-time link may wait to be used. The CLI prints one for a
# person to click when no browser opened, so it is the builder's own idle
# limit: a link older than that belongs to a builder that would have
# stopped anyway.
LINK_LIFE_S = 600
MAX_BODY = 4096         # the widest theme is about 200 bytes of JSON
# The page's tab id on a preview, save or cancel. A header and not a body
# field because those bodies are the theme itself, which theme.validate
# checks key by key; see _Handler.do_POST.
TAB_HEADER = "X-Blink-Tab"
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
OPEN_EVERY_S = 2


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
        self._send(status, b"Not allowed. Run blink theme again.\n")

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
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.builder.port
        return origin in (f"http://127.0.0.1:{port}",
                          f"http://localhost:{port}")

    # -- GET

    def do_GET(self):
        if not self._host_ok():
            return self._refuse()
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/open":
            k = urllib.parse.parse_qs(url.query).get("k", [""])[0]
            session = self.builder.redeem(k)
            if session is None:
                return self._refuse()
            return self._send(303, headers=(
                ("Location", "/"),
                ("Set-Cookie", f"{cookie_name(self.builder.port)}="
                               f"{session}; HttpOnly;"
                               " SameSite=Strict; Path=/")))
        if not self._session_ok():
            return self._refuse()
        if url.path == "/":
            self.builder.touch()
            return self._send(200, self.builder.page_bytes,
                              "text/html; charset=utf-8", headers=(
                ("Content-Security-Policy",
                 "default-src 'none'; script-src 'unsafe-inline';"
                 " style-src 'unsafe-inline'; connect-src 'self';"
                 " img-src data:; frame-ancestors 'none';"
                 " form-action 'none'; base-uri 'none'"),
                ("X-Frame-Options", "DENY")))
        if url.path == "/api/state":
            self.builder.touch()
            # "draft": the last preview this builder took, so a tab opened
            # by another press of Edit on computer starts on the colours the
            # board is showing and not on the saved theme. Without it the
            # older tab was told to stop and the newer one opened on the
            # saved theme: the draft was on the board and in no tab that
            # could still send it (review of fp/edit-reset r2, SHOULD-1).
            return self._json(200, {"saved": theme.load_saved(
                self.builder.home), "draft": self.builder.draft,
                "idle_s": self.builder.idle_s})
        return self._refuse(404)

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
        raw, err = self._body()
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
        if path in ("/api/preview", "/api/save", "/api/cancel") and \
                self.builder.superseded(_tab_id(
                    self.headers.get(TAB_HEADER))):
            return self._json(409, {"newer": True, "error":
                                    "Continued in a newer tab."})
        if path == "/api/cancel":
            self._json(200, {"ok": True})
            return self.builder.finish("cancelled")
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
            theme.write_preview(self.builder.home, t)
            self.builder.draft = t
            return self._json(200, {"ok": True})
        keep = theme.saved_custom(self.builder.home) \
            if t["id"] != theme.CUSTOM else None
        try:
            theme.save(self.builder.home, t, keep_custom=keep)
        except theme.Unreadable as e:
            # theme.json is there and unreadable, so it is not written over
            # (contract Addendum 4a); the message says which file, not what
            # it holds.
            return self._json(500, {"error": f"Not saved: {e}."})
        except OSError as e:
            # Said to the page, and the builder stays up so Save can be tried
            # again. "Saved" over a failed write is a lie the owner acts on.
            return self._json(500, {"error": "Not saved: this computer could"
                                    f" not write the theme file ({e})."})
        # A cancel, not just dropping the preview file: a preview still
        # waiting would reach the board after the save and show something
        # other than what was just stored, and one already sent stays on the
        # board for 15 minutes when this Save picked the theme already
        # stored, because then no save line goes out to end it (review
        # NICE-4). ThemeSync.outgoing drops the cancel when a save is owed.
        theme.write_cancel(self.builder.home)
        self._json(200, {"ok": True})
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

    def handle_error(self, request, client_address):
        # socketserver prints a traceback to stderr here, and under the daemon
        # stderr is the log a customer sends us. A failed request is the
        # browser's problem, not the log's.
        pass


class BuilderServer:
    """One builder session: a server, its tokens, and its deadlines."""

    def __init__(self, home, idle_s=IDLE_S, now=time.monotonic, tick_s=1.0):
        self.home = home
        self.idle_s = idle_s
        self._now = now
        self._tick_s = tick_s
        self._lock = threading.Lock()
        self._links = {}            # unspent link -> when it was made
        self._sessions = set()
        self._last = now()
        self._seen = None
        self._closed_at = None
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
        self.outcome = None
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

    def listening_socket(self):
        return self._httpd.socket

    def start(self):
        threading.Thread(target=self._httpd.serve_forever,
                         kwargs={"poll_interval": 0.2},
                         name="blink-theme-http", daemon=True).start()
        threading.Thread(target=self._watch, name="blink-theme-idle",
                         daemon=True).start()

    def _watch(self):
        while not self._done.wait(self._tick_s):
            self.tick()

    # -- tokens

    def new_link(self):
        """A one-time link. It replaces any link not yet used, and lives
        LINK_LIFE_S: "Edit on computer" mints one on every reshow (contract
        Addendum 6), and each used to stay valid, unspent, for the server's
        whole life (review of fp/audit-nice r2, NICE 1). A spent link was
        always refused; now so is a superseded or stale one."""
        k = secrets.token_urlsafe(32)
        with self._lock:
            self._links = {k: self._now()}
        return f"http://127.0.0.1:{self.port}/open?k={k}"

    def redeem(self, k):
        with self._lock:
            now = self._now()
            for good, made in list(self._links.items()):
                if _same(good, k):
                    del self._links[good]
                    if now - made >= LINK_LIFE_S:
                        return None
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
        otherwise it keeps its colours on screen under a notice saying it
        is no longer active, which offers the share code of any changes in
        it (showNotice in the page), and the newer tab opens on the last
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
            if self._tabs:
                return      # another tab is still showing this builder
            self._closed_at = now

    def tick(self):
        with self._lock:
            now = self._now()
            closed = (self._closed_at is not None
                      and now - self._closed_at >= CLOSE_GRACE_S)
            idle = now - self._last >= self.idle_s
        if closed:
            self.finish("closed")
        elif idle:
            self.finish("idle")

    def finish(self, outcome, cancel=True):
        """Stop. Anything but a save also tells the board to drop the
        preview, through the same file every preview went through."""
        with self._lock:
            if self.outcome is not None:
                return
            self.outcome = outcome
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


class Launcher:
    """The daemon's builder: at most one running, reopened on request."""

    def __init__(self, home, opener=None, make_server=BuilderServer,
                 now=time.monotonic):
        self._home = home
        self._opener = opener or open_url
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

    def _open(self, server):
        """Open a browser on `server`, or on a new builder when it is None.
        (ok, the server it was opened on). Called with the lock held."""
        fresh = server is None
        if fresh:
            try:
                server = self._make(self._home)
            except OSError as e:
                # The page missing from a broken install, or no port to
                # bind. One line, and the board's own fallback.
                print(f"[theme] builder could not start: {e}",
                      file=sys.stderr)
                self._server = None
                return False, None
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
