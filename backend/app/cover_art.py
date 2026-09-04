"""Cover-art post-processing: downscaling what an external source hands us.

Cover Art Archive serves originals -- scans that are routinely 3000 px square
and several megabytes.  A ``cover.jpg`` that size costs disk on every album and
buys nothing: Navidrome and every client resize it down for display anyway.
Phase 9 fetches the art; this is the one step between the fetch and the write.

The rule everywhere here is that art is a nice-to-have.  Anything that goes
wrong -- ffmpeg missing from the image, a non-zero exit, a wedged process, an
empty pipe -- returns the original bytes and logs why, because filing an album
with a large cover is better than filing it with none.

Deliberately not used by the downloader: the embedded-thumbnail conversion in
``downloader.py`` runs ffmpeg itself, because YouTube thumbnails arrive small
enough not to need downscaling and that path has to stay cancellable.  This
helper is for the Cover Art Archive scans written to disk in Phase 9.
"""

from __future__ import annotations

import logging
import subprocess

from app.downloader import FFMPEG_BINARY

logger = logging.getLogger(__name__)

# Longest edge, in pixels, a stored cover is allowed to keep.  1000 px is large
# enough for a full-screen "now playing" view on a laptop and small enough that
# a thousand albums cost megabytes rather than gigabytes.
DEFAULT_MAX_PIXELS = 1000

# Seconds a single downscale may take.  One JPEG resize is milliseconds; a run
# still going after this is wedged, not slow.
DOWNSCALE_TIMEOUT_SECONDS = 20


def downscale_cover(data: bytes, max_px: int = DEFAULT_MAX_PIXELS) -> bytes:
    """Return *data* scaled so its width is at most *max_px*, or *data* itself.

    The image is piped through ffmpeg rather than written to a temporary file:
    the bytes are already in memory, and a pipe keeps a half-written file off
    the disk when a run dies.

    Three details in the command are load-bearing.  The quotes inside
    ``scale='min(<max>,iw)':-2`` are ffmpeg's own, not the shell's: the comma
    would otherwise end the filter, and there is no shell here to strip them.
    ``min`` is what stops a small cover being *up*-scaled, and ``-2`` keeps the
    aspect ratio while rounding to an even height.  ``-pix_fmt yuvj420p`` keeps
    a grayscale or CMYK source from being re-encoded as 4:4:4, which is
    noticeably larger than the file we started from.

    Returns the original bytes unchanged when ffmpeg cannot be run, exits
    non-zero, times out, or writes nothing.
    """
    if not data:
        return data

    command = [
        FFMPEG_BINARY,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        "pipe:0",
        # One frame out, whatever the input holds (an MPO or animated source
        # would otherwise stream every frame down the pipe).
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({max_px},iw)':-2",
        "-pix_fmt",
        "yuvj420p",
        "-q:v",
        "2",
        "-f",
        "image2pipe",
        "-c:v",
        "mjpeg",
        "pipe:1",
    ]

    try:
        completed = subprocess.run(
            command,
            input=data,
            capture_output=True,
            timeout=DOWNSCALE_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        logger.warning(
            "%s is not installed; keeping the cover at its original size",
            FFMPEG_BINARY,
        )
        return data
    except subprocess.TimeoutExpired:
        # run() has already killed the child by the time this is raised.
        logger.warning(
            "Downscaling the cover timed out after %ss; keeping the original",
            DOWNSCALE_TIMEOUT_SECONDS,
        )
        return data
    except OSError as exc:
        logger.warning("Could not start %s to downscale the cover: %s", FFMPEG_BINARY, exc)
        return data

    if completed.returncode != 0:
        logger.warning(
            "Downscaling the cover failed (%s exited %d); keeping the original: %s",
            FFMPEG_BINARY,
            completed.returncode,
            (completed.stderr or b"").decode("utf-8", "replace").strip(),
        )
        return data

    if not completed.stdout:
        logger.warning(
            "Downscaling the cover produced no output; keeping the original"
        )
        return data

    logger.info(
        "Downscaled cover from %d to %d bytes", len(data), len(completed.stdout)
    )
    return completed.stdout
