"""Downloader module for yt-dlp Web UI.

Provides two operations:
  1. extract_metadata(url) -- fetches title, thumbnail URL, and duration
  2. download_audio(job, on_progress, ...) -- runs the three-stage pipeline
     below, reporting progress and phase changes via callbacks

The pipeline is deliberately ours rather than yt-dlp's::

    yt-dlp (best audio, no postprocessors)  ->  ffmpeg (FLAC)  ->  mutagen (tags)

yt-dlp is used through its Python API and given no postprocessors at all, so it
only ever writes the raw best-audio stream and the thumbnail sidecar into the
job's scratch directory.  We then run ``ffmpeg`` ourselves as a
:class:`subprocess.Popen` -- the one thing yt-dlp's ``FFmpegExtractAudio``
cannot give us is a handle on that process, and without a handle a conversion
cannot be cancelled or timed out; it just runs to completion while the user
watches a job that says it is stopping.  Finally mutagen writes the tags,
the ``SOURCEID``/``SOURCEURL`` provenance fields the library's dedup and tag
fixing rely on, and the cover art, which is more control than
``FFmpegMetadata`` and ``EmbedThumbnail`` offered (they also inherited whatever
junk the source container carried, which ``-map_metadata -1`` now drops).
"""

import contextlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import yt_dlp
import yt_dlp.cookies
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

from app.file_organizer import (
    DEFAULT_DOWNLOAD_PATH,
    FALLBACK_ALBUM,
    TEMP_DIRNAME,
    UnsafePathError,
    get_output_path,
    resolve_artist_album,
)
from app.models import (
    ALREADY_IN_LIBRARY_PREFIX,
    BANDCAMP_NO_STREAM_MESSAGE,
    Job,
)

logger = logging.getLogger(__name__)

# Seconds a single socket operation may block before yt-dlp gives up on it.
SOCKET_TIMEOUT_SECONDS = 15

# The converter.  Not configurable: it ships in the image next to yt-dlp, which
# needs it for muxing anyway, so a path override would only ever be a way to
# point the container at something that is not there.
FFMPEG_BINARY = "ffmpeg"

# libFLAC's own default.  Higher levels cost noticeably more CPU for well under
# a percent of size on already-lossy source audio, which is what every one of
# these downloads is.
FLAC_COMPRESSION_LEVEL = 5

# How long a terminated ffmpeg gets to exit before it is killed outright.
# Encoding one track holds no state worth flushing, so this only has to cover
# process teardown.
FFMPEG_TERMINATE_GRACE_SECONDS = 5

# The only two image formats a FLAC PICTURE block may carry in practice: the
# spec names a MIME type freely, but players and taggers in the wild assume
# JPEG or PNG, so anything else (YouTube serves WebP) is transcoded before it
# is embedded.  These are the leading bytes each one starts with; a sidecar's
# extension comes from the server's Content-Type and is not always right, so
# the file is sniffed rather than trusted.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)

# Extensions yt-dlp gives a thumbnail sidecar.  Used to tell the sidecar apart
# from the audio file in the scratch directory, both of which are named after
# the job id.
_THUMBNAIL_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})

# ffmpeg's stderr is unbounded; only the tail is useful in a job's error text.
_FFMPEG_ERROR_TAIL_CHARS = 400

# How often a running ffmpeg is looked at while nothing has asked it to stop.
# Only the escalation to SIGKILL needs this poll: the SIGTERM itself is sent by
# whoever cancels, and a process that honours it ends ``communicate`` by
# exiting.  See :func:`_await_ffmpeg`.
_CANCEL_POLL_SECONDS = 0.25

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
    same reason: a thumbnail yt-dlp could not fetch is reported only this way,
    and it explains a FLAC that arrives without cover art.
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


# Options shared by every YoutubeDL instance this module creates.  Use
# :func:`base_opts`, which adds the cookie jar when one is configured.
_STATIC_OPTS: dict = {
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

# Points at a Netscape-format cookies file exported from a signed-in browser.
# Optional: without it YouTube is fetched anonymously, which is fine until it
# starts asking the container to prove it is not a bot.
COOKIES_ENV_VAR = "YTDLP_COOKIES_FILE"

# How long yt-dlp waits between extractor requests, and where that is
# configured.  0.75 is what yt-dlp's own `-t sleep` preset uses.
SLEEP_REQUESTS_ENV = "YTDLP_SLEEP_REQUESTS"
DEFAULT_SLEEP_INTERVAL_REQUESTS = 0.75

# The PO-token provider sidecar (`brainicism/bgutil-ytdlp-pot-provider`), and
# the extractor-args key its yt-dlp plugin reads its base URL from.  A PO token
# is what keeps an anonymous, containerised yt-dlp out of YouTube's bot check
# in the first place; cookies stay as the manual last resort.
POT_PROVIDER_ENV = "POT_PROVIDER_URL"
POT_PROVIDER_EXTRACTOR_KEY = "youtubepot-bgutilhttp"

# The one cookie jar every YoutubeDL in this process shares, loaded once at
# startup by :func:`load_cookie_jar`.
#
# Not `cookiefile`: yt-dlp *writes the jar back* to that path when a YoutubeDL
# is closed (YoutubeDL.close -> save_cookies -> cookiejar.save), with a plain
# open('w') and no locking, so two downloads finishing at once shred the file
# between them -- permanently, since it is also the file the next run reads.
# Assigning the jar to `ydl.cookiejar` instead leaves `cookiefile` unset, which
# makes save_cookies a no-op, so nothing is ever written: the configured file
# is read once and only read.  The stdlib CookieJar this inherits from holds an
# RLock across every add/extract, so one jar shared by concurrent downloads is
# safe -- and better than a jar each, because a cookie YouTube rotates in
# during one download is there for the next.
_cookie_jar: "yt_dlp.cookies.YoutubeDLCookieJar | None" = None


def cookies_file() -> str | None:
    """The configured cookies file, or ``None`` when the feature is off.

    docker compose substitutes an unset variable with an empty string, so
    ``""`` counts as unset rather than as a path to nowhere.
    """
    return os.environ.get(COOKIES_ENV_VAR) or None


def load_cookie_jar() -> None:
    """Read the configured cookies file into the process-wide jar.

    Called from :func:`validate_cookies_file` at startup, so a file that is
    not actually loadable -- a JSON export from the wrong extension, a file
    that is not Netscape format at all -- stops the container at boot with a
    message rather than failing every download for the rest of its life.

    Nothing about the file's *contents* is ever logged.

    Raises:
        RuntimeError: If the file cannot be parsed as a Netscape cookies file.
    """
    global _cookie_jar
    # Cleared first, so a reload that fails leaves no stale jar behind.
    _cookie_jar = None
    path = cookies_file()
    if path is None:
        return
    jar = yt_dlp.cookies.YoutubeDLCookieJar(path)
    try:
        jar.load()
    # LoadError (a malformed Netscape file) is an OSError; a binary or UTF-16
    # file raises UnicodeDecodeError, which is a ValueError.  Both are the
    # same mistake to the user, so both get the same message.
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"{COOKIES_ENV_VAR} {path!r} could not be read as a cookies file: "
            f"{exc}. It must be a Netscape-format export (the first line is "
            '"# Netscape HTTP Cookie File"), not a JSON one. See the README.'
        ) from exc
    _cookie_jar = jar
    # The count, never the cookies.
    logger.info("Loaded %d cookies from %s %s", len(jar), COOKIES_ENV_VAR, path)


def validate_cookies_file() -> None:
    """Fail fast when ``YTDLP_COOKIES_FILE`` points at nothing usable.

    Called from the app lifespan.  A cookies file that silently does not load
    is the worst outcome: downloads keep failing with YouTube's bot check and
    nothing says why.

    Raises:
        RuntimeError: If the variable is set but the path is not a readable,
            loadable Netscape cookies file.
    """
    path = cookies_file()
    if path is None:
        return
    candidate = Path(path)
    if candidate.exists() and not candidate.is_file():
        raise RuntimeError(
            f"{COOKIES_ENV_VAR} {path!r} exists but is not a regular file. "
            "Under docker compose this usually means YTDLP_COOKIES_HOST_PATH "
            "is unset, so its /dev/null default was bind-mounted at that "
            f"path: set YTDLP_COOKIES_HOST_PATH to the cookies file on the "
            f"host, or unset {COOKIES_ENV_VAR}."
        )
    if not candidate.is_file():
        raise RuntimeError(
            f"{COOKIES_ENV_VAR} {path!r} does not exist or is not a file. "
            "Point it at a Netscape-format cookies file readable by the "
            f"container user, or unset {COOKIES_ENV_VAR}."
        )
    if not os.access(candidate, os.R_OK):
        raise RuntimeError(
            f"{COOKIES_ENV_VAR} {path!r} is not readable by this process. "
            "Fix its ownership/permissions (the container runs as "
            f"PUID:PGID) or unset {COOKIES_ENV_VAR}."
        )
    load_cookie_jar()


def sleep_interval_requests() -> float:
    """Seconds yt-dlp sleeps between extractor requests, from the env.

    Prevention rather than cure: yt-dlp's own ``-t sleep`` preset uses 0.75 for
    exactly this, and pacing the extractor is what keeps a forty-track playlist
    from arriving at YouTube as forty requests in two seconds.  It applies to
    every session this app opens, the probe's flat enumeration included --
    which is the one that fires the most requests in the shortest time.

    ``0`` turns it off; an unset or empty variable (which is what docker
    compose substitutes) means the default.
    """
    raw = os.environ.get(SLEEP_REQUESTS_ENV)
    if not raw:
        return DEFAULT_SLEEP_INTERVAL_REQUESTS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number; using %s",
            SLEEP_REQUESTS_ENV,
            raw,
            DEFAULT_SLEEP_INTERVAL_REQUESTS,
        )
        return DEFAULT_SLEEP_INTERVAL_REQUESTS
    return max(0.0, value)


def pot_provider_url() -> str | None:
    """The PO-token provider sidecar's base URL, or ``None`` when unset.

    Empty counts as unset, which leaves the plugin at its own default
    (``http://127.0.0.1:4416``) -- and with no sidecar there, simply logging
    that it could not be reached.  Under compose the variable is set to the
    service, so this is only ever ``None`` for a backend run by hand.
    """
    return os.environ.get(POT_PROVIDER_ENV) or None


def base_opts() -> dict:
    """The yt-dlp options every extraction in this app starts from.

    A fresh dict each call, because callers routinely add to what they get
    back.  Cookies are *not* in here; they reach yt-dlp as the shared jar
    :func:`ytdl` attaches.
    """
    opts = dict(_STATIC_OPTS)

    sleep = sleep_interval_requests()
    if sleep > 0:
        opts["sleep_interval_requests"] = sleep

    provider = pot_provider_url()
    if provider:
        # The bgutil HTTP provider plugin's documented key.  yt-dlp's
        # `extractor_args` is {ie_key_lowercase: {arg: [values]}}, so the URL
        # travels as a one-element list -- the same shape
        # `--extractor-args "youtubepot-bgutilhttp:base_url=..."` produces.
        opts["extractor_args"] = {
            POT_PROVIDER_EXTRACTOR_KEY: {"base_url": [provider]}
        }

    return opts


def describe_pot_providers() -> list[str]:
    """The PO-token providers yt-dlp has loaded, for the startup banner.

    yt-dlp only mentions these at ``-v`` trace level and only when a video is
    actually fetched, which is far too late to notice that the plugin is not
    installed.  Constructing one ``YoutubeDL`` is what triggers plugin
    discovery, after which the registry names what was found.

    The registry is private, so every step is defensive: a yt-dlp that moves it
    costs a log line, never a boot.
    """
    try:
        with ytdl(base_opts()):
            from yt_dlp.extractor.youtube.pot import _registry

            names = sorted(_registry._pot_providers.value.keys())
    except Exception as exc:  # pragma: no cover - depends on yt-dlp internals
        logger.debug("Could not list yt-dlp PO-token providers: %s", exc)
        return []
    return names


@contextlib.contextmanager
def ytdl(opts: dict):
    """A ``YoutubeDL`` for ``opts``, wired to the shared cookie jar.

    The only way this module constructs one.  ``params={"cookiejar": ...}``
    would be ignored -- ``YoutubeDL.cookiejar`` is a ``cached_property`` -- so
    the jar is assigned onto the instance, which has to happen before the
    first request and does.
    """
    with yt_dlp.YoutubeDL(opts) as ydl:
        if _cookie_jar is not None:
            ydl.cookiejar = _cookie_jar
        yield ydl


# YouTube auto-generated artist channels are named "<Artist> - Topic".
_TOPIC_SUFFIX = " - Topic"


@dataclass
class TrackMetadata:
    """Metadata extracted from a URL before downloading.

    ``artist`` and ``album`` are what yt-dlp reported, unresolved and
    unsanitised: the caller feeds them to
    :func:`~app.file_organizer.resolve_artist_album` together with whatever the
    user typed, so that the folder a job is going to land in is known the moment
    it is queued rather than only once the download thread starts.
    """

    title: str
    thumbnail_url: str | None
    duration: float | None
    artist: str | None = None
    album: str | None = None


class DownloadError(Exception):
    """Raised when a download or metadata extraction fails."""


CANCELLED_MESSAGE = "Download cancelled"

# Both live in :mod:`app.models`, which cannot import this module, and are
# re-exported here because this is where the pipeline raises them and where
# every caller has always looked for them.  :attr:`app.models.Job.skipped` is
# derived from the same two strings.

# The two halves of yt-dlp's message that, together, mean exactly that.  Both
# are required: "No video formats found" from any other extractor is a
# different failure and keeps its own wording, and a Bandcamp error that is not
# this one does too.  The tag is matched as yt-dlp writes it -- ``[Bandcamp]``
# or an ``IE_NAME`` with one suffix, ``[Bandcamp:album]``, ``[Bandcamp:user]``
# -- rather than as anything between brackets, so a *track title* in brackets
# cannot trip it.
_BANDCAMP_TAG = re.compile(r"\[Bandcamp(?::\w+)?\]", re.IGNORECASE)
_NO_FORMATS = "No video formats found"


def readable_ytdlp_message(message: str) -> str | None:
    """A sentence for the user, or None to keep yt-dlp's own words.

    One case so far.  Everything else is None: yt-dlp's messages are usually
    the most specific thing anyone has, and paraphrasing them by guesswork
    loses more than it gains.

    Split from :func:`_readable_ytdlp_error` because the probe reaches this
    with a *string* -- ``ignoreerrors`` turns a top-level failure into a logged
    line and a ``None`` return, so there is no exception to hand over.
    """
    if _NO_FORMATS in message and _BANDCAMP_TAG.search(message):
        return BANDCAMP_NO_STREAM_MESSAGE
    return None


def _readable_ytdlp_error(exc: Exception, fallback: str) -> str:
    """*fallback*, unless yt-dlp said something we can say better."""
    return readable_ytdlp_message(str(exc)) or fallback


def _raise_if_cancelled(cancel: "CancelToken | None") -> None:
    """Abort the run if the stop button has been pressed.

    Called at every point the pipeline is between two long operations and has
    no hook of its own -- yt-dlp's progress hook and ffmpeg's process handle
    only cover the stages they belong to.

    Raises:
        DownloadError: If *cancel* is set.
    """
    if cancel is not None and cancel.is_set():
        raise DownloadError(CANCELLED_MESSAGE)


class CancelToken:
    """One download run's stop button.

    Two different things have to be interruptible and neither can see the
    other: yt-dlp is only interruptible from inside its own progress hook (by
    raising), while ffmpeg is only interruptible from outside (by signalling
    the process).  A plain ``threading.Event`` covers the first and nothing
    covers the second, so the flag and the process handle live together here
    behind one lock.

    The lock is what closes the race that matters.  Without it, a cancel that
    arrives between "ffmpeg has been spawned" and "the handle has been stored"
    sees no process, does nothing, and leaves a conversion running that nobody
    can stop.  :meth:`register_process` therefore claims the slot and re-reads
    the flag in the same critical section and reports back whether the caller
    still owns the run.

    ``is_set`` keeps the ``threading.Event`` spelling so the yt-dlp progress
    hook can be handed either this or a bare Event.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._process: subprocess.Popen | None = None

    def is_set(self) -> bool:
        """Return whether this run has been asked to stop."""
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        """Ask the run to stop, signalling a registered child process if there is one.

        Safe to call more than once and from any thread.  Deliberately does not
        wait for anything: the caller is the event loop, and parking it for the
        grace period an ffmpeg that ignores SIGTERM is entitled to would freeze
        every other request for those seconds.  ``terminate`` only, so ffmpeg
        gets to close its output file; escalating to ``kill`` and reaping the
        process belongs to the run's own thread, which is already parked on
        ``communicate`` with the pipes open (see :func:`_await_ffmpeg`).
        """
        with self._lock:
            self._cancelled = True
            process = self._process
        if process is None:
            return
        try:
            process.terminate()
        except OSError:
            # Already gone: the run's thread reaped it between the two locks.
            pass

    def register_process(self, process: subprocess.Popen) -> bool:
        """Take ownership of *process*, unless the run was already cancelled.

        Returns ``False`` when a cancel got in first, in which case the caller
        must dispose of the process itself -- nobody else holds a reference.
        """
        with self._lock:
            if self._cancelled:
                return False
            self._process = process
            return True

    def clear_process(self) -> None:
        """Forget the current child process once it has exited."""
        with self._lock:
            self._process = None


def _terminate_process(process: subprocess.Popen) -> None:
    """Stop *process*, escalating from SIGTERM to SIGKILL, and reap it.

    ``wait`` after each signal is not optional: without it the child stays a
    zombie until the parent exits, and the caller would go on to delete an
    output file that a still-running ffmpeg is about to write to again.  Which
    is also why this blocks and :meth:`CancelToken.cancel` does not -- the only
    caller is the download thread disposing of a process it could not hand over,
    and that thread has nothing else to do meanwhile.
    """
    try:
        process.terminate()
        process.wait(timeout=FFMPEG_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning(
            "ffmpeg (pid %s) ignored SIGTERM for %ss, killing it",
            process.pid,
            FFMPEG_TERMINATE_GRACE_SECONDS,
        )
        process.kill()
        process.wait()
    except OSError:
        # Already reaped between the flag check and the signal.
        pass


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
        **base_opts(),
        "skip_download": True,
        # This is only a quick probe; the download phase retries properly.
        "retries": 1,
        "extractor_retries": 1,
    }

    try:
        with ytdl(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.YoutubeDLError as exc:
        logger.error("Metadata extraction failed for %s: %s", url, exc)
        raise DownloadError(
            _readable_ytdlp_error(exc, f"Failed to extract metadata: {exc}")
        ) from exc

    if info is None:
        logger.error("yt-dlp returned no metadata for %s", url)
        raise DownloadError("yt-dlp returned no metadata")

    _reject_collections(info, url)

    metadata = TrackMetadata(
        title=info.get("title") or "Unknown Title",
        thumbnail_url=info.get("thumbnail"),
        duration=info.get("duration"),
        artist=_pick_artist(info),
        album=info.get("album"),
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
    Lidarr ignore it (the same reason Phase 7 puts ``.trash`` here).  Phase 4's
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
    cancel: CancelToken | threading.Event | None = None,
) -> Callable[[dict], None]:
    """Create a yt-dlp progress hook that translates raw progress dicts
    into a simple percentage and forwards it to *on_progress*.

    If *cancel* is set, the hook aborts the download by raising
    ``DownloadCancelled``, which yt-dlp propagates out of ``download()``.  This
    is the only place a running yt-dlp can be interrupted from, which is why
    the hook is registered even when nobody wants progress.
    """

    def hook(d: dict) -> None:
        if cancel is not None and cancel.is_set():
            raise yt_dlp.utils.DownloadCancelled(CANCELLED_MESSAGE)

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


def _downloaded_audio_path(info: dict | None) -> Path | None:
    """Return the raw audio file yt-dlp wrote, or ``None`` if it wrote nothing.

    ``extract_info(..., download=True)`` records one entry per downloaded format
    in ``requested_downloads``; with ``noplaylist`` and a single format there is
    only ever one.  Its ``filepath`` is the file as downloaded -- no
    postprocessor has touched it, because we register none -- so its extension
    is whatever container the site served (``.webm``, ``.m4a``, ``.opus``) and
    is deliberately not checked here.  Converting it is our job, and ffmpeg
    decides what it can read.
    """
    downloads = (info or {}).get("requested_downloads") or []
    if not downloads:
        return None
    produced = downloads[0].get("filepath")
    if not produced or not os.path.exists(produced):
        return None
    return Path(produced)


def _await_ffmpeg(process: subprocess.Popen, cancel: CancelToken | None) -> bytes:
    """Wait for *process* to exit, draining its pipes, and return its stderr.

    ``communicate`` rather than ``wait``: ffmpeg's diagnostics go to stderr, and
    a pipe nobody drains fills its buffer and deadlocks the child.  Retrying it
    after a ``TimeoutExpired`` loses no output.

    This is also where a cancel is escalated from SIGTERM to SIGKILL.
    :meth:`CancelToken.cancel` runs on the event loop, so it only signals and
    returns; waiting out the grace period there would freeze every other request
    for as long as ffmpeg ignored the signal.  Here there is nothing to freeze --
    the thread is parked on this call either way -- and it is the thread that
    must not go on to delete files a live ffmpeg is still writing.
    """
    if cancel is None:
        return process.communicate()[1]

    deadline: float | None = None
    killed = False
    while True:
        if killed:
            # SIGKILL cannot be ignored; there is nothing left to escalate to.
            timeout = None
        elif cancel.is_set():
            if deadline is None:
                deadline = time.monotonic() + FFMPEG_TERMINATE_GRACE_SECONDS
            timeout = max(deadline - time.monotonic(), 0.0)
        else:
            timeout = _CANCEL_POLL_SECONDS
        try:
            return process.communicate(timeout=timeout)[1]
        except subprocess.TimeoutExpired:
            if killed or deadline is None or time.monotonic() < deadline:
                continue
            logger.warning(
                "ffmpeg (pid %s) ignored SIGTERM for %ss, killing it",
                process.pid,
                FFMPEG_TERMINATE_GRACE_SECONDS,
            )
            process.kill()
            killed = True


def _run_ffmpeg(
    arguments: list[str],
    cancel: CancelToken | None,
    description: str,
) -> None:
    """Run ffmpeg to completion, or raise.

    The process is spawned in its own session so a signal aimed at the backend
    (a container stop, a Ctrl-C in development) does not reach it mid-write and
    leave a half-flushed file we would then have to recognise as corrupt; it is
    stopped through the cancel token instead, which is the only path that also
    knows to clean up afterwards.  ``stdin`` is closed because ffmpeg reads its
    console for interactive commands and would otherwise eat whatever the
    backend's own stdin holds.

    Raises:
        DownloadError: If ffmpeg is missing, exits non-zero, or the run was
            cancelled while it was working.
    """
    command = [FFMPEG_BINARY, *arguments]
    logger.info("Running %s: %s", description, " ".join(command))
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise DownloadError(
            f"{FFMPEG_BINARY} is not installed or not on PATH; "
            "audio cannot be converted"
        ) from exc
    except OSError as exc:
        raise DownloadError(f"Could not start {FFMPEG_BINARY}: {exc}") from exc

    if cancel is not None and not cancel.register_process(process):
        # Cancelled between the spawn and the handover: nobody else has this
        # handle, so this thread has to be the one that reaps it -- and to close
        # the two pipes it just opened, which `with` does on the way out.
        with process:
            _terminate_process(process)
        raise DownloadError(CANCELLED_MESSAGE)

    try:
        stderr = _await_ffmpeg(process, cancel)
    except BaseException:
        # Nothing else holds this handle, so an exception out of the wait --
        # ffmpeg killed from under us, the thread interrupted -- would otherwise
        # leave a live child and two open pipes behind.  Deliberately not a
        # `with process:` around the wait itself: its ``__exit__`` waits without
        # a timeout, so a wedged ffmpeg would park this thread forever instead
        # of being escalated to SIGKILL here.
        with process:
            _terminate_process(process)
        raise
    finally:
        if cancel is not None:
            cancel.clear_process()

    _raise_if_cancelled(cancel)
    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        raise DownloadError(
            f"{description} failed (exit {process.returncode}): "
            f"{detail[-_FFMPEG_ERROR_TAIL_CHARS:] or 'no output'}"
        )


def _convert_to_flac(source: Path, target: Path, cancel: CancelToken | None) -> None:
    """Encode *source* to FLAC at *target*.

    ``-vn`` drops the cover-art "video" stream some containers carry, and
    ``-map_metadata -1`` drops the source's tags wholesale.  Both are about the
    same thing: everything the finished file says about itself is written by
    :func:`_write_tags` from metadata we resolved, so nothing arrives by
    accident from a container yt-dlp happened to pick.  Without them a YouTube
    m4a donates a ``comment`` full of the video description and a ``PURL``, and
    the embedded art ends up duplicated as a stream *and* a PICTURE block.
    """
    _run_ffmpeg(
        [
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-map_metadata",
            "-1",
            "-c:a",
            "flac",
            "-compression_level",
            str(FLAC_COMPRESSION_LEVEL),
            str(target),
        ],
        cancel,
        "FLAC conversion",
    )
    if not target.exists():
        raise DownloadError("FLAC conversion produced no file")


def _sniff_image_mime(data: bytes) -> str | None:
    """Return the MIME type of *data*, or ``None`` if it is not JPEG or PNG."""
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    return None


def _find_thumbnail(temp_dir: Path, job_id: str) -> Path | None:
    """Return the thumbnail sidecar ``writethumbnail`` left in *temp_dir*.

    Everything in the scratch directory is named after the job id, so the
    sidecar is told apart from the audio by its extension.  ``sorted`` only
    makes the choice deterministic if a site ever hands us two.
    """
    candidates = sorted(
        entry
        for entry in temp_dir.glob(f"{job_id}.*")
        if entry.suffix.lower() in _THUMBNAIL_SUFFIXES and entry.is_file()
    )
    return candidates[0] if candidates else None


def _cover_picture(
    thumbnail: Path, temp_dir: Path, cancel: CancelToken | None
) -> Picture | None:
    """Build the front-cover PICTURE block for *thumbnail*, converting if needed.

    YouTube serves WebP, which a FLAC PICTURE block may technically name but
    which players and taggers do not decode, so anything that is not already
    JPEG or PNG goes through ffmpeg once more.  A thumbnail that cannot be read
    or converted is not worth failing a finished download over: the track is
    filed without art and the reason is logged.

    ``width``/``height``/``depth`` are left at zero.  They are advisory in the
    format, no player reads them in preference to the image itself, and filling
    them in would mean either decoding the image (a dependency we do not have)
    or hand-parsing two container formats for a field nothing consumes.
    """
    try:
        data = thumbnail.read_bytes()
    except OSError as exc:
        logger.warning("Could not read thumbnail %s: %s", thumbnail, exc)
        return None

    mime = _sniff_image_mime(data)
    if mime is None:
        converted = temp_dir / (thumbnail.stem + ".cover.jpg")
        try:
            _run_ffmpeg(
                [
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(thumbnail),
                    str(converted),
                ],
                cancel,
                "cover art conversion",
            )
            data = converted.read_bytes()
        except (DownloadError, OSError) as exc:
            if cancel is not None and cancel.is_set():
                raise
            logger.warning(
                "Could not convert thumbnail %s to JPEG, filing the track "
                "without cover art: %s",
                thumbnail,
                exc,
            )
            return None
        mime = _sniff_image_mime(data)
        if mime is None:
            logger.warning(
                "Converted cover art from %s is still not JPEG or PNG, "
                "filing the track without it",
                thumbnail,
            )
            return None

    picture = Picture()
    picture.type = PictureType.COVER_FRONT
    picture.mime = mime
    picture.desc = "Cover"
    picture.data = data
    return picture


def _source_id(info: dict) -> str | None:
    """Return the ``<extractor>:<id>`` provenance string for *info*.

    ``extractor`` is the extractor's ``IE_NAME`` (``youtube``, ``soundcloud``),
    lower-cased here because the id half is case-sensitive and a reader
    comparing two of these should not have to normalise the other half as well.
    Returns ``None`` when either half is missing, which is better than writing
    a tag that only looks like an identifier.
    """
    extractor = info.get("extractor") or info.get("extractor_key")
    source_id = info.get("id")
    if not extractor or not source_id:
        return None
    return f"{str(extractor).lower()}:{source_id}"


def _release_date(info: dict) -> str | None:
    """Return the release date for a ``DATE`` tag, if the source gave one.

    ``upload_date`` is deliberately ignored: when a track was posted to YouTube
    says nothing about when it was released, and a wrong DATE is worse than an
    absent one for anything that sorts a discography.
    """
    release_date = info.get("release_date")
    if isinstance(release_date, str) and len(release_date) == 8 and release_date.isdigit():
        return f"{release_date[:4]}-{release_date[4:6]}-{release_date[6:]}"
    release_year = info.get("release_year")
    if release_year:
        return str(release_year)
    return None


def _write_tags(
    flac_path: Path,
    title: str,
    artist: str,
    album: str,
    info: dict | None,
    source_url: str,
    picture: Picture | None,
) -> None:
    """Write the finished FLAC's Vorbis comments and cover art.

    *artist* and *album* are the resolved, sanitised names the file is filed
    under, so the tags and the folders can never disagree.  ``ARTIST`` and
    ``ALBUMARTIST`` are both set to that one name: the track artist is not
    reliably distinguishable from the album artist in what yt-dlp gives us, and
    Navidrome groups by ``ALBUMARTIST``, so guessing differently would scatter
    an album across two entries.

    ``SOURCEID`` and ``SOURCEURL`` are the provenance pair the library's dedup
    and the later tag-fixing passes key on; they are written here, once, and
    every later writer is required to preserve them.

    ``DATE`` and ``TRACKNUMBER`` appear only when the source actually carried
    them -- an invented track number would reorder an album.

    Raises:
        DownloadError: If the file cannot be read or written as FLAC.
    """
    info = info or {}
    try:
        audio = FLAC(flac_path)
    except Exception as exc:
        raise DownloadError(f"Could not open the converted file as FLAC: {exc}") from exc

    # `-map_metadata -1` should already have left this empty; being explicit
    # means the tag set does not depend on an ffmpeg flag staying correct.
    audio.delete()
    audio.clear_pictures()

    tags: dict[str, str] = {
        "TITLE": title,
        "ARTIST": artist,
        "ALBUMARTIST": artist,
    }
    # A loose Single has no album folder and gets no ``ALBUM`` tag: an invented
    # one is what makes Navidrome show a shelf full of one-track albums.
    if album:
        tags["ALBUM"] = album
    source_id = _source_id(info)
    if source_id:
        tags["SOURCEID"] = source_id
    if source_url:
        tags["SOURCEURL"] = source_url
    date = _release_date(info)
    if date:
        tags["DATE"] = date
    track_number = info.get("track_number")
    if track_number:
        tags["TRACKNUMBER"] = str(track_number)

    for key, value in tags.items():
        audio[key] = value
    if picture is not None:
        audio.add_picture(picture)

    try:
        audio.save()
    except Exception as exc:
        raise DownloadError(f"Could not write tags to the FLAC: {exc}") from exc

    logger.info(
        "Tagged %s: artist=%r, album=%r, source=%r, cover=%s",
        flac_path.name,
        artist,
        album,
        source_id,
        picture is not None,
    )


def _legacy_single_path(output_path: Path, download_path: str) -> Path | None:
    """Where an earlier version would have filed this loose Single.

    Album-less downloads used to land at ``Artist/Unknown Album/<title>.flac``
    and now land at ``Artist/<title>.flac``.  A library written before that
    change still holds the old path, and re-downloading a track the user
    already has is exactly what the duplicate check exists to prevent, so the
    check looks in both places.

    Returns None for anything that is not a loose Single -- a track filed under
    a real album folder has no legacy twin.
    """
    try:
        relative = output_path.relative_to(Path(download_path))
    except ValueError:  # pragma: no cover - get_output_path guarantees this
        return None
    if len(relative.parts) != 2:
        return None
    return output_path.parent / FALLBACK_ALBUM / output_path.name


def _already_in_library(output_path: Path, download_path: str) -> None:
    """Raise if *output_path* is already occupied by a file in the library.

    Never overwriting is the whole rule (domain model: "a download whose target
    filename exists is treated as a duplicate and skipped with a visible
    reason"), so this is checked twice: once before the download starts, which
    is free and saves the bandwidth, and once immediately before the move,
    which is what actually protects the file when two jobs resolve to the same
    name at the same time.  A window of a few microseconds remains between that
    second check and ``os.replace``; closing it properly needs an atomic
    create, which ``os.replace`` cannot do and ``os.link`` cannot do across the
    cases we support.

    The message is the job's visible reason, so it names the path the user would
    look for rather than the absolute one inside the container.

    Raises:
        DownloadError: If the target file exists.
    """
    occupied = output_path
    if not occupied.exists():
        legacy = _legacy_single_path(output_path, download_path)
        if legacy is None or not legacy.exists():
            return
        occupied = legacy
    try:
        relative = occupied.relative_to(Path(download_path)).as_posix()
    except ValueError:  # pragma: no cover - get_output_path guarantees this
        relative = occupied.name
    logger.info("Skipping download: %s is already in the library", relative)
    raise DownloadError(f"{ALREADY_IN_LIBRARY_PREFIX}{relative}")


def _remove_empty_output_dirs(output_path: Path, created_only: frozenset[Path]) -> None:
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


def _missing_output_dirs(output_path: Path, download_path: str | Path) -> frozenset[Path]:
    """Return which of *output_path*'s album/artist directories do not exist yet.

    Sampled before the download starts, so the failure path can tell the
    directories this run would have created from ones that were already there.

    The library root is never one of them, however it started out.  A loose
    Single is ``<root>/Artist/track.flac``, so ``output_path.parent.parent`` is
    the root itself -- and on a first run into an empty volume the root does
    not exist yet, which would put it in the candidate set and let the cleanup
    ``rmdir`` the whole library root after one failed download.
    """
    root = Path(download_path)
    return frozenset(
        directory
        for directory in (output_path.parent, output_path.parent.parent)
        if directory != root and not directory.exists()
    )


@dataclass(frozen=True)
class FiledTrack:
    """A finished FLAC as it was moved into the library.

    ``created_dirs`` are the album/artist directories this run had to create
    for it.  Carrying them alongside the path is what makes the move undoable
    without touching folders that were already there; see :func:`unfile_track`.
    """

    path: Path
    created_dirs: frozenset[Path]


def unfile_track(filed: FiledTrack) -> None:
    """Take a track back out of the library, undoing :func:`download_audio`'s move.

    For the one case where a download that succeeded must not count: the job
    timed out and was failed from the event loop while its thread was still
    moving the file.  Without this the library keeps a track that no queue entry
    admits to and that the user was told had failed.

    A file that cannot be removed is logged rather than raised on: the caller is
    already unwinding a failure.
    """
    try:
        filed.path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Could not remove %s from the library: %s", filed.path, exc)
        return
    logger.info("Removed %s from the library", filed.path)
    _remove_empty_output_dirs(filed.path, filed.created_dirs)


def download_audio(
    job: Job,
    on_progress: Callable[[float], None] | None = None,
    cancel: CancelToken | None = None,
    on_phase: Callable[[str], None] | None = None,
    on_filed: Callable[[FiledTrack], None] | None = None,
    on_target: Callable[[str], None] | None = None,
) -> Path:
    """Run the download pipeline for *job* and return the finished FLAC's path.

    yt-dlp fetches the best audio stream and its thumbnail into the job's
    scratch directory with no postprocessing; ffmpeg converts that stream to
    FLAC; mutagen writes the tags, the source provenance fields and the cover
    art; only then is the file moved into the library.  Nothing unfinished is
    ever written under ``DOWNLOAD_PATH/Artist/Album``.

    Args:
        job: Job instance containing url, artist, and album.  Missing
            title/duration/thumbnail are filled in from yt-dlp metadata.
        on_progress: Optional callback invoked with download percentage (0-100).
        cancel: Optional :class:`CancelToken`.  Cancelling it aborts the
            download at the next yt-dlp progress callback, and terminates a
            running ffmpeg -- killed by this thread if it ignores that.
        on_phase: Optional callback invoked with ``"metadata"`` once the job's
            metadata fields have been filled in, and ``"converting"`` just
            before ffmpeg is started.
        on_target: Optional callback invoked with the library folder this run
            has resolved as its destination (POSIX, relative to
            ``DOWNLOAD_PATH``), before anything is created.  This is the
            authoritative answer -- yt-dlp's artist and album are only known
            here -- and the in-flight guard reads it, so it is published before
            the first mkdir rather than after the download.
        on_filed: Optional callback invoked with a :class:`FiledTrack` the
            moment the finished file joins the library.  It carries the
            directories this run created as well as the path, which is what a
            caller that has to undo the move needs; see :func:`unfile_track`.

    Returns:
        Path to the downloaded FLAC file.

    Raises:
        DownloadError: If any stage fails, the run is cancelled, the output
            path is unsafe, or the target file is already in the library.
    """
    download_path = _get_download_path()
    logger.info("Starting download for job %s (url=%s)", job.id, job.url)
    logger.info("DOWNLOAD_PATH = %s", download_path)

    # Nothing unfinished may be written into the library, so yt-dlp gets a
    # per-job scratch directory and we move the finished file out ourselves.
    # Created before the extraction rather than after it, because the
    # extraction and the download are now one yt-dlp session and that session
    # has to know where it may write from the first call.
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

    # No `postprocessors` and no `postprocessor_hooks`: yt-dlp's only job is to
    # put the best audio stream and the thumbnail on disk.  Everything after
    # that is ours, which is what makes it interruptible.
    opts = {
        **base_opts(),
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        # Everything -- the raw audio, .part/.ytdl scratch, the thumbnail
        # sidecar, the FLAC we encode -- stays inside the job's own temp
        # directory until the finished file is moved.
        "paths": {
            "home": str(temp_dir),
            "temp": str(temp_dir),
            "thumbnail": str(temp_dir),
        },
        "writethumbnail": True,
        "progress_hooks": [_make_progress_hook(on_progress, cancel)],
    }

    # Set inside the session below and read by the failure path outside it.
    output_path: Path | None = None
    would_create: frozenset[Path] = frozenset()

    logger.info("Starting yt-dlp download for job %s ...", job.id)
    try:
        try:
            # The stop button is read once more before anything is fetched.
            _raise_if_cancelled(cancel)

            with ytdl(opts) as ydl:
                # ---- one extraction per track ----
                #
                # This used to be two: a metadata-only probe to resolve the
                # output path, then a second full `extract_info(download=True)`
                # that extracted the same page again.  That doubled every
                # child's request count against the source, which is precisely
                # what earns a 429 on a large playlist.  Now the page is read
                # once and the resulting info dict is handed straight back to
                # yt-dlp to download from -- `process_ie_result` is what
                # `download_with_info_file` uses for exactly this, so it is the
                # supported way in.
                #
                # The old "if the probe failed, try the download anyway"
                # fallback is gone with the second extraction: an extraction
                # that fails now leaves no info dict, and there is nothing left
                # to download from.  It never had much to offer -- the two
                # calls differed only in `skip_download` -- and it cost every
                # job a second page fetch to keep.
                info = ydl.extract_info(job.url, download=False)
                if info is None:
                    logger.error("yt-dlp returned no metadata for job %s", job.id)
                    raise DownloadError("yt-dlp returned no metadata")

                output_path, would_create, title, tag_artist, tag_album = _plan_output(
                    job,
                    info,
                    download_path,
                    on_phase=on_phase,
                    on_target=on_target,
                )

                # Everything between the extraction and here -- the path
                # resolution, the library check -- runs without a hook, so the
                # stop button is read again before any bytes are fetched.
                _raise_if_cancelled(cancel)

                result_info = ydl.process_ie_result(info, download=True)

            source_audio = _downloaded_audio_path(result_info)
            if source_audio is None:
                logger.error(
                    "Download reported success but produced no file for job %s", job.id
                )
                raise DownloadError(
                    f"Output file not found after download: {output_path.name}"
                )

            # ---- converting ----
            # Reported by us, not by a yt-dlp hook, because we are the ones
            # about to start the encoder.
            if on_phase is not None:
                on_phase("converting")

            flac_path = temp_dir / f"{job.id}.flac"
            if source_audio == flac_path:
                # A site that served FLAC directly: the encode still runs (to
                # strip the source's metadata and normalise the compression
                # level), so the input has to move out of the output's way.
                source_audio = source_audio.rename(temp_dir / f"{job.id}.source.flac")
            _convert_to_flac(source_audio, flac_path, cancel)

            thumbnail = _find_thumbnail(temp_dir, job.id)
            if thumbnail is None:
                logger.info("No thumbnail sidecar for job %s; filing without art", job.id)
                picture = None
            else:
                picture = _cover_picture(thumbnail, temp_dir, cancel)

            # The download's own info dict is the better source: it carries the
            # extractor and id fields the provenance tags need, and
            # `process_ie_result` returns the same dict enriched with what the
            # download itself learned.
            tag_info = result_info if isinstance(result_info, dict) else info
            _write_tags(
                flac_path,
                title=title,
                artist=tag_artist,
                album=tag_album,
                info=tag_info,
                source_url=tag_info.get("webpage_url") or job.url,
                picture=picture,
            )

            # A cancel that lands during tagging still has to leave nothing
            # behind, so the last thing checked before the file becomes part of
            # the library is the stop button.
            _raise_if_cancelled(cancel)

            # Checked again: the first check was before the download, and a
            # concurrent job resolving to the same name could have finished in
            # between.
            _already_in_library(output_path, download_path)

            # Created only now: a failed download must not leave an empty
            # Artist/Album pair behind in the library.
            output_path.parent.mkdir(parents=True, exist_ok=True)
            # Same filesystem by construction (.tmp lives inside DOWNLOAD_PATH),
            # so this is a rename.  `os.replace` rather than `shutil.move`
            # because it never moves the file *into* a directory that happens to
            # carry the target name; the "already in library" check above is
            # what stops it replacing anything.
            os.replace(flac_path, output_path)
            if on_filed is not None:
                on_filed(FiledTrack(output_path, would_create))
        except yt_dlp.utils.DownloadCancelled as exc:
            logger.info("Download cancelled for job %s", job.id)
            raise DownloadError(CANCELLED_MESSAGE) from exc
        except yt_dlp.utils.YoutubeDLError as exc:
            logger.error("Download failed for job %s: %s", job.id, exc)
            raise DownloadError(
                _readable_ytdlp_error(exc, f"Download failed: {exc}")
            ) from exc
        except OSError as exc:
            logger.error("Filesystem error during download for job %s: %s", job.id, exc)
            raise DownloadError(f"Download failed: {exc}") from exc
    except Exception:
        # yt-dlp and ffmpeg only ever see the scratch directory, so the album
        # folder can only have been created by the `mkdir` two lines above
        # `os.replace`; this therefore has something to do only when that move
        # itself failed.  The scratch directory, with every partial and temp
        # file in it, is removed by the caller's `finally`.
        if output_path is not None:
            _remove_empty_output_dirs(output_path, would_create)
        raise

    # Only the log line needs the size, and the download is already over: the
    # file is in the library and the caller has been told about it.  A stat that
    # fails here (the file moved or removed by something else in the meantime)
    # must not turn a finished download into a failure, so it costs the size in
    # one log line and nothing more.
    try:
        size_mb = output_path.stat().st_size / (1024 * 1024)
    except OSError as exc:
        logger.info(
            "Download complete for job %s: %s (size unavailable: %s)",
            job.id,
            output_path,
            exc,
        )
    else:
        logger.info(
            "Download complete for job %s: %s (%.2f MB)", job.id, output_path, size_mb
        )
    return output_path


def _plan_output(
    job: Job,
    info: dict,
    download_path: str,
    on_phase: Callable[[str], None] | None,
    on_target: Callable[[str], None] | None,
) -> tuple[Path, frozenset[Path], str, str, str | None]:
    """Decide where this track lands, and refuse it if it is already there.

    Split out of :func:`download_audio` when the two extractions became one:
    everything here happens between "yt-dlp has read the page" and "yt-dlp may
    start fetching bytes", and keeping it inline would have buried that
    boundary inside a hundred lines of a single ``with`` block.

    Returns the output path, the library directories this run would have to
    create, the title the file and tags will carry, and the resolved artist and
    album.

    Raises:
        DownloadError: The URL is a collection, the resolved path is unsafe, or
            the track is already in the library.
    """
    _reject_collections(info, job.url)
    # A child of a bulk parent was enumerated: its title and album came
    # from the collection listing, and yt-dlp is looking at one video.
    # Where the two disagree the enumeration wins -- but the two fields
    # are gated differently.
    #
    # Title: on the parent alone, because every enumerated child's title
    # came from a listing and is what the preview showed.  The enumeration
    # has the track's name ("Horizon"), yt-dlp has the video's ("Glass
    # Beams - 'Horizon' (Official Audio)"); the file and the tag have to
    # be the first.  A row's title may have been truncated by
    # ``probe._cap`` on its way through the preview, and that truncated
    # title is still the row the user ticked, so it still wins.
    enumerated = job.parent_id is not None
    # Album: only where the source read the *release*, which is what
    # ``album_final`` says.  There the answer is whole, empty included --
    # a Single deliberately has no album, and taking yt-dlp's would refile
    # a loose Single into a folder the preview never mentioned.  A row
    # from the flat pass carries no such promise: its empty album is a
    # listing that had none, so yt-dlp still fills it in, as it does for a
    # plain ``POST /download``.
    enumerated_album = job.parent_id is not None and job.album_final
    title = (job.title if enumerated and job.title else None) or (
        info.get("title") or job.title or "Unknown Title"
    )
    ytdlp_artist = _pick_artist(info)
    ytdlp_album = None if enumerated_album else info.get("album")
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

    # The same resolution that chose the folders, so the tags cannot disagree
    # with where the file lands.
    tag_artist, tag_album = resolve_artist_album(
        job.artist, job.album, ytdlp_artist, ytdlp_album
    )

    # Published before the download and every mkdir: the in-flight guard has to
    # know where this job is going before anything of it exists in the library,
    # otherwise a move could rename the folder out from under a download whose
    # destination nobody could name yet.
    if on_target is not None:
        on_target(f"{tag_artist}/{tag_album}" if tag_album else tag_artist)

    logger.info("Output file path: %s", output_path)

    # Cheap check first: nothing has been fetched yet, so a track that is
    # already in the library costs one stat() rather than a whole download.
    _already_in_library(output_path, download_path)

    # Sampled before anything is created so the failure path can tell "we made
    # this empty folder" from "the user's empty folder" (or a concurrent job's).
    would_create = _missing_output_dirs(output_path, download_path)

    return output_path, would_create, title, tag_artist, tag_album
