"""Tests for the queue manager module.

Covers the full job state machine, concurrency control, timeout
enforcement, retry logic, and event callback system.  All tests
mock the downloader module -- no real network calls or downloads.
"""

import asyncio
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mutagen.flac import FLAC

from app import album_tagger
from app.downloader import DownloadError, FiledTrack, unfile_track
from app.job_store import JobStore
from app.models import Job, JobKind, JobStatus, SSEEvent
from app.tagger import (
    NOTE_CANCELLED,
    NOTE_FAILED,
    NOTE_FILE_MISSING,
    NOTE_NO_MATCH,
    NOTE_TIMED_OUT,
    NOTE_UNAVAILABLE,
    TagFixResult,
)
from tests.conftest import minimal_flac_bytes
from app.queue_manager import (
    DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    MAX_RESTART_ATTEMPTS,
    THREAD_DRAIN_GRACE_SECONDS,
    RESTART_GIVE_UP_MESSAGE,
    RETENTION_DAYS,
    JobNotFound,
    QueueError,
    QueueManager,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _GatedAsyncio:
    """`asyncio` as app.queue_manager sees it, with a gated `wait_for`.

    The timeout path used to be provoked with a real 0.2 s timeout and a
    thread that had to file its track first: an ordering that held only while
    the machine was idle, and that a loaded CI box lost.  Standing in for the
    module's `asyncio` reference makes the ordering explicit -- the test opens
    the gate exactly when the track has been filed, and `wait_for` then
    cancels the run and raises `TimeoutError` as the real one would.
    """

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def wait_for(self, awaitable, timeout):
        task = asyncio.ensure_future(awaitable)
        await self._gate.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        raise asyncio.TimeoutError


def _make_job(**overrides) -> Job:
    """Create a Job with sensible defaults, overriding any field.

    The URL is derived from the id unless one is given: ``add_job`` now
    rejects a URL that is already in flight, so tests that queue several
    jobs need distinct URLs.
    """
    defaults = {
        "id": "job-1",
        "status": JobStatus.QUEUED,
        "title": "Test Track",
        "thumbnail_url": "https://img.youtube.com/thumb.jpg",
        "duration": 210.0,
    }
    defaults.update(overrides)
    defaults.setdefault("url", f"https://www.youtube.com/watch?v={defaults['id']}")
    return Job(**defaults)


async def _wait_for_job_status(
    qm: QueueManager, job_id: str, status: JobStatus, timeout: float = 5.0
) -> None:
    """Poll until a job reaches the expected status (or timeout)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        job = qm.get_job(job_id)
        if job is not None and job.status == status:
            return
        await asyncio.sleep(0.01)
    job = qm.get_job(job_id)
    current = job.status.value if job else "NOT FOUND"
    raise AssertionError(
        f"Job {job_id!r} did not reach {status.value!r} within {timeout}s "
        f"(current: {current!r})"
    )


# ===========================================================================
# Constructor / configuration
# ===========================================================================


class TestQueueManagerInit:
    """Tests for QueueManager initialization and configuration."""

    def test_defaults_when_no_args_or_env(self):
        with patch.dict("os.environ", {}, clear=True):
            qm = QueueManager()
        assert qm._max_concurrent == DEFAULT_MAX_CONCURRENT_DOWNLOADS
        assert qm._timeout == DEFAULT_DOWNLOAD_TIMEOUT_SECONDS

    def test_explicit_args_override_defaults(self):
        qm = QueueManager(max_concurrent=5, timeout=60)
        assert qm._max_concurrent == 5
        assert qm._timeout == 60

    def test_env_vars_override_defaults(self):
        env = {"MAX_CONCURRENT_DOWNLOADS": "4", "DOWNLOAD_TIMEOUT_SECONDS": "120"}
        with patch.dict("os.environ", env, clear=True):
            qm = QueueManager()
        assert qm._max_concurrent == 4
        assert qm._timeout == 120

    def test_explicit_args_take_precedence_over_env(self):
        env = {"MAX_CONCURRENT_DOWNLOADS": "4", "DOWNLOAD_TIMEOUT_SECONDS": "120"}
        with patch.dict("os.environ", env, clear=True):
            qm = QueueManager(max_concurrent=1, timeout=30)
        assert qm._max_concurrent == 1
        assert qm._timeout == 30

    def test_on_event_callback_stored(self):
        cb = MagicMock()
        qm = QueueManager(on_event=cb)
        assert qm._on_event is cb

    def test_no_event_callback_by_default(self):
        qm = QueueManager()
        assert qm._on_event is None

    def test_starts_with_empty_job_list(self):
        qm = QueueManager()
        assert qm.get_jobs() == []


# ===========================================================================
# State transitions -- happy path
# ===========================================================================


class TestStateTransitionsHappyPath:
    """Tests that a successful download transitions through all states."""

    @patch("app.queue_manager.download_audio")
    async def test_job_reaches_done_status(self, mock_download):
        mock_download.return_value = "/data/music/Artist/Album/track.flac"

        qm = QueueManager(max_concurrent=2, timeout=10)
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert qm.get_job("job-1").status == JobStatus.DONE

    @patch("app.queue_manager.download_audio")
    async def test_download_audio_called_with_job_and_progress_callback(self, mock_download):
        mock_download.return_value = "/data/music/Artist/Album/track.flac"

        qm = QueueManager(max_concurrent=2, timeout=10)
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        mock_download.assert_called_once()
        call_args = mock_download.call_args
        assert call_args[0][0] is job  # first positional arg is the job
        assert callable(call_args[0][1])  # second positional arg is on_progress

    @patch("app.queue_manager.download_audio")
    async def test_state_transitions_emitted_in_order(self, mock_download):
        mock_download.return_value = "/data/music/Artist/Album/track.flac"
        events = []

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=lambda e: events.append(e))
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        status_events = [e for e in events if e.event == "status_change"]
        statuses = [e.data["status"] for e in status_events]
        # CONVERTING is only reported when the downloader says ffmpeg started;
        # a mocked download never does, so it goes straight to done.
        assert statuses == ["downloading", "done"]

    @patch("app.queue_manager.download_audio")
    async def test_progress_events_emitted(self, mock_download):
        """When download_audio invokes the on_progress callback, progress
        events should be emitted via the on_event hook."""
        events = []

        def fake_download(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            on_progress(25.0)
            on_progress(50.0)
            on_progress(100.0)
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = fake_download

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=lambda e: events.append(e))
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        progress_events = [e for e in events if e.event == "progress"]
        percentages = [e.data["progress"] for e in progress_events]
        assert percentages == [25.0, 50.0, 100.0]

    @patch("app.queue_manager.download_audio")
    async def test_job_progress_updated_on_callback(self, mock_download):
        recorded_progresses = []

        def fake_download(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            on_progress(42.0)
            recorded_progresses.append(job.progress)
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = fake_download

        qm = QueueManager(max_concurrent=2, timeout=10)
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert recorded_progresses == [42.0]


# ===========================================================================
# State transitions -- error path
# ===========================================================================


class TestStateTransitionsErrorPath:
    """Tests that failures result in ERROR status with appropriate messages."""

    @patch("app.queue_manager.download_audio")
    async def test_download_error_sets_error_status(self, mock_download):
        mock_download.side_effect = DownloadError("Video unavailable")

        qm = QueueManager(max_concurrent=2, timeout=10)
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        result = qm.get_job("job-1")
        assert result.status == JobStatus.ERROR
        assert result.error == "Video unavailable"

    @patch("app.queue_manager.download_audio")
    async def test_unexpected_error_sets_error_status(self, mock_download):
        mock_download.side_effect = RuntimeError("Something unexpected")

        qm = QueueManager(max_concurrent=2, timeout=10)
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        result = qm.get_job("job-1")
        assert result.status == JobStatus.ERROR
        assert "Unexpected error" in result.error

    @patch("app.queue_manager.download_audio")
    async def test_error_events_emitted_on_failure(self, mock_download):
        mock_download.side_effect = DownloadError("Network error")
        events = []

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=lambda e: events.append(e))
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        error_events = [e for e in events if e.event == "error"]
        assert len(error_events) == 1
        assert error_events[0].data["error"] == "Network error"

    @patch("app.queue_manager.download_audio")
    async def test_error_path_state_transitions(self, mock_download):
        mock_download.side_effect = DownloadError("Fail")
        events = []

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=lambda e: events.append(e))
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        status_events = [e for e in events if e.event == "status_change"]
        statuses = [e.data["status"] for e in status_events]
        assert statuses == ["downloading", "error"]


# ===========================================================================
# Concurrency control
# ===========================================================================


class TestConcurrencyControl:
    """Tests that the asyncio semaphore enforces concurrency limits."""

    @patch("app.queue_manager.download_audio")
    async def test_only_max_concurrent_jobs_run_simultaneously(self, mock_download):
        """Submit 4 jobs with max_concurrent=2.  At most 2 should be
        downloading at any point in time."""
        max_running = 0
        current_running = 0
        lock = asyncio.Lock()
        download_started = asyncio.Event()
        proceed = asyncio.Event()

        async def controlled_download(job, on_progress):
            nonlocal max_running, current_running
            async with lock:
                current_running += 1
                max_running = max(max_running, current_running)
            download_started.set()
            # Wait until test says to proceed
            await proceed.wait()
            async with lock:
                current_running -= 1
            return "/data/music/output.flac"

        # download_audio is called in run_in_executor, so we need to replace
        # _run_download entirely for this concurrency test.
        qm = QueueManager(max_concurrent=2, timeout=10)

        # Patch _run_download to use our async controlled version
        original_run_download = qm._run_download

        async def patched_run_download(job_id, run):
            job = qm._jobs[job_id]
            await controlled_download(job, None)

        qm._run_download = patched_run_download

        # Submit 4 jobs
        for i in range(4):
            qm.add_job(_make_job(id=f"job-{i}"))

        # Let tasks start and hit the semaphore
        await asyncio.sleep(0.1)

        # At most 2 should be running concurrently
        assert max_running <= 2

        # The 2 that acquired the semaphore should be DOWNLOADING
        downloading = [j for j in qm.get_jobs() if j.status == JobStatus.DOWNLOADING]
        assert len(downloading) == 2

        # The other 2 should still be QUEUED (waiting for semaphore)
        queued = [j for j in qm.get_jobs() if j.status == JobStatus.QUEUED]
        assert len(queued) == 2

        # Let all jobs finish
        proceed.set()
        for i in range(4):
            await _wait_for_job_status(qm, f"job-{i}", JobStatus.DONE)

    @patch("app.queue_manager.download_audio")
    async def test_queued_jobs_proceed_when_slot_opens(self, mock_download):
        """When a job completes, a waiting job should pick up the slot."""
        slot_events = []
        proceed_events = {}

        qm = QueueManager(max_concurrent=1, timeout=10)

        for i in range(3):
            proceed_events[f"job-{i}"] = asyncio.Event()

        async def patched_run_download(job_id, run):
            slot_events.append(("start", job_id))
            await proceed_events[job_id].wait()
            slot_events.append(("end", job_id))

        qm._run_download = patched_run_download

        for i in range(3):
            qm.add_job(_make_job(id=f"job-{i}"))

        # Wait for job-0 to start
        await asyncio.sleep(0.05)
        assert qm.get_job("job-0").status == JobStatus.DOWNLOADING
        assert qm.get_job("job-1").status == JobStatus.QUEUED

        # Let job-0 finish
        proceed_events["job-0"].set()
        await _wait_for_job_status(qm, "job-0", JobStatus.DONE)

        # job-1 should now start
        await asyncio.sleep(0.05)
        assert qm.get_job("job-1").status == JobStatus.DOWNLOADING

        # Let remaining jobs finish
        proceed_events["job-1"].set()
        proceed_events["job-2"].set()
        for i in range(3):
            await _wait_for_job_status(qm, f"job-{i}", JobStatus.DONE)

    @patch("app.queue_manager.download_audio")
    async def test_all_jobs_complete_with_concurrency_limit(self, mock_download):
        mock_download.return_value = "/data/music/output.flac"

        qm = QueueManager(max_concurrent=2, timeout=10)

        for i in range(5):
            qm.add_job(_make_job(id=f"job-{i}"))

        for i in range(5):
            await _wait_for_job_status(qm, f"job-{i}", JobStatus.DONE)

        assert all(j.status == JobStatus.DONE for j in qm.get_jobs())


# ===========================================================================
# Timeout enforcement
# ===========================================================================


class TestTimeoutEnforcement:
    """Tests that jobs exceeding the timeout are marked as error."""

    async def test_slow_download_is_timed_out(self):
        qm = QueueManager(max_concurrent=2, timeout=1)  # 1 second timeout

        async def slow_download(job_id, run):
            try:
                await asyncio.sleep(10)  # Way longer than timeout
            finally:
                # The real _run_download sets this when its thread exits, and
                # the timeout path waits for it before releasing the slot.
                run.finished.set()

        qm._run_download = slow_download

        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR, timeout=5.0)

        result = qm.get_job("job-1")
        assert result.status == JobStatus.ERROR
        assert "timed out" in result.error.lower()

    async def test_timeout_error_message_includes_duration(self):
        qm = QueueManager(max_concurrent=2, timeout=1)

        async def slow_download(job_id, run):
            try:
                await asyncio.sleep(10)
            finally:
                run.finished.set()

        qm._run_download = slow_download

        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR, timeout=5.0)

        result = qm.get_job("job-1")
        assert "1 seconds" in result.error

    async def test_timeout_emits_error_event(self):
        events = []
        qm = QueueManager(max_concurrent=2, timeout=1, on_event=lambda e: events.append(e))

        async def slow_download(job_id, run):
            try:
                await asyncio.sleep(10)
            finally:
                run.finished.set()

        qm._run_download = slow_download

        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR, timeout=5.0)

        error_events = [e for e in events if e.event == "error"]
        assert len(error_events) == 1
        assert "timed out" in error_events[0].data["error"].lower()

    @patch("app.queue_manager.download_audio")
    async def test_fast_download_completes_before_timeout(self, mock_download):
        mock_download.return_value = "/data/music/output.flac"

        qm = QueueManager(max_concurrent=2, timeout=30)
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert qm.get_job("job-1").status == JobStatus.DONE

    async def test_timeout_frees_concurrency_slot(self):
        """A timed-out job should release the semaphore so waiting jobs
        can proceed."""
        qm = QueueManager(max_concurrent=1, timeout=1)

        proceed_event = asyncio.Event()

        call_count = 0

        async def patched_run_download(job_id, run):
            nonlocal call_count
            call_count += 1
            try:
                if job_id == "job-0":
                    # This one will time out
                    await asyncio.sleep(10)
                else:
                    # This one completes quickly
                    await proceed_event.wait()
            finally:
                run.finished.set()

        qm._run_download = patched_run_download

        qm.add_job(_make_job(id="job-0"))
        qm.add_job(_make_job(id="job-1"))

        # job-0 times out
        await _wait_for_job_status(qm, "job-0", JobStatus.ERROR, timeout=5.0)

        # job-1 should now be downloading (got the slot)
        await asyncio.sleep(0.1)
        assert qm.get_job("job-1").status == JobStatus.DOWNLOADING

        # Let job-1 finish
        proceed_event.set()
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)


# ===========================================================================
# Retry logic
# ===========================================================================


class TestRetryLogic:
    """Tests for retrying failed and timed-out jobs."""

    @patch("app.queue_manager.download_audio")
    async def test_retry_resets_job_to_queued(self, mock_download):
        mock_download.side_effect = DownloadError("First attempt fails")

        qm = QueueManager(max_concurrent=2, timeout=10)
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        # Now make download succeed on retry
        mock_download.side_effect = None
        mock_download.return_value = "/data/music/output.flac"

        retried = qm.retry_job("job-1")
        assert retried.status == JobStatus.QUEUED
        assert retried.error is None
        assert retried.progress == 0.0

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert qm.get_job("job-1").status == JobStatus.DONE

    @patch("app.queue_manager.download_audio")
    async def test_retry_clears_error_and_progress(self, mock_download):
        mock_download.side_effect = DownloadError("Fail")

        qm = QueueManager(max_concurrent=2, timeout=10)
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        assert qm.get_job("job-1").error is not None

        mock_download.side_effect = None
        mock_download.return_value = "/data/music/output.flac"

        qm.retry_job("job-1")

        # Immediately after retry, error and progress should be cleared
        job_after = qm.get_job("job-1")
        assert job_after.error is None
        assert job_after.progress == 0.0

    async def test_retry_timed_out_job(self):
        qm = QueueManager(max_concurrent=2, timeout=1)

        first_call = True

        async def patched_run_download(job_id, run):
            nonlocal first_call
            if first_call:
                first_call = False
                await asyncio.sleep(10)  # Will time out
            # Second call succeeds immediately.  The real _run_download sets
            # this from its thread's `finally`; the retry guard waits on it.
            run.finished.set()

        qm._run_download = patched_run_download

        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR, timeout=5.0)
        assert "timed out" in qm.get_job("job-1").error.lower()

        # The timed-out attempt is a zombie coroutine here, so release the
        # guard the way its thread would have.
        qm._active_runs["job-1"].finished.set()
        qm.retry_job("job-1")

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE, timeout=5.0)

    @patch("app.queue_manager.download_audio")
    async def test_retry_keeps_the_resolved_target(self, mock_download):
        """A retry keeps the destination the previous run resolved.

        The re-run resolves from the same ``artist``/``album`` and overwrites
        it through ``on_target`` before it creates anything, so the stored
        answer is the best guess available.  Dropping it made the retried job
        count as *unresolved*, and one unresolved job refuses every move in
        the library with a 409 until it finally runs.
        """
        mock_download.side_effect = DownloadError("Fail")

        qm = QueueManager(max_concurrent=2, timeout=10)
        qm.add_job(_make_job(target_dir="Bonobo/Fragments"))

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)
        assert qm.get_job("job-1").target_dir == "Bonobo/Fragments"

        assert qm.retry_job("job-1").target_dir == "Bonobo/Fragments"
        assert qm.in_flight_library_targets().unresolved == 0

    @patch("app.queue_manager.download_audio")
    async def test_a_re_run_overwrites_the_kept_target(self, mock_download, tmp_path):
        """The kept value is stale for no longer than the run takes to resolve."""
        result = tmp_path / "out.flac"
        result.write_text("flac")

        runs: list[int] = []

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            runs.append(1)
            if len(runs) == 1:
                raise DownloadError("Fail")
            on_target("Bonobo/Migration")
            return str(result)

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=2, timeout=10)
        qm.add_job(_make_job(target_dir="Bonobo/Fragments"))
        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        qm.retry_job("job-1")
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert qm.get_job("job-1").target_dir == "Bonobo/Migration"

    def test_retry_nonexistent_job_raises(self):
        qm = QueueManager(max_concurrent=2, timeout=10)

        with pytest.raises(QueueError, match="not found"):
            qm.retry_job("nonexistent")

    @patch("app.queue_manager.download_audio")
    async def test_retry_non_error_job_raises(self, mock_download):
        mock_download.return_value = "/data/music/output.flac"

        qm = QueueManager(max_concurrent=2, timeout=10)
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        with pytest.raises(QueueError, match="only ERROR jobs can be retried"):
            qm.retry_job("job-1")

    @patch("app.queue_manager.download_audio")
    async def test_retry_emits_status_change_event(self, mock_download):
        mock_download.side_effect = DownloadError("Fail")
        events = []

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=lambda e: events.append(e))
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        events.clear()

        mock_download.side_effect = None
        mock_download.return_value = "/data/music/output.flac"

        qm.retry_job("job-1")

        # Should have a status_change event for the reset to QUEUED
        queued_events = [
            e for e in events
            if e.event == "status_change" and e.data["status"] == "queued"
        ]
        assert len(queued_events) == 1


# ===========================================================================
# Job retrieval
# ===========================================================================


class TestJobRetrieval:
    """Tests for get_job and get_jobs."""

    def test_get_job_returns_none_for_missing_id(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        assert qm.get_job("nonexistent") is None

    @patch("app.queue_manager.download_audio")
    async def test_get_jobs_returns_all_jobs(self, mock_download):
        mock_download.return_value = "/data/music/output.flac"

        qm = QueueManager(max_concurrent=2, timeout=10)
        for i in range(3):
            qm.add_job(_make_job(id=f"job-{i}"))

        jobs = qm.get_jobs()
        assert len(jobs) == 3
        assert {j.id for j in jobs} == {"job-0", "job-1", "job-2"}

    @patch("app.queue_manager.download_audio")
    async def test_get_jobs_preserves_insertion_order(self, mock_download):
        mock_download.return_value = "/data/music/output.flac"

        qm = QueueManager(max_concurrent=2, timeout=10)
        for i in range(5):
            qm.add_job(_make_job(id=f"job-{i}"))

        jobs = qm.get_jobs()
        assert [j.id for j in jobs] == [f"job-{i}" for i in range(5)]

    @patch("app.queue_manager.download_audio")
    async def test_get_job_returns_correct_job(self, mock_download):
        mock_download.return_value = "/data/music/output.flac"

        qm = QueueManager(max_concurrent=2, timeout=10)
        qm.add_job(_make_job(id="job-a", title="Track A"))
        qm.add_job(_make_job(id="job-b", title="Track B"))

        assert qm.get_job("job-a").title == "Track A"
        assert qm.get_job("job-b").title == "Track B"


# ===========================================================================
# Event callback system
# ===========================================================================


class TestEventCallbackSystem:
    """Tests that SSEEvent objects are correctly constructed and emitted."""

    @patch("app.queue_manager.download_audio")
    async def test_events_are_sse_event_instances(self, mock_download):
        mock_download.return_value = "/data/music/output.flac"
        events = []

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=lambda e: events.append(e))
        qm.add_job(_make_job())

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert all(isinstance(e, SSEEvent) for e in events)

    @patch("app.queue_manager.download_audio")
    async def test_events_contain_job_id(self, mock_download):
        mock_download.return_value = "/data/music/output.flac"
        events = []

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=lambda e: events.append(e))
        qm.add_job(_make_job(id="my-job"))

        await _wait_for_job_status(qm, "my-job", JobStatus.DONE)

        assert all(e.job_id == "my-job" for e in events)

    @patch("app.queue_manager.download_audio")
    async def test_no_events_without_callback(self, mock_download):
        """When no on_event callback is set, nothing should blow up."""
        mock_download.return_value = "/data/music/output.flac"

        qm = QueueManager(max_concurrent=2, timeout=10)
        qm.add_job(_make_job())

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        # No assertion beyond "it didn't crash"

    @patch("app.queue_manager.download_audio")
    async def test_error_event_data_contains_error_message(self, mock_download):
        mock_download.side_effect = DownloadError("Something broke")
        events = []

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=lambda e: events.append(e))
        qm.add_job(_make_job())

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        error_events = [e for e in events if e.event == "error"]
        assert len(error_events) == 1
        assert error_events[0].data["error"] == "Something broke"
        assert error_events[0].data["status"] == "error"

    @patch("app.queue_manager.download_audio")
    async def test_status_change_event_data_contains_status(self, mock_download):
        mock_download.return_value = "/data/music/output.flac"
        events = []

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=lambda e: events.append(e))
        qm.add_job(_make_job())

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        for e in events:
            if e.event == "status_change":
                assert "status" in e.data


# ===========================================================================
# Phase reporting from the downloader
# ===========================================================================


class TestPhaseReporting:
    """CONVERTING and the metadata backfill are driven by the downloader's
    ``on_phase`` callback rather than guessed by the queue manager."""

    @patch("app.queue_manager.download_audio")
    async def test_converting_phase_emits_status_change(self, mock_download):
        events = []

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            on_progress(100.0)
            on_phase("converting")
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=lambda e: events.append(e))
        qm.add_job(_make_job())

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        statuses = [e.data["status"] for e in events if e.event == "status_change"]
        assert statuses == ["downloading", "converting", "done"]

    @patch("app.queue_manager.download_audio")
    async def test_metadata_phase_emits_metadata_event(self, mock_download):
        events = []

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            job.title = "Backfilled Title"
            job.duration = 99.0
            on_phase("metadata")
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=lambda e: events.append(e))
        qm.add_job(_make_job(title=None, duration=None))

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        metadata_events = [e for e in events if e.event == "metadata"]
        assert len(metadata_events) == 1
        assert metadata_events[0].data["title"] == "Backfilled Title"
        assert metadata_events[0].data["duration"] == 99.0


# ===========================================================================
# Progress throttling
# ===========================================================================


class TestProgressThrottling:
    """yt-dlp fires the progress hook per chunk; only visible changes are sent."""

    @patch("app.queue_manager.download_audio")
    async def test_only_whole_percent_changes_are_emitted(self, mock_download):
        events = []

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            on_progress(10.2)
            on_progress(10.7)
            on_progress(11.0)
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=lambda e: events.append(e))
        qm.add_job(_make_job())

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        progress_events = [e for e in events if e.event == "progress"]
        assert [e.data["progress"] for e in progress_events] == [10.2, 11.0]

    @patch("app.queue_manager.download_audio")
    async def test_job_progress_still_tracks_every_callback(self, mock_download):
        """Throttling is about SSE traffic, not about the job's own state."""

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            on_progress(10.2)
            on_progress(10.7)
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=2, timeout=10)
        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert job.progress == 10.7


# ===========================================================================
# Cancellation on timeout
# ===========================================================================


class TestTimeoutCancellation:
    """A timeout must actually stop the download thread, not just give up
    waiting for it -- otherwise it keeps writing files and emitting events."""

    @patch("app.queue_manager.download_audio")
    async def test_timeout_cancels_the_run_and_silences_progress(self, mock_download):
        import threading

        events = []
        release = threading.Event()
        seen = {}

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            release.wait(5)
            seen["cancelled"] = cancel.is_set()
            # A zombie thread must not be able to resurrect the job's progress.
            on_progress(50.0)
            seen["progress_events_after_cancel"] = [
                e for e in events if e.event == "progress"
            ]
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=2, timeout=1, on_event=lambda e: events.append(e))
        qm.add_job(_make_job())

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR, timeout=5.0)
        assert "timed out" in qm.get_job("job-1").error.lower()

        release.set()
        for _ in range(200):
            if "progress_events_after_cancel" in seen:
                break
            await asyncio.sleep(0.01)

        assert seen["cancelled"] is True
        assert seen["progress_events_after_cancel"] == []


# ===========================================================================
# Retry while the previous attempt is still running
# ===========================================================================


class TestRetryGuard:
    """Retrying a timed-out job while its thread is still alive would put two
    yt-dlp runs on the same output file."""

    @patch("app.queue_manager.download_audio")
    async def test_retry_refused_until_previous_thread_exits(self, mock_download):
        import threading

        release = threading.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            release.wait(5)
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=2, timeout=1)
        qm.add_job(_make_job())

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR, timeout=5.0)

        with pytest.raises(QueueError, match="shutting down"):
            qm.retry_job("job-1")

        # Let the zombie thread finish, then the retry is allowed.
        release.set()
        run = qm._active_runs.get("job-1")
        for _ in range(200):
            if run is None or run.finished.is_set():
                break
            await asyncio.sleep(0.01)

        mock_download.side_effect = None
        mock_download.return_value = "/data/music/Artist/Album/track.flac"

        retried = qm.retry_job("job-1")
        assert retried.status == JobStatus.QUEUED
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE, timeout=5.0)


# ===========================================================================
# Duplicate submissions
# ===========================================================================


class TestDuplicateUrls:
    """The same URL twice would have two yt-dlp runs writing one .flac."""

    @patch("app.queue_manager.download_audio")
    async def test_duplicate_in_flight_url_is_rejected(self, mock_download):
        import threading

        release = threading.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            release.wait(5)
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = fake

        url = "https://www.youtube.com/watch?v=dupe"
        qm = QueueManager(max_concurrent=2, timeout=10)
        qm.add_job(_make_job(id="job-a", url=url))

        await _wait_for_job_status(qm, "job-a", JobStatus.DOWNLOADING)

        with pytest.raises(QueueError, match="already in the queue"):
            qm.add_job(_make_job(id="job-b", url=url))

        assert len(qm.get_jobs()) == 1

        release.set()
        await _wait_for_job_status(qm, "job-a", JobStatus.DONE, timeout=5.0)

    @patch("app.queue_manager.download_audio")
    async def test_queued_duplicate_is_rejected(self, mock_download):
        mock_download.return_value = "/data/music/Artist/Album/track.flac"

        url = "https://www.youtube.com/watch?v=dupe2"
        qm = QueueManager(max_concurrent=2, timeout=10)
        qm.add_job(_make_job(id="job-a", url=url))

        with pytest.raises(QueueError, match="already in the queue"):
            qm.add_job(_make_job(id="job-b", url=url))

    @patch("app.queue_manager.download_audio")
    async def test_finished_url_can_be_resubmitted(self, mock_download):
        mock_download.return_value = "/data/music/Artist/Album/track.flac"

        url = "https://www.youtube.com/watch?v=again"
        qm = QueueManager(max_concurrent=2, timeout=10)
        qm.add_job(_make_job(id="job-a", url=url))
        await _wait_for_job_status(qm, "job-a", JobStatus.DONE)

        qm.add_job(_make_job(id="job-b", url=url))
        await _wait_for_job_status(qm, "job-b", JobStatus.DONE)

        # Both rows still exist (get_jobs hides done jobs from the queue view).
        assert qm.get_job("job-a") is not None
        assert qm.get_job("job-b") is not None


# ===========================================================================
# Environment parsing
# ===========================================================================


class TestEnvParsing:
    """docker compose turns an unset variable into an empty string."""

    def test_empty_env_values_are_treated_as_unset(self):
        env = {"MAX_CONCURRENT_DOWNLOADS": "", "DOWNLOAD_TIMEOUT_SECONDS": ""}
        with patch.dict("os.environ", env):
            qm = QueueManager()
        assert qm._max_concurrent == DEFAULT_MAX_CONCURRENT_DOWNLOADS
        assert qm._timeout == DEFAULT_DOWNLOAD_TIMEOUT_SECONDS


# ===========================================================================
# Event payloads
# ===========================================================================


class TestEventSnapshot:
    """Every event carries the job's user-visible fields so a client can
    apply it without having seen the job before."""

    @patch("app.queue_manager.download_audio")
    async def test_events_carry_a_full_job_snapshot(self, mock_download):
        mock_download.return_value = "/data/music/Artist/Album/track.flac"
        events = []

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=lambda e: events.append(e))
        qm.add_job(_make_job(artist="An Artist", album="An Album"))

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert events
        for event in events:
            if event.event == "library_changed":
                continue  # not a job snapshot; it carries the changed paths
            assert event.data["title"] == "Test Track"
            assert event.data["thumbnail_url"] == "https://img.youtube.com/thumb.jpg"
            assert event.data["duration"] == 210.0
            assert event.data["artist"] == "An Artist"
            assert event.data["album"] == "An Album"
            assert "status" in event.data
            assert "progress" in event.data


# ===========================================================================
# Persistence: write-through
# ===========================================================================


@pytest.fixture()
def store(tmp_path):
    """A JobStore on a throwaway database file."""
    store = JobStore(tmp_path / "queue.db")
    yield store
    store.close()


class TestRepeatedStatusIsIgnored:
    """yt-dlp calls each postprocessor hook twice, once per registration."""

    async def test_a_second_converting_produces_one_event_and_one_write(self, store):
        events: list[SSEEvent] = []
        qm = QueueManager(
            max_concurrent=2, timeout=10, on_event=events.append, store=store
        )
        job = _make_job(id="job-1")
        qm._jobs[job.id] = job

        qm._update_status("job-1", JobStatus.CONVERTING)
        first_write = store.get("job-1").updated_at
        qm._update_status("job-1", JobStatus.CONVERTING)

        assert [event.event for event in events] == ["status_change"]
        assert store.get("job-1").updated_at == first_write


class TestWriteThrough:
    """Every transition reaches SQLite before its SSE event is emitted."""

    @patch("app.queue_manager.download_audio")
    async def test_add_job_persists_before_processing(self, mock_download, store):
        mock_download.return_value = "/data/music/Artist/Album/track.flac"

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))

        # add_job returns before the task has had a chance to run.
        stored = store.get("job-1")
        assert stored is not None
        assert stored.status == JobStatus.QUEUED

    @patch("app.queue_manager.download_audio")
    async def test_row_is_current_when_the_event_fires(self, mock_download, store):
        """The assertion runs *inside* the callback, so ordering is real.

        This is the acceptance criterion "every transition is present in
        queue.db before its SSE event reaches a client".
        """
        mock_download.return_value = "/data/music/Artist/Album/track.flac"
        mismatches: list[str] = []

        def check(event: SSEEvent) -> None:
            if event.event == "progress":
                return  # progress is deliberately not persisted
            if event.event == "library_changed":
                return  # carries no status, and follows the status_change
            row = store.get(event.job_id)
            if row is None or row.status.value != event.data["status"]:
                mismatches.append(
                    f"{event.event}: event says {event.data['status']}, "
                    f"row says {row.status.value if row else 'MISSING'}"
                )

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=check, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert mismatches == []

    @patch("app.queue_manager.download_audio")
    async def test_error_message_is_in_the_row_before_the_status_event(
        self, mock_download, store
    ):
        mock_download.side_effect = DownloadError("boom")
        seen: list[str | None] = []

        def check(event: SSEEvent) -> None:
            if event.event == "status_change" and event.data["status"] == "error":
                seen.append(store.get(event.job_id).error)

        qm = QueueManager(max_concurrent=2, timeout=10, on_event=check, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        assert seen == ["boom"]

    @patch("app.queue_manager.download_audio")
    async def test_finished_at_is_stamped_on_terminal_states(self, mock_download, store):
        mock_download.return_value = "/data/music/Artist/Album/track.flac"

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert store.get("job-1").finished_at is not None

    @patch("app.queue_manager.download_audio")
    async def test_queued_job_has_no_finished_at(self, mock_download, store):
        mock_download.return_value = "/data/music/Artist/Album/track.flac"

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))

        assert store.get("job-1").finished_at is None

    @patch("app.queue_manager.download_audio")
    async def test_retry_increments_attempts_and_persists(self, mock_download, store):
        mock_download.side_effect = DownloadError("boom")

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        qm.retry_job("job-1")

        stored = store.get("job-1")
        assert stored.attempts == 1
        assert stored.restart_attempts == 0
        assert stored.status == JobStatus.QUEUED
        assert stored.error is None
        assert stored.finished_at is None

    async def test_a_store_failure_does_not_break_the_queue(self, store):
        """A database write must never take a download down with it."""
        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        store.close()  # every later write now raises

        job = qm.add_job(_make_job(id="job-1"))

        assert qm.get_job("job-1") is job


# ===========================================================================
# Persistence: restore at boot
# ===========================================================================


class TestRestoreFromStore:
    """What the queue does with the rows a previous run left behind."""

    @patch("app.queue_manager.download_audio")
    async def test_queued_rows_are_restored_and_run(self, mock_download, store):
        mock_download.return_value = "/data/music/Artist/Album/track.flac"
        store.upsert(_make_job(id="job-1", status=JobStatus.QUEUED))

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        restored = qm.restore_from_store()

        assert [job.id for job in restored] == ["job-1"]
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

    async def test_error_rows_are_restored_but_not_run(self, store):
        store.upsert(_make_job(id="job-1", status=JobStatus.ERROR, error="old failure"))

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        job = qm.get_job("job-1")
        assert job.status == JobStatus.ERROR
        assert job.error == "old failure"

    async def test_done_and_cancelled_rows_are_not_restored(self, store):
        store.upsert(_make_job(id="done", status=JobStatus.DONE))
        store.upsert(_make_job(id="cancelled", status=JobStatus.CANCELLED))

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)

        assert qm.restore_from_store() == []

    @patch("app.queue_manager.download_audio")
    async def test_downloading_row_is_requeued_with_an_extra_attempt(
        self, mock_download, store
    ):
        mock_download.return_value = "/data/music/Artist/Album/track.flac"
        store.upsert(_make_job(id="job-1", status=JobStatus.DOWNLOADING, attempts=0))

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        assert qm.get_job("job-1").attempts == 1
        assert qm.get_job("job-1").restart_attempts == 1
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

    @pytest.mark.parametrize(
        "status", [JobStatus.DOWNLOADING, JobStatus.CONVERTING]
    )
    @patch("app.queue_manager.download_audio")
    async def test_every_downloading_status_is_requeued(
        self, mock_download, status, store
    ):
        """``tagging`` is the exception and has its own class below: its file
        is finished, so only the fix is re-run."""
        mock_download.return_value = "/data/music/Artist/Album/track.flac"
        store.upsert(_make_job(id="job-1", status=status))

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        assert qm.get_job("job-1").status == JobStatus.QUEUED
        assert store.get("job-1").status == JobStatus.QUEUED

    async def test_third_interruption_ends_in_error(self, store):
        """A job that keeps killing the process must not resume forever."""
        store.upsert(
            _make_job(
                id="job-1",
                status=JobStatus.DOWNLOADING,
                restart_attempts=MAX_RESTART_ATTEMPTS - 1,
            )
        )

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        job = qm.get_job("job-1")
        assert job.status == JobStatus.ERROR
        assert job.error == "interrupted by restart 3 times"
        assert job.error == RESTART_GIVE_UP_MESSAGE
        assert store.get("job-1").status == JobStatus.ERROR

    @patch("app.queue_manager.download_audio")
    async def test_two_interruptions_still_requeue(self, mock_download, store):
        store.upsert(
            _make_job(id="job-1", status=JobStatus.DOWNLOADING, restart_attempts=1)
        )

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        assert qm.get_job("job-1").status == JobStatus.QUEUED
        assert qm.get_job("job-1").restart_attempts == 2

    @patch("app.queue_manager.download_audio")
    async def test_manual_retries_do_not_spend_the_restart_budget(
        self, mock_download, store
    ):
        """Two retries plus one restart is not "interrupted by restart 3 times"."""
        store.upsert(
            _make_job(id="job-1", status=JobStatus.DOWNLOADING, attempts=2)
        )

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        job = qm.get_job("job-1")
        assert job.status == JobStatus.QUEUED
        assert job.attempts == 3
        assert job.restart_attempts == 1

    @patch("app.queue_manager.download_audio")
    async def test_a_retry_gives_the_restart_budget_back(self, mock_download, store):
        mock_download.side_effect = DownloadError("boom")
        store.upsert(
            _make_job(
                id="job-1",
                status=JobStatus.ERROR,
                restart_attempts=MAX_RESTART_ATTEMPTS - 1,
            )
        )

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()
        qm.retry_job("job-1")

        assert qm.get_job("job-1").restart_attempts == 0
        assert store.get("job-1").restart_attempts == 0

    @patch("app.queue_manager.download_audio")
    async def test_restore_runs_jobs_in_created_at_order(self, mock_download, store):
        started: list[str] = []

        def record(job, *args, **kwargs):
            started.append(job.id)
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = record

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in (2, 0, 1):
            store.upsert(
                _make_job(id=f"job-{index}", created_at=base + timedelta(minutes=index))
            )

        # One slot, so the jobs are dispatched strictly in order.
        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.restore_from_store()
        for index in range(3):
            await _wait_for_job_status(qm, f"job-{index}", JobStatus.DONE)

        assert started == ["job-0", "job-1", "job-2"]

    @patch("app.queue_manager.download_audio")
    async def test_restored_row_blocks_a_duplicate_submission(self, mock_download, store):
        """The duplicate check has to see the previous run's queue."""
        url = "https://www.youtube.com/watch?v=restored"
        store.upsert(_make_job(id="job-1", url=url, status=JobStatus.QUEUED))

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        with pytest.raises(QueueError, match="already in the queue"):
            qm.add_job(_make_job(id="job-2", url=url))

    @patch("app.queue_manager.download_audio")
    async def test_a_restored_error_row_does_not_block_resubmission(self, mock_download, store):
        url = "https://www.youtube.com/watch?v=restored"
        store.upsert(_make_job(id="job-1", url=url, status=JobStatus.ERROR))

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        with patch("app.queue_manager.download_audio"):
            assert qm.add_job(_make_job(id="job-2", url=url)) is not None

    async def test_restore_without_a_store_is_a_no_op(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        assert qm.restore_from_store() == []


class TestRestoreCleansTempDirectories:
    """An interrupted download leaves a scratch directory behind, nothing else."""

    # Real job ids are uuid4; the boot sweep only touches directories named
    # like one, so anything else here would not be swept at all.
    JOB_ID = str(uuid.uuid4())
    ORPHAN_ID = str(uuid.uuid4())

    @staticmethod
    def _seed_library(download_dir):
        """Files a user already had in the album folder before this job ran."""
        album_dir = download_dir / "An Artist" / "An Album"
        album_dir.mkdir(parents=True)
        for name in (
            "Test Track.mp3",
            "Test Track.2019.flac",
            "Test Track.lrc",
            "Test Track.flac",
            "Another Track.flac",
        ):
            (album_dir / name).write_text("mine")
        return album_dir

    @staticmethod
    def _seed_temp(download_dir, job_id):
        temp_dir = download_dir / ".tmp" / job_id
        temp_dir.mkdir(parents=True)
        for name in (
            "Test Track.webm.part",
            "Test Track.webm.ytdl",
            "Test Track.webm",
            "Test Track.webp",
        ):
            (temp_dir / name).write_text("x")
        return temp_dir

    @staticmethod
    def _interrupted(**overrides):
        defaults = {
            "id": TestRestoreCleansTempDirectories.JOB_ID,
            "status": JobStatus.DOWNLOADING,
            "title": "Test Track",
            "artist": "An Artist",
            "album": "An Album",
        }
        defaults.update(overrides)
        return _make_job(**defaults)

    @patch("app.queue_manager.download_audio")
    async def test_the_temp_directory_is_removed(
        self, mock_download, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        temp_dir = self._seed_temp(download_dir, TestRestoreCleansTempDirectories.JOB_ID)
        store.upsert(self._interrupted())

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        assert not temp_dir.exists()

    @patch("app.queue_manager.download_audio")
    async def test_the_users_library_files_survive(
        self, mock_download, store, isolated_paths
    ):
        """The old cleanup deleted every "Test Track.*" that was not the flac."""
        download_dir, _ = isolated_paths
        album_dir = self._seed_library(download_dir)
        self._seed_temp(download_dir, TestRestoreCleansTempDirectories.JOB_ID)
        store.upsert(self._interrupted())

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        assert {path.name for path in album_dir.iterdir()} == {
            "Test Track.mp3",
            "Test Track.2019.flac",
            "Test Track.lrc",
            "Test Track.flac",
            "Another Track.flac",
        }
        assert (album_dir / "Test Track.mp3").read_text() == "mine"

    @patch("app.queue_manager.download_audio")
    async def test_a_job_without_an_artist_still_gets_cleaned(
        self, mock_download, store, isolated_paths
    ):
        """The scratch path comes from the job id, not from artist/album."""
        download_dir, _ = isolated_paths
        fallback_dir = download_dir / "Unknown Artist" / "Unknown Album"
        fallback_dir.mkdir(parents=True)
        (fallback_dir / "Test Track.mp3").write_text("mine")
        temp_dir = self._seed_temp(download_dir, TestRestoreCleansTempDirectories.JOB_ID)
        store.upsert(self._interrupted(artist=None, album=None))

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        assert not temp_dir.exists()
        assert (fallback_dir / "Test Track.mp3").read_text() == "mine"

    @patch("app.queue_manager.download_audio")
    async def test_a_job_without_a_title_still_gets_cleaned(
        self, mock_download, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        temp_dir = self._seed_temp(download_dir, TestRestoreCleansTempDirectories.JOB_ID)
        store.upsert(self._interrupted(title=None))

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        assert not temp_dir.exists()

    @patch("app.queue_manager.download_audio")
    async def test_orphan_temp_directories_are_removed_at_boot(
        self, mock_download, store, isolated_paths
    ):
        """A crash can leave scratch dirs for jobs the store no longer knows."""
        download_dir, _ = isolated_paths
        self._seed_temp(download_dir, TestRestoreCleansTempDirectories.JOB_ID)
        orphan = self._seed_temp(download_dir, TestRestoreCleansTempDirectories.ORPHAN_ID)
        store.upsert(self._interrupted())

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        assert not orphan.exists()
        # Nothing is left, so the hidden root goes too.
        assert not (download_dir / ".tmp").exists()

    @patch("app.queue_manager.download_audio")
    async def test_a_missing_temp_directory_is_harmless(self, mock_download, store):
        store.upsert(_make_job(id=TestRestoreCleansTempDirectories.JOB_ID, status=JobStatus.DOWNLOADING))

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        assert qm.get_job(TestRestoreCleansTempDirectories.JOB_ID).status == JobStatus.QUEUED


class TestTimeoutDoesNotRaceTheDownloadThread:
    """A timed-out job's thread keeps running; cleanup belongs to that thread."""

    JOB_ID = str(uuid.uuid4())

    @patch("app.queue_manager.download_audio")
    async def test_a_late_finish_neither_records_a_path_nor_leaks_a_temp_dir(
        self, mock_download, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        temp_dir = download_dir / ".tmp" / self.JOB_ID
        result = download_dir / "Artist" / "Album" / "Title.flac"
        def slow_download(job, *args, on_filed=None, **kwargs):
            """Still converting and moving when the timeout has given up."""
            time.sleep(0.5)
            temp_dir.mkdir(parents=True, exist_ok=True)
            (temp_dir / "leftover.part").write_text("x")
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_text("flac")
            on_filed(FiledTrack(result, frozenset({result.parent, result.parent.parent})))
            return result

        mock_download.side_effect = slow_download

        qm = QueueManager(max_concurrent=2, timeout=0.2, store=store)
        qm.add_job(_make_job(id=self.JOB_ID))
        await _wait_for_job_status(qm, self.JOB_ID, JobStatus.ERROR)

        run = qm._active_runs.get(self.JOB_ID)
        while run is not None and not run.finished.is_set():
            await asyncio.sleep(0.01)

        assert qm.get_job(self.JOB_ID).status == JobStatus.ERROR
        assert qm.get_job(self.JOB_ID).result_path is None
        assert store.get(self.JOB_ID).result_path is None
        assert not temp_dir.exists()
        # The track goes back out of the library: the user was told the job
        # failed, and nothing in the queue would ever point at this file.
        assert not result.exists()
        assert not (download_dir / "Artist").exists()

    @staticmethod
    def _unfile_spy(callers: list[int]):
        """Wrap ``unfile_track``, recording which thread each call came from.

        Which side of the hand-off removed the track is the whole point of the
        two orderings below, and the thread id is what tells them apart from
        the outside.
        """

        def spy(filed: FiledTrack) -> None:
            callers.append(threading.get_ident())
            unfile_track(filed)

        return spy

    @patch("app.queue_manager.download_audio")
    async def test_a_track_filed_before_the_verdict_is_taken_back_by_the_loop(
        self, mock_download, store, isolated_paths
    ):
        """Ordering (a): the thread files, and only then does the timeout fire.

        The ordering is enforced, not raced: the timeout is held open until the
        run really has published its track, so the test says the same thing on
        a loaded machine as on an idle one.
        """
        download_dir, _ = isolated_paths
        result = download_dir / "Artist" / "Album" / "Title.flac"
        may_return = threading.Event()
        worker: dict[str, int] = {}
        callers: list[int] = []
        gate = asyncio.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            worker["ident"] = threading.get_ident()
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_text("flac")
            on_filed(
                FiledTrack(result, frozenset({result.parent, result.parent.parent}))
            )
            # Still inside download_audio when the timeout gives up on the job.
            may_return.wait(5)
            return result

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=0.2, store=store)
        with (
            patch("app.queue_manager.unfile_track", self._unfile_spy(callers)),
            patch("app.queue_manager.asyncio", _GatedAsyncio(gate)),
        ):
            qm.add_job(_make_job(id=self.JOB_ID))

            # Wait for the thread's track to be published, then let the
            # timeout fire -- ordering (a), with nothing left to the clock.
            while True:
                run = qm._active_runs.get(self.JOB_ID)
                if run is not None:
                    with run.lock:
                        if run.filed is not None:
                            break
                await asyncio.sleep(0.01)
            gate.set()

            await _wait_for_job_status(qm, self.JOB_ID, JobStatus.ERROR)
            while not callers:
                await asyncio.sleep(0.01)

        assert callers != [worker["ident"]], "the parked thread cannot have unfiled it"
        assert not result.exists()
        assert store.get(self.JOB_ID).result_path is None
        may_return.set()

    @patch("app.queue_manager.download_audio")
    async def test_a_track_filed_after_the_verdict_is_taken_back_by_the_thread(
        self, mock_download, store, isolated_paths
    ):
        """Ordering (b): the timeout fails the job, and only then does it file."""
        download_dir, _ = isolated_paths
        result = download_dir / "Artist" / "Album" / "Title.flac"
        may_file = threading.Event()
        worker: dict[str, int] = {}
        callers: list[int] = []

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            worker["ident"] = threading.get_ident()
            may_file.wait(5)  # released only once the job has already errored
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_text("flac")
            on_filed(
                FiledTrack(result, frozenset({result.parent, result.parent.parent}))
            )
            return result

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=0.2, store=store)
        with patch("app.queue_manager.unfile_track", self._unfile_spy(callers)):
            qm.add_job(_make_job(id=self.JOB_ID))
            await _wait_for_job_status(qm, self.JOB_ID, JobStatus.ERROR)
            may_file.set()
            while not callers:
                await asyncio.sleep(0.01)

        assert callers == [worker["ident"]], "the thread has to undo its own move"
        assert not result.exists()
        assert store.get(self.JOB_ID).result_path is None

    @patch("app.queue_manager.download_audio")
    async def test_a_track_filed_after_the_drain_grace_is_still_taken_back(
        self, mock_download, store, isolated_paths, caplog
    ):
        """The slot is released on a bound; the hand-off has none."""
        download_dir, _ = isolated_paths
        result = download_dir / "Artist" / "Album" / "Title.flac"
        may_file = threading.Event()
        callers: list[int] = []

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            may_file.wait(5)
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_text("flac")
            on_filed(
                FiledTrack(result, frozenset({result.parent, result.parent.parent}))
            )
            return result

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=0.2, store=store)
        with patch("app.queue_manager.unfile_track", self._unfile_spy(callers)):
            with caplog.at_level(logging.WARNING, logger="app.queue_manager"):
                with patch("app.queue_manager.THREAD_DRAIN_GRACE_SECONDS", 0.1):
                    qm.add_job(_make_job(id=self.JOB_ID))
                    await _wait_for_job_status(qm, self.JOB_ID, JobStatus.ERROR)
                    # The grace expires with the thread still inside the fake.
                    while "still running" not in caplog.text:
                        await asyncio.sleep(0.01)
            may_file.set()
            while not callers:
                await asyncio.sleep(0.01)

        assert not result.exists()
        assert store.get(self.JOB_ID).result_path is None


# ===========================================================================
# A terminal status is absorbing
# ===========================================================================


class TestTerminalStatusIsAbsorbing:
    """The timeout path fails a job without waiting for its thread, so the
    thread can report a phase a moment after the verdict was written.  Letting
    that through stranded the job in ``converting``: retry and dismiss both
    refuse that status, and cancel only signalled a token nobody was reading."""

    JOB_ID = str(uuid.uuid4())

    @patch("app.queue_manager.download_audio")
    async def test_a_late_converting_does_not_overwrite_the_error(
        self, mock_download, store
    ):
        events: list[SSEEvent] = []
        reported = threading.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            # The pre-check in on_phase reads the cancel flag a moment before it
            # acts on it; a stale False is what the download thread saw in the
            # real race, and pinning it here makes that instant reproducible.
            cancel.is_set = lambda: False
            while job.status != JobStatus.ERROR:
                time.sleep(0.01)
            on_phase("converting")
            reported.set()
            return "/data/music/Artist/Album/Title.flac"

        mock_download.side_effect = fake

        qm = QueueManager(
            max_concurrent=1, timeout=0.2, on_event=events.append, store=store
        )
        qm.add_job(_make_job(id=self.JOB_ID))
        await _wait_for_job_status(qm, self.JOB_ID, JobStatus.ERROR)
        while not reported.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

        assert qm.get_job(self.JOB_ID).status == JobStatus.ERROR
        assert store.get(self.JOB_ID).status == JobStatus.ERROR

        kinds = [event.event for event in events]
        after_error = [
            event
            for event in events[kinds.index("error") + 1 :]
            if event.event == "status_change"
        ]
        assert after_error == []

    @patch("app.queue_manager.download_audio")
    async def test_a_late_progress_or_metadata_is_dropped_too(
        self, mock_download, store
    ):
        """A late `metadata` persist would re-stamp finished_at on a job that
        has already ended, and a late `progress` would put a bar back on it."""
        events: list[SSEEvent] = []
        reported = threading.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            cancel.is_set = lambda: False
            while job.status != JobStatus.ERROR:
                time.sleep(0.01)
            on_progress(42.0)
            on_phase("metadata")
            reported.set()
            return "/data/music/Artist/Album/Title.flac"

        mock_download.side_effect = fake

        qm = QueueManager(
            max_concurrent=1, timeout=0.2, on_event=events.append, store=store
        )
        qm.add_job(_make_job(id=self.JOB_ID))
        await _wait_for_job_status(qm, self.JOB_ID, JobStatus.ERROR)
        finished_at = store.get(self.JOB_ID).finished_at
        while not reported.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

        kinds = [event.event for event in events]
        assert kinds[kinds.index("error") + 1 :] == []
        assert store.get(self.JOB_ID).finished_at == finished_at
        assert qm.get_job(self.JOB_ID).finished_at == finished_at


def _files(result) -> "callable":
    """A fake download that reports the move the way the real one does.

    ``result_path`` is read off what the run filed, not off the return value,
    because that is the value the timeout hand-off arbitrates -- so a stand-in
    for ``download_audio`` has to call ``on_filed`` like the real one.
    """

    def fake(job, on_progress=None, cancel=None, on_phase=None, on_filed=None, on_target=None):
        path = Path(result)
        on_filed(FiledTrack(path, frozenset({path.parent, path.parent.parent})))
        return path

    return fake


class TestResultPath:
    """Where the finished file landed, recorded relative to DOWNLOAD_PATH."""

    @patch("app.queue_manager.download_audio")
    async def test_result_path_is_stored_relative_to_the_library_root(
        self, mock_download, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        mock_download.side_effect = _files(
            download_dir / "Artist" / "Album" / "Title.flac"
        )

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert qm.get_job("job-1").result_path == "Artist/Album/Title.flac"
        assert store.get("job-1").result_path == "Artist/Album/Title.flac"

    @patch("app.queue_manager.download_audio")
    async def test_a_path_outside_the_root_leaves_result_path_unset(
        self, mock_download, store
    ):
        mock_download.side_effect = _files("/somewhere/else/Title.flac")

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert qm.get_job("job-1").result_path is None

    @patch("app.queue_manager.download_audio")
    async def test_a_failed_job_has_no_result_path(self, mock_download, store):
        mock_download.side_effect = DownloadError("boom")

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        assert store.get("job-1").result_path is None


class TestProcessJobCleansItsTempDirectory:
    """Whatever a run leaves in .tmp is debris once the job is over."""

    @staticmethod
    def _make_temp_dir(download_dir, job_id):
        temp_dir = download_dir / ".tmp" / job_id
        temp_dir.mkdir(parents=True)
        (temp_dir / "leftover.part").write_text("x")
        return temp_dir

    @patch("app.queue_manager.download_audio")
    async def test_removed_after_a_failure(self, mock_download, store, isolated_paths):
        download_dir, _ = isolated_paths
        temp_dir = self._make_temp_dir(download_dir, "job-1")
        mock_download.side_effect = DownloadError("boom")

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        assert not temp_dir.exists()

    @patch("app.queue_manager.download_audio")
    async def test_removed_after_success(self, mock_download, store, isolated_paths):
        download_dir, _ = isolated_paths
        temp_dir = self._make_temp_dir(download_dir, "job-1")
        mock_download.return_value = download_dir / "A" / "B" / "T.flac"

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert not temp_dir.exists()


# ===========================================================================
# Persistence: retention sweep
# ===========================================================================


class TestRetentionSweep:
    """Memory and table are bounded by age, not by a job count."""

    @staticmethod
    def _seed(store, job_id: str, status: JobStatus, age_days: float) -> None:
        finished = datetime.now(timezone.utc) - timedelta(days=age_days)
        store.upsert(
            _make_job(
                id=job_id, status=status, updated_at=finished, finished_at=finished
            )
        )

    def test_old_done_jobs_are_removed_from_table_and_memory(self, store):
        self._seed(store, "old", JobStatus.DONE, RETENTION_DAYS + 1)

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm._jobs["old"] = store.get("old")

        assert qm.sweep() == 1
        assert qm.get_job("old") is None
        assert store.get("old") is None

    def test_recent_done_jobs_are_kept(self, store):
        self._seed(store, "recent", JobStatus.DONE, 1)

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm._jobs["recent"] = store.get("recent")

        assert qm.sweep() == 0
        assert qm.get_job("recent") is not None

    async def test_error_jobs_are_never_swept(self, store):
        self._seed(store, "ancient", JobStatus.ERROR, 400)

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.restore_from_store()

        assert qm.sweep() == 0
        assert qm.get_job("ancient") is not None

    def test_sweep_works_without_a_store(self):
        """Memory-only mode applies the same rule to the dict."""
        qm = QueueManager(max_concurrent=2, timeout=10)
        old = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS + 1)
        qm._jobs["old"] = _make_job(
            id="old", status=JobStatus.DONE, updated_at=old, finished_at=old
        )
        qm._jobs["recent"] = _make_job(id="recent", status=JobStatus.DONE)

        assert qm.sweep() == 1
        assert qm.get_job("old") is None
        assert qm.get_job("recent") is not None


# ===========================================================================
# The queue view
# ===========================================================================


class TestQueueViewFiltering:
    """GET /queue is about what still needs attention."""

    @patch("app.queue_manager.download_audio")
    async def test_done_jobs_are_omitted(self, mock_download):
        mock_download.return_value = "/data/music/Artist/Album/track.flac"

        qm = QueueManager(max_concurrent=2, timeout=10)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert qm.get_jobs() == []
        # ...but the job itself is still there until the sweep drops it.
        assert qm.get_job("job-1") is not None

    def test_cancelled_jobs_are_omitted(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        qm._jobs["job-1"] = _make_job(id="job-1", status=JobStatus.CANCELLED)

        assert qm.get_jobs() == []

    @patch("app.queue_manager.download_audio")
    async def test_error_jobs_are_included(self, mock_download):
        mock_download.side_effect = DownloadError("boom")

        qm = QueueManager(max_concurrent=2, timeout=10)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        assert [job.id for job in qm.get_jobs()] == ["job-1"]

    def test_in_flight_jobs_are_included(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        for status in (
            JobStatus.QUEUED,
            JobStatus.DOWNLOADING,
            JobStatus.CONVERTING,
            JobStatus.TAGGING,
        ):
            qm._jobs[status.value] = _make_job(id=status.value, status=status)

        assert len(qm.get_jobs()) == 4


# ===========================================================================
# Cancel
# ===========================================================================


class TestCancelQueued:
    """A queued job has no thread to interrupt, so it just stops existing."""

    async def test_a_queued_job_is_cancelled_immediately(self, store):
        events: list[SSEEvent] = []
        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=events.append, store=store
        )
        job = _make_job()
        qm._jobs[job.id] = job  # not dispatched: no task to race with

        qm.cancel_job(job.id)

        assert job.status == JobStatus.CANCELLED
        assert job.error is None
        assert [e.event for e in events] == ["status_change"]

    async def test_the_row_is_written_before_the_event(self, store):
        """Write-through: a client that reads the table sees what it was told."""
        seen: list[str | None] = []
        qm = QueueManager(
            max_concurrent=1,
            timeout=10,
            on_event=lambda e: seen.append(store.get(e.job_id).status.value),
            store=store,
        )
        job = _make_job()
        qm._jobs[job.id] = job
        qm._persist(job)

        qm.cancel_job(job.id)

        assert seen == [JobStatus.CANCELLED.value]

    @patch("app.queue_manager.download_audio")
    async def test_a_job_cancelled_while_waiting_for_a_slot_never_starts(
        self, mock_download
    ):
        """The status re-check after acquiring the semaphore is what stops it."""
        import threading

        release = threading.Event()
        started: list[str] = []

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            started.append(job.id)
            release.wait(5)
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=10)
        qm.add_job(_make_job(id="job-running"))
        qm.add_job(_make_job(id="job-waiting"))
        await _wait_for_job_status(qm, "job-running", JobStatus.DOWNLOADING)

        qm.cancel_job("job-waiting")
        assert qm.get_job("job-waiting").status == JobStatus.CANCELLED

        release.set()
        await _wait_for_job_status(qm, "job-running", JobStatus.DONE, timeout=5.0)
        await asyncio.sleep(0.1)  # give the freed slot time to dispatch anything

        assert started == ["job-running"]

    async def test_a_cancelled_job_leaves_the_queue_view(self, store):
        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        job = _make_job()
        qm._jobs[job.id] = job

        qm.cancel_job(job.id)

        assert qm.get_jobs() == []
        assert qm.get_job(job.id).status == JobStatus.CANCELLED

    async def test_a_cancelled_job_cannot_be_retried(self, store):
        """The user resubmits instead, so the duplicate check runs again."""
        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        job = _make_job()
        qm._jobs[job.id] = job
        qm.cancel_job(job.id)

        with pytest.raises(QueueError, match="only ERROR jobs"):
            qm.retry_job(job.id)

    async def test_cancelling_an_unknown_job_is_not_found(self):
        qm = QueueManager(max_concurrent=1, timeout=10)

        with pytest.raises(JobNotFound):
            qm.cancel_job("no-such-job")

    @pytest.mark.parametrize(
        "status", [JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED]
    )
    async def test_cancelling_a_terminal_job_is_refused(self, status):
        """A Cancel on a finished job means the client's view is stale."""
        qm = QueueManager(max_concurrent=1, timeout=10)
        job = _make_job(status=status)
        qm._jobs[job.id] = job

        with pytest.raises(QueueError, match="only queued or running"):
            qm.cancel_job(job.id)

        assert qm.get_job(job.id).status == status


class TestCancelRunning:
    """A running job is signalled; its own thread decides the outcome."""

    @patch("app.queue_manager.download_audio")
    async def test_cancelling_a_download_ends_it_cancelled_not_errored(
        self, mock_download, store
    ):
        import threading

        cancelled_seen = threading.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            for _ in range(500):
                if cancel.is_set():
                    cancelled_seen.set()
                    raise DownloadError("Download cancelled")
                time.sleep(0.01)
            raise AssertionError("the cancel never reached the downloader")

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=30, store=store)
        qm.add_job(_make_job())
        await _wait_for_job_status(qm, "job-1", JobStatus.DOWNLOADING)

        qm.cancel_job("job-1")

        await _wait_for_job_status(qm, "job-1", JobStatus.CANCELLED, timeout=5.0)
        assert cancelled_seen.is_set()
        job = qm.get_job("job-1")
        assert job.error is None
        assert job.result_path is None
        assert store.get("job-1").status == JobStatus.CANCELLED

    @patch("app.queue_manager.download_audio")
    async def test_the_stop_button_exists_before_the_job_says_downloading(
        self, mock_download
    ):
        """Otherwise a Cancel in that window signals nothing and is lost."""
        mock_download.return_value = "/data/music/Artist/Album/track.flac"
        cancellable_when_announced: list[bool] = []

        def watch(event: SSEEvent) -> None:
            if event.data.get("status") == JobStatus.DOWNLOADING.value:
                cancellable_when_announced.append(
                    qm._active_runs.get(event.job_id) is not None
                )

        qm = QueueManager(max_concurrent=1, timeout=30, on_event=watch)
        qm.add_job(_make_job())

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE, timeout=5.0)
        assert cancellable_when_announced == [True]

    @patch("app.queue_manager.download_audio")
    async def test_a_run_cancelled_before_its_thread_starts_downloads_nothing(
        self, mock_download
    ):
        """The thread re-reads the stop button before it fetches a byte."""
        from app.queue_manager import _ActiveRun

        qm = QueueManager(max_concurrent=1, timeout=30)
        job = _make_job()
        qm._jobs[job.id] = job
        run = _ActiveRun()
        run.cancel.cancel()

        with pytest.raises(DownloadError, match="cancelled"):
            await qm._run_download(job.id, run)

        mock_download.assert_not_called()
        assert run.finished.is_set()

    @patch("app.queue_manager.download_audio")
    async def test_the_status_only_changes_once_the_thread_has_stopped(
        self, mock_download
    ):
        """The queue must not say `cancelled` while ffmpeg is still writing."""
        import threading

        release = threading.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            release.wait(5)
            raise DownloadError("Download cancelled")

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=30)
        qm.add_job(_make_job())
        await _wait_for_job_status(qm, "job-1", JobStatus.DOWNLOADING)

        qm.cancel_job("job-1")
        await asyncio.sleep(0.1)
        assert qm.get_job("job-1").status == JobStatus.DOWNLOADING

        release.set()
        await _wait_for_job_status(qm, "job-1", JobStatus.CANCELLED, timeout=5.0)

    @patch("app.queue_manager.download_audio")
    async def test_cancelling_during_converting_is_allowed(self, mock_download):
        import threading

        release = threading.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            on_phase("converting")
            release.wait(5)
            raise DownloadError("Download cancelled")

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=30)
        qm.add_job(_make_job())
        await _wait_for_job_status(qm, "job-1", JobStatus.CONVERTING)

        qm.cancel_job("job-1")
        release.set()

        await _wait_for_job_status(qm, "job-1", JobStatus.CANCELLED, timeout=5.0)

    @patch("app.queue_manager.download_audio")
    async def test_the_scratch_directory_is_gone_after_a_cancel(
        self, mock_download, isolated_paths
    ):
        import threading

        download_dir, _ = isolated_paths
        temp_dir = download_dir / ".tmp" / "job-1"
        release = threading.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            temp_dir.mkdir(parents=True, exist_ok=True)
            (temp_dir / "job-1.webm.part").write_text("half a download")
            release.wait(5)
            raise DownloadError("Download cancelled")

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=30)
        qm.add_job(_make_job())
        await _wait_for_job_status(qm, "job-1", JobStatus.DOWNLOADING)
        assert temp_dir.exists()

        qm.cancel_job("job-1")
        release.set()
        await _wait_for_job_status(qm, "job-1", JobStatus.CANCELLED, timeout=5.0)

        assert not temp_dir.exists()

    @patch("app.queue_manager.download_audio")
    async def test_a_genuine_failure_is_still_an_error_not_a_cancellation(
        self, mock_download
    ):
        mock_download.side_effect = DownloadError("HTTP Error 403: Forbidden")

        qm = QueueManager(max_concurrent=1, timeout=30)
        qm.add_job(_make_job())

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR, timeout=5.0)
        assert "403" in qm.get_job("job-1").error

    @patch("app.queue_manager.download_audio")
    async def test_a_cancel_that_loses_the_race_leaves_the_job_done_with_its_file(
        self, mock_download, store, isolated_paths
    ):
        """Never `cancelled` with a track sitting in the library."""
        import threading

        download_dir, _ = isolated_paths
        result = download_dir / "Artist" / "Album" / "Title.flac"
        filed = threading.Event()
        may_return = threading.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_bytes(b"flac")
            on_filed(FiledTrack(result, frozenset({result.parent})))
            filed.set()
            may_return.wait(5)  # the cancel lands in here, after the move
            return result

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=30, store=store)
        qm.add_job(_make_job())
        await _wait_for_job_status(qm, "job-1", JobStatus.DOWNLOADING)
        while not filed.is_set():
            await asyncio.sleep(0.01)

        qm.cancel_job("job-1")
        may_return.set()

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE, timeout=5.0)
        assert qm.get_job("job-1").result_path == "Artist/Album/Title.flac"
        assert result.exists()


# ===========================================================================
# Dismiss
# ===========================================================================


class TestDismiss:
    """Errored jobs are the only ones the sweep never drops, so this is how
    they leave."""

    async def test_an_errored_job_is_removed_from_the_dict_and_the_table(self, store):
        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        job = _make_job(status=JobStatus.ERROR, error="boom")
        qm._jobs[job.id] = job
        qm._persist(job)

        qm.dismiss_job(job.id)

        assert qm.get_job(job.id) is None
        assert store.get(job.id) is None
        assert qm.get_jobs() == []

    async def test_dismissing_an_unknown_job_is_not_found(self, store):
        qm = QueueManager(max_concurrent=1, timeout=10, store=store)

        with pytest.raises(JobNotFound):
            qm.dismiss_job("no-such-job")

    @pytest.mark.parametrize(
        "status",
        [
            JobStatus.QUEUED,
            JobStatus.DOWNLOADING,
            JobStatus.CONVERTING,
            JobStatus.DONE,
            JobStatus.CANCELLED,
        ],
    )
    async def test_only_errored_jobs_can_be_dismissed(self, status, store):
        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        job = _make_job(status=status)
        qm._jobs[job.id] = job

        with pytest.raises(QueueError, match="only errored jobs"):
            qm.dismiss_job(job.id)

        assert qm.get_job(job.id) is not None

    async def test_a_dismissed_job_does_not_come_back_after_a_restart(self, store):
        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        job = _make_job(status=JobStatus.ERROR, error="boom")
        qm._jobs[job.id] = job
        qm._persist(job)
        qm.dismiss_job(job.id)

        restarted = QueueManager(max_concurrent=1, timeout=10, store=store)
        assert restarted.restore_from_store() == []

    async def test_the_url_of_a_dismissed_job_can_be_submitted_again(self, store):
        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        job = _make_job(status=JobStatus.ERROR, error="boom")
        qm._jobs[job.id] = job
        qm._persist(job)

        qm.dismiss_job(job.id)

        assert qm.find_in_flight(job.url) is None


class TestTimeoutStopsTheConverter:
    """The timeout used to be able to give up waiting while ffmpeg carried on."""

    @patch("app.queue_manager.download_audio")
    async def test_a_timeout_during_converting_cancels_the_run(self, mock_download):
        import threading

        release = threading.Event()
        seen = {}

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            on_phase("converting")
            # Real code is blocked in ffmpeg's communicate() here; the token is
            # what reaches into that and signals the process.
            for _ in range(500):
                if cancel.is_set():
                    seen["cancelled_while_converting"] = True
                    break
                time.sleep(0.01)
            release.set()
            raise DownloadError("Download cancelled")

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=2, timeout=0.3)
        qm.add_job(_make_job())

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR, timeout=5.0)
        assert "timed out" in qm.get_job("job-1").error.lower()

        while not release.is_set():
            await asyncio.sleep(0.01)
        assert seen.get("cancelled_while_converting") is True

    @patch("app.queue_manager.download_audio")
    async def test_a_timed_out_job_stays_errored_when_its_thread_stops(
        self, mock_download
    ):
        """The thread's DownloadError must not turn a timeout into a cancel."""
        import threading

        stopped = threading.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            while not cancel.is_set():
                time.sleep(0.01)
            stopped.set()
            raise DownloadError("Download cancelled")

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=2, timeout=0.3)
        qm.add_job(_make_job())

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR, timeout=5.0)
        while not stopped.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)

        assert qm.get_job("job-1").status == JobStatus.ERROR


# ===========================================================================
# Cancel: what it costs the event loop, and what a restart makes of it
# ===========================================================================


class TestCancelDoesNotBlockTheEventLoop:
    """Cancel is a route handler: it may not park the loop on a child process."""

    @patch("app.queue_manager.download_audio")
    async def test_cancelling_a_wedged_ffmpeg_returns_at_once(self, mock_download):
        """The grace an ffmpeg that ignores SIGTERM is entitled to is waited
        out by the download thread, not by whoever pressed Cancel."""
        from tests.conftest import FakeFfmpegProcess

        registered = threading.Event()
        gate = threading.Event()  # never set: the fake encode hangs

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            process = FakeFfmpegProcess(
                ["ffmpeg"], gate, 0, b"", ignore_terminate=True
            )
            cancel.register_process(process)
            registered.set()
            while not cancel.is_set():
                time.sleep(0.005)
            process.kill()  # what _run_ffmpeg does once the grace expires
            raise DownloadError("Download cancelled")

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=30)
        qm.add_job(_make_job())
        await _wait_for_job_status(qm, "job-1", JobStatus.DOWNLOADING)
        assert registered.wait(5), "the fake ffmpeg was never registered"

        before = time.monotonic()
        qm.cancel_job("job-1")
        elapsed = time.monotonic() - before

        assert elapsed < 0.1, f"cancel_job blocked the loop for {elapsed:.2f}s"
        await _wait_for_job_status(qm, "job-1", JobStatus.CANCELLED, timeout=5.0)


class TestCancelSurvivesARestart:
    """A cancel takes as long as the thread takes; a restart in that window
    must finish the job, not resurrect it."""

    JOB_ID = str(uuid.uuid4())

    @patch("app.queue_manager.download_audio")
    async def test_a_restart_mid_cancel_finishes_the_job_cancelled(
        self, mock_download, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        release = threading.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            release.wait(5)  # the thread is still working when we "restart"
            raise DownloadError("Download cancelled")

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=30, store=store)
        qm.add_job(_make_job(id=self.JOB_ID))
        await _wait_for_job_status(qm, self.JOB_ID, JobStatus.DOWNLOADING)

        qm.cancel_job(self.JOB_ID)

        # The request is on disk before the thread has reported anything back.
        row = store.get(self.JOB_ID)
        assert row.cancel_requested is True
        assert row.status == JobStatus.DOWNLOADING

        # The process dies here: a fresh manager reloads the same database.
        restarted = QueueManager(max_concurrent=1, timeout=30, store=store)
        restarted.restore_from_store()

        assert restarted.get_job(self.JOB_ID).status == JobStatus.CANCELLED
        assert store.get(self.JOB_ID).status == JobStatus.CANCELLED
        mock_download.reset_mock()
        await asyncio.sleep(0.05)
        mock_download.assert_not_called()  # nothing was re-queued

        release.set()

    @patch("app.queue_manager.download_audio")
    async def test_the_restart_costs_the_cancelled_job_no_attempt(
        self, mock_download, store, isolated_paths
    ):
        """It is not a failed attempt: the restart did what the cancel asked."""
        download_dir, _ = isolated_paths
        temp_dir = download_dir / ".tmp" / self.JOB_ID
        temp_dir.mkdir(parents=True)
        (temp_dir / "Test Track.webm.part").write_text("x")
        store.upsert(
            _make_job(
                id=self.JOB_ID,
                status=JobStatus.DOWNLOADING,
                cancel_requested=True,
                attempts=1,
            )
        )

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.restore_from_store()

        job = qm.get_job(self.JOB_ID)
        assert job.status == JobStatus.CANCELLED
        assert job.attempts == 1
        assert job.restart_attempts == 0
        assert job.finished_at is not None
        assert not temp_dir.exists()

    @patch("app.queue_manager.download_audio")
    async def test_a_cancel_that_lost_its_race_is_not_left_on_the_done_row(
        self, mock_download, store, isolated_paths
    ):
        """Otherwise a restart would read the flag as "the user stopped this"."""
        download_dir, _ = isolated_paths
        result = download_dir / "Artist" / "Album" / "Title.flac"
        filed = threading.Event()
        may_return = threading.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_text("flac")
            on_filed(FiledTrack(result, frozenset({result.parent})))
            filed.set()
            may_return.wait(5)  # the cancel lands in here, after the move
            return result

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=30, store=store)
        qm.add_job(_make_job(id=self.JOB_ID))
        await _wait_for_job_status(qm, self.JOB_ID, JobStatus.DOWNLOADING)
        while not filed.is_set():
            await asyncio.sleep(0.01)

        qm.cancel_job(self.JOB_ID)
        assert store.get(self.JOB_ID).cancel_requested is True
        may_return.set()

        await _wait_for_job_status(qm, self.JOB_ID, JobStatus.DONE, timeout=5.0)
        assert store.get(self.JOB_ID).cancel_requested is False
        assert qm.get_job(self.JOB_ID).cancel_requested is False

    @patch("app.queue_manager.download_audio")
    async def test_a_retried_job_is_no_longer_a_job_the_user_asked_to_stop(
        self, mock_download, store
    ):
        """A cancel can lose to a genuine failure; the row would keep the flag."""
        mock_download.side_effect = DownloadError("boom")

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_job(_make_job(id=self.JOB_ID))
        await _wait_for_job_status(qm, self.JOB_ID, JobStatus.ERROR)
        qm.get_job(self.JOB_ID).cancel_requested = True

        mock_download.side_effect = None
        mock_download.return_value = "/data/music/Artist/Album/track.flac"
        qm.retry_job(self.JOB_ID)

        assert store.get(self.JOB_ID).cancel_requested is False


# ===========================================================================
# The timeout does not release a slot the thread is still using
# ===========================================================================


class TestTimeoutWaitsForItsThread:
    """The semaphore counts download threads, so it may not be handed on while
    the timed-out one is still holding its scratch directory and its ffmpeg."""

    @patch("app.queue_manager.download_audio")
    async def test_the_next_job_waits_for_the_timed_out_thread_to_stop(
        self, mock_download
    ):
        release = threading.Event()
        started: list[str] = []

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            started.append(job.id)
            release.wait(5)  # ignores the cancel, like a wedged conversion
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=0.2)
        qm.add_job(_make_job(id="job-0"))
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-0", JobStatus.ERROR, timeout=5.0)

        await asyncio.sleep(0.2)
        assert qm.get_job("job-1").status == JobStatus.QUEUED
        assert started == ["job-0"]

        release.set()
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE, timeout=5.0)
        assert started == ["job-0", "job-1"]

    @patch("app.queue_manager.download_audio")
    async def test_a_thread_that_outlives_the_grace_does_not_stall_the_queue(
        self, mock_download, caplog
    ):
        """Bounded: one stuck thread must not close the queue for good."""
        release = threading.Event()

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            release.wait(10)
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=0.2)
        with caplog.at_level(logging.WARNING, logger="app.queue_manager"):
            with patch("app.queue_manager.THREAD_DRAIN_GRACE_SECONDS", 0.2):
                qm.add_job(_make_job(id="job-0"))
                qm.add_job(_make_job(id="job-1"))
                await _wait_for_job_status(
                    qm, "job-1", JobStatus.DOWNLOADING, timeout=5.0
                )

        assert "still running" in caplog.text
        release.set()

    @patch("app.queue_manager.download_audio")
    async def test_the_drain_does_not_need_a_free_executor_thread(self, mock_download):
        """It used to wait in the same pool the download threads run in, so a
        full pool meant the wait could not start and the slot never came back."""
        release = threading.Event()
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=1)
        )

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            release.wait(10)  # the pool's only worker, and it ignores the cancel
            return "/data/music/Artist/Album/track.flac"

        mock_download.side_effect = fake

        qm = QueueManager(max_concurrent=1, timeout=0.2)
        try:
            with patch("app.queue_manager.THREAD_DRAIN_GRACE_SECONDS", 0.2):
                qm.add_job(_make_job(id="job-0"))
                qm.add_job(_make_job(id="job-1"))
                await _wait_for_job_status(
                    qm, "job-1", JobStatus.DOWNLOADING, timeout=2.0
                )
        finally:
            release.set()

    def test_the_grace_covers_the_worker_killing_a_wedged_ffmpeg(self):
        """Otherwise the slot would be released while ffmpeg was still alive."""
        from app.downloader import FFMPEG_TERMINATE_GRACE_SECONDS

        assert THREAD_DRAIN_GRACE_SECONDS > FFMPEG_TERMINATE_GRACE_SECONDS


# ===========================================================================
# Dispatching a job's processing task
# ===========================================================================


class TestDispatch:
    """asyncio keeps only a weak reference to a running task."""

    async def test_a_running_task_is_held_and_released_when_it_finishes(self):
        qm = QueueManager(max_concurrent=1, timeout=10)
        started = asyncio.Event()
        finish = asyncio.Event()

        async def parked(job_id: str) -> None:
            started.set()
            await finish.wait()

        qm._process_job = parked

        qm._dispatch("job-1")
        await started.wait()
        assert len(qm._tasks) == 1

        finish.set()
        await asyncio.sleep(0.05)
        assert qm._tasks == set()

    async def test_an_exception_that_escapes_processing_names_its_job(self, caplog):
        qm = QueueManager(max_concurrent=1, timeout=10)

        async def explode(job_id: str) -> None:
            raise RuntimeError("boom")

        qm._process_job = explode

        with caplog.at_level(logging.ERROR, logger="app.queue_manager"):
            qm._dispatch("job-1")
            await asyncio.sleep(0.05)

        assert "job-1" in caplog.text
        assert "boom" in caplog.text


# ===========================================================================
# library_changed
# ===========================================================================


class TestLibraryChanged:
    """A job that wrote a file tells every client the library moved on."""

    JOB_ID = str(uuid.uuid4())

    @staticmethod
    def _files(download_dir: Path, relative: str):
        """A fake download that files *relative* under the library root."""
        result = download_dir / relative

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_text("flac")
            on_filed(
                FiledTrack(result, frozenset({result.parent, result.parent.parent}))
            )
            return result

        return fake

    @patch("app.queue_manager.download_audio")
    async def test_it_follows_the_file_landing_and_carries_the_relative_path(
        self, mock_download, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        mock_download.side_effect = self._files(
            download_dir, "Bonobo/Migration/Kerala.flac"
        )
        events: list[SSEEvent] = []

        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=events.append, store=store
        )
        qm.add_job(_make_job(id=self.JOB_ID))

        await _wait_for_job_status(qm, self.JOB_ID, JobStatus.DONE)

        # The event announces the *file*, so it follows the transition that
        # says the file exists -- into `tagging`, which is where a download now
        # goes once its FLAC is filed -- rather than the end of the job.  The
        # fix that follows changes nothing here (the fake wrote four bytes that
        # are not a FLAC), so there is exactly one.
        names = [e.event for e in events]
        assert names == [
            "status_change",  # downloading
            "status_change",  # tagging
            "library_changed",
            "status_change",  # done
        ], names
        assert events[1].data["status"] == JobStatus.TAGGING.value
        assert events[-1].data["status"] == JobStatus.DONE.value
        assert store.get(self.JOB_ID).status == JobStatus.DONE

        changed = events[2]
        assert changed.job_id == self.JOB_ID
        assert changed.data["paths"] == ["Bonobo/Migration/Kerala.flac"]

    @patch("app.queue_manager.download_audio")
    async def test_a_failed_job_changes_nothing(self, mock_download, store):
        mock_download.side_effect = DownloadError("boom")
        events: list[SSEEvent] = []

        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=events.append, store=store
        )
        job = _make_job(id=str(uuid.uuid4()))
        qm.add_job(job)

        await _wait_for_job_status(qm, job.id, JobStatus.ERROR)

        assert [e for e in events if e.event == "library_changed"] == []

    @patch("app.queue_manager.download_audio")
    async def test_a_cancelled_job_changes_nothing(self, mock_download, store):
        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            for _ in range(500):
                if cancel.is_set():
                    raise DownloadError("Download cancelled")
                time.sleep(0.01)
            raise AssertionError("the cancel never reached the downloader")

        mock_download.side_effect = fake
        events: list[SSEEvent] = []

        qm = QueueManager(
            max_concurrent=1, timeout=30, on_event=events.append, store=store
        )
        job = _make_job(id=str(uuid.uuid4()))
        qm.add_job(job)
        await _wait_for_job_status(qm, job.id, JobStatus.DOWNLOADING)

        qm.cancel_job(job.id)
        await _wait_for_job_status(qm, job.id, JobStatus.CANCELLED)

        assert [e for e in events if e.event == "library_changed"] == []

    @patch("app.queue_manager.download_audio")
    async def test_a_path_outside_the_library_root_sends_an_empty_list(
        self, mock_download, store, isolated_paths, tmp_path
    ):
        """result_path stays unset, but the library still changed as far as we
        know, so the event goes out saying only that."""
        stray = tmp_path / "elsewhere" / "Title.flac"

        def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            stray.parent.mkdir(parents=True, exist_ok=True)
            stray.write_text("flac")
            on_filed(FiledTrack(stray, frozenset({stray.parent})))
            return stray

        mock_download.side_effect = fake
        events: list[SSEEvent] = []

        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=events.append, store=store
        )
        job = _make_job(id=str(uuid.uuid4()))
        qm.add_job(job)

        await _wait_for_job_status(qm, job.id, JobStatus.DONE)

        changed = [e for e in events if e.event == "library_changed"]
        assert len(changed) == 1
        assert changed[0].data["paths"] == []

    async def test_the_emitter_takes_changes_no_job_caused(self):
        """Later phases call it for moves, trash, restore, and tag writes."""
        events: list[SSEEvent] = []
        qm = QueueManager(max_concurrent=1, timeout=10, on_event=events.append)

        qm.emit_library_changed(["Bonobo/Migration", "Bonobo/Black Sands"])

        assert len(events) == 1
        assert events[0].event == "library_changed"
        assert events[0].job_id is None
        assert events[0].data["paths"] == [
            "Bonobo/Migration",
            "Bonobo/Black Sands",
        ]

    async def test_the_emitter_is_silent_without_a_callback(self):
        QueueManager(max_concurrent=1, timeout=10).emit_library_changed([])


# ===========================================================================
# The tagging stage (phase 8)
# ===========================================================================


def _filing_download(download_dir: Path, relative: str, *, convert: bool = True):
    """A fake download that files *relative* under the library root.

    Writes a real (empty) FLAC so the file the tagger is handed is a file the
    tagger could open, and reports the ``converting`` phase on the way, which
    is what puts a full ``downloading -> converting -> tagging -> done``
    sequence on the wire.

    ``{job}`` in *relative* is replaced with the job's id, which is how a test
    with two jobs tells their files (and so their tag fixes) apart.
    """

    def fake(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
        result = download_dir / relative.replace("{job}", job.id)
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_bytes(minimal_flac_bytes(44100 * 183))
        if convert and on_phase is not None:
            on_phase("converting")
        if on_filed is not None:
            on_filed(FiledTrack(result, frozenset({result.parent, result.parent.parent})))
        return result

    return fake


def _fix_result(**kwargs) -> TagFixResult:
    return TagFixResult(**kwargs)


class TestTaggingStage:
    """A finished download waits for the tagging worker, then ends `done`."""

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_the_status_sequence(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        mock_download.side_effect = _filing_download(
            download_dir, "Bonobo/Migration/Kerala.flac"
        )
        mock_fix.return_value = _fix_result(matched=True, changed=True)
        events: list[SSEEvent] = []

        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=events.append, store=store
        )
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        statuses = [
            event.data["status"] for event in events if event.event == "status_change"
        ]
        assert statuses == ["downloading", "converting", "tagging", "done"]
        assert qm.get_job("job-1").detail is None

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_the_fix_is_asked_about_the_filed_track(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        """It gets the absolute path and the *folder* artist -- the library's
        own answer to whose track this is."""
        download_dir, _ = isolated_paths
        mock_download.side_effect = _filing_download(
            download_dir, "Bonobo/Migration/Kerala.flac"
        )
        mock_fix.return_value = _fix_result(matched=True, changed=False)

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        args, kwargs = mock_fix.call_args
        assert args[0] == download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        assert args[1] == "Bonobo"
        assert callable(kwargs["should_cancel"])

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_the_download_slot_is_free_while_a_job_is_tagging(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        """The acceptance criterion: one download slot, and the second job is
        already downloading while the first one is still being tagged."""
        download_dir, _ = isolated_paths
        released = threading.Event()

        def slow_fix(*args, **kwargs):
            released.wait(timeout=5)
            return _fix_result(matched=True, changed=False)

        mock_fix.side_effect = slow_fix
        mock_download.side_effect = _filing_download(
            download_dir, "Bonobo/Migration/Kerala.flac"
        )

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.TAGGING)

        # The slot the first job held is back, with nobody in it.
        assert qm._semaphore._value == 1

        # The second job runs its whole download while the first one is still
        # being tagged, and stops at `tagging` only because the *tagging* lock
        # is held -- never because a download slot was.
        qm.add_job(_make_job(id="job-2"))
        await _wait_for_job_status(qm, "job-2", JobStatus.TAGGING)
        assert qm.get_job("job-1").status is JobStatus.TAGGING

        released.set()
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_two_jobs_are_tagged_one_after_the_other(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        """Two downloads finishing together must never talk to MusicBrainz at
        the same time: the tagging lock is the app's whole rate limit."""
        download_dir, _ = isolated_paths
        mock_download.side_effect = _filing_download(
            download_dir, "Bonobo/Migration/Kerala.flac"
        )
        inside = 0
        overlaps: list[str] = []
        first_in = threading.Event()
        let_go = threading.Event()

        def counting_fix(*args, **kwargs):
            nonlocal inside
            inside += 1
            if inside > 1:
                overlaps.append("two fixes ran at once")
            first_in.set()
            let_go.wait(timeout=5)
            inside -= 1
            return _fix_result(matched=True, changed=False)

        mock_fix.side_effect = counting_fix

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        qm.add_job(_make_job(id="job-2"))

        await _wait_for_job_status(qm, "job-1", JobStatus.TAGGING)
        await _wait_for_job_status(qm, "job-2", JobStatus.TAGGING)
        await asyncio.to_thread(first_in.wait, 5)
        # The second job is in `tagging` but has not started its fix: it is
        # queued on the lock.
        assert mock_fix.call_count == 1

        let_go.set()
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)
        await _wait_for_job_status(qm, "job-2", JobStatus.DONE)

        assert overlaps == []
        assert mock_fix.call_count == 2

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_a_failed_lookup_still_finishes_the_job(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        """MusicBrainz down: the job is `done`, the note says why, and the file
        is exactly as the download left it."""
        download_dir, _ = isolated_paths
        mock_download.side_effect = _filing_download(
            download_dir, "Bonobo/Migration/Kerala.flac"
        )
        mock_fix.return_value = _fix_result(note=NOTE_UNAVAILABLE)
        flac = download_dir / "Bonobo" / "Migration" / "Kerala.flac"

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        job = qm.get_job("job-1")
        assert job.detail == NOTE_UNAVAILABLE
        assert job.error is None
        assert store.get("job-1").detail == NOTE_UNAVAILABLE
        assert flac.exists()
        assert flac.read_bytes() == minimal_flac_bytes(44100 * 183)

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_a_hung_lookup_times_out_and_the_job_still_finishes(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        mock_download.side_effect = _filing_download(
            download_dir, "Bonobo/Migration/Kerala.flac"
        )
        stuck = threading.Event()

        def never_returns(*args, **kwargs):
            stuck.wait(timeout=5)
            return _fix_result(matched=True, changed=True)

        mock_fix.side_effect = never_returns

        qm = QueueManager(
            max_concurrent=1, timeout=10, store=store, tag_fix_timeout=0.05
        )
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert qm.get_job("job-1").detail == NOTE_TIMED_OUT
        stuck.set()  # let the abandoned thread unwind

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_a_tag_write_announces_the_library_again(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        mock_download.side_effect = _filing_download(
            download_dir, "Bonobo/Migration/Kerala.flac"
        )
        mock_fix.return_value = _fix_result(matched=True, changed=True)
        events: list[SSEEvent] = []

        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=events.append, store=store
        )
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        changed = [event for event in events if event.event == "library_changed"]
        assert len(changed) == 2, [event.event for event in events]
        assert all(
            event.data["paths"] == ["Bonobo/Migration/Kerala.flac"] for event in changed
        )
        # The second one follows the `done` transition, whose row was written
        # first, so a client that refetches on it never sees the job in flight.
        assert events[-1].event == "library_changed"
        assert store.get("job-1").status is JobStatus.DONE

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_a_fix_that_changed_nothing_announces_nothing(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        mock_download.side_effect = _filing_download(
            download_dir, "Bonobo/Migration/Kerala.flac"
        )
        mock_fix.return_value = _fix_result(matched=True, changed=False)
        events: list[SSEEvent] = []

        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=events.append, store=store
        )
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        changed = [event for event in events if event.event == "library_changed"]
        assert len(changed) == 1

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_the_tagging_row_is_written_before_its_event(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        """Write-through, asserted inside the callback so the ordering is
        real rather than inferred."""
        download_dir, _ = isolated_paths
        mock_download.side_effect = _filing_download(
            download_dir, "Bonobo/Migration/Kerala.flac"
        )
        mock_fix.return_value = _fix_result(note=NOTE_UNAVAILABLE)
        mismatches: list[str] = []

        def check(event: SSEEvent) -> None:
            if event.event != "status_change":
                return
            row = store.get(event.job_id)
            if row is None or row.status.value != event.data["status"]:
                mismatches.append(str(event.data["status"]))
            if event.data["status"] == "done" and row.detail != NOTE_UNAVAILABLE:
                mismatches.append("the note reached the client before the row")

        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=check, store=store
        )
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        assert mismatches == []

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_the_note_is_in_the_status_change_event(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        mock_download.side_effect = _filing_download(
            download_dir, "Bonobo/Migration/Kerala.flac"
        )
        mock_fix.return_value = _fix_result(note=NOTE_NO_MATCH)
        events: list[SSEEvent] = []

        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=events.append, store=store
        )
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        done = [
            event
            for event in events
            if event.event == "status_change" and event.data["status"] == "done"
        ]
        assert done[-1].data["detail"] == NOTE_NO_MATCH

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_a_job_that_filed_nothing_never_reaches_tagging(
        self, mock_download, mock_fix, store
    ):
        """No `result_path` means no file to fix, so the job is done at the end
        of the download rather than passing through a stage with no work."""
        mock_download.return_value = "/data/music/Artist/Album/track.flac"

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        mock_fix.assert_not_called()
        assert qm.get_job("job-1").detail is None


class TestCancelDuringTagging:
    """Cancelling a tag fix cancels the fix, not the download."""

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_the_job_ends_done_with_the_file_intact(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        mock_download.side_effect = _filing_download(
            download_dir, "Bonobo/Migration/Kerala.flac"
        )
        flac = download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        started = threading.Event()

        def watch_for_the_cancel(path, artist, should_cancel=None):
            """Stand in for the real fix's checkpoints: poll until the cancel
            arrives, then report that it did."""
            started.set()
            for _ in range(500):
                if should_cancel():
                    return _fix_result(note=NOTE_CANCELLED)
                time.sleep(0.01)
            raise AssertionError("the cancel never reached the tag fix")

        mock_fix.side_effect = watch_for_the_cancel

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.TAGGING)
        await asyncio.to_thread(started.wait, 5)

        qm.cancel_job("job-1")
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        job = qm.get_job("job-1")
        assert job.detail == NOTE_CANCELLED
        assert job.status is JobStatus.DONE  # not `cancelled`: the track exists
        assert flac.exists()

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_a_job_waiting_for_the_lock_skips_its_fix_entirely(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        mock_download.side_effect = _filing_download(
            download_dir, "Bonobo/Migration/{job}.flac"
        )
        first_in = threading.Event()
        let_go = threading.Event()
        tagged: list[str] = []

        def slow_fix(path, artist, should_cancel=None):
            tagged.append(str(path))
            first_in.set()
            let_go.wait(timeout=5)
            return _fix_result(matched=True, changed=False)

        mock_fix.side_effect = slow_fix

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        qm.add_job(_make_job(id="job-2"))
        await _wait_for_job_status(qm, "job-1", JobStatus.TAGGING)
        await _wait_for_job_status(qm, "job-2", JobStatus.TAGGING)
        await asyncio.to_thread(first_in.wait, 5)

        # Whichever job did not get the lock is the one still waiting for it.
        waiting = "job-2" if "job-1" in tagged[0] else "job-1"
        qm.cancel_job(waiting)
        let_go.set()

        await _wait_for_job_status(qm, waiting, JobStatus.DONE)
        assert qm.get_job(waiting).detail == NOTE_CANCELLED
        # One fix ran in total: the cancelled job never talked to MusicBrainz.
        assert len(tagged) == 1

    async def test_cancelling_a_tagging_job_is_still_allowed(self, store):
        """The route's 400-for-terminal check must keep accepting `tagging`."""
        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        job = _make_job(id="job-1", status=JobStatus.TAGGING)
        qm._jobs[job.id] = job

        qm.cancel_job("job-1")

        assert job.cancel_requested is True
        assert store.get("job-1").cancel_requested is True


class TestRestoreDuringTagging:
    """A restart during the fix re-runs only the fix."""

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_only_the_tag_fix_is_re_run(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        flac = download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        flac.parent.mkdir(parents=True)
        flac.write_bytes(minimal_flac_bytes(44100 * 183))
        mock_fix.return_value = _fix_result(matched=True, changed=True)
        store.upsert(
            _make_job(
                id="job-1",
                status=JobStatus.TAGGING,
                result_path="Bonobo/Migration/Kerala.flac",
            )
        )

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        restored = qm.restore_from_store()

        assert [job.status for job in restored] == [JobStatus.TAGGING]
        await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        mock_download.assert_not_called()
        assert mock_fix.call_count == 1
        # A restart during a metadata lookup is not a dangerous interruption.
        assert qm.get_job("job-1").restart_attempts == 0
        assert qm.get_job("job-1").attempts == 0

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_a_missing_file_finishes_the_job(
        self, mock_download, mock_fix, store
    ):
        store.upsert(
            _make_job(
                id="job-1",
                status=JobStatus.TAGGING,
                result_path="Bonobo/Migration/Gone.flac",
            )
        )

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.restore_from_store()

        job = qm.get_job("job-1")
        assert job.status is JobStatus.DONE
        assert job.detail == NOTE_FILE_MISSING
        mock_download.assert_not_called()
        mock_fix.assert_not_called()

    @patch("app.queue_manager.download_audio")
    async def test_a_row_with_no_result_path_finishes_the_job(
        self, mock_download, store
    ):
        store.upsert(_make_job(id="job-1", status=JobStatus.TAGGING))

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.restore_from_store()

        assert qm.get_job("job-1").status is JobStatus.DONE
        assert qm.get_job("job-1").detail == NOTE_FILE_MISSING
        mock_download.assert_not_called()

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_a_cancel_that_the_restart_interrupted_ends_done(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        """Not `cancelled`: the track is in the library either way, and a
        restart is not the moment to start claiming otherwise."""
        download_dir, _ = isolated_paths
        flac = download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        flac.parent.mkdir(parents=True)
        flac.write_bytes(minimal_flac_bytes(44100 * 183))
        store.upsert(
            _make_job(
                id="job-1",
                status=JobStatus.TAGGING,
                result_path="Bonobo/Migration/Kerala.flac",
                cancel_requested=True,
            )
        )

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.restore_from_store()

        job = qm.get_job("job-1")
        assert job.status is JobStatus.DONE
        assert job.detail == NOTE_CANCELLED
        mock_fix.assert_not_called()
        assert flac.exists()


# ===========================================================================
# The tagging thread
# ===========================================================================


class TestTheTaggingThread:
    """The tag fix runs on the queue's own single worker, and a lookup the
    queue has given up on can neither write nor block the next job."""

    async def test_the_fix_runs_off_the_shared_executor(self, isolated_paths):
        """A lookup that has to be abandoned must not be able to strand a
        thread the downloads and the library routes are sharing."""
        download_dir, _ = isolated_paths
        flac = download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        flac.parent.mkdir(parents=True)
        flac.write_bytes(minimal_flac_bytes(44100 * 183))
        seen: list[str] = []

        def note_the_thread(*args, **kwargs):
            seen.append(threading.current_thread().name)
            return TagFixResult(matched=False, note=NOTE_NO_MATCH)

        qm = QueueManager(max_concurrent=1, timeout=10)
        job = _make_job(
            id="job-1",
            status=JobStatus.TAGGING,
            result_path="Bonobo/Migration/Kerala.flac",
        )
        qm._jobs["job-1"] = job

        with patch("app.queue_manager.fix_track", note_the_thread):
            await qm._run_tagging_stage("job-1")

        assert len(seen) == 1
        assert seen[0].startswith("tag-fix")
        qm.close()

    async def test_the_fix_thread_is_a_daemon(self, isolated_paths):
        """A lookup parked in urllib must not be joined at interpreter exit:
        that is what held the container open past its stop grace and turned an
        orderly shutdown into a SIGKILL."""
        download_dir, _ = isolated_paths
        flac = download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        flac.parent.mkdir(parents=True)
        flac.write_bytes(minimal_flac_bytes(44100 * 183))
        threads: list[threading.Thread] = []

        def note_the_thread(*args, **kwargs):
            threads.append(threading.current_thread())
            return TagFixResult(matched=False, note=NOTE_NO_MATCH)

        qm = QueueManager(max_concurrent=1, timeout=10)
        qm._jobs["job-1"] = _make_job(
            id="job-1",
            status=JobStatus.TAGGING,
            result_path="Bonobo/Migration/Kerala.flac",
        )

        with patch("app.queue_manager.fix_track", note_the_thread):
            await qm._run_tagging_stage("job-1")

        assert len(threads) == 1
        assert threads[0].daemon is True, "the fix thread would be joined at exit"
        qm.close()

    async def test_close_refuses_a_fix_for_good(self, isolated_paths):
        """``close`` is irreversible: nothing re-opens the queue, so a job
        that reaches its fix afterwards is answered, never started."""
        download_dir, _ = isolated_paths
        flac = download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        flac.parent.mkdir(parents=True)
        flac.write_bytes(minimal_flac_bytes(44100 * 183))

        qm = QueueManager(max_concurrent=1, timeout=10)
        qm.close()
        qm.close()  # idempotent

        for job_id in ("job-1", "job-2"):
            qm._jobs[job_id] = _make_job(
                id=job_id,
                status=JobStatus.TAGGING,
                result_path="Bonobo/Migration/Kerala.flac",
            )

        with patch("app.queue_manager.fix_track") as mock_fix:
            await qm._run_tagging_stage("job-1")
            await qm._run_tagging_stage("job-2")

        assert mock_fix.call_count == 0
        assert qm.get_job("job-2").detail == NOTE_CANCELLED

    async def test_an_abandoned_lookup_does_not_rewrite_the_tags(
        self, isolated_paths
    ):
        """The thread comes back minutes after the job was answered `done`.
        By then the queue has told every client the track is finished, so the
        one thing it must not do is quietly change the file underneath them."""
        from mutagen.flac import FLAC

        from app.tagger import Candidate, fix_track

        download_dir, _ = isolated_paths
        flac = download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        flac.parent.mkdir(parents=True)
        flac.write_bytes(minimal_flac_bytes(44100 * 183))
        audio = FLAC(flac)
        audio["TITLE"] = ["Kerala (Official Video)"]
        audio["ARTIST"] = ["SomeChannel"]
        audio.save()
        before = flac.read_bytes()

        released = threading.Event()

        def stalled_search(title, artist, duration, limit=None):
            # The MusicBrainz request that never came back in time.
            released.wait(timeout=5)
            return [
                Candidate(
                    title="Kerala",
                    artist_credit="Bonobo",
                    artist_names=("Bonobo",),
                    length_ms=183_000,
                )
            ]

        def real_fix_with_a_stalled_lookup(path, folder_artist, **kwargs):
            return fix_track(path, folder_artist, search=stalled_search, **kwargs)

        qm = QueueManager(max_concurrent=1, timeout=10, tag_fix_timeout=0.1)
        job = _make_job(
            id="job-1",
            status=JobStatus.TAGGING,
            result_path="Bonobo/Migration/Kerala.flac",
        )
        qm._jobs["job-1"] = job

        with patch("app.queue_manager.fix_track", real_fix_with_a_stalled_lookup):
            await qm._run_tagging_stage("job-1")

            assert job.status is JobStatus.DONE
            assert job.detail == NOTE_TIMED_OUT
            assert flac.read_bytes() == before

            # The lookup finally answers, with a match it would have written.
            released.set()
            deadline = time.monotonic() + 5
            while qm._stuck_tag_fix is not None and time.monotonic() < deadline:
                await asyncio.sleep(0.01)

        assert qm._stuck_tag_fix is None, "the worker was never released"
        assert flac.read_bytes() == before, "the abandoned lookup rewrote the tags"
        assert FLAC(flac)["TITLE"] == ["Kerala (Official Video)"]
        qm.close()

    @patch("app.queue_manager.fix_track")
    async def test_the_next_job_is_not_made_to_wait_for_a_stuck_lookup(
        self, mock_fix, isolated_paths
    ):
        """Otherwise an outage costs every later job the full timeout each."""
        download_dir, _ = isolated_paths
        released = threading.Event()

        def never_returns(*args, **kwargs):
            released.wait(timeout=5)
            return TagFixResult(matched=False, note=NOTE_NO_MATCH)

        mock_fix.side_effect = never_returns

        qm = QueueManager(max_concurrent=1, timeout=10, tag_fix_timeout=0.3)
        for job_id in ("job-1", "job-2"):
            flac = download_dir / "Bonobo" / "Migration" / f"{job_id}.flac"
            flac.parent.mkdir(parents=True, exist_ok=True)
            flac.write_bytes(minimal_flac_bytes(44100 * 183))
            qm._jobs[job_id] = _make_job(
                id=job_id,
                status=JobStatus.TAGGING,
                result_path=f"Bonobo/Migration/{job_id}.flac",
            )

        await qm._run_tagging_stage("job-1")
        assert qm.get_job("job-1").detail == NOTE_TIMED_OUT

        started = time.monotonic()
        await qm._run_tagging_stage("job-2")
        elapsed = time.monotonic() - started

        assert qm.get_job("job-2").detail == NOTE_TIMED_OUT
        assert elapsed < 0.2, f"the second job waited {elapsed:.2f}s for the first"
        assert mock_fix.call_count == 1, "the second fix should never be submitted"

        released.set()
        qm.close()

    @patch("app.queue_manager.fix_track")
    async def test_the_worker_takes_jobs_again_once_it_is_free(
        self, mock_fix, isolated_paths
    ):
        """The fast path above is a state, not a verdict: MusicBrainz coming
        back must not leave the queue permanently refusing to tag."""
        download_dir, _ = isolated_paths
        released = threading.Event()

        def never_returns_then_matches(*args, **kwargs):
            if not mock_fix.call_count > 1:
                released.wait(timeout=5)
            return TagFixResult(matched=True, changed=True)

        mock_fix.side_effect = never_returns_then_matches

        qm = QueueManager(max_concurrent=1, timeout=10, tag_fix_timeout=0.2)
        for job_id in ("job-1", "job-2"):
            flac = download_dir / "Bonobo" / "Migration" / f"{job_id}.flac"
            flac.parent.mkdir(parents=True, exist_ok=True)
            flac.write_bytes(minimal_flac_bytes(44100 * 183))
            qm._jobs[job_id] = _make_job(
                id=job_id,
                status=JobStatus.TAGGING,
                result_path=f"Bonobo/Migration/{job_id}.flac",
            )

        await qm._run_tagging_stage("job-1")
        assert qm.get_job("job-1").detail == NOTE_TIMED_OUT

        released.set()
        deadline = time.monotonic() + 5
        while qm._stuck_tag_fix is not None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert qm._stuck_tag_fix is None

        await qm._run_tagging_stage("job-2")
        assert qm.get_job("job-2").status is JobStatus.DONE
        assert qm.get_job("job-2").detail is None
        qm.close()

    async def test_close_does_not_wait_for_a_lookup_in_flight(self, isolated_paths):
        """Shutdown must not be held up by a socket the OS has not given up
        on yet."""
        download_dir, _ = isolated_paths
        flac = download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        flac.parent.mkdir(parents=True)
        flac.write_bytes(minimal_flac_bytes(44100 * 183))
        released = threading.Event()

        def never_returns(*args, **kwargs):
            released.wait(timeout=5)
            return TagFixResult(matched=False, note=NOTE_NO_MATCH)

        qm = QueueManager(max_concurrent=1, timeout=10, tag_fix_timeout=0.1)
        qm._jobs["job-1"] = _make_job(
            id="job-1",
            status=JobStatus.TAGGING,
            result_path="Bonobo/Migration/Kerala.flac",
        )

        with patch("app.queue_manager.fix_track", never_returns):
            await qm._run_tagging_stage("job-1")

            started = time.monotonic()
            qm.close()
            assert time.monotonic() - started < 0.5

        released.set()

    async def test_close_is_safe_on_a_queue_that_never_tagged(self):
        QueueManager(max_concurrent=1, timeout=10).close()

    @patch("app.queue_manager.fix_track")
    async def test_a_fix_that_arrives_after_close_finishes_its_job_anyway(
        self, mock_fix, isolated_paths
    ):
        """Shutting down mid-tagging must not leave a RuntimeError escaping a
        background task; the track is already in the library either way."""
        download_dir, _ = isolated_paths
        flac = download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        flac.parent.mkdir(parents=True)
        flac.write_bytes(minimal_flac_bytes(44100 * 183))

        qm = QueueManager(max_concurrent=1, timeout=10)
        qm._jobs["job-1"] = _make_job(
            id="job-1",
            status=JobStatus.TAGGING,
            result_path="Bonobo/Migration/Kerala.flac",
        )
        qm.close()

        await qm._run_tagging_stage("job-1")

        assert qm.get_job("job-1").status is JobStatus.DONE
        assert qm.get_job("job-1").detail == NOTE_CANCELLED
        mock_fix.assert_not_called()

    async def test_a_fix_that_raises_still_finishes_its_job(
        self, isolated_paths, caplog
    ):
        """An error nobody anticipated must not escape the stage: the job
        would stay `tagging`, and `tagging` is recovered on every restart
        without spending a restart attempt, so it would raise forever."""
        download_dir, _ = isolated_paths
        flac = download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        flac.parent.mkdir(parents=True)
        flac.write_bytes(minimal_flac_bytes(44100 * 183))
        before = flac.read_bytes()

        qm = QueueManager(max_concurrent=1, timeout=10)
        qm._jobs["job-1"] = _make_job(
            id="job-1",
            status=JobStatus.TAGGING,
            result_path="Bonobo/Migration/Kerala.flac",
        )

        with caplog.at_level(logging.ERROR, logger="app.queue_manager"):
            with patch(
                "app.queue_manager.fix_track", side_effect=RuntimeError("boom")
            ):
                await qm._run_tagging_stage("job-1")

        job = qm.get_job("job-1")
        assert job.status is JobStatus.DONE
        assert job.detail == NOTE_FAILED
        assert flac.read_bytes() == before
        assert "job-1" in caplog.text
        assert "boom" in caplog.text
        qm.close()

    @patch("app.queue_manager.download_audio")
    async def test_a_restored_row_whose_fix_raises_finishes_too(
        self, mock_download, store, isolated_paths, caplog
    ):
        """The dispatched path is the one that would loop on every restart."""
        download_dir, _ = isolated_paths
        flac = download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        flac.parent.mkdir(parents=True)
        flac.write_bytes(minimal_flac_bytes(44100 * 183))
        before = flac.read_bytes()
        store.upsert(
            _make_job(
                id="job-1",
                status=JobStatus.TAGGING,
                result_path="Bonobo/Migration/Kerala.flac",
            )
        )

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        with caplog.at_level(logging.ERROR, logger="app.queue_manager"):
            with patch(
                "app.queue_manager.fix_track", side_effect=RuntimeError("boom")
            ):
                qm.restore_from_store()
                await _wait_for_job_status(qm, "job-1", JobStatus.DONE)

        job = qm.get_job("job-1")
        assert job.detail == NOTE_FAILED
        assert job.restart_attempts == 0
        assert flac.read_bytes() == before
        assert "boom" in caplog.text
        mock_download.assert_not_called()
        qm.close()

    async def test_cancelling_the_tagging_task_still_unwinds(
        self, isolated_paths
    ):
        """The guard around the fix catches `Exception`, never
        `BaseException`: shutdown cancels these tasks, and a cancel that got
        swallowed here would be reported as a finished job."""
        download_dir, _ = isolated_paths
        flac = download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        flac.parent.mkdir(parents=True)
        flac.write_bytes(minimal_flac_bytes(44100 * 183))
        started = threading.Event()
        released = threading.Event()

        def never_returns(*args, **kwargs):
            started.set()
            released.wait(timeout=5)
            return TagFixResult(matched=False, note=NOTE_NO_MATCH)

        qm = QueueManager(max_concurrent=1, timeout=10)
        qm._jobs["job-1"] = _make_job(
            id="job-1",
            status=JobStatus.TAGGING,
            result_path="Bonobo/Migration/Kerala.flac",
        )

        with patch("app.queue_manager.fix_track", never_returns):
            task = asyncio.ensure_future(qm._run_tagging_stage("job-1"))
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert qm.get_job("job-1").status is JobStatus.TAGGING
        released.set()
        qm.close()


# ===========================================================================
# Active runs
# ===========================================================================


class TestActiveRunsBookkeeping:
    """The dict holds the runs a cancel or a retry might still need."""

    @patch("app.queue_manager.fix_track")
    @patch("app.queue_manager.download_audio")
    async def test_a_finished_download_leaves_nothing_behind(
        self, mock_download, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        mock_download.side_effect = _filing_download(
            download_dir, "Bonobo/Migration/{job}.flac"
        )
        mock_fix.return_value = _fix_result(matched=False, note=NOTE_NO_MATCH)

        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        job_ids = [f"job-{index}" for index in range(5)]
        for job_id in job_ids:
            qm.add_job(_make_job(id=job_id))
        for job_id in job_ids:
            await _wait_for_job_status(qm, job_id, JobStatus.DONE)

        assert qm._active_runs == {}
        qm.close()

    @patch("app.queue_manager.download_audio")
    async def test_a_failed_download_keeps_its_run_for_retry(
        self, mock_download, store, isolated_paths
    ):
        """`retry` reads the entry to refuse while the previous attempt's
        thread is still unwinding, so the error path must keep it."""
        mock_download.side_effect = DownloadError("no such video")

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_job(_make_job(id="job-1"))
        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR)

        assert "job-1" in qm._active_runs
        qm.close()


# ===========================================================================
# Manual tagging jobs (phase 9)
# ===========================================================================


def _tagging_job(path: str, **overrides) -> Job:
    """A queued tagging job for *path*, the way POST /library/tag builds one."""
    defaults = {
        "id": "tag-1",
        "url": "",
        "kind": JobKind.TAGGING,
        "status": JobStatus.QUEUED,
        "path": path,
        "title": "Kerala",
        "artist": "Bonobo",
        "album": "Migration",
    }
    defaults.update(overrides)
    return Job(**defaults)


def _album_on_disk(download_dir: Path, *names: str) -> Path:
    """An album folder holding one empty-but-valid FLAC per name."""
    folder = download_dir / "Bonobo" / "Migration"
    folder.mkdir(parents=True, exist_ok=True)
    for name in names or ("Kerala.flac",):
        (folder / name).write_bytes(minimal_flac_bytes(44100 * 183))
    return folder


def _release_rows():
    """The two rows `release_tracks` returns for the release `_lookup` names."""
    from app.album_tagger import ReleaseTrack

    return [
        ReleaseTrack(
            recording_id=f"rec-{position}",
            title=f"Track {position}",
            length_ms=183000,
            artist_credit="Bonobo",
            artist_names=("Bonobo",),
            track_number=position,
            disc_number=1,
        )
        for position in (1, 2)
    ]


def _lookup(path: Path, title: str, recording_id: str, release_id: str = "rel-1"):
    """A matched TrackLookup, as app.album_tagger.lookup_track would return."""
    from app.album_tagger import TrackLookup
    from app.tagger import Candidate, ReleaseRef

    return TrackLookup(
        path=path,
        candidates=(
            Candidate(
                title=title,
                artist_credit="Bonobo",
                artist_names=("Bonobo",),
                length_ms=183000,
                recording_id=recording_id,
                releases=(
                    ReleaseRef(id=release_id, track_count=2, status="Official"),
                ),
            ),
        ),
    )


class TestTrackTaggingJobs:
    """The row action: one file, the same fix a download runs automatically."""

    @patch("app.queue_manager.fix_track")
    async def test_it_runs_queued_tagging_done(self, mock_fix, store, isolated_paths):
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        mock_fix.return_value = TagFixResult(matched=True, changed=True)
        events: list[SSEEvent] = []

        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=events.append, store=store
        )
        qm.add_tagging_job(_tagging_job("Bonobo/Migration/Kerala.flac"))
        await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)

        statuses = [e.data["status"] for e in events if e.event == "status_change"]
        assert statuses == ["tagging", "done"]
        assert qm.get_job("tag-1").detail is None
        # It got the absolute path and the folder artist, like the automatic fix.
        args, kwargs = mock_fix.call_args
        assert args[0] == download_dir / "Bonobo" / "Migration" / "Kerala.flac"
        assert args[1] == "Bonobo"
        qm.close()

    @patch("app.queue_manager.fix_track")
    async def test_a_track_in_a_disc_subfolder_runs_the_same_fix(
        self, mock_fix, store, isolated_paths
    ):
        """The route accepts a file at any depth below its artist, so the
        worker has to run one: the folder artist is still the top segment."""
        download_dir, _ = isolated_paths
        folder = download_dir / "Bonobo" / "Migration" / "Disc 2"
        folder.mkdir(parents=True)
        (folder / "Kerala.flac").write_bytes(minimal_flac_bytes(44100 * 183))
        mock_fix.return_value = TagFixResult(matched=True, changed=True)

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_tagging_job(_tagging_job("Bonobo/Migration/Disc 2/Kerala.flac"))
        await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)

        args, _ = mock_fix.call_args
        assert args[0] == folder / "Kerala.flac"
        assert args[1] == "Bonobo"
        qm.close()

    @patch("app.queue_manager.fix_track")
    async def test_it_reports_no_progress_counts(
        self, mock_fix, store, isolated_paths
    ):
        """One track is one step: "0 of 1" then "1 of 1" says nothing."""
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        mock_fix.return_value = TagFixResult(matched=True, changed=True)
        events: list[SSEEvent] = []

        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=events.append, store=store
        )
        qm.add_tagging_job(_tagging_job("Bonobo/Migration/Kerala.flac"))
        await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)

        assert [e for e in events if e.event == "progress"] == []
        job = qm.get_job("tag-1")
        assert job.progress_done is None
        assert job.progress_total is None
        qm.close()

    @patch("app.queue_manager.fix_track")
    async def test_no_match_finishes_done_with_the_note(
        self, mock_fix, store, isolated_paths
    ):
        """A recording MusicBrainz does not know is a result, not a failure."""
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        mock_fix.return_value = TagFixResult(note=NOTE_NO_MATCH)

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_tagging_job(_tagging_job("Bonobo/Migration/Kerala.flac"))
        await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)

        assert qm.get_job("tag-1").detail == NOTE_NO_MATCH
        assert qm.get_job("tag-1").error is None
        qm.close()

    @patch("app.queue_manager.fix_track")
    async def test_musicbrainz_being_down_ends_in_error(
        self, mock_fix, store, isolated_paths
    ):
        """The ticket's rule: a manual tagging job that could not run fails."""
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        mock_fix.return_value = TagFixResult(note=NOTE_UNAVAILABLE)

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_tagging_job(_tagging_job("Bonobo/Migration/Kerala.flac"))
        await _wait_for_job_status(qm, "tag-1", JobStatus.ERROR)

        assert qm.get_job("tag-1").error == NOTE_UNAVAILABLE
        qm.close()

    @patch("app.queue_manager.fix_track")
    async def test_an_errored_job_can_be_retried_and_then_succeeds(
        self, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        mock_fix.return_value = TagFixResult(note=NOTE_UNAVAILABLE)

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_tagging_job(_tagging_job("Bonobo/Migration/Kerala.flac"))
        await _wait_for_job_status(qm, "tag-1", JobStatus.ERROR)

        mock_fix.return_value = TagFixResult(matched=True, changed=True)
        qm.retry_job("tag-1")
        await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)

        job = qm.get_job("tag-1")
        assert job.error is None and job.detail is None and job.attempts == 1
        qm.close()

    @patch("app.queue_manager.fix_track")
    async def test_an_errored_job_can_be_dismissed(
        self, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        mock_fix.return_value = TagFixResult(note=NOTE_UNAVAILABLE)

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_tagging_job(_tagging_job("Bonobo/Migration/Kerala.flac"))
        await _wait_for_job_status(qm, "tag-1", JobStatus.ERROR)

        qm.dismiss_job("tag-1")

        assert qm.get_job("tag-1") is None
        qm.close()

    async def test_a_path_that_is_gone_ends_in_error(self, store, isolated_paths):
        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_tagging_job(_tagging_job("Bonobo/Migration/Gone.flac"))
        await _wait_for_job_status(qm, "tag-1", JobStatus.ERROR)

        assert qm.get_job("tag-1").error == NOTE_FILE_MISSING
        qm.close()

    async def test_a_second_job_for_the_same_path_is_refused(
        self, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        released = threading.Event()

        with patch("app.queue_manager.fix_track") as mock_fix:
            mock_fix.side_effect = lambda *a, **k: (
                released.wait(timeout=5),
                TagFixResult(matched=True),
            )[1]
            qm = QueueManager(max_concurrent=1, timeout=10, store=store)
            qm.add_tagging_job(_tagging_job("Bonobo/Migration/Kerala.flac"))
            await _wait_for_job_status(qm, "tag-1", JobStatus.TAGGING)

            with pytest.raises(QueueError):
                qm.add_tagging_job(
                    _tagging_job("Bonobo/Migration/Kerala.flac", id="tag-2")
                )
            # And the album that contains it, from the other end.
            with pytest.raises(QueueError):
                qm.add_tagging_job(_tagging_job("Bonobo/Migration", id="tag-3"))

            released.set()
            await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)
            qm.close()

    async def test_an_unrelated_path_is_allowed_alongside(self, store, isolated_paths):
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm._jobs["tag-1"] = _tagging_job(
            "Bonobo/Migration/Kerala.flac", status=JobStatus.TAGGING
        )

        assert qm.find_tagging_conflict("Bonobo/Black Sands") is None
        qm.close()

    @patch("app.queue_manager.fix_track")
    async def test_library_changed_fires_after_a_run(
        self, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        mock_fix.return_value = TagFixResult(matched=True, changed=True)
        events: list[SSEEvent] = []

        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=events.append, store=store
        )
        qm.add_tagging_job(_tagging_job("Bonobo/Migration/Kerala.flac"))
        await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)

        changed = [e for e in events if e.event == "library_changed"]
        assert [e.data["paths"] for e in changed] == [
            ["Bonobo/Migration/Kerala.flac"]
        ]
        qm.close()

    @patch("app.queue_manager.fix_track")
    async def test_a_run_that_changed_nothing_still_wakes_the_rescan(
        self, mock_fix, store, isolated_paths
    ):
        """The ticket: below the match bar nothing changes, and the rescan
        still fires -- a manual run has no download event behind it."""
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        mock_fix.return_value = TagFixResult(note=NOTE_NO_MATCH)
        events: list[SSEEvent] = []

        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=events.append, store=store
        )
        qm.add_tagging_job(_tagging_job("Bonobo/Migration/Kerala.flac"))
        await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)

        assert [e.data["paths"] for e in events if e.event == "library_changed"] == [
            ["Bonobo/Migration/Kerala.flac"]
        ]
        qm.close()

    async def test_a_queued_job_cancelled_before_it_starts_never_runs(
        self, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)

        with patch("app.queue_manager.fix_track") as mock_fix:
            qm = QueueManager(max_concurrent=1, timeout=10, store=store)
            job = _tagging_job("Bonobo/Migration/Kerala.flac")
            job.cancel_requested = True
            qm._jobs[job.id] = job
            qm._dispatch_tagging_job(job.id)
            await _wait_for_job_status(qm, "tag-1", JobStatus.CANCELLED)

            mock_fix.assert_not_called()
            qm.close()


class TestAlbumTaggingJobs:
    """The album header action: every track, one release, N of M progress."""

    async def test_it_reports_n_of_m_and_writes_the_numbers(
        self, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        folder = _album_on_disk(download_dir, "01 Migration.flac", "02 Kerala.flac")
        events: list[SSEEvent] = []

        def fake_lookup(path, folder_artist, **kwargs):
            index = 1 if path.name.startswith("01") else 2
            return _lookup(path, f"Track {index}", f"rec-{index}")

        with (
            patch("app.album_tagger.lookup_track", side_effect=fake_lookup),
            patch(
                "app.album_tagger.release_tracks",
                return_value=_release_rows(),
            ),
            patch(
                "app.album_tagger.store_cover",
                return_value=(folder / "cover.jpg", None),
            ),
        ):
            qm = QueueManager(
                max_concurrent=1, timeout=10, on_event=events.append, store=store
            )
            qm.add_tagging_job(_tagging_job("Bonobo/Migration"))
            await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)
            qm.close()

        assert qm.get_job("tag-1").detail is None
        counts = [
            (e.data["progress_done"], e.data["progress_total"])
            for e in events
            if e.event == "progress"
        ]
        assert counts == [(0, 2), (1, 2), (2, 2)]
        first = FLAC(folder / "01 Migration.flac")
        assert first["TITLE"] == ["Track 1"] and first["TRACKNUMBER"] == ["1"]

    async def test_a_partial_album_says_so_and_writes_no_numbers(
        self, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        folder = _album_on_disk(download_dir, "01 Migration.flac", "02 Kerala.flac")

        def fake_lookup(path, folder_artist, **kwargs):
            from app.album_tagger import TrackLookup

            if path.name.startswith("01"):
                return _lookup(path, "Track 1", "rec-1")
            return TrackLookup(path=path, note=NOTE_NO_MATCH)

        with patch("app.album_tagger.lookup_track", side_effect=fake_lookup):
            qm = QueueManager(max_concurrent=1, timeout=10, store=store)
            qm.add_tagging_job(_tagging_job("Bonobo/Migration"))
            await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)
            qm.close()

        assert qm.get_job("tag-1").detail == "partial: 1 of 2"
        assert "TRACKNUMBER" not in FLAC(folder / "01 Migration.flac")
        assert not (folder / "cover.jpg").exists()

    async def test_cancelling_mid_album_leaves_the_written_tracks_alone(
        self, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        folder = _album_on_disk(download_dir, "01 Migration.flac", "02 Kerala.flac")
        second = folder / "02 Kerala.flac"
        untouched = second.read_bytes()
        first_lookup_done = threading.Event()
        keep_going = threading.Event()

        def fake_lookup(path, folder_artist, **kwargs):
            index = 1 if path.name.startswith("01") else 2
            if index == 2:
                first_lookup_done.set()
                keep_going.wait(timeout=5)
            return _lookup(path, f"Track {index}", f"rec-{index}")

        with patch("app.album_tagger.lookup_track", side_effect=fake_lookup):
            qm = QueueManager(max_concurrent=1, timeout=10, store=store)
            qm.add_tagging_job(_tagging_job("Bonobo/Migration"))
            await _wait_for_job_status(qm, "tag-1", JobStatus.TAGGING)
            await asyncio.to_thread(first_lookup_done.wait, 5)

            qm.cancel_job("tag-1")
            keep_going.set()
            await _wait_for_job_status(qm, "tag-1", JobStatus.CANCELLED)
            qm.close()

        # The pass stopped before its write phase, so nothing was rewritten --
        # and nothing that had been written would have been undone either.
        assert second.read_bytes() == untouched

    async def test_a_write_phase_cancel_keeps_the_tracks_already_written(
        self, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        folder = _album_on_disk(download_dir, "01 Migration.flac", "02 Kerala.flac")
        second = folder / "02 Kerala.flac"
        untouched = second.read_bytes()
        qm = QueueManager(max_concurrent=1, timeout=10, store=store)

        def fake_lookup(path, folder_artist, **kwargs):
            index = 1 if path.name.startswith("01") else 2
            return _lookup(path, f"Track {index}", f"rec-{index}")

        real_write = album_tagger.write_track

        def write_then_cancel(path, match, numbers=None):
            written = real_write(path, match, numbers)
            # The cancel lands between the first write and the second.
            qm.get_job("tag-1").cancel_requested = True
            return written

        with (
            patch("app.album_tagger.lookup_track", side_effect=fake_lookup),
            patch("app.album_tagger.write_track", side_effect=write_then_cancel),
            patch(
                "app.album_tagger.release_tracks",
                return_value=_release_rows(),
            ),
        ):
            qm.add_tagging_job(_tagging_job("Bonobo/Migration"))
            await _wait_for_job_status(qm, "tag-1", JobStatus.CANCELLED)
            qm.close()

        assert FLAC(folder / "01 Migration.flac")["TITLE"] == ["Track 1"]
        assert second.read_bytes() == untouched
        assert not (folder / "cover.jpg").exists()

    async def test_musicbrainz_being_down_fails_the_album_job(
        self, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir, "01 Migration.flac")

        def fake_lookup(path, folder_artist, **kwargs):
            from app.album_tagger import TrackLookup

            return TrackLookup(path=path, note=NOTE_UNAVAILABLE)

        with patch("app.album_tagger.lookup_track", side_effect=fake_lookup):
            qm = QueueManager(max_concurrent=1, timeout=10, store=store)
            qm.add_tagging_job(_tagging_job("Bonobo/Migration"))
            await _wait_for_job_status(qm, "tag-1", JobStatus.ERROR)
            qm.close()

        assert qm.get_job("tag-1").error == NOTE_UNAVAILABLE

    async def test_a_step_that_never_returns_times_the_job_out(
        self, store, isolated_paths
    ):
        """The lookup cannot be interrupted, so the queue bounds the wait and
        the job fails rather than hanging in `tagging` forever."""
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir, "01 Migration.flac")
        release = threading.Event()

        def stalled_lookup(path, folder_artist, **kwargs):
            release.wait(timeout=5)
            return _lookup(path, "Track 1", "rec-1")

        with patch("app.album_tagger.lookup_track", side_effect=stalled_lookup):
            qm = QueueManager(
                max_concurrent=1, timeout=10, store=store, tag_fix_timeout=0
            )
            qm.add_tagging_job(_tagging_job("Bonobo/Migration"))
            await _wait_for_job_status(qm, "tag-1", JobStatus.ERROR)
            release.set()
            qm.close()

        assert qm.get_job("tag-1").error == NOTE_TIMED_OUT


class TestWaitingForTheTaggingLock:
    """The lock is this app's whole MusicBrainz rate limit, so a second job
    can sit behind the first for minutes.  What its row says while it waits,
    and what a cancel does to it, is the subject here."""

    @patch("app.queue_manager.fix_track")
    async def test_a_job_waiting_its_turn_still_says_queued(
        self, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        release = threading.Event()
        mock_fix.side_effect = lambda *a, **k: (
            release.wait(timeout=5),
            TagFixResult(matched=True),
        )[1]

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_tagging_job(_tagging_job("Bonobo/Migration/Kerala.flac"))
        await _wait_for_job_status(qm, "tag-1", JobStatus.TAGGING)
        # A second job for a different path, so the tagging-vs-tagging guard
        # does not refuse it: the only thing in its way is the lock.
        (download_dir / "Bonobo" / "Migration" / "Kong.flac").write_bytes(
            minimal_flac_bytes(44100 * 183)
        )
        qm.add_tagging_job(
            _tagging_job("Bonobo/Migration/Kong.flac", id="tag-2")
        )

        await asyncio.sleep(0.1)
        assert qm.get_job("tag-2").status == JobStatus.QUEUED

        release.set()
        await _wait_for_job_status(qm, "tag-2", JobStatus.DONE)
        qm.close()

    @patch("app.queue_manager.fix_track")
    async def test_cancelling_a_waiting_job_ends_it_without_running_the_pass(
        self, mock_fix, store, isolated_paths
    ):
        """`_finish_cancelled` writes ``cancelled`` without setting the flag,
        so the pass has to re-read the status once it holds the lock."""
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        release = threading.Event()
        calls: list[Path] = []

        def blocking(path, *args, **kwargs):
            calls.append(path)
            release.wait(timeout=5)
            return TagFixResult(matched=True)

        mock_fix.side_effect = blocking

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.add_tagging_job(_tagging_job("Bonobo/Migration/Kerala.flac"))
        await _wait_for_job_status(qm, "tag-1", JobStatus.TAGGING)
        (download_dir / "Bonobo" / "Migration" / "Kong.flac").write_bytes(
            minimal_flac_bytes(44100 * 183)
        )
        qm.add_tagging_job(
            _tagging_job("Bonobo/Migration/Kong.flac", id="tag-2")
        )
        await asyncio.sleep(0.05)

        # Cancelled while queued: immediate, because nothing has been touched.
        qm.cancel_job("tag-2")
        assert qm.get_job("tag-2").status == JobStatus.CANCELLED

        release.set()
        await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)
        await asyncio.sleep(0.1)

        assert qm.get_job("tag-2").status == JobStatus.CANCELLED
        assert [path.name for path in calls] == ["Kerala.flac"]
        qm.close()


class TestTaggingJobsAcrossARestart:
    """Nothing is stored about matches, so a restart re-runs the whole pass."""

    def _row(self, store, download_dir, **overrides):
        job = _tagging_job("Bonobo/Migration/Kerala.flac", **overrides)
        store.upsert(job)
        return job

    @patch("app.queue_manager.fix_track")
    async def test_an_interrupted_job_runs_again(
        self, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        mock_fix.return_value = TagFixResult(matched=True, changed=True)
        self._row(store, download_dir, status=JobStatus.TAGGING)

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.restore_from_store()
        await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)

        assert mock_fix.called
        qm.close()

    @patch("app.queue_manager.fix_track")
    async def test_it_does_not_spend_a_restart_attempt(
        self, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        mock_fix.return_value = TagFixResult(matched=True, changed=True)
        self._row(store, download_dir, status=JobStatus.TAGGING)

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.restore_from_store()
        await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)

        assert qm.get_job("tag-1").restart_attempts == 0
        assert qm.get_job("tag-1").attempts == 0
        qm.close()

    async def test_a_job_whose_path_is_gone_ends_in_error(self, store, isolated_paths):
        download_dir, _ = isolated_paths
        self._row(store, download_dir, status=JobStatus.TAGGING)

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.restore_from_store()

        assert qm.get_job("tag-1").status is JobStatus.ERROR
        assert qm.get_job("tag-1").error == NOTE_FILE_MISSING
        qm.close()

    async def test_a_job_being_cancelled_ends_cancelled(self, store, isolated_paths):
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        self._row(
            store, download_dir, status=JobStatus.TAGGING, cancel_requested=True
        )

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.restore_from_store()

        assert qm.get_job("tag-1").status is JobStatus.CANCELLED
        qm.close()

    @patch("app.queue_manager.fix_track")
    async def test_a_still_queued_job_is_dispatched_onto_the_tagging_worker(
        self, mock_fix, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        _album_on_disk(download_dir)
        mock_fix.return_value = TagFixResult(matched=True, changed=True)
        self._row(store, download_dir, status=JobStatus.QUEUED)

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        qm.restore_from_store()
        await _wait_for_job_status(qm, "tag-1", JobStatus.DONE)

        # No download slot was ever taken: the semaphore is untouched.
        assert qm._semaphore._value == 1
        qm.close()

    def test_progress_is_never_persisted(self, store, isolated_paths):
        job = _tagging_job("Bonobo/Migration")
        job.progress_done = 3
        job.progress_total = 12
        store.upsert(job)

        [restored] = store.load_active()

        assert restored.progress_done is None and restored.progress_total is None
