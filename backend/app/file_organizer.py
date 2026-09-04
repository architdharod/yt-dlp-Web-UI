"""File organizer module for yt-dlp Web UI.

Determines the output file path for a downloaded track following the pattern:
    DOWNLOAD_PATH / Artist / Album / track.flac

Artist and album are resolved using a priority chain:
    1. User-provided values (if given)
    2. yt-dlp extracted metadata (fallback)
    3. "Unknown Artist" / "Unknown Album" (final fallback)

Every path component is sanitised so that user- or site-supplied names can
never escape ``DOWNLOAD_PATH`` (no separators, no ``..``, no absolute paths).
"""

import logging
from pathlib import Path

from yt_dlp.utils import sanitize_filename

logger = logging.getLogger(__name__)

DEFAULT_DOWNLOAD_PATH = "/data/music/downloads"
FALLBACK_ARTIST = "Unknown Artist"
FALLBACK_ALBUM = "Unknown Album"

# Scratch directory inside DOWNLOAD_PATH where yt-dlp writes everything
# unfinished, and the directory Phase 7 moves deleted tracks into.  Both are
# hidden so music library scanners skip them, and both live here rather than in
# the downloader because this module has to keep them out of artist/album names.
TEMP_DIRNAME = ".tmp"
TRASH_DIRNAME = ".trash"

# Names that must never become a path component: "." and ".." mean "this
# directory" and "parent directory", and the two housekeeping directories are
# swept by the boot cleanup, which would happily delete an artist called ".tmp".
# ``sanitize_filename`` leaves all of them alone, so we must not.  Compared
# case-folded because the filesystem may well be case-insensitive.
#
# A leading dot on its own is fine: "...And You Will Know Us by the Trail of
# Dead" is a real band.
_RESERVED_NAMES = {".", "..", TEMP_DIRNAME, TRASH_DIRNAME}


class UnsafePathError(ValueError):
    """Raised when a resolved output path would fall outside DOWNLOAD_PATH."""


def sanitize_component(value: str, fallback: str) -> str:
    """Turn an arbitrary string into a single safe path component.

    Path separators are replaced with look-alike characters by yt-dlp's
    ``sanitize_filename``; empty results and the reserved names it leaves
    untouched (see :data:`_RESERVED_NAMES`) fall back to *fallback*.
    """
    cleaned = sanitize_filename(value).strip()
    if not cleaned or cleaned.casefold() in _RESERVED_NAMES:
        logger.debug("Component %r is unsafe or empty, using fallback %r", value, fallback)
        return fallback
    return cleaned


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


def resolve_artist_album(
    user_artist: str | None = None,
    user_album: str | None = None,
    ytdlp_artist: str | None = None,
    ytdlp_album: str | None = None,
) -> tuple[str, str]:
    """Return the sanitised ``(artist, album)`` a download will be filed under.

    Split out of :func:`get_output_path` so the tagger can write exactly the
    names the folders carry.  A FLAC whose ``ALBUMARTIST`` disagrees with the
    folder it sits in is what makes Navidrome and Lidarr file the same track
    twice, so both must come from one resolution and not be resolved twice.
    """
    artist = sanitize_component(
        _resolve("artist", user_artist, ytdlp_artist, FALLBACK_ARTIST), FALLBACK_ARTIST
    )
    album = sanitize_component(
        _resolve("album", user_album, ytdlp_album, FALLBACK_ALBUM), FALLBACK_ALBUM
    )
    return artist, album


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

    Raises:
        UnsafePathError: If the resulting path would fall outside download_path.
    """
    artist, album = resolve_artist_album(
        user_artist, user_album, ytdlp_artist, ytdlp_album
    )
    filename = sanitize_component(track_filename, "Unknown Title.flac")

    root = Path(download_path)
    output = root / artist / album / filename

    # Belt and braces: even after sanitising, refuse anything that resolves
    # outside the download root.
    if not output.resolve().is_relative_to(root.resolve()):
        raise UnsafePathError(f"Output path {output} escapes download root {root}")

    logger.info("Resolved output path: %s", output)
    return output
