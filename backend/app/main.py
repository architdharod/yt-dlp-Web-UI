"""FastAPI application entry point for yt-dlp Web UI.

Defines all API routes:
  - GET  /health         -- liveness check
  - POST /download       -- submit a URL for download
  - GET  /queue          -- list all jobs
  - GET  /queue/stream   -- SSE stream of job events
  - POST /queue/{id}/retry -- retry a failed job
"""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from app.downloader import DownloadError, extract_metadata
from app.models import DownloadRequest, HealthResponse, Job, SSEEvent
from app.queue_manager import QueueError, QueueManager

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

# ---------------------------------------------------------------------------
# SSE broadcast infrastructure
# ---------------------------------------------------------------------------

# Connected SSE clients each get their own asyncio.Queue.
# The on_event callback fans out events to all connected clients.
_sse_clients: list[asyncio.Queue[SSEEvent]] = []
_sse_clients_lock = asyncio.Lock()


async def _broadcast_event(event: SSEEvent) -> None:
    """Push an SSE event to every connected client queue."""
    async with _sse_clients_lock:
        for q in _sse_clients:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("SSE client queue full, dropping event")


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — capture the event loop for cross-thread callbacks."""
    global _loop
    _loop = asyncio.get_running_loop()

    # Log effective configuration at startup so misconfigured paths are
    # immediately visible in `docker logs`.
    from app.file_organizer import DEFAULT_DOWNLOAD_PATH

    download_path = os.environ.get("DOWNLOAD_PATH", DEFAULT_DOWNLOAD_PATH)
    max_concurrent = os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2")
    timeout = os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", "900")

    logger.info("=== yt-dlp Web UI backend starting ===")
    logger.info("DOWNLOAD_PATH          = %s", download_path)
    logger.info("MAX_CONCURRENT_DOWNLOADS = %s", max_concurrent)
    logger.info("DOWNLOAD_TIMEOUT_SECONDS = %s", timeout)

    # Verify the download path exists and is writable
    from pathlib import Path

    dp = Path(download_path)
    if not dp.exists():
        logger.warning("DOWNLOAD_PATH %s does NOT exist", download_path)
    elif not os.access(download_path, os.W_OK):
        logger.warning("DOWNLOAD_PATH %s exists but is NOT writable", download_path)
    else:
        logger.info("DOWNLOAD_PATH %s exists and is writable", download_path)

    yield
    _loop = None
    logger.info("=== yt-dlp Web UI backend shutting down ===")


app = FastAPI(title="yt-dlp Web UI", version="0.1.0", lifespan=lifespan)

# Wide-open CORS — internal network tool, not exposed to the public internet
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
    immediately gets title, thumbnail, and duration when possible.
    If metadata extraction fails the job is still enqueued -- the actual
    download phase does its own extraction and can succeed independently.
    """
    # Run the synchronous extract_metadata in a thread executor
    loop = asyncio.get_running_loop()
    title = None
    thumbnail_url = None
    duration = None

    try:
        metadata = await loop.run_in_executor(None, extract_metadata, request.url)
        title = metadata.title
        thumbnail_url = metadata.thumbnail_url
        duration = metadata.duration
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

    queue_manager.add_job(job)
    return job


@app.get("/queue", response_model=list[Job])
async def get_queue() -> list[Job]:
    """Return the full list of jobs with their current state."""
    return queue_manager.get_jobs()


@app.get("/queue/stream")
async def queue_stream():
    """SSE endpoint that emits real-time events for job state changes,
    progress updates, and errors.
    """

    async def event_generator():
        client_queue: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=256)

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
            async with _sse_clients_lock:
                _sse_clients.remove(client_queue)

    return EventSourceResponse(event_generator())


@app.post("/queue/{job_id}/retry", response_model=Job)
async def retry_job(job_id: str) -> Job:
    """Retry a failed job — resets it to 'queued' and re-enters the queue."""
    try:
        job = queue_manager.retry_job(job_id)
    except QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job
