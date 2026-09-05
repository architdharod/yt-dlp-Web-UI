import type {
  DownloadRequest,
  Job,
  LibraryAlbum,
  LibraryDeleteRequest,
  LibraryDeleteResponse,
  LibraryMoveRequest,
  LibraryMoveResponse,
  LibraryResponse,
  LibraryTagRequest,
  Notice,
  SSEEvent,
  TrashEmptyResponse,
  TrashResponse,
  TrashRestoreRequest,
  TrashRestoreResponse,
} from "./types";

/**
 * Backend base URL — configurable via VITE_API_BASE_URL env var.
 * Defaults to empty string (same-origin) — in production nginx serves the
 * built app and proxies "/api" to the backend, so the app is same-origin.
 * For local development against a standalone backend set it to e.g.
 * "http://localhost:8000".
 */
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

/** `JSON.parse` that answers null for a body that was not JSON at all. */
function parseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

/**
 * Extract a human-readable message from an already-read error body.
 *
 * FastAPI returns `{"detail": "..."}` for HTTPException and
 * `{"detail": [{"msg": "...", ...}, ...]}` for request validation errors (422).
 * Anything else falls back to *fallback*, which callers set to the status text.
 *
 * It takes the parsed body rather than the `Response` because a body can only
 * be read once: a caller that has already looked at the body for a shape of
 * its own — `moveLibraryPath` and its 409 conflicts — would otherwise get
 * "body already read" here and report the bare status.
 */
function errorDetail(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string" && detail.trim().length > 0) {
      return detail;
    }
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) =>
          item &&
          typeof item === "object" &&
          typeof (item as { msg?: unknown }).msg === "string"
            ? (item as { msg: string }).msg
            : null,
        )
        .filter((msg): msg is string => msg !== null);
      if (messages.length > 0) return messages.join("; ");
    }
  }
  return fallback;
}

/** The message for an error response whose body nobody has read yet. */
async function parseErrorDetail(res: Response): Promise<string> {
  const text = await res.text().catch(() => "");
  return errorDetail(parseJson(text), res.statusText);
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
 * Fetch the whole scanned library: artists, their albums, and every track.
 *
 * One request for the whole tree is deliberate — the backend serves it from an
 * mtime-keyed scan cache, and having it all client-side is what lets the flat
 * search run over every level without a round trip.
 *
 * *signal* is TanStack Query's — a tab switched away from mid-scan aborts the
 * request instead of leaving the whole tree in flight.
 */
export async function getLibrary(
  signal?: AbortSignal,
): Promise<LibraryResponse> {
  const res = await fetch(`${API_BASE_URL}/library`, { signal });

  if (!res.ok) {
    throw new Error(`Failed to fetch library: ${await parseErrorDetail(res)}`);
  }

  return res.json() as Promise<LibraryResponse>;
}

/**
 * Fetch the open Navidrome and Lidarr problems.
 *
 * New ones arrive over the SSE stream; this is what a tab opened after the
 * backend raised one needs — the Lidarr tag-scrub warning is raised seconds
 * after boot, long before any browser is watching.
 */
export async function fetchNotices(): Promise<Notice[]> {
  const res = await fetch(`${API_BASE_URL}/notices`);

  if (!res.ok) {
    throw new Error(`Failed to fetch notices: ${await parseErrorDetail(res)}`);
  }

  return res.json() as Promise<Notice[]>;
}

/**
 * The cover art URL for an album.
 *
 * The path travels as a query parameter rather than a URL segment because it
 * contains slashes; `encodeURIComponent` is what keeps `#`, `&`, and `?` in a
 * folder name from being read as URL syntax. `v` is the album's
 * `cover_version`, so a folder that changed gets a fresh URL instead of the
 * browser's cached image.
 */
export function coverUrl(
  album: Pick<LibraryAlbum, "path" | "cover_version">,
): string {
  const path = encodeURIComponent(album.path);
  return `${API_BASE_URL}/library/cover?path=${path}&v=${album.cover_version}`;
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
    "notices",
  ];
  for (const eventType of eventTypes) {
    eventSource.addEventListener(eventType, (e: MessageEvent) => {
      // EventSource's own connection "error" event is dispatched on the same
      // target and reaches these listeners as a plain Event with no `data`.
      // `JSON.parse(undefined)` would throw into the catch below, which is
      // harmless but hides nothing useful; bail out explicitly instead.
      if (typeof e.data !== "string") return;
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

/**
 * A move, delete, or restore the backend refused because something is already
 * in the way.
 *
 * `conflicts` are the target paths that are occupied — every one of them, so
 * the dialog can show the whole list rather than making the user discover them
 * one retry at a time. An in-flight download aiming at one of the folders
 * involved arrives as the same 409 with the folder as its one conflict.
 */
export class LibraryMoveConflict extends Error {
  readonly conflicts: string[];

  constructor(message: string, conflicts: string[]) {
    super(message);
    this.name = "LibraryMoveConflict";
    this.conflicts = conflicts;
  }
}

/**
 * Read the structured 409 body `{"detail": {"message", "conflicts"}}`.
 * Returns null for any other shape, so an unexpected body falls through to the
 * plain error path instead of rendering an empty conflict list.
 */
function parseConflict(body: unknown): LibraryMoveConflict | null {
  if (body === null || typeof body !== "object" || !("detail" in body)) {
    return null;
  }
  const detail = (body as { detail: unknown }).detail;
  if (detail === null || typeof detail !== "object") return null;
  const { message, conflicts } = detail as {
    message?: unknown;
    conflicts?: unknown;
  };
  if (typeof message !== "string" || !Array.isArray(conflicts)) return null;
  return new LibraryMoveConflict(
    message,
    conflicts.filter((item): item is string => typeof item === "string"),
  );
}

/**
 * `POST` a JSON body to a library endpoint and read the JSON that comes back.
 *
 * Every library action shares one error contract: a 409 carries
 * `{"detail": {"message", "conflicts"}}` and throws a `LibraryConflict` with
 * the occupied paths; anything else throws a plain Error with the backend's
 * message. Written once here so delete, restore, and empty-trash cannot drift
 * from move in how a refusal reaches the UI.
 */
async function postLibraryJson<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });

  if (!res.ok) {
    // Read once: a 409 whose body is not the conflict shape still has to
    // produce a message, and a second read of the same body would throw.
    const text = await res.text().catch(() => "");
    const parsed = parseJson(text);
    if (res.status === 409) {
      const conflict = parseConflict(parsed);
      if (conflict !== null) throw conflict;
    }
    throw new Error(errorDetail(parsed, res.statusText));
  }

  return res.json() as Promise<T>;
}

/**
 * Move tracks, move an album to another artist, or rename an artist.
 *
 * Paths travel in the body, never in the URL: a folder called "AC/DC" is a
 * perfectly good library path and would need an encoding scheme of its own in
 * a URL segment. A 409 throws a `LibraryMoveConflict` carrying the occupied
 * paths; every other failure throws a plain Error with the backend's message.
 */
export async function moveLibraryPath(
  request: LibraryMoveRequest,
): Promise<LibraryMoveResponse> {
  return postLibraryJson<LibraryMoveResponse>("/library/move", request);
}

/**
 * Move a track, a selection of tracks, an album, or an artist to the trash.
 *
 * Nothing is destroyed: the backend renames the item under `.trash` and
 * answers with the entry the Trash tab now lists. A 409 means an in-flight
 * download is aiming at one of the folders involved.
 */
export async function deleteLibraryPath(
  request: LibraryDeleteRequest,
): Promise<LibraryDeleteResponse> {
  return postLibraryJson<LibraryDeleteResponse>("/library/delete", request);
}

/**
 * Queue a metadata update for one track or album folder.
 *
 * Non-destructive and asynchronous: the backend answers with the `tagging` Job
 * it created and the work happens on the single tagging worker, so the caller's
 * only job is to put the row in the queue. A 409 throws a
 * `LibraryMoveConflict` — the same shape every other library refusal uses —
 * when the path is already being tagged or a download is aiming at it.
 */
export async function tagLibraryPath(
  request: LibraryTagRequest,
): Promise<Job> {
  return postLibraryJson<Job>("/library/tag", request);
}

/** Everything currently in `.trash`, newest first. */
export async function getTrash(signal?: AbortSignal): Promise<TrashResponse> {
  const res = await fetch(`${API_BASE_URL}/library/trash`, { signal });

  if (!res.ok) {
    throw new Error(`Failed to fetch the trash: ${await parseErrorDetail(res)}`);
  }

  return res.json() as Promise<TrashResponse>;
}

/**
 * Put a trash entry back, either where it came from or — with *artist* and
 * *album* — somewhere else.
 *
 * A 409 throws a `LibraryMoveConflict` naming the occupied paths and nothing
 * has moved, which is what opens the move dialog on a restore.
 */
export async function restoreTrashEntry(
  request: TrashRestoreRequest,
): Promise<TrashRestoreResponse> {
  return postLibraryJson<TrashRestoreResponse>(
    "/library/trash/restore",
    request,
  );
}

/** Destroy the whole of `.trash`. There is nothing to undo this with. */
export async function emptyTrash(): Promise<TrashEmptyResponse> {
  return postLibraryJson<TrashEmptyResponse>("/library/trash/empty");
}
