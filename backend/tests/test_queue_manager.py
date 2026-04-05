"""Tests for the queue manager module.

Covers the full job state machine, concurrency control, timeout
enforcement, retry logic, and event callback system.  All tests
mock the downloader module -- no real network calls or downloads.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.downloader import DownloadError
from app.models import Job, JobStatus, SSEEvent
from app.queue_manager import (
    DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    QueueError,
    QueueManager,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_job(**overrides) -> Job:
    """Create a Job with sensible defaults, overriding any field."""
    defaults = {
        "id": "job-1",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "status": JobStatus.QUEUED,
        "title": "Test Track",
        "thumbnail_url": "https://img.youtube.com/thumb.jpg",
        "duration": 210.0,
    }
    defaults.update(overrides)
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
        assert statuses == ["downloading", "converting", "done"]

    @patch("app.queue_manager.download_audio")
    async def test_progress_events_emitted(self, mock_download):
        """When download_audio invokes the on_progress callback, progress
        events should be emitted via the on_event hook."""
        events = []

        def fake_download(job, on_progress):
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

        def fake_download(job, on_progress):
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
