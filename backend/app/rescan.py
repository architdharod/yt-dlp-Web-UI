"""The debounced rescan hook, and the Navidrome and Lidarr clients behind it.

Every file this app writes, moves, or deletes is invisible to the rest of the
homelab until Navidrome and Lidarr look at the disk again.  Rather than call
them once per file -- a ten-track album would mean twenty HTTP requests and, if
the password is wrong, twenty identical complaints -- one hook collects the
changed folders, waits for the writing to stop, and then asks each service
exactly once.

The three pieces:

* :class:`RescanConfig` reads the six environment variables.  A service is
  configured only when all of its variables are set; a half-configured one is
  a startup warning and is then treated as absent, because silently scanning
  with a missing password is worse than not scanning at all.
* :class:`NoticeBoard` holds the failures the user has to act on (bad
  credentials, a non-admin Navidrome user, Lidarr's tag scrubber) and
  de-duplicates them, so a wrong password is one banner rather than one per
  download.
* :class:`RescanHook` is the debounce: ``notify`` accumulates album folders,
  a worker task waits for the quiet period, touches the folders (Navidrome's
  quick scan only re-reads folders whose mtime moved) and then calls both
  services concurrently.

Threading: ``notify`` and the ``NoticeBoard`` mutators touch asyncio primitives
and must run on the event-loop thread.  ``main`` guarantees that by scheduling
the whole ``library_changed`` handler with ``run_coroutine_threadsafe``; a
caller on another thread must do the same (or ``loop.call_soon_threadsafe``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.library import LibraryPathError, get_download_path, validate_library_path
from app.models import Notice

logger = logging.getLogger(__name__)

# Subsonic protocol version and client name sent on every Navidrome call.
# 1.16.1 is what Navidrome itself implements; the client name only shows up in
# Navidrome's logs and its "players" list.
SUBSONIC_API_VERSION = "1.16.1"
SUBSONIC_CLIENT_NAME = "music-for-arr"

# Subsonic error codes worth naming.  Everything else is reported verbatim.
SUBSONIC_WRONG_CREDENTIALS = 40
SUBSONIC_NOT_AUTHORIZED = 50

# Seconds of quiet after the last change before the services are called.
DEFAULT_DEBOUNCE_SECONDS = 5.0

# Timeouts for every outbound call.  Both services are on the same LAN, so a
# connect that takes longer than five seconds is a wrong host.  httpx applies
# the fifteen-second timeout to each read, write and pool acquisition
# separately -- it is an inactivity limit, not a budget for the whole request,
# so a service that keeps dribbling bytes can hold a call open for longer.
# That is acceptable here: the scan itself is asynchronous on both sides, and
# the call runs in a background worker that blocks nothing.
CONNECT_TIMEOUT_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 15.0

# Ceiling on the Lidarr configuration probe, which runs as a background task at
# boot so it can never delay or fail startup, and again after each rescan.
STARTUP_CHECK_TIMEOUT_SECONDS = 20.0

# De-duplication key for the "Lidarr scrubs audio tags" warning.  Named because
# two places need it: the check that raises it, and the same check retracting it
# once the setting has been turned off.
SCRUB_NOTICE_KEY = "scrub-audio-tags"


class RescanFailure(Exception):
    """A service could not be asked to rescan; the message is shown to the user.

    Every message is written to be safe to display: it never carries a password
    or an API key.

    *category* names the *kind* of failure for de-duplication.  Messages that
    embed variable text -- a connection error, an HTTP status -- would otherwise
    look like a brand new problem on every retry and stack up one banner per
    attempt, so those raise sites pass a stable category ("unreachable",
    "http-status", ...).  A failure with fixed wording needs nothing: its
    message is already its own category.
    """

    def __init__(self, message: str, category: str | None = None) -> None:
        super().__init__(message)
        self.category = category if category is not None else message


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NavidromeConfig:
    """Everything needed to call Navidrome's Subsonic API."""

    url: str
    user: str
    password: str


@dataclass(frozen=True)
class LidarrConfig:
    """Everything needed to call Lidarr's v1 API.

    *root_folder* is optional: when unset the client asks Lidarr for its root
    folders and uses the first one.
    """

    url: str
    api_key: str
    root_folder: str | None


@dataclass(frozen=True)
class RescanConfig:
    """The parsed service configuration, plus what was half-filled in."""

    navidrome: NavidromeConfig | None = None
    lidarr: LidarrConfig | None = None
    # One line per partially configured service, logged once at startup.
    warnings: tuple[str, ...] = ()

    @property
    def any_configured(self) -> bool:
        return self.navidrome is not None or self.lidarr is not None


def _env(name: str, env: dict[str, str] | None = None) -> str:
    """Read *name*, treating an empty value as unset.

    docker compose substitutes an unset variable with an empty string, so
    ``os.environ.get(name)`` alone would hand a client an empty URL.
    """
    source = os.environ if env is None else env
    return (source.get(name) or "").strip()


def load_config(env: dict[str, str] | None = None) -> RescanConfig:
    """Build a :class:`RescanConfig` from the environment.

    A service whose variables are all missing is simply off, and says nothing.
    A service missing only some of them is a configuration mistake worth one
    warning -- and is then treated as off, so the hook never sends half a
    request.
    """
    warnings: list[str] = []

    nav_url = _env("NAVIDROME_URL", env).rstrip("/")
    nav_user = _env("NAVIDROME_USER", env)
    nav_password = _env("NAVIDROME_PASSWORD", env)
    navidrome: NavidromeConfig | None = None
    nav_parts = (nav_url, nav_user, nav_password)
    if all(nav_parts):
        navidrome = NavidromeConfig(url=nav_url, user=nav_user, password=nav_password)
    elif any(nav_parts):
        missing = [
            name
            for name, value in (
                ("NAVIDROME_URL", nav_url),
                ("NAVIDROME_USER", nav_user),
                ("NAVIDROME_PASSWORD", nav_password),
            )
            if not value
        ]
        warnings.append(
            "Navidrome is only half configured and will be skipped; "
            f"unset: {', '.join(missing)}"
        )

    lidarr_url = _env("LIDARR_URL", env).rstrip("/")
    lidarr_key = _env("LIDARR_API_KEY", env)
    lidarr_root = _env("LIDARR_ROOT_FOLDER", env) or None
    lidarr: LidarrConfig | None = None
    if lidarr_url and lidarr_key:
        lidarr = LidarrConfig(url=lidarr_url, api_key=lidarr_key, root_folder=lidarr_root)
    elif lidarr_url or lidarr_key:
        missing = [
            name
            for name, value in (
                ("LIDARR_URL", lidarr_url),
                ("LIDARR_API_KEY", lidarr_key),
            )
            if not value
        ]
        warnings.append(
            "Lidarr is only half configured and will be skipped; "
            f"unset: {', '.join(missing)}"
        )

    return RescanConfig(navidrome=navidrome, lidarr=lidarr, warnings=tuple(warnings))


def describe_config(config: RescanConfig) -> list[str]:
    """Startup log lines describing what is configured.  Never a secret."""
    lines = []
    if config.navidrome is None:
        lines.append("NAVIDROME                = not configured, rescans skipped")
    else:
        lines.append(
            f"NAVIDROME                = {config.navidrome.url} "
            f"as {config.navidrome.user}"
        )
    if config.lidarr is None:
        lines.append("LIDARR                   = not configured, rescans skipped")
    else:
        root = config.lidarr.root_folder or "first root folder"
        lines.append(f"LIDARR                   = {config.lidarr.url} ({root})")
    return lines


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------


class NoticeBoard:
    """The set of service problems currently worth showing the user.

    De-duplication is by ``(source, key or message)``: the same complaint from
    the same service while it is still open raises nothing, so a wrong password
    produces one banner however many downloads follow.  The first message wins
    -- a repeat is dropped whole, so the open notice keeps the wording and the
    id it was raised with.  A *different* failure from that service, or the same
    one after :meth:`clear` (which a later success calls), is a new notice with
    a new id -- which is what re-shows a banner the user had dismissed.

    *on_change* is called with the full list of open notices every time that
    list actually changes: a notice raised for the first time, or a clear that
    removed at least one.  Repeats and no-op clears say nothing, so a client
    fed from this callback sees one message per real change.
    """

    def __init__(self, on_change: Callable[[list[Notice]], None] | None = None) -> None:
        self._on_change = on_change
        self._open: dict[tuple[str, str], Notice] = {}
        # Keys that a success must not retract; see raise_notice(sticky=...).
        self._sticky: set[tuple[str, str]] = set()

    def raise_notice(
        self,
        source: str,
        level: str,
        message: str,
        *,
        key: str | None = None,
        sticky: bool = False,
    ) -> Notice | None:
        """Record a failure.  Returns the new notice, or None when it is a repeat.

        *key* is the de-duplication key within *source*, for messages whose text
        varies between otherwise identical failures; it defaults to *message*.

        *sticky* marks a notice that a later success must not clear.  The Lidarr
        tag-scrub warning is the case: it is a setting in Lidarr, not a symptom
        of the call that found it, so a rescan that succeeds says nothing about
        whether the setting was turned off.
        """
        dedup = (source, key or message)
        if dedup in self._open:
            logger.debug("Notice already open for %s: %s", source, message)
            return None

        notice = Notice(
            id=str(uuid.uuid4()),
            level=level,  # type: ignore[arg-type]
            source=source,  # type: ignore[arg-type]
            message=message,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._open[dedup] = notice
        if sticky:
            self._sticky.add(dedup)
        if level == "error":
            logger.error("%s: %s", source, message)
        else:
            logger.warning("%s: %s", source, message)
        self._broadcast()
        return notice

    def clear(self, source: str) -> None:
        """Forget every open non-sticky notice from *source* -- it works again."""
        removed = [
            key
            for key in self._open
            if key[0] == source and key not in self._sticky
        ]
        if not removed:
            return
        for key in removed:
            del self._open[key]
        self._broadcast()

    def retract(self, source: str, key: str) -> None:
        """Drop one notice by key, sticky or not -- the condition behind it is gone.

        :meth:`clear` says "the service works again", which is no argument about
        a sticky notice.  This says "I looked, and the thing this notice
        complains about is no longer true", which is, so it takes the sticky
        entry with it.
        """
        dedup = (source, key)
        self._sticky.discard(dedup)
        if self._open.pop(dedup, None) is None:
            return
        self._broadcast()

    def _broadcast(self) -> None:
        if self._on_change is not None:
            self._on_change(self.open_notices())

    def open_notices(self) -> list[Notice]:
        """The open notices, oldest first."""
        return sorted(self._open.values(), key=lambda notice: notice.created_at)


# ---------------------------------------------------------------------------
# Service clients
# ---------------------------------------------------------------------------


class NavidromeClient:
    """Calls Navidrome's Subsonic ``startScan``.

    Auth is token+salt (``t = md5(password + salt)``) with a fresh salt per
    request, which is what Navidrome's own UI uses and what keeps the password
    out of the URL, the access log, and any proxy in between.
    """

    def __init__(self, config: NavidromeConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client

    def _params(self) -> dict[str, str]:
        salt = secrets.token_hex(8)
        # md5 is not a security choice here: the Subsonic protocol mandates it
        # for the token, so this is interoperability, not hashing for secrecy.
        token = hashlib.md5(
            (self._config.password + salt).encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        return {
            "u": self._config.user,
            "t": token,
            "s": salt,
            "v": SUBSONIC_API_VERSION,
            "c": SUBSONIC_CLIENT_NAME,
            "f": "json",
        }

    async def start_scan(self) -> None:
        """Ask Navidrome for a quick scan.  Raises :class:`RescanFailure`.

        ``fullScan=false`` is the quick scan: Navidrome only opens folders whose
        mtime changed, which is exactly what the hook has just touched.  The
        scan itself runs asynchronously in Navidrome; the response only says it
        was accepted, and that is all this app needs to know.
        """
        url = f"{self._config.url}/rest/startScan"
        params = {**self._params(), "fullScan": "false"}
        try:
            response = await self._client.get(url, params=params)
        # InvalidURL is not an HTTPError: a malformed NAVIDROME_URL (a bad port,
        # say) raises it while httpx is still parsing, and left uncaught it
        # would escape as something no caller knows to turn into a notice.
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise RescanFailure(
                f"Could not reach Navidrome at {self._config.url}: {exc}",
                category="unreachable",
            ) from exc

        # A redirect is a failure, not a success.  Redirects are deliberately
        # not followed: httpx would turn this into a GET at whatever host the
        # Location header names and hand it the Subsonic token, so a 3xx means
        # NAVIDROME_URL has the wrong scheme or base path and needs fixing.
        if response.status_code >= 300:
            raise RescanFailure(
                f"Navidrome answered {response.status_code} for startScan; "
                "check NAVIDROME_URL",
                category="http-status",
            )

        # Subsonic reports its own errors inside a 200, so the body decides.
        try:
            body = response.json()["subsonic-response"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RescanFailure(
                f"Navidrome at {self._config.url} did not answer with a Subsonic "
                "response; check NAVIDROME_URL"
            ) from exc

        if body.get("status") == "ok":
            return

        error = body.get("error") or {}
        code = error.get("code")
        if code == SUBSONIC_WRONG_CREDENTIALS:
            raise RescanFailure(
                "Navidrome rejected the credentials; check NAVIDROME_USER and "
                "NAVIDROME_PASSWORD"
            )
        if code == SUBSONIC_NOT_AUTHORIZED:
            raise RescanFailure(
                f"The Navidrome user {self._config.user!r} is not an admin, and "
                "only admins may start a scan",
                category="not-admin",
            )
        message = error.get("message") or "no message"
        raise RescanFailure(
            f"Navidrome refused the scan (error {code}: {message})",
            category="subsonic-error",
        )


class LidarrClient:
    """Calls Lidarr's ``RescanFolders`` command, and reads its tag settings.

    ``filter=known`` with ``addNewArtists=false`` keeps the rescan to artists
    Lidarr already tracks: this app files music Lidarr did not import, and
    letting a disk scan add every downloaded artist to Lidarr's library would
    be a surprise nobody asked for.
    """

    def __init__(self, config: LidarrConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client
        # Discovered root folder, cached after the first successful lookup:
        # it changes about as often as the Lidarr install itself.
        self._discovered_root: str | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._config.api_key}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One Lidarr call, with connection and auth failures turned into text."""
        url = f"{self._config.url}{path}"
        try:
            response = await self._client.request(
                method, url, headers=self._headers, **kwargs
            )
        # InvalidURL is not an HTTPError; see the note in NavidromeClient.
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise RescanFailure(
                f"Could not reach Lidarr at {self._config.url}: {exc}",
                category="unreachable",
            ) from exc

        if response.status_code in (401, 403):
            raise RescanFailure("Lidarr rejected the API key; check LIDARR_API_KEY")
        # Anything from 300 up failed.  Redirects are deliberately not followed:
        # httpx would replay this as a GET against the host in the Location
        # header, carrying ``X-Api-Key`` off to it, so a 3xx here means
        # LIDARR_URL has the wrong scheme or base path.
        if response.status_code >= 300:
            raise RescanFailure(
                f"Lidarr answered {response.status_code} for {path}; check LIDARR_URL",
                category="http-status",
            )
        return response

    async def _root_folder(self) -> str:
        """The folder to rescan: the configured one, else Lidarr's first."""
        if self._config.root_folder:
            return self._config.root_folder
        if self._discovered_root is not None:
            return self._discovered_root

        response = await self._request("GET", "/api/v1/rootfolder")
        try:
            folders = response.json()
        except ValueError as exc:
            raise RescanFailure(
                "Lidarr did not answer with a root-folder list; check LIDARR_URL"
            ) from exc
        paths = [
            entry["path"]
            for entry in folders
            if isinstance(entry, dict) and entry.get("path")
        ]
        if not paths:
            raise RescanFailure(
                "Lidarr has no root folder configured; add one, or set "
                "LIDARR_ROOT_FOLDER"
            )
        self._discovered_root = paths[0]
        return self._discovered_root

    async def rescan(self) -> None:
        """Ask Lidarr to disk-scan its root folder.  Raises :class:`RescanFailure`."""
        root = await self._root_folder()
        response = await self._request(
            "POST",
            "/api/v1/command",
            json={
                "name": "RescanFolders",
                "folders": [root],
                "filter": "known",
                "addNewArtists": False,
            },
        )

        # A 2xx alone proves nothing: a reverse proxy or a login page can
        # answer 200 with HTML.  Lidarr answers a queued command with the
        # command resource, which always carries its id.
        try:
            body = response.json()
        except ValueError as exc:
            raise RescanFailure(
                "Lidarr did not answer with a command; check LIDARR_URL"
            ) from exc
        if not isinstance(body, dict) or body.get("id") is None:
            raise RescanFailure(
                "Lidarr did not accept the rescan command; check LIDARR_URL"
            )

    async def scrub_warning(self) -> str | None:
        """Return a warning if Lidarr is set to scrub audio tags, else None.

        ``scrubAudioTags`` makes Lidarr strip every tag off a file it has
        matched before rewriting it, which would take this app's ``SOURCEID``
        and ``SOURCEURL`` with it -- and those are what dedup reads.
        """
        response = await self._request("GET", "/api/v1/config/metadataprovider")
        try:
            config = response.json()
        except ValueError as exc:
            raise RescanFailure(
                "Lidarr did not answer with its metadata-provider config; "
                "check LIDARR_URL"
            ) from exc
        if not isinstance(config, dict) or not config.get("scrubAudioTags"):
            return None
        return (
            "Lidarr is set to scrub audio tags. It will strip the SOURCEID and "
            "SOURCEURL tags this app writes from any file it matches. Turn "
            "'Scrub Existing Tags' off in Lidarr under Settings > Metadata."
        )


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


def album_folder(rel_path: str, root: Path) -> str | None:
    """The library folder to touch for a changed path, relative to *root*.

    A track gives its parent: ``Artist/Album/x.flac`` -> ``Artist/Album``, and a
    loose Single ``Artist/x.flac`` -> ``Artist``.  A path that is itself a
    folder (a moved album, a deleted artist) is used as it is.  A file at the
    root gives ``""`` -- the library root, which is always safe to touch.

    Returns None only for a path that cannot be expressed inside the library at
    all, which the caller drops.
    """
    if not rel_path:
        return ""
    try:
        resolved = validate_library_path(rel_path, root)
    except LibraryPathError:
        logger.debug("Ignoring unusable path for rescan: %r", rel_path)
        return None

    if resolved.is_dir():
        folder = rel_path
    else:
        folder = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    return folder


class RescanHook:
    """Debounces library changes into one Navidrome and one Lidarr call.

    Call :meth:`notify` from the event-loop thread after any file change.  The
    first call starts a worker task that waits for ``debounce_seconds`` of
    quiet -- every further notification restarts that wait -- and then runs
    once.  Notifications that arrive while a run is in flight are not lost:
    they land in the pending set and the worker comes straight back round.
    """

    def __init__(
        self,
        config: RescanConfig,
        client: httpx.AsyncClient,
        notices: NoticeBoard,
        root: Path | None = None,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        self._config = config
        self._notices = notices
        self._root = root
        self._debounce = debounce_seconds
        self._navidrome = (
            NavidromeClient(config.navidrome, client)
            if config.navidrome is not None
            else None
        )
        self._lidarr = (
            LidarrClient(config.lidarr, client) if config.lidarr is not None else None
        )

        # Album folders owed a touch on the next run.
        self._pending: set[str] = set()
        # Set by notify, cleared by the worker: "there is work owed".
        self._wake = asyncio.Event()
        # Set while the worker has nothing left to do; what flush() waits on.
        self._idle = asyncio.Event()
        self._idle.set()
        self._worker: asyncio.Task | None = None

    @property
    def notices(self) -> NoticeBoard:
        return self._notices

    def _library_root(self) -> Path:
        # Read late rather than at construction so tests (and the lifespan's
        # own ordering) see the DOWNLOAD_PATH that is current.
        return self._root if self._root is not None else get_download_path()

    def notify(self, paths: list[str]) -> None:
        """Record that *paths* changed and (re)start the quiet period.

        *paths* are POSIX paths relative to ``DOWNLOAD_PATH``, exactly as
        ``library_changed`` carries them.  An empty list still schedules a run:
        it means "something changed but we could not name it", and both
        services should still be told.

        The run touches the changed folders even when neither service is
        configured, on purpose: Navidrome's own scheduled quick scan decides
        what to re-read by folder mtime, and an in-place tag rewrite bumps only
        the file's mtime, so without the touch that scan would walk straight
        past the change.

        Must be called on the event-loop thread; see the module docstring.
        """
        root = self._library_root()
        for path in paths:
            folder = album_folder(path, root)
            if folder is not None:
                self._pending.add(folder)

        self._idle.clear()
        self._wake.set()
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_forever())

    async def _run_forever(self) -> None:
        while True:
            await self._wake.wait()

            # Quiet period: each notification that lands while we sleep sets
            # the event again and buys another full window, so a burst of
            # downloads collapses into one run.
            while True:
                self._wake.clear()
                await asyncio.sleep(self._debounce)
                if not self._wake.is_set():
                    break

            folders = sorted(self._pending)
            self._pending.clear()
            try:
                await self._run(folders)
            except Exception:
                # A run must never kill the worker: the next change has to be
                # able to try again.
                logger.exception("Rescan run failed")

            if not self._wake.is_set():
                self._idle.set()

    async def _run(self, folders: list[str]) -> None:
        """Touch *folders*, then ask both configured services to rescan."""
        await asyncio.to_thread(self._touch_folders, folders)

        calls = []
        if self._navidrome is not None:
            calls.append(self._call("navidrome", self._navidrome.start_scan()))
        if self._lidarr is not None:
            calls.append(self._call("lidarr", self._rescan_and_recheck()))
        if calls:
            await asyncio.gather(*calls)

    async def _rescan_and_recheck(self) -> None:
        """Rescan Lidarr, then re-read the setting behind the scrub warning.

        The warning is sticky, so nothing else would ever take it down; a
        successful rescan is the natural moment to look again, because it proves
        Lidarr is reachable and answering.  The re-check swallows its own
        failures: only ``rescan`` decides whether this call counts as a success.
        """
        assert self._lidarr is not None
        await self._lidarr.rescan()
        await self._check_scrub_setting()

    async def _call(self, source: str, coroutine) -> None:
        """Await one service call, turning a failure into a notice.

        A success clears that service's notices, which is what lets a banner
        the user dismissed come back if the problem returns later.
        """
        try:
            await coroutine
        except RescanFailure as exc:
            self._notices.raise_notice(source, "error", str(exc), key=exc.category)
        except Exception as exc:  # pragma: no cover - defensive
            self._notices.raise_notice(
                source, "error", f"{source} rescan failed: {exc}", key="failed"
            )
        else:
            self._notices.clear(source)
            logger.info("Asked %s to rescan", source)

    def _touch_folders(self, folders: list[str]) -> None:
        """Bump the mtime of every folder that still exists.

        Navidrome's quick scan only re-reads folders whose mtime moved, and an
        in-place tag rewrite changes the file's mtime, not the folder's.  Runs
        in a thread because ``utime`` is a syscall per folder.
        """
        root = self._library_root()
        for folder in folders:
            target = root / folder if folder else root
            try:
                if target.is_dir():
                    os.utime(target)
            except OSError as exc:
                # A folder that vanished between the change and the run is
                # normal (a delete followed by an empty-trash); nothing here is
                # worth a banner.
                logger.debug("Could not touch %s: %s", target, exc)

    async def check_lidarr_config(self) -> None:
        """Check Lidarr's tag settings at startup.  Never raises.

        Bounded and swallowed whole: a Lidarr that is down at boot must not
        stop this app from starting, and the next failing rescan will say so
        anyway.
        """
        await self._check_scrub_setting()

    async def _check_scrub_setting(self) -> None:
        """Bring the tag-scrub warning into line with what Lidarr says now.

        Raises the warning when ``scrubAudioTags`` is on and retracts it when it
        is off, so turning the setting off in Lidarr clears the banner at the
        next rescan rather than leaving it up until a restart.

        Any failure -- a timeout, an unreachable Lidarr, an answer that is not
        the config -- leaves the board exactly as it was.  Not knowing is not
        the same as knowing the setting is off, and a Lidarr this app cannot
        reach is the rescan's complaint to raise, not this one's.
        """
        if self._lidarr is None:
            return
        try:
            warning = await asyncio.wait_for(
                self._lidarr.scrub_warning(), timeout=STARTUP_CHECK_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning("Lidarr did not answer the configuration check")
            return
        except RescanFailure as exc:
            logger.warning("Could not read Lidarr's configuration: %s", exc)
            return
        except Exception as exc:
            # A background probe must never fail boot or shutdown, whatever it
            # manages to raise.
            logger.warning("Lidarr configuration check failed: %s", exc)
            return

        if warning is None:
            self._notices.retract("lidarr", SCRUB_NOTICE_KEY)
            return
        # Sticky: this is a Lidarr setting, not a failure of the call that
        # found it, so a rescan that succeeds later must not clear it.  Only
        # this check, having read the setting as off, retracts it.
        self._notices.raise_notice(
            "lidarr", "warning", warning, key=SCRUB_NOTICE_KEY, sticky=True
        )

    async def flush(self) -> None:
        """Wait until every notified change has been through a run.

        For tests, and for anything that wants to be sure the services have
        been told before it moves on.
        """
        if self._worker is None:
            return
        while not self._idle.is_set():
            await self._idle.wait()

    async def aclose(self) -> None:
        """Stop the worker.  Pending changes are dropped, not awaited.

        Shutdown is not the moment to hold the process open for a debounce
        window and two HTTP calls; whatever was pending will be picked up by
        the next scan either service runs on its own.
        """
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


def build_http_client() -> httpx.AsyncClient:
    """The one shared client for both services.

    One client for the process, so connections to Navidrome and Lidarr are
    pooled rather than re-established per download.  Both are LAN neighbours,
    hence the short timeouts.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
        # Never follow a redirect: httpx would replay a POST as a GET and carry
        # ``X-Api-Key`` or the Subsonic token to whatever host the Location
        # header names.  The clients treat a 3xx as a misconfigured URL instead.
        follow_redirects=False,
    )
