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
ffmpeg.  ``tagging`` is part of the persisted status vocabulary from the first
schema version but nothing enters it yet -- the tagging worker arrives in phase
8; cancel already treats it like ``converting`` so it will not need a special
case then.

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
from app.models import Job, JobStatus, SSEEvent

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT_DOWNLOADS = 2
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 900  # 15 minutes

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
    """

    def __init__(
        self,
        max_concurrent: int | None = None,
        timeout: int | None = None,
        on_event: Callable[[SSEEvent], None] | None = None,
        store: JobStore | None = None,
    ) -> None:
        if max_concurrent is None:
            max_concurrent = _env_int("MAX_CONCURRENT_DOWNLOADS", DEFAULT_MAX_CONCURRENT_DOWNLOADS)
        if timeout is None:
            timeout = _env_int("DOWNLOAD_TIMEOUT_SECONDS", DEFAULT_DOWNLOAD_TIMEOUT_SECONDS)

        self._max_concurrent = max_concurrent
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)
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

        A ``downloading``, ``converting`` or ``tagging`` job is only signalled:
        its thread has files open and a child process running, and only it knows
        when both are gone.  The status therefore changes when the thread
        reports back in :meth:`_process_job`, not here, so the queue never shows
        ``cancelled`` while an ffmpeg is still writing.  ``tagging`` is handled
        with the others so phase 8 does not have to revisit this.

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
        ``downloading``, ``converting`` or ``tagging`` with ``cancel_requested``
        set were being cancelled when the process died: the restart is what
        their cancel was waiting for, so they are finished as ``cancelled``.
        The rest mean the process died mid-job: their scratch directory is
        removed, ``attempts`` and
        ``restart_attempts`` are incremented, and they go back to ``queued`` --
        or to ``error`` once a restart has interrupted them
        :data:`MAX_RESTART_ATTEMPTS` times, so a job that crashes the process
        cannot resume forever.

        Scratch directories left behind by jobs the store no longer knows about
        are removed too; nothing is running yet, so any of them is stale.

        A ``tagging`` row is re-queued from the start here.  Once the tagging
        worker exists (phase 8) the FLAC is already complete at that point and
        only the tag fix should be re-run; today there is no tagging pipeline
        to re-enter, so a full re-download is the correct conservative choice.

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
        """
        remove_job_temp_dir(job.id)
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
        job.finished_at = None
        self._persist(job)

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
        """Start :meth:`_process_job` for *job_id* as a tracked task.

        asyncio keeps only a weak reference to a running task, so a task nobody
        holds can be garbage-collected mid-await and the job would silently stop
        moving.  The done callback is the other half: an exception that escaped
        ``_process_job`` is otherwise reported only when the task object is
        finalised, in a message that does not say which job it was.
        """
        task = asyncio.create_task(self._process_job(job_id))
        self._tasks.add(task)

        def _done(finished: asyncio.Task) -> None:
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                logger.error(
                    "Job %s: its processing task raised %r", job_id, error,
                    exc_info=error,
                )

        task.add_done_callback(_done)

    async def _process_job(self, job_id: str) -> None:
        """Acquire a concurrency slot, run the download with timeout,
        and transition the job through the state machine.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return

        # Wait for a concurrency slot (job stays QUEUED while waiting)
        async with self._semaphore:
            # Re-fetch: the job may have been pruned or changed while waiting
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED:
                return

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
                if filed is not None:
                    self._record_result_path(job, filed.path)

                # ---- done ---- (converting is reported by the downloader)
                self._update_status(job_id, JobStatus.DONE)

                # The library gained a file.  Emitted after the DONE
                # transition, whose _persist wrote the row first, so a client
                # that refetches on this event never reads a queue that still
                # calls the job in-flight.  A job that lost the race to a
                # cancel or a timeout never reached DONE and changed nothing.
                if job.status is JobStatus.DONE:
                    self.emit_library_changed(
                        [job.result_path] if job.result_path else [],
                        job_id=job_id,
                    )

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

            except Exception as exc:
                logger.exception("Job %s encountered unexpected error", job_id)
                self._fail(job_id, f"Unexpected error: {exc}")
                await self._disown_run(job_id, run)

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
    # Helpers
    # ------------------------------------------------------------------

    def _cancel_run(self, job_id: str, timed_out: bool = False) -> None:
        """Ask the download thread for *job_id* (if any) to stop.

        Returns immediately.  Cancelling the token raises out of yt-dlp's next
        progress callback and signals a running ffmpeg, but the thread still has
        to unwind and delete its scratch directory, so the job's final status is
        written when it reports back, not here.
        """
        run = self._active_runs.get(job_id)
        if run is None:
            return
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

        event = SSEEvent(event=event_type, job_id=job.id, data=data)
        self._on_event(event)
