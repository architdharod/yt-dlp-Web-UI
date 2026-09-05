"""Integration tests for API routes.

Tests the full request/response cycle using FastAPI's TestClient,
with mocked downloader and controlled queue behavior.  Covers:
  - POST /download (happy path, validation, metadata extraction failure)
  - GET /queue (empty, populated)
  - POST /queue/{id}/retry (happy path, errors)
  - POST /queue/{id}/cancel (queued, running, terminal, unknown)
  - POST /queue/{id}/dismiss (errored, wrong state, unknown)
  - GET /queue/stream SSE (event emission)
"""

import asyncio
import logging
import os
import sqlite3
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.downloader import DownloadError, TrackMetadata
from app.job_store import SCHEMA_VERSION, JobStore
from app.models import Job, JobKind, JobStatus, SSEEvent
from app.queue_manager import QueueManager

from mutagen.flac import FLAC

from tests.conftest import minimal_flac_bytes


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
    def test_submit_records_the_folder_the_job_will_land_in(
        self, mock_extract, client_and_qm
    ):
        """The move guard reads ``target_dir``, so it exists from submit on.

        yt-dlp's artist is what most downloads are filed under -- nobody types
        one -- so the guard has to know it before the download thread starts,
        not after.
        """
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata(artist="Blender", album="Deep")

        resp = client.post("/download", json={"url": "https://youtube.com/watch?v=abc"})

        assert resp.status_code == 200
        assert qm.get_job(resp.json()["id"]).target_dir == "Blender/Deep"

    @patch("app.main.extract_metadata")
    def test_a_job_with_no_album_targets_the_artist_folder_alone(
        self, mock_extract, client_and_qm
    ):
        """No album is a loose Single: the artist folder and nothing below it."""
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata(artist="Blender", album=None)

        resp = client.post("/download", json={"url": "https://youtube.com/watch?v=abc"})

        assert qm.get_job(resp.json()["id"]).target_dir == "Blender"

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_a_probe_that_failed_and_no_typed_artist_makes_the_target_a_guess(
        self, mock_extract, mock_download, client_and_qm, isolated_paths
    ):
        """"Unknown Artist" is not a destination, and must not guard one.

        With no probe and nothing typed, ``target_dir`` is the fallback while
        the download thread will file the track under whatever yt-dlp says.
        Guarding the fallback folder leaves the real one unguarded, so the job
        counts as unresolved instead and every move waits for it.
        """
        client, qm = client_and_qm
        download_dir, _ = isolated_paths
        (download_dir / "Bonobo").mkdir(parents=True, exist_ok=True)
        mock_extract.side_effect = DownloadError("no probe")
        release = threading.Event()
        mock_download.side_effect = _blocking_download(release)

        try:
            resp = client.post("/download", json={"url": "https://youtube.com/watch?v=a"})
            job = qm.get_job(resp.json()["id"])

            assert job.target_dir == "Unknown Artist"
            assert job.target_guessed is True

            in_flight = qm.in_flight_library_targets()
            assert in_flight.targets == []
            assert in_flight.unresolved == 1

            move = client.post(
                "/library/move", json={"path": "Bonobo", "artist": "Bonobo (UK)"}
            )
            assert move.status_code == 409
            # Named by url: the probe never returned a title to call it by.
            assert move.json()["detail"]["conflicts"] == [
                "https://youtube.com/watch?v=a"
            ]
        finally:
            release.set()

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_a_typed_artist_is_a_real_target_even_when_the_probe_failed(
        self, mock_extract, mock_download, client_and_qm
    ):
        """The user's own name is not a guess: nothing can overrule it."""
        client, qm = client_and_qm
        mock_extract.side_effect = DownloadError("no probe")
        release = threading.Event()
        mock_download.side_effect = _blocking_download(release)

        try:
            resp = client.post(
                "/download",
                json={"url": "https://youtube.com/watch?v=a", "artist": "Lone"},
            )
            job = qm.get_job(resp.json()["id"])

            assert job.target_guessed is False
            assert qm.in_flight_library_targets().targets == ["Lone"]
        finally:
            release.set()

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

    def test_retry_nonexistent_job_returns_404(self, client):
        """An unknown id is a missing resource, not a bad state."""
        resp = client.post("/queue/nonexistent-id/retry")
        assert resp.status_code == 404
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

        def blocking_download(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
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


class TestLifespanShutdown:
    """Shutdown is a chain of independent steps: no teardown step can strand
    the ones after it.

    Each test breaks exactly one step -- the startup probe, the rescan hook,
    the job store -- and asks that the lifespan still exit cleanly, say what
    went wrong in the log, and carry out the steps that come after the broken
    one.  ``store.close()`` is the one that has to happen.
    """

    @staticmethod
    def _spy_on_store_close(monkeypatch):
        """Record every ``JobStore.close`` call, then do the real close.

        ``JobStore.close`` is idempotent and an unclosed WAL database still
        answers ``SELECT 1``, so reading the file back proves nothing about
        whether teardown actually reached the close.  A spy does.
        """
        calls = []
        original = JobStore.close

        def spy(self):
            calls.append(self)
            return original(self)

        monkeypatch.setattr(JobStore, "close", spy)
        return calls

    def test_a_failing_startup_check_does_not_strand_the_close(
        self, monkeypatch, caplog
    ):
        import app.main as main_module
        from app.rescan import RescanHook

        async def boom(self):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(RescanHook, "check_lidarr_config", boom)
        closes = self._spy_on_store_close(monkeypatch)

        with caplog.at_level(logging.ERROR):
            with TestClient(main_module.app) as client:
                assert client.get("/health").status_code == 200

        assert "The startup check task failed" in caplog.text
        assert closes, "teardown never reached store.close()"

    def test_a_failing_hook_shutdown_does_not_strand_the_close(
        self, monkeypatch, caplog
    ):
        import app.main as main_module
        from app.rescan import RescanHook

        async def boom(self):
            raise RuntimeError("hook would not stop")

        monkeypatch.setattr(RescanHook, "aclose", boom)
        closes = self._spy_on_store_close(monkeypatch)

        with caplog.at_level(logging.ERROR):
            with TestClient(main_module.app) as client:
                assert client.get("/health").status_code == 200

        assert "Could not stop the rescan hook" in caplog.text
        assert closes, "teardown never reached store.close()"

    def test_a_failing_close_does_not_strand_the_steps_after_it(
        self, monkeypatch, caplog
    ):
        import app.main as main_module

        def boom(self):
            raise RuntimeError("the database would not close")

        monkeypatch.setattr(JobStore, "close", boom)

        with caplog.at_level(logging.ERROR):
            with TestClient(main_module.app) as client:
                assert client.get("/health").status_code == 200

        assert "Could not close the job store" in caplog.text
        # The last step still ran: a stale loop reference would have SSE
        # events posted into a dead loop on the next boot.
        assert main_module._loop is None

    def test_teardown_releases_the_tagging_thread(self, monkeypatch):
        """Otherwise a lookup nobody is waiting for any more keeps a thread
        alive past the point where the app has stopped serving."""
        import app.main as main_module

        closes = []
        original = QueueManager.close
        monkeypatch.setattr(
            QueueManager,
            "close",
            lambda self: (closes.append(self), original(self))[1],
        )

        with TestClient(main_module.app) as client:
            assert client.get("/health").status_code == 200

        assert closes == [main_module.queue_manager]

    def test_a_failing_tagging_release_does_not_strand_the_store(
        self, monkeypatch, caplog
    ):
        """The queue is released before the store, because it is the last
        thing still writing to it -- so a failure there must not be what stops
        the database from being closed."""
        import app.main as main_module

        def boom(self):
            raise RuntimeError("the tagging thread would not go")

        monkeypatch.setattr(QueueManager, "close", boom)
        closes = self._spy_on_store_close(monkeypatch)

        with caplog.at_level(logging.ERROR):
            with TestClient(main_module.app) as client:
                assert client.get("/health").status_code == 200

        assert "Could not release the tagging thread" in caplog.text
        assert closes, "teardown never reached store.close()"

    def test_a_malformed_lidarr_url_boots_and_shuts_down_without_a_traceback(
        self, monkeypatch, isolated_paths
    ):
        """A smoke test, not a failure-path test.

        ``notaport`` is not a port, so httpx raises ``InvalidURL`` somewhere
        inside the probe.  Nothing here forces the failure to reach the
        lifespan -- the probe swallows it -- so this only asserts that the
        real configuration path boots, serves, and tears down.
        """
        import app.main as main_module

        monkeypatch.setenv("LIDARR_URL", "http://lidarr:notaport")
        monkeypatch.setenv("LIDARR_API_KEY", "x")

        with TestClient(main_module.app) as client:
            assert client.get("/health").status_code == 200

        assert main_module.rescan_hook is None


class TestStartupBanner:
    """The boot log is where a homelab finds out what the container thinks it
    was configured with."""

    def test_it_reports_the_tag_fix_configuration(self, monkeypatch, caplog):
        import app.main as main_module

        monkeypatch.setenv("TAG_FIX_ENABLED", "false")
        monkeypatch.setenv("MUSICBRAINZ_CONTACT", "someone@example.com")

        with caplog.at_level(logging.INFO, logger="app.main"):
            with TestClient(main_module.app) as client:
                assert client.get("/health").status_code == 200

        assert "TAG_FIX_ENABLED          = False" in caplog.text
        assert "MUSICBRAINZ_CONTACT      = someone@example.com" in caplog.text
        assert (
            f"TAG_FIX_TIMEOUT_SECONDS  = {main_module.queue_manager.tag_fix_timeout}"
            in caplog.text
        )


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


# ===========================================================================
# POST /queue/{id}/cancel
# ===========================================================================


class TestCancelEndpoint:
    """Cancel is the only way to stop a job that is already running."""

    def test_cancelling_a_queued_job_returns_it_cancelled(self, client_and_qm):
        client, qm = client_and_qm
        qm._jobs["job-1"] = _make_job()

        resp = client.post("/queue/job-1/cancel")

        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_a_cancelled_job_is_gone_from_the_queue(self, client_and_qm):
        client, qm = client_and_qm
        qm._jobs["job-1"] = _make_job()

        client.post("/queue/job-1/cancel")

        assert client.get("/queue").json() == []

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_cancelling_a_running_job_reaches_cancelled(
        self, mock_extract, mock_download, client_and_qm
    ):
        """The response comes back at once; the state follows the thread."""
        client, qm = client_and_qm
        mock_extract.return_value = _make_metadata()
        release = threading.Event()

        def fake_download(job, on_progress, cancel=None, on_phase=None, on_filed=None, on_target=None):
            release.wait(timeout=5)
            raise DownloadError("Download cancelled")

        mock_download.side_effect = fake_download

        job_id = client.post(
            "/download", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
        ).json()["id"]
        for _ in range(200):
            if qm.get_job(job_id).status == JobStatus.DOWNLOADING:
                break
            time.sleep(0.01)

        resp = client.post(f"/queue/{job_id}/cancel")
        assert resp.status_code == 200

        release.set()
        for _ in range(500):
            if qm.get_job(job_id).status == JobStatus.CANCELLED:
                break
            time.sleep(0.01)
        assert qm.get_job(job_id).status == JobStatus.CANCELLED
        assert qm.get_job(job_id).error is None

    def test_cancelling_an_unknown_job_returns_404(self, client):
        resp = client.post("/queue/nonexistent-id/cancel")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    @pytest.mark.parametrize("status", ["done", "error", "cancelled"])
    def test_cancelling_a_terminal_job_returns_400(self, status, client_and_qm):
        """A stale UI, not a valid request: the answer must not be "fine"."""
        client, qm = client_and_qm
        qm._jobs["job-1"] = _make_job(status=JobStatus(status))

        resp = client.post("/queue/job-1/cancel")

        assert resp.status_code == 400
        assert "cancelled" in resp.json()["detail"]

    def test_a_cancelled_job_cannot_be_retried(self, client_and_qm):
        client, qm = client_and_qm
        qm._jobs["job-1"] = _make_job()
        client.post("/queue/job-1/cancel")

        resp = client.post("/queue/job-1/retry")

        assert resp.status_code == 400



class TestRetryGuardsTheLibrary:
    """A retry is a second submission, and the library has moved on since."""

    def _errored_tagging_job(self, path: str, job_id: str = "tag-1") -> Job:
        return _make_job(
            id=job_id,
            url="",
            kind=JobKind.TAGGING,
            status=JobStatus.ERROR,
            path=path,
            error="tags not fixed: MusicBrainz unavailable",
            detail="tags not fixed: MusicBrainz unavailable",
        )

    def test_an_overlapping_tagging_job_makes_it_a_409(self, client_and_qm):
        client, qm = client_and_qm
        qm._jobs["tag-1"] = self._errored_tagging_job("Bonobo/Migration")
        qm._jobs["tag-2"] = _make_job(
            id="tag-2",
            url="",
            kind=JobKind.TAGGING,
            status=JobStatus.TAGGING,
            path="Bonobo/Migration/01 Kerala.flac",
        )

        resp = client.post("/queue/tag-1/retry")

        assert resp.status_code == 409
        # A plain string, which is the only shape the retry error line unwraps.
        detail = resp.json()["detail"]
        assert isinstance(detail, str) and "already being tagged" in detail

    def test_a_refused_retry_leaves_the_row_exactly_as_it_was(self, client_and_qm):
        client, qm = client_and_qm
        job = self._errored_tagging_job("Bonobo/Migration")
        qm._jobs["tag-1"] = job
        qm._jobs["tag-2"] = _make_job(
            id="tag-2",
            url="",
            kind=JobKind.TAGGING,
            status=JobStatus.TAGGING,
            path="Bonobo/Migration",
        )

        assert client.post("/queue/tag-1/retry").status_code == 409

        assert job.status is JobStatus.ERROR
        assert job.detail == "tags not fixed: MusicBrainz unavailable"
        assert job.attempts == 0

    def test_a_download_aiming_into_the_folder_makes_it_a_409(self, client_and_qm):
        client, qm = client_and_qm
        qm._jobs["tag-1"] = self._errored_tagging_job("Bonobo/Migration/01 Kerala.flac")
        qm._jobs["dl"] = _make_job(
            id="dl", status=JobStatus.DOWNLOADING, target_dir="Bonobo/Migration"
        )

        resp = client.post("/queue/tag-1/retry")

        assert resp.status_code == 409
        assert "a download is in progress" in resp.json()["detail"]

    def test_a_download_with_no_destination_yet_makes_it_a_409(self, client_and_qm):
        client, qm = client_and_qm
        qm._jobs["tag-1"] = self._errored_tagging_job("Bonobo/Migration")
        qm._jobs["dl"] = _make_job(id="dl", status=JobStatus.QUEUED, target_dir=None)

        resp = client.post("/queue/tag-1/retry")

        assert resp.status_code == 409
        assert "has not resolved its destination" in resp.json()["detail"]

    def test_an_unrelated_tagging_job_does_not_block_it(self, client_and_qm):
        client, qm = client_and_qm
        qm._jobs["tag-1"] = self._errored_tagging_job("Bonobo/Migration")
        qm._jobs["tag-2"] = _make_job(
            id="tag-2",
            url="",
            kind=JobKind.TAGGING,
            status=JobStatus.TAGGING,
            path="Bonobo/Black Sands",
        )

        with patch("app.queue_manager.QueueManager._dispatch_tagging_job"):
            resp = client.post("/queue/tag-1/retry")

        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"

    @patch("app.queue_manager.download_audio")
    @patch("app.main.extract_metadata")
    def test_a_download_retry_still_works(
        self, mock_extract, mock_download, client_and_qm
    ):
        """The guards are the tagging kind's; a download retry is untouched."""
        client, qm = client_and_qm
        qm._jobs["job-1"] = _make_job(status=JobStatus.ERROR, error="boom")
        qm._jobs["tag-2"] = _make_job(
            id="tag-2",
            url="",
            kind=JobKind.TAGGING,
            status=JobStatus.TAGGING,
            path="Bonobo/Migration",
        )

        with patch("app.queue_manager.QueueManager._dispatch"):
            resp = client.post("/queue/job-1/retry")

        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"

    async def test_a_retry_waits_for_a_move_that_is_already_running(self):
        """The lock, not the guard: a move holding it must finish first."""
        import app.main as main_module
        from app.mover import LIBRARY_WRITE_LOCK

        qm = QueueManager(max_concurrent=1, timeout=10)
        original = main_module.queue_manager
        main_module.queue_manager = qm
        qm._jobs["tag-1"] = self._errored_tagging_job("Bonobo/Migration")

        try:
            with patch("app.queue_manager.QueueManager._dispatch_tagging_job"):
                async with LIBRARY_WRITE_LOCK:
                    waiting = asyncio.create_task(main_module.retry_job("tag-1"))
                    await asyncio.sleep(0.05)
                    assert not waiting.done()
                    assert qm.get_job("tag-1").status is JobStatus.ERROR
                job = await asyncio.wait_for(waiting, timeout=2)

            assert job.status is JobStatus.QUEUED
        finally:
            main_module.queue_manager = original
            qm.close()


# ===========================================================================
# POST /queue/{id}/dismiss
# ===========================================================================


class TestDismissEndpoint:
    """Errored jobs stay in the queue until somebody says they have been seen."""

    def test_dismissing_an_errored_job_removes_it(self, client_and_qm):
        client, qm = client_and_qm
        qm._jobs["job-1"] = _make_job(status=JobStatus.ERROR, error="boom")

        resp = client.post("/queue/job-1/dismiss")

        assert resp.status_code == 204
        assert client.get("/queue").json() == []
        assert qm.get_job("job-1") is None

    def test_dismissing_an_unknown_job_returns_404(self, client):
        resp = client.post("/queue/nonexistent-id/dismiss")
        assert resp.status_code == 404

    @pytest.mark.parametrize("status", ["queued", "downloading", "done", "cancelled"])
    def test_dismissing_a_job_that_is_not_errored_returns_400(
        self, status, client_and_qm
    ):
        client, qm = client_and_qm
        qm._jobs["job-1"] = _make_job(status=JobStatus(status))

        resp = client.post("/queue/job-1/dismiss")

        assert resp.status_code == 400
        assert "errored" in resp.json()["detail"]
        assert qm.get_job("job-1") is not None

    def test_a_dismissed_job_is_deleted_from_the_database(self, client_and_qm):
        client, qm = client_and_qm
        job = _make_job(status=JobStatus.ERROR, error="boom")
        qm._jobs[job.id] = job
        qm._persist(job)
        assert qm._store.get(job.id) is not None

        client.post(f"/queue/{job.id}/dismiss")

        assert qm._store.get(job.id) is None


def test_a_queue_event_whose_handling_raises_is_logged(client, caplog):
    """Nothing awaits the scheduled coroutine, so it has to log for itself.

    Without the done-callback an exception in ``_handle_event`` vanished into
    a future nobody read: the rescan simply never happened and no line said so.
    """
    import app.main as main_module

    class ExplodingHook:
        def notify(self, paths):
            raise RuntimeError("boom")

    original = main_module.rescan_hook
    main_module.rescan_hook = ExplodingHook()
    try:
        with caplog.at_level(logging.ERROR, logger="app.main"):
            main_module._on_queue_event(
                SSEEvent(event="library_changed", data={"paths": ["Bonobo"]})
            )
            deadline = time.time() + 5
            while (
                "Handling a queue event failed" not in caplog.text
                and time.time() < deadline
            ):
                time.sleep(0.01)
    finally:
        main_module.rescan_hook = original

    assert "Handling a queue event failed" in caplog.text
    assert "boom" in caplog.text


# ===========================================================================
# The tagging state over the API
# ===========================================================================


class TestTaggingOverTheApi:
    """A job being tagged is in flight, and its note travels with it."""

    def test_a_tagging_job_is_listed(self, client_and_qm):
        client, qm = client_and_qm
        qm._jobs["tagging"] = _make_job(
            id="tagging",
            status=JobStatus.TAGGING,
            result_path="Bonobo/Migration/Kerala.flac",
        )

        [row] = client.get("/queue").json()

        assert row["id"] == "tagging"
        assert row["status"] == "tagging"
        assert row["detail"] is None

    def test_a_done_job_leaves_the_view_even_with_a_note(self, client_and_qm):
        client, qm = client_and_qm
        qm._jobs["done"] = _make_job(
            id="done", status=JobStatus.DONE, detail="tags not fixed: no match"
        )

        assert client.get("/queue").json() == []

    def test_a_tagging_job_can_be_cancelled(self, client_and_qm):
        client, qm = client_and_qm
        qm._jobs["tagging"] = _make_job(id="tagging", status=JobStatus.TAGGING)

        resp = client.post("/queue/tagging/cancel")

        assert resp.status_code == 200
        # It stays in `tagging` until the fix reaches its next checkpoint; the
        # cancel is the persisted request, not the verdict.
        assert resp.json()["status"] == "tagging"
        assert qm.get_job("tagging").cancel_requested is True

    async def test_the_status_change_carries_tagging_and_its_note(self):
        """The SSE payload a client applies without re-reading the queue."""
        import app.main as main_module

        q: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=256)
        async with main_module._sse_clients_lock:
            main_module._sse_clients.append(q)
        original_loop = main_module._loop
        main_module._loop = asyncio.get_running_loop()

        qm = QueueManager(
            max_concurrent=1, timeout=10, on_event=main_module._on_queue_event
        )
        job = _make_job(id="tag-sse", status=JobStatus.QUEUED)
        qm._jobs[job.id] = job

        try:
            qm._update_status("tag-sse", JobStatus.TAGGING)
            qm._finish_tagged("tag-sse", "tags not fixed: no match")
            await asyncio.sleep(0.05)

            events = []
            while not q.empty():
                events.append(q.get_nowait())

            statuses = [event.data["status"] for event in events]
            assert statuses == ["tagging", "done"]
            assert "detail" not in events[0].data
            assert events[1].data["detail"] == "tags not fixed: no match"
        finally:
            main_module._loop = original_loop
            async with main_module._sse_clients_lock:
                main_module._sse_clients.remove(q)


# ===========================================================================
# POST /library/tag (phase 9)
# ===========================================================================


def _library_track(download_dir, relative: str, *, title: str = "Kerala") -> str:
    """Write a real FLAC at *relative* under the library root and return it."""
    path = download_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(minimal_flac_bytes(44100 * 183))
    audio = FLAC(path)
    audio["TITLE"] = title
    audio.save()
    return relative


@pytest.fixture()
def no_real_lookup():
    """Never let a route test reach MusicBrainz.

    ``POST /library/tag`` starts the job before it answers, so a test that only
    checked the response body would otherwise run a real lookup against the
    real service.
    """
    with patch("app.queue_manager.fix_track") as mock_fix:
        from app.tagger import TagFixResult

        mock_fix.return_value = TagFixResult(matched=True, changed=False)
        yield mock_fix


class TestTagEndpoint:
    """The Library tab's two 'Update metadata' actions, as an API."""

    def test_a_track_is_accepted_and_queued(
        self, client_and_qm, isolated_paths, no_real_lookup
    ):
        client, qm = client_and_qm
        download_dir, _ = isolated_paths
        path = _library_track(download_dir, "Bonobo/Migration/01 Kerala.flac")

        resp = client.post("/library/tag", json={"path": path})

        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "tagging"
        assert body["status"] in ("queued", "tagging", "done")
        assert body["path"] == path
        assert body["title"] == "Kerala"
        assert body["artist"] == "Bonobo" and body["album"] == "Migration"
        assert body["url"] == ""

    def test_a_track_with_no_title_tag_is_named_after_its_file(
        self, client, isolated_paths, no_real_lookup
    ):
        download_dir, _ = isolated_paths
        path = download_dir / "Bonobo" / "Migration" / "Untitled.flac"
        path.parent.mkdir(parents=True)
        path.write_bytes(minimal_flac_bytes(44100 * 183))

        resp = client.post(
            "/library/tag", json={"path": "Bonobo/Migration/Untitled.flac"}
        )

        assert resp.json()["title"] == "Untitled"

    def test_an_album_folder_is_accepted(self, client, isolated_paths, no_real_lookup):
        download_dir, _ = isolated_paths
        _library_track(download_dir, "Bonobo/Migration/01 Kerala.flac")

        resp = client.post("/library/tag", json={"path": "Bonobo/Migration"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Migration" and body["album"] == "Migration"

    def test_a_loose_single_is_accepted_with_no_album(
        self, client, isolated_paths, no_real_lookup
    ):
        download_dir, _ = isolated_paths
        _library_track(download_dir, "Bonobo/Kerala.flac")

        resp = client.post("/library/tag", json={"path": "Bonobo/Kerala.flac"})

        assert resp.status_code == 200
        assert resp.json()["album"] is None

    def test_a_track_in_a_disc_subfolder_is_accepted_as_the_albums(
        self, client, isolated_paths, no_real_lookup
    ):
        """A disc subfolder is not a level of the library the domain model
        has: its tracks are the Album's, and the album pass over ``Migration``
        already tags this file.  The row's own button must reach it too."""
        download_dir, _ = isolated_paths
        path = _library_track(
            download_dir, "Bonobo/Migration/Disc 2/01 Kerala.flac", title="Kerala"
        )

        resp = client.post("/library/tag", json={"path": path})

        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "tagging" and body["path"] == path
        assert body["artist"] == "Bonobo" and body["album"] == "Migration"
        assert body["title"] == "Kerala"

    def test_an_artist_folder_is_refused(self, client, isolated_paths):
        download_dir, _ = isolated_paths
        _library_track(download_dir, "Bonobo/Migration/01 Kerala.flac")

        resp = client.post("/library/tag", json={"path": "Bonobo"})

        assert resp.status_code == 400
        assert "album folder or a single track" in resp.json()["detail"]

    def test_a_disc_subfolder_is_refused(self, client, isolated_paths):
        download_dir, _ = isolated_paths
        _library_track(download_dir, "Bonobo/Migration/Disc 1/01 Kerala.flac")

        resp = client.post("/library/tag", json={"path": "Bonobo/Migration/Disc 1"})

        assert resp.status_code == 400

    def test_the_root_is_refused(self, client):
        assert client.post("/library/tag", json={"path": ""}).status_code == 422

    def test_the_trash_is_refused(self, client, isolated_paths):
        download_dir, _ = isolated_paths
        _library_track(download_dir, ".trash/20260101/Bonobo/Migration/x.flac")

        resp = client.post("/library/tag", json={"path": ".trash/20260101"})

        assert resp.status_code == 400
        assert "trash" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "path", ["../etc", "Bonobo/../../etc", "Bonobo\\Migration", "Bonobo//x.flac"]
    )
    def test_a_malformed_path_is_refused(self, client, path):
        assert client.post("/library/tag", json={"path": path}).status_code == 400

    def test_a_path_that_is_not_there_is_a_404(self, client):
        resp = client.post("/library/tag", json={"path": "Bonobo/Migration"})

        assert resp.status_code == 404

    def test_a_file_that_is_not_audio_is_refused(self, client, isolated_paths):
        download_dir, _ = isolated_paths
        notes = download_dir / "Bonobo" / "Migration" / "notes.txt"
        notes.parent.mkdir(parents=True)
        notes.write_text("hello")

        resp = client.post(
            "/library/tag", json={"path": "Bonobo/Migration/notes.txt"}
        )

        assert resp.status_code == 400
        assert "not audio" in resp.json()["detail"]

    def test_a_second_job_for_the_same_path_is_a_409(
        self, client_and_qm, isolated_paths
    ):
        client, qm = client_and_qm
        download_dir, _ = isolated_paths
        path = _library_track(download_dir, "Bonobo/Migration/01 Kerala.flac")
        release = threading.Event()

        with patch("app.queue_manager.fix_track") as mock_fix:
            from app.tagger import TagFixResult

            mock_fix.side_effect = lambda *a, **k: (
                release.wait(timeout=5),
                TagFixResult(matched=True),
            )[1]
            assert client.post("/library/tag", json={"path": path}).status_code == 200

            resp = client.post("/library/tag", json={"path": path})
            assert resp.status_code == 409
            assert "already being tagged" in resp.json()["detail"]["message"]
            assert resp.json()["detail"]["conflicts"] == [path]

            # And the album that contains it, from the other end.
            assert (
                client.post(
                    "/library/tag", json={"path": "Bonobo/Migration"}
                ).status_code
                == 409
            )
            release.set()

    def test_a_download_aiming_at_the_folder_is_a_409(
        self, client_and_qm, isolated_paths
    ):
        client, qm = client_and_qm
        download_dir, _ = isolated_paths
        path = _library_track(download_dir, "Bonobo/Migration/01 Kerala.flac")
        qm._jobs["dl"] = _make_job(
            id="dl", status=JobStatus.DOWNLOADING, target_dir="Bonobo/Migration"
        )

        resp = client.post("/library/tag", json={"path": path})

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "a download is in progress" in detail["message"]
        assert detail["conflicts"] == ["Bonobo/Migration"]

    def test_a_download_elsewhere_does_not_block_it(
        self, client_and_qm, isolated_paths, no_real_lookup
    ):
        client, qm = client_and_qm
        download_dir, _ = isolated_paths
        path = _library_track(download_dir, "Bonobo/Migration/01 Kerala.flac")
        qm._jobs["dl"] = _make_job(
            id="dl", status=JobStatus.DOWNLOADING, target_dir="Floating Points/Crush"
        )

        assert client.post("/library/tag", json={"path": path}).status_code == 200

    def test_a_download_with_no_destination_yet_is_a_409(
        self, client_and_qm, isolated_paths
    ):
        """It could be about to land in this very folder; nobody can say."""
        client, qm = client_and_qm
        download_dir, _ = isolated_paths
        path = _library_track(download_dir, "Bonobo/Migration/01 Kerala.flac")
        qm._jobs["dl"] = _make_job(
            id="dl", status=JobStatus.DOWNLOADING, target_dir=None, title="A Download"
        )

        resp = client.post("/library/tag", json={"path": path})

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "has not resolved its destination" in detail["message"]
        assert detail["conflicts"] == ["A Download"]

    def test_a_symlinked_library_root_still_works(
        self, client_and_qm, tmp_path, monkeypatch, no_real_lookup
    ):
        """`is_reserved` compares against the resolved root, so this must too.

        A DOWNLOAD_PATH that is a symlink (the ordinary shape of a container
        bind mount) made every real path look like it was outside the library.
        """
        real = tmp_path / "real-library"
        real.mkdir()
        link = tmp_path / "library-link"
        link.symlink_to(real, target_is_directory=True)
        monkeypatch.setenv("DOWNLOAD_PATH", str(link))
        path = _library_track(real, "Bonobo/Migration/01 Kerala.flac")

        client, _ = client_and_qm
        resp = client.post("/library/tag", json={"path": path})

        assert resp.status_code == 200, resp.json()

    def test_the_queue_view_carries_the_n_of_m_fields(
        self, client_and_qm, isolated_paths
    ):
        client, qm = client_and_qm
        qm._jobs["tag-1"] = Job(
            id="tag-1",
            url="",
            kind=JobKind.TAGGING,
            status=JobStatus.TAGGING,
            path="Bonobo/Migration",
            title="Migration",
            progress_done=7,
            progress_total=12,
        )

        [row] = client.get("/queue").json()

        assert row["progress_done"] == 7 and row["progress_total"] == 12
        assert row["kind"] == "tagging"

    def test_a_finished_run_wakes_the_rescan_hook(
        self, client_and_qm, isolated_paths
    ):
        """`library_changed` is the single announcement the hook listens on,
        so a manual tag run reaches Navidrome and Lidarr for free."""
        import app.main as main_module

        client, qm = client_and_qm
        download_dir, _ = isolated_paths
        path = _library_track(download_dir, "Bonobo/Migration/01 Kerala.flac")

        hook = MagicMock()
        original = main_module.rescan_hook
        main_module.rescan_hook = hook
        try:
            with patch("app.queue_manager.fix_track") as mock_fix:
                from app.tagger import TagFixResult

                mock_fix.return_value = TagFixResult(matched=True, changed=True)
                client.post("/library/tag", json={"path": path})

                deadline = time.monotonic() + 5
                while not hook.notify.called and time.monotonic() < deadline:
                    time.sleep(0.02)
        finally:
            main_module.rescan_hook = original

        hook.notify.assert_called_with([path])
