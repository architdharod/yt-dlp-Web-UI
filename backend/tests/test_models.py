"""Tests for Pydantic models: validation, serialization, and SSE event structure."""

import pytest
from pydantic import ValidationError

from app.models import DownloadRequest, HealthResponse, Job, JobStatus, SSEEvent


# ---------------------------------------------------------------------------
# DownloadRequest tests
# ---------------------------------------------------------------------------


class TestDownloadRequest:
    """Validate DownloadRequest schema: required URL, optional artist/album."""

    def test_valid_url_only(self):
        req = DownloadRequest(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert req.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert req.artist is None
        assert req.album is None

    def test_valid_with_artist_and_album(self):
        req = DownloadRequest(
            url="https://soundcloud.com/artist/track",
            artist="Rick Astley",
            album="Whenever You Need Somebody",
        )
        assert req.url == "https://soundcloud.com/artist/track"
        assert req.artist == "Rick Astley"
        assert req.album == "Whenever You Need Somebody"

    def test_valid_with_artist_only(self):
        req = DownloadRequest(url="https://youtube.com/watch?v=abc", artist="Artist")
        assert req.artist == "Artist"
        assert req.album is None

    def test_valid_with_album_only(self):
        req = DownloadRequest(url="https://youtube.com/watch?v=abc", album="Album")
        assert req.artist is None
        assert req.album == "Album"

    def test_missing_url_raises_validation_error(self):
        with pytest.raises(ValidationError) as exc_info:
            DownloadRequest()  # type: ignore[call-arg]
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("url",) for e in errors)

    def test_empty_url_raises_validation_error(self):
        with pytest.raises(ValidationError) as exc_info:
            DownloadRequest(url="")
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("url",) for e in errors)

    def test_serialization_roundtrip(self):
        req = DownloadRequest(url="https://youtube.com/watch?v=abc", artist="A", album="B")
        data = req.model_dump()
        assert data == {"url": "https://youtube.com/watch?v=abc", "artist": "A", "album": "B"}
        restored = DownloadRequest.model_validate(data)
        assert restored == req


# ---------------------------------------------------------------------------
# JobStatus enum tests
# ---------------------------------------------------------------------------


class TestJobStatus:
    """Verify JobStatus enum values and string behavior."""

    def test_all_statuses_exist(self):
        expected = {"queued", "downloading", "converting", "done", "error"}
        actual = {s.value for s in JobStatus}
        assert actual == expected

    def test_status_is_string(self):
        assert isinstance(JobStatus.QUEUED, str)
        assert JobStatus.QUEUED == "queued"

    def test_status_used_in_json(self):
        job = Job(id="j1", url="https://example.com", status=JobStatus.DOWNLOADING)
        data = job.model_dump()
        assert data["status"] == "downloading"


# ---------------------------------------------------------------------------
# Job model tests
# ---------------------------------------------------------------------------


class TestJob:
    """Validate Job model fields, defaults, and serialization."""

    def test_minimal_job(self):
        job = Job(id="job-1", url="https://youtube.com/watch?v=abc")
        assert job.id == "job-1"
        assert job.url == "https://youtube.com/watch?v=abc"
        assert job.status == JobStatus.QUEUED
        assert job.title is None
        assert job.thumbnail_url is None
        assert job.duration is None
        assert job.progress == 0.0
        assert job.error is None
        assert job.artist is None
        assert job.album is None

    def test_full_job(self):
        job = Job(
            id="job-2",
            url="https://soundcloud.com/artist/track",
            status=JobStatus.DONE,
            title="Never Gonna Give You Up",
            thumbnail_url="https://i.ytimg.com/vi/abc/hqdefault.jpg",
            duration=213.0,
            progress=100.0,
            error=None,
            artist="Rick Astley",
            album="Whenever You Need Somebody",
        )
        assert job.status == JobStatus.DONE
        assert job.title == "Never Gonna Give You Up"
        assert job.progress == 100.0
        assert job.artist == "Rick Astley"

    def test_invalid_status_raises_validation_error(self):
        with pytest.raises(ValidationError) as exc_info:
            Job(id="j1", url="https://example.com", status="invalid_status")  # type: ignore[arg-type]
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("status",) for e in errors)

    def test_progress_bounds_lower(self):
        with pytest.raises(ValidationError):
            Job(id="j1", url="https://example.com", progress=-1.0)

    def test_progress_bounds_upper(self):
        with pytest.raises(ValidationError):
            Job(id="j1", url="https://example.com", progress=101.0)

    def test_job_serialization(self):
        job = Job(
            id="j1",
            url="https://youtube.com/watch?v=abc",
            status=JobStatus.QUEUED,
            title="Test Track",
            thumbnail_url="https://example.com/thumb.jpg",
            duration=120.5,
            progress=0.0,
            artist="Test Artist",
            album="Test Album",
        )
        data = job.model_dump()
        assert data["id"] == "j1"
        assert data["status"] == "queued"
        assert data["title"] == "Test Track"
        assert data["duration"] == 120.5
        assert data["progress"] == 0.0
        assert data["error"] is None

    def test_job_json_roundtrip(self):
        job = Job(id="j1", url="https://example.com", status=JobStatus.ERROR, error="Timed out")
        json_str = job.model_dump_json()
        restored = Job.model_validate_json(json_str)
        assert restored == job
        assert restored.error == "Timed out"

    def test_missing_required_fields_raises_validation_error(self):
        with pytest.raises(ValidationError):
            Job()  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            Job(id="j1")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# SSEEvent tests
# ---------------------------------------------------------------------------


class TestSSEEvent:
    """Validate SSE event payload structure."""

    def test_status_change_event(self):
        event = SSEEvent(
            event="status_change",
            job_id="job-1",
            data={"status": "downloading"},
        )
        assert event.event == "status_change"
        assert event.job_id == "job-1"
        assert event.data == {"status": "downloading"}

    def test_progress_event(self):
        event = SSEEvent(
            event="progress",
            job_id="job-1",
            data={"progress": 45.2},
        )
        assert event.event == "progress"
        assert event.data["progress"] == 45.2

    def test_error_event(self):
        event = SSEEvent(
            event="error",
            job_id="job-1",
            data={"error": "Video unavailable"},
        )
        assert event.data["error"] == "Video unavailable"

    def test_metadata_event(self):
        event = SSEEvent(
            event="metadata",
            job_id="job-1",
            data={
                "title": "Track Title",
                "thumbnail_url": "https://cdn.example.com/thumb.jpg",
                "duration": 180.0,
            },
        )
        assert event.data["title"] == "Track Title"
        assert event.data["duration"] == 180.0

    def test_default_empty_data(self):
        event = SSEEvent(event="status_change", job_id="job-1")
        assert event.data == {}

    def test_missing_required_fields_raises_validation_error(self):
        with pytest.raises(ValidationError):
            SSEEvent()  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            SSEEvent(event="test")  # type: ignore[call-arg]

    def test_serialization(self):
        event = SSEEvent(event="progress", job_id="j1", data={"progress": 50.0})
        data = event.model_dump()
        assert data == {"event": "progress", "job_id": "j1", "data": {"progress": 50.0}}


# ---------------------------------------------------------------------------
# HealthResponse tests
# ---------------------------------------------------------------------------


class TestHealthResponse:
    """Validate health endpoint response schema."""

    def test_valid_health_response(self):
        resp = HealthResponse(status="ok", service="yt-dlp-web-ui-backend")
        assert resp.status == "ok"
        assert resp.service == "yt-dlp-web-ui-backend"

    def test_serialization(self):
        resp = HealthResponse(status="ok", service="yt-dlp-web-ui-backend")
        data = resp.model_dump()
        assert data == {"status": "ok", "service": "yt-dlp-web-ui-backend"}
