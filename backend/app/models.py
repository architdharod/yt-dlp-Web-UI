"""Pydantic models for yt-dlp Web UI backend.

Defines request/response schemas, job state model, and SSE event payloads.
"""

import ipaddress
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

# Hosts (and their subdomains) that may be submitted for download.  The
# backend also restricts yt-dlp's extractors, but rejecting at the API edge
# gives the user an immediate, readable error and keeps the container from
# fetching arbitrary URLs on the internal network.
ALLOWED_URL_HOSTS: tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "soundcloud.com",
)


def validate_download_url(url: str) -> str:
    """Return *url* if it is an http(s) URL on an allowed host, else raise ValueError."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("URL has no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP-address URLs are not allowed")
    if not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_URL_HOSTS):
        raise ValueError(
            "Unsupported site; allowed hosts are " + ", ".join(ALLOWED_URL_HOSTS)
        )
    return url.strip()


class JobStatus(str, Enum):
    """Possible states in the job lifecycle.

    A download runs ``queued -> downloading -> converting -> tagging -> done``;
    a tagging job runs ``queued -> tagging -> done``.  ``error`` and
    ``cancelled`` are terminal and reachable from every non-terminal state.

    ``CANCELLED`` is live: ``POST /queue/{id}/cancel`` (phase 2) ends a queued
    or running job there.  ``TAGGING`` is still only vocabulary -- the tagging
    worker arrives in phase 8 -- but it has been a legal value of the persisted
    ``status`` column since the first schema version, so a database written by
    a later build is readable by this one and the restore logic does not have
    to grow a special case then.
    """

    QUEUED = "queued"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    TAGGING = "tagging"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class JobKind(str, Enum):
    """What a queue entry actually does.

    ``DOWNLOAD`` is the only kind produced today.  ``BULK`` (a parent that
    aggregates child downloads) and ``TAGGING`` (a metadata fix run on the
    single tagging worker) arrive in later phases; the column exists now so no
    migration is needed then.
    """

    DOWNLOAD = "download"
    BULK = "bulk"
    TAGGING = "tagging"


class DownloadRequest(BaseModel):
    """Schema for POST /download request body."""

    url: str = Field(..., min_length=1, description="YouTube or SoundCloud URL to download")
    artist: str | None = Field(None, max_length=200, description="Optional artist name for file organization")
    album: str | None = Field(None, max_length=200, description="Optional album name for file organization")

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return validate_download_url(value)


class Job(BaseModel):
    """Represents a queue entry with its current state and metadata.

    Every field except ``progress`` is persisted by
    :class:`~app.job_store.JobStore`; progress is memory-only because an
    interrupted job re-runs from zero after a restart.
    """

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
    kind: JobKind = Field(default=JobKind.DOWNLOAD, description="What this queue entry does")
    parent_id: str | None = Field(None, description="Id of the bulk parent this job belongs to")
    path: str | None = Field(None, description="Library path a tagging job targets (track or album)")
    result_path: str | None = Field(None, description="Library path written once the download finished")
    attempts: int = Field(
        default=0,
        ge=0,
        description=(
            "How often this job had to be started again: restart recoveries "
            "plus manual retries. A first run that succeeds leaves it at 0."
        ),
    )
    restart_attempts: int = Field(
        default=0,
        ge=0,
        description="How often a restart interrupted this job; reset by a manual retry",
    )
    cancel_requested: bool = Field(
        default=False,
        description=(
            "Whether the user asked for this job to stop.  Persisted so a "
            "restart during a cancel finishes the job as cancelled instead of "
            "re-queuing it; never part of an SSE payload."
        ),
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Job creation timestamp")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Timestamp of the last state change")
    finished_at: datetime | None = Field(None, description="Timestamp the job reached done/error/cancelled")


class SSEEvent(BaseModel):
    """Payload for a Server-Sent Event pushed to clients.

    For a job event, ``data`` always carries a snapshot of the job's
    user-visible fields (status, progress, title, thumbnail_url, duration,
    error) so a client can apply any event without knowing the job beforehand.

    ``library_changed`` announces that files under ``DOWNLOAD_PATH`` changed and
    carries ``data["paths"]``, the POSIX paths of what changed relative to that
    root.  It has no ``job_id`` when nothing queued caused the change -- a move,
    a delete, or a manual tag write -- which is why the field is optional.
    """

    event: str = Field(
        ...,
        description="Event type: status_change, progress, error, library_changed",
    )
    job_id: str | None = Field(
        None,
        description="ID of the job this event relates to, or null when no job caused it",
    )
    data: dict[str, Any] = Field(default_factory=dict, description="Event-specific payload data")


class HealthResponse(BaseModel):
    """Schema for GET /health response."""

    status: str = Field(..., description="Service status")
    service: str = Field(..., description="Service name")


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------
# Every ``path`` below is a POSIX path relative to ``DOWNLOAD_PATH`` -- the
# identity a Track, Album or Artist travels as everywhere in this app (domain
# model).  Absolute paths never leave the backend.


class LibraryTrack(BaseModel):
    """One audio file, as the scanner read it."""

    path: str = Field(..., description="POSIX path relative to DOWNLOAD_PATH")
    name: str = Field(..., description="Filename, including its extension")
    title: str = Field(..., description="TITLE tag, or the filename stem when untagged")
    artist: str | None = Field(None, description="ARTIST tag")
    album: str | None = Field(None, description="ALBUM tag")
    album_artist: str | None = Field(None, description="ALBUMARTIST tag")
    track_number: int | None = Field(None, description="TRACKNUMBER, '3/12' forms parsed to 3")
    disc_number: int | None = Field(None, description="DISCNUMBER, parsed the same way")
    duration: float | None = Field(None, description="Length in seconds")
    format: str = Field(..., description="Lower-case extension without the dot, e.g. 'flac'")
    bitrate: int | None = Field(None, description="Bitrate in bits per second")
    sample_rate: int | None = Field(None, description="Sample rate in Hz")
    size: int = Field(..., description="File size in bytes")
    mtime: str = Field(..., description="Last modification time, ISO 8601 UTC")
    has_embedded_art: bool = Field(..., description="Whether the file carries a picture")
    tags: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Raw tag dump; never includes binary picture data",
    )
    error: str | None = Field(
        None, description="Short reason the file could not be read, when it could not"
    )


class LibraryAlbum(BaseModel):
    """A folder at depth 2, with every audio file at or below it."""

    name: str = Field(..., description="Folder name")
    path: str = Field(..., description="POSIX path relative to DOWNLOAD_PATH")
    track_count: int = Field(..., description="Tracks in this album, nested folders included")
    cover_version: int = Field(
        ...,
        description=(
            "Opaque change stamp over the album folder, its sidecar images and "
            "its audio files -- not a timestamp.  The frontend appends it to "
            "the cover URL as ?v=, so anything that could change the art busts "
            "the browser cache."
        ),
    )
    has_cover: bool = Field(
        ...,
        description="Any track has embedded art, or a cover/folder image sits in the folder",
    )
    tracks: list[LibraryTrack] = Field(default_factory=list)


class LibraryArtist(BaseModel):
    """A folder at depth 1, or the synthetic bucket for root-level files."""

    name: str = Field(..., description="Folder name, or 'Unknown Artist' when synthetic")
    path: str = Field(..., description="POSIX path relative to DOWNLOAD_PATH; empty when synthetic")
    synthetic: bool = Field(
        ...,
        description=(
            "True for the 'Unknown Artist' bucket holding root-level files, "
            "which exists only in this response and never on disk"
        ),
    )
    album_count: int = Field(..., description="Number of albums")
    track_count: int = Field(..., description="Album tracks plus singles")
    albums: list[LibraryAlbum] = Field(default_factory=list)
    singles: list[LibraryTrack] = Field(
        default_factory=list, description="Loose tracks directly under the artist"
    )
    cover_album_path: str | None = Field(
        None,
        description=(
            "Path of the album whose art the artist tile shows: the first album "
            "with a cover that also has tracks, else the first with a cover"
        ),
    )


class LibraryResponse(BaseModel):
    """Schema for GET /library."""

    artists: list[LibraryArtist] = Field(default_factory=list)
    artist_count: int = Field(..., description="Number of artists, synthetic bucket included")
    album_count: int = Field(..., description="Number of albums across all artists")
    track_count: int = Field(..., description="Number of tracks across all artists")
    scanned_at: str = Field(..., description="When this scan ran, ISO 8601 UTC")
