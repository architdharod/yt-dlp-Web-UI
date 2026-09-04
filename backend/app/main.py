"""FastAPI application entry point for yt-dlp Web UI.

Defines all API routes:
  - GET  /health         -- liveness check
  - POST /download       -- submit a URL for download
  - GET  /queue          -- list in-flight and errored jobs
  - GET  /queue/stream   -- SSE stream of job events
  - POST /queue/{id}/retry   -- retry a failed job
  - POST /queue/{id}/cancel  -- stop a queued or running job
  - POST /queue/{id}/dismiss -- forget an errored job
"""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import yt_dlp.version
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from app.downloader import DownloadError, extract_metadata
from app.file_organizer import DEFAULT_DOWNLOAD_PATH
from app.job_store import JobStore, get_data_path, get_db_path
from app.models import DownloadRequest, HealthResponse, Job, SSEEvent
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


def _on_queue_event(event: SSEEvent) -> None:
    """Synchronous callback for QueueManager — schedules the async broadcast.

    This may be called from a background thread (e.g. yt-dlp progress hooks
    running inside ``run_in_executor``), so we use
    ``asyncio.run_coroutine_threadsafe`` which is safe to call from any thread,
    rather than ``loop.create_task`` which only works from the event-loop thread.
    """
    if _loop is None or _loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(_broadcast_event(event), _loop)
    except RuntimeError:
        # Loop was closed between the check and the call — nothing to do.
        pass


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

    logger.info("=== yt-dlp Web UI backend starting ===")
    logger.info("yt-dlp version           = %s", yt_dlp.version.__version__)
    logger.info("DOWNLOAD_PATH            = %s", download_path)
    logger.info("DATA_PATH                = %s", data_path)
    logger.info("MAX_CONCURRENT_DOWNLOADS = %s", max_concurrent)
    logger.info("DOWNLOAD_TIMEOUT_SECONDS = %s", timeout)

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

    try:
        yield
    finally:
        sweep_task.cancel()
        try:
            await sweep_task
        except asyncio.CancelledError:
            pass
        store.close()
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

    try:
        metadata = await asyncio.wait_for(
            loop.run_in_executor(None, extract_metadata, request.url),
            timeout=METADATA_TIMEOUT_SECONDS,
        )
        title = metadata.title
        thumbnail_url = metadata.thumbnail_url
        duration = metadata.duration
    except asyncio.TimeoutError:
        logger.warning(
            "Metadata extraction timed out after %ss, enqueuing job anyway",
            METADATA_TIMEOUT_SECONDS,
        )
    except DownloadError as exc:
        logger.warning("Metadata extraction failed, enqueuing job anyway: %s", exc)

    job = Job(
        id=str(uuid.uuid4()),
        url=request.url,
        title=title,
        thumbnail_url=thumbnail_url,
        duration=duration,
        artist=request.artist,
        album=request.album,
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
