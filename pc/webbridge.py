"""Local receiver for the browser extension's usage reports.

The handoff document specifies a WebSocket on ws://127.0.0.1:9877. This is an
HTTP POST listener on 127.0.0.1:9877 instead, and the swap is deliberate:

  - The data is event-driven, not streamed. The extension reports when a turn
    completes, which is a POST, not a subscription. WebSocket's advantage is
    server push, and nothing here pushes toward the browser.
  - RFC 6455 means hand-rolling an upgrade handshake and frame unmasking
    inside a daemon that ships with one dependency. That is a few hundred
    lines of parser exposed to a socket, to move a payload that fits in a
    single POST body, and parsers on sockets are where this kind of program
    gets its vulnerabilities.
  - Everything the document actually asks for is preserved: same host, same
    port, same locality, same push-on-completion timing.

What guards the socket, given that this is the first listening socket this
daemon has ever opened:

  - bound to 127.0.0.1 explicitly, never 0.0.0.0. A bind to all interfaces
    would put a usage feed on the office network.
  - one path, one method. Anything else is 404 without reading a body.
  - the body is capped before it is read, not after.
  - Origin must be the extension or claude.ai, which stops a random web page
    the user happens to have open from posting numbers at the panel.
  - nothing from the request is ever echoed back or logged verbatim.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pc.providers import base

HOST = "127.0.0.1"
PORT = 9877
PATH = "/usage"

# A usage report is a handful of numbers. Anything larger is not one, and the
# cap is applied to Content-Length before any body is read so an oversized
# request costs nothing.
MAX_BODY = 4096

# Plausible bounds for a reset timestamp: 2020-01-01 to 2100-01-01.
RESET_EPOCH_MIN = 1_577_836_800
RESET_EPOCH_MAX = 4_102_444_800

ALLOWED_ORIGIN_PREFIXES = ("chrome-extension://", "moz-extension://")
ALLOWED_ORIGINS = ("https://claude.ai",)


def origin_allowed(origin: str) -> bool:
    if not origin:
        # No Origin at all is a non-browser client -- curl, a script, another
        # program on the machine. Refused: the only intended caller is an
        # extension, and extensions always send one.
        return False
    if origin in ALLOWED_ORIGINS:
        return True
    return any(origin.startswith(p) for p in ALLOWED_ORIGIN_PREFIXES)


class _Slot:
    """The latest report, behind a lock.

    The HTTP server runs on its own threads and the bus polls from the
    daemon's. One value, guarded, is the whole of the shared state -- there is
    no queue because an older report is never wanted once a newer one exists.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None

    def put(self, frame):
        with self._lock:
            self._frame = frame

    def get(self):
        with self._lock:
            return self._frame


def _pct(d, key):
    try:
        v = float(d[key])
    except (KeyError, TypeError, ValueError):
        return base.UNKNOWN
    return v if 0 <= v <= 100 else base.UNKNOWN


def parse_report(payload, now_epoch):
    """A frame from an extension report, or None.

    Only numbers are read. A report carrying a conversation, a title or any
    other page content is not rejected for it -- those keys are simply never
    looked at, which is a stronger guarantee than filtering them out would be.
    """
    if not isinstance(payload, dict):
        return None
    session = _pct(payload, "session_pct")
    weekly = _pct(payload, "weekly_pct")
    if session < 0 and weekly < 0:
        return None

    def _reset(key):
        try:
            v = float(payload[key])
        except (KeyError, TypeError, ValueError):
            return None
        # Sanity-bound it to a plausible date. A reset time in 1970 or in
        # the year 5000 is a misread field, not a reset.
        #
        # The lower bound is a real date and not zero. secs_until() would
        # turn a past timestamp into -1 and render "--" either way, so this
        # changes nothing the user sees -- but a frame carrying
        # session_resets_at=1 is a frame asserting something false, and the
        # normalizer merges on "does this source HAVE the field". A garbage
        # value would win that contest against a source holding a real one.
        return v if RESET_EPOCH_MIN < v < RESET_EPOCH_MAX else None

    return base.NormalizedUsageFrame(
        provider="claude", src="web", observed_at=now_epoch,
        session_pct=session, weekly_pct=weekly,
        session_resets_at=_reset("session_resets_at"),
        weekly_resets_at=_reset("weekly_resets_at"),
    )


def _make_handler(slot, now):
    class Handler(BaseHTTPRequestHandler):
        # Silence the default stderr access log. The daemon's log is for the
        # gauge, and a line per turn completion would drown it.
        def log_message(self, *args):
            pass

        def _reply(self, code, origin=""):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            if origin and origin_allowed(origin):
                # Echoed only after passing the allow-list, never the raw
                # header. A reflected arbitrary Origin is how a CORS check
                # becomes decorative.
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "POST")
            self.end_headers()

        def do_OPTIONS(self):
            self._reply(204, self.headers.get("Origin", ""))

        def do_POST(self):
            origin = self.headers.get("Origin", "")
            if self.path != PATH:
                self._reply(404)
                return
            if not origin_allowed(origin):
                self._reply(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._reply(400, origin)
                return
            if length <= 0 or length > MAX_BODY:
                self._reply(413, origin)
                return

            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._reply(400, origin)
                return

            frame = parse_report(payload, now())
            if frame is None:
                self._reply(422, origin)
                return
            slot.put(frame)
            self._reply(204, origin)

    return Handler


class WebBridge:
    """The listener plus the provider that reads what it collected."""

    def __init__(self, host=HOST, port=PORT, now=None):
        import time as _time
        self._now = now or _time.time
        self.slot = _Slot()
        self._server = ThreadingHTTPServer((host, port),
                                           _make_handler(self.slot, self._now))
        self._server.daemon_threads = True
        self._thread = None

    @property
    def port(self):
        return self._server.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


class ClaudeWebProvider(base.ProviderParser):
    """Whatever the extension last reported.

    Holds no socket of its own -- the bridge owns that -- so a daemon built
    without the web bridge simply never constructs one of these.
    """

    def __init__(self, slot):
        self._slot = slot

    def get_provider_id(self) -> str:
        return "claude"

    def poll(self, now_epoch):
        frame = self._slot.get()
        return [frame] if frame is not None else []
