"""Integration tests for API routes.

Tests the full request/response cycle using FastAPI's TestClient,
with mocked downloader and controlled queue behavior.  Covers:
  - POST /download (happy path, validation, metadata extraction failure)
  - GET /queue (empty, populated)
  - POST /queue/{id}/retry (happy path, errors)
  - GET /queue/stream SSE (event emission)
"""

import asyncio
import os
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.downloader import DownloadError, TrackMetadata
from app.job_store import SCHEMA_VERSION, JobStore
from app.models import Job, JobStatus, SSEEvent
from app.queue_manager import QueueManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_metadata(**overrides) -> TrackMetadata:
    """Create a TrackMetadata with sensible defaults."""
    defaults = {
        "title": "Never Gonna Give You Up",
        "thumbnail_url": "https://img.youtube.com/thumb.jpg",
        "duration": 213.0,
    }
    defaults.update(overrides)
    return TrackMetadata(**defaults)


def _blocking_download(release: threading.Event):
    """Return a download_audio stand-in that parks until *release* is set.

    GET /queue only lists in-flight and errored jobs, so a test that wants to
    see its jobs there has to keep them from finishing first.  The autouse
    stub in conftest returns instantly, which is right for every other test.
    """

    def fake_download(job, *args, **kwargs):
        release.wait(timeout=5)
        return "/data/music/output.flac"

    return fake_download


def _make_job(**overrides) -> Job:
    """Create a Job with sensible defaults."""
    defaults = {
        "id": "job-1",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "status": JobStatus.QUEUED,
        "title": "Never Gonna Give You Up",
        "thumbnail_url": "https://img.youtube.com/thumb.jpg",
        "duration": 213.0,
    }
    defaults.update(overrides)
    return Job(**defaults)


@pytest.fixture()
def fresh_app():
    """Create a fresh FastAPI app with a clean QueueManager for each test.

    We re-import and patch the module-level queue_manager so tests don't
    leak state between each other.
    """
    import app.main as main_module

    # Create a fresh QueueManager for this test
    fresh_qm = QueueManager(max_concurrent=2, timeout=10, on_event=main_module._on_queue_event)
    original_qm = main_module.queue_manager
    main_module.queue_manager = fresh_qm

    yield main_module.app, fresh_qm

    # Restore original
    main_module.queue_manager = original_qm


@pytest.fixture()
def client(fresh_app):
    """TestClient bound to a fresh app instance."""
    app, _ = fresh_app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client_and_qm(fresh_app):
    """TestClient + QueueManager for tests that need to inspect queue state."""
    app, qm = fresh_app
    with TestClient(app) as c:
        yield c, qm


# ===========================================================================
# GET /health
# ===========================================================================


class TestHealthEndpoint:
    """Verify the health endpoint still works after route additions."""

    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "yt-dlp-web-ui-backend"


# ===========================================================================
# POST /download
# ===========================================================================


class TestPostDownload:
    """Tests for the POST /download endpoint."""

    @patch("app.main.extract_metadata")
    def test_submit_valid_url_returns_job_with_metadata(self, mock_extract, client_and_qm):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()

        resp = client.post("/download", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert data["status"] == "queued"
        assert data["title"] == "Never Gonna Give You Up"
        assert data["thumbnail_url"] == "https://img.youtube.com/thumb.jpg"
        assert data["duration"] == 213.0
        assert data["id"]  # UUID should be present

    @patch("app.main.extract_metadata")
    def test_submit_with_artist_and_album(self, mock_extract, client_and_qm):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()

        resp = client.post(
            "/download",
            json={
                "url": "https://www.youtube.com/watch?v=abc",
                "artist": "Rick Astley",
                "album": "Greatest Hits",
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["artist"] == "Rick Astley"
        assert data["album"] == "Greatest Hits"

    @patch("app.main.extract_metadata")
    def test_submit_creates_job_in_queue(self, mock_extract, client_and_qm):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()

        resp = client.post("/download", json={"url": "https://youtube.com/watch?v=abc"})
        job_id = resp.json()["id"]

        # Looked up by id rather than through get_jobs(): the stubbed download
        # can finish first, and get_jobs() omits finished jobs.
        assert qm.get_job(job_id) is not None

    def test_missing_url_returns_422(self, client):
        resp = client.post("/download", json={})
        assert resp.status_code == 422

    def test_empty_url_returns_422(self, client):
        resp = client.post("/download", json={"url": ""})
        assert resp.status_code == 422

    def test_invalid_json_returns_422(self, client):
        resp = client.post(
            "/download",
            content="not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422

    @patch("app.main.extract_metadata")
    def test_metadata_extraction_failure_still_enqueues_job(self, mock_extract, client_and_qm):
        client, qm = client_and_qm
        mock_extract.side_effect = DownloadError("Video unavailable")

        resp = client.post("/download", json={"url": "https://youtube.com/watch?v=invalid"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "queued"
        assert data["title"] is None
        assert data["thumbnail_url"] is None
        assert data["duration"] is None
        assert data["url"] == "https://youtube.com/watch?v=invalid"
        # By id: the stubbed download may already have finished the job, and
        # get_jobs() omits finished ones.
        assert qm.get_job(data["id"]) is not None

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_multiple_submissions_create_separate_jobs(
        self, mock_extract, mock_download, client_and_qm
    ):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        # GET /queue omits finished jobs, so both have to stay in flight.
        release = threading.Event()
        mock_download.side_effect = _blocking_download(release)

        try:
            resp1 = client.post("/download", json={"url": "https://youtube.com/watch?v=a"})
            resp2 = client.post("/download", json={"url": "https://youtube.com/watch?v=b"})

            assert resp1.json()["id"] != resp2.json()["id"]
            assert len(qm.get_jobs()) == 2
        finally:
            release.set()

    @patch("app.main.extract_metadata")
    def test_response_includes_all_job_fields(self, mock_extract, client):
        mock_extract.return_value = _make_metadata(
            title="Test Song",
            thumbnail_url="https://example.com/thumb.jpg",
            duration=180.5,
        )

        resp = client.post(
            "/download",
            json={"url": "https://youtube.com/watch?v=test", "artist": "Test Artist"},
        )

        data = resp.json()
        expected_fields = {"id", "url", "status", "title", "thumbnail_url", "duration",
                           "progress", "error", "artist", "album"}
        assert expected_fields.issubset(set(data.keys()))
        assert data["progress"] == 0.0
        assert data["error"] is None


# ===========================================================================
# GET /queue
# ===========================================================================


class TestGetQueue:
    """Tests for the GET /queue endpoint."""

    def test_empty_queue_returns_empty_list(self, client):
        resp = client.get("/queue")
        assert resp.status_code == 200
        assert resp.json() == []

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_queue_returns_all_submitted_jobs(
        self, mock_extract, mock_download, client_and_qm
    ):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        release = threading.Event()
        mock_download.side_effect = _blocking_download(release)

        try:
            client.post("/download", json={"url": "https://youtube.com/watch?v=a"})
            client.post("/download", json={"url": "https://youtube.com/watch?v=b"})

            resp = client.get("/queue")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 2
        finally:
            release.set()

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_queue_returns_current_job_state(
        self, mock_extract, mock_download, client_and_qm
    ):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        release = threading.Event()
        mock_download.side_effect = _blocking_download(release)

        try:
            client.post("/download", json={"url": "https://youtube.com/watch?v=a"})

            resp = client.get("/queue")
            data = resp.json()
            assert len(data) == 1
            assert data[0]["url"] == "https://youtube.com/watch?v=a"
            assert data[0]["title"] == "Never Gonna Give You Up"
            # Status could be queued or already progressing (downloading)
            assert data[0]["status"] in ["queued", "downloading", "converting"]
        finally:
            release.set()


# ===========================================================================
# POST /queue/{job_id}/retry
# ===========================================================================


class TestRetryEndpoint:
    """Tests for the POST /queue/{job_id}/retry endpoint."""

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_retry_failed_job_returns_queued_job(self, mock_extract, mock_download, client_and_qm):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        mock_download.side_effect = DownloadError("Network error")

        # Submit and wait for it to fail
        resp = client.post("/download", json={"url": "https://youtube.com/watch?v=fail"})
        job_id = resp.json()["id"]

        # Wait for job to reach error state
        for _ in range(100):
            job = qm.get_job(job_id)
            if job and job.status == JobStatus.ERROR:
                break
            time.sleep(0.05)

        assert qm.get_job(job_id).status == JobStatus.ERROR

        # Now retry
        mock_download.side_effect = None
        mock_download.return_value = "/data/music/output.flac"

        retry_resp = client.post(f"/queue/{job_id}/retry")
        assert retry_resp.status_code == 200
        data = retry_resp.json()
        assert data["status"] == "queued"
        assert data["error"] is None
        assert data["progress"] == 0.0

    def test_retry_nonexistent_job_returns_400(self, client):
        resp = client.post("/queue/nonexistent-id/retry")
        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"].lower()

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_retry_non_error_job_returns_400(self, mock_extract, mock_download, client_and_qm):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        mock_download.return_value = "/data/music/output.flac"

        resp = client.post("/download", json={"url": "https://youtube.com/watch?v=ok"})
        job_id = resp.json()["id"]

        # Wait for job to complete
        for _ in range(100):
            job = qm.get_job(job_id)
            if job and job.status == JobStatus.DONE:
                break
            time.sleep(0.05)

        retry_resp = client.post(f"/queue/{job_id}/retry")
        assert retry_resp.status_code == 400
        assert "only error jobs can be retried" in retry_resp.json()["detail"].lower()


# ===========================================================================
# GET /queue/stream (SSE)
# ===========================================================================


class TestSSEStream:
    """Tests for the SSE event stream endpoint.

    SSE streaming with TestClient is tricky because the stream is infinite.
    We test the broadcast infrastructure directly and verify the HTTP-level
    SSE endpoint returns the correct content type.
    """

    def test_sse_endpoint_exists(self, client):
        """The SSE endpoint should be registered in the app routes."""
        # We verify the route exists by checking the app's route table
        # (streaming tests are done via the async broadcast tests below).
        import app.main as main_module

        routes = [r.path for r in main_module.app.routes]
        assert "/queue/stream" in routes

    async def test_broadcast_sends_events_to_connected_clients(self):
        """Verify the _broadcast_event function fans out to all queues."""
        import app.main as main_module

        q1: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=256)
        q2: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=256)

        async with main_module._sse_clients_lock:
            main_module._sse_clients.append(q1)
            main_module._sse_clients.append(q2)

        try:
            event = SSEEvent(event="status_change", job_id="test-1", data={"status": "downloading"})
            await main_module._broadcast_event(event)

            assert not q1.empty()
            assert not q2.empty()

            e1 = q1.get_nowait()
            e2 = q2.get_nowait()
            assert e1.event == "status_change"
            assert e1.job_id == "test-1"
            assert e2.event == "status_change"
            assert e2.job_id == "test-1"
        finally:
            async with main_module._sse_clients_lock:
                main_module._sse_clients.remove(q1)
                main_module._sse_clients.remove(q2)

    async def test_broadcast_handles_full_queue_gracefully(self):
        """A full client queue loses its OLDEST event, not the newest.

        Dropping the newest would lose the terminal done/error event and
        leave the card stuck at "Downloading 100%" forever.
        """
        import app.main as main_module

        q_full: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=1)
        # Fill the queue
        q_full.put_nowait(SSEEvent(event="filler", job_id="x", data={}))

        async with main_module._sse_clients_lock:
            main_module._sse_clients.append(q_full)

        try:
            event = SSEEvent(event="status_change", job_id="test-2", data={"status": "done"})
            # Should not raise
            await main_module._broadcast_event(event)
            # Still bounded, and the stale filler was the one discarded.
            assert q_full.qsize() == 1
            kept = q_full.get_nowait()
            assert kept.job_id == "test-2"
            assert kept.event == "status_change"
        finally:
            async with main_module._sse_clients_lock:
                main_module._sse_clients.remove(q_full)

    async def test_on_queue_event_schedules_broadcast(self):
        """The synchronous _on_queue_event callback should schedule an
        async broadcast task on the running loop."""
        import app.main as main_module

        q: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=256)

        async with main_module._sse_clients_lock:
            main_module._sse_clients.append(q)

        # Set the module-level loop reference so _on_queue_event can schedule work
        original_loop = main_module._loop
        main_module._loop = asyncio.get_running_loop()

        try:
            event = SSEEvent(event="progress", job_id="test-3", data={"progress": 50.0})
            # Call the synchronous callback — it should schedule the async broadcast
            main_module._on_queue_event(event)

            # Give the task time to run
            await asyncio.sleep(0.05)

            assert not q.empty()
            received = q.get_nowait()
            assert received.event == "progress"
            assert received.job_id == "test-3"
        finally:
            main_module._loop = original_loop
            async with main_module._sse_clients_lock:
                main_module._sse_clients.remove(q)

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    async def test_queue_manager_events_reach_sse_clients(self, mock_extract, mock_download):
        """Integration: when a job is processed by QueueManager, the events
        should be broadcast to connected SSE client queues."""
        import app.main as main_module

        mock_extract.return_value = _make_metadata()
        mock_download.return_value = "/data/music/output.flac"

        q: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=256)

        async with main_module._sse_clients_lock:
            main_module._sse_clients.append(q)

        # Set the module-level loop reference so _on_queue_event can schedule work
        original_loop = main_module._loop
        main_module._loop = asyncio.get_running_loop()

        fresh_qm = QueueManager(max_concurrent=2, timeout=10, on_event=main_module._on_queue_event)
        original_qm = main_module.queue_manager
        main_module.queue_manager = fresh_qm

        try:
            job = Job(
                id="sse-test-job",
                url="https://youtube.com/watch?v=test",
                title="Test",
                thumbnail_url="https://img.example.com/t.jpg",
                duration=120.0,
            )
            fresh_qm.add_job(job)

            # Wait for job to complete
            for _ in range(100):
                j = fresh_qm.get_job("sse-test-job")
                if j and j.status == JobStatus.DONE:
                    break
                await asyncio.sleep(0.05)

            # Collect all events from the queue
            events = []
            while not q.empty():
                events.append(q.get_nowait())

            # Should have status_change events: downloading, converting, done
            status_events = [e for e in events if e.event == "status_change"]
            statuses = [e.data["status"] for e in status_events]
            assert "downloading" in statuses
            assert "done" in statuses

        finally:
            main_module._loop = original_loop
            main_module.queue_manager = original_qm
            async with main_module._sse_clients_lock:
                main_module._sse_clients.remove(q)

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    async def test_sse_events_contain_correct_structure(self, mock_extract, mock_download):
        """Verify SSE events have the expected fields: event, job_id, data."""
        import app.main as main_module

        mock_extract.return_value = _make_metadata()
        mock_download.side_effect = DownloadError("Test failure")

        q: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=256)

        async with main_module._sse_clients_lock:
            main_module._sse_clients.append(q)

        # Set the module-level loop reference so _on_queue_event can schedule work
        original_loop = main_module._loop
        main_module._loop = asyncio.get_running_loop()

        fresh_qm = QueueManager(max_concurrent=2, timeout=10, on_event=main_module._on_queue_event)
        original_qm = main_module.queue_manager
        main_module.queue_manager = fresh_qm

        try:
            job = Job(
                id="sse-error-job",
                url="https://youtube.com/watch?v=fail",
                title="Fail Track",
            )
            fresh_qm.add_job(job)

            for _ in range(100):
                j = fresh_qm.get_job("sse-error-job")
                if j and j.status == JobStatus.ERROR:
                    break
                await asyncio.sleep(0.05)

            events = []
            while not q.empty():
                events.append(q.get_nowait())

            # All events should be SSEEvent instances
            assert all(isinstance(e, SSEEvent) for e in events)

            # All events should have the correct job_id
            assert all(e.job_id == "sse-error-job" for e in events)

            # Error event should contain the error message
            error_events = [e for e in events if e.event == "error"]
            assert len(error_events) >= 1
            assert "Test failure" in error_events[0].data["error"]

        finally:
            main_module._loop = original_loop
            main_module.queue_manager = original_qm
            async with main_module._sse_clients_lock:
                main_module._sse_clients.remove(q)


# ===========================================================================
# Full flow integration
# ===========================================================================


class TestFullFlow:
    """End-to-end integration tests verifying the submit → metadata → download flow."""

    _blocking_download = staticmethod(_blocking_download)

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_submit_then_check_queue(self, mock_extract, mock_download, client_and_qm):
        """Submit a URL, verify it appears in the queue with correct metadata."""
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        release = threading.Event()
        mock_download.side_effect = self._blocking_download(release)

        try:
            # Submit
            submit_resp = client.post(
                "/download",
                json={"url": "https://youtube.com/watch?v=flow", "artist": "Test Artist"},
            )
            assert submit_resp.status_code == 200
            job_id = submit_resp.json()["id"]

            # Check queue while the job is still in flight
            queue_resp = client.get("/queue")
            jobs = queue_resp.json()
            assert len(jobs) == 1
            assert jobs[0]["id"] == job_id
            assert jobs[0]["title"] == "Never Gonna Give You Up"
            assert jobs[0]["artist"] == "Test Artist"
        finally:
            release.set()

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_submit_failure_then_retry_success(self, mock_extract, mock_download, client_and_qm):
        """Submit a URL that fails, then retry it successfully."""
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        mock_download.side_effect = DownloadError("Temporary error")

        # Submit
        resp = client.post("/download", json={"url": "https://youtube.com/watch?v=retry"})
        job_id = resp.json()["id"]

        # Wait for failure
        for _ in range(100):
            job = qm.get_job(job_id)
            if job and job.status == JobStatus.ERROR:
                break
            time.sleep(0.05)
        assert qm.get_job(job_id).status == JobStatus.ERROR

        # Verify error is in queue response
        queue_resp = client.get("/queue")
        jobs = queue_resp.json()
        failed_job = next(j for j in jobs if j["id"] == job_id)
        assert failed_job["status"] == "error"
        assert "Temporary error" in failed_job["error"]

        # Retry
        mock_download.side_effect = None
        mock_download.return_value = "/data/music/output.flac"

        retry_resp = client.post(f"/queue/{job_id}/retry")
        assert retry_resp.status_code == 200
        assert retry_resp.json()["status"] == "queued"

        # Wait for completion
        for _ in range(100):
            job = qm.get_job(job_id)
            if job and job.status == JobStatus.DONE:
                break
            time.sleep(0.05)
        assert qm.get_job(job_id).status == JobStatus.DONE

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_multiple_jobs_tracked_independently(self, mock_extract, mock_download, client_and_qm):
        """Submit multiple URLs and verify each is tracked independently."""
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        release = threading.Event()
        mock_download.side_effect = self._blocking_download(release)

        try:
            ids = []
            for i in range(3):
                resp = client.post("/download", json={"url": f"https://youtube.com/watch?v={i}"})
                ids.append(resp.json()["id"])

            # All should be unique
            assert len(set(ids)) == 3

            # Queue should have all 3 while they are still in flight
            queue_resp = client.get("/queue")
            assert {job["id"] for job in queue_resp.json()} == set(ids)
        finally:
            release.set()


# ===========================================================================
# URL validation at the API edge
# ===========================================================================


class TestUrlValidation:
    """yt-dlp's generic extractor would happily fetch anything on the
    container network, so unsupported URLs never reach it."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/x",
            "http://localhost:8000/health",
            "http://192.168.1.1/",
            "file:///etc/passwd",
            "ftp://youtube.com/watch?v=abc",
        ],
    )
    def test_unsupported_url_returns_422(self, url, client):
        resp = client.post("/download", json={"url": url})
        assert resp.status_code == 422

    @patch("app.main.extract_metadata")
    def test_supported_url_is_accepted(self, mock_extract, client):
        mock_extract.return_value = _make_metadata()
        resp = client.post("/download", json={"url": "https://youtu.be/abc"})
        assert resp.status_code == 200


# ===========================================================================
# Duplicate submissions
# ===========================================================================


class TestDuplicateSubmission:
    """Submitting the same URL twice would put two yt-dlp runs on one file."""

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_duplicate_in_flight_url_returns_409(
        self, mock_extract, mock_download, client_and_qm
    ):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()

        release = threading.Event()

        def blocking_download(job, on_progress, cancel_event=None, on_phase=None):
            release.wait(5)
            return "/data/music/output.flac"

        mock_download.side_effect = blocking_download

        url = "https://www.youtube.com/watch?v=dupe"
        try:
            first = client.post("/download", json={"url": url})
            assert first.status_code == 200

            second = client.post("/download", json={"url": url})
            assert second.status_code == 409
            assert "already in the queue" in second.json()["detail"]

            assert len(qm.get_jobs()) == 1
        finally:
            release.set()

        job_id = first.json()["id"]
        for _ in range(100):
            if qm.get_job(job_id).status == JobStatus.DONE:
                break
            time.sleep(0.05)
        assert qm.get_job(job_id).status == JobStatus.DONE


# ===========================================================================
# Metadata probe timeout
# ===========================================================================


class TestMetadataTimeout:
    """A slow probe must not hold POST /download open past nginx's timeout."""

    @patch("app.main.extract_metadata")
    def test_slow_metadata_still_enqueues_the_job(self, mock_extract, client_and_qm):
        client, qm = client_and_qm

        def slow_extract(url):
            time.sleep(0.5)
            return _make_metadata()

        mock_extract.side_effect = slow_extract

        with patch("app.main.METADATA_TIMEOUT_SECONDS", 0.05):
            resp = client.post("/download", json={"url": "https://youtube.com/watch?v=slow"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "queued"
        assert data["title"] is None
        assert data["duration"] is None
        assert len(qm.get_jobs()) == 1


# ===========================================================================
# Startup configuration check
# ===========================================================================


class TestStartupChecks:
    """A misconfigured DOWNLOAD_PATH or DATA_PATH must fail the container.

    Failing at startup beats turning every job (or every queue write) into a
    permission-denied traceback later.
    """

    def test_missing_download_path_fails_startup(self, monkeypatch, tmp_path):
        import app.main as main_module

        monkeypatch.setenv("DOWNLOAD_PATH", str(tmp_path / "does-not-exist"))

        with pytest.raises(RuntimeError, match="does not exist"):
            with TestClient(main_module.app):
                pass

    def test_unwritable_download_path_fails_startup(self, monkeypatch, tmp_path):
        import app.main as main_module

        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        monkeypatch.setenv("DOWNLOAD_PATH", str(readonly))

        try:
            with pytest.raises(RuntimeError, match="not writable"):
                with TestClient(main_module.app):
                    pass
        finally:
            readonly.chmod(0o700)

    def test_a_plain_file_as_download_path_fails_startup(self, monkeypatch, tmp_path):
        """A writable regular file passed os.access but is not a directory."""
        import app.main as main_module

        not_a_dir = tmp_path / "a-file"
        not_a_dir.write_text("x")
        monkeypatch.setenv("DOWNLOAD_PATH", str(not_a_dir))

        with pytest.raises(RuntimeError, match="not a directory"):
            with TestClient(main_module.app):
                pass

    def test_a_plain_file_as_data_path_fails_startup(self, monkeypatch, tmp_path):
        import app.main as main_module

        not_a_dir = tmp_path / "config-file"
        not_a_dir.write_text("x")
        monkeypatch.setenv("DATA_PATH", str(not_a_dir))

        with pytest.raises(RuntimeError, match="not a directory"):
            with TestClient(main_module.app):
                pass

    def test_writable_download_path_starts_up(self, client):
        assert client.get("/health").status_code == 200


# ===========================================================================
# CORS
# ===========================================================================


class TestCors:
    """Production is same-origin through nginx; CORS is opt-in for vite dev."""

    def test_no_origins_configured_by_default(self, monkeypatch):
        import app.main as main_module

        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        assert main_module._cors_origins() == []

    def test_empty_value_counts_as_unset(self, monkeypatch):
        import app.main as main_module

        monkeypatch.setenv("CORS_ORIGINS", "")
        assert main_module._cors_origins() == []

    def test_comma_separated_origins_are_parsed(self, monkeypatch):
        import app.main as main_module

        monkeypatch.setenv(
            "CORS_ORIGINS", "http://localhost:5173, http://127.0.0.1:5173 ,"
        )
        assert main_module._cors_origins() == [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]

    def test_no_cors_middleware_installed_by_default(self):
        import app.main as main_module
        from fastapi.middleware.cors import CORSMiddleware

        assert not any(
            m.cls is CORSMiddleware for m in main_module.app.user_middleware
        )

    def test_response_carries_no_wildcard_origin_header(self, client):
        resp = client.get("/health", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 200
        assert "access-control-allow-origin" not in resp.headers


class TestDataPathStartupChecks:
    """DATA_PATH holds queue.db, so it gets the same fail-fast treatment."""

    def test_missing_data_path_fails_startup(self, monkeypatch, tmp_path):
        import app.main as main_module

        monkeypatch.setenv("DATA_PATH", str(tmp_path / "does-not-exist"))

        with pytest.raises(RuntimeError, match="DATA_PATH.*does not exist"):
            with TestClient(main_module.app):
                pass

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="root ignores directory permissions"
    )
    def test_unwritable_data_path_fails_startup(self, monkeypatch, tmp_path):
        import app.main as main_module

        readonly = tmp_path / "readonly-config"
        readonly.mkdir()
        readonly.chmod(0o500)
        monkeypatch.setenv("DATA_PATH", str(readonly))

        try:
            with pytest.raises(RuntimeError, match="DATA_PATH.*not writable"):
                with TestClient(main_module.app):
                    pass
        finally:
            # Restore write permission so pytest can clean tmp_path up.
            readonly.chmod(0o700)


class TestQueueDatabaseLifecycle:
    """The store is opened, restored from, and closed by the app lifespan."""

    def test_startup_creates_the_database(self, client, isolated_paths):
        _, data_dir = isolated_paths

        assert client.get("/health").status_code == 200
        assert (data_dir / "queue.db").exists()

    def test_schema_version_is_stamped(self, client, isolated_paths):
        _, data_dir = isolated_paths

        conn = sqlite3.connect(data_dir / "queue.db")
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            conn.close()

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_a_submitted_job_is_written_to_the_database(
        self, mock_extract, mock_download, client_and_qm, isolated_paths
    ):
        client, qm = client_and_qm
        _, data_dir = isolated_paths
        mock_extract.return_value = _make_metadata()
        release = threading.Event()
        mock_download.side_effect = TestFullFlow._blocking_download(release)

        try:
            resp = client.post("/download", json={"url": "https://youtube.com/watch?v=persist"})
            job_id = resp.json()["id"]

            store = JobStore(data_dir / "queue.db")
            try:
                assert store.get(job_id) is not None
            finally:
                store.close()
        finally:
            release.set()

    @patch("app.main.extract_metadata")
    def test_a_queued_job_reappears_after_a_restart(
        self, mock_extract, fresh_app, isolated_paths
    ):
        """The acceptance criterion: restart, and the queue is still there."""
        app, qm = fresh_app
        _, data_dir = isolated_paths
        mock_extract.return_value = _make_metadata()
        release = threading.Event()

        with patch("app.queue_manager.download_audio") as mock_download:
            mock_download.side_effect = TestFullFlow._blocking_download(release)
            with TestClient(app) as client:
                job_id = client.post(
                    "/download", json={"url": "https://youtube.com/watch?v=restart"}
                ).json()["id"]
                assert len(client.get("/queue").json()) == 1
            # Shutting the client down "kills" the process mid-download.
            release.set()

        # A brand new manager, as after a real restart: nothing in memory.
        import app.main as main_module

        restarted_qm = QueueManager(
            max_concurrent=2, timeout=10, on_event=main_module._on_queue_event
        )
        main_module.queue_manager = restarted_qm

        with patch("app.queue_manager.download_audio") as mock_download:
            mock_download.return_value = "/data/music/output.flac"
            with TestClient(app) as client:
                job = restarted_qm.get_job(job_id)
                assert job is not None
                # It was interrupted mid-download, so it is re-queued with a
                # spent attempt rather than lost.
                assert job.attempts == 1
                assert job.restart_attempts == 1


class TestQueueViewOmitsFinishedJobs:
    """GET /queue is the in-flight view, not a history."""

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_done_jobs_are_omitted(self, mock_extract, mock_download, client_and_qm):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        mock_download.return_value = "/data/music/output.flac"

        job_id = client.post(
            "/download", json={"url": "https://youtube.com/watch?v=done"}
        ).json()["id"]

        for _ in range(100):
            job = qm.get_job(job_id)
            if job and job.status == JobStatus.DONE:
                break
            time.sleep(0.05)
        assert qm.get_job(job_id).status == JobStatus.DONE

        assert client.get("/queue").json() == []

    def test_cancelled_jobs_are_omitted(self, client_and_qm):
        client, qm = client_and_qm
        qm._jobs["cancelled"] = _make_job(id="cancelled", status=JobStatus.CANCELLED)

        assert client.get("/queue").json() == []

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_error_jobs_are_listed(self, mock_extract, mock_download, client_and_qm):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        mock_download.side_effect = DownloadError("boom")

        job_id = client.post(
            "/download", json={"url": "https://youtube.com/watch?v=fails"}
        ).json()["id"]

        for _ in range(100):
            job = qm.get_job(job_id)
            if job and job.status == JobStatus.ERROR:
                break
            time.sleep(0.05)

        assert [job["id"] for job in client.get("/queue").json()] == [job_id]

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_a_done_url_can_be_resubmitted(self, mock_extract, mock_download, client_and_qm):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        mock_download.return_value = "/data/music/output.flac"
        url = "https://youtube.com/watch?v=again"

        first = client.post("/download", json={"url": url}).json()["id"]
        for _ in range(100):
            job = qm.get_job(first)
            if job and job.status == JobStatus.DONE:
                break
            time.sleep(0.05)

        assert client.post("/download", json={"url": url}).status_code == 200

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_an_in_flight_url_is_rejected(self, mock_extract, mock_download, client_and_qm):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        release = threading.Event()
        mock_download.side_effect = TestFullFlow._blocking_download(release)
        url = "https://youtube.com/watch?v=busy"

        try:
            assert client.post("/download", json={"url": url}).status_code == 200
            resp = client.post("/download", json={"url": url})
            assert resp.status_code == 409
            assert "already in the queue" in resp.json()["detail"]
        finally:
            release.set()
