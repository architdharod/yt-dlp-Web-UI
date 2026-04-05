"""Pydantic models for yt-dlp Web UI backend.

Defines request/response schemas, job state model, and SSE event payloads.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    """Possible states in the job lifecycle."""

    QUEUED = "queued"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    DONE = "done"
    ERROR = "error"


class DownloadRequest(BaseModel):
    """Schema for POST /download request body."""

    url: str = Field(..., min_length=1, description="YouTube or SoundCloud URL to download")
    artist: str | None = Field(None, description="Optional artist name for file organization")
    album: str | None = Field(None, description="Optional album name for file organization")


class Job(BaseModel):
    """Represents a download job with its current state and metadata."""

    id: str = Field(..., description="Unique job identifier")
    url: str = Field(..., description="Source URL")
    status: JobStatus = Field(default=JobStatus.QUEUED, description="Current job status")
    title: str | None = Field(None, description="Track title from metadata extraction")
    thumbnail_url: str | None = Field(None, description="Thumbnail URL from source CDN")
    duration: float | None = Field(None, description="Track duration in seconds")
    progress: float = Field(default=0.0, ge=0.0, le=100.0, description="Download progress percentage")
    error: str | None = Field(None, description="Error message if job failed")
    artist: str | None = Field(None, description="Artist name (user-provided or from metadata)")
    album: str | None = Field(None, description="Album name (user-provided or from metadata)")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Job creation timestamp")


class SSEEvent(BaseModel):
    """Payload for a Server-Sent Event pushed to clients."""

    event: str = Field(..., description="Event type: status_change, progress, error, metadata")
    job_id: str = Field(..., description="ID of the job this event relates to")
    data: dict[str, Any] = Field(default_factory=dict, description="Event-specific payload data")


class HealthResponse(BaseModel):
    """Schema for GET /health response."""

    status: str = Field(..., description="Service status")
    service: str = Field(..., description="Service name")
