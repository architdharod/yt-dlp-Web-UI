"""Downloader module for yt-dlp Web UI.

Provides two operations using yt-dlp as a Python library:
  1. extract_metadata(url) -- fetches title, thumbnail URL, and duration
  2. download_audio(job, on_progress) -- downloads audio as FLAC with embedded
     metadata and thumbnail, reporting progress via callback

No CLI subprocess calls -- yt-dlp is used entirely through its Python API.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yt_dlp

from app.file_organizer import DEFAULT_DOWNLOAD_PATH, get_output_path
from app.models import Job

logger = logging.getLogger(__name__)


@dataclass
class TrackMetadata:
    """Metadata extracted from a URL before downloading."""

    title: str
    thumbnail_url: str | None
    duration: float | None


class DownloadError(Exception):
    """Raised when a download or metadata extraction fails."""


def _get_download_path() -> str:
    """Read DOWNLOAD_PATH from environment, falling back to the default."""
    return os.environ.get("DOWNLOAD_PATH", DEFAULT_DOWNLOAD_PATH)


def extract_metadata(url: str) -> TrackMetadata:
    """Extract track metadata from a URL without downloading.

    Uses yt-dlp to fetch the page and extract title, thumbnail URL,
    and duration.  No audio data is downloaded.

    Args:
        url: YouTube or SoundCloud URL.

    Returns:
        TrackMetadata with title, thumbnail_url, and duration.

    Raises:
        DownloadError: If yt-dlp cannot extract metadata.
    """
    logger.info("Extracting metadata for URL: %s", url)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        logger.error("Metadata extraction failed for %s: %s", url, exc)
        raise DownloadError(f"Failed to extract metadata: {exc}") from exc

    if info is None:
        logger.error("yt-dlp returned no metadata for %s", url)
        raise DownloadError("yt-dlp returned no metadata")

    metadata = TrackMetadata(
        title=info.get("title", "Unknown Title"),
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


def _make_progress_hook(
    on_progress: Callable[[float], None] | None,
) -> Callable[[dict], None]:
    """Create a yt-dlp progress hook that translates raw progress dicts
    into a simple percentage and forwards it to *on_progress*.
    """

    def hook(d: dict) -> None:
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


def download_audio(
    job: Job,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Download audio from the job's URL, convert to FLAC, embed metadata
    and thumbnail, and write to the file-organizer-determined path.

    Args:
        job: Job instance containing url, artist, and album.
        on_progress: Optional callback invoked with download percentage (0-100).

    Returns:
        Path to the downloaded FLAC file.

    Raises:
        DownloadError: If yt-dlp fails to download or convert.
    """
    download_path = _get_download_path()
    logger.info("Starting download for job %s (url=%s)", job.id, job.url)
    logger.info("DOWNLOAD_PATH = %s", download_path)

    # Try to extract metadata for building the output path.  If this
    # fails (e.g. stale yt-dlp, transient network issue) we fall back
    # to the job's title or a generic name so the download can still
    # be attempted — yt-dlp may resolve formats during the actual
    # download that it couldn't during a metadata-only probe.
    info: dict | None = None
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(job.url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        logger.warning(
            "Pre-download metadata extraction failed for job %s: %s", job.id, exc
        )

    if info is not None:
        title = info.get("title", "Unknown Title")
        ytdlp_artist = info.get("artist") or info.get("uploader")
        ytdlp_album = info.get("album")
        logger.info(
            "yt-dlp metadata: title=%r, artist=%r, album=%r",
            title,
            ytdlp_artist,
            ytdlp_album,
        )
    else:
        title = job.title or "Unknown Title"
        ytdlp_artist = None
        ytdlp_album = None
        logger.warning(
            "No yt-dlp metadata available, using fallback title=%r", title
        )

    track_filename = yt_dlp.utils.sanitize_filename(title) + ".flac"

    output_path = get_output_path(
        track_filename=track_filename,
        user_artist=job.artist,
        user_album=job.album,
        ytdlp_artist=ytdlp_artist,
        ytdlp_album=ytdlp_album,
        download_path=download_path,
    )

    # Ensure the target directory exists.
    target_dir = output_path.parent
    dir_existed = target_dir.exists()
    target_dir.mkdir(parents=True, exist_ok=True)
    if dir_existed:
        logger.info("Output directory already exists: %s", target_dir)
    else:
        logger.info("Created output directory: %s", target_dir)
    logger.info("Output file path: %s", output_path)

    # Build yt-dlp options for actual download + FLAC conversion.
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_path.with_suffix(".%(ext)s")),
        "quiet": True,
        "no_warnings": True,
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
        "progress_hooks": [_make_progress_hook(on_progress)],
    }

    logger.info("Starting yt-dlp download for job %s ...", job.id)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([job.url])
    except yt_dlp.utils.DownloadError as exc:
        logger.error("Download failed for job %s: %s", job.id, exc)
        raise DownloadError(f"Download failed: {exc}") from exc

    # Verify the file was actually written
    if output_path.exists():
        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info(
            "Download complete for job %s: %s (%.2f MB)", job.id, output_path, size_mb
        )
    else:
        logger.warning(
            "Download reported success but output file not found: %s", output_path
        )

    return output_path
