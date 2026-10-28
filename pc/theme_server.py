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
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser

from pc import theme

IDLE_S = 600            # 10 minutes with no edit (plan, revision 2026-10-25)
CLOSE_GRACE_S = 5       # a reload fires pagehide too, then loads again
MAX_BODY = 4096         # the widest theme is about 200 bytes of JSON
# A builder heartbeat (/api/ping) arrives every 20 s from an open page. A page
# heard within this long is "still open": the Launcher does not put a second
# tab beside it. Chrome slows a hidden tab's timers to once a minute, hence
# the margin over 60 s.
PAGE_ALIVE_S = 75
# One browser opened per this many seconds of "Edit on computer" (plan,
# Answers 2026-10-25). On the Launcher, not the Bridge: a Bridge is rebuilt on
# every reconnect, and a nudged cable must not reset the limit.
OPEN_EVERY_S = 10


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
            return self._json(200, {"saved": theme.load_saved(
                self.builder.home), "idle_s": self.builder.idle_s})
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
        if path == "/api/cancel":
            self._json(200, {"ok": True})
            return self.builder.finish("cancelled")
        if path == "/api/closed":
            self.builder.closing()
            return self._json(200, {"ok": True})
        if path == "/api/ping":
            # Alive, not active: an open tab nobody touches still times out.
            self.builder.seen()
            return self._json(200, {"ok": True})
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


class _HTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

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
        self._links = set()
        self._sessions = set()
        self._last = now()
        self._seen = None
        self._closed_at = None
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
        k = secrets.token_urlsafe(32)
        with self._lock:
            self._links.add(k)
        return f"http://127.0.0.1:{self.port}/open?k={k}"

    def redeem(self, k):
        with self._lock:
            for good in list(self._links):
                if _same(good, k):
                    self._links.discard(good)
                    s = secrets.token_urlsafe(32)
                    self._sessions.add(s)
                    self._last = self._seen = self._now()
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

    def seen(self):
        with self._lock:
            self._seen = self._now()
            self._closed_at = None

    def page_alive(self):
        """Is a builder page open on this server right now?"""
        with self._lock:
            return (not self.done and self._seen is not None
                    and self._closed_at is None
                    and self._now() - self._seen < PAGE_ALIVE_S)

    def closing(self):
        with self._lock:
            self._closed_at = self._now()

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
        self._lock = threading.Lock()

    def launch(self):
        """True when a builder page is open, or has just been opened."""
        with self._lock:
            now = self._now()
            server = self._server
            live = server is not None and not server.done
            if live and self._opened_at is not None and (
                    now - self._opened_at < OPEN_EVERY_S):
                return self._last_ok
            if live and server.page_alive():
                # The page is open in a tab already; a second one would
                # send previews that fight the first.
                return True
            fresh = not live
            if fresh:
                try:
                    server = self._make(self._home)
                except OSError as e:
                    # The page missing from a broken install, or no port to
                    # bind. One line, and the board's own fallback.
                    print(f"[theme] builder could not start: {e}",
                          file=sys.stderr)
                    self._server = None
                    return False
                server.start()
                self._server = server
            self._opened_at = now
            # Held across the opener on purpose, so two taps cannot open two
            # tabs; close() does not wait for it (see there).
            ok = bool(self._opener(server.new_link()))
            self._last_ok = ok
            if not ok and fresh:
                # Nothing was ever shown, so there is nothing to cancel; the
                # board puts up its own "Run blink theme" fallback.
                server.finish("no browser", cancel=False)
                if self._server is server:
                    self._server = None
            return ok

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
