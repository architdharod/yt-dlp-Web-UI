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

/**
 * Payload for Server-Sent Events from GET /queue/stream.
 *
 * `job_id` is null for events that are not about one job — `library_changed`
 * is emitted for moves, deletes, and tag writes that no job produced.
 */
export interface SSEEvent {
  event: string;
  job_id: string | null;
  data: Record<string, unknown>;
}

/**
 * A standing problem with Navidrome or Lidarr, from `GET /notices` and the
 * `notices` SSE event, which carries the whole open set on every change.
 *
 * The backend de-duplicates these by (source, message) while they are open, so
 * a wrong password is one notice however many downloads follow it. `id` is
 * fresh every time a problem is raised again after a success in between, which
 * is what lets a dismissed banner come back when the problem returns.
 */
export interface Notice {
  id: string;
  level: "error" | "warning";
  source: "navidrome" | "lidarr";
  message: string;
  created_at: string;
}

/**
 * One audio file under the library root, as `GET /library` reports it.
 *
 * `path` is the POSIX path relative to `DOWNLOAD_PATH` and is the track's
 * identity — the backend has no library table, so nothing else is stable.
 * `title` falls back to the filename when the file carries no title tag;
 * `error` holds the reason a tag read failed, and the row still renders.
 */
export interface LibraryTrack {
  path: string;
  name: string;
  title: string;
  artist: string | null;
  album: string | null;
  album_artist: string | null;
  track_number: number | null;
  disc_number: number | null;
  duration: number | null;
  format: string;
  bitrate: number | null;
  sample_rate: number | null;
  size: number;
  mtime: string;
  has_embedded_art: boolean;
  tags: Record<string, string[]>;
  error: string | null;
}

/**
 * A folder at depth 2. `cover_version` is an opaque change stamp over the
 * folder, its sidecar images and its audio files -- not a timestamp -- so it
 * busts the browser cache for the cover endpoint after a new `cover.jpg` or a
 * tag write lands.
 */
export interface LibraryAlbum {
  name: string;
  path: string;
  track_count: number;
  cover_version: number;
  has_cover: boolean;
  tracks: LibraryTrack[];
}

/**
 * A folder at depth 1, plus the synthetic root bucket.
 *
 * `singles` are the loose tracks sitting directly in the artist folder, which
 * the domain model treats as legitimate rather than misfiled. The synthetic
 * bucket (`synthetic: true`, `path: ""`, name "Unknown Artist") holds files
 * found at the library root and only appears when there are any; the UI marks
 * it as needing sorting. `cover_album_path` is the album whose art stands in
 * for the artist, or null when there is no album to take it from.
 */
export interface LibraryArtist {
  name: string;
  path: string;
  synthetic: boolean;
  album_count: number;
  track_count: number;
  albums: LibraryAlbum[];
  singles: LibraryTrack[];
  cover_album_path: string | null;
}

/** The whole `GET /library` response: the scanned tree and its totals. */
export interface LibraryResponse {
  artists: LibraryArtist[];
  artist_count: number;
  album_count: number;
  track_count: number;
  scanned_at: string;
}

/**
 * Request body for `POST /library/move`.
 *
 * One shape covers the three moves: `paths` for tracks that share a folder,
 * `path` for an album folder (moved to `artist`, optionally renamed by
 * `album`) or an artist folder (renamed to `artist`). Exactly one of the two
 * is sent. `album` blank or omitted means a loose Single, whose `ALBUM` tag
 * the backend clears.
 */
export interface LibraryMoveRequest {
  path?: string;
  paths?: string[];
  artist: string;
  album?: string | null;
}

/** One file's old and new path, as `POST /library/move` reports it. */
export interface MovedPath {
  from: string;
  to: string;
}

/**
 * The `POST /library/move` response. `removed` are the folders the move
 * emptied and the backend cleaned up.
 *
 * `destination` is the POSIX path, relative to `DOWNLOAD_PATH`, of the folder
 * the album or artist now lives at — authoritative even when `moved` is empty,
 * which happens for a merge into an existing folder where every file was
 * skipped. It is null for track moves, which leave the browsed folder standing.
 */
export interface LibraryMoveResponse {
  moved: MovedPath[];
  removed: string[];
  destination: string | null;
}
