"""In-memory async job queue manager for yt-dlp Web UI.

Owns the job lifecycle state machine, concurrency control via asyncio
semaphore, per-job timeout enforcement, and retry logic.  Integrates
with the downloader module to execute downloads.

State machine::

    queued ──► downloading ──► converting ──► [tagging] ──► done
       │             │              │             │
       │             └──────────────┴─────────────┴──► error  (failure / timeout)
       │             │              │             │
       └─────────────┴──────────────┴─────────────┴──► cancelled  (user)

    error ──► queued  (retry)
    error ──► gone    (dismiss)

A *bulk parent* (``JobKind.BULK``, phase 10) runs no machine of its own: it has
no thread, no slot and no URL to fetch, and its status is **derived** from its
children on every one of their transitions and written to its row from there
(:meth:`QueueManager._refresh_parent`).  Cancel on a parent cascades to every
child that has not finished; Retry belongs to the failed child, never to the
parent; Dismiss on the parent takes every child with it.

A *manual tagging job* (``JobKind.TAGGING``, phase 9) is the same machine with
the download taken out: ``queued ──► tagging ──► done``, plus ``error`` for a
lookup that could not happen and ``cancelled`` for one the user stopped.  It
takes no download slot, only the tagging lock.

``converting`` is reported by the downloader immediately before it starts
ffmpeg.  ``tagging`` is the automatic MusicBrainz tag fix (phase 8), and it is
the one stage that runs *outside* a download slot: the FLAC is already in the
library by then, so holding a slot for a lookup would idle a download for the
sake of a metadata request.  Instead the job releases its slot, the next
download starts, and the job waits on the queue's single tagging lock -- one
MusicBrainz conversation at a time, whatever else is running.

That also makes ``tagging`` the one in-flight state a job cannot fail or be
cancelled *out of*: its file exists and the user asked for it, so a cancel, a
lookup failure, a timeout and a restart all end the same way -- ``done``, with
the reason in ``Job.detail`` as "tags not fixed: ...".

Cancel is cooperative and asymmetric, because the two things a running job can
be doing are interruptible in opposite directions: yt-dlp only stops when its
own progress hook raises, ffmpeg only stops when its process is signalled.
Both live behind one :class:`~app.downloader.CancelToken` per run.  A queued
job never reaches either -- it is moved straight to ``cancelled``, and
:meth:`QueueManager._process_job` re-checks the status after acquiring its
concurrency slot, so a job cancelled while it was waiting for a slot never
starts.  A running job is only *signalled*; its thread decides the outcome,
so a cancel that arrives after the file has already been filed loses the race
and the job finishes ``done`` with its file, rather than being reported as
``cancelled`` with a track sitting in the library.  The request itself is
persisted as it is made, so a restart during those seconds finishes the job as
``cancelled`` rather than re-queuing a download the user stopped.

Persistence
-----------
An optional :class:`~app.job_store.JobStore` makes the queue survive a restart.
The dispatcher stays in memory; SQLite is a *write-through mirror*: every
transition is written before its SSE event is emitted, so the table is never
behind what a client has seen.  ``restore_from_store`` reloads the mirror at
boot, re-queues whatever was interrupted, and a daily :meth:`QueueManager.sweep`
drops finished rows once they are older than :data:`RETENTION_DAYS`.

The one deliberate exception to write-through: :meth:`QueueManager._persist`
logs and swallows a store failure rather than raising, and its caller emits the
SSE event regardless -- a database that cannot be written must not take a
running download down with it, so in that case the client is briefly ahead of
the table and the job simply will not survive a restart.

The store is optional so unit tests can drive the state machine without a
database; production always attaches one from the app lifespan.
"""

import asyncio
import concurrent.futures
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from app.downloader import (
    ALREADY_IN_LIBRARY_PREFIX,
    FFMPEG_TERMINATE_GRACE_SECONDS,
    CancelToken,
    DownloadError,
    FiledTrack,
    download_audio,
    remove_job_temp_dir,
    remove_orphan_temp_dirs,
    unfile_track,
)
from app.album_tagger import AlbumTagResult, TagStepFailed, tag_album
from app.file_organizer import DEFAULT_DOWNLOAD_PATH
from app.job_store import JobStore
from app.library_ops import (
    LibraryConflict,
    check_in_flight,
    check_resolved,
    is_audio,
)
from app.models import Job, JobKind, JobStatus, SSEEvent
from app.tagger import (
    NOTE_CANCELLED,
    NOTE_FAILED,
    NOTE_FILE_MISSING,
    NOTE_TIMED_OUT,
    NOTE_UNAVAILABLE,
    NOTE_UNREADABLE,
    NOTE_WRITE_FAILED,
    TagFixResult,
    fix_track,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT_DOWNLOADS = 2
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 900  # 15 minutes

# How long one automatic tag fix may take before it is abandoned.  Its own
# bound, deliberately not covered by DOWNLOAD_TIMEOUT_SECONDS: that timeout is
# sized for fetching a whole track over a slow line, and a MusicBrainz lookup
# that has not answered in a minute is not going to.
#
# It has to be enforced out here because ``musicbrainzngs`` cannot be told to
# time out: it opens its request with no timeout at all and retries a 5xx up to
# eight times with a growing delay, holding a process-global rate-limit lock
# for the whole retry loop.  ``asyncio.wait_for`` therefore bounds the *wait*,
# not the work -- the worker thread stays parked in urllib until the OS gives
# up on the socket, and a stalled connection can park it indefinitely.
#
# Three things make that survivable.  The fix runs on a thread of its own
# (:meth:`QueueManager._submit_tag_fix`) rather than on the loop's shared
# default executor, so an abandoned lookup can only ever occupy that one
# thread -- never a download or a library request.  The tagging lock plus the
# stuck-fix guard below keep the number of those threads at one.  And once a
# fix has timed out the next job's fix does not queue behind it: it is
# answered with the timed-out note immediately, until the stuck thread
# finally returns.
#
# That thread is a *daemon*, which is the whole reason it is not a
# ``ThreadPoolExecutor``: the executor registers an atexit hook that joins its
# worker at interpreter exit no matter what ``shutdown(wait=False)`` was told,
# so an abandoned lookup held the process open for minutes -- long past the
# container's stop grace, which then turned an orderly shutdown into a
# SIGKILL.  The trade-off is explicit: a daemon thread is killed at
# interpreter finalisation wherever it happens to be, and if that is inside
# ``FLAC.save`` the file could in principle be left truncated.  That window is
# a millisecond of writing at the end of a lookup we have already waited a
# minute for, against minutes of a process that will not die; the queue takes
# the millisecond.
DEFAULT_TAG_FIX_TIMEOUT_SECONDS = 60

# Days a done/cancelled job is kept before the retention sweep drops it.
# Errored jobs are never swept; they stay until the user dismisses them.
RETENTION_DAYS = 7

# How often a *restart* may interrupt a job before we stop re-queuing it.
# Counted separately from `attempts` (which also counts manual retries) so a
# user who retries twice does not spend the job's restart budget.
MAX_RESTART_ATTEMPTS = 3
RESTART_GIVE_UP_MESSAGE = f"interrupted by restart {MAX_RESTART_ATTEMPTS} times"

# The 400 a Retry on a bulk parent gets.  A parent has nothing of its own to
# re-run -- it is the sum of its children -- so the only meaningful retry is on
# the child that failed, and saying so is more use than "wrong status".
BULK_RETRY_MESSAGE = (
    "A bulk download has nothing of its own to retry; retry the failed track "
    "instead"
)

# How long a timed-out job's thread may take to unwind before its concurrency
# slot is released anyway.  Cancelling the run signals ffmpeg, which its thread
# then gives FFMPEG_TERMINATE_GRACE_SECONDS to exit before killing it, so the
# grace is that plus a moment for the unwinding itself.  Bounded because a
# thread that is somehow stuck must not take the whole queue down with it.
THREAD_DRAIN_GRACE_SECONDS = FFMPEG_TERMINATE_GRACE_SECONDS + 1

# How often the drain wait looks at the thread it is waiting for.  Polling from
# the event loop rather than parking a worker on `finished.wait`: that wait
# would run in the same default executor as the download threads themselves
# (and as `extract_metadata`), so with every worker busy the wait could not
# start, and the slot it is guarding would be held for the whole grace anyway.
_DRAIN_POLL_SECONDS = 0.05

_IN_FLIGHT = (
    JobStatus.QUEUED,
    JobStatus.DOWNLOADING,
    JobStatus.CONVERTING,
    JobStatus.TAGGING,
)

# Statuses that only a running process can be in.  Finding one of these in the
# database at boot means the process died mid-job.
_INTERRUPTED = (JobStatus.DOWNLOADING, JobStatus.CONVERTING, JobStatus.TAGGING)

# Statuses no in-flight transition may leave.
_TERMINAL = (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED)

# Statuses the retention sweep may drop.  Mirrors JobStore.TERMINAL_STATUSES.
_SWEEPABLE = (JobStatus.DONE, JobStatus.CANCELLED)

# Notes a *manual* tagging job reports as a failure rather than as a finished
# job with something to say.  The metadata ticket's rule: a download always
# completes even when its automatic fix could not run ("tags not fixed: ..."
# in the detail), but a job whose entire purpose was to fix tags and did not
# has failed, and the user gets a Retry and a Dismiss.  "No match" is
# deliberately absent -- MusicBrainz answering "I do not know this recording"
# is a result, not a failure.
_TAGGING_FAILURES = frozenset(
    {
        NOTE_UNAVAILABLE,
        NOTE_TIMED_OUT,
        NOTE_FAILED,
        NOTE_WRITE_FAILED,
        NOTE_UNREADABLE,
        NOTE_FILE_MISSING,
    }
)


def _env_int(name: str, default: int) -> int:
    """Read an integer env var; empty or missing values yield *default*.

    docker compose substitutes an unset variable with an empty string, so
    ``""`` must count as unset rather than crash ``int()``.
    """
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class InFlightTargets:
    """Where the running downloads are going, and how many will not say yet.

    ``targets`` are the library folders (artist, and artist/album when the job
    has an album) that in-flight jobs will write into; ``unresolved`` is how
    many in-flight jobs have not resolved a destination yet.  The two travel
    together because a caller that only looked at ``targets`` would read an
    unresolved job as "aiming nowhere" and happily rename the folder it is
    about to land in.

    ``tagging_paths`` is the third thing a library write has to wait for and is
    deliberately its own field rather than more ``targets``: a tagging job
    creates no folder, so it is not a destination -- it is a path already in
    the library whose files are about to be rewritten, and it is guarded in
    both directions (see
    :func:`~app.library_ops.check_not_being_tagged`) where a download's target
    is only ever guarded as a folder something lands inside.
    """

    targets: list[str]
    unresolved: int
    # One short label per unresolved job -- its title, or its id while the
    # probe has not returned one -- so the 409 can name the download the user
    # is being asked to wait for instead of leaving them to guess at a queue
    # that may be several jobs deep.
    unresolved_jobs: list[str] = field(default_factory=list)
    # The ``job.path`` of every in-flight tagging job -- the album folder or
    # the single track the pass is rewriting.  Never the artist folder above
    # it: the artist is not what the pass touches, and guarding it would
    # refuse a move of an unrelated album by the same artist.
    tagging_paths: list[str] = field(default_factory=list)


def tagging_conflict_message(conflict: "Job") -> str:
    """The 409 text for "something is already tagging this path".

    One function because two callers say it: the route checks before it builds
    a job (so the tagging-vs-tagging answer wins over the in-flight download
    one, which would otherwise be the first to fire) and
    :meth:`QueueManager.add_tagging_job` checks again as it adds.
    """
    return (
        f"{conflict.path!r} is already being tagged "
        f"(job {conflict.id}, {conflict.status.value})"
    )


def tagging_guarded_folder(path: str) -> str:
    """The folder a tagging job on *path* is guarded as, from the path alone.

    The album trigger names a folder and is guarded as itself; the track
    trigger names a file and is guarded as the folder it sits in, because that
    is the folder a download could file into underneath the pass.  The same
    two answers the ``POST /library/tag`` route computes with a ``is_dir``
    call, derived here from the name instead -- a retry has only the stored
    row to go on, and :func:`~app.library_ops.is_audio` is the very test the
    route made the file take before the job existed.
    """
    if not path or not is_audio(Path(path)):
        return path
    parent, separator, _ = path.rpartition("/")
    return parent if separator else ""


class QueueError(Exception):
    """Raised for queue-level errors (invalid retry, wrong state, etc.).

    Routes map this to 400: it always means the caller asked for something the
    job's current state does not allow.
    """


class JobNotFound(QueueError):
    """Raised when an operation names a job id the queue does not know.

    A subclass so a route can tell "no such job" (404) from "not in a state
    that allows this" (400) without inspecting the message.
    """


class TaggingConflict(QueueError):
    """Raised when re-running a tagging job would collide with other work.

    A subclass so the retry route can answer **409** ("something else is using
    this path, try again in a moment") rather than the 400 a plain
    :class:`QueueError` means ("this job cannot be retried at all").  The job
    is left exactly as it was: still ``error``, still carrying its detail,
    still retryable, and its ``attempts`` not spent on a run that never
    happened.

    Its message travels as a plain string, because that is what the frontend
    unwraps out of a failed retry.
    """


@dataclass
class _ActiveRun:
    """Bookkeeping for one download thread.

    ``cancel`` is the run's stop button, shared with the downloader: it aborts
    yt-dlp at its next progress callback and signals a running ffmpeg, which
    the thread then kills if it does not go.
    ``finished`` is set by the thread when it has fully exited.

    ``lock``, ``filed`` and ``disowned`` are the hand-off between the thread and
    the event loop over the one thing both can touch: a track that has already
    been moved into the library.  The thread publishes it in ``filed``; the loop
    sets ``disowned`` when it has given the job a final status without waiting
    for the thread (the timeout path).  Both read the other's field inside
    ``lock`` in the same critical section they write their own, so whichever
    happens second sees the first and exactly one of them takes the track back
    out of the library -- never both, and never neither.  ``disowned`` doubles
    as "the loop's verdict is already written", which is how a thread's
    ``DownloadError`` is told from a user's Cancel.
    """

    cancel: CancelToken = field(default_factory=CancelToken)
    finished: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    filed: FiledTrack | None = None
    disowned: bool = False


class QueueManager:
    """Async job queue with concurrency control, timeouts, and event hooks.

    Args:
        max_concurrent: Maximum simultaneous downloads (from env or default).
        timeout: Per-job download timeout in seconds (from env or default).
        on_event: Optional callback invoked with ``SSEEvent`` on every
            job state change and progress update.
        store: Optional SQLite store.  When given, every transition is written
            to it before its event is emitted.  ``None`` keeps the queue purely
            in memory (unit tests).
        tag_fix_timeout: Seconds one automatic tag fix may take before the job
            gives up on it and finishes ``done`` anyway.
    """

    def __init__(
        self,
        max_concurrent: int | None = None,
        timeout: int | None = None,
        on_event: Callable[[SSEEvent], None] | None = None,
        store: JobStore | None = None,
        tag_fix_timeout: int | None = None,
    ) -> None:
        if max_concurrent is None:
            max_concurrent = _env_int("MAX_CONCURRENT_DOWNLOADS", DEFAULT_MAX_CONCURRENT_DOWNLOADS)
        if timeout is None:
            timeout = _env_int("DOWNLOAD_TIMEOUT_SECONDS", DEFAULT_DOWNLOAD_TIMEOUT_SECONDS)

        if tag_fix_timeout is None:
            tag_fix_timeout = _env_int(
                "TAG_FIX_TIMEOUT_SECONDS", DEFAULT_TAG_FIX_TIMEOUT_SECONDS
            )

        self._max_concurrent = max_concurrent
        self._timeout = timeout
        self._tag_fix_timeout = tag_fix_timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # The single tagging slot.  A lock rather than a Semaphore(1) because
        # asyncio's lock hands ownership to waiters in the order they arrived,
        # which is the FIFO the ticket asks for: two downloads that finish
        # together are tagged in the order they finished, not in whichever
        # order the loop happens to wake them.  It is also what keeps this
        # app to one MusicBrainz request at a time, which is what the service's
        # rate limit expects of a client.
        self._tagging_lock = asyncio.Lock()
        # Set by :meth:`close` and never cleared: once the app is on its way
        # down, a fix that reaches the worker is answered rather than started.
        self._closed = False
        # The abandoned lookup, while its thread is still running.
        # While it is set, a fix is answered with the timed-out note straight
        # away rather than queueing behind a thread that cannot start it.
        self._stuck_tag_fix: concurrent.futures.Future | None = None
        self._jobs: dict[str, Job] = {}
        self._on_event = on_event
        self._store = store
        # Download threads that are, or may still be, running per job id.
        self._active_runs: dict[str, _ActiveRun] = {}
        # Strong references to the dispatcher tasks; see _dispatch.
        self._tasks: set[asyncio.Task] = set()

    def attach_store(self, store: JobStore) -> None:
        """Attach the persistence layer after construction.

        ``main`` builds the QueueManager singleton at import time so routes can
        reference it, but the store can only be opened once ``DATA_PATH`` has
        been validated in the lifespan handler.
        """
        self._store = store

    @property
    def tag_fix_timeout(self) -> int:
        """The tag-fix timeout this queue is actually using, in seconds.

        Resolved once in ``__init__``; exposed so the startup banner can report
        the effective value rather than re-reading and re-parsing the env.
        """
        return self._tag_fix_timeout

    def close(self) -> None:
        """Release the tagging thread on the way down.

        Called from the app lifespan, before ``store.close()``: this is the
        producer of the last writes the store will see, so it is released
        first.

        It does not wait, and now that the fix runs on a daemon thread that is
        true all the way to process exit rather than only until the interpreter
        starts shutting down: a lookup still in flight is a metadata request
        whose result nothing is left to use, and an abandoned one may not
        return for as long as the socket takes to die -- neither is worth
        holding up shutdown for.  A fix submitted from here on is answered with
        the cancelled note instead of being started, and the flag is never
        cleared: a closed queue stays closed.
        """
        self._closed = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_job(self, job: Job) -> Job:
        """Add a job to the queue and kick off async processing.

        The job should already have metadata populated (title,
        thumbnail_url, duration) from a prior ``extract_metadata`` call.
        Its status must be ``QUEUED``.

        Returns the job as stored in the queue.

        Raises:
            QueueError: If the same URL is already queued or in progress.
        """
        duplicate = self.find_in_flight(job.url)
        if duplicate is not None:
            raise QueueError(
                f"This URL is already in the queue (job {duplicate.id}, {duplicate.status.value})"
            )

        self._jobs[job.id] = job
        # Persist before the job can start moving: a crash between here and the
        # first transition must still leave a queued row behind.
        self._persist(job)
        logger.info(
            "Job %s added to queue: url=%s, artist=%r, album=%r, title=%r",
            job.id,
            job.url,
            job.artist,
            job.album,
            job.title,
        )
        self._dispatch(job.id)
        return job

    def find_in_flight(self, url: str) -> Job | None:
        """Return a queued/downloading/converting/tagging job for *url*, if any.

        Scans the in-memory dict, which ``restore_from_store`` fills before the
        app serves its first request -- so a URL restored from a previous run
        blocks a resubmission just like a freshly queued one.
        """
        # ``list(...)`` for the same reason as in :meth:`children_of`: this is
        # reachable from a worker thread, and the loop may be inserting.
        for job in list(self._jobs.values()):
            if job.url == url and job.status in _IN_FLIGHT:
                return job
        return None

    def add_tagging_job(self, job: Job) -> Job:
        """Add a manual tagging job and start it on the tagging worker.

        Unlike :meth:`add_job` there is no download slot and no URL: what makes
        two tagging jobs the same is the library path they are about to write
        into, which is also why the duplicate check is
        :meth:`find_tagging_conflict` rather than the URL check downloads use.

        Raises:
            QueueError: another tagging job is already working on this path,
                or on one that contains it or sits inside it.
        """
        conflict = self.find_tagging_conflict(job.path or "")
        if conflict is not None:
            raise QueueError(tagging_conflict_message(conflict))

        self._jobs[job.id] = job
        self._persist(job)
        logger.info(
            "Tagging job %s added to queue for %r", job.id, job.path
        )
        self._dispatch_tagging_job(job.id)
        return job

    def find_tagging_conflict(self, path: str, exclude: str | None = None) -> Job | None:
        """An in-flight tagging job whose path overlaps *path*, if there is one.

        Overlap, not equality: a track inside an album that is already being
        tagged would have the two passes writing the same file, and an album
        containing a track that is being tagged is the same collision seen from
        the other end.  Compared as path prefixes, which is what the library's
        own guards do.

        *exclude* is a job id to ignore -- the retry path's own job, which is
        asking whether anything *else* is on its path.
        """
        if not path:
            return None
        for job in self._jobs.values():
            if job.id == exclude:
                continue
            if job.kind is not JobKind.TAGGING or job.status not in _IN_FLIGHT:
                continue
            other = job.path or ""
            if not other:
                continue
            if (
                other == path
                or other.startswith(path + "/")
                or path.startswith(other + "/")
            ):
                return job
        return None

    # ------------------------------------------------------------------
    # Bulk parents and their children
    # ------------------------------------------------------------------

    def add_bulk_job(
        self,
        parent: Job,
        children: list[Job],
        already_in_library: dict[str, str] | None = None,
    ) -> Job:
        """Queue a bulk download: one parent row and one child per track.

        The parent is written first, because every child row carries a
        ``parent_id`` foreign key at it, and because a crash between the two
        must leave a parent the next boot can re-derive rather than orphans.

        Each child then takes one of three shapes before it is ever dispatched:

        * **already in the library** -- ``already_in_library`` maps a child's
          id to the library path the dedup rule matched.  The child is created
          directly in ``error`` with
          :data:`~app.downloader.ALREADY_IN_LIBRARY_PREFIX` and that path,
          which is the same string a single download's own duplicate check
          writes, so the frontend renders it as "Skipped" with no Retry.  It is
          persisted (the user asked for it and deserves to see why it did not
          happen) but never dispatched and it takes no download slot;
        * **already in flight** -- the URL is queued or running somewhere else,
          including earlier in this same submission.  Also ``error``, with the
          job it collided with named;
        * anything else is ``queued`` and dispatched, in the order the user
          selected them, through the ordinary download slots.

        Children resolve their own metadata when they run: the preview's flat
        pass gives a title and sometimes a duration, and the download pipeline
        probes properly anyway (source enumeration research), so paying for a
        full extraction per row at submit time would be minutes of waiting for
        data that is about to be fetched again.

        Args:
            parent: The ``BULK`` job, carrying the collection URL, the title and
                the artist the user chose.
            children: ``DOWNLOAD`` jobs with ``parent_id`` already set, in the
                order they should run.
            already_in_library: child id -> the library path that matched.

        Returns:
            The parent, with its derived status written.

        Raises:
            QueueError: the collection URL already has an in-flight parent --
                the same rule a single download's duplicate check applies.
        """
        duplicate = self.find_in_flight(parent.url)
        if duplicate is not None:
            raise QueueError(
                f"This URL is already in the queue (job {duplicate.id}, "
                f"{duplicate.status.value})"
            )

        skipped = already_in_library or {}
        self._jobs[parent.id] = parent
        self._persist(parent)
        logger.info(
            "Bulk job %s added: url=%s, artist=%r, %d child job(s)",
            parent.id,
            parent.url,
            parent.artist,
            len(children),
        )

        # One pass over the queue instead of one per child: a 2000-track
        # submission would otherwise be 2000 scans of a dict that is 2000
        # entries longer each time.  A child queued here is added to the same
        # index, so a URL that appears twice in one submission still collides
        # with itself.  First writer wins, matching ``find_in_flight``.
        in_flight_by_url: dict[str, Job] = {}
        for job in list(self._jobs.values()):
            if job.status in _IN_FLIGHT and job.url:
                in_flight_by_url.setdefault(job.url, job)

        dispatch: list[str] = []
        for child in children:
            library_path = skipped.get(child.id)
            if library_path:
                # No in-flight check at all: the answer cannot change what this
                # child says, and the row is already the more useful one.
                child.status = JobStatus.ERROR
                child.error = f"{ALREADY_IN_LIBRARY_PREFIX}{library_path}"
            else:
                in_flight = in_flight_by_url.get(child.url)
                if in_flight is not None:
                    child.status = JobStatus.ERROR
                    child.error = (
                        f"This URL is already in the queue (job {in_flight.id}, "
                        f"{in_flight.status.value})"
                    )
                else:
                    child.status = JobStatus.QUEUED
                    if child.url:
                        in_flight_by_url[child.url] = child
                    dispatch.append(child.id)
            self._jobs[child.id] = child

        # One transaction for every child, after the parent's own row is
        # committed (the foreign key points at it).  Dispatch waits for that
        # commit: a child that starts downloading before its row exists could
        # write a status update the restart would then have nothing to attach
        # to.
        self._persist_many(children)

        for job_id in dispatch:
            self._dispatch(job_id)

        # Written and announced once, after every child exists: a refresh per
        # child would emit N status_change events for a parent whose status
        # only settles at the end of the loop.
        self._refresh_parent(parent.id)
        return parent

    def children_of(self, parent_id: str) -> list[Job]:
        """This parent's child jobs, oldest first.

        ``created_at`` order rather than dict order, so the queue shows the
        children in the order the user selected them however they were stored.

        Iterates a ``list`` snapshot rather than ``_jobs.values()`` directly
        because this is one of the few queue reads that happens **off the event
        loop**: a download worker thread's ``on_phase("converting")`` hook
        reaches ``_update_status`` and so ``_refresh_parent`` from inside
        yt-dlp, and :meth:`add_bulk_job` is meanwhile inserting a child per
        iteration on the loop.  Without the snapshot that is a "dictionary
        changed size during iteration" raised inside an unrelated child's
        download, which fails it with "Unexpected error".
        """
        return sorted(
            (job for job in list(self._jobs.values()) if job.parent_id == parent_id),
            key=lambda job: job.created_at,
        )

    @staticmethod
    def derive_bulk_status(children: Iterable[Job]) -> JobStatus:
        """The status a bulk parent has, given its children.

        The ticket's rule, in its order: anything running makes the parent
        ``downloading``; else anything waiting makes it ``queued``; else a
        failure outranks a cancellation, because an error is what the user
        still has to act on; else everything finished and the parent is
        ``done``.

        A parent with no children left is ``done``.  That is the honest answer
        for the two ways it happens -- every child dismissed, or the retention
        sweep having taken them -- and it is also what makes such a parent
        leave the queue view instead of sitting there for ever.
        """
        statuses = [child.status for child in children]
        if any(
            status
            in (JobStatus.DOWNLOADING, JobStatus.CONVERTING, JobStatus.TAGGING)
            for status in statuses
        ):
            return JobStatus.DOWNLOADING
        if any(status is JobStatus.QUEUED for status in statuses):
            return JobStatus.QUEUED
        if any(status is JobStatus.ERROR for status in statuses):
            return JobStatus.ERROR
        if any(status is JobStatus.CANCELLED for status in statuses):
            return JobStatus.CANCELLED
        return JobStatus.DONE

    @staticmethod
    def _is_skipped(job: Job) -> bool:
        """Whether this child failed only because the track was already there.

        The downloader ends a duplicate as an error carrying
        :data:`~app.downloader.ALREADY_IN_LIBRARY_PREFIX`, and that is the
        right shape -- the reason has to stay visible and there is nothing to
        retry -- but it is not work still to be done, so the parent's "N of M"
        counts it as finished.
        """
        return job.status is JobStatus.ERROR and bool(
            job.error and job.error.startswith(ALREADY_IN_LIBRARY_PREFIX)
        )

    def _refresh_parent(self, parent_id: str | None, emit: bool = True) -> None:
        """Re-derive a parent from its children after one of them moved.

        Called after every child transition.  Two things come out of it:

        * the derived status is **written to the parent row**.  The column is
          ``NOT NULL`` and ``load_active`` filters on it, so a parent whose
          status lived only in a ``GET /queue`` computation would not survive a
          restart -- and the retention sweep, which reads the same column, would
          never reap it either.  ``finished_at`` follows the status, including
          back to ``None`` when a retried child brings the parent back to life;
        * a synthetic ``status_change`` for the parent carrying ``progress_done``
          of ``progress_total`` -- children finished, of children in total,
          where a skipped duplicate counts as finished -- which is the "N of M"
          the queue row shows.  Emitted on every child change,
          not only when the parent's status changes, because N moves while the
          status stands still.

        *emit* is False during boot restore, where there is nobody connected to
        hear it and every client refetches ``GET /queue`` anyway.
        """
        parent = self._jobs.get(parent_id) if parent_id else None
        if parent is None or parent.kind is not JobKind.BULK:
            return
        children = self.children_of(parent.id)
        status = self.derive_bulk_status(children)

        parent.progress_total = len(children)
        # Skipped duplicates count as done: the parent of a collection whose
        # tracks were all already in the library reads "12 of 12", not "0 of
        # 12".  Its *status* is still ``error`` -- ``derive_bulk_status`` is
        # deliberately untouched -- so the skip reason stays on screen.
        parent.progress_done = sum(
            1
            for child in children
            if child.status is JobStatus.DONE or self._is_skipped(child)
        )
        parent.progress = (
            100.0 * parent.progress_done / parent.progress_total
            if parent.progress_total
            else 0.0
        )
        # The parent has no error of its own; the failed child carries it.  The
        # field is cleared so a parent that goes back in flight after a retry
        # does not keep a stale one.
        parent.error = None

        if status is not parent.status:
            old_status = parent.status.value
            parent.status = status
            if status not in _TERMINAL:
                parent.finished_at = None
            self._persist(parent)
            logger.info("Bulk job %s: %s -> %s", parent.id, old_status, status.value)

        if emit:
            self._emit_event("status_change", parent)

    def _delete_job(self, job_id: str) -> None:
        """Drop one job from the queue and from the table, children included.

        The row's ``ON DELETE CASCADE`` takes a parent's children with it in
        SQLite (the store opens with ``foreign_keys=ON``), but the in-memory
        mirror has no such thing, so the children are dropped here explicitly --
        otherwise a dismissed parent's children would live on in ``GET /queue``
        until the process restarted.
        """
        for child in self.children_of(job_id):
            self._jobs.pop(child.id, None)
            self._active_runs.pop(child.id, None)
        self._jobs.pop(job_id, None)
        self._active_runs.pop(job_id, None)
        if self._store is not None:
            try:
                self._store.delete(job_id)
            except Exception:
                # The row outliving the dict costs one stale entry after the
                # next restart, which is a great deal better than a 500 on a
                # button whose whole purpose is tidying up.
                logger.exception("Could not delete job %s from the store", job_id)

    def queue_view(self) -> list[Job]:
        """The queue as ``GET /queue`` returns it: parents with children nested.

        Top level is what still needs attention -- in-flight and errored
        standalone downloads and tagging jobs, exactly as before, plus bulk
        parents whose *derived* status is in flight or error -- in insertion
        order.  A child never appears at the top level, and a parent nests
        **all** of its children whatever their status: a bulk of ten with nine
        done is still one row that expands to ten, and hiding the finished nine
        would make "1 of 10" unreadable.

        A done or cancelled parent is omitted with its children, like any other
        finished job.

        The nested jobs are copies (``model_copy``), never the queue's own Job
        objects: ``children`` is a response shape, and hanging it off the live
        object would leave every later reader -- the store, an SSE payload, the
        next request -- carrying a snapshot that is already stale.
        """
        view: list[Job] = []
        for entry in list(self._jobs.values()):
            if entry.parent_id:
                continue
            if entry.status in _SWEEPABLE:
                continue
            if entry.kind is JobKind.BULK:
                view.append(
                    entry.model_copy(
                        update={
                            "children": [
                                child.model_copy(update={"children": []})
                                for child in self.children_of(entry.id)
                            ]
                        }
                    )
                )
            else:
                view.append(entry)
        return view

    def with_children(self, job: Job) -> Job:
        """*job* as a route returns it: a bulk parent carries its children.

        The single-job counterpart of :meth:`queue_view`, for the routes that
        answer with one job (submit, cancel, retry).  Anything that is not a
        parent is returned unchanged, children empty.
        """
        if job.kind is not JobKind.BULK:
            return job
        return job.model_copy(
            update={
                "children": [
                    child.model_copy(update={"children": []})
                    for child in self.children_of(job.id)
                ]
            }
        )

    def in_flight_library_targets(self) -> InFlightTargets:
        """The library folders in-flight jobs are going to write into.

        Read off ``Job.target_dir``, the folder the job resolved for itself:
        one entry for the artist folder and, when the job has an album, a
        second for the album folder, as POSIX paths relative to
        ``DOWNLOAD_PATH``.  ``target_dir`` is set at submit time from the
        user's names plus the probe's metadata and rewritten by the download
        thread the moment it has resolved the real one, so this is the folder
        a running download will actually create rather than a second guess at
        it -- guessing from ``job.artist`` alone answered "Unknown Artist" for
        every job the user did not name an artist for, while the file landed
        under whatever yt-dlp said.

        ``unresolved`` counts the in-flight downloads whose ``target_dir`` is
        either missing or still the submit-time guess, and ``unresolved_jobs``
        names them.  Their destination is
        genuinely unknown, so the only safe answer for a caller about to change
        the tree is "not yet" -- see :func:`~app.mover.move_library_entry`,
        which turns a non-zero count into a 409 that says which download it is
        waiting for.

        ``tagging_paths`` is separate and comes from ``Job.path``: a tagging
        job has no ``target_dir`` (it creates nothing) but is rewriting files
        that are already there, and a move or a delete of those has to wait
        for it just the same.

        The library's move and delete routes refuse to touch a folder that
        appears here: a download that lands in a folder renamed out from under
        it leaves a track the user cannot find.
        """
        targets: list[str] = []
        unresolved_jobs: list[str] = []
        tagging_paths: list[str] = []
        for job in list(self._jobs.values()):
            if job.status not in _IN_FLIGHT:
                continue
            # Only downloads create folders in the library.  A bulk parent and
            # a standalone tagging job never write a new path, so neither is a
            # destination anybody has to wait for -- and a tagging *job* has no
            # ``target_dir`` at all, which would make it "unresolved" and so
            # refuse every move for a reason that is not true.  A tagging job
            # is guarded instead through ``tagging_paths``, by the path it is
            # rewriting.
            #
            # A download in the ``tagging`` *status* is a different thing and
            # is deliberately still guarded here: it keeps its kind and its
            # resolved ``target_dir``, and it is about to rewrite tags in a
            # file inside that folder, so a move or a delete of the folder
            # while the fix is running would have the tagger writing to a path
            # that no longer exists.
            if job.kind is JobKind.TAGGING:
                if job.path and job.path not in tagging_paths:
                    tagging_paths.append(job.path)
                continue
            if job.kind is not JobKind.DOWNLOAD:
                continue
            # A guessed target is no better than no target: it is the
            # "Unknown Artist" fallback, and the download will land somewhere
            # else entirely once the download thread has probed.  Naming that
            # folder in ``targets`` would guard the wrong one and leave the
            # real one unguarded.
            if not job.target_dir or job.target_guessed:
                unresolved_jobs.append(job.title or job.url or job.id)
                continue
            artist = job.target_dir.split("/")[0]
            if artist not in targets:
                targets.append(artist)
            if job.target_dir not in targets:
                targets.append(job.target_dir)
        return InFlightTargets(
            targets=targets,
            unresolved=len(unresolved_jobs),
            unresolved_jobs=unresolved_jobs,
            tagging_paths=tagging_paths,
        )

    def get_jobs(self) -> list[Job]:
        """Return the in-flight and errored jobs, ordered by insertion.

        ``done`` and ``cancelled`` jobs are omitted: the queue view is about
        what still needs attention.  They stay in the in-memory dict until the
        retention sweep drops them, so a resubmitted URL can still be told
        apart from a running one and a finished job can still be looked up by
        id.

        The dict is not a full copy of the table: it holds the in-flight and
        errored jobs plus whatever finished during *this* process's lifetime,
        because ``load_active`` deliberately does not reload ``done``/
        ``cancelled`` rows at boot.  The table additionally holds the rows that
        finished in earlier runs, until the sweep drops them.
        """
        return [
            job for job in self._jobs.values() if job.status not in _SWEEPABLE
        ]

    def get_job(self, job_id: str) -> Job | None:
        """Return a single job by ID, or ``None`` if not found."""
        return self._jobs.get(job_id)

    def retry_job(self, job_id: str) -> Job:
        """Re-queue a failed or errored job.

        Resets the job to ``QUEUED`` status, clears the error message and
        progress, increments ``attempts``, zeroes ``restart_attempts``,
        persists the row, and schedules it for processing again.  Retries are
        manual and unlimited.

        A *tagging* job additionally re-takes the guards the ``POST
        /library/tag`` route took before the job existed.  A retry is a second
        submission of the same request, minutes or hours later, and the library
        has moved on: the folder may now be inside another tagging job's path,
        or a download may be about to file into it.  Re-running the pass
        without asking would have two writers on the same files.

        Raises:
            QueueError: If the job does not exist, is not in ERROR status,
                or its previous download thread has not exited yet.
            TaggingConflict: If a tagging job's path is now busy.  Nothing is
                mutated -- the row stays ``error``, keeps its detail and can be
                retried again once the collision clears.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"Job {job_id!r} not found")
        if job.kind is JobKind.BULK:
            # Checked before the status test so the answer is the useful one:
            # a parent in `error` is exactly the parent a user would press
            # Retry on, and "only ERROR jobs can be retried" would be a lie.
            raise QueueError(BULK_RETRY_MESSAGE)
        if job.status != JobStatus.ERROR:
            raise QueueError(
                f"Job {job_id!r} is in {job.status.value!r} status, only ERROR jobs can be retried"
            )
        run = self._active_runs.get(job_id)
        if run is not None and not run.finished.is_set():
            raise QueueError(
                f"Job {job_id!r} is still shutting down its previous attempt; retry in a moment"
            )
        if job.kind is JobKind.TAGGING:
            # Before the first mutation, so a refused retry costs the row
            # nothing -- not its detail, and not an attempt.
            self._check_tagging_path_free(job)

        job.status = JobStatus.QUEUED
        job.error = None
        # The note from the previous run's tag fix describes a run that is
        # about to be replaced; leaving it would have the new run's outcome
        # read against the old run's reason.
        job.detail = None
        job.progress = 0.0
        job.progress_done = None
        job.progress_total = None
        job.finished_at = None
        job.attempts += 1
        # A deliberate retry is a fresh start: give the job its full restart
        # budget back.  Not reset anywhere else -- resetting on every boot
        # would let a job that crashes the process resume forever.
        job.restart_attempts = 0
        # The previous attempt may have been cancelled and have failed for some
        # other reason first; a job the user has just asked to run again is not
        # a job the user has asked to stop.
        job.cancel_requested = False
        # ``target_dir`` and ``target_guessed`` are deliberately left as they
        # are.  It was resolved from
        # the same ``artist``/``album`` this re-run will resolve from again
        # (neither is ever mutated), and ``on_target`` overwrites it before the
        # run creates any folder -- so the stored answer is the best guess
        # available and guarding it costs nothing.  Nulling it made every
        # retried job count as unresolved, which refused *every* move with a
        # 409 until the job finally ran.
        self._persist(job)
        self._emit_event("status_change", job)

        logger.info("Job %s retried, re-queued for processing", job.id)
        # A retried child puts its parent back in flight (ticket): the derived
        # status is recomputed and written before the child can start moving.
        self._refresh_parent(job.parent_id)
        if job.kind is JobKind.TAGGING:
            self._dispatch_tagging_job(job.id)
        else:
            self._dispatch(job.id)
        return job

    def _check_tagging_path_free(self, job: Job) -> None:
        """Raise :class:`TaggingConflict` when *job* cannot be re-run yet.

        The three checks the tag route makes, in the same order and for the
        same reasons: another tagging job on an overlapping path first (so the
        answer names what the user actually collided with), then a download
        aiming into the guarded folder, then a download that has not said where
        it is aiming at all.

        The caller holds ``LIBRARY_WRITE_LOCK``, which is what keeps this from
        passing against a tree a move already running in a thread is halfway
        through renaming.
        """
        path = job.path or ""
        conflict = self.find_tagging_conflict(path, exclude=job.id)
        if conflict is not None:
            raise TaggingConflict(tagging_conflict_message(conflict))
        in_flight = self.in_flight_library_targets()
        try:
            check_in_flight([tagging_guarded_folder(path)], in_flight.targets)
            check_resolved(in_flight.unresolved, in_flight.unresolved_jobs)
        except LibraryConflict as exc:
            # A plain string, not the route's ``{"message", "conflicts"}``:
            # a retry has one line of UI to say this in.
            raise TaggingConflict(exc.message) from exc

    def cancel_job(self, job_id: str) -> Job:
        """Stop a queued or running job and end it in ``cancelled``.

        A ``queued`` job has no thread yet, so it goes straight to ``cancelled``
        here; :meth:`_process_job` re-reads the status after acquiring its
        concurrency slot, which is what stops a job that was cancelled while it
        was waiting for one from ever starting.

        A ``downloading`` or ``converting`` job is only signalled: its thread
        has files open and a child process running, and only it knows when both
        are gone.  The status therefore changes when the thread reports back in
        :meth:`_run_download_stage`, not here, so the queue never shows
        ``cancelled`` while an ffmpeg is still writing.

        A *download* in ``tagging`` is signalled the same way but ends
        differently: its FLAC is already in the library, so cancelling the
        *fix* cannot undo the download.  It finishes ``done`` with "tags not
        fixed: cancelled" in its detail rather than ``cancelled``, which would
        tell the user a track they can play was never downloaded.  A job still
        waiting for the tagging lock skips its lookup entirely; one whose
        MusicBrainz request is already open stops at the next checkpoint,
        because that request cannot be interrupted.

        A **bulk parent** is not signalled at all: it has no thread and no
        status of its own.  The cancel cascades to every child that has not
        finished -- queued children straight to ``cancelled``, running ones
        signalled -- and the parent follows them as each one reports back.

        A *manual tagging job* in ``tagging`` is the one case that does end in
        ``cancelled``: nothing was downloaded, so the word describes exactly
        what happened.  It is signalled through the same persisted flag, which
        its pass reads before each lookup and before each write -- so the
        tracks it had already rewritten stay rewritten and the rest are left
        alone.

        Terminal jobs are refused rather than silently accepted: a Cancel on a
        job that just finished is a stale UI, and answering "done" to it would
        leave the user believing a track was not filed when it was.

        Raises:
            JobNotFound: If no such job exists.
            QueueError: If the job is already done, errored or cancelled.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"Job {job_id!r} not found")
        if job.status not in _IN_FLIGHT:
            raise QueueError(
                f"Job {job_id!r} is in {job.status.value!r} status, "
                "only queued or running jobs can be cancelled"
            )

        if job.kind is JobKind.BULK:
            self._cancel_children(job)
            return job

        if job.status == JobStatus.QUEUED:
            self._finish_cancelled(job_id)
            return job

        logger.info("Job %s: cancel requested while %s", job_id, job.status.value)
        self._cancel_run(job_id)
        return job

    def _cancel_children(self, parent: Job) -> None:
        """Cascade a parent's cancel to every child that has not finished.

        Each child is cancelled exactly as it would be on its own: a queued one
        goes straight to ``cancelled`` here, a running one is only signalled and
        reaches ``cancelled`` when its thread has stopped ffmpeg and cleaned up.
        The parent's own status is not written here -- it is derived, and each
        child's transition refreshes it -- so a parent whose last child is still
        unwinding stays in flight until it really has stopped.
        """
        children = self.children_of(parent.id)
        logger.info(
            "Bulk job %s: cancelling %d child job(s) still in flight",
            parent.id,
            sum(1 for child in children if child.status in _IN_FLIGHT),
        )
        for child in children:
            if child.status not in _IN_FLIGHT:
                continue
            if child.status is JobStatus.QUEUED:
                self._finish_cancelled(child.id)
            else:
                self._cancel_run(child.id)
        self._refresh_parent(parent.id)

    def dismiss_job(self, job_id: str) -> None:
        """Forget an errored job entirely: no row, no queue entry, no history.

        Only ``error`` jobs can be dismissed, because they are the only ones the
        retention sweep never drops -- they sit in the queue until somebody says
        they have been seen.  Everything else either leaves on its own or is
        still running.

        Two bulk rules ride on the same check.  Dismissing a **parent** (whose
        derived status is ``error``, so at least one child failed) deletes the
        parent and every child, however far the rest of them got: the user is
        saying they are done with the whole request.  Dismissing the last
        failed **child** of a parent whose others are all ``done`` deletes the
        parent and those children too -- "all done or dismissed" is the
        ticket's rule -- because a ``done`` parent left behind is a queue row
        nobody can act on.

        Raises:
            JobNotFound: If no such job exists.
            QueueError: If the job is not in ``error``.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"Job {job_id!r} not found")
        if job.status != JobStatus.ERROR:
            raise QueueError(
                f"Job {job_id!r} is in {job.status.value!r} status, "
                "only errored jobs can be dismissed"
            )

        parent_id = job.parent_id
        self._delete_job(job_id)
        logger.info("Job %s dismissed", job_id)

        if parent_id is None:
            return
        parent = self._jobs.get(parent_id)
        if parent is None:
            return
        siblings = self.children_of(parent_id)
        if all(child.status is JobStatus.DONE for child in siblings):
            # "A parent whose children are all done or dismissed is deleted
            # with them" (ticket).  Dismissing the last failed child of a bulk
            # is the user saying they are finished with it, and leaving a
            # ``done`` parent behind would put a row in the queue that nothing
            # can ever remove but the retention sweep.
            logger.info(
                "Bulk job %s has nothing left but finished children; removing it",
                parent_id,
            )
            self._delete_job(parent_id)
            return
        self._refresh_parent(parent_id)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def restore_from_store(self) -> list[Job]:
        """Reload the queue from SQLite and resume where the last run stopped.

        Called once from the app lifespan, after the store is open and before
        the app serves requests, so ``add_job``'s duplicate check and
        ``GET /queue`` both see the restored rows immediately.

        ``queued`` and ``error`` rows are loaded as they are.  Rows still in
        ``downloading`` or ``converting`` with ``cancel_requested`` set were
        being cancelled when the process died: the restart is what their cancel
        was waiting for, so they are finished as ``cancelled``.
        The rest mean the process died mid-job: their scratch directory is
        removed, ``attempts`` and
        ``restart_attempts`` are incremented, and they go back to ``queued`` --
        or to ``error`` once a restart has interrupted them
        :data:`MAX_RESTART_ATTEMPTS` times, so a job that crashes the process
        cannot resume forever.

        Scratch directories left behind by jobs the store no longer knows about
        are removed too; nothing is running yet, so any of them is stale.

        A restored bulk parent also gets its **finished** children back, which
        ``load_active`` does not return: they are what the parent's "N of M" is
        counted from and what its queue row expands to, and a restart that
        dropped them would show a nearly finished bulk as if it had barely
        started.

        A ``tagging`` row is the exception to all of that: its FLAC is finished
        and filed, so there is nothing to download again.  It stays in
        ``tagging`` and is dispatched straight onto the tagging worker, without
        a download slot and without spending a restart attempt -- a restart
        during a metadata lookup damaged nothing, and the whole cost of getting
        it wrong is one more MusicBrainz query.  A row whose ``result_path`` is
        missing, or whose file has since gone, is finished ``done`` with "tags
        not fixed: file missing" instead: there is no file left to fix and no
        failure to report either.

        No SSE events are emitted: nothing is connected yet, and clients
        refetch ``GET /queue`` when their stream reconnects.

        Returns the restored jobs in ``created_at`` order.
        """
        if self._store is None:
            return []

        # load_active already orders by created_at; sorting again here keeps
        # the dispatch order a property of this method rather than of the query.
        restored = sorted(self._store.load_active(), key=lambda j: j.created_at)
        for job in restored:
            self._jobs[job.id] = job
            # A bulk parent has no work of its own: its stored status is the
            # last value derived from its children, and re-deriving it below is
            # the whole of its recovery.  Running it through the interrupted
            # path would remove a scratch directory that never existed and,
            # worse, re-queue and dispatch the *collection* URL as a download.
            if job.kind is JobKind.BULK:
                continue
            if job.status in _INTERRUPTED:
                self._recover_interrupted(job)

        # ``load_active`` skips ``done``/``cancelled`` rows, which for a bulk
        # parent is most of what it is made of: a parent halfway through 50
        # tracks has 20 finished children the query left behind, and without
        # them the restored parent would say "0 of 30".  They are loaded here
        # and put straight into the dict, but deliberately kept out of
        # ``restored`` -- they are finished, so there is nothing to recover, to
        # dispatch, or to count in the log line.  ``setdefault`` because an
        # active child is already in the dict with its *recovered* status, and
        # the stored row would undo that.
        children = self._store.load_children_of(
            [job.id for job in restored if job.kind is JobKind.BULK]
        )
        for child in children:
            self._jobs.setdefault(child.id, child)

        remove_orphan_temp_dirs({job.id for job in restored})

        # The user's original queue order is preserved across the restart.
        for job in restored:
            if job.kind is JobKind.BULK:
                continue
            if job.kind is JobKind.TAGGING:
                # A manual tagging job has no download half at all: whichever
                # of the two in-flight states it was in, what it needs is the
                # tagging worker and a fresh pass.
                if job.status in (JobStatus.QUEUED, JobStatus.TAGGING):
                    self._dispatch_tagging_job(job.id)
            elif job.status == JobStatus.QUEUED:
                self._dispatch(job.id)
            elif job.status == JobStatus.TAGGING:
                self._dispatch_tagging(job.id)

        # After every child is back in the dict with its recovered status, so
        # each parent derives from what will actually run.  A parent whose
        # children have all been swept derives to ``done`` and leaves the queue
        # view; no events, because nothing is connected yet and clients refetch
        # ``GET /queue`` when their stream reconnects.
        for job in restored:
            if job.kind is JobKind.BULK:
                self._refresh_parent(job.id, emit=False)

        if restored:
            logger.info(
                "Restored %d job(s) from the store (%d re-queued)",
                len(restored),
                sum(1 for job in restored if job.status == JobStatus.QUEUED),
            )
        return restored

    def _recover_interrupted(self, job: Job) -> None:
        """Re-queue *job*, or give up on it after too many interruptions.

        A job the user had already asked to stop is finished as ``cancelled``
        instead: the restart did what the cancel was waiting for the thread to
        do, so re-queuing it would resurrect a download nobody wants and the
        restart is not the job's fault either, so it costs no attempt.

        A job interrupted in ``tagging`` is not re-queued at all; see
        :meth:`_recover_tagging` for a download's tag stage and
        :meth:`_recover_tagging_job` for a manual tagging job.
        """
        if job.kind is JobKind.TAGGING:
            self._recover_tagging_job(job)
            return
        remove_job_temp_dir(job.id)
        if job.status is JobStatus.TAGGING:
            self._recover_tagging(job)
            return
        if job.cancel_requested:
            logger.info(
                "Job %s was being cancelled when the process stopped, "
                "finishing it as cancelled",
                job.id,
            )
            job.status = JobStatus.CANCELLED
            job.error = None
            job.progress = 0.0
            self._persist(job)
            return
        job.attempts += 1
        job.restart_attempts += 1
        if job.restart_attempts >= MAX_RESTART_ATTEMPTS:
            logger.warning(
                "Job %s was interrupted by a restart %d times, marking it failed",
                job.id,
                job.restart_attempts,
            )
            job.status = JobStatus.ERROR
            job.error = RESTART_GIVE_UP_MESSAGE
        else:
            logger.info(
                "Job %s was interrupted mid-%s, re-queuing (restart attempt %d)",
                job.id,
                job.status.value,
                job.restart_attempts,
            )
            job.status = JobStatus.QUEUED
            job.error = None
            job.progress = 0.0
            # ``target_dir`` and ``target_guessed`` are kept, as they are on a
            # manual retry: the next run
            # resolves from the same inputs and ``on_target`` overwrites it
            # before anything is created, while dropping it would make the job
            # unresolved and block every move until it ran.
        job.finished_at = None
        self._persist(job)

    def _recover_tagging(self, job: Job) -> None:
        """Decide what a row interrupted mid-tag-fix does now.

        Three outcomes, and none of them is a download:

        * the user had asked to cancel -- ``done`` with "tags not fixed:
          cancelled", the same ending a cancel gets while the process is up.
          Not ``cancelled``: the track is in the library either way, and a
          restart is not the moment to start claiming otherwise;
        * no ``result_path``, or the file is no longer where it said -- ``done``
          with "tags not fixed: file missing".  Somebody moved or deleted the
          track while the process was down, and there is nothing left to fix;
        * anything else -- left in ``tagging`` for :meth:`restore_from_store`
          to dispatch onto the tagging worker.

        ``restart_attempts`` is deliberately not incremented.  It exists to
        stop a job that crashes the process from resuming forever, and a job
        that reaches this point has already finished everything that touches
        yt-dlp, ffmpeg and the filesystem; re-running its lookup reads a file
        and, at most, rewrites two tags.
        """
        if job.cancel_requested:
            job.status = JobStatus.DONE
            job.detail = NOTE_CANCELLED
            job.error = None
            job.progress = 0.0
            self._persist(job)
            logger.info(
                "Job %s was being cancelled during its tag fix, finishing it "
                "as done with %r",
                job.id,
                NOTE_CANCELLED,
            )
            return

        root = Path(os.environ.get("DOWNLOAD_PATH") or DEFAULT_DOWNLOAD_PATH)
        if not job.result_path or not (root / job.result_path).exists():
            job.status = JobStatus.DONE
            job.detail = NOTE_FILE_MISSING
            job.error = None
            job.progress = 0.0
            self._persist(job)
            logger.info(
                "Job %s was tagging %s, which is no longer there; finishing it "
                "as done",
                job.id,
                job.result_path,
            )
            return

        logger.info(
            "Job %s was interrupted mid-tagging; re-running only the tag fix",
            job.id,
        )

    def _recover_tagging_job(self, job: Job) -> None:
        """Decide what a *manual* tagging job interrupted by a restart does now.

        Nothing about a match is ever stored (metadata ticket), so there is no
        half-finished pass to resume: the job simply runs again from the top,
        and the whole cost of the restart is a repeated MusicBrainz query.  It
        is therefore left in ``tagging`` for :meth:`restore_from_store` to hand
        back to the tagging worker.

        ``restart_attempts`` is deliberately not spent, exactly as it is not
        for a download's tag stage: the budget exists to stop a job that
        crashes the process from resuming forever, and a pass that reads tags
        and writes two of them is not that job.

        Two endings instead of a re-run:

        * the user had asked to cancel -- ``cancelled``.  Unlike a download's
          tag stage this really is a cancellation: no file was downloaded, so
          there is nothing the word would misrepresent;
        * the path is gone -- ``error`` with "file missing".  A manual job that
          did not do what it was asked has failed (ticket), and the user gets
          the Dismiss that goes with it.
        """
        job.progress_done = None
        job.progress_total = None
        if job.cancel_requested:
            job.status = JobStatus.CANCELLED
            job.error = None
            self._persist(job)
            logger.info(
                "Tagging job %s was being cancelled when the process stopped, "
                "finishing it as cancelled",
                job.id,
            )
            return

        root = Path(os.environ.get("DOWNLOAD_PATH") or DEFAULT_DOWNLOAD_PATH)
        if not job.path or not (root / job.path).exists():
            job.status = JobStatus.ERROR
            job.error = NOTE_FILE_MISSING
            self._persist(job)
            logger.info(
                "Tagging job %s was working on %s, which is no longer there",
                job.id,
                job.path,
            )
            return

        job.status = JobStatus.TAGGING
        logger.info(
            "Tagging job %s was interrupted by a restart; running its pass again",
            job.id,
        )

    def _dispatch_tagging_job(self, job_id: str) -> None:
        """Start a manual tagging job's pass as a tracked task."""
        self._track(job_id, self._run_tagging_job(job_id), "tagging job")

    def _dispatch_tagging(self, job_id: str) -> None:
        """Start the tagging stage alone, for a job restored in ``tagging``.

        Same task bookkeeping as :meth:`_dispatch`.  What it deliberately does
        not do is take a download slot: there is nothing left to download.
        """
        self._track(job_id, self._run_tagging_stage(job_id), "tagging")

    def sweep(self) -> int:
        """Drop done and cancelled jobs older than :data:`RETENTION_DAYS`.

        Run at boot and once a day thereafter.  Errored jobs are never swept --
        they stay until the user dismisses them.

        The table is the authority here: it also holds finished rows from
        earlier runs, which ``load_active`` did not reload into the dict.  Every
        id the store reports as pruned is dropped from the dict as well, so the
        dict never keeps a job the table has forgotten.

        Only top-level jobs are candidates.  A bulk parent's children leave
        with the parent and never on their own: a parent that is still working
        through a 200-track collection has done children older than the cutoff,
        and reaping those would rewrite its "N of M" and eventually retire the
        live parent as ``done`` with nothing under it.

        Returns the number of top-level jobs removed; the children that went
        with them are logged.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

        if self._store is not None:
            removed = self._store.prune_terminal(cutoff)
        else:
            # Memory-only mode (unit tests): apply the same rule to the dict.
            removed = [
                job_id
                for job_id, job in list(self._jobs.items())
                if job.parent_id is None
                and job.status in _SWEEPABLE
                and (job.finished_at or job.updated_at) < cutoff
            ]

        for job_id in removed:
            self._jobs.pop(job_id, None)
            self._active_runs.pop(job_id, None)
        # A pruned parent takes its children with it in the table (ON DELETE
        # CASCADE); the in-memory mirror has no cascade, so a child whose
        # parent has just gone is dropped here.  This is the only way a child
        # ever leaves the sweep, which is deliberate -- see the docstring.
        orphans = [
            job_id
            for job_id, job in list(self._jobs.items())
            if job.parent_id and job.parent_id not in self._jobs
        ]
        for job_id in orphans:
            self._jobs.pop(job_id, None)
            self._active_runs.pop(job_id, None)
        if removed:
            logger.info(
                "Retention sweep dropped %d job(s) and %d child job(s)",
                len(removed),
                len(orphans),
            )
        return len(removed)

    # ------------------------------------------------------------------
    # Internal processing
    # ------------------------------------------------------------------

    def _dispatch(self, job_id: str) -> None:
        """Start :meth:`_process_job` for *job_id* as a tracked task."""
        self._track(job_id, self._process_job(job_id), "processing")

    def _track(self, job_id: str, coro, what: str) -> None:
        """Run *coro* for *job_id* as a task this queue holds on to.

        asyncio keeps only a weak reference to a running task, so a task nobody
        holds can be garbage-collected mid-await and the job would silently stop
        moving.  The done callback is the other half: an exception that escaped
        the coroutine is otherwise reported only when the task object is
        finalised, in a message that does not say which job it was -- *what*
        names the stage in that message.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _done(finished: asyncio.Task) -> None:
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                logger.error(
                    "Job %s: its %s task raised %r", job_id, what, error,
                    exc_info=error,
                )

        task.add_done_callback(_done)

    async def _process_job(self, job_id: str) -> None:
        """Run a job's two stages: the download, then the tag fix.

        They are separate methods because they hold different things.  The
        download stage owns a concurrency slot for as long as it runs; the
        tagging stage owns the single tagging lock and no slot at all, so the
        next download starts the moment this one's file is filed rather than
        waiting behind a MusicBrainz lookup.

        The tag fix only runs when the download stage says the job has a file
        to fix; every other ending (error, cancel, timeout, a file the queue
        could not express relative to ``DOWNLOAD_PATH``) is already terminal.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return

        if await self._run_download_stage(job_id):
            await self._run_tagging_stage(job_id)

    async def _run_download_stage(self, job_id: str) -> bool:
        """Download the track inside a concurrency slot.

        Returns ``True`` when the job is now in ``tagging`` with a file waiting
        for the fix, and ``False`` when it has already reached a terminal
        status here.
        """
        # Wait for a concurrency slot (job stays QUEUED while waiting)
        async with self._semaphore:
            # Re-fetch: the job may have been pruned or changed while waiting
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED:
                return False

            # The run's stop button exists before the job is advertised as
            # running, so a Cancel that arrives in the same tick as the status
            # change has something to signal.  Registering it after the
            # transition would leave a window in which Cancel silently did
            # nothing and the job kept downloading.
            run = _ActiveRun()
            self._active_runs[job_id] = run

            # ---- downloading ----
            self._update_status(job_id, JobStatus.DOWNLOADING)

            try:
                await asyncio.wait_for(
                    self._run_download(job_id, run),
                    timeout=self._timeout,
                )

                # The thread has exited without the loop disowning it, so the
                # track it filed is the job's result.  Recorded before the DONE
                # transition, whose _persist writes the row before the SSE event
                # a client would follow to read it.
                with run.lock:
                    filed = run.filed

                # Nothing reads the entry after a successful download: `retry`
                # only inspects it for an errored job, and a cancel arriving
                # during ``tagging`` is carried by the persisted flag, not by a
                # thread to signal.  The failure paths below keep theirs -- a
                # timed-out or errored job's thread may still be unwinding, and
                # that is exactly what `retry` checks.
                self._active_runs.pop(job_id, None)
                if filed is not None:
                    self._record_result_path(job, filed.path)

                # ---- tagging, or straight to done ----
                # (converting is reported by the downloader)
                #
                # A job with a ``result_path`` has a FLAC in the library and
                # goes on to the tag fix; one without has nothing to fix -- the
                # file landed outside ``DOWNLOAD_PATH``, or the thread never
                # reported one -- and is finished here.
                next_status = (
                    JobStatus.TAGGING if job.result_path else JobStatus.DONE
                )
                self._update_status(job_id, next_status)

                # The library gained a file.  Emitted after the transition,
                # whose _persist wrote the row first, so a client that refetches
                # on this event never reads a queue that is behind the event it
                # just got.  A job that lost the race to a cancel or a timeout
                # reached neither status and changed nothing.
                if job.status is next_status:
                    self.emit_library_changed(
                        [job.result_path] if job.result_path else [],
                        job_id=job_id,
                    )
                    return next_status is JobStatus.TAGGING

            except asyncio.TimeoutError:
                logger.warning("Job %s timed out after %ss", job_id, self._timeout)
                # Cancelling the token aborts yt-dlp at its next progress
                # callback and signals a running ffmpeg, so the thread stops
                # within a moment rather than holding a core until a conversion
                # happens to finish.
                self._cancel_run(job_id, timed_out=True)
                self._fail(job_id, f"Download timed out after {self._timeout} seconds")
                # The verdict is written, so the run is no longer ours: from
                # here on whatever it files is taken back out of the library,
                # by whichever side of the hand-off gets there second.
                await self._disown_run(job_id, run)
                # The job is failed, but its thread is still unwinding and still
                # holds a temp directory and possibly an ffmpeg.  Waiting for it
                # here, inside the semaphore, is what keeps the number of
                # download threads at max_concurrent instead of letting the next
                # job start alongside a dying one.  Bounded: a thread that
                # outlives the grace is logged and abandoned rather than allowed
                # to stall the queue.
                deadline = time.monotonic() + THREAD_DRAIN_GRACE_SECONDS
                while not run.finished.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(_DRAIN_POLL_SECONDS)
                if not run.finished.is_set():
                    logger.warning(
                        "Job %s: its download thread was still running %ss after "
                        "the timeout; releasing its slot anyway",
                        job_id,
                        THREAD_DRAIN_GRACE_SECONDS,
                    )
                return False

            except DownloadError as exc:
                if run.cancel.is_set() and not run.disowned:
                    # The thread stopped because the user asked it to.  The
                    # downloader guarantees nothing was left in the library, so
                    # this is a clean cancellation, not a failure.
                    logger.info("Job %s stopped after a cancel request", job_id)
                    self._finish_cancelled(job_id)
                else:
                    logger.warning("Job %s failed: %s", job_id, exc)
                    self._fail(job_id, str(exc))
                await self._disown_run(job_id, run)
                return False

            except Exception as exc:
                logger.exception("Job %s encountered unexpected error", job_id)
                self._fail(job_id, f"Unexpected error: {exc}")
                await self._disown_run(job_id, run)
                return False

        # The success path fell through without reaching either status: the job
        # was cancelled or failed from under it, and whoever did that already
        # wrote its verdict.
        return False

    async def _run_download(self, job_id: str, run: _ActiveRun) -> None:
        """Run the synchronous ``download_audio`` call in a thread executor
        so it doesn't block the event loop.

        *run* is created by the caller, before the job is advertised as
        running, and is what a Cancel or a timeout signals; a retry refuses to
        start while its ``finished`` event is still clear.
        """
        job = self._jobs[job_id]
        last_whole_percent = -1

        # Both callbacks run on the download thread, which the timeout path does
        # not wait for before writing the job's verdict, so each starts by
        # checking that the job it is reporting on is still running.  The cancel
        # flag alone is not enough: it is read a moment before the callback acts
        # on it, and a job can reach a terminal status in between.
        def on_progress(percentage: float) -> None:
            nonlocal last_whole_percent
            if job.status in _TERMINAL:
                return  # the job is over; nothing it reports now is news
            if run.cancel.is_set():
                return  # job is stopping; don't resurrect its progress
            job.progress = percentage
            # yt-dlp calls this per chunk; only emit when the visible
            # percentage changes to keep SSE traffic sane.
            whole = int(percentage)
            if whole == last_whole_percent:
                return
            last_whole_percent = whole
            self._emit_event("progress", job)

        def on_phase(phase: str) -> None:
            if job.status in _TERMINAL:
                # A late `metadata` would re-stamp finished_at on a job that has
                # already ended; a late `converting` is dropped in
                # _update_status too, but there is no reason to write a row to
                # find that out.
                return
            if run.cancel.is_set():
                return
            if phase == "converting":
                self._update_status(job_id, JobStatus.CONVERTING)
            elif phase == "metadata":
                # The downloader has just backfilled title/duration/thumbnail
                # on the job; those are stored columns, so write before emit.
                self._persist(job)
                self._emit_event("metadata", job)

        def on_target(target_dir: str) -> None:
            """Record the folder this run has resolved as its destination.

            Persisted but not announced: ``target_dir`` is a stored column the
            in-flight guard reads, and nothing in the UI shows it, so an SSE
            event would be noise.  Same shape as the ``metadata`` phase hook --
            write the row, say nothing.

            ``target_guessed`` clears with it: this is the download thread's
            own answer, resolved from yt-dlp's metadata, so the folder is where
            the track is really going and no longer the submit-time fallback.
            """
            if job.status in _TERMINAL:
                return
            # The order of these two is load-bearing.  This runs on the
            # download thread while the event loop may be inside
            # ``in_flight_library_targets`` for a move or a delete, and that
            # reader takes ``target_guessed is False`` as its licence to
            # believe ``target_dir``.  Writing the real folder first means the
            # worst a reader can see is the guess it already distrusted;
            # clearing the flag first would offer it the *old* guessed folder
            # as a resolved answer, and the guard would then protect a folder
            # the download is not going to write into while leaving the one it
            # is unguarded.
            job.target_dir = target_dir
            job.target_guessed = False
            self._persist(job)

        def note_filed(track: FiledTrack) -> None:
            """Publish the move to the event loop, or undo it if it is too late.

            The loop reads ``filed`` under the same lock it sets ``disowned``
            in, so seeing ``disowned`` here means the job already has a final
            status the loop reached without this track -- an error row from the
            timeout path.  Leaving the file would mean a track in the library
            that no queue entry admits to and that the user was told had failed,
            so this thread, which is the only one that knows about it, removes
            it again.
            """
            with run.lock:
                run.filed = track
                disowned = run.disowned
            if disowned:
                logger.warning(
                    "Job %s filed %s after the queue had given up on it; taking "
                    "it back out of the library",
                    job_id,
                    track.path,
                )
                unfile_track(track)

        def runner() -> None:
            try:
                if run.cancel.is_set():
                    # Cancelled between the status change and this thread being
                    # scheduled: nothing has been fetched, so there is nothing
                    # to unwind beyond reporting it.
                    raise DownloadError("Download cancelled")
                download_audio(
                    job,
                    on_progress,
                    cancel=run.cancel,
                    on_phase=on_phase,
                    on_filed=note_filed,
                    on_target=on_target,
                )
            finally:
                # Cleanup belongs to the thread that owns the temp directory:
                # it is the only one that knows yt-dlp and ffmpeg are done with
                # it.  This is the `finally` that guarantees no partial, .part
                # or temp file survives a cancel, a timeout or a crash.
                remove_job_temp_dir(job_id)
                # The entry deliberately outlives the thread: _process_job reads
                # `cancel`/`disowned` off it to tell a user's Cancel from a
                # genuine failure, and it is the exception's unwinding that gets
                # it there, i.e. after this `finally`.  A retry replaces the
                # entry and the retention sweep and dismiss drop it, so it lives
                # exactly as long as the job does.
                run.finished.set()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, runner)

    # ------------------------------------------------------------------
    # Tagging
    # ------------------------------------------------------------------

    async def _run_tagging_stage(self, job_id: str) -> None:
        """Fix the job's tags on the single tagging worker, then finish it.

        Every path through here ends the job ``done``.  The track is in the
        library and playable before this stage starts, so nothing that happens
        to a *lookup* is worth telling the user their download failed -- a
        cancel, an unreachable MusicBrainz, a timeout and "no match" all end
        the same way, with the reason in ``Job.detail``.

        The second ``library_changed`` is emitted only when the fix actually
        wrote to the file.  A match whose tags were already correct changes no
        bytes, and the download's own event has already been sent, so a second
        one would re-read the library and wake the rescan hook for nothing.
        """
        job = self._jobs.get(job_id)
        if job is None or job.status is not JobStatus.TAGGING:
            return

        result = await self._tag_fix(job)

        # Written before the status change (it is part of the same row) and
        # therefore before the event a client would follow to read it.
        self._finish_tagged(job_id, result.note)

        if result.changed and job.result_path:
            self.emit_library_changed([job.result_path], job_id=job_id)

    async def _tag_fix(self, job: Job) -> TagFixResult:
        """Run one tag fix for *job*, holding the tagging lock while it runs.

        Cancellation has three checkpoints, because a MusicBrainz request in
        flight cannot be interrupted: before the lock (a job still queued
        behind another one's lookup skips its fix entirely), after acquiring it,
        and inside :func:`~app.tagger.fix_track` between the lookup and the
        write.  A cancel that arrives while the HTTP request is open takes
        effect when that request returns -- or when this method's own timeout
        fires, whichever comes first.

        The fix runs on its own daemon thread (:meth:`_submit_tag_fix`), one
        at a time, and the last of those three checkpoints also covers the job leaving
        ``tagging``: a lookup this method has already given up on returns to a
        job that is ``done``, and must not rewrite the tags of a track whose
        verdict has been written and whose ``library_changed`` has been sent.

        Every path returns a :class:`~app.tagger.TagFixResult`, including one
        the fix never anticipated, because an exception escaping here would
        skip :meth:`_finish_tagged` and strand the job in ``tagging`` -- and
        ``tagging`` is recovered on restart (:meth:`_recover_tagging`) without
        spending a restart attempt, so a deterministic failure would raise
        again on every restart, forever.  ``Exception`` and not
        ``BaseException``: a cancelled tagging task must still unwind.
        """
        if not job.result_path:
            return TagFixResult(note=NOTE_FILE_MISSING)
        root = Path(os.environ.get("DOWNLOAD_PATH") or DEFAULT_DOWNLOAD_PATH)
        path = root / job.result_path

        # The artist folder is the library's own answer to "whose track is
        # this", and the match bar checks the MusicBrainz credit against it.  A
        # loose file directly under the root has no artist folder and so no
        # artist to check against; fix_track then finds nothing that clears the
        # bar, which is the right outcome.
        parts = job.result_path.split("/")
        folder_artist = parts[0] if len(parts) > 1 else None

        if job.cancel_requested:
            return TagFixResult(note=NOTE_CANCELLED)

        async with self._tagging_lock:
            if job.cancel_requested:
                return TagFixResult(note=NOTE_CANCELLED)

            if self._stuck_tag_fix is not None:
                # An earlier lookup is still holding the tagging thread.  This
                # job's fix could only queue behind it and time out too, so it
                # is given the same answer now instead of in a minute's time.
                logger.warning(
                    "Job %s: skipping its tag fix, an earlier lookup is still "
                    "holding the tagging thread",
                    job.id,
                )
                return TagFixResult(note=NOTE_TIMED_OUT)

            # ``should_cancel`` is polled inside fix_track between the lookup
            # and the write, and it is the guard against a late write: a thread
            # this method has abandoned finds the job no longer ``tagging`` and
            # returns without touching the file.
            if self._closed:
                # :meth:`close` has already run: the app is shutting down and
                # this job reached its fix a moment too late.  The track is in
                # the library, so the job is finished the way every other
                # lookup that did not happen finishes -- with the reason.
                logger.info(
                    "Job %s: the tagging worker is shut down, leaving its tags "
                    "as they are",
                    job.id,
                )
                return TagFixResult(note=NOTE_CANCELLED)

            pending = self._submit_tag_fix(
                path,
                folder_artist,
                lambda: (
                    job.cancel_requested or job.status is not JobStatus.TAGGING
                ),
            )
            try:
                return await asyncio.wait_for(
                    asyncio.wrap_future(pending),
                    timeout=self._tag_fix_timeout,
                )
            except asyncio.TimeoutError:
                # The thread is still inside urllib and will stay there until
                # the socket gives up; see DEFAULT_TAG_FIX_TIMEOUT_SECONDS.
                # Cancelling is kept for the vanishing case where the thread
                # has not reached ``set_running_or_notify_cancel`` yet; a fix
                # given a thread of its own has all but always started, so it
                # almost always returns False and the fix becomes the stuck
                # one below.
                pending.cancel()
                if not pending.done():
                    self._stuck_tag_fix = pending
                    pending.add_done_callback(self._release_stuck_tag_fix)
                logger.warning(
                    "Job %s: its tag fix did not finish within %ss",
                    job.id,
                    self._tag_fix_timeout,
                )
                return TagFixResult(note=NOTE_TIMED_OUT)
            except Exception:
                logger.exception("Job %s: its tag fix raised", job.id)
                return TagFixResult(note=NOTE_FAILED)

    def _submit_tag_fix(
        self,
        path: Path,
        folder_artist: str | None,
        should_cancel: Callable[[], bool],
    ) -> concurrent.futures.Future:
        """Run one :func:`~app.tagger.fix_track` on a thread of its own.

        Returns a real :class:`concurrent.futures.Future`, so the caller can
        wrap it for ``asyncio`` and hang a done callback off it exactly as it
        would an executor's.  What it is not is an executor: see
        :data:`DEFAULT_TAG_FIX_TIMEOUT_SECONDS` for why the thread has to be a
        daemon, and what that costs.

        The thread is created per fix rather than kept around, because it may
        never come back: an abandoned lookup keeps its own thread until the
        socket dies, and the next fix needs one that is free.  What bounds
        them to one at a time is the tagging lock plus the stuck-fix guard in
        :meth:`_tag_fix`, not a pool size.
        """
        return self._submit_tag_step(
            lambda: fix_track(path, folder_artist, should_cancel=should_cancel)
        )

    def _submit_tag_step(self, step: Callable[[], object]) -> concurrent.futures.Future:
        """Run one blocking tagging step on a daemon thread of its own.

        The general form of :meth:`_submit_tag_fix`: the album pass is a
        sequence of blocking steps -- a lookup per track, the release fetch,
        each write, the cover -- and every one of them has to be on a thread
        this loop can walk away from, for the reason spelled out at
        :data:`DEFAULT_TAG_FIX_TIMEOUT_SECONDS`.

        A thread per step rather than one thread for the whole pass, because
        the timeout is per step: one wedged lookup must not make the eleven
        tracks after it unreachable, and a step this queue has abandoned has to
        be able to keep its own thread until its socket dies.
        """
        pending: concurrent.futures.Future = concurrent.futures.Future()

        def runner() -> None:
            # False means the caller cancelled the future before this thread
            # was scheduled; it is already resolved, so there is nothing to do.
            if not pending.set_running_or_notify_cancel():
                return
            try:
                result = step()
            except BaseException as exc:  # noqa: BLE001 -- reported to the caller
                pending.set_exception(exc)
            else:
                pending.set_result(result)

        threading.Thread(target=runner, name="tag-fix", daemon=True).start()
        return pending

    def _release_stuck_tag_fix(self, finished: concurrent.futures.Future) -> None:
        """Note that the abandoned lookup *finished* has let the thread go.

        Runs on the tagging thread itself, as that call finally returns.  The
        identity check matters: by then a later fix may already have timed out
        in its turn, and it is that one -- not this one -- that is now holding
        the worker.
        """
        if self._stuck_tag_fix is finished:
            self._stuck_tag_fix = None

    def _finish_tagged(self, job_id: str, detail: str | None) -> None:
        """Move a tagging job to ``done``, carrying *detail* if the fix failed.

        ``detail`` and the status are set together so ``_update_status``'s
        persist writes them in one row: a client that refetches on the
        ``status_change`` finds the note already there.

        Logged at INFO when there is one.  A ``done`` job leaves the in-flight
        queue view immediately, so for anything but a very fast user the log is
        where "why is this track still called 'Song (Official Video)'" gets
        answered.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return
        if detail:
            logger.info("Job %s: %s", job_id, detail)
        job.detail = detail
        self._update_status(job_id, JobStatus.DONE)

    # ------------------------------------------------------------------
    # Manual tagging jobs (phase 9)
    # ------------------------------------------------------------------

    async def _run_tagging_job(self, job_id: str) -> None:
        """Run one manual tagging job from ``queued`` to its final status.

        A tagging job is a download job with the download taken out: no slot,
        no yt-dlp, no temp directory -- just the single tagging lock, which is
        this app's whole MusicBrainz rate limit, and a pass over one path.

        How it ends is the one place a manual job differs from the automatic
        fix a download carries.  A download always finishes ``done``, because
        its file is in the library whatever the lookup did.  A tagging job has
        no file to fall back on, so the metadata ticket's rule applies: a
        lookup that could not happen (MusicBrainz unreachable, a timeout, an
        unexpected failure, a file that could not be written) is an ``error``
        with a Retry and a Dismiss, while "no match" and a partial album are
        ``done`` with the reason in ``detail``.

        ``library_changed`` fires at the end of every run that got as far as
        the pass -- including one that failed or was cancelled partway, because
        the tracks written before it stopped really did change, and including
        one that changed nothing, because the rescan fires after any manual run
        (ticket).
        """
        job = self._jobs.get(job_id)
        if job is None or job.status not in (JobStatus.QUEUED, JobStatus.TAGGING):
            return

        if job.cancel_requested:
            # Cancelled while it waited its turn: nothing has been touched.
            self._finish_cancelled(job_id)
            return

        # ``tagging`` is set *inside* the lock, not here: the lock is this
        # app's whole MusicBrainz rate limit, so a second manual job can wait
        # behind the first for minutes, and a row that said "tagging" all that
        # while would be claiming work that has not started.  It stays
        # ``queued``, which is what it is.
        async with self._tagging_lock:
            if self._closed:
                # The app is on its way down.  The row is left in whichever
                # in-flight state it holds -- ``queued`` for a job that never
                # got its turn, ``tagging`` for one restored from an earlier
                # boot -- and ``restore_from_store`` re-dispatches both, so the
                # next start picks it up either way.  Finishing it here would
                # throw the user's request away instead.
                logger.info(
                    "Tagging job %s: the worker is shut down, leaving it for "
                    "the next start",
                    job_id,
                )
                return
            if job.cancel_requested:
                self._finish_cancelled(job_id)
                return
            if job.status not in (JobStatus.QUEUED, JobStatus.TAGGING):
                # Finished while it waited: ``_finish_cancelled`` writes
                # ``cancelled`` without setting ``cancel_requested``, so the
                # check above cannot see a job that was cancelled from the
                # queue while it sat here.  Without this the job would wake up,
                # run the whole pass and end ``done``.
                return
            self._update_status(job_id, JobStatus.TAGGING)
            await self._run_tagging_pass(job)

    async def _run_tagging_pass(self, job: Job) -> None:
        """Do the work of one tagging job and write its verdict.

        Split out from :meth:`_run_tagging_job` so the lock, the shutdown check
        and the cancel checkpoints stay readable above, and so every exit from
        the pass itself goes through one ``finally`` that emits
        ``library_changed``.
        """
        root = Path(os.environ.get("DOWNLOAD_PATH") or DEFAULT_DOWNLOAD_PATH)
        target = root / (job.path or "")
        # The artist folder is the library's own answer to "whose track is
        # this", and the match bar checks the MusicBrainz credit against it.
        folder_artist = (job.path or "").split("/")[0] or None

        changed: list[str] = []
        try:
            if not job.path or not target.exists():
                # Moved or deleted between the request and its turn on the
                # worker.  Nothing to fix, and a manual job that did not do
                # what it was asked has failed.
                self._fail(job.id, NOTE_FILE_MISSING)
                return

            if target.is_dir():
                result = await self._tag_album_job(job, target, folder_artist)
            else:
                result = await self._tag_track_job(job, target, folder_artist)

            changed = self._relative_paths(result.changed, root)
            if result.cancelled:
                self._finish_cancelled(job.id)
            else:
                self._finish_tagged(job.id, result.detail)
        except TagStepFailed as exc:
            # Whatever was written before the step that failed stays written;
            # there is no way to unwrite a tag that is not another write, and
            # the tags that did land are correct.
            self._fail(job.id, exc.note)
        except Exception:
            logger.exception("Tagging job %s raised", job.id)
            self._fail(job.id, NOTE_FAILED)
        finally:
            # An empty-ish list still says "re-read the library": the rescan
            # hook maps the job's own path to the folder to touch, which is the
            # honest answer when the pass stopped before it could say more.
            self.emit_library_changed(changed or [job.path or ""], job_id=job.id)

    async def _tag_track_job(
        self, job: Job, path: Path, folder_artist: str | None
    ) -> AlbumTagResult:
        """Redo the per-track fix for one file, as its own job.

        Exactly the fix a download runs automatically (phase 8), down to the
        same function and the same match bar -- what differs is only how the
        outcome is reported, which is :meth:`_run_tagging_pass`'s business.

        No progress counters.  One track is one step, and "0 of 1" then "1 of
        1" is a progress bar that says nothing the status does not: both
        counters stay ``None`` and the row shows the status alone.
        """
        outcome: TagFixResult = await self._run_tag_step(
            lambda: fix_track(
                path, folder_artist, should_cancel=lambda: job.cancel_requested
            )
        )

        if outcome.note in _TAGGING_FAILURES:
            raise TagStepFailed(outcome.note)
        if outcome.note == NOTE_CANCELLED:
            return AlbumTagResult(total=1, cancelled=True)
        return AlbumTagResult(
            total=1,
            matched=1 if outcome.matched else 0,
            changed=[path] if outcome.changed else [],
            detail=outcome.note,
        )

    async def _tag_album_job(
        self, job: Job, folder: Path, folder_artist: str | None
    ) -> AlbumTagResult:
        """Run the whole-folder pass, reporting N of M as it goes."""
        return await tag_album(
            folder,
            folder_artist,
            run=self._run_tag_step,
            on_progress=lambda done, total: self._set_tag_progress(job, done, total),
            should_cancel=lambda: job.cancel_requested,
        )

    async def _run_tag_step(self, step: Callable[[], object]):
        """Run one blocking tagging step, bounded by the tag-fix timeout.

        The hook :func:`~app.album_tagger.tag_album` is handed: it owns the
        thread, the timeout and the stuck-worker guard, so the pass itself
        knows nothing about either and can be tested without them.

        Every failure leaves here as a :class:`~app.album_tagger.TagStepFailed`
        carrying the note the job's ``error`` will show, because an exception
        escaping the pass would strand the job in ``tagging`` -- a status the
        next restart re-runs without spending a restart attempt, so a
        deterministic failure would raise again on every boot, forever.
        """
        if self._stuck_tag_fix is not None:
            # An earlier lookup is still holding a tagging thread and has not
            # come back; this step could only wait for the same socket.
            logger.warning(
                "Skipping a tagging step: an earlier lookup is still running"
            )
            raise TagStepFailed(NOTE_TIMED_OUT)

        pending = self._submit_tag_step(step)
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(pending), timeout=self._tag_fix_timeout
            )
        except asyncio.TimeoutError as exc:
            # The thread is still inside urllib and will stay there until the
            # socket gives up; see DEFAULT_TAG_FIX_TIMEOUT_SECONDS.
            pending.cancel()
            if not pending.done():
                self._stuck_tag_fix = pending
                pending.add_done_callback(self._release_stuck_tag_fix)
            logger.warning(
                "A tagging step did not finish within %ss", self._tag_fix_timeout
            )
            raise TagStepFailed(NOTE_TIMED_OUT) from exc
        except TagStepFailed:
            raise
        except Exception as exc:
            logger.exception("A tagging step raised")
            raise TagStepFailed(NOTE_FAILED) from exc

    def _set_tag_progress(self, job: Job, done: int, total: int) -> None:
        """Publish a tagging job's N of M, when it has moved.

        Memory-only and therefore never persisted: a restarted job re-runs its
        whole pass, so a stored count would describe work that is about to
        happen again.  The event is the ordinary ``progress`` one, carrying the
        same job snapshot every other event does.
        """
        if job.progress_done == done and job.progress_total == total:
            return
        job.progress_done = done
        job.progress_total = total
        if job.status in _TERMINAL:
            return
        self._emit_event("progress", job)

    def _relative_paths(self, paths: Iterable[Path], root: Path) -> list[str]:
        """*paths* as POSIX paths relative to the library root, skipping any
        that cannot be expressed that way."""
        relative: list[str] = []
        for path in paths:
            try:
                relative.append(Path(path).relative_to(root).as_posix())
            except ValueError:
                logger.warning(
                    "A tagging pass wrote %s, which is outside %s", path, root
                )
        return relative

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cancel_run(self, job_id: str, timed_out: bool = False) -> None:
        """Ask the run for *job_id* (if any) to stop.

        Returns immediately.  Cancelling the token raises out of yt-dlp's next
        progress callback and signals a running ffmpeg, but the thread still has
        to unwind and delete its scratch directory, so the job's final status is
        written when it reports back, not here.  A job in ``tagging`` has no
        thread to signal; for it, the persisted ``cancel_requested`` flag is the
        whole mechanism.
        """
        run = self._active_runs.get(job_id)
        job = self._jobs.get(job_id)
        if job is not None and not timed_out:
            # Written before the token is signalled, and before the thread can
            # report back: a restart in the seconds a cancel takes would
            # otherwise find a `downloading` row and re-queue a job the user
            # stopped.  Only a user's cancel sets it -- a timeout has already
            # failed the job, and marking that row cancel_requested would have
            # a restart finish it as `cancelled` instead of `error`.
            job.cancel_requested = True
            self._persist(job)
        # A job in ``tagging`` has no thread of its own -- the download's has
        # long exited, and after a restart there was never one -- so the flag
        # above *is* the cancel: the tagging stage reads it at each of its
        # checkpoints.
        if run is not None:
            run.cancel.cancel()

    async def _disown_run(self, job_id: str, run: _ActiveRun) -> None:
        """Give up ownership of *run* now that its job has a final status.

        Called from the event loop immediately after ``_fail`` or
        ``_finish_cancelled``, which is the only moment at which the loop and
        the run's thread can disagree about a track: the thread may have moved
        the file into the library in the instant before the verdict was written,
        or may be about to.  Setting the flag and reading ``filed`` in one
        critical section decides that race once -- whatever the thread had
        already filed is removed here, and anything it files afterwards is
        removed by :func:`note_filed`, which sees the flag.

        The unlink runs off the loop: it is a filesystem call on a path that may
        be a slow network mount, and no request should wait on it.
        """
        with run.lock:
            run.disowned = True
            track = run.filed
        if track is None:
            return
        logger.warning(
            "Job %s had already filed %s when the queue gave up on it; taking "
            "it back out of the library",
            job_id,
            track.path,
        )
        await asyncio.to_thread(unfile_track, track)

    def _finish_cancelled(self, job_id: str) -> None:
        """Move a job to CANCELLED and emit its status change.

        No ``error`` event and no error text: a cancellation is something the
        user did, not something that went wrong, and the job leaves the queue
        view either way.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return
        old_status = job.status.value
        job.status = JobStatus.CANCELLED
        job.error = None
        job.progress = 0.0
        self._persist(job)
        logger.info("Job %s: %s -> %s", job_id, old_status, JobStatus.CANCELLED.value)
        self._emit_event("status_change", job)
        self._refresh_parent(job.parent_id)

    def _record_result_path(self, job: Job, result: Path | str | None) -> None:
        """Remember where the finished file landed, relative to DOWNLOAD_PATH.

        Stored relative so the value survives a change of mount point, and as a
        POSIX path so it reads the same everywhere.  A file outside the
        configured root cannot be expressed that way; that is a configuration
        problem worth a log line, not a reason to fail a finished download.
        """
        if result is None:
            return
        root = Path(os.environ.get("DOWNLOAD_PATH") or DEFAULT_DOWNLOAD_PATH)
        try:
            job.result_path = Path(result).relative_to(root).as_posix()
        except ValueError:
            logger.warning(
                "Job %s finished at %s, which is outside DOWNLOAD_PATH %s; "
                "leaving result_path unset",
                job.id,
                result,
                root,
            )

    def _fail(self, job_id: str, message: str) -> None:
        """Move a job to ERROR with *message* and emit its events.

        Status and message are set together and written in one row, so a client
        that reads the table after seeing ``status_change`` already finds the
        error text there.  The event order (``status_change`` then ``error``)
        is unchanged.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return
        old_status = job.status.value
        job.status = JobStatus.ERROR
        job.error = message
        self._persist(job)
        logger.info("Job %s: %s -> %s", job_id, old_status, JobStatus.ERROR.value)
        self._emit_event("status_change", job)
        self._emit_event("error", job)
        self._refresh_parent(job.parent_id)

    def _persist(self, job: Job) -> None:
        """Stamp the job's timestamps and write it through to the store.

        Called from the event loop and from executor threads (yt-dlp's phase
        hook); the store serialises the SQLite side behind its own lock.

        Every caller must invoke this *before* ``_emit_event`` so the row is
        never behind the event a client just received.
        """
        job.updated_at = datetime.now(timezone.utc)
        if job.status in _TERMINAL:
            job.finished_at = job.updated_at
        if job.status in (JobStatus.DONE, JobStatus.ERROR):
            # A cancel that lost its race is not a fact about the finished job:
            # the track is in the library, or the job failed for its own reason,
            # and the flag would only tell a later reader the user had stopped
            # something that ran to the end.  A `cancelled` row keeps it, where
            # it is the record of why the job ended that way.
            job.cancel_requested = False
        if self._store is None:
            return
        try:
            self._store.upsert(job)
        except Exception:
            # A failed write must not take the download down with it: the job
            # simply will not survive a restart.
            logger.exception("Could not persist job %s", job.id)

    def _persist_many(self, jobs: list[Job]) -> None:
        """Stamp and write a whole set of jobs in one store transaction.

        The batch form of :meth:`_persist`, for :meth:`add_bulk_job`: 2000
        single-statement autocommit writes are 2000 commits on the event loop,
        and one ``executemany`` is one.  Same stamping rules, same "a failed
        write must not take the submission down" contract.

        Anything these rows reference by foreign key -- a bulk parent -- must
        already be persisted.
        """
        if not jobs:
            return
        now = datetime.now(timezone.utc)
        for job in jobs:
            job.updated_at = now
            if job.status in _TERMINAL:
                job.finished_at = now
            if job.status in (JobStatus.DONE, JobStatus.ERROR):
                job.cancel_requested = False
        if self._store is None:
            return
        try:
            self._store.upsert_many(jobs)
        except Exception:
            logger.exception("Could not persist %d job(s)", len(jobs))

    def _update_status(self, job_id: str, status: JobStatus) -> None:
        """Update a job's status, persist it, and emit a status_change event.

        A transition to the status the job is already in is dropped.  Nothing
        reports a phase twice today, but the phase callbacks run on the download
        thread and this is one comparison against a duplicate row write and a
        duplicate SSE event for every client.

        A terminal status is absorbing here.  The phase callbacks run on the
        download thread and cannot check the status atomically, so the timeout
        path -- which fails the job without waiting for its thread -- leaves a
        window in which the thread reports ``converting`` a moment after the
        verdict was written.  Letting that through would overwrite the terminal
        status in memory and in the row, emit ``status_change converting``
        after ``error``, and strand the job in a running state that retry and
        dismiss both refuse until the next restart.

        The two legitimate exits from a terminal state -- :meth:`retry_job` and
        :meth:`_recover_interrupted` -- deliberately assign ``job.status``
        directly rather than come through here.
        """
        job = self._jobs.get(job_id)
        if job is None or job.status == status:
            return
        if job.status in _TERMINAL:
            logger.debug(
                "Job %s: ignoring late %s from its download thread; already %s",
                job_id,
                status.value,
                job.status.value,
            )
            return
        old_status = job.status.value
        job.status = status
        self._persist(job)
        logger.info("Job %s: %s -> %s", job_id, old_status, status.value)
        self._emit_event("status_change", job)
        self._refresh_parent(job.parent_id)

    def emit_library_changed(self, paths: list[str], job_id: str | None = None) -> None:
        """Tell every client that files under ``DOWNLOAD_PATH`` changed.

        *paths* are POSIX paths relative to ``DOWNLOAD_PATH`` -- the identity a
        library path travels as everywhere in this app.  An empty list still
        says "something changed, re-read the library", which is the honest
        answer when the file that was written could not be expressed relative
        to the configured root.

        *job_id* is the job responsible, when there is one.  Moves, deletes,
        restores, and manual tag writes have no job behind them and leave it
        None; the event is the same shape either way, which is why this is a
        public method rather than something folded into the download path.
        """
        if self._on_event is None:
            return
        event = SSEEvent(
            event="library_changed",
            job_id=job_id,
            data={"paths": list(paths)},
        )
        self._on_event(event)

    def _emit_event(self, event_type: str, job: Job) -> None:
        """Build an SSEEvent carrying a snapshot of the job and invoke the callback."""
        if self._on_event is None:
            return

        data: dict = {
            "status": job.status.value,
            "progress": job.progress,
            "title": job.title,
            "thumbnail_url": job.thumbnail_url,
            "duration": job.duration,
            "artist": job.artist,
            "album": job.album,
            # Always present, null for anything that does not count in units:
            # a client rendering "7 of 12" should not have to tell "no progress
            # yet" from "this kind of job has no N of M".
            "progress_done": job.progress_done,
            "progress_total": job.progress_total,
            # Always present, and ``parent_id`` null for anything standalone.
            # A client that has never seen a job -- a bulk child that started
            # while the queue view was showing only its parent -- can place the
            # event from these two alone instead of having to refetch.
            "kind": job.kind.value,
            "parent_id": job.parent_id,
        }
        if job.error:
            data["error"] = job.error
        if job.detail:
            # Only on `done` rows today ("tags not fixed: ..."), and absent
            # rather than null when there is nothing to say, like `error`.
            data["detail"] = job.detail

        event = SSEEvent(event=event_type, job_id=job.id, data=data)
        self._on_event(event)
