import type { DownloadRequest, Job, SSEEvent } from "./types";

/**
 * Backend base URL — configurable via VITE_API_BASE_URL env var.
 * Defaults to empty string (same-origin) for production behind Traefik,
 * or can be set to e.g. "http://localhost:8000" for local development.
 */
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

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
    const detail = await res.text();
    throw new Error(`Download submission failed: ${detail}`);
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
    const detail = await res.text();
    throw new Error(`Retry failed: ${detail}`);
  }

  return res.json() as Promise<Job>;
}

/**
 * Open an SSE connection to the queue stream endpoint.
 * Calls the provided handler for each parsed SSE event.
 * Returns a function to close the connection.
 */
export function connectQueueStream(
  onEvent: (event: SSEEvent) => void,
  onError?: (error: Event) => void,
): () => void {
  const eventSource = new EventSource(`${API_BASE_URL}/queue/stream`);

  const eventTypes = ["status_change", "progress", "error", "metadata"];
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

  eventSource.onerror = (e) => {
    onError?.(e);
  };

  return () => {
    eventSource.close();
  };
}
