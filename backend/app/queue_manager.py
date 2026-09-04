"""In-memory async job queue manager for yt-dlp Web UI.

Owns the job lifecycle state machine, concurrency control via asyncio
semaphore, per-job timeout enforcement, and retry logic.  Integrates
with the downloader module to execute downloads.

State machine::

    queued ──► downloading ──► converting ──► [tagging] ──► done
                     │              │             │
                     └──────────────┴─────────────┴──► error  (failure / timeout)
                                    │
                                    └──────────────────► cancelled

    error ──► queued  (retry)

``converting`` is reported by the downloader when ffmpeg actually starts;
a download whose conversion is instantaneous may go straight to ``done``.
``tagging`` and ``cancelled`` are part of the persisted status vocabulary from
the first schema version but nothing enters them yet: the tagging worker
arrives in phase 8 and cancel in phase 2.  Restore already treats ``tagging``
as an interrupted state so it does not need a special case then.

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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.downloader import (
    DownloadError,
    download_audio,
    remove_job_temp_dir,
    remove_orphan_temp_dirs,
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

_IN_FLIGHT = (
    JobStatus.QUEUED,
    JobStatus.DOWNLOADING,
    JobStatus.CONVERTING,
    JobStatus.TAGGING,
)

# Statuses that only a running process can be in.  Finding one of these in the
# database at boot means the process died mid-job.
_INTERRUPTED = (JobStatus.DOWNLOADING, JobStatus.CONVERTING, JobStatus.TAGGING)

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
    """Raised for queue-level errors (invalid retry, missing job, etc.)."""


@dataclass
class _ActiveRun:
    """Bookkeeping for one download thread.

    ``cancel_event`` tells the downloader to abort at its next progress
    callback; ``finished`` is set by the thread when it has fully exited.
    """

    cancel_event: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)


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
        asyncio.create_task(self._process_job(job.id))
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
            raise QueueError(f"Job {job_id!r} not found")
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
        self._persist(job)
        self._emit_event("status_change", job)

        logger.info("Job %s retried, re-queued for processing", job.id)
        asyncio.create_task(self._process_job(job.id))
        return job

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def restore_from_store(self) -> list[Job]:
        """Reload the queue from SQLite and resume where the last run stopped.

        Called once from the app lifespan, after the store is open and before
        the app serves requests, so ``add_job``'s duplicate check and
        ``GET /queue`` both see the restored rows immediately.

        ``queued`` and ``error`` rows are loaded as they are.  Rows still in
        ``downloading``, ``converting`` or ``tagging`` mean the process died
        mid-job: their scratch directory is removed, ``attempts`` and
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
                asyncio.create_task(self._process_job(job.id))

        if restored:
            logger.info(
                "Restored %d job(s) from the store (%d re-queued)",
                len(restored),
                sum(1 for job in restored if job.status == JobStatus.QUEUED),
            )
        return restored

    def _recover_interrupted(self, job: Job) -> None:
        """Re-queue *job*, or give up on it after too many interruptions."""
        remove_job_temp_dir(job.id)
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

            # ---- downloading ----
            self._update_status(job_id, JobStatus.DOWNLOADING)

            try:
                await asyncio.wait_for(
                    self._run_download(job_id),
                    timeout=self._timeout,
                )

                # ---- done ---- (converting is reported by the downloader)
                self._update_status(job_id, JobStatus.DONE)

            except asyncio.TimeoutError:
                logger.warning("Job %s timed out after %ss", job_id, self._timeout)
                # Releasing the semaphore slot (on leaving this block) does not
                # stop the thread: the cancel event is only checked from the
                # progress hook, so a job that timed out while ffmpeg was
                # converting keeps a yt-dlp thread busy until ffmpeg returns.
                # The next queued job therefore starts against transiently more
                # than `max_concurrent` running downloads.  Bounded by one
                # conversion and self-correcting; phase 2 makes converting
                # killable, which removes it.
                self._cancel_run(job_id)
                self._fail(job_id, f"Download timed out after {self._timeout} seconds")

            except DownloadError as exc:
                logger.warning("Job %s failed: %s", job_id, exc)
                self._fail(job_id, str(exc))

            except Exception as exc:
                logger.exception("Job %s encountered unexpected error", job_id)
                self._fail(job_id, f"Unexpected error: {exc}")

    async def _run_download(self, job_id: str) -> None:
        """Run the synchronous ``download_audio`` call in a thread executor
        so it doesn't block the event loop.

        The thread is tracked in ``_active_runs`` so a timeout can signal it
        to stop and a retry can refuse to start while it is still alive.
        """
        job = self._jobs[job_id]
        run = _ActiveRun()
        self._active_runs[job_id] = run
        last_whole_percent = -1

        def on_progress(percentage: float) -> None:
            nonlocal last_whole_percent
            if run.cancel_event.is_set():
                return  # job already failed; don't resurrect its progress
            job.progress = percentage
            # yt-dlp calls this per chunk; only emit when the visible
            # percentage changes to keep SSE traffic sane.
            whole = int(percentage)
            if whole == last_whole_percent:
                return
            last_whole_percent = whole
            self._emit_event("progress", job)

        def on_phase(phase: str) -> None:
            if run.cancel_event.is_set():
                return
            if phase == "converting":
                self._update_status(job_id, JobStatus.CONVERTING)
            elif phase == "metadata":
                # The downloader has just backfilled title/duration/thumbnail
                # on the job; those are stored columns, so write before emit.
                self._persist(job)
                self._emit_event("metadata", job)

        def runner() -> None:
            try:
                result = download_audio(
                    job,
                    on_progress,
                    cancel_event=run.cancel_event,
                    on_phase=on_phase,
                )
                if run.cancel_event.is_set():
                    # A timeout already failed the job and persisted its error
                    # row; recording a result now would put a path on a job the
                    # user is looking at as failed, and would diverge from the
                    # database.  The file itself is left alone: it may have
                    # replaced a track that was already in the library.
                    logger.warning(
                        "Job %s finished after it was cancelled; %s is a stray file",
                        job_id,
                        result,
                    )
                else:
                    # Recorded here rather than after the await so the DONE
                    # transition's _persist writes it before the SSE event.
                    self._record_result_path(job, result)
            finally:
                # Cleanup belongs to the thread that owns the temp directory.
                # Doing it in _process_job's `finally` would race this thread,
                # which is still converting and moving files after a timeout has
                # already given up waiting for it.
                #
                # Phase 2: once cancel runs ffmpeg as our own subprocess we can
                # kill it, so conversion becomes cancellable and the stray-file
                # case above disappears.
                remove_job_temp_dir(job_id)
                if self._active_runs.get(job_id) is run:
                    self._active_runs.pop(job_id, None)
                run.finished.set()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, runner)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cancel_run(self, job_id: str) -> None:
        """Ask the download thread for *job_id* (if any) to stop."""
        run = self._active_runs.get(job_id)
        if run is not None:
            run.cancel_event.set()

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
        if job.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED):
            job.finished_at = job.updated_at
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

        A transition to the status the job is already in is dropped: yt-dlp
        calls each postprocessor hook twice per postprocessor, so ``converting``
        would otherwise be written and broadcast twice for one conversion.
        """
        job = self._jobs.get(job_id)
        if job is None or job.status == status:
            return
        old_status = job.status.value
        job.status = status
        self._persist(job)
        logger.info("Job %s: %s -> %s", job_id, old_status, status.value)
        self._emit_event("status_change", job)

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
