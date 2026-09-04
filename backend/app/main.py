"""FastAPI application entry point for yt-dlp Web UI.

Defines all API routes:
  - GET  /health         -- liveness check
  - POST /download       -- submit a URL for download
  - GET  /library        -- the DOWNLOAD_PATH tree as artists, albums, tracks
  - GET  /library/cover  -- cover art for one album
  - POST /library/move   -- move tracks or an album, or rename an artist
  - GET  /notices        -- open Navidrome/Lidarr problems worth showing
  - GET  /queue          -- list in-flight and errored jobs
  - GET  /queue/stream   -- SSE stream of job events
  - POST /queue/{id}/retry   -- retry a failed job
  - POST /queue/{id}/cancel  -- stop a queued or running job
  - POST /queue/{id}/dismiss -- forget an errored job
"""

import asyncio
import concurrent.futures
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import yt_dlp.version
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from app.downloader import DownloadError, extract_metadata
from app.file_organizer import DEFAULT_DOWNLOAD_PATH, resolve_artist_album
from app.job_store import JobStore, get_data_path, get_db_path
from app.library import (
    LibraryNotFound,
    LibraryPathError,
    get_album_cover,
    get_download_path,
    invalidate as library_invalidate,
    scan_library,
)
from app.mover import LIBRARY_WRITE_LOCK, LibraryConflict, move_library_entry
from app.models import (
    DownloadRequest,
    HealthResponse,
    Job,
    LibraryMoveRequest,
    LibraryMoveResponse,
    LibraryResponse,
    MovedPath,
    Notice,
    SSEEvent,
)
from app.rescan import (
    NoticeBoard,
    RescanHook,
    build_http_client,
    describe_config,
    load_config,
)
from app.queue_manager import (
    DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    RETENTION_DAYS,
    JobNotFound,
    QueueError,
    QueueManager,
)

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
# Configure root logger so all app.* loggers emit structured, timestamped
# output visible in `docker logs`.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

# How long POST /download may spend probing a URL for metadata before it
# gives up and enqueues the job without it.  Keeps the request well inside
# nginx's 60 s proxy_read_timeout.
METADATA_TIMEOUT_SECONDS = 20

# How long a browser may keep a cover it fetched with an explicit ?v= version.
# The version is a change stamp over the album folder, its sidecar images and
# its audio files, so the URL changes whenever the art could have changed and
# the entry it replaces is never asked for again -- which is exactly what
# "immutable" promises.
COVER_MAX_AGE_SECONDS = 31536000

# Events buffered per connected SSE client before the oldest is dropped.
SSE_CLIENT_QUEUE_SIZE = 256

# How often the retention sweep runs while the app is up.  The sweep also runs
# once at boot, so a box that is restarted more often than this still prunes.
SWEEP_INTERVAL_SECONDS = 24 * 60 * 60

# ---------------------------------------------------------------------------
# SSE broadcast infrastructure
# ---------------------------------------------------------------------------

# Connected SSE clients each get their own asyncio.Queue.
# The on_event callback fans out events to all connected clients.
_sse_clients: list[asyncio.Queue[SSEEvent]] = []
_sse_clients_lock = asyncio.Lock()


async def _broadcast_event(event: SSEEvent) -> None:
    """Push an SSE event to every connected client queue.

    A slow client's queue fills up during a download (yt-dlp emits progress
    far faster than a throttled browser tab consumes it).  Dropping the new
    event would lose the terminal ``done``/``error`` event, so the OLDEST
    queued event is discarded instead: stale progress is worthless, the
    latest event never is.
    """
    async with _sse_clients_lock:
        for q in _sse_clients:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Expected under load, hence debug rather than warning.
                logger.debug("SSE client queue full, dropping oldest event")
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass


# Reference to the main event loop, captured during app startup so that
# callbacks invoked from background threads (e.g. yt-dlp progress hooks
# running inside run_in_executor) can safely schedule async work.
_loop: asyncio.AbstractEventLoop | None = None


async def _handle_event(event: SSEEvent) -> None:
    """Fan an event out to the SSE clients, and act on it where we must.

    ``library_changed`` is the single announcement that files under
    ``DOWNLOAD_PATH`` moved on, so it is also where the rescan hook is fed:
    every later phase that moves, trashes, restores, or re-tags a file already
    ends by calling ``QueueManager.emit_library_changed``, and gets the
    Navidrome and Lidarr rescan for free by doing so.
    """
    await _broadcast_event(event)

    if event.event == "library_changed" and rescan_hook is not None:
        paths = event.data.get("paths") or []
        # Runs on the event-loop thread because this coroutine was scheduled
        # onto it, which is what RescanHook.notify requires.
        rescan_hook.notify([path for path in paths if isinstance(path, str)])


def _on_queue_event(event: SSEEvent) -> None:
    """Synchronous callback for QueueManager — schedules the async handling.

    This may be called from a background thread (e.g. yt-dlp progress hooks
    running inside ``run_in_executor``), so we use
    ``asyncio.run_coroutine_threadsafe`` which is safe to call from any thread,
    rather than ``loop.create_task`` which only works from the event-loop thread.
    """
    if _loop is None or _loop.is_closed():
        return
    try:
        future = asyncio.run_coroutine_threadsafe(_handle_event(event), _loop)
    except RuntimeError:
        # Loop was closed between the check and the call — nothing to do.
        return
    # Nothing awaits this future, so without a callback an exception inside
    # ``_handle_event`` -- a rescan hook that raised, a broadcast that failed --
    # would be swallowed by asyncio and surface only as "exception was never
    # retrieved" at garbage-collection time, if at all.
    future.add_done_callback(_log_event_failure)


def _log_event_failure(future: concurrent.futures.Future) -> None:
    """Log whatever handling a queue event raised.

    Cancellation is not a failure: the loop shutting down cancels what it has
    not run yet, and that is the normal end of a session.
    """
    try:
        future.result()
    except concurrent.futures.CancelledError:
        pass
    except Exception:
        logger.exception("Handling a queue event failed")


def _on_notices_changed(notices: list[Notice]) -> None:
    """Push the whole open notice list to every connected client.

    The event carries the full list rather than the one notice that changed, so
    a clear is expressible at all: "Navidrome works again" has no notice of its
    own to send.  The payload is exactly what ``GET /notices`` returns, which
    lets a client replace its state outright instead of reconciling.
    """
    _on_queue_event(
        SSEEvent(
            event="notices",
            job_id=None,
            data={"notices": [notice.model_dump(mode="json") for notice in notices]},
        )
    )


# The board of open Navidrome/Lidarr problems, and the hook that fills it.
# The board is a module singleton so GET /notices can read it; the hook needs a
# running event loop and an HTTP client, so it is built in the lifespan.
notice_board = NoticeBoard(on_change=_on_notices_changed)
rescan_hook: RescanHook | None = None


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------

# QueueManager singleton — created here so routes can reference it.
queue_manager = QueueManager(on_event=_on_queue_event)


async def _sweep_periodically() -> None:
    """Run the queue's retention sweep once a day until cancelled."""
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            removed = queue_manager.sweep()
            logger.info("Daily retention sweep removed %d finished job(s)", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A sweep failure must not kill the loop; try again tomorrow.
            logger.exception("Retention sweep failed")


def _validate_directory(name: str, path: str) -> None:
    """Fail fast when a configured path is missing, not a directory, or read-only.

    Both directories are bind mounts or volumes in production; getting one
    wrong would otherwise turn every job (or every write of the job database)
    into a permission-denied traceback at the worst possible moment.
    """
    if not Path(path).is_dir():
        raise RuntimeError(
            f"{name} {path!r} does not exist or is not a directory. "
            "Create it (and make it writable by the container user) or set "
            f"the {name} environment variable to an existing directory."
        )
    if not os.access(path, os.W_OK):
        raise RuntimeError(
            f"{name} {path!r} is not writable by this process. "
            f"Fix its ownership/permissions or set the {name} "
            "environment variable to a writable directory."
        )
    logger.info("%s %s exists and is writable", name, path)


def _cors_origins() -> list[str]:
    """Parse the CORS_ORIGINS env var into a list of allowed origins.

    In production the frontend is served by nginx which proxies /api to this
    backend, so requests are same-origin and no CORS headers are needed at
    all.  The variable exists for `vite dev` (http://localhost:5173) talking
    to a backend running directly on the host.
    """
    raw = os.environ.get("CORS_ORIGINS") or ""
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan.

    Captures the event loop for cross-thread callbacks, validates the two
    configured directories, opens the job database, restores the queue from it,
    and starts the retention sweep.  Everything is torn down in reverse on
    shutdown.
    """
    global _loop
    _loop = asyncio.get_running_loop()

    # docker compose substitutes an unset variable with an empty string, so
    # `or` rather than a dict default.
    download_path = os.environ.get("DOWNLOAD_PATH") or DEFAULT_DOWNLOAD_PATH
    data_path = get_data_path()
    max_concurrent = os.environ.get("MAX_CONCURRENT_DOWNLOADS") or str(
        DEFAULT_MAX_CONCURRENT_DOWNLOADS
    )
    timeout = os.environ.get("DOWNLOAD_TIMEOUT_SECONDS") or str(
        DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    )

    rescan_config = load_config()

    logger.info("=== yt-dlp Web UI backend starting ===")
    logger.info("yt-dlp version           = %s", yt_dlp.version.__version__)
    logger.info("DOWNLOAD_PATH            = %s", download_path)
    logger.info("DATA_PATH                = %s", data_path)
    logger.info("MAX_CONCURRENT_DOWNLOADS = %s", max_concurrent)
    logger.info("DOWNLOAD_TIMEOUT_SECONDS = %s", timeout)
    for line in describe_config(rescan_config):
        logger.info("%s", line)
    for warning in rescan_config.warnings:
        logger.warning("%s", warning)

    _validate_directory("DOWNLOAD_PATH", download_path)
    _validate_directory("DATA_PATH", data_path)

    store = JobStore(get_db_path(data_path))
    queue_manager.attach_store(store)

    # Restore before the app serves anything, so the duplicate check and
    # GET /queue both see the previous run's jobs from the first request on.
    restored = queue_manager.restore_from_store()
    logger.info("Restored %d job(s) from %s", len(restored), get_db_path(data_path))
    swept = queue_manager.sweep()
    logger.info(
        "Boot retention sweep removed %d job(s) finished more than %d days ago",
        swept,
        RETENTION_DAYS,
    )
    sweep_task = asyncio.create_task(_sweep_periodically())

    global rescan_hook
    http_client = build_http_client()
    rescan_hook = RescanHook(rescan_config, http_client, notice_board)
    # Lidarr's tag-scrubbing setting is worth a banner, but reading it must
    # never delay or fail boot, so it goes out as a background task with its
    # own timeout inside.
    startup_check = asyncio.create_task(rescan_hook.check_lidarr_config())

    try:
        yield
    finally:
        # Every step below is independent on purpose.  These are background
        # tasks and network clients; any of them can fail on the way down, and
        # a failure here used to strand the ones after it -- most importantly
        # store.close(), which is the one that has to happen.
        for name, task in (("startup check", startup_check), ("retention sweep", sweep_task)):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("The %s task failed", name)

        try:
            await rescan_hook.aclose()
        except Exception:
            logger.exception("Could not stop the rescan hook")
        finally:
            rescan_hook = None

        try:
            await http_client.aclose()
        except Exception:
            logger.exception("Could not close the HTTP client")
        finally:
            try:
                store.close()
            except Exception:
                logger.exception("Could not close the job store")
            finally:
                _loop = None
                logger.info("=== yt-dlp Web UI backend shutting down ===")


app = FastAPI(title="yt-dlp Web UI", version="0.1.0", lifespan=lifespan)

# No CORS middleware by default: in production the browser talks to nginx,
# which serves the UI and proxies /api to this backend, so every request is
# same-origin.  Set CORS_ORIGINS (comma-separated) only when running the vite
# dev server against a local backend.  Credentials stay off — there is no
# auth, and echoing arbitrary origins with credentials is the worst case.
_configured_cors_origins = _cors_origins()
if _configured_cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_configured_cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="yt-dlp-web-ui-backend")


@app.get("/notices", response_model=list[Notice])
async def get_notices() -> list[Notice]:
    """The Navidrome and Lidarr problems currently worth showing the user.

    The ``notices`` SSE event carries this same list every time it changes,
    but a client that connects after
    the backend raised one -- the Lidarr tag-scrub warning is raised seconds
    after boot -- would never hear about it, hence this endpoint.
    """
    return notice_board.open_notices()


@app.post("/download", response_model=Job)
async def submit_download(request: DownloadRequest) -> Job:
    """Accept a download request, attempt to extract metadata, enqueue
    the job, and return it with status 'queued'.

    Metadata extraction (1-3 s) happens during this request so the client
    immediately gets title, thumbnail, and duration when possible.  It is
    bounded by ``METADATA_TIMEOUT_SECONDS``; on timeout or failure the job
    is still enqueued -- the download phase does its own extraction and can
    succeed independently.
    """
    loop = asyncio.get_running_loop()
    title = None
    thumbnail_url = None
    duration = None
    ytdlp_artist = None
    ytdlp_album = None
    # Whether the probe really answered, as opposed to timing out or failing.
    probed = False

    try:
        metadata = await asyncio.wait_for(
            loop.run_in_executor(None, extract_metadata, request.url),
            timeout=METADATA_TIMEOUT_SECONDS,
        )
        title = metadata.title
        thumbnail_url = metadata.thumbnail_url
        duration = metadata.duration
        ytdlp_artist = metadata.artist
        ytdlp_album = metadata.album
        probed = True
    except asyncio.TimeoutError:
        logger.warning(
            "Metadata extraction timed out after %ss, enqueuing job anyway",
            METADATA_TIMEOUT_SECONDS,
        )
    except DownloadError as exc:
        logger.warning("Metadata extraction failed, enqueuing job anyway: %s", exc)

    # Resolved here, from the same function the downloader uses, so the queue
    # can say where this job is going the moment it is queued.  The download
    # thread republishes it once it has yt-dlp's own answer; until then this is
    # the best one available, and it is what the move guard reads.  A blank
    # album is a loose Single (file_organizer.NO_ALBUM), which is the artist
    # folder and nothing below it.
    artist, album = resolve_artist_album(
        request.artist, request.album, ytdlp_artist, ytdlp_album
    )
    # When the probe never returned and the user named no artist, the folder
    # above is not a destination at all -- it is ``Unknown Artist``, the
    # fallback, while the download thread will file the track under whatever
    # yt-dlp says once it has looked.  Recorded as a guess so the move guard
    # treats this job as "has not said where it is going" rather than pinning
    # a folder that will almost certainly not be the one.
    target_guessed = not (probed or bool(request.artist and request.artist.strip()))

    job = Job(
        id=str(uuid.uuid4()),
        url=request.url,
        title=title,
        thumbnail_url=thumbnail_url,
        duration=duration,
        artist=request.artist,
        album=request.album,
        target_dir=f"{artist}/{album}" if album else artist,
        target_guessed=target_guessed,
    )

    try:
        queue_manager.add_job(job)
    except QueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job


@app.get("/queue", response_model=list[Job])
async def get_queue() -> list[Job]:
    """Return the in-flight and errored jobs with their current state.

    Done and cancelled jobs are omitted: the queue view is about what still
    needs attention.  They live on in the database until the retention sweep
    drops them.
    """
    return queue_manager.get_jobs()


@app.get("/queue/stream")
async def queue_stream():
    """SSE endpoint that emits real-time events for job state changes,
    progress updates, and errors.
    """

    async def event_generator():
        client_queue: asyncio.Queue[SSEEvent] = asyncio.Queue(
            maxsize=SSE_CLIENT_QUEUE_SIZE
        )

        async with _sse_clients_lock:
            _sse_clients.append(client_queue)

        try:
            while True:
                event = await client_queue.get()
                yield {
                    "event": event.event,
                    "data": event.model_dump_json(),
                }
        except asyncio.CancelledError:
            pass
        finally:
            # Deliberately lock-free: sse-starlette cancels this generator on
            # client disconnect, and awaiting a contended lock while being
            # cancelled re-raises CancelledError before the removal runs,
            # leaking the queue forever.  list.remove is atomic w.r.t. the
            # event loop, which is the only thread that mutates this list.
            try:
                _sse_clients.remove(client_queue)
            except ValueError:
                pass

    return EventSourceResponse(event_generator())


@app.post("/queue/{job_id}/retry", response_model=Job)
async def retry_job(job_id: str) -> Job:
    """Retry a failed job — resets it to 'queued' and re-enters the queue.

    Only errored jobs can be retried; a cancelled job is resubmitted as a new
    download rather than revived, so that its duplicate check runs again.
    """
    try:
        job = queue_manager.retry_job(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job


@app.post("/queue/{job_id}/cancel", response_model=Job)
async def cancel_job(job_id: str) -> Job:
    """Stop a queued or running job.

    Returns the job immediately.  A queued job comes back already
    ``cancelled``; a running one comes back in the state it is still in and
    reaches ``cancelled`` over the SSE stream a moment later, once its thread
    has stopped ffmpeg and removed its temp files.

    A job that has already finished, failed or been cancelled is a **400**: the
    client is acting on a view that is out of date, and pretending the call
    worked would tell the user a finished track was not filed.
    """
    try:
        job = queue_manager.cancel_job(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job


@app.post("/queue/{job_id}/dismiss", status_code=204)
async def dismiss_job(job_id: str) -> None:
    """Forget an errored job: its row and its queue entry are deleted.

    Errored jobs are the only ones the retention sweep never drops, so this is
    how they leave.  404 for an unknown job, 400 for one that is not in
    ``error``.
    """
    try:
        queue_manager.dismiss_job(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


@app.get("/library", response_model=LibraryResponse)
async def get_library(response: Response) -> LibraryResponse:
    """Return the whole library tree: artists, their albums, and their singles.

    The filesystem is the only source of truth, so this is a live scan -- served
    from the in-memory tag cache, which makes a repeat call with nothing changed
    a stat per file and no tag parses at all.  The walk is blocking, hence the
    thread; ``no-store`` because a stale tree is worse than a second scan.
    """
    payload = await asyncio.to_thread(scan_library, get_download_path())
    response.headers["Cache-Control"] = "no-store"
    return LibraryResponse.model_validate(payload)


def _if_none_match_hits(header: str | None, etag: str) -> bool:
    """Whether *header* lists *etag*, in either the strong or the ``W/`` form.

    RFC 9110 compares ``If-None-Match`` weakly, so ``W/"x"`` and ``"x"`` are the
    same validator here; the prefix only matters for range requests, which this
    endpoint does not serve.
    """
    if not header:
        return False
    if header.strip() == "*":
        return True
    for candidate in header.split(","):
        value = candidate.strip()
        if value.startswith("W/"):
            value = value[2:].strip()
        if value == etag:
            return True
    return False


@app.get("/library/cover")
async def get_library_cover(
    request: Request,
    path: str = Query(
        "",
        description="Album path relative to DOWNLOAD_PATH; empty means the synthetic bucket",
    ),
    v: str | None = Query(
        None,
        description="Cover version (the album's cover_version); makes the response cacheable",
    ),
) -> Response:
    """Serve an album's cover: embedded picture, else `cover.jpg`, else a placeholder.

    An artist path, the synthetic root bucket, and an album with no art anywhere
    all get the generated SVG placeholder -- artist tiles ask for their
    ``cover_album_path`` instead, so there is nothing to look up for a bare
    artist folder.

    ``v`` is the album's ``cover_version``.  When the frontend passes it the
    URL is unique to that version of the art, so the response may be cached
    forever; without it the browser must revalidate, because the same URL will
    later serve different bytes.
    """
    try:
        cover = await asyncio.to_thread(
            get_album_cover, path, get_download_path(), Path(get_data_path())
        )
    except LibraryPathError as exc:
        # str(exc) is written to never contain a filesystem path.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LibraryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    cache_control = (
        f"public, max-age={COVER_MAX_AGE_SECONDS}, immutable" if v else "no-cache"
    )
    # nosniff on every response: the body is only ever bytes we have sniffed
    # ourselves (or our own SVG), and the header stops a browser from
    # second-guessing that and rendering a cover as something executable.
    headers = {
        "Cache-Control": cache_control,
        "ETag": cover.etag,
        "X-Content-Type-Options": "nosniff",
    }
    if _if_none_match_hits(request.headers.get("if-none-match"), cover.etag):
        # The no-cache path above still costs a request per cover; answering it
        # with 304 means it costs headers rather than an image.
        return Response(status_code=304, headers=headers)
    return Response(content=cover.data, media_type=cover.content_type, headers=headers)


@app.post("/library/move", response_model=LibraryMoveResponse)
async def move_library_path(request: LibraryMoveRequest) -> LibraryMoveResponse:
    """Move tracks, move an album to another artist, or rename an artist.

    All-or-nothing: a target that is occupied refuses the whole request with
    409 and the list of conflicting paths, and nothing on disk has moved. The
    same 409 covers the in-flight guard -- a download already aiming at one of
    the folders involved -- because both mean "not now, here is what is in the
    way", and the dialog shows them the same.

    The filesystem work runs in a thread under one library-wide lock, so a
    second move (or, from phase 7, a delete) can never slip between another's
    collision check and its renames.
    """
    async with LIBRARY_WRITE_LOCK:
        # Read inside the lock, on the event-loop thread where the queue's
        # dicts are only ever mutated: a snapshot taken before the lock could
        # have been overtaken by a job that resolved its destination while an
        # earlier move held it, and this move would then not see it at all.
        in_flight = queue_manager.in_flight_library_targets()

        try:
            outcome = await asyncio.to_thread(
                move_library_entry,
                root=get_download_path(),
                artist=request.artist,
                album=request.album,
                path=request.path,
                paths=request.paths,
                in_flight=in_flight.targets,
                unresolved=in_flight.unresolved,
                unresolved_jobs=in_flight.unresolved_jobs,
            )
        except LibraryPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LibraryNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LibraryConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={"message": exc.message, "conflicts": exc.conflicts},
            ) from exc
        except OSError as exc:
            logger.exception("Move failed")
            raise HTTPException(
                status_code=500, detail=f"The move failed: {exc.strerror or exc}"
            ) from exc

        # Inside the lock: the next writer's collision check must not run
        # against a scan cache that still describes the tree we just changed.
        # A rename leaves every file's size and mtime alone, so the mtime-keyed
        # cache cannot notice this on its own.
        if outcome.changed:
            await asyncio.to_thread(library_invalidate)

    if outcome.changed:
        # One event for the whole move: it invalidates the library query in
        # every open tab and feeds the debounced Navidrome/Lidarr rescan with
        # the folders on both ends.
        queue_manager.emit_library_changed(outcome.changed)

    return LibraryMoveResponse(
        moved=[MovedPath(source=item["from"], target=item["to"]) for item in outcome.moved],
        removed=outcome.removed,
        destination=outcome.destination,
    )
