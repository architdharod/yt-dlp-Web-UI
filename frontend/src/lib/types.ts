/** Job lifecycle states matching backend JobStatus enum. */
export type JobStatus = "queued" | "downloading" | "converting" | "done" | "error";

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
