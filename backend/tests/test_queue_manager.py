"""Tests for the queue manager module.

Covers the full job state machine, concurrency control, timeout
enforcement, retry logic, and event callback system.  All tests
mock the downloader module -- no real network calls or downloads.
"""

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.downloader import DownloadError
from app.job_store import JobStore
from app.models import Job, JobStatus, SSEEvent
from app.queue_manager import (
    DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    MAX_RESTART_ATTEMPTS,
    RESTART_GIVE_UP_MESSAGE,
    RETENTION_DAYS,
    QueueError,
    QueueManager,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


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

        def fake_download(job, on_progress, cancel_event=None, on_phase=None):
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

        def fake_download(job, on_progress, cancel_event=None, on_phase=None):
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

        async def patched_run_download(job_id):
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

        async def patched_run_download(job_id):
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

        async def slow_download(job_id):
            await asyncio.sleep(10)  # Way longer than timeout

        qm._run_download = slow_download

        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR, timeout=5.0)

        result = qm.get_job("job-1")
        assert result.status == JobStatus.ERROR
        assert "timed out" in result.error.lower()

    async def test_timeout_error_message_includes_duration(self):
        qm = QueueManager(max_concurrent=2, timeout=1)

        async def slow_download(job_id):
            await asyncio.sleep(10)

        qm._run_download = slow_download

        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR, timeout=5.0)

        result = qm.get_job("job-1")
        assert "1 seconds" in result.error

    async def test_timeout_emits_error_event(self):
        events = []
        qm = QueueManager(max_concurrent=2, timeout=1, on_event=lambda e: events.append(e))

        async def slow_download(job_id):
            await asyncio.sleep(10)

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

        async def patched_run_download(job_id):
            nonlocal call_count
            call_count += 1
            if job_id == "job-0":
                # This one will time out
                await asyncio.sleep(10)
            else:
                # This one completes quickly
                await proceed_event.wait()

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

        async def patched_run_download(job_id):
            nonlocal first_call
            if first_call:
                first_call = False
                await asyncio.sleep(10)  # Will time out
            # Second call succeeds immediately

        qm._run_download = patched_run_download

        job = _make_job()
        qm.add_job(job)

        await _wait_for_job_status(qm, "job-1", JobStatus.ERROR, timeout=5.0)
        assert "timed out" in qm.get_job("job-1").error.lower()

        qm.retry_job("job-1")

        await _wait_for_job_status(qm, "job-1", JobStatus.DONE, timeout=5.0)

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

        def fake(job, on_progress, cancel_event=None, on_phase=None):
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

        def fake(job, on_progress, cancel_event=None, on_phase=None):
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

        def fake(job, on_progress, cancel_event=None, on_phase=None):
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

        def fake(job, on_progress, cancel_event=None, on_phase=None):
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
    async def test_timeout_sets_cancel_event_and_silences_progress(self, mock_download):
        import threading

        events = []
        release = threading.Event()
        seen = {}

        def fake(job, on_progress, cancel_event=None, on_phase=None):
            release.wait(5)
            seen["cancelled"] = cancel_event.is_set()
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

        def fake(job, on_progress, cancel_event=None, on_phase=None):
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

        def fake(job, on_progress, cancel_event=None, on_phase=None):
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
        "status", [JobStatus.DOWNLOADING, JobStatus.CONVERTING, JobStatus.TAGGING]
    )
    @patch("app.queue_manager.download_audio")
    async def test_every_running_status_is_requeued(self, mock_download, status, store):
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
        def slow_download(job, *args, **kwargs):
            """Still converting and moving when the timeout has given up."""
            time.sleep(0.5)
            temp_dir.mkdir(parents=True, exist_ok=True)
            (temp_dir / "leftover.part").write_text("x")
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_text("flac")
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
        # The file itself stays: it may have replaced a pre-existing track.
        assert result.exists()


class TestResultPath:
    """Where the finished file landed, recorded relative to DOWNLOAD_PATH."""

    @patch("app.queue_manager.download_audio")
    async def test_result_path_is_stored_relative_to_the_library_root(
        self, mock_download, store, isolated_paths
    ):
        download_dir, _ = isolated_paths
        mock_download.return_value = (
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
        mock_download.return_value = "/somewhere/else/Title.flac"

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
