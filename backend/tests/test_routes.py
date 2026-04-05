"""Integration tests for API routes.

Tests the full request/response cycle using FastAPI's TestClient,
with mocked downloader and controlled queue behavior.  Covers:
  - POST /download (happy path, validation, metadata extraction failure)
  - GET /queue (empty, populated)
  - POST /queue/{id}/retry (happy path, errors)
  - GET /queue/stream SSE (event emission)
"""

import asyncio
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.downloader import DownloadError, TrackMetadata
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

        # Job should now be in the queue
        jobs = qm.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == job_id

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
        assert len(qm.get_jobs()) == 1

    @patch("app.main.extract_metadata")
    def test_multiple_submissions_create_separate_jobs(self, mock_extract, client_and_qm):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()

        resp1 = client.post("/download", json={"url": "https://youtube.com/watch?v=a"})
        resp2 = client.post("/download", json={"url": "https://youtube.com/watch?v=b"})

        assert resp1.json()["id"] != resp2.json()["id"]
        assert len(qm.get_jobs()) == 2

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

    @patch("app.main.extract_metadata")
    def test_queue_returns_all_submitted_jobs(self, mock_extract, client_and_qm):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()

        client.post("/download", json={"url": "https://youtube.com/watch?v=a"})
        client.post("/download", json={"url": "https://youtube.com/watch?v=b"})

        resp = client.get("/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    @patch("app.main.extract_metadata")
    def test_queue_returns_current_job_state(self, mock_extract, client_and_qm):
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()

        client.post("/download", json={"url": "https://youtube.com/watch?v=a"})

        resp = client.get("/queue")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["url"] == "https://youtube.com/watch?v=a"
        assert data[0]["title"] == "Never Gonna Give You Up"
        # Status could be queued or already progressing (downloading)
        assert data[0]["status"] in ["queued", "downloading", "converting", "done", "error"]


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
        """If a client queue is full, the event is dropped without error."""
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
            # Queue still has the original event, not the new one
            assert q_full.qsize() == 1
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

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_submit_then_check_queue(self, mock_extract, mock_download, client_and_qm):
        """Submit a URL, verify it appears in the queue with correct metadata."""
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        mock_download.return_value = "/data/music/output.flac"

        # Submit
        submit_resp = client.post(
            "/download",
            json={"url": "https://youtube.com/watch?v=flow", "artist": "Test Artist"},
        )
        assert submit_resp.status_code == 200
        job_id = submit_resp.json()["id"]

        # Check queue immediately
        queue_resp = client.get("/queue")
        jobs = queue_resp.json()
        assert len(jobs) == 1
        assert jobs[0]["id"] == job_id
        assert jobs[0]["title"] == "Never Gonna Give You Up"
        assert jobs[0]["artist"] == "Test Artist"

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
        mock_download.return_value = "/data/music/output.flac"

        ids = []
        for i in range(3):
            resp = client.post("/download", json={"url": f"https://youtube.com/watch?v={i}"})
            ids.append(resp.json()["id"])

        # All should be unique
        assert len(set(ids)) == 3

        # Queue should have all 3
        queue_resp = client.get("/queue")
        assert len(queue_resp.json()) == 3
