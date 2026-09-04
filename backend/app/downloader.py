"""Downloader module for yt-dlp Web UI.

Provides two operations using yt-dlp as a Python library:
  1. extract_metadata(url) -- fetches title, thumbnail URL, and duration
  2. download_audio(job, on_progress, ...) -- downloads audio as FLAC with
     embedded metadata and thumbnail, reporting progress via callback

No CLI subprocess calls -- yt-dlp is used entirely through its Python API.
"""

import logging
import os
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import yt_dlp

from app.file_organizer import (
    DEFAULT_DOWNLOAD_PATH,
    TEMP_DIRNAME,
    UnsafePathError,
    get_output_path,
)
from app.models import Job

logger = logging.getLogger(__name__)

# Seconds a single socket operation may block before yt-dlp gives up on it.
SOCKET_TIMEOUT_SECONDS = 15

# Every extractor except the generic one.  The generic extractor would fetch
# any URL it is handed, which turns the backend into an open proxy for the
# internal network.
ALLOWED_EXTRACTORS = ["default", "-generic"]

class _YtDlpLogger:
    """Funnel yt-dlp's own console output into ``logging``.

    ``quiet`` does not silence yt-dlp: the progress bar is controlled by
    ``noprogress``, and ``to_stderr`` (which carries ``ERROR:`` lines) ignores
    ``quiet`` entirely -- both would otherwise land on the container's stdout
    and stderr.  Note that supplying ``logger`` also overrides ``no_warnings``,
    so every warning arrives here too.

    ``debug`` and ``info`` are yt-dlp's running commentary (``[youtube] …``,
    ``[download] …``) and stay at debug level.

    ``warning`` and ``error`` are both mapped to ``logger.warning``.  ``error``
    is *not* silenced even though a fatal failure also reaches us as a
    ``DownloadError``: ``YoutubeDL.trouble(is_error=False)`` routes through
    ``to_stderr`` as well, so ``ERROR:`` lines that are only advisory -- a
    PO-token provider that could not be reached, cookies that are not scoped to
    the requested domain -- arrive here and nowhere else.  Logging them at
    warning keeps the one ERROR line per failed job (our own ``DownloadError``)
    unambiguous while leaving these diagnosable.  ``warning`` matters for the
    same reason: EmbedThumbnail's "skipping embedding the thumbnail" is
    reported only this way, and it explains a FLAC that arrives without cover
    art.
    """

    _log = logging.getLogger("yt_dlp")

    def debug(self, msg: str) -> None:
        self._log.debug(msg)

    def info(self, msg: str) -> None:
        self._log.debug(msg)

    def warning(self, msg: str) -> None:
        self._log.warning(msg)

    def error(self, msg: str) -> None:
        # Warning, not error: yt-dlp's fatal errors are re-raised as
        # DownloadError and logged there; these are the advisory ones.
        self._log.warning(msg)


# Options shared by every YoutubeDL instance this module creates.
_BASE_OPTS: dict = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "logger": _YtDlpLogger(),
    # A URL copied while a playlist/mix is open carries a `list=` parameter;
    # without this yt-dlp would download the whole list into one file.
    "noplaylist": True,
    "socket_timeout": SOCKET_TIMEOUT_SECONDS,
    "allowed_extractors": ALLOWED_EXTRACTORS,
}

# YouTube auto-generated artist channels are named "<Artist> - Topic".
_TOPIC_SUFFIX = " - Topic"


@dataclass
class TrackMetadata:
    """Metadata extracted from a URL before downloading."""

    title: str
    thumbnail_url: str | None
    duration: float | None


class DownloadError(Exception):
    """Raised when a download or metadata extraction fails."""


def _get_download_path() -> str:
    """Read DOWNLOAD_PATH from environment, falling back to the default.

    An empty value (which docker compose produces for an unset variable)
    counts as unset.
    """
    return os.environ.get("DOWNLOAD_PATH") or DEFAULT_DOWNLOAD_PATH


def _reject_collections(info: dict, url: str) -> None:
    """Raise DownloadError if *info* describes a playlist rather than a track."""
    if info.get("_type") == "playlist":
        logger.warning("Rejected playlist URL %s (%r)", url, info.get("title"))
        raise DownloadError(
            "This URL points to a playlist or channel; only single tracks are supported"
        )


def _pick_artist(info: dict) -> str | None:
    """Choose the best artist name yt-dlp offers for a track."""
    artist = (
        info.get("artist")
        or info.get("creator")
        or info.get("channel")
        or info.get("uploader")
    )
    if isinstance(artist, str) and artist.endswith(_TOPIC_SUFFIX):
        artist = artist[: -len(_TOPIC_SUFFIX)]
    return artist


def extract_metadata(url: str) -> TrackMetadata:
    """Extract track metadata from a URL without downloading.

    Uses yt-dlp to fetch the page and extract title, thumbnail URL,
    and duration.  No audio data is downloaded.

    Args:
        url: YouTube or SoundCloud URL.

    Returns:
        TrackMetadata with title, thumbnail_url, and duration.

    Raises:
        DownloadError: If yt-dlp cannot extract metadata, or the URL is a
            playlist.
    """
    logger.info("Extracting metadata for URL: %s", url)

    opts = {
        **_BASE_OPTS,
        "skip_download": True,
        # This is only a quick probe; the download phase retries properly.
        "retries": 1,
        "extractor_retries": 1,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.YoutubeDLError as exc:
        logger.error("Metadata extraction failed for %s: %s", url, exc)
        raise DownloadError(f"Failed to extract metadata: {exc}") from exc

    if info is None:
        logger.error("yt-dlp returned no metadata for %s", url)
        raise DownloadError("yt-dlp returned no metadata")

    _reject_collections(info, url)

    metadata = TrackMetadata(
        title=info.get("title") or "Unknown Title",
        thumbnail_url=info.get("thumbnail"),
        duration=info.get("duration"),
    )
    logger.info(
        "Metadata extracted: title=%r, duration=%s, has_thumbnail=%s",
        metadata.title,
        metadata.duration,
        metadata.thumbnail_url is not None,
    )
    return metadata


def track_filename_for(title: str) -> str:
    """Return the FLAC filename a download with *title* writes.

    Single source of truth for the naming convention.
    """
    return yt_dlp.utils.sanitize_filename(title) + ".flac"


def job_temp_dir(job_id: str, download_path: str | None = None) -> Path:
    """Return the scratch directory yt-dlp uses while *job_id* is downloading.

    Everything yt-dlp writes -- ``.part``, ``.ytdl``, ``.part-Frag*``, the
    thumbnail sidecar, the raw audio and the finished FLAC before it is moved --
    lands here instead of in the library, so the library only ever contains
    finished files and cleanup never has to guess which files are ours.

    The directory lives *inside* ``DOWNLOAD_PATH`` so the final move is a rename
    on the same filesystem, and is hidden behind a leading dot so Navidrome and
    Lidarr ignore it (the same reason Phase 3 puts ``.trash`` here).  Phase 4's
    library scan must skip ``.tmp`` as well as ``.trash``.

    *job_id* becomes a path segment and the result is handed to ``rmtree``, so
    it is validated here rather than trusted: ``".."`` alone would make the
    scratch directory the library root.

    Raises:
        ValueError: If *job_id* is not usable as a single path segment.
    """
    if job_id in ("", ".", "..") or any(char in job_id for char in ("/", "\\", "\0")):
        raise ValueError(f"Job id {job_id!r} is not a valid path segment")
    return Path(download_path or _get_download_path()) / TEMP_DIRNAME / job_id


def remove_job_temp_dir(job_id: str, download_path: str | None = None) -> bool:
    """Delete the scratch directory of *job_id*, if it still exists.

    Derived from the job id alone: no title, artist or album is needed, so the
    only way this could reach a library file is a job id that is not a single
    path segment -- which :func:`job_temp_dir` rejects.  Returns ``True`` if a
    directory was actually removed.

    ``.tmp`` itself is removed once the last job leaves it, so the library root
    stays clean between downloads rather than only after the next boot's
    :func:`remove_orphan_temp_dirs`.  ``rmdir`` is a no-op while another job's
    directory is still in there.  It can still race a concurrent job that is
    about to create its own scratch directory, but harmlessly: ``download_audio``
    creates it with ``mkdir(parents=True, exist_ok=True)``, which puts ``.tmp``
    back if it vanished in between.
    """
    temp_dir = job_temp_dir(job_id, download_path)
    if not temp_dir.is_dir():
        return False
    shutil.rmtree(temp_dir, ignore_errors=True)
    if temp_dir.exists():
        logger.warning("Could not fully remove temp directory %s", temp_dir)
        return False
    logger.info("Removed temp directory %s", temp_dir)
    try:
        temp_dir.parent.rmdir()  # only succeeds while it is empty
    except OSError:
        pass
    return True


def remove_orphan_temp_dirs(
    keep_ids: Iterable[str], download_path: str | None = None
) -> list[str]:
    """Delete scratch directories that belong to no known job, at boot.

    A crash can leave ``DOWNLOAD_PATH/.tmp/<id>`` behind for a job the database
    no longer knows about.  Nothing is running while this is called, so every
    directory whose name is not in *keep_ids* is stale.  ``.tmp`` itself is
    removed once it is empty so the library root stays clean.

    Only directories named like a job id (a UUID) are ever removed.  ``.tmp`` is
    a legal artist name, so if the reserved-name check in the file organizer
    ever regressed this sweep would be deleting somebody's albums; anything else
    found here is left alone and logged.

    Returns the job ids whose directories were removed.
    """
    root = Path(download_path or _get_download_path()) / TEMP_DIRNAME
    keep = set(keep_ids)
    removed: list[str] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return removed

    for entry in entries:
        if entry.name in keep or not entry.is_dir():
            continue
        try:
            uuid.UUID(entry.name)
        except ValueError:
            logger.warning(
                "Skipping %s: not a job scratch directory (name is not a UUID)", entry
            )
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed.append(entry.name)

    if removed:
        logger.info("Removed %d orphaned temp directory/ies", len(removed))
    try:
        root.rmdir()  # only succeeds while it is empty
    except OSError:
        pass
    return removed


def _make_progress_hook(
    on_progress: Callable[[float], None] | None,
    cancel_event: threading.Event | None = None,
) -> Callable[[dict], None]:
    """Create a yt-dlp progress hook that translates raw progress dicts
    into a simple percentage and forwards it to *on_progress*.

    If *cancel_event* is set, the hook aborts the download by raising
    ``DownloadCancelled``, which yt-dlp propagates out of ``download()``.
    """

    def hook(d: dict) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("Download cancelled")

        if on_progress is None:
            return

        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)

            if total and total > 0:
                percentage = min((downloaded / total) * 100, 100.0)
                on_progress(percentage)

        elif d.get("status") == "finished":
            on_progress(100.0)

    return hook


def _make_postprocessor_hook(
    on_phase: Callable[[str], None] | None,
) -> Callable[[dict], None]:
    """Create a yt-dlp postprocessor hook that reports when the FLAC
    conversion actually starts.

    yt-dlp registers every entry of ``postprocessor_hooks`` twice per
    postprocessor -- once from ``PostProcessor.__init__`` via ``set_downloader``
    and again from ``YoutubeDL.add_post_processor`` -- so this fires twice for
    one conversion.  Deduplication lives in ``QueueManager._update_status``,
    which ignores a transition to the status a job is already in.
    """

    def hook(d: dict) -> None:
        if on_phase is None:
            return
        if d.get("status") == "started" and d.get("postprocessor") == "ExtractAudio":
            on_phase("converting")

    return hook


def _produced_filepath(info: dict | None) -> str | None:
    """Return the file yt-dlp actually wrote, after its postprocessors ran.

    ``extract_info(..., download=True)`` records one entry per downloaded format
    in ``requested_downloads``; its ``filepath`` is updated by ``post_process``,
    so it names the finished FLAC rather than the raw audio.  ``noplaylist`` and
    a single format mean there is only ever one entry.

    The suffix is checked rather than assumed.  The move below renames the
    produced file to ``<title>.flac``; if FFmpegExtractAudio had been skipped
    (ffmpeg missing, a format yt-dlp decided not to re-encode) that would file
    raw ``.m4a`` audio in the library under a ``.flac`` name, which every
    tagger and player downstream would then trust.

    Raises:
        DownloadError: If the produced file is not a FLAC.
    """
    downloads = (info or {}).get("requested_downloads") or []
    if not downloads:
        return None
    produced = downloads[0].get("filepath")
    if not produced or not os.path.exists(produced):
        return None
    suffix = Path(produced).suffix
    if suffix.lower() != ".flac":
        raise DownloadError(
            f"Download produced a {suffix or 'extensionless'} file, not FLAC; "
            "the audio conversion did not run"
        )
    return produced


def _remove_empty_output_dirs(output_path: Path, created_only: set[Path]) -> None:
    """Remove the album and artist directories of *output_path* if now empty.

    A failed download must not leave an empty ``Artist/Album`` pair in the
    library.  Only directories in *created_only* -- the ones that did not exist
    when this run started, see :func:`_missing_output_dirs` -- are candidates:
    an ``Artist/Album`` pair the user created by hand and has not filled yet is
    theirs to keep, and a concurrent job that has just created the same album
    folder must not have it pulled out from under its own ``os.replace``.

    ``rmdir`` additionally refuses a non-empty directory, so a folder that
    holds tracks is safe either way.
    """
    for directory in (output_path.parent, output_path.parent.parent):
        if directory not in created_only:
            return
        try:
            directory.rmdir()
        except OSError:
            return


def _missing_output_dirs(output_path: Path) -> set[Path]:
    """Return which of *output_path*'s album/artist directories do not exist yet.

    Sampled before the download starts, so the failure path can tell the
    directories this run would have created from ones that were already there.
    """
    return {
        directory
        for directory in (output_path.parent, output_path.parent.parent)
        if not directory.exists()
    }


def download_audio(
    job: Job,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> Path:
    """Download audio from the job's URL, convert to FLAC, embed metadata
    and thumbnail, and write to the file-organizer-determined path.

    Args:
        job: Job instance containing url, artist, and album.  Missing
            title/duration/thumbnail are filled in from yt-dlp metadata.
        on_progress: Optional callback invoked with download percentage (0-100).
        cancel_event: Optional event; when set, the download is aborted at the
            next progress callback.
        on_phase: Optional callback invoked with ``"metadata"`` once the job's
            metadata fields have been filled in, and ``"converting"`` when the
            FLAC conversion starts.

    Returns:
        Path to the downloaded FLAC file.

    Raises:
        DownloadError: If yt-dlp fails to download or convert, the output
            path is unsafe, or the output file is missing afterwards.
    """
    download_path = _get_download_path()
    logger.info("Starting download for job %s (url=%s)", job.id, job.url)
    logger.info("DOWNLOAD_PATH = %s", download_path)

    # Try to extract metadata for building the output path.  If this
    # fails (e.g. stale yt-dlp, transient network issue) we fall back
    # to the job's title or a generic name so the download can still
    # be attempted -- yt-dlp may resolve formats during the actual
    # download that it couldn't during a metadata-only probe.
    info: dict | None = None
    try:
        with yt_dlp.YoutubeDL(dict(_BASE_OPTS)) as ydl:
            info = ydl.extract_info(job.url, download=False)
    except yt_dlp.utils.YoutubeDLError as exc:
        logger.warning(
            "Pre-download metadata extraction failed for job %s: %s", job.id, exc
        )

    if info is not None:
        _reject_collections(info, job.url)
        title = info.get("title") or job.title or "Unknown Title"
        ytdlp_artist = _pick_artist(info)
        ytdlp_album = info.get("album")
        logger.info(
            "yt-dlp metadata: title=%r, artist=%r, album=%r",
            title,
            ytdlp_artist,
            ytdlp_album,
        )
        # Backfill anything the submit-time probe could not provide.
        job.title = job.title or title
        job.duration = job.duration or info.get("duration")
        job.thumbnail_url = job.thumbnail_url or info.get("thumbnail")
        if on_phase is not None:
            on_phase("metadata")
    else:
        title = job.title or "Unknown Title"
        ytdlp_artist = None
        ytdlp_album = None
        logger.warning(
            "No yt-dlp metadata available, using fallback title=%r", title
        )

    track_filename = track_filename_for(title)

    try:
        output_path = get_output_path(
            track_filename=track_filename,
            user_artist=job.artist,
            user_album=job.album,
            ytdlp_artist=ytdlp_artist,
            ytdlp_album=ytdlp_album,
            download_path=download_path,
        )
    except UnsafePathError as exc:
        logger.error("Refusing unsafe output path for job %s: %s", job.id, exc)
        raise DownloadError(f"Refusing unsafe output path: {exc}") from exc

    logger.info("Output file path: %s", output_path)

    # Sampled before anything is created so the failure path can tell "we made
    # this empty folder" from "the user's empty folder" (or a concurrent job's).
    would_create = _missing_output_dirs(output_path)

    # Nothing unfinished may be written into the library, so yt-dlp gets a
    # per-job scratch directory and we move the finished file out ourselves.
    temp_dir = job_temp_dir(job.id, download_path)
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Cannot create temp directory %s: %s", temp_dir, exc)
        raise DownloadError(f"Cannot create temp directory {temp_dir}: {exc}") from exc

    # yt-dlp runs the output template *and* `paths` through
    # `expandvars(expanduser(...))` before substituting any field, and there is
    # no option to turn that off (`$$` is not an escape either).  A title such
    # as "$HOME sweet home" would therefore expand to an absolute path, which
    # `os.path.join(paths["home"], filename)` happily lets win -- writing
    # outside the library.  So yt-dlp is only ever told about the job id and its
    # own scratch directory, neither of which can contain a `$`, and the move to
    # the real name is done here afterwards.
    outtmpl = f"{job.id}.%(ext)s"

    # Build yt-dlp options for actual download + FLAC conversion.
    opts = {
        **_BASE_OPTS,
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        # Everything -- finished file, .part/.ytdl scratch, thumbnail sidecar --
        # stays inside the job's own temp directory until we move the FLAC.
        "paths": {
            "home": str(temp_dir),
            "temp": str(temp_dir),
            "thumbnail": str(temp_dir),
        },
        "writethumbnail": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "flac",
                "preferredquality": "0",
            },
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "progress_hooks": [_make_progress_hook(on_progress, cancel_event)],
        "postprocessor_hooks": [_make_postprocessor_hook(on_phase)],
    }

    logger.info("Starting yt-dlp download for job %s ...", job.id)
    try:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                result_info = ydl.extract_info(job.url, download=True)

            produced = _produced_filepath(result_info)
            if produced is None:
                logger.error(
                    "Download reported success but produced no file for job %s", job.id
                )
                raise DownloadError(
                    f"Output file not found after download: {output_path.name}"
                )

            # Created only now: a failed download must not leave an empty
            # Artist/Album pair behind in the library.
            output_path.parent.mkdir(parents=True, exist_ok=True)
            # Same filesystem by construction (.tmp lives inside DOWNLOAD_PATH),
            # so this is a rename.  `os.replace` rather than `shutil.move`:
            # it overwrites an existing FLAC of the same name (the behaviour
            # yt-dlp had) and never moves the file *into* a directory that
            # happens to carry the target name.
            os.replace(produced, output_path)
        except yt_dlp.utils.DownloadCancelled as exc:
            logger.info("Download cancelled for job %s", job.id)
            raise DownloadError("Download cancelled") from exc
        except yt_dlp.utils.YoutubeDLError as exc:
            logger.error("Download failed for job %s: %s", job.id, exc)
            raise DownloadError(f"Download failed: {exc}") from exc
        except OSError as exc:
            logger.error("Filesystem error during download for job %s: %s", job.id, exc)
            raise DownloadError(f"Download failed: {exc}") from exc
    except Exception:
        # yt-dlp only ever sees the scratch directory, so the album folder can
        # only have been created by the `mkdir` two lines above `os.replace`;
        # this therefore has something to do only when that move itself failed.
        _remove_empty_output_dirs(output_path, would_create)
        raise

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(
        "Download complete for job %s: %s (%.2f MB)", job.id, output_path, size_mb
    )
    return output_path
