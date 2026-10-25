"""Incidents on the providers' public status pages, as one level per page.

When Claude Code stops answering, the panel used to have nothing to say about
whose fault it was: the numbers freeze, the dot goes red on an API error, and
the owner opens a browser to find out whether it is their machine or the
service. status.claude.com and status.openai.com already answer that, in the
same Statuspage format, unauthenticated. This reads them and reduces each to
one of protocol.SVC_LEVELS plus the short name of the component responsible,
which the Bridge sends as a `svc` message on change.

What it is careful about, in order of how much a mistake would cost:

  - OFF BY DEFAULT, and off means no request at all -- not a request whose
    answer is ignored. Blink reads files on this machine, and the only host
    it otherwise talks to is github.com, once a day, for its own updates
    (README-full.md, "Security & privacy"). A periodic request to two more
    vendors is a change to that, so the owner makes it, with
    `blink config status-page on`.

  - Never an incident it was not told about. A timeout, a captive portal's
    HTML, a truncated body, a page that renamed its components: every one of
    those is "unknown", and unknown draws nothing. A false "major outage" on a
    desk is worse than a missed one, because it sends someone to debug a
    service that is fine.

  - Never clearing one early, either. A failed fetch is not news: the last
    good answer stands until it is STALE_S old, and only then goes back to
    unknown.

  - Short blips stay off the panel. Statuspage incidents are often opened and
    resolved inside a few minutes; an incident is shown only once two
    consecutive good polls report one, so at least POLL_S apart, and at the
    milder of their two levels, so no single poll can make it worse. An
    improvement needs no second opinion: holding a resolved outage on the
    desk for five more minutes is the one wrong this exists to avoid.

  - Only the pages the board is showing, and only while it is showing them.
    A Claude-only desk never asks openai.com anything, and an unplugged,
    reflashing or silent board stops the requests by itself (WANT_TTL_S).

  - Wall-clock ages. time.monotonic stops while a Mac or Linux machine
    sleeps -- measured on the desk Mac at 21.9 h of monotonic time against
    48.5 h of uptime -- so a monotonic half hour could last a night, an
    incident from before it would be re-sent on waking, and a report from
    before the night would confirm one from after it.

No response body is ever logged, at any level, in part or in whole: a log
line says which page and what level, and a failure says only its kind.
"""
import collections
import json
import os
import sys
import threading
import time
import urllib.request

from pc import interval, protocol, user_config
from pc.version import RELEASE_VERSION

# The key in <blink_home>/config.json. Absent, or anything but true, is off.
SETTING = "status_page"

# At most one request per page per five minutes. Statuspage's summary is a
# CDN-cached document and five minutes is already finer than most incidents
# are updated by hand; it also sets the debounce, which needs two polls.
POLL_S = 300

# How long the last good answer may stand in for a page we cannot read. Half
# an hour: long enough to ride out a laptop's Wi-Fi dropping for a meeting,
# short enough that a resolved incident cannot sit on a desk all afternoon
# because the machine went offline the moment before it was resolved.
STALE_S = 30 * 60

# How far the wall clock may step BACK before an answer's age stops making
# sense. An NTP slew is milliseconds; a clock a minute behind the moment an
# answer arrived is a clock that was reset, and an age measured across that
# is not one to hold an incident up on.
CLOCK_SLACK_S = 60

# Ten seconds: the summary is a few kilobytes (2.1 KB from status.claude.com
# and 5.3 KB from status.openai.com, both measured 2026-10-25), so anything
# slower than this is a network that is not going to deliver it.
TIMEOUT_S = 10

# And the whole body, however it arrives. A healthy reply takes well under a
# second; this is only for a server that answers a byte at a time.
READ_DEADLINE_S = 15
_CHUNK = 16 * 1024

# A cap on what is READ, not a check on what was read. Both pages measured
# under 6 KB; a body fifty times that is not a status summary, and reading an
# unbounded one into memory on somebody's laptop because a CDN misbehaved is
# not a risk worth taking for a status light.
MAX_BYTES = 256 * 1024

# How often the background thread looks at what is due. Not a fetch cadence
# -- POLL_S is -- just how quickly `blink config status-page off` is noticed.
TICK_S = 5

# How long a want() lasts. The Bridge renews it every fast tick (2 s) while
# the board answers its pings, so it only runs out when nobody is renewing:
# the cable is out, the board is being reflashed, the daemon is waiting for a
# port, or the board has gone quiet. Every one of those paths stops the
# requests by doing nothing, instead of each having to remember to say so.
# Twice the 60 s heartbeat, so one late heartbeat does not count as gone.
WANT_TTL_S = 2 * 60

# Serve summaries from a directory instead of the network: <dir>/claude.json
# and <dir>/codex.json. The same idea as ota.BLINK_OTA_DIR, and for the same
# reason: an incident cannot be arranged on demand, and the firmware half of
# this has to be shown one on a real board. With it set, BLINK_STATUS_POLL_S
# may shorten the poll; without it that variable is ignored, so nothing on a
# customer's machine can be talked into polling a vendor every second.
DIR_ENV = "BLINK_STATUS_DIR"
POLL_ENV = "BLINK_STATUS_POLL_S"

Page = collections.namedtuple("Page", "url components")

# (Statuspage component name, the short name the board is sent). The names
# are matched exactly; both were read off the live pages 2026-10-25. Order
# breaks ties: two components at the same level name the one listed first,
# which is the one the panel's owner is most likely using. The order of the
# pages themselves is the order the Bridge sends them in.
#
# "Responses" is the OpenAI API Codex calls, so it is "API" on the board the
# same way the Claude API is. status.openai.com lists "Login" twice, once per
# product group; a name that appears twice counts at its worse status.
PAGES = {
    "claude": Page("https://status.claude.com/api/v2/summary.json", (
        ("Claude Code", "Claude Code"),
        ("claude.ai", "claude.ai"),
        ("Claude API (api.anthropic.com)", "API"),
    )),
    "codex": Page("https://status.openai.com/api/v2/summary.json", (
        ("Codex in ChatGPT Desktop", "Codex"),
        ("Responses", "API"),
        ("Login", "Login"),
    )),
}

# Statuspage's five component statuses, onto the wire's five levels.
_LEVEL_OF = {
    "operational": "ok",
    "under_maintenance": "maint",
    "degraded_performance": "degraded",
    "partial_outage": "partial",
    "major_outage": "major",
}

OK = ("ok", "")


def _rank(level):
    """Worse is larger, in the wire's own order: maintenance sits under every
    real fault, because it was announced and a degraded component beside it
    is the thing worth naming. Read off protocol.SVC_LEVELS so a level added
    there cannot be missing here."""
    return protocol.SVC_LEVELS.index(level)


def enabled(blink_home):
    """True or False for the owner's choice; None when it cannot be read.

    None is not "off". A file mid-edit, a permissions slip or an antivirus
    holding it for a moment would otherwise switch the feature off for one
    tick -- wiping a confirmed incident from the panel and resetting the
    five-minute spacing, so the very next tick fetched again. The watch
    keeps what it has and asks nothing until the answer is readable.
    """
    try:
        return user_config.load(blink_home).get(SETTING) is True
    except (user_config.Unreadable, OSError):
        return None


def parse_summary(body: bytes, provider: str):
    """(level, what) for `provider`'s page, or None when it says nothing.

    None -- unknown -- for anything that is not a summary naming at least one
    of our components with a status string, and that promise covers
    every shape a body can take: a status that is a list or an object, or a
    document nested ten thousand deep, is None here and not an exception. A
    page that dropped or renamed every component we watch has not told us
    they are fine, and reporting "ok" for it would be making that up.
    """
    page = PAGES.get(provider)
    if page is None:
        return None
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    comps = doc.get("components") if isinstance(doc, dict) else None
    if not isinstance(comps, list):
        return None
    order = {name: i for i, (name, _) in enumerate(page.components)}
    short = dict(page.components)
    best = None                 # (rank, -order, level, what)
    for c in comps:
        if not isinstance(c, dict):
            continue
        name, status = c.get("name"), c.get("status")
        if not isinstance(name, str) or not isinstance(status, str):
            continue
        if name not in order:
            continue
        # A status string Statuspage adds tomorrow is not "operational", and
        # skipping it would let an outage read as ok. It counts as the
        # mildest fault rather than as nothing.
        level = _LEVEL_OF.get(status, "degraded")
        key = (_rank(level), -order[name], level, short[name])
        if best is None or key > best:
            best = key
    if best is None:
        return None
    level, what = best[2], best[3]
    return OK if level == "ok" else (level, what)


class _TooSlow(Exception):
    pass


def _socket_of(r):
    """The socket under an http.client response, or None for anything else.

    HTTPResponse keeps it at fp.raw._sock (a BufferedReader over SocketIO).
    Private, so every step is a getattr: a response of another shape -- a
    test's, or a future Python's -- simply gets no per-read timeout.
    """
    raw = getattr(getattr(r, "fp", None), "raw", None)
    return getattr(raw, "_sock", None)


def _read_capped(r, deadline, clock):
    """Up to MAX_BYTES + 1 of `r`, in chunks, inside the deadline.

    TIMEOUT_S on its own bounds each socket read, not the whole body, and
    HTTPResponse.read(n) makes that worse: it blocks across reads until it
    has n bytes, so a server trickling a byte a second held the thread --
    and every other page's turn -- until it chose to stop. read1 returns
    whatever has arrived, and the socket's timeout is cut to the time left
    before every read, so the read ends by the deadline either way.
    """
    read = getattr(r, "read1", None) or r.read
    sock = _socket_of(r)
    chunks, n = [], 0
    while n <= MAX_BYTES:
        left = deadline - clock()
        if left <= 0:
            raise _TooSlow()
        if sock is not None:
            sock.settimeout(left)
        chunk = read(min(_CHUNK, MAX_BYTES + 1 - n))
        if not chunk:
            break
        chunks.append(chunk)
        n += len(chunk)
    return b"".join(chunks)


def _read(provider, opener=None, clock=time.monotonic):
    """(result, why): one page's answer, and a reason when it is None.

    Reading and parsing share ONE try. They used not to, and an exception out
    of the parse escaped tick() after the next page's turn had already been
    spent -- so a bad Claude body meant Codex was never fetched at all.

    The whole request has READ_DEADLINE_S: the connect and the headers get
    min(TIMEOUT_S, the time left) as urlopen's timeout, and the body is read
    against the deadline in _read_capped. A server trickling its HEADERS is bounded only per byte by
    that timeout -- http.client offers no hook between header bytes.

    `why` is the KIND of failure and nothing more -- an exception's class
    name, or a fixed phrase. Never the body, never a slice of it, and not an
    exception's message either, which for a JSON error quotes the document.
    """
    deadline = clock() + READ_DEADLINE_S
    try:
        local = os.environ.get(DIR_ENV)
        if local:
            with open(os.path.join(local, provider + ".json"), "rb") as f:
                body = f.read(MAX_BYTES + 1)
        else:
            from pc import ota          # its CA bundle; see ota._ssl_context
            req = urllib.request.Request(PAGES[provider].url, headers={
                "User-Agent": f"blink/{RELEASE_VERSION}",
                "Accept": "application/json",
            })
            # Looked up at call time, so a test that replaces urlopen is
            # what actually runs.
            urlopen = opener if opener is not None else urllib.request.urlopen
            timeout = max(0.1, min(TIMEOUT_S, deadline - clock()))
            with urlopen(req, timeout=timeout,
                         context=ota._ssl_context()) as r:
                body = _read_capped(r, deadline, clock)
        if len(body) > MAX_BYTES:
            return None, f"reply over {MAX_BYTES // 1024} KB"
        result = parse_summary(body, provider)
    except _TooSlow:
        return None, f"reply too slow (over {READ_DEADLINE_S} s)"
    except Exception as e:
        return None, type(e).__name__
    return result, (None if result is not None else "not a status summary")


def _poll_interval():
    if not os.environ.get(DIR_ENV):
        return POLL_S
    return max(1.0, interval.seconds_from_env(os.environ, POLL_ENV, POLL_S,
                                              prefix="[status]"))


def _fresh(at, now, limit):
    """Is a wall-clock stamp `at` within `limit` seconds before `now`?

    False for a stamp too far in the future as well: see CLOCK_SLACK_S.
    """
    return at is not None and -CLOCK_SLACK_S <= now - at <= limit


class _PageState:
    def __init__(self):
        self.shown = OK         # the confirmed answer
        self.prev = None        # the last good answer, confirmed or not
        self.good_at = None     # wall time that answer arrived
        self.asked_at = None    # wall time of the last request
        self.failing = False    # for logging the transition only


class StatusWatch:
    """What each status page says, debounced, for the Bridge to send.

    Two threads touch it. The Bridge, on the serial loop, calls want() and
    shown(), both of which only read memory. Fetching happens in tick(), on a
    thread of its own (start()) -- never on the serial loop, where two 10 s
    timeouts back to back would hold the board's pongs past its 30 s
    liveness window and put HOST LOST on a desk because a website was slow.

    `enabled` is a zero-argument callable answering True, False or None
    (see enabled() above). `now` is WALL time, time.time by default -- see
    the module docstring for why monotonic time is wrong here.
    """

    def __init__(self, enabled, opener=None, now=time.time):
        self._enabled = enabled
        self._opener = opener
        self._now = now
        self._poll_s = _poll_interval()
        self._lock = threading.Lock()
        self._wanted = frozenset()
        self._wanted_at = None
        self._pages = {}

    def want(self, providers):
        """The providers whose pages are on the board right now.

        Lasts WANT_TTL_S unless said again.
        """
        with self._lock:
            self._wanted = frozenset(p for p in providers
                                     if isinstance(p, str))
            self._wanted_at = self._now()

    def shown(self, provider):
        """(level, what) the board should draw for `provider` now.

        The staleness test here is not redundant with tick()'s. A page that
        left the board stops being ticked, so its state is not expired; when
        it comes back, the Bridge asks this before the next tick has run, and
        without the test it would be handed an incident an hour old.
        """
        with self._lock:
            st = self._pages.get(provider)
            if st is None or not _fresh(st.good_at, self._now(), STALE_S):
                return OK
            return st.shown

    def tick(self):
        """Fetch whatever is due, and say how many pages were asked.

        Blocks for the fetches; see start(). The setting is asked again
        before every request, so `off` stops the one after it rather than
        waiting for the whole round.
        """
        if not self._settle():
            return 0
        # Log lines are collected under the lock and printed after it. A
        # print can block -- a full pipe, the Journal's own lock -- and
        # shown(), on the serial loop, would wait behind it.
        notes = []
        with self._lock:
            now = self._now()
            wanted = (self._wanted
                      if _fresh(self._wanted_at, now, WANT_TTL_S)
                      else frozenset())
            due = []
            for p in PAGES:
                if p not in wanted:
                    continue
                st = self._pages.setdefault(p, _PageState())
                self._expire(p, st, now, notes)
                # Due after POLL_S of wall time -- which a night's sleep
                # passes, so a waking machine asks at once rather than
                # showing what it knew yesterday. A clock that went back past
                # the last request would read to arithmetic as "not for
                # hours"; that request is counted as made just now instead,
                # so the next is POLL_S from here -- never stuck, and never
                # two requests closer than POLL_S.
                if st.asked_at is not None and now < st.asked_at:
                    st.asked_at = now
                if st.asked_at is None or now - st.asked_at >= self._poll_s:
                    due.append((p, st, st.asked_at))
                    st.asked_at = now
        asked = 0
        for i, (p, st, before) in enumerate(due):
            if not self._settle():
                # Cut short -- off, or a config file that could not be read.
                # The pages not reached keep the turn they had, so they are
                # asked on the next good tick rather than five minutes on.
                with self._lock:
                    for _, rest, was in due[i:]:
                        rest.asked_at = was
                break
            # (the notes from the pages asked so far still go out below)
            result, why = _read(p, self._opener)
            asked += 1
            with self._lock:
                # Identity, not membership: off-then-on during the request
                # replaced the state, and this answer belongs to the old one.
                if self._pages.get(p) is st:
                    self._observe(p, st, result, why, self._now(), notes)
        for line in notes:
            print(line, file=sys.stderr)
        return asked

    def _settle(self):
        """True when it may ask. Off forgets; unreadable keeps and waits."""
        state = self._enabled()
        if state is True:
            return True
        if state is False:
            with self._lock:
                # Forget everything, so turning it back on starts over: a
                # confirmation from before it was off is not one.
                self._pages.clear()
        return False

    def _expire(self, provider, st, now, notes):
        if st.good_at is None or _fresh(st.good_at, now, STALE_S):
            return
        # Too old to stand for the page. The unconfirmed answer goes too, so
        # the next incident needs two FRESH reports, not one fresh report
        # paired with one from before the machine slept.
        st.prev = None
        st.good_at = None
        if st.shown != OK:
            st.shown = OK
            notes.append(f"[status] {provider} unknown (no answer for"
                         f" {STALE_S // 60} min)")

    def _observe(self, provider, st, result, why, now, notes):
        if result is None:
            # Reported once per run of failures, not every five minutes.
            if not st.failing:
                notes.append(f"[status] {provider} page unreadable ({why})")
                st.failing = True
            return
        st.failing = False
        self._expire(provider, st, now, notes)
        before = st.shown
        if result == OK or _rank(result[0]) < _rank(before[0]):
            # Better news needs no confirmation. Being wrong about it costs
            # one poll of a milder panel; holding it would keep a resolved
            # outage on the desk after the vendor has said it is over.
            st.shown = result
        elif st.prev is not None and st.prev != OK:
            # Two incidents in a row: that there IS one is confirmed. How bad
            # takes the milder of the two, so no single poll can raise the
            # level on its own -- and the component comes with the level,
            # from whichever poll supplied it, so "maint" is never paired
            # with the component that is actually down. A tie names now.
            #
            # It used to need the identical (level, component) twice, and
            # real outages do not hold still: degraded, partial, major,
            # major showed nothing until the fourth poll, and one flapping
            # between partial and major stayed "ok" for as long as it lasted.
            st.shown = (st.prev if _rank(st.prev[0]) < _rank(result[0])
                         else result)
        st.prev = result
        st.good_at = now
        if st.shown != before:
            level, what = st.shown
            notes.append(f"[status] {provider} {level}"
                         + (f" ({what})" if what else ""))

    def start(self):
        """Run tick() every TICK_S on a daemon thread, for the process's life."""
        t = threading.Thread(target=self._run, name="blink-status",
                             daemon=True)
        t.start()
        return t

    def _run(self):
        said = None
        while True:
            try:
                if self.tick():
                    said = None
            except Exception as e:
                # A status light is never worth the daemon. Said once per run
                # of failures, by kind only -- and a run ends only at a tick
                # that actually asked a page and came back, not at the idle
                # ticks between requests, which used to reset it every 5 s
                # and repeat the line at every poll.
                if said != type(e).__name__:
                    said = type(e).__name__
                    print(f"[status] check failed: {said}", file=sys.stderr)
            time.sleep(TICK_S)
