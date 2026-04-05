"""File organizer module for yt-dlp Web UI.

Determines the output file path for a downloaded track following the pattern:
    DOWNLOAD_PATH / Artist / Album / track.flac

Artist and album are resolved using a priority chain:
    1. User-provided values (if given)
    2. yt-dlp extracted metadata (fallback)
    3. "Unknown Artist" / "Unknown Album" (final fallback)
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DOWNLOAD_PATH = "/data/music/downloads"
FALLBACK_ARTIST = "Unknown Artist"
FALLBACK_ALBUM = "Unknown Album"


def _resolve(
    field_name: str,
    user_value: str | None,
    ytdlp_value: str | None,
    fallback: str,
) -> str:
    """Resolve a metadata field using the priority chain.

    Returns the first non-empty, non-whitespace value from:
    user_value -> ytdlp_value -> fallback.
    """
    if user_value is not None and user_value.strip():
        logger.debug("%s resolved from user-provided value: %r", field_name, user_value.strip())
        return user_value.strip()
    if ytdlp_value is not None and ytdlp_value.strip():
        logger.debug("%s resolved from yt-dlp metadata: %r", field_name, ytdlp_value.strip())
        return ytdlp_value.strip()
    logger.debug("%s resolved to fallback: %r", field_name, fallback)
    return fallback


def get_output_path(
    track_filename: str,
    user_artist: str | None = None,
    user_album: str | None = None,
    ytdlp_artist: str | None = None,
    ytdlp_album: str | None = None,
    download_path: str = DEFAULT_DOWNLOAD_PATH,
) -> Path:
    """Compute the output file path for a downloaded track.

    Args:
        track_filename: The filename of the track (e.g. "song.flac").
        user_artist: Artist name provided by the user (highest priority).
        user_album: Album name provided by the user (highest priority).
        ytdlp_artist: Artist name extracted by yt-dlp (fallback).
        ytdlp_album: Album name extracted by yt-dlp (fallback).
        download_path: Root download directory. Defaults to /data/music/downloads.

    Returns:
        Path object: download_path / Artist / Album / track_filename
    """
    artist = _resolve("artist", user_artist, ytdlp_artist, FALLBACK_ARTIST)
    album = _resolve("album", user_album, ytdlp_album, FALLBACK_ALBUM)

    output = Path(download_path) / artist / album / track_filename
    logger.info("Resolved output path: %s", output)
    return output
