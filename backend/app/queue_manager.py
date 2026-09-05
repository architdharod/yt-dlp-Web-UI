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
from typing import Callable

from app.downloader import (
    FFMPEG_TERMINATE_GRACE_SECONDS,
    CancelToken,
    DownloadError,
    FiledTrack,
    download_audio,
    remove_job_temp_dir,
    remove_orphan_temp_dirs,
    unfile_track,
)
from app.file_organizer import DEFAULT_DOWNLOAD_PATH
from app.job_store import JobStore
from app.models import Job, JobKind, JobStatus, SSEEvent
from app.tagger import (
    NOTE_CANCELLED,
    NOTE_FAILED,
    NOTE_FILE_MISSING,
    NOTE_TIMED_OUT,
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
    """

    targets: list[str]
    unresolved: int
    # One short label per unresolved job -- its title, or its id while the
    # probe has not returned one -- so the 409 can name the download the user
    # is being asked to wait for instead of leaving them to guess at a queue
    # that may be several jobs deep.
    unresolved_jobs: list[str] = field(default_factory=list)


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
        for job in self._jobs.values():
            if job.url == url and job.status in _IN_FLIGHT:
                return job
        return None

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

        The library's move and delete routes refuse to touch a folder that
        appears here: a download that lands in a folder renamed out from under
        it leaves a track the user cannot find.
        """
        targets: list[str] = []
        unresolved_jobs: list[str] = []
        for job in self._jobs.values():
            if job.status not in _IN_FLIGHT:
                continue
            # Only downloads create folders in the library.  A bulk parent and
            # a standalone tagging job never write a new path, so neither is a
            # destination anybody has to wait for -- and a tagging *job* has no
            # ``target_dir`` at all, which would otherwise make every tagging
            # run refuse every move.
            #
            # A download in the ``tagging`` *status* is a different thing and
            # is deliberately still guarded here: it keeps its kind and its
            # resolved ``target_dir``, and it is about to rewrite tags in a
            # file inside that folder, so a move or a delete of the folder
            # while the fix is running would have the tagger writing to a path
            # that no longer exists.
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

        Raises:
            QueueError: If the job does not exist, is not in ERROR status,
                or its previous download thread has not exited yet.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"Job {job_id!r} not found")
        if job.status != JobStatus.ERROR:
            raise QueueError(
                f"Job {job_id!r} is in {job.status.value!r} status, only ERROR jobs can be retried"
            )
        run = self._active_runs.get(job_id)
        if run is not None and not run.finished.is_set():
            raise QueueError(
                f"Job {job_id!r} is still shutting down its previous attempt; retry in a moment"
            )

        job.status = JobStatus.QUEUED
        job.error = None
        # The note from the previous run's tag fix describes a run that is
        # about to be replaced; leaving it would have the new run's outcome
        # read against the old run's reason.
        job.detail = None
        job.progress = 0.0
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
        self._dispatch(job.id)
        return job

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

        A ``tagging`` job is signalled the same way but ends differently: its
        FLAC is already in the library, so cancelling the *fix* cannot undo the
        download.  It finishes ``done`` with "tags not fixed: cancelled" in its
        detail rather than ``cancelled``, which would tell the user a track
        they can play was never downloaded.  A job still waiting for the
        tagging lock skips its lookup entirely; one whose MusicBrainz request
        is already open stops at the next checkpoint, because that request
        cannot be interrupted.

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

        if job.status == JobStatus.QUEUED:
            self._finish_cancelled(job_id)
            return job

        logger.info("Job %s: cancel requested while %s", job_id, job.status.value)
        self._cancel_run(job_id)
        return job

    def dismiss_job(self, job_id: str) -> None:
        """Forget an errored job entirely: no row, no queue entry, no history.

        Only ``error`` jobs can be dismissed, because they are the only ones the
        retention sweep never drops -- they sit in the queue until somebody says
        they have been seen.  Everything else either leaves on its own or is
        still running.

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
        logger.info("Job %s dismissed", job_id)

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
            if job.status in _INTERRUPTED:
                self._recover_interrupted(job)

        remove_orphan_temp_dirs({job.id for job in restored})

        # The user's original queue order is preserved across the restart.
        for job in restored:
            if job.status == JobStatus.QUEUED:
                self._dispatch(job.id)
            elif job.status == JobStatus.TAGGING:
                self._dispatch_tagging(job.id)

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
        :meth:`_recover_tagging`.
        """
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

        Returns the number of jobs removed.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

        if self._store is not None:
            removed = self._store.prune_terminal(cutoff)
        else:
            # Memory-only mode (unit tests): apply the same rule to the dict.
            removed = [
                job_id
                for job_id, job in self._jobs.items()
                if job.status in _SWEEPABLE
                and (job.finished_at or job.updated_at) < cutoff
            ]

        for job_id in removed:
            self._jobs.pop(job_id, None)
            self._active_runs.pop(job_id, None)
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
        pending: concurrent.futures.Future = concurrent.futures.Future()

        def runner() -> None:
            # False means the caller cancelled the future before this thread
            # was scheduled; it is already resolved, so there is nothing to do.
            if not pending.set_running_or_notify_cancel():
                return
            try:
                result = fix_track(path, folder_artist, should_cancel=should_cancel)
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
        }
        if job.error:
            data["error"] = job.error
        if job.detail:
            # Only on `done` rows today ("tags not fixed: ..."), and absent
            # rather than null when there is nothing to say, like `error`.
            data["detail"] = job.detail

        event = SSEEvent(event=event_type, job_id=job.id, data=data)
        self._on_event(event)
