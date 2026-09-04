import type { DownloadRequest, Job, SSEEvent } from "./types";

/**
 * Backend base URL — configurable via VITE_API_BASE_URL env var.
 * Defaults to empty string (same-origin) — in production nginx serves the
 * built app and proxies "/api" to the backend, so the app is same-origin.
 * For local development against a standalone backend set it to e.g.
 * "http://localhost:8000".
 */
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

/**
 * Extract a human-readable message from an error response body.
 *
 * FastAPI returns `{"detail": "..."}` for HTTPException and
 * `{"detail": [{"msg": "...", ...}, ...]}` for request validation errors (422).
 */
async function parseErrorDetail(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === "string" && detail.trim().length > 0) {
        return detail;
      }
      if (Array.isArray(detail)) {
        const messages = detail
          .map((item) =>
            item && typeof item === "object" && typeof (item as { msg?: unknown }).msg === "string"
              ? ((item as { msg: string }).msg)
              : null,
          )
          .filter((msg): msg is string => msg !== null);
        if (messages.length > 0) return messages.join("; ");
      }
    }
  } catch {
    // Body was not JSON — fall through to the status text.
  }
  return res.statusText;
}

/**
 * Submit a URL for download. Synchronously extracts metadata on the backend
 * and returns the newly created job with status "queued".
 */
export async function submitDownload(request: DownloadRequest): Promise<Job> {
  const res = await fetch(`${API_BASE_URL}/download`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });

  if (!res.ok) {
    throw new Error(await parseErrorDetail(res));
  }

  return res.json() as Promise<Job>;
}

/**
 * Fetch the current queue of all jobs.
 */
export async function getQueue(): Promise<Job[]> {
  const res = await fetch(`${API_BASE_URL}/queue`);

  if (!res.ok) {
    throw new Error(`Failed to fetch queue: ${res.statusText}`);
  }

  return res.json() as Promise<Job[]>;
}

/**
 * Retry a failed job by ID. Resets it to "queued" and re-enqueues.
 */
export async function retryJob(jobId: string): Promise<Job> {
  const res = await fetch(`${API_BASE_URL}/queue/${jobId}/retry`, {
    method: "POST",
  });

  if (!res.ok) {
    throw new Error(await parseErrorDetail(res));
  }

  return res.json() as Promise<Job>;
}

/**
 * Cancel a queued or running job.
 *
 * A queued job comes back already "cancelled"; a running one comes back in the
 * state it is still in and reaches "cancelled" over the SSE stream once its
 * thread has stopped ffmpeg and cleaned up. 400 when the job already finished.
 */
export async function cancelJob(jobId: string): Promise<Job> {
  const res = await fetch(`${API_BASE_URL}/queue/${jobId}/cancel`, {
    method: "POST",
  });

  if (!res.ok) {
    throw new Error(await parseErrorDetail(res));
  }

  return res.json() as Promise<Job>;
}

/**
 * Dismiss an errored job: the backend deletes it outright, so there is no job
 * left to return and the response is a 204 with no body.
 */
export async function dismissJob(jobId: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/queue/${jobId}/dismiss`, {
    method: "POST",
  });

  if (!res.ok) {
    throw new Error(await parseErrorDetail(res));
  }
}

/**
 * Open an SSE connection to the queue stream endpoint.
 * Calls the provided handler for each parsed SSE event.
 * Returns a function to close the connection.
 */
export function connectQueueStream(
  onEvent: (event: SSEEvent) => void,
  onError?: (error: Event) => void,
  onOpen?: () => void,
): () => void {
  const eventSource = new EventSource(`${API_BASE_URL}/queue/stream`);

  // Every event type the backend emits. EventSource only delivers a named
  // event to a listener registered for that name, so anything missing here is
  // silently dropped. "metadata" carries the title/duration/thumbnail the
  // downloader backfilled and is merged like any other job snapshot.
  const eventTypes = [
    "status_change",
    "progress",
    "error",
    "metadata",
    "library_changed",
  ];
  for (const eventType of eventTypes) {
    eventSource.addEventListener(eventType, (e: MessageEvent) => {
      try {
        const parsed = JSON.parse(e.data) as SSEEvent;
        onEvent(parsed);
      } catch {
        // Ignore malformed events
      }
    });
  }

  eventSource.onopen = () => {
    onOpen?.();
  };

  eventSource.onerror = (e) => {
    onError?.(e);
  };

  return () => {
    eventSource.close();
  };
}
