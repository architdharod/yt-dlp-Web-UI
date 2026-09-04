/**
 * Job lifecycle states matching backend JobStatus enum.
 *
 * A download runs queued -> downloading -> converting -> tagging -> done;
 * "error" and "cancelled" are terminal and reachable from any of the others.
 * "tagging" has its own label and icon in the queue.
 */
export type JobStatus =
  | "queued"
  | "downloading"
  | "converting"
  | "tagging"
  | "done"
  | "error"
  | "cancelled";

/**
 * Prefix of the error a job carries when its target file already exists in the
 * library. Mirrors ALREADY_IN_LIBRARY_PREFIX in backend/app/downloader.py — the
 * backend ends such a job as "error", but it is a skip rather than a failure,
 * so the queue renders it neutrally and offers no Retry. Keep the two in sync.
 */
export const ALREADY_IN_LIBRARY_PREFIX = "already in library: ";

/** A download job as returned by the backend API. */
export interface Job {
  id: string;
  url: string;
  status: JobStatus;
  title: string | null;
  thumbnail_url: string | null;
  duration: number | null;
  progress: number;
  error: string | null;
  artist: string | null;
  album: string | null;
  created_at: string;
}

/** Request body for POST /download. */
export interface DownloadRequest {
  url: string;
  artist?: string | null;
  album?: string | null;
}

/** Payload for Server-Sent Events from GET /queue/stream. */
export interface SSEEvent {
  event: string;
  job_id: string;
  data: Record<string, unknown>;
}
