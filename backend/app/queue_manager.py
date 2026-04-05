"""In-memory async job queue manager for yt-dlp Web UI.

Owns the job lifecycle state machine, concurrency control via asyncio
semaphore, per-job timeout enforcement, and retry logic.  Integrates
with the downloader module to execute downloads.

State machine::

    queued ──► downloading ──► converting ──► done
                     │
                     └──► error  (failure / timeout)

    error ──► queued  (retry)
"""

import asyncio
import logging
import os
from typing import Callable

from app.downloader import DownloadError, download_audio
from app.models import Job, JobStatus, SSEEvent

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT_DOWNLOADS = 2
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 900  # 15 minutes


class QueueError(Exception):
    """Raised for queue-level errors (invalid retry, missing job, etc.)."""


class QueueManager:
    """Async job queue with concurrency control, timeouts, and event hooks.

    Args:
        max_concurrent: Maximum simultaneous downloads (from env or default).
        timeout: Per-job download timeout in seconds (from env or default).
        on_event: Optional callback invoked with ``SSEEvent`` on every
            job state change and progress update.  Task 6 will wire this
            to the SSE stream.
    """

    def __init__(
        self,
        max_concurrent: int | None = None,
        timeout: int | None = None,
        on_event: Callable[[SSEEvent], None] | None = None,
    ) -> None:
        if max_concurrent is None:
            max_concurrent = int(
                os.environ.get("MAX_CONCURRENT_DOWNLOADS", DEFAULT_MAX_CONCURRENT_DOWNLOADS)
            )
        if timeout is None:
            timeout = int(
                os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", DEFAULT_DOWNLOAD_TIMEOUT_SECONDS)
            )

        self._max_concurrent = max_concurrent
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._jobs: dict[str, Job] = {}
        self._on_event = on_event

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_job(self, job: Job) -> Job:
        """Add a job to the queue and kick off async processing.

        The job should already have metadata populated (title,
        thumbnail_url, duration) from a prior ``extract_metadata`` call.
        Its status must be ``QUEUED``.

        Returns the job as stored in the queue.
        """
        self._jobs[job.id] = job
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

    def get_jobs(self) -> list[Job]:
        """Return all jobs ordered by insertion (dict preserves order)."""
        return list(self._jobs.values())

    def get_job(self, job_id: str) -> Job | None:
        """Return a single job by ID, or ``None`` if not found."""
        return self._jobs.get(job_id)

    def retry_job(self, job_id: str) -> Job:
        """Re-queue a failed or errored job.

        Resets the job to ``QUEUED`` status, clears the error message and
        progress, and schedules it for processing again.

        Raises:
            QueueError: If the job does not exist or is not in ERROR status.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise QueueError(f"Job {job_id!r} not found")
        if job.status != JobStatus.ERROR:
            raise QueueError(
                f"Job {job_id!r} is in {job.status.value!r} status, only ERROR jobs can be retried"
            )

        job.status = JobStatus.QUEUED
        job.error = None
        job.progress = 0.0
        self._emit_event("status_change", job)

        logger.info("Job %s retried, re-queued for processing", job.id)
        asyncio.create_task(self._process_job(job.id))
        return job

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
            # Re-fetch in case job was modified (e.g. cancelled) while waiting
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

                # ---- converting ----
                self._update_status(job_id, JobStatus.CONVERTING)

                # ---- done ----
                self._update_status(job_id, JobStatus.DONE)

            except asyncio.TimeoutError:
                logger.warning("Job %s timed out after %ss", job_id, self._timeout)
                self._update_status(job_id, JobStatus.ERROR)
                job = self._jobs[job_id]
                job.error = f"Download timed out after {self._timeout} seconds"
                self._emit_event("error", job)

            except DownloadError as exc:
                logger.warning("Job %s failed: %s", job_id, exc)
                self._update_status(job_id, JobStatus.ERROR)
                job = self._jobs[job_id]
                job.error = str(exc)
                self._emit_event("error", job)

            except Exception as exc:
                logger.exception("Job %s encountered unexpected error", job_id)
                self._update_status(job_id, JobStatus.ERROR)
                job = self._jobs[job_id]
                job.error = f"Unexpected error: {exc}"
                self._emit_event("error", job)

    async def _run_download(self, job_id: str) -> None:
        """Run the synchronous ``download_audio`` call in a thread executor
        so it doesn't block the event loop.
        """
        job = self._jobs[job_id]

        def on_progress(percentage: float) -> None:
            job.progress = percentage
            self._emit_event("progress", job)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, download_audio, job, on_progress)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_status(self, job_id: str, status: JobStatus) -> None:
        """Update a job's status and emit a status_change event."""
        job = self._jobs.get(job_id)
        if job is None:
            return
        old_status = job.status.value
        job.status = status
        logger.info("Job %s: %s -> %s", job_id, old_status, status.value)
        self._emit_event("status_change", job)

    def _emit_event(self, event_type: str, job: Job) -> None:
        """Build an SSEEvent and invoke the callback (if set)."""
        if self._on_event is None:
            return

        data: dict = {"status": job.status.value, "progress": job.progress}
        if job.error:
            data["error"] = job.error

        event = SSEEvent(event=event_type, job_id=job.id, data=data)
        self._on_event(event)
