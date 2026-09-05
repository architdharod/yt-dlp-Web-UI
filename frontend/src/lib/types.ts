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

/**
 * What a job is doing, as the backend's `kind` column names it.
 *
 * `download` is one track; `bulk` is the parent of a collection download;
 * `tagging` is a manual "Update metadata" run over a track or an album folder.
 * The kind decides how a queue row reads: a tagging job has no URL to show and
 * counts tracks rather than bytes.
 */
export type JobKind = "download" | "bulk" | "tagging";

/** A job as returned by the backend API: a download, a bulk parent, or a tagging run. */
export interface Job {
  id: string;
  kind: JobKind;
  /**
   * The bulk parent this job is a child of, or null for a top-level job.
   *
   * `GET /queue` never lists a child at the top level, so a job with a
   * `parent_id` is only ever reached through its parent's `children` — but
   * every SSE event carries the field, which is what lets the cache go
   * straight to the parent holding the child the event is about.
   */
  parent_id: string | null;
  /**
   * The children of a bulk parent, in `created_at` order — every one of them,
   * whatever its status, because a finished or cancelled track still has to be
   * accounted for under the collection it came from. Absent on anything that
   * is not a `bulk` parent, and never nested more than one level deep.
   */
  children?: Job[];
  url: string;
  status: JobStatus;
  title: string | null;
  thumbnail_url: string | null;
  duration: number | null;
  progress: number;
  error: string | null;
  /**
   * A note about a job that finished anyway — today only "tags not fixed: ..."
   * on a `done` row, when the automatic MusicBrainz fix did not apply. It is
   * not an error: the track is in the library. Optional here because `done`
   * jobs leave the in-flight view, so most rows the UI holds never carry one.
   */
  detail?: string | null;
  artist: string | null;
  album: string | null;
  /**
   * The library path the job is about — the track or album folder a tagging
   * job is rewriting. Null for a download, which has a URL instead and only
   * learns its path once the file has landed.
   */
  path?: string | null;
  /**
   * N of M for a job that counts whole items rather than bytes: the tracks a
   * tagging job has written of the tracks in the album folder. Both null for a
   * download and for a single-track tagging run, which have nothing to count,
   * so the row shows the percent bar or nothing at all instead.
   */
  progress_done?: number | null;
  progress_total?: number | null;
  created_at: string;
}

/** Request body for POST /download. */
export interface DownloadRequest {
  url: string;
  artist?: string | null;
  album?: string | null;
}

/**
 * Request body for POST /download/probe.
 *
 * `artist` is the folder the form is currently showing, and only the dedup
 * pass reads it: sending it marks the preview's rows against the folder the
 * tracks would really land in. Omitted, the backend dedups against its own
 * suggestion.
 */
export interface ProbeRequest {
  url: string;
  artist?: string | null;
}

/** Where a collection came from, which decides the notices the preview shows. */
export type CollectionSource =
  | "youtube"
  | "soundcloud"
  | "bandcamp"
  | "other";

/**
 * Whether a previewed track can be downloaded.
 *
 * `in_library` is the dedup rule's verdict (`SOURCEID`, then normalised title,
 * under the target artist) and only unticks the row — it can still be
 * submitted, and the backend then skips it with a visible reason.
 * `unavailable` is a row nothing can be done with, a DRM SoundCloud track
 * being the case that made this exist; `reason` says which.
 */
export type PreviewRowStatus = "available" | "in_library" | "unavailable";

/**
 * One track in a collection preview, as the flat enumeration found it.
 *
 * `id` is the preview's own row handle (the checklist's key), not a job id —
 * no job exists until the selection is submitted. `source_id` is the
 * extractor's id for the track, which is what dedup matched on. `title` is
 * nullable because the flat pass often has none: a Bandcamp `/track/` row
 * arrives as a bare URL and nothing else.
 */
export interface PreviewRow {
  id: string;
  url: string;
  source_id: string | null;
  title: string | null;
  album: string | null;
  /**
   * Whether `album` is the whole answer because the source read the release
   * this track is on — a null album is then deliberately none, a loose
   * Single. False for the flat pass, whose listings often carry no album at
   * all. Sent straight back in the bulk submission.
   */
  album_final: boolean;
  duration: number | null;
  thumbnail_url: string | null;
  status: PreviewRowStatus;
  reason: string | null;
}

/**
 * The flat enumeration of a collection: everything the checklist needs.
 *
 * `total`, `in_library`, and `unavailable` are counts over `rows`, sent
 * rather than derived so the header reads the same as the backend's own view.
 * `large` is the 500-row flag: above it nothing is preselected and the preview
 * warns. `notices` are source-level remarks ("Bandcamp streams are 128 kbps"),
 * not errors — the 2000-row stop is a 400 instead.
 */
export interface CollectionPreview {
  url: string;
  title: string | null;
  artist: string | null;
  source: CollectionSource;
  rows: PreviewRow[];
  total: number;
  in_library: number;
  unavailable: number;
  large: boolean;
  notices: string[];
}

/**
 * What `POST /download/probe` made of a URL.
 *
 * A discriminated union on `type`: a single item queues straight through the
 * download form as it always has, and a collection opens the preview. The
 * probe is the only thing that knows which a URL is, so the form cannot decide
 * from the URL shape.
 */
export type ProbeResponse =
  | {
      type: "track";
      title: string | null;
      duration: number | null;
      thumbnail_url: string | null;
      artist: string | null;
      album: string | null;
    }
  | { type: "collection"; preview: CollectionPreview };

/**
 * One selected track in a bulk submission.
 *
 * The metadata the preview already has travels with the selection so the child
 * row reads properly while it waits; the child job resolves the rest itself
 * when it runs.
 */
export interface BulkTrack {
  url: string;
  title: string | null;
  album: string | null;
  /** The row's `album_final`, forwarded unchanged. */
  album_final: boolean;
  duration: number | null;
  thumbnail_url: string | null;
  source_id: string | null;
}

/**
 * Request body for `POST /download/bulk`: the parent and its children in one
 * post.
 *
 * `artist` is the field the user edited above the checklist and applies to
 * every child, which is what makes the whole collection land under one artist
 * folder. 409 means this collection is already in the queue.
 */
export interface BulkDownloadRequest {
  url: string;
  artist: string;
  title: string | null;
  tracks: BulkTrack[];
}

/**
 * Payload for Server-Sent Events from GET /queue/stream.
 *
 * `job_id` is null for events that are not about one job — `library_changed`
 * is emitted for moves, deletes, and tag writes that no job produced.
 *
 * `data` always carries `kind` and `parent_id` alongside the job snapshot, so
 * an event about a child of a bulk parent can be routed to the parent holding
 * it without searching the whole cache.
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

/**
 * Request body for `POST /library/tag`.
 *
 * One path: a track (title and artist only) or an album folder (the album
 * pass, which may also write track numbers and fetch a cover). There is no
 * artist-level or whole-library form — the metadata ticket rules both out.
 */
export interface LibraryTagRequest {
  path: string;
}

/**
 * Request body for `POST /library/delete`.
 *
 * The same two shapes as a move: `paths` for a selection of tracks from one
 * folder, `path` for a single track, an album folder, or an artist folder.
 * Exactly one of the two is sent, and the whole request becomes one trash
 * entry, so restoring brings an album or an artist back intact.
 */
export interface LibraryDeleteRequest {
  path?: string;
  paths?: string[];
}

/**
 * What kind of thing a trash entry holds.
 *
 * `track` is one file, `tracks` a selection from one folder; `album` and
 * `artist` are whole folders moved as one, which is what lets Restore put the
 * folder back with its `cover.jpg` and everything else that was in it.
 */
export type TrashEntryKind = "artist" | "album" | "track" | "tracks";

/**
 * One entry in `.trash`, as `GET /library/trash` lists them.
 *
 * `path` is the original library path the entry came from — the identity the
 * UI shows and the target Restore aims at. `paths` are the individual audio
 * files the entry covers, which is what the restore dialog reads the original
 * artist and album out of for a multi-track entry. `id` is the handle Restore
 * takes; nothing else about an entry is stable enough to name it.
 */
export interface TrashEntry {
  id: string;
  path: string;
  kind: TrashEntryKind;
  paths: string[];
  /** ISO 8601, UTC. */
  deleted_at: string;
  track_count: number;
}

/** The `POST /library/delete` response: the entry made, and folders cleaned up. */
export interface LibraryDeleteResponse {
  entry: TrashEntry;
  removed: string[];
}

/** The whole `GET /library/trash` response: entries newest first, plus totals. */
export interface TrashResponse {
  entries: TrashEntry[];
  track_count: number;
}

/**
 * Request body for `POST /library/trash/restore`.
 *
 * `artist` and `album` are omitted for the plain "put it back where it was"
 * restore, and carry the names the user picked in the move dialog after a 409
 * said the original path is occupied. A blank `album` files restored tracks
 * loose under the artist, exactly as it does for a move.
 */
export interface TrashRestoreRequest {
  id: string;
  artist?: string;
  album?: string | null;
}

/** One file's trash path and where the restore put it back. */
export interface RestoredPath {
  source: string;
  target: string;
}

/** The `POST /library/trash/restore` response. */
export interface TrashRestoreResponse {
  restored: RestoredPath[];
}

/** The `POST /library/trash/empty` response: how much was destroyed. */
export interface TrashEmptyResponse {
  removed: number;
  track_count: number;
}
