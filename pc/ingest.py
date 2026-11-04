"""The pluggable ingestion bus: every provider, polled and normalized.

This is the seam the daemon holds. claude_usage_bridge asks it for a usage
message and knows nothing about which providers exist, how many sources each
one has, or what any of their files look like -- which is the property that
lets a second provider be added without touching the daemon, the protocol or
the firmware.

A provider that misbehaves is contained here rather than being allowed to
reach the reconnect loop. The daemon's job is to keep a board fed; a parser
for an app we do not control is exactly the kind of code that should never be
able to stop it.
"""
import sys
import time

from pc import (burn_reader, claude_account, claude_plan, cowork_audit,
                desktop_idb, desktop_paths, normalizer, protocol,
                session_verdict, spend_count, weekly_anchor)
from pc.providers import base
from pc.providers import claude_cli
from pc.providers.claude_cli import ClaudeCliProvider
from pc.providers.claude_desktop import ClaudeDesktopProvider
from pc.providers.claude_desktop_ls import ClaudeDesktopLocalStorageProvider
from pc.providers.claude_state import ClaudeStateProvider
from pc.providers.codex_cli import CodexCliProvider
from pc.providers.weekly_anchor import DesktopHistoryProvider, WeeklyAnchorProvider
from pc.providers.weekly_anchor import SRC_ID as WEEKLY_ANCHOR_SRC_ID


# How long a provider that raised sits out before it is tried again, and how
# far that grows while it keeps failing.
#
# Not forever, which is what it used to be. The daemon's poll dropped from 60 s
# to 2 s (claude_usage_bridge.FAST_POLL_INTERVAL_S), so a provider now gets 30
# chances a minute to catch a file mid-rewrite, an EBUSY, a half-flushed JSON
# line -- and under the old rule the first one it lost retired it until the user
# restarted the daemon. Every one of those failures is transient by
# construction: the file that could not be parsed this second is written by
# another application that is still running and will finish its write.
#
# One minute first, because a transient should cost about a minute and not a
# workday. Doubling, because a provider whose upstream really has changed shape
# should not be re-parsed thirty times an hour for the life of the process.
# Fifteen minutes at the ceiling, which is short enough that a user who fixes
# the cause -- signs in, upgrades the other app -- sees the panel come back
# without being told to restart anything.
FIRST_RETRY_AFTER_S = 60.0
MAX_RETRY_AFTER_S = 900.0


def default_providers(counter=None, plan=None, home=None):
    """Every provider shipped today, in preference order.

    Order is not the merge rule -- pc/normalizer merges by recency per field,
    not by position -- but it does decide which source is polled first and so
    which one a tie resolves toward.

    `counter` is the bus's pc/spend_count.SpendCounter and `plan` its
    claude_plan.PlanReader, both handed to the status-line provider: the
    count is the bus's to keep, and one reader serves every user of the
    config. None for either gives that provider its own (a count in memory
    only, a reader of its own), which is what `blink status` wants.

    `home` is IngestionBus's: whose drop folder the Claude Code providers
    read.
    """
    return [ClaudeCliProvider(counter=counter, plan=plan, home=home),
            ClaudeDesktopProvider(),
            ClaudeDesktopLocalStorageProvider(),
            ClaudeStateProvider(home=home), CodexCliProvider(),
            # LAST: it only ever offers a remembered projection, and every
            # source above it is polled first so any of them carrying a real
            # weekly reset is what observe() below learns from this cycle.
            #
            # history_provider gives it something to be refuted BY: Claude
            # Desktop's own usage cache, the one file on this machine with a
            # weekly-percentage series to check the projection against. With
            # no Claude Desktop installed the file is simply absent and
            # DesktopHistoryProvider.samples() returns None -- refutation
            # cannot fire, and this is unchanged from having no
            # history_provider at all.
            WeeklyAnchorProvider(history_provider=DesktopHistoryProvider())]


# IngestionBus(verdict=...)'s default: a persisted SessionVerdict, built per
# bus. A sentinel and not None, because None means "mark nothing".
DEFAULT_VERDICT = object()


class IngestionBus:
    def __init__(self, providers=None, preferred_provider="claude",
                 now=time.time, burn=None, verdict=DEFAULT_VERDICT,
                 account=None, counter=None, plan_source=None, home=None):
        """`plan_source`, when given, is where the bus's one reader of
        Claude Code's config takes its view from instead of the file: a
        WSL home's pc/wsl_watch.WslWatch.view, which answers from memory,
        so the config -- a UNC path into the distribution -- is never
        stat'ed or read on the serial loop (docs/plans/enterprise-spend.md,
        section 4, "Reading"). None reads the file, as every native desk
        does.

        `home`, a callable returning a claude_home.ClaudeHome, pins whose
        Claude Code the bus's own readers read: the config, the status
        line payload and the hook state. The daemon's bus is built once
        per process, and with None each of them asks
        claude_home.current() on every poll, so a native bus followed a
        record that turned WSL under it -- `install --wsl` saves before it
        restarts, and a refused restart never comes -- and the serial loop
        stat'ed UNC paths (final branch review, finding 5).

        `counter` (a pc/spend_count.SpendCounter) is used only when the
        bus builds its own providers (`providers` None): it is handed to
        the status-line provider there. With `providers` given it feeds
        nothing -- those providers were built by the caller, who gives a
        ClaudeCliProvider its counter directly -- and is asked only for
        spend_burn()'s buckets: a scenario's bus passes the scenario's
        stand-in (scripted.ScriptedSpendCount) for exactly that."""
        # The login watch (pc/claude_account), made here only for the
        # shipped providers. A bus built from a scenario exists to replay the
        # same timeline on any desk, and whoever happens to be signed in to
        # Claude on that desk is exactly what it must not depend on.
        #
        # One reader of Claude Code's config for the whole bus: the login
        # watch, the status-line provider (the Ultra plan, the spend seat
        # and its saved /usage) and the spend-seat check in _collect_frames
        # below all take their view from it, so the file is looked at once a
        # poll (PlanReader.look) and all three agree on what it said. None on
        # a scenario's bus, for the reason above: it never asks.
        self._plan_reader = None
        if providers is None:
            self._plan_reader = claude_plan.PlanReader(source=plan_source,
                                                       home=home)
            if account is None:
                account = claude_account.AccountWatch(self._plan_reader.look)
        self._account = account
        # False until a frame on this bus has had a plan other than windows
        # -- in practice, until Claude Code's status line has carried a
        # spend cap, or the config has named a seat billed by usage. Until
        # then the login watch is never polled and no frame is filtered for
        # it, so a desk that has never seen a spend reading merges exactly
        # as it did before any of this existed (the config is still looked
        # at once a poll, for the seat: _spend_seat). Once true it
        # stays true for the life of the process: the guard matters most
        # straight AFTER the spend page goes away, at the switch back.
        self._plans_seen = False
        # The running spend count (pc/spend_count), the bus's to own:
        # persisted in ~/.blink/spend-count.json, and fed and saved only
        # while the config names a spend seat. `counter` replaces it --
        # `blink status --wire` passes one that writes nothing.
        self._counter = counter
        if providers is None:
            if counter is None:
                counter = spend_count.SpendCounter()
            self._counter = counter
            self._providers = default_providers(counter, self._plan_reader,
                                                home)
        else:
            self._providers = list(providers)
        self._preferred = preferred_provider
        self._now = now
        # Weighs what the claude sources prove about the account's
        # five-hour window and marks their frames before the merge
        # (pc/session_verdict). The default is the persisted one under
        # ~/.blink. None marks nothing, and a bus that replays a scenario
        # is built with None (claude_usage_bridge.build_bus): its steps say
        # for themselves whether the account has a window, and the desk's
        # own verdict -- an Ultra desk's "no window", say -- must not decide
        # it for them on a bench.
        self._verdict = (session_verdict.SessionVerdict()
                         if verdict is DEFAULT_VERDICT else verdict)
        # What answers burn() below: anything with poll(now) -> (q, tokens)
        # or None -- pc/burn_reader.BurnReader on a desk, the scenario's
        # replay under BLINK_SCENARIO. None, the default, means no burn line
        # at all. It is handed in rather than built here so that a bus made
        # of scripted providers can never read the machine's real
        # transcripts: claude_usage_bridge.build_bus decides, the way it
        # decides the providers. One reader is the claude page's; a dict
        # gives each provider's page its own ({"claude": BurnReader(),
        # "codex": CodexBurnReader()} on a desk). A WSL home's claude page
        # has its watch's burn_source() instead (pc/wsl_watch): answers
        # from memory, read on the watch's thread, so it is not a
        # BurnReader and burn() below asks it as it asks a scenario's.
        if burn is not None and not isinstance(burn, dict):
            burn = {claude_cli.PROVIDER_ID: burn}
        self._burn = burn or {}
        # Every merged frame the last poll() put on one of the board's two
        # pages, by provider. A page's no_session_window is the one
        # condition under which its burn line is sent, read off this frame
        # rather than copied into a flag of its own, so that it answers for
        # the same poll the usage line came from. See page().
        self._pages = {}
        # What is left of the read budget for this pass's burn lines, shared
        # by every reader (burn_reader.READ_BUDGET_BYTES, or a reader's own
        # when it was given less); None for a full one. Filled by
        # burn_pass(), which the bridge asks once a pass of the serial loop,
        # and when it is, the page asked first moves on by one (_burn_turn):
        # pages always asked in the same order left a Codex page owed 10 KB
        # with nothing for 55 passes behind a 430 KB Claude cold start (3rd
        # review, 2026-11-01).
        #
        # The pass is the bridge's to say, not the clock's. Until the 4th
        # review of 2026-11-01 a pass was any 1.5 s since the last fill
        # (BURN_PASS_S), re-checked by every burn(), beside a 1.0 s gate on
        # asking a page again (bridge.BURN_ASK_GAP_S). On a desk where a
        # budget's read takes a second or more neither held: at 1.0-1.5 s
        # the second page was asked with nothing left, its ask stamped, and
        # skipped on its own turn the pass after, so a Codex page waited out
        # the whole Claude backlog (the review's repro: pass 7 behind
        # 200 MiB); at 1.5 s or more the gate ran out inside the pass and
        # the second page was handed a budget of its own, two a pass and
        # four with the heartbeat in it.
        self._burn_budget = None
        self._burn_turn = 0
        # {id(provider): {"since", "retry_at", "wait"}} for providers sitting
        # out after a failure. Keyed by IDENTITY, not by class: two instances
        # of the same provider class are two sources -- two Codex homes, the
        # same parser pointed at a second directory -- and under the old class
        # key one of them raising silenced its healthy twin, which reads on the
        # panel as the second source simply having no data. Identity is safe as
        # a key here because self._providers holds every provider for the life
        # of the bus, so no id can be recycled underneath us.
        self._broken = {}
        # (label, n) for the frame the last poll() chose. Recorded there
        # rather than recomputed here, so session_pair() answers for the same
        # poll the board's usage message came from -- polling the providers a
        # second time could pick a different winner and name a project that
        # does not belong to the state on the panel.
        self._session_pair = ("", 0)
        # The plan message for the pair the last poll() chose, or None while
        # this bus has never seen a plan -- see plan_msg().
        self._plan = None
        # Guards the one-shot anchor seeding below: see
        # _seed_anchor_once. False until the first poll_frames() call,
        # regardless of whether that call finds anything to seed.
        self._seeded = False

    def provider_ids(self):
        """Which providers this bus is polling, by id.

        A public accessor so a caller that needs to know -- the fleet tests
        checking that BLINK_SCENARIO really did replace the real sources --
        does not have to reach into _providers and freeze that name in place.
        """
        return [p.get_provider_id() for p in self._providers]

    def add_provider(self, provider):
        """Onboard a provider at runtime. Nothing else has to change."""
        self._providers.append(provider)

    def set_preferred(self, provider):
        """Which provider gets the outer ring and the big number.

        Set from the BOARD, not from here: the user picks it on the settings
        screen, the board persists it and announces it, and this follows. A
        preference that lived only in the daemon would reset every time the
        daemon restarted, and the person choosing it is looking at the panel,
        not at a config file.

        An unknown name is ignored rather than applied. select_pair() falls
        back to the freshest provider when its preference matches nothing, so
        a typo would silently hand the outer ring to whichever source wrote
        last -- which looks like a bug in the merge, not a bad setting.
        """
        if not provider:
            return False
        known = {p.get_provider_id() for p in self._providers}
        if provider not in known:
            # Not named: an unknown name is by definition one this daemon
            # did not produce, so it is a raw value off the serial buffer
            # (the board's `pref`), and CLAUDE.md keeps those out of every
            # log line. This printed it with !r until 2026-10-30. What we
            # keep is ours, so that one is still said.
            print(f"[ingest] the board asked for a provider that is not"
                  f" reporting; keeping {self._preferred!r}",
                  file=sys.stderr)
            return False
        self._preferred = provider
        return True

    def _seed_anchor_once(self):
        """One-shot fallback: seed the weekly anchor from a legacy Cowork
        audit file, or failing that from Claude Desktop's IndexedDB, when
        nothing has ever populated it.

        Runs at most once per process, guarded by self._seeded -- set before
        any work is attempted, so a raise on the first try still counts as
        the one try this process gets. Without that, a machine with no
        audit file (the common case: see pc/cowork_audit's own docstring)
        would walk its whole session tree on every single poll, forever.

        Tried only while the anchor file itself is empty, and wrapped whole
        in try/except: this walks a directory tree and parses files owned
        by another application, and none of that may be allowed to take a
        poll down.

        The order is deliberate and is a cost order. The audit file is plain
        JSON in a directory holding no chat store; the IndexedDB store holds
        the customer's conversations, so it is tried last, only when the
        cheaper source has produced nothing, and only within the same single
        attempt this process gets.

        Called at the END of poll_frames, after every provider has been polled
        and after weekly_anchor.observe has seen their frames. The cheapest
        and most authoritative source of a weekly boundary is a live frame
        that already carries one -- ordinarily the Claude Code status line --
        and it costs this function nothing to let that land first: the guard
        above then finds an anchor and neither seeder runs at all. Running
        before the loop, as this used to, spent an os.walk of the session tree
        and a V8 decode of the conversation store on the first poll of every
        process to learn something the next few lines were about to be told.
        """
        if self._seeded:
            return
        self._seeded = True
        try:
            path = weekly_anchor.anchor_path()
            if weekly_anchor.load(path) is not None:
                return
            found = cowork_audit.seven_day_reset()
            if found is None:
                found = desktop_idb.seven_day_reset()
            if found is None:
                return
            resets_at, observed_at = found
            weekly_anchor.save(path, resets_at, observed_at)
        except Exception:
            pass

    def poll_frames(self):
        """Every frame every provider can produce right now.

        Each provider is polled inside its own try. A provider that raises is
        reported once and then sits out a cooldown: the alternative is a stack
        trace on the daemon's log thirty times a minute for as long as some
        other application's file stays malformed.

        Skipped means skipped. This used to add the provider to _broken and
        then go on polling it every cycle regardless -- only the log line was
        suppressed, so a second, different failure was invisible and the
        docstring above was describing a policy the loop did not have.

        But skipped no longer means retired. A cooldown, not a tombstone: see
        FIRST_RETRY_AFTER_S for why a two-second poll makes the difference
        between the two enormous.

        Reported ONCE all the same, and that is deliberate. The log line marks
        the transition from working to broken, not each failure -- a provider
        that keeps failing keeps quiet, and only says anything again when it
        recovers, or when it breaks again after having recovered. A daemon
        that logs the same traceback on a timer is its own defect, and it is
        the reason the retry could not simply be "try it every poll".
        """
        # One Desktop data folder for the whole poll. Four readers here ask
        # for it, every 2 s on the serial thread; held, they resolve it once
        # and cannot split across two folders mid-poll. See pc/desktop_paths.
        with desktop_paths.one_answer():
            return self._collect_frames()

    def _spend_seat(self, now):
        """Whether Claude Code's config names a seat billed by usage, from
        the bus's one reader; False on a scenario's bus, which has none.
        Contained like the rest of the spend path: a failure is said in
        fixed words, once (claude_cli.spend_failed), and is "no"."""
        if self._plan_reader is None:
            return False
        try:
            return bool(self._plan_reader.look(now).spend_seat)
        except Exception:
            claude_cli.spend_failed()
            return False

    @staticmethod
    def _off_the_spend_page(f):
        """A claude frame a spend page does not show
        (docs/plans/enterprise-spend.md, 1): any reading of windows -- the
        status line's, Claude Desktop's, its Local Storage's, a remembered
        payload -- and the weekly anchor's projection, which carries a
        weekly reset and no figure. Frames that speak only for a terminal
        (the hook's state and counts) stay."""
        if f.provider != claude_cli.PROVIDER_ID:
            return False
        if f.src == WEEKLY_ANCHOR_SRC_ID:
            return True
        return (claude_account._speaks_for_account(f)
                and f.plan != base.PLAN_SPEND)

    def _collect_frames(self):
        """poll_frames()'s body, run under one Desktop folder answer."""
        frames = []
        now = self._now()
        for p in self._providers:
            key = id(p)
            name = p.__class__.__name__
            sitting_out = self._broken.get(key)
            if (sitting_out is not None
                    and not self._cooldown_over(sitting_out, now)):
                continue
            try:
                frames.extend(p.poll(now) or [])
            except Exception as e:
                # Doubled from whatever the last wait was, so a provider whose
                # upstream genuinely changed shape backs off to the ceiling on
                # its own without anyone deciding it is hopeless.
                wait = (min(sitting_out["wait"] * 2, MAX_RETRY_AFTER_S)
                        if sitting_out is not None else FIRST_RETRY_AFTER_S)
                if sitting_out is None:
                    # By type, never the message: it can carry what the
                    # provider was reading ("int too large to convert to
                    # float" off a damaged status line, a path, a character
                    # of Claude Code's config), into the file customers send
                    # us (CLAUDE.md).
                    print(f"[ingest] {name} failed and will be skipped"
                          f" ({type(e).__name__})", file=sys.stderr)
                self._broken[key] = {"since": now, "retry_at": now + wait,
                                     "wait": wait}
            else:
                if sitting_out is not None:
                    # Worth a line precisely because the failure only got one:
                    # without this, a provider that came back left no trace of
                    # having done so, and the log said only that it had died.
                    del self._broken[key]
                    print(f"[ingest] {name} is working again",
                          file=sys.stderr)
        # On a spend seat the config is authoritative for the Claude page,
        # and windows readings are not shown on it. Dropped here, before the
        # weekly anchor learns from them and before the verdict marks them:
        # a seat billed by usage has no seven-day reset to remember and no
        # five-hour window to lack, and a Max status line still in the file
        # from before /login, or a Claude Desktop sample a minute newer than
        # the spend frame, would otherwise take the page back to windows
        # (claude_account._same_plan_as_newest keeps the newest plan).
        if self._spend_seat(now):
            frames = [f for f in frames if not self._off_the_spend_page(f)]
        try:
            # Bonus learning, never a poll cost: whatever just carried a real
            # weekly reset becomes tomorrow's WeeklyAnchorProvider projection.
            # Anything this raises -- a bad path, a permissions error, a race
            # on the file -- must never take today's poll down with it.
            #
            # Filtered to the claude provider ONLY. weekly_anchor.observe is
            # deliberately source-agnostic about WHICH store a reset came
            # from -- the status line, a legacy Cowork audit, the IndexedDB
            # seeder -- but it is never agnostic about WHOSE account it
            # describes. Codex has its own weekly_resets_at (codex_cli.py),
            # and handing that to observe() unfiltered would let a Codex
            # boundary become the anchor that WeeklyAnchorProvider then
            # republishes with provider="claude" -- a different account's
            # reset, presented as this one's.
            claude_frames = [f for f in frames if f.provider == "claude"]
            weekly_anchor.observe(
                claude_frames, weekly_anchor.anchor_path(), now)
        except Exception:
            pass

        # LAST, and that ordering is the whole point. observe() has just had
        # its chance at every live frame, so on any machine running Claude
        # Code the anchor file is already written and _seed_anchor_once's own
        # `load(path) is not None` guard turns this into a no-op that costs a
        # single small read. Called before the loop -- as it was -- it walked
        # the session tree and V8-decoded the customer's conversation store on
        # the first poll of every process, to learn a boundary the status line
        # was about to hand over for free.
        self._seed_anchor_once()
        # Not on a spend seat: the frames left there are the spend page and
        # frames with no figure (a status line without rate_limits, the
        # hook's), and a verdict that an earlier Ultra login saved would mark
        # those, turning the token burn view on over a page of dollars.
        # pc/normalizer.merge refuses the mark beside a spend figure as well;
        # this keeps it off the frames in the first place.
        if self._verdict is not None and not self._spend_seat(now):
            # Before any merge, so every claude frame -- the status line's,
            # or Claude Desktop's alone on a desk that runs no Claude Code --
            # carries the account's verdict into it. Contained like a
            # provider: the verdict is a bonus on the poll, never its cost.
            try:
                self._verdict.mark(frames, now)
            except Exception:
                pass
        return frames

    def verdict(self):
        """The bus's SessionVerdict, or None: for `blink status`, which says
        what the verdict rests on."""
        return self._verdict

    @staticmethod
    def _cooldown_over(sitting_out, now) -> bool:
        """Has a skipped provider's cooldown elapsed?

        The second arm is for a clock that went backwards. self._now is wall
        time -- the whole bus reasons in it, because every frame's age is a
        wall-clock reading from another application's file -- and a sleeping
        laptop, an NTP step or a timezone-clumsy VM can put `now` before the
        moment the provider was quarantined. Left to arithmetic alone that
        would hold a healthy provider out for however far the clock moved,
        which is exactly the permanent skip this cooldown exists to end.
        """
        return now >= sitting_out["retry_at"] or now < sitting_out["since"]

    def _login(self, now):
        """(kind, since, touched_at) from the login watch, or blanks.

        Blanks when there is no watch and when it raises: the profile is
        another application's file, and failing to read it has to leave the
        page as it would have been without the watch, not cost the poll.
        """
        watch = self._account
        if watch is None:
            return "", None, None
        try:
            watch.poll(now)
            return watch.kind, watch.since, watch.touched_at
        except Exception:
            return "", None, None

    def _guard_account(self, frames, now):
        """Filter this poll's frames for the signed-in login, once armed.

        Not armed, the frames come back as they went in and the profile is
        never opened -- see _plans_seen.
        """
        if not self._plans_seen:
            self._plans_seen = any(
                f is not None and f.plan != base.PLAN_WINDOWS for f in frames)
            if not self._plans_seen:
                return frames
        kind, since, touched = self._login(now)
        return claude_account.select(frames, now, kind, since, touched)

    def poll(self):
        """The usage message for the board, or None when nothing is known."""
        frames = self.poll_frames()
        # Read after the providers have run, as it always was: a slow poll
        # must not date the usage line by the moment it started.
        now = self._now()
        frames = self._guard_account(frames, now)
        primary, secondary = normalizer.select_pair(
            frames, preferred=self._preferred)
        # The claude page wherever it sits: the main one, or the second when
        # the owner made another provider main. A provider left off the
        # board's two pages has no page to draw a burn view on.
        self._pages = {f.provider: f for f in (primary, secondary)
                       if f is not None}
        self.prune_burn()
        if self._plans_seen:
            # Computed even when primary is None below. The moment that
            # matters is /login back to Max: the guard has just thrown out
            # every spend reading as pre-switch, Claude Code has not yet
            # written a Max status line, and so there is nothing to draw --
            # but the board is still captioning its Claude page SPEND. The
            # "windows" this produces is what takes that caption down.
            self._plan = protocol.plan_for(primary, secondary)
        if primary is None:
            return None
        msg = protocol.frame_to_usage(primary, now, secondary)
        # Against the state the message actually carries, which is not always
        # the primary frame's own -- see _pair_from.
        self._session_pair = _pair_from(
            primary, secondary, msg.get("state", base.STATE_UNKNOWN))
        return msg

    def claude_page(self):
        """The merged claude frame the last poll() put on a page, or None.

        For `blink status --wire`, whose "Session" row says whether the
        account is being treated as having no five-hour window. It used to
        build a provider of its own and ask that, which disagreed with the
        line printed under it: a Local Storage window lifts the rule in the
        merge, and a fresh provider never saw it (review, 2026-10-31). This
        is the frame the usage line was made from.
        """
        return self.page(claude_cli.PROVIDER_ID)

    def page(self, provider):
        """The merged frame the last poll() put on `provider`'s page, or
        None: claude_page() for any provider. `blink status --wire` prints
        the Codex Session row from page("codex"), so the row is about the
        page the wire line carries, and says nothing of a Codex reading
        that select_pair left off the board."""
        return self._pages.get(provider)

    def burn_pages(self):
        """The providers whose page the last poll() found without a session
        window and that have a reader: the pages a burn line is for. Asks
        no reader; Bridge decides from this which to ask, and when.

        In turn order: the page asked first moves on by one with each
        burn_pass(), so each page has the whole budget every other pass
        whatever the other is owed."""
        pages = [p for p, f in self._pages.items()
                 if f.no_session_window and p in self._burn]
        if not pages:
            return pages
        k = self._burn_turn % len(pages)
        return pages[k:] + pages[:k]

    def burn_pass(self):
        """Start a pass: the read budget burn() hands the readers is full
        again, shared by every page asked until the next call, and the
        page asked first moves on by one.

        Asked by Bridge once a pass of the serial loop, before the pass's
        first ask, however many times in it the heartbeat and the fast tick
        go on to ask. Nothing else fills the budget: a caller that never
        starts a pass has the first budget and what is left of it."""
        self._burn_budget = None
        self._burn_turn += 1

    def prune_burn(self):
        """Have every reader forget what has left its window by now,
        reading nothing.

        README "Privacy" says a turn's token counts are held in memory for
        up to five hours. A reader forgets inside its own poll(), and burn()
        polls only a marked page, while the daemon polls the bus only while
        a board answers: so a page unmarked (a window back, an Ultra mark
        lifted) or a board unplugged left every turn read so far in memory
        until the daemon stopped -- ten Codex turns 8.4 h after the newest,
        through this bus (4th review, 2026-11-01). Asked at the end of every
        poll() and by the daemon's upkeep on every pass of its loop, cable
        or no cable; a quarter-hour's arithmetic per reader when nothing
        has left the window. A scenario's replay holds nothing and has no
        prune()."""
        now = self._now()
        for reader in self._burn.values():
            prune = getattr(reader, "prune", None)
            if prune is not None:
                prune(now)

    def plan_msg(self):
        """What the last poll() says the pages are a share of.

        Stays None for as long as every frame this bus has seen was a windows
        frame. That None is how the feature keeps off the wire on today's
        desks: the Bridge has nothing to send. Once a spend reading has been
        seen it is always a message, and "windows" is one of them -- the
        board learns that a spend page has gone only by being told.
        """
        return self._plan

    def session_pair(self):
        """(label, n) for the state the last poll() put on the panel.

        Deliberately NOT part of the usage message: that line was measured at
        506 of protocol.MAX_LINE_BYTES=512 fully loaded and proto.c drops an
        over-long line whole, so the project name travels as its own message
        (protocol.session) and this is where the daemon reads it.
        """
        return self._session_pair

    def burn(self, provider=claude_cli.PROVIDER_ID):
        """`provider`'s burn line for the board, or None when there is none
        to send.

        None unless the last poll() found that page without a session
        window, so on a Pro or Max desk the transcripts are never read at
        all, and None while the reader has nothing honest to say yet (no
        transcripts directory, or a cold start still being read). The reader
        is asked only from here, and this only from Bridge, after the
        board's caps have been checked: a board that cannot draw the view
        costs nothing.

        A reader that raises is not caught here. Bridge._send_burn_if_due,
        the only caller, contains it -- it costs the burn line, never the
        poll -- and logs it once, by type. It was caught here too, with a
        log-once flag of its own, so one failure had two handlers (review,
        2026-10-31).
        """
        page = self._pages.get(provider)
        reader = self._burn.get(provider)
        if reader is None or page is None or not page.no_session_window:
            return None
        if isinstance(reader, burn_reader.BurnReader):
            # One budget for the pass, not one per reader: two pages each
            # given READ_BUDGET_BYTES read twice what the serial loop was
            # promised (burn_reader.READ_BUDGET_BYTES; /code-review max,
            # 2026-11-01). The page asked first can spend all of it; the
            # other is then behind, says None, and is asked first on the
            # next pass (burn_pages). Asked with nothing left it still
            # answers, reading nothing: a page caught up says its line. A
            # scenario's replay reads nothing and is asked as before.
            left = (reader.read_budget if self._burn_budget is None
                    else min(reader.read_budget, self._burn_budget))
            got = reader.poll(self._now(), budget=left)
            self._burn_budget = max(0, left - reader.last.read_bytes)
        else:
            got = reader.poll(self._now())
        if got is None:
            return None
        q, tokens = got
        return protocol.burn(provider, q, tokens)

    def spend_burn(self, provider=claude_cli.PROVIDER_ID):
        """The dollar burn line for the board, or None when there is none.

        None unless the last poll() put a spend page on `provider`'s page
        (its primary frame's plan is PLAN_SPEND) and the bus holds a count,
        so a Max desk never builds one. A scenario's bus holds the
        scenario's stand-in (scripted.ScriptedSpendCount), whose buckets are
        None until a step names some: that is no line too. Pure arithmetic
        on the in-memory count: no file is read here.

        And None unless the page is the one the count feeds: the config's
        (base.SPEND_SRC_ID). A gateway's page (the status line's own
        spend_limit, pc/statusline_source) is a spend page too, but nothing
        feeds the count from it, so its buckets were twenty zeros, and new
        firmware gives the left half of the burn view to any fresh sburn,
        hiding the gateway's share behind an empty dollar line (final
        review of 2026-11-04). A scenario's stand-in is not held to this:
        its line is whatever its steps say, on whatever page they draw.
        """
        page = self._pages.get(provider)
        if (self._counter is None or page is None
                or getattr(page, "plan", base.PLAN_WINDOWS) != base.PLAN_SPEND):
            return None
        if (isinstance(self._counter, spend_count.SpendCounter)
                and page.src != base.SPEND_SRC_ID):
            return None
        now = self._now()
        cents = self._counter.buckets(now)
        if cents is None:
            return None
        return protocol.spend_burn(provider, protocol.burn_quarter(now),
                                   cents)

    def fetch(self):
        """The callable to hand Bridge(fetch_usage=...).

        The ONE way to obtain a fetch, and that is the whole point of it
        existing. The daemon used to write `fetch = bus.poll` while the tests
        built theirs through make_fetch, and a bound method proxies attribute
        reads to the plain function underneath -- so the Bridge's
        getattr(fetch, "session_pair", None) came back None on every real
        desk and the board was never named, with the entire suite green.
        Two ways to build the same object is what allowed that, so there is
        now one.
        """
        return _BusFetch(self)


class _BusFetch:
    """A zero-arg callable that also knows the project name, and the burn
    line (IngestionBus.burn) for the same poll.

    An object rather than a function with an attribute bolted on: the Bridge
    is handed a zero-arg callable returning a finished usage dict and never
    sees the frame the name lives on. Widening that contract instead (a
    tuple, or the label smuggled inside the usage dict) would either break
    every injected fake or put the label one slip away from the byte-capped
    usage line -- which is the one thing this whole message type exists to
    avoid.
    """

    def __init__(self, bus):
        self._bus = bus

    def __call__(self):
        return self._bus.poll()

    def session_pair(self):
        return self._bus.session_pair()

    def burn_pages(self):
        return self._bus.burn_pages()

    def burn_pass(self):
        return self._bus.burn_pass()

    def burn(self, provider=claude_cli.PROVIDER_ID):
        return self._bus.burn(provider)

    def spend_burn(self, provider=claude_cli.PROVIDER_ID):
        return self._bus.spend_burn(provider)

    def plan_msg(self):
        return self._bus.plan_msg()


# Which count describes which state. STATE_FAILED reads n_stuck because that
# is where the provider put it: claude_state.poll folds a failed session into
# the stuck count, so mapping it to nothing would leave two failed sessions
# reporting zero -- "Session failed" where the panel should say "Session
# failed - 2 sessions".
_COUNT_FOR_STATE = {
    base.STATE_RUNNING: "n_run",
    base.STATE_WAITING: "n_wait",
    base.STATE_STUCK: "n_stuck",
    base.STATE_FAILED: "n_stuck",
    base.STATE_IDLE: "n_idle",
}


def _pair_from(primary, secondary, wire_state):
    """(label, n) for the state the usage message actually carries.

    Against the WIRE state rather than the primary frame's own, because
    frame_to_usage sends worst_of(primary, secondary) and the summed counts:
    one light for the whole desk. A Claude session sitting idle-but-named
    beside a waiting Codex one would otherwise put "Waiting for you -
    MyProject" over a project that is not waiting -- a wrong statement, not
    a vague one, and a line nobody can trust is worse than no line.

    So the label survives only when the frame carrying it is in the state on
    the panel, and only when nothing else shares that state. Naming one of
    several is refused rather than guessed, which is the same rule
    claude_state.poll applies when it decides whether to set `label` at all.
    """
    frames = [f for f in (primary, secondary) if f is not None]
    field = _COUNT_FOR_STATE.get(wire_state)
    n = sum(getattr(f, field, 0) for f in frames) if field else 0
    named = [f for f in frames
             if f.state == wire_state and getattr(f, "label", "")]
    label = named[0].label if len(named) == 1 and n <= 1 else ""
    return (label, int(n))
