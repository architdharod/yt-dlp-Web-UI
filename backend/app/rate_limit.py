"""Per-source rate-limit lanes, and the detection that feeds them.

Large YouTube playlists at five concurrent downloads reliably earn an HTTP 429,
and yt-dlp has nothing to say about it: its YouTube extractor deliberately does
*not* retry a 403 or a 429 (``_extract_response`` re-raises those two statuses
instead of handing them to its ``RetryManager``), the fragment downloader that
does retry a 429 sleeps zero seconds between attempts unless
``retry_sleep_functions`` says otherwise, and ``Retry-After`` is ignored
throughout.  So every child of a big playlist used to fail within a second of
the first one, each with yt-dlp's own wording and a manual Retry button.

This module is the other half of the answer (the first half is prevention: one
extraction per child, and ``sleep_interval_requests`` pacing every session --
see :func:`app.downloader.base_opts`).  It holds:

* :func:`rate_limit_status`, :func:`retry_after_seconds` and
  :func:`is_bot_check` -- structural detection, no string matching for the 429
  itself.  yt-dlp wraps an extractor failure as
  ``DownloadError(msg, exc_info)`` whose ``exc_info[1]`` is the
  ``ExtractorError`` whose ``.cause`` is
  ``yt_dlp.networking.exceptions.HTTPError``, and *that* carries ``.status``
  and ``.response.headers``.  Our own ``DownloadError`` chains onto it with
  ``raise ... from``, so one walk over ``__cause__`` / ``__context__`` /
  ``.cause`` / ``.exc_info`` finds it from anywhere in the pipeline.

* :class:`LaneManager` -- one *lane* per source host, each either **open** or
  **held until T**, with a consecutive-429 count that drives the backoff.  The
  hold is lane-global rather than per job: a 429 is a statement about this
  server's relationship with that host, so making every other job of that host
  wait is the only thing that actually lets the pressure off.

The lane state machine is deliberately synchronous and clock-injected -- every
rule in it is a pure function of (state, now) -- and the asyncio waiting lives
in :class:`app.queue_manager.QueueManager`, which is the only thing that owns
jobs.
"""

import asyncio
import email.utils
import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Iterator, Literal
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# The hosts that get a lane.  Everything else is "other" and is never held:
# these three are the sources this app knows how to enumerate, and a lane is
# only worth having where a bulk download can put dozens of jobs on one host at
# once.
LaneHost = Literal["youtube", "soundcloud", "bandcamp"]
LANE_HOSTS: tuple[LaneHost, ...] = ("youtube", "soundcloud", "bandcamp")

# What the user calls each host in a message.  Capitalised as the sites spell
# themselves; "YouTube rate limit" reads as a sentence, "youtube rate limit"
# reads as a log line.
HOST_LABELS: dict[str, str] = {
    "youtube": "YouTube",
    "soundcloud": "SoundCloud",
    "bandcamp": "Bandcamp",
}

# Why a lane is held.  Two very different situations: a 429 clears on its own
# and is worth waiting out, a bot check does not and is worth telling the user
# about.
REASON_RATE_LIMIT = "rate_limit"
REASON_BOT_CHECK = "bot_check"

# The waits, in order, for the 1st..5th consecutive 429 on a lane.  Doubling
# from half a minute: short enough that one unlucky job recovers inside a
# coffee break, long enough by the fifth that we are no longer part of the
# problem.  The last entry repeats for any further 429 -- the ceiling below is
# what ends a lane that never recovers, not a growing exponent.
BACKOFF_SECONDS: tuple[int, ...] = (30, 60, 120, 240, 480)

# Fraction of the wait that is randomised, either way.  Without it every job
# that was 429ed in the same second would come back in the same second, which
# is how a thundering herd is built.
BACKOFF_JITTER = 0.20

# How many automatic attempts one job gets before it ends `error`.  Env, so a
# homelab on a shared IP can raise it.  Manual Retry always starts a fresh
# budget: the user is asking for one more, and they can see the banner.
RATE_LIMIT_ATTEMPTS_ENV = "RATE_LIMIT_ATTEMPTS"
DEFAULT_RATE_LIMIT_ATTEMPTS = 5

# How long a lane may stay held before we stop pretending it will recover and
# fail everything queued on it.  An hour is roughly the length of YouTube's own
# soft rate limit ("This content isn't available, try again later ... for up to
# an hour"), and a queue that has been stuck longer than that is better off
# empty and honest than full and silent.
CEILING_SECONDS = 3600

# When the banner stops saying "waiting" and starts pointing at the README.
#
# The design note asked for an hour here.  An hour is unreachable: the ceiling
# above fires at exactly that point and resets the lane, so the escalated text
# would never be rendered.  Half an hour is the same idea at a time the user
# can actually see it -- by then the automatic attempts of the first jobs are
# spent and the answer is no longer "wait".
NOTICE_ESCALATE_SECONDS = 1800

# How often the watchdog looks at a held lane, and the longest a waiting job
# sleeps before re-checking the ceiling.  The ceiling is an hour; noticing it
# fifteen seconds late costs nothing and keeps this off the event loop.
WATCHDOG_INTERVAL_SECONDS = 15.0
MAX_WAIT_SLICE_SECONDS = 30.0

# The README section every "you have been walled" message points at.  One
# spelling, because it is a heading in that file and a typo here is a dead
# reference.
README_SIGN_IN_SECTION = '"YouTube asks you to sign in" in the README'

# YouTube's bot wall, as the user sees it.  yt-dlp does not own this string --
# it is ``playabilityStatus.reason`` verbatim from YouTube, surfaced through
# ``raise_no_formats`` (yt_dlp/extractor/youtube/_video.py, the ``'sign in' in
# reason.lower()`` branch), which strips the trailing "This helps protect our
# community. Learn more" and appends its own login hint.  So the stable part is
# the opening clause, and the apostrophe is whichever of ' and U+2019 YouTube
# served that day.
_BOT_CHECK_RE = re.compile(r"sign\s+in\s+to\s+confirm\s+you.{0,3}re\s+not\s+a\s+bot", re.IGNORECASE)

# YouTube's *soft* rate limit: no 429, no status code at all, just a player
# response that says the video is unavailable.  yt-dlp recognises it and says
# so in the same breath ("has been rate-limited by YouTube for up to an hour.
# It is recommended to use `-t sleep`"), so treating it as a 429 is not a
# guess -- it is the one case where the wire carries 200 and the meaning is
# 429.  Matched on YouTube's clause, not on yt-dlp's advice, which is the half
# more likely to be reworded.
_SOFT_RATE_LIMIT_RE = re.compile(
    r"this content isn.{0,3}t available,?\s*try again later", re.IGNORECASE
)

# yt-dlp's own rendering of a 429, for the paths where only a string survives:
# ``ignoreerrors`` (which the probe needs, so one dead entry does not lose 200
# good rows) logs the failure and returns None rather than raising, so there is
# no chain left to walk.  ``HTTPError.__init__`` builds this message, so the
# wording is yt-dlp's and not a guess.
# Two spellings, because two libraries write it: yt-dlp's ``HTTPError`` says
# "HTTP Error 429: Too Many Requests", while ytmusicapi's ``YTMusicServerError``
# says "Server returned HTTP 429: Too Many Requests".
_HTTP_429_RE = re.compile(r"HTTP\s+(?:Error\s+)?429\b", re.IGNORECASE)

# The status we act on.  403 is deliberately absent: YouTube returns it for
# geo-blocks, age walls and expired URLs far more often than for pressure, and
# holding a whole lane for an hour over one region-locked track would be worse
# than the failure it replaces.
RATE_LIMITED_STATUS = 429


class _Unset:
    """"Nothing has been announced yet", told apart from "probing, no instant"."""


_UNSET = _Unset()


def rate_limit_attempts() -> int:
    """How many automatic attempts a rate-limited job gets, from the env.

    docker compose substitutes an unset variable with an empty string, so ``""``
    counts as unset.  A value below 1 is raised to 1: zero attempts would mean
    a job that fails without ever having tried, which no configuration should
    be able to ask for.
    """
    raw = os.environ.get(RATE_LIMIT_ATTEMPTS_ENV)
    if not raw:
        return DEFAULT_RATE_LIMIT_ATTEMPTS
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(
            "%s=%r is not a number; using %d",
            RATE_LIMIT_ATTEMPTS_ENV,
            raw,
            DEFAULT_RATE_LIMIT_ATTEMPTS,
        )
        return DEFAULT_RATE_LIMIT_ATTEMPTS


# ---------------------------------------------------------------------------
# Host classification
# ---------------------------------------------------------------------------


def source_for_host(url: str) -> str:
    """Which source a URL's *host* belongs to: a lane host, or ``"other"``.

    The host half of :func:`app.probe._source_of`, lifted here so that both the
    probe (which has an info dict and can also look at the extractor) and the
    queue (which has only a URL) classify a URL the same way.  Substring
    matching on the hostname is what covers ``music.youtube.com`` and every
    ``<artist>.bandcamp.com`` without a list of subdomains.
    """
    host = (urlsplit(url).hostname or "").casefold()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "soundcloud.com" in host:
        return "soundcloud"
    if "bandcamp.com" in host:
        return "bandcamp"
    return "other"


def lane_for_url(url: str) -> LaneHost | None:
    """The lane a URL belongs to, or ``None`` when it gets no lane."""
    source = source_for_host(url)
    return source if source in LANE_HOSTS else None  # type: ignore[return-value]


def host_label(host: str) -> str:
    """The user-facing name of a lane host."""
    return HOST_LABELS.get(host, host)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _chain(exc: BaseException | None) -> Iterator[BaseException]:
    """Every exception reachable from *exc*, innermost causes included.

    Four links, because a 429 travels through four different conventions before
    it reaches us: ``__cause__`` (our ``raise ... from``), ``__context__`` (an
    ``except`` block that raised something else), ``.cause`` (yt-dlp's
    ``ExtractorError``) and ``.exc_info`` (yt-dlp's ``DownloadError``, which
    keeps ``sys.exc_info()`` rather than chaining).  Cycles are possible --
    ``__context__`` can point back -- so visited ids are tracked.
    """
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current

        stack.append(current.__cause__)
        stack.append(current.__context__)

        cause = getattr(current, "cause", None)
        if isinstance(cause, BaseException):
            stack.append(cause)

        info = getattr(current, "exc_info", None)
        if isinstance(info, tuple) and len(info) == 3 and isinstance(info[1], BaseException):
            stack.append(info[1])


def _http_errors(exc: BaseException | None) -> Iterator[BaseException]:
    """The links of *exc*'s chain that look like an HTTP status carrier.

    Duck-typed rather than ``isinstance(..., yt_dlp.networking.exceptions
    .HTTPError)``: the attributes are what we use, a constructed stand-in in a
    unit test has exactly those, and importing a private-ish yt-dlp module for
    a type check buys nothing.
    """
    for link in _chain(exc):
        status = getattr(link, "status", None)
        if isinstance(status, int) and hasattr(link, "response"):
            yield link


def rate_limit_status(exc: BaseException | None) -> int | None:
    """``429`` if *exc* was caused by a rate limit, else ``None``.

    Structural first: any link in the chain that carries ``status == 429``.
    Failing that, YouTube's 200-OK soft rate limit, which has no status to
    read and is recognised by the clause YouTube puts in the player response
    (see :data:`_SOFT_RATE_LIMIT_RE`).

    Returns the status rather than a bool so a caller can log what it saw and
    so a future second status needs no new function.
    """
    for link in _http_errors(exc):
        if getattr(link, "status", None) == RATE_LIMITED_STATUS:
            return RATE_LIMITED_STATUS
    for link in _chain(exc):
        if rate_limit_in_message(str(link)):
            return RATE_LIMITED_STATUS
    return None


def rate_limit_in_message(message: str) -> bool:
    """Whether a bare yt-dlp *message* describes a rate limit.

    The fallback for callers that have a string and no exception -- the probe's
    ``ignoreerrors`` path -- and the second line of defence for
    :func:`rate_limit_status` if yt-dlp ever stops chaining the cause.
    """
    return bool(_HTTP_429_RE.search(message) or _SOFT_RATE_LIMIT_RE.search(message))


def retry_after_seconds(exc: BaseException | None) -> float | None:
    """The ``Retry-After`` the rate limiter asked for, in seconds, if any.

    yt-dlp ignores this header entirely, so reading it is ours to do.  Both
    forms in RFC 9110 are accepted: a delta in seconds, and an HTTP date, which
    is converted against the local clock.  A date in the past, a negative
    delta or anything unparseable yields ``None`` -- the schedule is then the
    only input, which is the safe direction.
    """
    for link in _http_errors(exc):
        response = getattr(link, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            continue
        try:
            raw = headers.get("Retry-After")
        except Exception:  # pragma: no cover - a headers object that is not one
            continue
        if not raw:
            continue
        raw = str(raw).strip()
        try:
            seconds = float(raw)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                continue
            if when is None:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        if seconds > 0:
            return seconds
    return None


def is_bot_check(exc: BaseException | None) -> bool:
    """Whether *exc* is YouTube's "prove you are not a bot" wall.

    Matched on the message, narrowly, because there is nothing structural to
    match: YouTube answers 200 with a player response that carries no formats,
    and yt-dlp turns that into a plain ``ExtractorError``.  The pattern is the
    opening clause only, so yt-dlp's appended login hint and YouTube's choice
    of apostrophe do not matter -- and "Sign in to confirm your age", which is
    a different problem with a different answer, does not match it.
    """
    return any(_BOT_CHECK_RE.search(str(link)) for link in _chain(exc))


# ---------------------------------------------------------------------------
# Lane state
# ---------------------------------------------------------------------------


@dataclass
class LaneRecord:
    """One lane's persisted state.

    Written to ``queue.db`` so a restart does not walk straight back into the
    limiter it was waiting out: a container that restarts twice in five minutes
    would otherwise send the same burst twice.

    ``held_since`` is separate from ``hold_until`` because the ceiling is about
    how long the trouble has lasted, not about how long the current wait is:
    five extensions of two minutes each are an hour of being rate limited, and
    the ceiling has to see that.
    """

    host: str
    hold_until: datetime | None = None
    consecutive: int = 0
    reason: str | None = None
    held_since: datetime | None = None


class _Lane:
    """The live state of one lane: the record, plus who is waiting on it.

    Every mutator is synchronous and takes *now*, so the whole state machine is
    testable against a fake clock with no event loop at all.  The only asyncio
    in here is :attr:`_wake`, the broadcast every waiter parks on.

    The three states, in the vocabulary the rest of the app uses:

    * **open** -- ``hold_until is None and canary is None``.  Anyone may run.
    * **held** -- ``hold_until`` is in the future.  Nobody may run.
    * **probing** -- the hold elapsed and one job (the *canary*, the oldest
      parked job) is allowed through to find out whether the limiter has let
      go.  Everyone else waits for the canary's answer, so a lane that is still
      limited spends exactly one attempt finding that out instead of N.

      With *nothing* parked there is no canary to elect, and a probing lane
      then lets arrivals straight through -- bounded, as always, by
      ``LANE_CONCURRENCY`` and ``MAX_CONCURRENT_DOWNLOADS``, so this is two
      YouTube jobs, not forty.  It is also short-lived: the watchdog settles
      every held lane and turns exactly that state into an
      :meth:`idle_lapse` within one tick.
    """

    def __init__(self, record: LaneRecord) -> None:
        self.host = record.host
        self.hold_until = record.hold_until
        self.consecutive = record.consecutive
        self.reason = record.reason
        self.held_since = record.held_since
        # True once a hold has elapsed (or been resumed) and the lane has not
        # yet been proven open by a success.
        self.probing = False
        # Whether the banner has already been switched to its escalated
        # wording.  A latch, so crossing NOTICE_ESCALATE_SECONDS raises the
        # notice once instead of once per watchdog tick.
        self.escalated = False
        self.canary: str | None = None
        # Jobs currently *parked* on this lane -- not merely on it.  A job that
        # is downloading is on the lane and holds one of its slots, but it is
        # not waiting for anything, so it must never be elected canary (the
        # others would then be blocked until it finished) and the ceiling must
        # never fail it as though it were queued.  Entries go in when
        # :meth:`LaneManager.wait_turn` actually blocks and come out the moment
        # it stops blocking.
        self.parked: list[str] = []
        # When each job first joined this lane, so "the oldest waiter" survives
        # a job leaving the parked list to take its canary attempt and coming
        # back: without it the canary that was just rate limited would go to
        # the back of the queue and the next job would spend an attempt on the
        # same still-closed lane, which is the one thing the canary exists to
        # prevent.
        self._joined: dict[str, int] = {}
        self._joins = 0
        # Set by :meth:`_settle` when it moved the lane on, cleared by whoever
        # announces that.  The settle can happen in any waiter's poll, so the
        # flag is how the watchdog finds out that the banner is out of date
        # without re-raising the notice on every tick.
        self.dirty = False
        self._wake = asyncio.Event()

    # -- plumbing ---------------------------------------------------------

    def record(self) -> LaneRecord:
        return LaneRecord(
            host=self.host,
            hold_until=self.hold_until,
            consecutive=self.consecutive,
            reason=self.reason,
            held_since=self.held_since,
        )

    def wake_event(self) -> asyncio.Event:
        """The event a waiter should park on for the *next* lane change."""
        return self._wake

    def broadcast(self) -> None:
        """Release every waiter to re-evaluate, and arm the next round."""
        self._wake.set()
        self._wake = asyncio.Event()

    def park(self, job_id: str) -> None:
        """Record that *job_id* has started waiting on this lane."""
        if job_id not in self._joined:
            self._joins += 1
            self._joined[job_id] = self._joins
        if job_id not in self.parked:
            self.parked.append(job_id)

    def oldest_parked(self) -> str | None:
        """The parked job that has been on this lane longest."""
        if not self.parked:
            return None
        return min(self.parked, key=lambda job_id: self._joined.get(job_id, 0))

    def unpark(self, job_id: str) -> None:
        """Record that *job_id* has stopped waiting -- it is running now.

        The canary is deliberately *not* stood down here: it stops being parked
        precisely because it has been let through, and it has to keep the
        others parked until its attempt says whether the limiter let go.
        :meth:`dequeue` is the one that stands it down.
        """
        if job_id in self.parked:
            self.parked.remove(job_id)

    def dequeue(self, job_id: str) -> None:
        """Forget a job entirely, standing the canary down if it was the canary.

        A canary that leaves without a verdict -- cancelled, or failed for some
        reason that is not a rate limit -- must not leave the lane waiting for
        an answer that is never coming, so the next parked job is elected
        instead.
        """
        self.unpark(job_id)
        self._joined.pop(job_id, None)
        if self.canary == job_id:
            self.canary = None
            self.broadcast()

    # -- state ------------------------------------------------------------

    def _settle(self, now: datetime) -> bool:
        """Move an elapsed hold into probing, and elect a canary if needed.

        Returns whether anything visible changed.  It is called from every
        waiter's poll as well as from the watchdog, so a caller that wants to
        announce the change has to know it happened; :attr:`dirty` is the same
        answer for a caller that was not the one to trigger it.
        """
        changed = False
        if self.hold_until is not None and self.hold_until <= now:
            self.hold_until = None
            self.probing = True
            changed = True
        if self.probing and self.canary is None and self.parked:
            self.canary = self.oldest_parked()
            changed = True
        if changed:
            self.dirty = True
            # Everyone parked re-evaluates: the hold has gone, and one of them
            # is now allowed through.
            self.broadcast()
        return changed

    def idle_lapse(self) -> None:
        """A hold that ran out with nobody waiting on it.

        Nothing was learned: no request went out, so there is no evidence the
        limiter let go and ``consecutive`` stays where it is -- otherwise a
        lane that only ever gets probe traffic would restart its ladder at 30 s
        every time, which is the opposite of backing off.

        ``held_since`` *is* cleared, though, because the ceiling measures how
        long this has been going on for jobs that are waiting, and there are
        none.  Leaving it would let a lane nobody has touched for an hour fail
        the first job that arrives.
        """
        self.hold_until = None
        self.probing = False
        self.escalated = False
        self.held_since = None
        self.canary = None
        self.dirty = True
        self.broadcast()

    def may_run(self, job_id: str, now: datetime) -> bool:
        """Whether *job_id* may attempt its download right now."""
        self._settle(now)
        if self.hold_until is not None:
            return False
        return self.canary is None or self.canary == job_id

    def remaining(self, now: datetime) -> float:
        """Seconds until the hold elapses; 0 when it already has."""
        if self.hold_until is None:
            return 0.0
        return max(0.0, (self.hold_until - now).total_seconds())

    def held_for(self, now: datetime) -> float:
        """Seconds since this lane first went into trouble; 0 when it has not."""
        if self.held_since is None:
            return 0.0
        return max(0.0, (now - self.held_since).total_seconds())

    def ceiling_reached(self, now: datetime) -> bool:
        return self.held_since is not None and self.held_for(now) >= CEILING_SECONDS

    # -- transitions ------------------------------------------------------

    def note_rate_limit(
        self, now: datetime, retry_after: float | None = None
    ) -> float:
        """Record a 429 and hold the lane.  Returns the wait, in seconds.

        The consecutive count -- not any one job's attempt number -- picks the
        entry in :data:`BACKOFF_SECONDS`, so two jobs that are both 429ed
        before the first hold elapses lengthen the *lane's* next wait rather
        than each starting their own schedule from 30 s.

        A ``Retry-After`` wins when it is longer than the schedule: the limiter
        knows something we do not.  It never shortens the wait, because the
        schedule is also a self-imposed politeness and a small ``Retry-After``
        on the fifth consecutive 429 is not an invitation to try again in a
        second.

        An existing, longer hold is never shortened either: whichever of the
        two is further out wins.
        """
        self.consecutive += 1
        index = min(self.consecutive, len(BACKOFF_SECONDS)) - 1
        base = float(BACKOFF_SECONDS[index])
        wait = base * (1.0 + random.uniform(-BACKOFF_JITTER, BACKOFF_JITTER))
        if retry_after is not None:
            wait = max(wait, retry_after)

        until = now + timedelta(seconds=wait)
        if self.hold_until is not None and self.hold_until > until:
            until = self.hold_until
        self.hold_until = until
        self.reason = REASON_RATE_LIMIT
        if self.held_since is None:
            self.held_since = now
        self.probing = False
        self.canary = None
        self.broadcast()
        return max(0.0, (self.hold_until - now).total_seconds())

    def note_bot_check(self, now: datetime) -> None:
        """Record the sign-in wall and hold the lane until someone intervenes.

        No canary and no schedule: a bot check does not lapse on its own, and
        every automatic retry against it is one more strike.  The hold runs a
        full ceiling's length *from now*, which is what makes "the only two
        ways out are Resume now and the ceiling" true as data rather than as a
        special case in the waiting loop.

        ``held_since`` is deliberately left where it was, so the ceiling still
        measures from the original trouble.  The two can therefore disagree --
        a wall met after fifty minutes of 429s has a hold running an hour out
        and a ceiling ten minutes away -- and the ceiling wins: it fires first,
        fails what is parked, and ``fire_ceiling``'s ``_changed`` retracts the
        banner.  That is the intended order.  Setting ``hold_until`` from
        ``held_since`` instead would have produced a hold already in the past
        for a lane that had been in trouble for over an hour, i.e. a wall the
        next job would walk straight into.
        """
        if self.held_since is None:
            self.held_since = now
        self.reason = REASON_BOT_CHECK
        self.hold_until = now + timedelta(seconds=CEILING_SECONDS)
        self.probing = False
        self.canary = None
        self.broadcast()

    def note_success(self) -> None:
        """The limiter has let go: open the lane and forget the streak."""
        self.hold_until = None
        self.held_since = None
        self.consecutive = 0
        self.reason = None
        self.probing = False
        self.escalated = False
        self.canary = None
        self.broadcast()

    def resume(self) -> None:
        """Clear the hold and let one waiter through as a canary.

        The user's "I have fixed it, try again".  The streak is forgotten so
        the next 429 starts at 30 s again, but the lane goes to *probing*
        rather than straight to open: if nothing has actually changed, one job
        finds out rather than all of them.
        """
        self.hold_until = None
        self.held_since = None
        self.consecutive = 0
        self.reason = None
        self.escalated = False
        self.probing = bool(self.parked)
        self.canary = self.oldest_parked()
        self.broadcast()

    def reset(self) -> None:
        """Forget everything, releasing every waiter.  Used by the ceiling."""
        self.hold_until = None
        self.held_since = None
        self.consecutive = 0
        self.reason = None
        self.probing = False
        self.escalated = False
        self.canary = None
        self.broadcast()


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def wait_detail(
    host: str,
    seconds: float | None,
    attempt: int | None = None,
    total: int | None = None,
) -> str:
    """The line a waiting job shows in the queue.

    Three shapes, because a waiting job is in one of three situations and
    saying the wrong one is worse than saying less:

    * **held, and this job has been rate limited itself** -- "retry 2 of 5 in
      45 s".  The attempt number is the job's own budget, so it is only
      truthful for a job that has actually spent some of it.
    * **held, and this job has spent nothing** -- "waiting 45 s".  Every other
      job on the lane waits for a limit one of them ran into, and telling them
      they are on "retry 1 of 5" invents an attempt they never made.
    * **probing** -- the hold has elapsed and the canary is in flight, so there
      is no instant to count down to.  The sentence says what is being waited
      for instead, and the job carries no ``retry_at``.

    Every shape that has an instant ends in "<n> s", which is the clause the
    frontend rewrites as it counts down.
    """
    label = host_label(host)
    if seconds is None:
        return f"{label} rate limit, waiting for the first download to get through"
    if attempt is None:
        return f"{label} rate limit, waiting {int(round(seconds))} s"
    return (
        f"{label} rate limit, retry {attempt} of {total} in {int(round(seconds))} s"
    )


def gave_up_message(host: str, attempts: int, elapsed: float) -> str:
    """The error a job ends with when its automatic attempts are spent."""
    minutes = max(1, int(round(elapsed / 60)))
    return (
        f"{host_label(host)} rate limit: gave up after {attempts} attempts "
        f"over {minutes} min"
    )


def ceiling_message(host: str, reason: str | None) -> str:
    """The error every job in a lane gets when the hold outlives the ceiling."""
    if reason == REASON_BOT_CHECK:
        return (
            f"{host_label(host)} has asked this server to sign in to confirm "
            f"it is not a bot for over an hour. See {README_SIGN_IN_SECTION}."
        )
    return f"{host_label(host)} rate limited for over an hour"


def bot_check_message(host: str) -> str:
    """The error the job that walked into the wall ends with."""
    return (
        f"{host_label(host)} asked this server to sign in to confirm it is not "
        f"a bot. Downloads from {host_label(host)} are paused until you resume "
        f"them. See {README_SIGN_IN_SECTION}."
    )


def probe_message(host: str, seconds: float, reason: str | None) -> str:
    """The 400 a probe gets while its host's lane is held."""
    if reason == REASON_BOT_CHECK:
        return (
            f"{host_label(host)} has asked this server to sign in to confirm "
            f"it is not a bot. See {README_SIGN_IN_SECTION}."
        )
    return (
        f"{host_label(host)} is rate limiting this server, try again in "
        f"{max(1, int(round(seconds)))} s"
    )


def notice_message(host: str, reason: str | None, held_for: float) -> str:
    """The banner text for a held lane -- everything but the countdown.

    The seconds are deliberately *not* in here.  A notice's id changes only
    when the notice is raised afresh, and a re-raise is what un-dismisses a
    banner, so a message that counted down would have to be re-raised every
    second or two: a dismissed banner would come straight back, and the id
    would churn.  The banner counts down from the ``hold_until`` the notice
    carries instead, exactly as a queue row counts down from ``retry_at``.
    """
    label = host_label(host)
    if reason == REASON_BOT_CHECK:
        return (
            f"{label} asked this server to sign in to confirm it is not a bot. "
            f"Downloads from {label} are paused. See {README_SIGN_IN_SECTION}, "
            "or resume now to try again."
        )
    text = f"{label} is rate limiting this server. Downloads from {label} are paused."
    if held_for >= NOTICE_ESCALATE_SECONDS:
        text += (
            f" This has been going on for {int(held_for // 60)} minutes; see "
            f"{README_SIGN_IN_SECTION}."
        )
    return text


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


class LaneManager:
    """Every lane, its persistence, and the watchdog that enforces the ceiling.

    A module-level singleton (:data:`lanes`) rather than something the queue
    owns outright, because two very different callers need the same state: the
    download stage, which waits on a lane, and ``POST /download/probe``, which
    refuses outright while one is held.  Tests construct their own.

    *clock* returns an aware UTC datetime and is injected so the state machine
    can be driven without sleeping.
    """

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lanes: dict[str, _Lane] = {}
        self._store = None
        self._on_change: Callable[[str, LaneRecord], None] | None = None
        self._on_ceiling: Callable[[str, str | None], None] | None = None
        self._watchdog: asyncio.Task | None = None

    # -- wiring -----------------------------------------------------------

    def attach_store(self, store) -> None:
        """Persist lane state to *store* from now on, loading what it holds.

        A hold whose ``hold_until`` is already in the past is dropped rather
        than restored: it did its job while the process was down.
        """
        self._store = store
        now = self._clock()
        for record in store.load_lanes():
            if record.host not in LANE_HOSTS:
                continue
            if record.hold_until is not None and record.hold_until <= now:
                store.delete_lane(record.host)
                continue
            if record.hold_until is None and record.held_since is None:
                continue
            lane = _Lane(record)
            # Primed, not left false: a hold that was already past the
            # escalation when the process stopped comes back with the escalated
            # wording, and the watchdog must not "discover" that a minute later
            # and re-raise an identical notice under a new id.
            lane.escalated = lane.held_for(now) >= NOTICE_ESCALATE_SECONDS
            self._lanes[record.host] = lane
            logger.info(
                "Restored %s lane hold until %s (%s, %d consecutive)",
                record.host,
                record.hold_until,
                record.reason,
                record.consecutive,
            )
        if self._lanes:
            self._ensure_watchdog()
            self._changed_all()

    def set_callbacks(
        self,
        on_change: Callable[[str, LaneRecord], None] | None = None,
        on_ceiling: Callable[[str, str | None], None] | None = None,
    ) -> None:
        """Wire the banner (*on_change*) and the ceiling (*on_ceiling*).

        Each argument is applied only when it is given, so the queue (which
        wires the ceiling at construction time) and the app lifespan (which
        wires the banner once the notice board exists) can each set their own
        half without clobbering the other's.
        """
        if on_change is not None:
            self._on_change = on_change
        if on_ceiling is not None:
            self._on_ceiling = on_ceiling

    def close(self) -> None:
        """Stop the watchdog.  Called from the app lifespan."""
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def reset_for_tests(self) -> None:
        """Drop every lane and detach the store.  Used by the test fixtures.

        The callbacks stay: the app's singleton QueueManager wires its ceiling
        hook at import time and would never wire it again."""
        self.close()
        self._lanes.clear()
        self._store = None

    # -- access -----------------------------------------------------------

    def now(self) -> datetime:
        return self._clock()

    def lane(self, host: str) -> _Lane:
        """The lane for *host*, created open if it has never been in trouble."""
        existing = self._lanes.get(host)
        if existing is None:
            existing = _Lane(LaneRecord(host=host))
            self._lanes[host] = existing
        return existing

    def state(self, host: str) -> LaneRecord:
        """A snapshot of *host*'s lane."""
        lane = self.lane(host)
        lane._settle(self._clock())
        return lane.record()

    def is_held(self, host: str) -> bool:
        """Whether *host* is holding requests back right now."""
        lane = self._lanes.get(host)
        if lane is None:
            return False
        return lane.remaining(self._clock()) > 0

    def hold_message(self, host: str) -> str | None:
        """The sentence to refuse a probe with, or ``None`` when the lane is free."""
        lane = self._lanes.get(host)
        if lane is None:
            return None
        now = self._clock()
        remaining = lane.remaining(now)
        if remaining <= 0:
            return None
        return probe_message(host, remaining, lane.reason)

    # -- transitions ------------------------------------------------------

    def note_rate_limit(self, host: str, retry_after: float | None = None) -> float:
        lane = self.lane(host)
        wait = lane.note_rate_limit(self._clock(), retry_after)
        logger.warning(
            "%s rate limited this server; holding its lane for %.0f s "
            "(%d consecutive)",
            host_label(host),
            wait,
            lane.consecutive,
        )
        self._persist(lane)
        self._changed(lane)
        self._ensure_watchdog()
        return wait

    def note_bot_check(self, host: str) -> None:
        lane = self.lane(host)
        lane.note_bot_check(self._clock())
        logger.warning(
            "%s asked this server to prove it is not a bot; its lane is paused",
            host_label(host),
        )
        self._persist(lane)
        self._changed(lane)
        self._ensure_watchdog()

    def note_success(self, host: str) -> None:
        """A request to *host* got through: open the lane if it was not open."""
        lane = self._lanes.get(host)
        if lane is None:
            return
        if lane.hold_until is None and lane.held_since is None and not lane.probing:
            return
        lane.note_success()
        logger.info("%s is answering again; its lane is open", host_label(host))
        self._forget(lane)
        self._changed(lane)

    def resume(self, host: str) -> LaneRecord:
        """Clear *host*'s hold and let one waiter through.  The Resume button."""
        lane = self.lane(host)
        lane.resume()
        logger.info("%s lane resumed by request", host_label(host))
        self._forget(lane)
        self._changed(lane)
        return lane.record()

    def fire_ceiling(self, host: str) -> None:
        """Give up on a lane that has been in trouble for over an hour."""
        lane = self._lanes.get(host)
        if lane is None:
            return
        reason = lane.reason
        logger.error(
            "%s has been holding this queue for over an hour; failing its jobs",
            host_label(host),
        )
        lane.reset()
        self._forget(lane)
        self._changed(lane)
        if self._on_ceiling is not None:
            self._on_ceiling(host, reason)

    # -- waiting ----------------------------------------------------------

    async def wait_turn(
        self,
        host: str,
        job_id: str,
        abort: asyncio.Event,
        on_wait: Callable[[float | None], None] | None = None,
        on_block: Callable[[], None] | None = None,
        on_unblock: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Block until *job_id* may attempt a download on *host*.

        Returns as soon as the lane is open, or as soon as this job is elected
        canary of a lane whose hold has elapsed.  Returns *immediately* on an
        open lane, which is the overwhelmingly common case and costs one
        dictionary lookup.

        *abort* is set by a Cancel or by the ceiling; this returns on it too,
        and the caller checks it.

        *on_wait* is called with the seconds left every time the wait changes
        shape -- a new hold, an extended one, or the hold elapsing into a
        canary probe, which is announced as ``None`` because there is no
        instant to count down to.  Once per *state*, not once per wait: a job
        waiting behind a canary that is 429ed again must not be left showing an
        instant that has already passed, and a job whose hold has not moved
        must not re-emit an event that says nothing new.

        *on_block* and *on_unblock* bracket the actual waiting, and exist so
        the caller can hand back a resource it should not sit on while parked.
        *on_block* is called at most once, the first time this really has to
        wait; *on_unblock* is awaited before a successful return, and
        deliberately **not** on the *abort* path -- a cancel should not queue
        behind other jobs for a slot it is about to give up anyway.

        The job is *parked* on the lane only for as long as this is actually
        blocking.  A job that is downloading is on the lane and holds one of
        its slots, but parking it too would let it be elected canary -- and
        everyone else would then wait for a download that was never waiting for
        anything -- and would put it in front of the ceiling, which fails what
        it finds parked.

        The caller must call :meth:`leave` when it is done with the lane,
        whatever the outcome -- that is what stands a dead canary down.
        """
        lane = self.lane(host)
        blocked = False
        # The hold this job has already told the world about.  A sentinel
        # rather than None, because "probing, no instant" is itself a state
        # worth announcing exactly once.
        announced: datetime | None | _Unset = _UNSET
        try:
            while True:
                if abort.is_set():
                    return
                now = self._clock()
                if lane.may_run(job_id, now):
                    if blocked and on_unblock is not None:
                        await self._reacquire(on_unblock, abort)
                    return
                if not blocked:
                    blocked = True
                    lane.park(job_id)
                    if on_block is not None:
                        on_block()
                announced = await self._park_once(lane, abort, on_wait, announced)
        finally:
            lane.unpark(job_id)

    @staticmethod
    async def _reacquire(
        on_unblock: Callable[[], Awaitable[None]], abort: asyncio.Event
    ) -> None:
        """Take the caller's resource back, unless the job is aborted first.

        Without the race a Cancel pressed while this job is queueing for a
        download slot would not be seen until the slot came free, which on a
        busy queue is however long the job in front takes.  Cancelling the
        acquire is safe: ``asyncio.Semaphore.acquire`` puts the permit back if
        it is cancelled after being handed one, and the caller sets its "I hold
        it" flag with no await in between, so the two cannot disagree.
        """
        acquire = asyncio.ensure_future(on_unblock())
        aborted = asyncio.ensure_future(abort.wait())
        try:
            await asyncio.wait(
                {acquire, aborted}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            aborted.cancel()
            if not acquire.done():
                acquire.cancel()

    async def _park_once(
        self,
        lane: _Lane,
        abort: asyncio.Event,
        on_wait: "Callable[[float | None], None] | None",
        announced: "datetime | None | _Unset",
    ) -> "datetime | None":
        """One turn of the wait: announce if the hold moved, then sleep on it.

        *announced* is the ``hold_until`` the caller last reported, so a hold
        that has not moved says nothing and one that has -- extended by another
        429, or elapsed into a probe -- is re-announced.

        Returns the hold it announced, which the caller must keep: reading
        ``lane.hold_until`` again after the sleep would read whatever woke it
        up, and the next turn would then think it had already said that.
        """
        now = self._clock()
        remaining = lane.remaining(now)
        current = lane.hold_until
        # ``hold_until`` is None while the lane is probing, which is a
        # different sentence and a job with no ``retry_at``.
        if on_wait is not None and announced != current:
            on_wait(remaining if remaining > 0 else None)
        wake = lane.wake_event()
        slice_seconds = MAX_WAIT_SLICE_SECONDS
        if remaining > 0:
            slice_seconds = min(slice_seconds, remaining)
        pending = [
            asyncio.ensure_future(wake.wait()),
            asyncio.ensure_future(abort.wait()),
        ]
        try:
            await asyncio.wait(
                pending,
                timeout=max(0.01, slice_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in pending:
                task.cancel()
        return current

    def leave(self, host: str, job_id: str) -> None:
        """Take *job_id* off *host*'s lane entirely."""
        lane = self._lanes.get(host)
        if lane is not None:
            lane.dequeue(job_id)

    def waiting(self, host: str) -> list[str]:
        """The job ids *parked* on *host*'s lane, oldest first.

        Parked, not "on the lane": a job that is downloading is neither waiting
        for the lane nor safe for the ceiling to fail, and the ceiling is this
        method's only caller.
        """
        lane = self._lanes.get(host)
        return list(lane.parked) if lane is not None else []

    # -- internals --------------------------------------------------------

    def _persist(self, lane: _Lane) -> None:
        if self._store is None:
            return
        try:
            self._store.save_lane(lane.record())
        except Exception:
            # A lane hold that cannot be written is a worse restart, not a
            # failed download.
            logger.exception("Could not persist the %s lane", lane.host)

    def _forget(self, lane: _Lane) -> None:
        if self._store is None:
            return
        try:
            self._store.delete_lane(lane.host)
        except Exception:
            logger.exception("Could not clear the %s lane", lane.host)

    def _changed(self, lane: _Lane) -> None:
        """Announce *lane*'s current state, and mark it as announced.

        Clearing :attr:`_Lane.dirty` is unconditional, and outside the
        callback guard on purpose.  A settle can be triggered by any waiter's
        poll, so ``dirty`` means "the watchdog still has to say this"; every
        path that says it -- a 429, a bot check, a success, a resume, the
        ceiling -- comes through here, and leaving the flag set would have the
        next tick repeat the announcement and un-dismiss the banner.  A manager
        with no banner wired (every unit test) would otherwise accumulate the
        flag and behave differently from the app.
        """
        lane.dirty = False
        if self._on_change is not None:
            self._on_change(lane.host, lane.record())

    def _changed_all(self) -> None:
        for lane in list(self._lanes.values()):
            self._changed(lane)

    def _ensure_watchdog(self) -> None:
        """Start the ceiling watchdog if a lane needs watching.

        Lazily, and only while something is held: an app that never meets a
        rate limit runs no extra task at all.
        """
        if self._watchdog is not None and not self._watchdog.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (a synchronous unit test): the ceiling is then whatever
            # the test drives by hand.
            return
        self._watchdog = loop.create_task(self._watch())

    async def _watch(self) -> None:
        """Enforce the ceiling, and escalate a banner that has stood too long.

        Deliberately *not* a periodic re-raise of the notice.  A notice is
        re-raised to say "this is new", which is also what brings a dismissed
        banner back, so a tick that re-raised would un-dismiss the banner every
        fifteen seconds and churn its id.  The countdown is the banner's own
        job, from the ``hold_until`` the notice carries; this loop only reports
        the two things that are genuinely new -- the wording escalating, and
        the lane giving up.
        """
        try:
            while True:
                await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
                now = self._clock()
                busy = False
                for lane in list(self._lanes.values()):
                    if lane.held_since is None and lane.hold_until is None:
                        continue
                    busy = True
                    if lane.ceiling_reached(now):
                        self.fire_ceiling(lane.host)
                        continue
                    # A hold only lapses when somebody looks at it, and on a
                    # lane nobody is waiting on nobody does: without this a
                    # hold started by a refused probe would leave the banner up
                    # until the escalation, and then until the ceiling.
                    lane._settle(now)
                    if lane.probing and not lane.parked and lane.canary is None:
                        logger.info(
                            "The %s hold ran out with nothing waiting on it",
                            host_label(lane.host),
                        )
                        lane.idle_lapse()
                        self._forget(lane)
                    if not lane.escalated and lane.held_for(now) >= NOTICE_ESCALATE_SECONDS:
                        lane.escalated = True
                        lane.dirty = True
                    # Only when something actually moved.  A tick that reports
                    # an unchanged lane would re-raise the notice, and a
                    # re-raise is what un-dismisses a banner.  ``_changed``
                    # clears the flag itself, which is what stops a transition
                    # already announced by ``note_rate_limit`` from being
                    # announced again here.
                    if lane.dirty:
                        self._changed(lane)
                if not busy:
                    self._watchdog = None
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("The rate-limit watchdog failed")
            self._watchdog = None


# The one every caller uses.  Wired to the store and the notice board in the
# app lifespan; tests build their own and inject it.
lanes = LaneManager()
