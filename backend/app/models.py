"""Pydantic models for yt-dlp Web UI backend.

Defines request/response schemas, job state model, and SSE event payloads.
"""

import ipaddress
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from pydantic_core import PydanticCustomError

from app.spotify import (
    SPOTIFY_HOST,
    UNSUPPORTED_KIND_MESSAGE,
    is_spotify_url,
    spotify_url_target,
)

# Hosts (and their subdomains) that may be submitted for download.  The
# backend also restricts yt-dlp's extractors, but rejecting at the API edge
# gives the user an immediate, readable error and keeps the container from
# fetching arbitrary URLs on the internal network.
ALLOWED_URL_HOSTS: tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "soundcloud.com",
    # Every Bandcamp artist lives on a subdomain (``zoekeating.bandcamp.com``),
    # which the ``endswith("." + allowed)`` test below covers; the bare domain
    # is listed so a label page pasted without a subdomain is accepted too.
    "bandcamp.com",
    # Spotify is not a download source -- nothing is ever fetched from it but
    # an artist's *name*, which the probe then searches YouTube Music for
    # (:mod:`app.spotify`).  The bare ``spotify.com`` is deliberately not
    # listed: the artist pages and the oEmbed endpoint are both on this one
    # host, and no other Spotify subdomain has any business being fetched.
    SPOTIFY_HOST,
)

# What ``POST /download`` says to a Spotify artist URL.  That route is one
# track, and a Spotify artist page is a whole discography that only exists here
# as a lookup; the preview is where it turns into something selectable.
SPOTIFY_ARTIST_MESSAGE = (
    "A Spotify artist URL is a whole discography, not a single track; it opens "
    "a checklist to pick from. Paste a YouTube / YouTube Music / SoundCloud / "
    "Bandcamp link for one track."
)


def reject_spotify_url(url: str) -> str:
    """Return *url* unless it is a Spotify one, which no download can be.

    Used by every field whose URL is handed to yt-dlp, which has no Spotify
    extractor at all: ``POST /download``'s single track and ``POST
    /download/bulk``'s children.  A Spotify URL in either would enqueue a job
    that could only fail.  ``POST /download/probe`` accepts one (that is the
    whole point of the phase), and so does the bulk request's own ``url`` --
    there it is the parent's *display* URL, never extracted, while the
    children carry the YouTube watch URLs the preview matched.
    """
    target = spotify_url_target(url)
    if target is not None and target.is_artist:
        raise PydanticCustomError("spotify_artist_url", SPOTIFY_ARTIST_MESSAGE)
    if is_spotify_url(url):
        raise PydanticCustomError("spotify_url", UNSUPPORTED_KIND_MESSAGE)
    return url


def validate_download_url(url: str) -> str:
    """Return *url* if it is an http(s) URL on an allowed host, else raise.

    The failures are raised as :class:`PydanticCustomError` rather than plain
    ``ValueError``.  Both end in the same 422 -- ``PydanticCustomError`` is a
    ``ValueError`` -- but a plain one makes Pydantic prefix the message it
    shows with ``"Value error, "``, and this message is read by the person who
    pasted the URL.  Each raise site gets its own ``type`` slug so a client can
    tell the cases apart without matching on prose.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise PydanticCustomError(
            "url_scheme", "URL must start with http:// or https://"
        )
    host = (parts.hostname or "").lower()
    if not host:
        raise PydanticCustomError("url_no_host", "URL has no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise PydanticCustomError(
            "url_ip_address", "IP-address URLs are not allowed"
        )
    if not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_URL_HOSTS):
        raise PydanticCustomError(
            "unsupported_site",
            "Unsupported site; allowed hosts are {hosts}",
            {"hosts": ", ".join(ALLOWED_URL_HOSTS)},
        )
    return url.strip()


class JobStatus(str, Enum):
    """Possible states in the job lifecycle.

    A download runs ``queued -> downloading -> converting -> tagging -> done``;
    a tagging job runs ``queued -> tagging -> done``.  ``error`` and
    ``cancelled`` are terminal and reachable from every non-terminal state.

    ``CANCELLED`` is live: ``POST /queue/{id}/cancel`` (phase 2) ends a queued
    or running job there.  ``TAGGING`` is live too (phase 8): a download enters
    it once its FLAC is in the library, having *released its download slot*,
    and waits there for the single tagging worker.  A *download* in ``tagging``
    therefore always has a finished file behind it, which is why a cancel from
    there ends in ``done`` (with a "tags not fixed" detail) rather than in
    ``cancelled`` -- the track exists and the user should be told so.

    A manual tagging job (``JobKind.TAGGING``, phase 9) shares the status and
    not that ending: it downloaded nothing, so a cancel really did stop the
    only thing it was doing and it ends in ``cancelled``, and a lookup that
    could not happen ends it in ``error`` rather than in ``done`` with a note.
    """

    QUEUED = "queued"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    TAGGING = "tagging"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


# Longest a folder name may be in a move request.  Not a filesystem limit (255
# bytes per component is the common one) but a sanity bound: anything longer is
# a paste accident, and the error is clearer here than from the kernel.
MAX_FOLDER_NAME = 200


class JobKind(str, Enum):
    """What a queue entry actually does.

    ``DOWNLOAD`` is a URL fetched into the library; ``TAGGING`` (phase 9) is a
    metadata fix the user asked for on a track or an album that is already
    there, run on the single tagging worker with no download slot and no URL.
    ``BULK`` -- a parent that aggregates child downloads -- arrives in a later
    phase; the column has held all three from the first migration so none of
    them needs one of its own.
    """

    DOWNLOAD = "download"
    BULK = "bulk"
    TAGGING = "tagging"


class DownloadRequest(BaseModel):
    """Schema for POST /download request body."""

    url: str = Field(
        ...,
        min_length=1,
        description="YouTube, SoundCloud or Bandcamp URL to download",
    )
    artist: str | None = Field(
        None,
        max_length=MAX_FOLDER_NAME,
        description="Optional artist name for file organization",
    )
    album: str | None = Field(
        None,
        max_length=MAX_FOLDER_NAME,
        description="Optional album name for file organization",
    )

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        # The host allowlist first, then the one host on it that this route
        # cannot serve: a Spotify URL is a preview, never a download.
        return reject_spotify_url(validate_download_url(value))


class Job(BaseModel):
    """Represents a queue entry with its current state and metadata.

    Every field except ``progress``, ``progress_done`` and ``progress_total``
    is persisted by :class:`~app.job_store.JobStore`; the three progress fields
    are memory-only because an interrupted job re-runs from zero after a
    restart.
    """

    id: str = Field(..., description="Unique job identifier")
    url: str = Field(..., description="Source URL")
    status: JobStatus = Field(default=JobStatus.QUEUED, description="Current job status")
    title: str | None = Field(None, description="Track title from metadata extraction")
    thumbnail_url: str | None = Field(None, description="Thumbnail URL from source CDN")
    duration: float | None = Field(None, description="Track duration in seconds")
    progress: float = Field(default=0.0, ge=0.0, le=100.0, description="Download progress percentage")
    progress_done: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Units of this job's work already finished -- the N of a tagging "
            "job's 'N of M'.  Memory-only, like ``progress``: a restarted job "
            "re-runs its whole pass, so a stored count would be a lie."
        ),
    )
    progress_total: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Units of work this job has in total -- the M of 'N of M', which "
            "for an album pass is every track in the folder, non-FLAC ones "
            "included.  Memory-only; see ``progress_done``."
        ),
    )
    error: str | None = Field(None, description="Error message if job failed")
    detail: str | None = Field(
        None,
        description=(
            "A note about a job that finished anyway, e.g. 'tags not fixed: no "
            "match'.  Separate from ``error`` because it is not a failure: the "
            "job is ``done`` and its file is in the library."
        ),
    )
    artist: str | None = Field(None, description="Artist name (user-provided or from metadata)")
    album: str | None = Field(None, description="Album name (user-provided or from metadata)")
    album_final: bool = Field(
        default=False,
        description=(
            "Whether ``album`` is the whole answer, because the enumeration "
            "that created this child read the release it is on.  A null album "
            "is then deliberately none -- a loose Single -- and yt-dlp's own "
            "album is not allowed to refile it.  Only ever true on a bulk "
            "child enumerated through YouTube Music"
        ),
    )
    kind: JobKind = Field(default=JobKind.DOWNLOAD, description="What this queue entry does")
    parent_id: str | None = Field(None, description="Id of the bulk parent this job belongs to")
    path: str | None = Field(None, description="Library path a tagging job targets (track or album)")
    target_dir: str | None = Field(
        None,
        description=(
            "The library folder this download will file into, POSIX and "
            "relative to DOWNLOAD_PATH; None until it has been resolved"
        ),
    )
    target_guessed: bool = Field(
        default=False,
        description=(
            "Whether ``target_dir`` is only a guess: the metadata probe never "
            "returned and the user named no artist, so the folder is the "
            "'Unknown Artist' fallback rather than anywhere the download will "
            "really land"
        ),
    )
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
    children: list["Job"] = Field(
        default_factory=list,
        description=(
            "The child downloads of a bulk parent, oldest first.  Response "
            "shape only: never persisted (the children are rows of their own, "
            "found by ``parent_id``) and never part of an SSE payload, which "
            "carries one job at a time.  Always empty on a child."
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

    ``notices`` carries ``data["notices"]``: the complete list of open service
    notices, the same shape ``GET /notices`` returns, sent every time that set
    changes.  It is the whole list rather than the one that changed because a
    notice going away has nothing of its own to send; a client replaces its
    state with the payload.  It never has a ``job_id``.
    """

    event: str = Field(
        ...,
        description="Event type: status_change, progress, metadata, error, library_changed, notices",
    )
    job_id: str | None = Field(
        None,
        description="ID of the job this event relates to, or null when no job caused it",
    )
    data: dict[str, Any] = Field(default_factory=dict, description="Event-specific payload data")



class Notice(BaseModel):
    """A standing complaint about one of the external services.

    Notices exist because a rescan failure has nowhere else to go: no job
    failed, no request was refused, and the user is the only one who can fix a
    wrong password or a non-admin Navidrome account.  The open set is broadcast
    as the ``notices`` SSE event and served by ``GET /notices`` so a client that
    connects after startup still sees the ones already open.

    ``id`` changes whenever a notice is raised afresh, which is what lets the
    frontend keep a dismissed notice hidden until the same problem happens
    again after a success in between.
    """

    id: str = Field(..., description="Unique id of this raising of the notice")
    level: Literal["error", "warning"] = Field(..., description="How loudly to show it")
    source: Literal["navidrome", "lidarr"] = Field(
        ..., description="The service the notice is about"
    )
    message: str = Field(..., description="Human-readable text; never contains a secret")
    created_at: str = Field(..., description="When it was raised, ISO 8601 UTC")


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


# Bounds on the path list a move may carry.  A move is a UI gesture over one
# folder, so a thousand tracks is already far past anything a user selects, and
# 4096 is the usual PATH_MAX: past either the request is a paste accident or an
# attempt to make the backend chew on megabytes of strings.
MAX_PATH_LENGTH = 4096
MAX_MOVE_PATHS = 1000


class LibraryMoveRequest(BaseModel):
    """Schema for POST /library/move.

    One body covers the three moves the UX ticket defines, told apart by what
    the caller sends and what is on disk:

    * ``paths`` -- tracks sharing one folder, moved to ``artist`` and the
      optional ``album``.  A blank ``album`` makes them loose Singles.
    * ``path`` naming a folder at depth 2 -- an album, moved to ``artist``,
      optionally renamed to ``album``, merging into a folder already there.
    * ``path`` naming a folder at depth 1 -- an artist, renamed to ``artist``.

    Library paths always travel in the body, never in a URL segment (domain
    model), so a folder called ``AC/DC`` needs no encoding scheme of its own.
    ``artist`` and ``album`` are *names*, not paths; the mover validates them
    as single components and sanitises new ones.
    """

    path: str | None = Field(
        None,
        max_length=MAX_PATH_LENGTH,
        description="The track, album, or artist to move, relative to DOWNLOAD_PATH",
    )
    paths: list[Annotated[str, StringConstraints(max_length=MAX_PATH_LENGTH)]] | None = (
        Field(
            None,
            max_length=MAX_MOVE_PATHS,
            description="Track paths to move, all from one folder; alternative to 'path'",
        )
    )
    artist: str = Field(
        ...,
        min_length=1,
        max_length=MAX_FOLDER_NAME,
        description="Destination artist folder name (created if new)",
    )
    album: str | None = Field(
        None,
        max_length=MAX_FOLDER_NAME,
        description="Destination album folder name; blank or omitted means a loose Single",
    )

    @field_validator("paths")
    @classmethod
    def _check_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and not value:
            raise ValueError("paths must name at least one track")
        return value


class MovedPath(BaseModel):
    """One file's old and new identity.

    ``from`` is a Python keyword, so the field is ``source`` with an alias; the
    route serialises by alias, which is what the API contract says.
    """

    model_config = ConfigDict(populate_by_name=True)

    source: str = Field(..., alias="from")
    target: str = Field(..., alias="to")


class LibraryMoveResponse(BaseModel):
    """Schema for POST /library/move.

    ``removed`` are the folders the move emptied and cleaned up, so a client
    can tell "the album is gone" from "the album is still there, minus a
    track" without re-reading the tree.

    ``destination`` is where the album or artist now lives, so the browser can
    follow the thing it just moved instead of dropping the user back at a
    folder that is no longer there.  ``None`` for a track move, whose tracks
    went into a folder rather than being one, and for a no-op.
    """

    moved: list[MovedPath] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    destination: str | None = Field(
        default=None,
        description="Where the moved album or artist now lives, relative POSIX",
    )


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------


class LibraryTagRequest(BaseModel):
    """Schema for POST /library/tag.

    ``path`` names an album folder (depth 2) or a single track -- one inside an
    album, or a loose Single directly under an artist.  An artist folder is
    refused: the metadata ticket defines a per-track and a per-album trigger
    and deliberately no per-artist one, so a whole-artist run would be a
    button nobody asked for and a queue nobody can follow.

    The path travels in the body rather than in a URL segment, like every other
    library path in this API (domain model), so a folder called ``AC/DC`` needs
    no encoding scheme of its own.
    """

    path: str = Field(
        ...,
        min_length=1,
        max_length=MAX_PATH_LENGTH,
        description="The album folder or track to re-tag, relative to DOWNLOAD_PATH",
    )


# ---------------------------------------------------------------------------
# Trash
# ---------------------------------------------------------------------------

# A trash entry id is a folder name under ``.trash``: a UTC timestamp, plus a
# ``-2`` when two deletes landed in the same microsecond.  Bounded so a bad
# client cannot hand the route a megabyte to validate.
MAX_ENTRY_ID = 100


class LibraryDeleteRequest(BaseModel):
    """Schema for POST /library/delete.

    ``path`` names a track, an album folder or an artist folder; ``paths``
    names tracks that share one parent folder -- a multi-selection inside one
    album or one artist's Singles.  Exactly one of the two is given, and the
    route answers 400 when that is not so.
    """

    path: str | None = Field(
        None,
        max_length=MAX_PATH_LENGTH,
        description="The track, album, or artist to delete, relative to DOWNLOAD_PATH",
    )
    paths: list[Annotated[str, StringConstraints(max_length=MAX_PATH_LENGTH)]] | None = (
        Field(
            None,
            max_length=MAX_MOVE_PATHS,
            description="Track paths to delete, all from one folder; alternative to 'path'",
        )
    )

    @field_validator("paths")
    @classmethod
    def _check_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and not value:
            raise ValueError("paths must name at least one track")
        return value


class TrashEntry(BaseModel):
    """One ``.trash/<id>/`` folder as the Trash tab shows it.

    ``path`` is the one line the tab has room for -- the artist, the album, the
    track, or the folder a multi-track delete came out of -- while ``paths``
    is everything Restore will put back.
    """

    id: str = Field(..., description="The .trash/<id> folder name")
    path: str = Field(
        ...,
        description="The deleted item's original relative path, or the shared parent folder",
    )
    kind: Literal["artist", "album", "track", "tracks"]
    paths: list[str] = Field(
        default_factory=list,
        description="Every original relative path inside this entry",
    )
    deleted_at: str = Field(..., description="ISO-8601 UTC, e.g. 2026-09-04T18:22:31Z")
    track_count: int = Field(0, description="Audio files inside the entry")


class LibraryDeleteResponse(BaseModel):
    """Schema for POST /library/delete.

    ``removed`` are the library folders the delete emptied and cleaned up, so
    the browser can tell "the album is gone" from "the album lost a track".
    """

    entry: TrashEntry
    removed: list[str] = Field(default_factory=list)


class TrashListResponse(BaseModel):
    """Schema for GET /library/trash: entries newest first, plus the tab badge."""

    entries: list[TrashEntry] = Field(default_factory=list)
    track_count: int = 0


class TrashRestoreRequest(BaseModel):
    """Schema for POST /library/trash/restore.

    ``artist`` and ``album`` are optional and mean the same as they do on
    ``/library/move``: without them the entry goes back where it came from,
    with them it is restored under that artist instead -- which is how the UI
    answers a 409 without making the user restore and then move.
    """

    id: str = Field(..., min_length=1, max_length=MAX_ENTRY_ID)
    artist: str | None = Field(
        None,
        max_length=MAX_FOLDER_NAME,
        description="Restore under this artist instead of the original one",
    )
    album: str | None = Field(
        None,
        max_length=MAX_FOLDER_NAME,
        description="Album folder name; blank means a loose Single (tracks only)",
    )


class RestoredPath(BaseModel):
    """One item a restore put back: where it was in the trash, where it is now."""

    source: str = Field(..., description="Path relative to .trash")
    target: str = Field(..., description="Path relative to DOWNLOAD_PATH")


class TrashRestoreResponse(BaseModel):
    """Schema for POST /library/trash/restore."""

    restored: list[RestoredPath] = Field(default_factory=list)


class TrashEmptyResponse(BaseModel):
    """Schema for POST /library/trash/empty: what was permanently deleted."""

    removed: int = Field(0, description="Entries removed")
    track_count: int = Field(0, description="Audio files removed")


# ---------------------------------------------------------------------------
# Collection probe and bulk downloads
# ---------------------------------------------------------------------------
# The bulk flow is two requests: ``POST /download/probe`` says whether a URL is
# one track or a collection and, for a collection, returns the checklist the
# user picks from; ``POST /download/bulk`` sends the picked rows back as one
# parent job with a child per track.

# Above this many rows the preview warns and starts with nothing selected: a
# 600-track channel is almost never what somebody meant to download in one go.
LARGE_COLLECTION_TRACKS = 500

# Hard stop.  Enumerating more than this is minutes of yt-dlp calls for a
# selection nobody is going to make one checkbox at a time, and the queue would
# hold the result for hours; the user is asked for a narrower URL instead.
MAX_COLLECTION_TRACKS = 2000

# How many sub-collections one probe may expand.  A YouTube ``/releases`` tab
# is 50-odd albums, a SoundCloud ``/sets`` page about the same, and a YouTube
# Music discography is the same shape again -- one extra call per release; 200
# is far past any real discography and keeps the worst case at 200 calls rather
# than at "however many rows the page had".  Lives here rather than in
# ``app.probe`` because ``app.probe`` and ``app.ytmusic`` both bound themselves
# by it and the probe imports the other way round.
MAX_SUBCOLLECTIONS = 200

# How many probes may hold an executor thread at once.  A probe is a long
# blocking call on the default executor, and the executor is shared with the
# rest of the app; two at a time is enough for a user with a second tab open
# and leaves the pool for everything else.  A third probe waits for a slot
# rather than queueing invisibly behind a thread.  Here for the same reason as
# above: ``app.ytmusic`` sizes its HTTPS connection pool for this many probes.
MAX_CONCURRENT_PROBES = 2

# Bounds on the free text a preview row carries back in a bulk submit.  A title
# is a display string, so a kilobyte is already absurd; a source id is
# ``<extractor>:<id>``, which is short by construction.  The preview truncates
# to exactly these, so no row a probe produced can be rejected by the submit
# that follows it.
MAX_TRACK_TITLE = 1000
MAX_SOURCE_ID = 200

# Longest a preview row's ``reason`` may be.  Display-only -- yt-dlp's own
# words for why a row cannot be downloaded, or a library path -- so a few lines
# is all a checklist row can show, and the raw error can be a page long.
MAX_REASON = 300


class ProbeRequest(BaseModel):
    """Schema for POST /download/probe: the URL the user pasted.

    ``artist`` is optional and only affects the dedup pass: it is the artist
    folder the form is currently showing, so that a user who corrects the
    suggestion sees "in library" recomputed against the folder the tracks would
    actually land in.  Omitted (or blank), the source's own suggestion is used.
    """

    url: str = Field(
        ...,
        min_length=1,
        description="The URL to classify: a track, or a collection to preview",
    )
    artist: str | None = Field(
        None,
        max_length=MAX_FOLDER_NAME,
        description="Artist folder to check the preview's rows against",
    )

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return validate_download_url(value)

    @field_validator("artist")
    @classmethod
    def _check_artist(cls, value: str | None) -> str | None:
        # Unlike the bulk request's, a blank artist here is not an error: the
        # form sends whatever is in the box, and an empty box simply means
        # "use the suggestion".
        if value is None:
            return None
        return value.strip() or None


class ProbeTrackResponse(BaseModel):
    """The probe's answer for a URL that is a single track.

    The same fields ``POST /download`` would have extracted, so the form can
    show the track and queue it without probing twice.  Every one of them is
    optional: a probe that reached the page but could not read a title is still
    a usable answer, and the download resolves its own metadata anyway.
    """

    type: Literal["track"] = "track"
    title: str | None = Field(None, description="Track title from the flat extraction")
    duration: float | None = Field(None, description="Length in seconds")
    thumbnail_url: str | None = Field(None, description="Thumbnail URL from the source CDN")
    artist: str | None = Field(None, description="Artist yt-dlp reported, if any")
    album: str | None = Field(None, description="Album yt-dlp reported, if any")


class PreviewRow(BaseModel):
    """One track in a collection preview.

    ``status`` drives the checklist: ``available`` rows start ticked,
    ``in_library`` rows start unticked with the matching library path in
    ``reason``, and ``unavailable`` rows cannot be selected at all and carry
    yt-dlp's own words for why.
    """

    id: str = Field(
        ...,
        description=(
            "Stable key for this row: its source id, or its URL when the "
            "source gave no id.  What the frontend keys the checklist on and "
            "what the dedup answer is returned against."
        ),
    )
    url: str = Field(..., description="The URL a child job would download")
    source_id: str | None = Field(
        None, description="``<extractor>:<id>``, the same shape SOURCEID carries"
    )
    title: str | None = Field(None, description="Track title, when the source gave one")
    album: str | None = Field(
        None,
        description="Album from the source; null means the track becomes a loose Single",
    )
    album_final: bool = Field(
        default=False,
        description=(
            "Whether ``album`` is the whole answer because the source read the "
            "release this track is on: a null album is then deliberately none "
            "-- a loose Single -- rather than an album nobody knew.  False for "
            "the flat pass, whose listings routinely carry no album at all"
        ),
    )
    duration: float | None = Field(None, description="Length in seconds, when known")
    thumbnail_url: str | None = Field(None, description="Thumbnail URL, when known")
    status: Literal["available", "in_library", "unavailable"] = Field(
        ..., description="Whether this row can be, or is worth, downloading"
    )
    reason: str | None = Field(
        None,
        max_length=MAX_REASON,
        description=(
            "The matching library path for ``in_library``, yt-dlp's message "
            "for ``unavailable``, and null for ``available``"
        ),
    )


class CollectionPreview(BaseModel):
    """The checklist behind a collection URL."""

    url: str = Field(..., description="The collection URL that was probed")
    title: str | None = Field(
        None,
        max_length=MAX_TRACK_TITLE,
        description="Collection title from the source",
    )
    artist: str | None = Field(
        None,
        max_length=MAX_FOLDER_NAME,
        description=(
            "The artist the source suggests, which the user edits before "
            "submitting; it applies to every selected row"
        ),
    )
    source: Literal["youtube", "soundcloud", "bandcamp", "other"] = Field(
        ..., description="Which source this came from, for the per-source notices"
    )
    rows: list[PreviewRow] = Field(default_factory=list)
    total: int = Field(..., description="Number of rows")
    in_library: int = Field(..., description="Rows the dedup rule already found on disk")
    unavailable: int = Field(..., description="Rows that cannot be downloaded")
    large: bool = Field(
        ...,
        description=(
            f"More than {LARGE_COLLECTION_TRACKS} rows, so the UI warns and "
            "preselects nothing"
        ),
    )
    notices: list[str] = Field(
        default_factory=list,
        description="Source caveats worth showing above the list",
    )


class ProbeCollectionResponse(BaseModel):
    """The probe's answer for a URL that is a playlist, album, set or channel."""

    type: Literal["collection"] = "collection"
    preview: CollectionPreview


# Discriminated on ``type`` so a client can branch without guessing from the
# shape, and so the OpenAPI schema names both arms.
ProbeResponse = Annotated[
    ProbeTrackResponse | ProbeCollectionResponse, Field(discriminator="type")
]


class BulkTrack(BaseModel):
    """One selected row on its way back to the backend.

    Everything but the URL is what the preview showed and may be null: a child
    resolves its own metadata when it runs, so these are only what the queue
    can display in the meantime.
    """

    url: str = Field(..., min_length=1, description="The track URL to download")
    title: str | None = Field(None, max_length=MAX_TRACK_TITLE)
    album: str | None = Field(None, max_length=MAX_FOLDER_NAME)
    album_final: bool = Field(
        default=False,
        description=(
            "The preview row's ``album_final``, sent back as it came: the "
            "source read the release, so a null album means deliberately none"
        ),
    )
    duration: float | None = Field(None, ge=0)
    thumbnail_url: str | None = Field(None, max_length=MAX_PATH_LENGTH)
    source_id: str | None = Field(None, max_length=MAX_SOURCE_ID)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        # A child is handed to yt-dlp exactly as a single download is, so the
        # same rejection applies: only the parent's *display* URL, on the bulk
        # request itself, may be a Spotify one.
        return reject_spotify_url(validate_download_url(value))


class BulkDownloadRequest(BaseModel):
    """Schema for POST /download/bulk: the selection, plus where it goes.

    ``artist`` is the one placement decision the user makes, and it applies to
    every track; the album travels per row because that is how the source gives
    it (bulk flow ticket).  A blank album means a loose Single under the artist.
    """

    url: str = Field(..., min_length=1, description="The collection URL these rows came from")
    artist: str = Field(
        ...,
        min_length=1,
        max_length=MAX_FOLDER_NAME,
        description="Artist folder every selected track is filed under",
    )
    title: str | None = Field(
        None,
        max_length=MAX_TRACK_TITLE,
        description="Collection title, shown on the parent row",
    )
    tracks: list[BulkTrack] = Field(
        ...,
        min_length=1,
        max_length=MAX_COLLECTION_TRACKS,
        description="The rows the user selected, in the order they were shown",
    )

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return validate_download_url(value)

    @field_validator("artist")
    @classmethod
    def _check_artist(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("artist must not be blank")
        return stripped
