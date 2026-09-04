# Persistent queue and job model

Label: `wayfinder:grilling`
Status: closed (2026-09-03)
Assignee: claude (2026-09-03)
Blocked by: 03

## Question

Design the persisted queue: SQLite via what library (stdlib `sqlite3`, `aiosqlite`, SQLModel); where the DB file lives (inside `DOWNLOAD_PATH` or a separate volume); job fields including bulk-parent link and resulting file path; what happens on restart to jobs that were downloading (re-queue versus mark error); retention of done and error jobs; cancel support as decided in [Delete semantics](05-delete-semantics.md): Cancel on queued, downloading, and converting jobs, interrupting the yt-dlp thread, removing partial files, ending in a `cancelled` state, plus Dismiss for errored jobs; and fields the domain model needs (`result_path`, `parent_id`).

See also [Artist bulk download flow](08-artist-bulk-download-flow.md): a parent Job aggregates child Jobs (`parent_id`), shows N of M progress, cancels remaining children as a cascade, and keeps failed children under it until dismissed.

See also [Metadata update behaviour](10-metadata-update-behaviour.md): a `tagging` job kind (per track or per
album, N of M progress, cancellable) run by one dedicated tagging worker outside the download slots, and a
`tagging` state on download jobs for the automatic per-track fix, whose failure must not fail the download.

## Resolution

Grilled with the user on 2026-09-03. Builds on [Domain model](03-domain-model.md) (`result_path`, `parent_id`),
[Delete semantics](05-delete-semantics.md) (Cancel, Dismiss), [Artist bulk download flow](08-artist-bulk-download-flow.md)
(parent and child Jobs) and [Metadata update behaviour](10-metadata-update-behaviour.md) (`tagging` Jobs and worker).

**Storage.** Stdlib `sqlite3`, no new dependency. One connection guarded by a lock, WAL mode. The file lives at
`DATA_PATH/queue.db` where `DATA_PATH` is a new env var (default `/config`) backed by its own small volume in compose,
separate from `DOWNLOAD_PATH` so app state never sits in the tree Navidrome and Lidarr scan. The backend fails fast
at startup if `DATA_PATH` is not writable, like it already does for `DOWNLOAD_PATH`. Schema versioning is
`PRAGMA user_version` with a numbered list of migration statements applied at boot; no Alembic.

**Architecture.** The in-memory dispatcher stays: `QueueManager` keeps its dict of Jobs and one asyncio task per job.
SQLite is write-through: every state transition is written before the SSE event is emitted, so the table is never
ahead of or behind what clients saw. Two worker pools: download slots (`MAX_CONCURRENT_DOWNLOADS`, unchanged) and
exactly one tagging slot. At boot the manager loads every non-terminal and error row, re-queues interrupted ones
(below) and spawns tasks in `created_at` order. SSE clients reconnecting after a restart refetch `GET /queue`.

**One `jobs` table** with a `kind` column, `kind in {download, bulk, tagging}`, and a nullable `parent_id`
self-reference for bulk children. Columns: `id` (uuid4 text, unchanged), `kind`, `parent_id`, `status`, `url`,
`title`, `thumbnail_url`, `duration`, `artist`, `album`, `path` (library path a tagging job targets; track or album),
`result_path` (relative library path once a download finishes), `error`, `attempts`, `created_at`, `updated_at`,
`finished_at`. A bulk parent row stores the source URL, the artist the user chose, and the child count via its
children. Progress (0-100) is memory-only and never written; after a restart it is 0 because the job re-runs.

**States.** `queued -> downloading -> converting -> tagging -> done`, plus `error` and `cancelled` as terminal states,
reachable from every non-terminal state. Tagging Jobs use `queued -> tagging -> done | error | cancelled`.
A bulk parent has no stored status; it is derived from its children on every read: `downloading` if any child is
running, else `queued` if any child is queued, else `error` if any child errored, else `cancelled` if any child was
cancelled, else `done`. Parents are in-flight while any child is in-flight or error.

**Restart.** Rows found in `downloading`, `converting`, or `tagging` at boot are re-queued automatically: partial and
temp files matching the job's output path are removed, `attempts` is incremented, status returns to `queued`.
After 3 interrupted attempts the job is marked `error` with "interrupted by restart 3 times" so a job that crashes
the process cannot loop forever. A download interrupted in `tagging` is not re-downloaded: the file is already
complete, so only the tag fix is re-queued (status `tagging`, waiting on the tagging worker).

**Cancel.** `POST /queue/{id}/cancel` on any queued, downloading, converting, or tagging job.
Queued: dropped straight to `cancelled`. Downloading: the existing `threading.Event` checked by the progress hook
aborts yt-dlp. Converting: the download pipeline changes so that yt-dlp fetches the raw best-audio file without
postprocessors and our code runs `ffmpeg` itself via `subprocess.Popen`; cancel terminates that process. Tags,
`SOURCEID`/`SOURCEURL`, and the embedded thumbnail are then written with mutagen (this replaces the current
`FFmpegMetadata` and `EmbedThumbnail` postprocessors). Partial and temp files are removed in a `finally` block; a
cancelled job ends in `cancelled` and leaves the in-flight view. Cancel while a download job is in `tagging` aborts
the tag fix only: the file stays, the job ends `done` with "tags not fixed". Cancel on a bulk parent cascades to every
child that is not terminal. Cancelling a manual tagging job leaves files untouched.

**Retry.** `POST /queue/{id}/retry` on `error` jobs only (download, child, or tagging): manual, unlimited, resets
error and progress, increments `attempts`. Cancelled jobs cannot be retried; the user resubmits. No automatic retry.
Retrying a child puts its parent back in-flight.

**Dismiss.** `POST /queue/{id}/dismiss` on `error` jobs deletes the row. Dismissing a bulk parent deletes the parent
and every child. A parent whose children are all done or dismissed is deleted with them.

**Retention.** `done` and `cancelled` rows are pruned by a daily sweep after 7 days (and on boot). `error` rows stay
until dismissed. The in-memory dict mirrors the table, so `MAX_TERMINAL_JOBS` goes away.

**Duplicates.** Submission is rejected when the same URL is already `queued`, `downloading`, `converting`, or
`tagging`, including jobs restored from SQLite after a restart. Done, error, and cancelled jobs never block a
resubmission. On-disk dedup (domain model) is a separate check that yields "skipped: already in library".

**Tagging worker.** The automatic per-track tag fix runs on the single tagging worker, not in the download slot: when
the FLAC is written the download slot is released, the job enters `tagging` and waits FIFO alongside manual
tagging jobs. All MusicBrainz traffic is therefore serialised in one place.

**API.** `GET /queue` returns in-flight and error jobs only (done and cancelled are omitted), parents with derived
status and their children nested. `GET /queue/stream` unchanged in shape; parent rows emit a synthetic
`status_change` whenever a child changes. Job ids stay in URL segments (the no-paths-in-URL rule applies to library
paths only).

Consequences: the plan (12) gets a slice for the SQLite layer and boot restore before any feature that creates new
job kinds, and a downloader slice that replaces the yt-dlp postprocessors with our own ffmpeg call plus mutagen
tagging (the domain model's `SOURCEID` write and the prototype's embedded cover art both land there); compose and
README gain `DATA_PATH` and its volume, which the CI/CD tickets (13, 14) must carry into the GHCR compose file.
