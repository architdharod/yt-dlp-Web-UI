# Plan: music-for-arr v2

> Source: the wayfinder map `wayfinder/MAP.md` and its closed tickets under `wayfinder/tickets/`.
> Every decision below is gisted from a ticket; the ticket holds the detail and wins on any conflict.
> Written 2026-09-04. The CI/CD pipeline and the code-review fixes are already in the working tree and are
> not phases here.

## Scope

Library browser with move and delete (trash), in-flight-only queue, persistent queue with cancel, artist and
collection bulk download with dedup, and a metadata update trigger with Navidrome and Lidarr rescans.
Out of scope: authentication, browsing Navidrome's or Lidarr's own library, lossy output.

## Architectural decisions

Durable across all phases. Tickets in brackets.

- **Vocabulary** [Domain model]: `Library` is the `DOWNLOAD_PATH` tree. `Artist` and `Album` are folders at
  depth 1 and 2. `Track` is an audio file (flac, mp3, m4a, ogg, opus, wav) at depth 3, or a loose "Single" at
  depth 2. `Job` is a queue entry. "Download" is a verb only. UI copy says "library", never "collection".
- **Identity** [Domain model]: a Track, Album, or Artist is its POSIX path relative to `DOWNLOAD_PATH`. The
  filesystem is the only source of truth for the Library; there is no library table. Library paths travel as a
  JSON body field `path` (or `paths`), never as URL segments. Job ids stay in URL segments.
- **Path validation** [Domain model]: split on `/`; segments non-empty, not `.`/`..`, no `\` or NUL; resolved
  path must be under `DOWNLOAD_PATH.resolve()` after following symlinks. New folder names go through
  `sanitize_component`; existing names are accepted as they are on disk. Never overwrite; collisions are 409
  with the conflicting path(s).
- **Wrong depth** [Domain model, amended]: root-level files show under a synthetic `Unknown Artist`
  (`synthetic: true`); loose files at `Artist/track.flac` are legitimate Singles; folders deeper than depth 2
  flatten into their Album. Nothing is auto-tidied.
- **Source tags** [Domain model]: every new download writes Vorbis `SOURCEID=<extractor>:<id>` and
  `SOURCEURL=<webpage_url>`. Older files fall back to yt-dlp's `PURL`. Dedup matches on `SOURCEID`/`PURL`
  first, then on normalised title, across every audio file under the target Artist (folder matched
  case-insensitively). Trash is invisible to dedup.
- **Routes**:
  - Queue: `GET /queue` (in-flight and error only, parents with derived status and nested children),
    `GET /queue/stream`, `POST /queue/{id}/retry`, `POST /queue/{id}/cancel`, `POST /queue/{id}/dismiss`.
  - Download: `POST /download` (single track, unchanged), `POST /download/probe` (returns
    `{type: "track"}` or `{type: "collection", preview}`), `POST /download/bulk` (parent + children from a
    selection).
  - Library: `GET /library`, `GET /library/cover?path=`, `POST /library/move`, `POST /library/delete`,
    `GET /library/trash`, `POST /library/trash/restore`, `POST /library/trash/empty`, `POST /library/tag`
    (track or album path).
  - SSE events: existing `status_change`, `progress`, `error`, plus `library_changed` after any file write,
    move, delete, restore, empty-trash, or tag write (automatic and manual tagging included).
- **Persistence** [Persistent queue]: stdlib `sqlite3`, WAL, one locked connection, at `DATA_PATH/queue.db`
  (`DATA_PATH` default `/config`, own compose volume, fail fast if not writable). `PRAGMA user_version`
  migrations. Write-through from the in-memory dispatcher: every transition is written before its SSE event.
  Stdlib `sqlite3` blocks the event loop; per-transition writes are tiny so this is accepted, but writes go through
  one helper so they can be moved to `asyncio.to_thread` if SSE latency ever shows it.
- **Schema** [Persistent queue]: one `jobs` table: `id` (uuid4 text), `kind` in {download, bulk, tagging},
  `parent_id` (nullable self-reference), `status`, `url`, `title`, `thumbnail_url`, `duration`, `artist`,
  `album`, `path`, `result_path`, `error`, `attempts`, `restart_attempts`, `created_at`, `updated_at`,
  `finished_at`. Progress is memory-only.
- **States** [Persistent queue]: download `queued -> downloading -> converting -> tagging -> done`; tagging
  `queued -> tagging -> done`; `error` and `cancelled` terminal from any non-terminal state. Bulk parent status
  is derived from children on every read. Retention: done and cancelled pruned after 7 days; error kept until
  dismissed.
- **Workers** [Persistent queue]: download slots (`MAX_CONCURRENT_DOWNLOADS`) and exactly one tagging slot that
  serialises all MusicBrainz traffic.
- **Download pipeline** [Persistent queue]: yt-dlp fetches best audio with no postprocessors; our own
  `ffmpeg` subprocess converts to FLAC (cancellable); mutagen writes tags, `SOURCEID`/`SOURCEURL`, and the
  embedded thumbnail. Filename stays `<sanitized title>.flac`.
- **Rescan hook** [Metadata]: one debounced hook after any file change, `utime`-touches changed album folders,
  calls Navidrome Subsonic `startScan` and Lidarr `RescanFolders` (`filter=known`, `addNewArtists=false`).
  Each service is skipped when its env vars are unset. Env: `NAVIDROME_URL`, `NAVIDROME_USER`,
  `NAVIDROME_PASSWORD`, `LIDARR_URL`, `LIDARR_API_KEY`, optional `LIDARR_ROOT_FOLDER`.
- **Tag fixing** [Metadata]: MusicBrainz text search only via `musicbrainzngs` (1 req/s, User-Agent). Match
  bar: duration within 5 s, normalised title equal, artist credit matches the folder. Writes `TITLE` and
  `ARTIST`, strips yt-dlp junk, never touches `ALBUMARTIST`/`ALBUM`/`SOURCEID`/`SOURCEURL`, never writes
  MusicBrainz ids. Album pass adds `TRACKNUMBER`/`DISCNUMBER` and `cover.jpg` (Cover Art Archive) only when the
  whole folder maps to one release. Only FLAC takes part.
- **Library scan cache** (plan decision, no ticket): `GET /library` serves from an in-memory scan cache keyed by
  folder mtimes; only albums whose mtime changed are re-read. Covers are cached on disk keyed by album path plus
  folder mtime, so a move or a new `cover.jpg` invalidates the entry.
- **In-flight guard** (plan decision, no ticket): move, delete, and restore return 409 when any in-flight job's
  target path (artist or album folder) lies inside the paths being changed, so a download can never land in a
  folder that was just renamed or trashed.
- **Trash** [Delete semantics]: `DOWNLOAD_PATH/.trash/<UTC timestamp>/<original relative path>`, same-filesystem
  rename, excluded from the scan. Restore to the original path, 409 on collision.
- **Move** [Library UX]: tracks move anywhere (album optional, blank means loose Single and clears `ALBUM`);
  albums move to another artist with merge; artists rename. Move rewrites `ALBUMARTIST`, `ARTIST`, `ALBUM` in
  FLACs only. All-or-nothing with 409 listing conflicts. Empty album and artist folders are removed with
  non-audio leftovers.
- **Frontend** [Library UX, Prototype]: TanStack Query; `GET /library` and `GET /queue` are queries; SSE
  patches the queue query and `library_changed`/`done` invalidate the library query; `refetchOnWindowFocus`
  on; no polling. Tabs: Download (form plus in-flight queue with active-count badge), Library (artist grid >
  album grid > numbered track list, breadcrumb, flat search), Trash (hidden when empty, count badge). The
  reference UI is `wayfinder/prototypes/library-mock.html`.
- **Bulk download** [Bulk flow]: one URL box; collections open a flat checklist preview (checkbox, title,
  album, duration, status) with everything selected except in-library duplicates, warn at 500 (nothing
  preselected), stop at 2000. Artist editable once at the top; album per track from the source. One parent
  Job with N of M progress over child Jobs. Sources: YouTube/YTM artists via keyless `ytmusicapi`
  (albums, singles, EPs; no videos); every other collection via flat yt-dlp; Spotify artist URLs resolve to
  the top YouTube Music artist match with no picker. `ALLOWED_URL_HOSTS` widens to `music.youtube.com`,
  `bandcamp.com`, `open.spotify.com`.

## Ordering constraints carried from the tickets

1. SQLite layer and boot restore before any feature that creates new job kinds.
2. Our own ffmpeg plus mutagen pipeline (source tags, embedded art) before dedup, tag fixing, and cover art.
3. TanStack Query migration before the Library UI.
4. Rescan hook before the library actions that call it.
5. `ytmusicapi` and the probe endpoint before the preview UI.

---

## Phase 1: Persistent queue that survives a restart

**Decisions**: Persistent queue and job model; Domain model (`result_path`, `parent_id`).

### What to build

Add `DATA_PATH` and the SQLite `jobs` table with the full schema above (all columns from day one, even the
ones later phases fill in). The existing dispatcher writes through on every transition. At boot the manager
loads non-terminal and error rows, re-queues interrupted ones (temp files removed, `attempts` incremented,
error after 3), and spawns them in `created_at` order. Daily and boot retention sweep. `GET /queue` returns
in-flight and error only; `MAX_TERMINAL_JOBS` goes away. Duplicate URL rejection includes restored rows.
Compose already carries the `/config` volume; README `DATA_PATH` row already exists, the "No persistent queue"
limitation line is removed. Backend dependencies (FastAPI, Pydantic, uvicorn, sse-starlette) are bumped to
current releases first, while the test suite is still untouched.

### Acceptance criteria

- [ ] Backend dependencies are on current releases and the existing tests pass before any queue work starts.
- [ ] Backend fails fast at startup when `DATA_PATH` is missing or not writable, with a clear message.
- [ ] A job queued, then the backend restarted, reappears as `queued` and completes; a job interrupted three
      times ends in `error` with "interrupted by restart 3 times".
- [ ] Every transition is present in `queue.db` before its SSE event reaches a client.
- [ ] `GET /queue` omits done and cancelled jobs; done rows older than 7 days are gone after the sweep; error
      rows stay.
- [ ] Resubmitting a URL that is queued or running is rejected; resubmitting a done or errored URL is accepted.
- [ ] Existing 220 backend tests still pass, plus tests for restore, sweep, and duplicates using a temp `DATA_PATH`.

---

## Phase 2: Cancel, Dismiss, and the in-house ffmpeg and tagging pipeline

**Decisions**: Persistent queue (cancel, retry, dismiss); Domain model (`SOURCEID`/`SOURCEURL`); Delete
semantics (in-flight jobs).

### What to build

Replace yt-dlp's postprocessors: yt-dlp downloads best audio only, our code runs `ffmpeg` as a subprocess to
FLAC, then mutagen writes the standard tags, `SOURCEID`, `SOURCEURL`, and the embedded thumbnail. `converting`
is now interruptible by killing that process. `POST /queue/{id}/cancel` on queued, downloading, converting
jobs ends them `cancelled` with partial and temp files removed in a `finally`. `POST /queue/{id}/dismiss`
deletes an error row. Retry stays error-only and increments `attempts`. Queue rows in the UI gain Cancel on
in-flight jobs and Dismiss on errored jobs.

### Acceptance criteria

- [ ] A fresh download's FLAC carries `SOURCEID=<extractor>:<id>`, `SOURCEURL`, title/artist/album tags, and an
      embedded picture; output path and filename are unchanged from today.
- [ ] Cancel during `downloading` and during `converting` both end in `cancelled` within a few seconds and
      leave no partial, `.part`, or temp file behind.
- [ ] Cancelled jobs vanish from the in-flight view and cannot be retried; Dismiss removes an error job.
- [ ] Download-slot concurrency is unchanged; the timeout path still cleans up.
- [ ] A single download whose target filename already exists is skipped with a visible "already in library"
      reason and never overwrites the file.
- [ ] Tests cover the ffmpeg call, the mutagen tag write, cancel in each state, and dismiss.

---

## Phase 3: TanStack Query, tabs, and the `library_changed` event

**Decisions**: Library view and move UX (data layer); Prototype (tabs).

### What to build

Frontend moves to React 19 and Vite 7 in the same slice, since the data layer is rewritten anyway, then adopts
TanStack Query. `GET /queue` is the queue query; the SSE stream patches it with `setQueryData`
and no longer holds its own state. The single page becomes tabs: Download (form plus in-flight queue with an
active-count badge), Library (placeholder until Phase 4), Trash (hidden). Backend emits `library_changed` on
the SSE stream whenever a job finishes writing a file (later phases reuse the same emitter for moves, trash,
and tag writes). UI copy changes from "collection" to "library".

### Acceptance criteria

- [ ] Queue renders from the query cache; SSE reconnect after a backend restart refetches `GET /queue` and shows
      the restored jobs; no polling.
- [ ] Download tab badge shows the count of in-flight jobs and updates live.
- [ ] `library_changed` arrives on the stream after a job reaches `done`.
- [ ] `useSSE` no longer owns job state; frontend typechecks and builds on React 19 and Vite 7.

---

## Phase 4: Read-only Library browser

**Decisions**: Domain model (scan, synthetic buckets, Singles); Library UX and Prototype (grid, search, cover art).

### What to build

`GET /library` scans `DOWNLOAD_PATH` (skipping `.trash` and `.tmp`, the per-job yt-dlp scratch directory) into Artists > Albums > Tracks plus Singles per artist
and a synthetic `Unknown Artist` for root files, with title (tag, else filename), duration, format, and counts
from a cheap tag read, served from the mtime-keyed scan cache so only changed albums are re-read.
`GET /library/cover?path=` serves the embedded picture of the first track that carries a valid picture, else
`cover.jpg`, else a generated placeholder, cached on disk keyed by album path plus a change stamp over the
folder, sidecar images and audio file mtimes. The Library tab shows artist tiles, album tiles, and
the numbered track list with breadcrumb navigation, a flat search across all levels, a format badge on
non-FLAC tracks only, and a detail popover with size and full tags. The library query is invalidated by
`library_changed` and refetches on window focus.

### Acceptance criteria

- [ ] A tree with normal albums, loose Singles, a root-level file, and a folder nested too deep renders as the
      domain model says (Singles section, `Unknown Artist` marked as needing sorting, deep folder flattened).
- [ ] Cover endpoint returns art by each of the three fallbacks and refuses any path outside the root.
- [ ] Finishing a download makes the new track appear without a manual refresh.
- [ ] Search for a track title jumps to it; breadcrumb returns to the artist grid.
- [ ] `GET /library` on a few thousand files responds in well under a second on the homelab, and a second call
      with no changes reads no tags.
- [ ] Writing a new `cover.jpg` into an album folder changes what the cover endpoint returns on the next call.

---

## Phase 5: Rescan hook and Navidrome/Lidarr configuration

**Decisions**: Metadata update behaviour (hook, service config, failures); Delete semantics (README settings).

### What to build

The debounced rescan hook fires after every job that writes a file: touch changed album folders, Navidrome
`startScan`, Lidarr `RescanFolders` with `filter=known`, each skipped when unconfigured. Failures surface once
as a dismissible banner delivered over SSE, and are logged. At startup the app reads Lidarr's metadata-provider
config and warns in the banner if `scrubAudioTags` is on. Compose and `.env.example` gain the six service
variables. README documents them, the Navidrome admin-user requirement, `ND_SCANNER_PURGEMISSING=always`, and
Lidarr monitoring guidance. Covers fetched from the Cover Art Archive are downscaled with ffmpeg before being
written, falling back to the original bytes if ffmpeg fails.

### Acceptance criteria

- [ ] With both services configured, one download triggers exactly one `startScan` and one `RescanFolders`
      after the quiet period; three quick downloads trigger one of each.
- [ ] With neither configured, nothing is called and nothing is logged as an error.
- [ ] Bad credentials produce one banner, not one per file; dismissing it hides it until the next failure.
- [ ] Tests use a fake HTTP layer for both services and cover debounce, skip, and failure paths.

---

## Phase 6: Move and rename

**Decisions**: Library view and move UX (move levels, picker, tags, merge, empty folders); Domain model
(validation, collisions); Prototype (optional album, Singles).

### What to build

`POST /library/move` handles a track (or multi-selected tracks within one album or Singles) to any artist and
optional album, an album to another artist with merge, and an artist rename. All-or-nothing with 409 listing
every conflict. FLAC tags `ALBUMARTIST`, `ARTIST`, `ALBUM` are rewritten (`ALBUM` cleared for loose Singles),
everything else preserved. Empty album then artist folders are removed with non-audio leftovers. The move
dialog has artist and album comboboxes with free-text creation; artist rename shows only the artist field.
Checkboxes enable Move selected. A move whose source or target contains the target folder of an in-flight job
returns 409 (in-flight guard). Every move fires the rescan hook and `library_changed`. Deferred here from Phase
4: the downloader switches album-less downloads from `Artist/Unknown Album/` to the loose-Single form
`Artist/<title>.flac`, with `_already_in_library` checking both the new and the legacy path. The cover disk
cache also gains a prune of entries whose album path no longer exists after a scan.

### Acceptance criteria

- [ ] Moving a track to a new artist and album creates both folders with sanitised names and updates the three
      tags; `SOURCEID` survives.
- [ ] Moving an album onto an artist that already has that album merges disjoint files and returns 409 with the
      list when any filename collides, leaving nothing half-moved.
- [ ] Renaming an artist moves the whole folder and rewrites `ALBUMARTIST` in every FLAC below it.
- [ ] A request containing `..`, an absolute path, or a symlink escaping the root is rejected with 4xx.
- [ ] After moving the last track out of an album, the album folder and its `cover.jpg` are gone.
- [ ] Renaming an artist while a job is downloading into it returns 409 and moves nothing.

---

## Phase 7: Delete, Trash tab, Restore, Empty trash

**Decisions**: Delete semantics; Prototype (Trash tab).

### What to build

`POST /library/delete` moves a track, multi-selection, album, or artist into `.trash/<timestamp>/...` as one
entry. `GET /library/trash` lists entries (original path, deleted-at, track count). `POST
/library/trash/restore` puts an entry back, 409 on collision, after which the UI offers the move dialog for a
different target. `POST /library/trash/empty` deletes everything permanently. The Trash tab appears with a
count only when non-empty. One confirm dialog names the item and its track count; Empty trash confirms with
the total. All four actions fire the rescan hook and `library_changed`; empty-folder cleanup runs after track
deletes. Delete and restore apply the in-flight guard. `.trash` carries a `.ndignore` file so Navidrome skips it
even if hidden-folder handling changes; whether Lidarr's `RescanFolders` skips hidden folders is verified against
a live Lidarr in this phase, and if it does not, the README tells the user to add `.trash` to Lidarr's ignore list.

### Acceptance criteria

- [ ] Deleting an album and restoring it brings back the identical folder, including `cover.jpg`.
- [ ] Restore onto an occupied path returns 409 and the UI opens the move dialog prefilled with the entry.
- [ ] `.trash` never appears in `GET /library`, and a trashed track no longer counts as an in-library duplicate.
- [ ] Trash tab is hidden at zero items and shows the count otherwise.
- [ ] Empty trash removes the `.trash` contents and returns the count removed.
- [ ] `.trash/.ndignore` exists after the first delete; Lidarr's behaviour on hidden folders is recorded in the
      README.
- [ ] Deleting an album that an in-flight job targets returns 409.

---

## Phase 8: Automatic tag fix after every download

**Decisions**: Metadata update behaviour (lookup, match bar, fields, worker, failures); Persistent queue
(`tagging` state, tagging worker).

### What to build

Add the single tagging worker and the `tagging` state. When a download's FLAC is written, the download slot is
released and the job waits FIFO on the tagging worker, which queries MusicBrainz with the cleaned title,
folder artist, and duration. On a match it writes `TITLE` and `ARTIST`, strips yt-dlp junk fields, and
preserves the source tags; below the bar it changes nothing. The job then reaches `done`; a failed fix still
ends `done` with "tags not fixed" in the job detail. Cancel during `tagging` aborts only the fix. A restart
during `tagging` re-queues just the tag fix. `library_changed` is emitted after every tag write so the Library tab
picks up corrected titles. `musicbrainzngs` is added to requirements with the mandatory
User-Agent.

### Acceptance criteria

- [ ] With a stubbed MusicBrainz, a matching recording rewrites `TITLE`/`ARTIST` and leaves `ALBUMARTIST`,
      `ALBUM`, `SOURCEID`, `SOURCEURL` untouched; a non-matching one changes no bytes.
- [ ] Two downloads finishing together are tagged one after the other, never concurrently.
- [ ] The queue row shows `tagging` after `converting`, and the download slot is free during it.
- [ ] MusicBrainz down or rate-limited: the job ends `done` with "tags not fixed" and the file is intact.
- [ ] A corrected `TITLE` shows in the Library tab without a manual refresh.

---

## Phase 9: Manual tagging jobs and the album pass

**Decisions**: Metadata update behaviour (triggers, album pass, cover art, jobs and progress).

### What to build

`POST /library/tag` with a track or album path creates a `tagging` Job shown in the in-flight queue with N of M
progress for albums, cancellable, retryable on error, dismissable. The track action redoes Phase 8's fix. The
album pass looks up every track; when all map to one release it also writes `TRACKNUMBER`/`DISCNUMBER` and
fetches `cover.jpg` from Cover Art Archive (never overwriting an existing one, never writing release ids);
otherwise it applies per-track fixes and reports `partial: N of M`. Singles get title and artist only. Row and
album-header actions "Update metadata" in the Library tab. Rescan hook and `library_changed` fire after each run.

### Acceptance criteria

- [ ] An album whose tracks all match one release gets sequential track numbers and a `cover.jpg`; the library
      grid then shows that art.
- [ ] An album with a partial match gets no numbers and no cover, and the job reports `partial: N of M`.
- [ ] A Single never receives cover art or an `ALBUM` tag.
- [ ] Cancel mid-album leaves already-written tracks tagged and the rest untouched; nothing is stored about
      matches, so re-running re-queries.

---

## Phase 10: Collection probe and bulk jobs (backend and queue UI)

**Decisions**: Artist bulk download flow (entry, placement, queue shape); Domain model (dedup); Persistent queue
(bulk parents); Source enumeration research.

### What to build

`POST /download/probe` runs a flat yt-dlp extraction with `ignoreerrors`: a single item returns
`{type: "track"}` and the form queues it as today; a `playlist` returns a preview payload built from the flat
entries (YouTube playlists and OLAK albums, SoundCloud users/sets/albums, Bandcamp artists/albums, other yt-dlp
collections), with album grouping where the source gives it, per-row dedup status from the on-disk rule,
DRM-unavailable rows flagged, source notices, the 500 warning flag and the 2000 stop. Enumeration is cached per
URL for the session. `POST /download/bulk` creates the parent and one child per selected track; children resolve
their metadata when they run. The queue shows the parent as one row with N of M and counts, expandable to
children; Cancel cascades; Retry on a failed child; Dismiss on the parent deletes all. Host allowlist widens to
`bandcamp.com`. Duplicate tracks submitted anyway are skipped with a visible reason. The frontend in this phase
only gains the parent/child queue rows; the preview UI is Phase 11, so the probe is exercised through tests and
the API.

### Acceptance criteria

- [ ] Probing a single video URL returns `{type: "track"}`; probing a YouTube playlist returns a preview payload
      with one row per entry, album grouping where present, and dedup status per row.
- [ ] Tracks already under the target artist (by `SOURCEID`, then normalised title) are marked "in library";
      rows the flat pass reports unavailable (`availability`, `live_status`, or an `ignoreerrors` entry error)
      are marked unavailable. SoundCloud DRM is not visible to the flat pass -- yt-dlp only reaches the
      transcodings loop under a full extraction -- so a DRM track previews as available and fails in its child
      job with yt-dlp's DRM message.
- [ ] Submitting 3 of 10 rows to `/download/bulk` creates one parent and 3 children; the parent row shows
      "0 of 3" and disappears when all are done; a failed child keeps the parent visible with Retry.
- [ ] Cancel on the parent cancels every non-terminal child; restart mid-bulk resumes the remaining children.
- [ ] A 2001-track collection stops enumeration with the "narrower URL" message.

---

## Phase 11: Collection preview UI

**Decisions**: Artist bulk download flow (preview).

### What to build

The Download form calls the probe on submit. A `collection` result opens the flat checklist preview: checkbox,
title, album, duration, status columns; Select all, Select none, and a selected count; the editable artist field
at the top; in-library rows unticked, unavailable rows greyed and unselectable; the Bandcamp 128 kbps notice
when relevant; above 500 rows a warning and nothing preselected. Submit posts the selection to
`/download/bulk` and returns to the Download tab with the parent row visible.

### Acceptance criteria

- [ ] A YouTube playlist URL opens the preview; a single video URL queues directly with no preview.
- [ ] In-library rows start unticked with an "in library" label; greyed rows cannot be ticked.
- [ ] Select all, Select none, and the selected count behave; a 600-row preview starts with nothing selected and
      shows the warning.
- [ ] Editing the artist field applies to every submitted child.

---

## Phase 12: YouTube and YouTube Music artist enumeration via `ytmusicapi`

**Decisions**: Artist bulk download flow (enumeration by source); Source enumeration research.

### What to build

The probe recognises YouTube channel and YouTube Music artist URLs, resolves the channel to its YouTube Music
artist, and enumerates albums, singles, and EPs through keyless `ytmusicapi`, excluding videos, live clips, and
visualisers. Rows carry album and duration; downloads use the album's `audioPlaylistId` or the track
`videoId` through yt-dlp. Host allowlist widens to `music.youtube.com`. `ytmusicapi` is added to requirements.

### Acceptance criteria

- [ ] A `youtube.com/channel/UC...`, `@handle`, or `music.youtube.com/channel/...` URL for an artist yields a
      preview grouped by album with singles and EPs included and no videos.
- [ ] Each row downloads to `Artist/Album/<title>.flac` with the album from YouTube Music; album-less tracks
      land as Singles.
- [ ] Dedup and the 500/2000 limits behave as in Phases 10 and 11.
- [ ] Enumeration of a large artist completes in a few seconds and is cached for the session.

---

## Phase 13: Spotify artist URLs

**Decisions**: Artist bulk download flow (Spotify resolution and notices).

### What to build

The probe accepts `open.spotify.com/artist/...`, reads the artist name from oEmbed or the page title, searches
YouTube Music, takes the top artist match without a picker, and hands off to Phase 12's enumeration. The
preview shows the resolved artist in the editable field and the notice that the match may differ from the
Spotify discography. Host allowlist widens to `open.spotify.com`. No Spotify credentials anywhere.

### Acceptance criteria

- [ ] A Spotify artist URL opens the YouTube Music preview for that artist with the notice shown.
- [ ] An unresolvable name returns a clear error rather than an empty preview.
- [ ] Spotify track and album URLs are rejected with a message naming what is supported.

---

## Phase 14: Docs and limitations sweep

**Decisions**: all; README obligations from Delete semantics, Metadata, Bulk flow.

### What to build

Update README: How It Works and Architecture for the tabs, trash, tagging, bulk, and persistence; the
Configuration table with the Navidrome/Lidarr variables; the Limitations list (drop single-track-only, drop
no-persistent-queue, keep no-auth and FLAC-only, add the Spotify-match and Bandcamp-quality caveats);
screenshot refresh. Remove `MAX_TERMINAL_JOBS` and any dead config from `.env.example`.

### Acceptance criteria

- [ ] Every env var the backend reads is in the table with its default.
- [ ] README setup steps work from a clean homelab host using only the published images.
- [ ] No remaining "collection" wording in UI or docs.

---

## Phase 15: Rate limits and PO tokens

**Decisions**: Persistent queue (worker slots, write-through, notices); Bulk flow (a playlist is N jobs on one
host); Domain model (no "collection" in user-visible copy).

### What to build

Stop a large YouTube playlist from failing every child with a 429 and a manual Retry.

**Prevention.** Fold the per-child metadata pre-probe into the download: one `extract_info(download=False)`
whose info dict feeds the metadata event *and* the already-in-library check, then `process_ie_result(info,
download=True)`, so each track costs one page read instead of two. Add `YTDLP_SLEEP_REQUESTS` (default `0.75`,
`0` disables) to `base_opts()`, so it paces every session including the probe's flat enumeration. Cap YouTube
at two concurrent downloads whatever `MAX_CONCURRENT_DOWNLOADS` says, with a per-lane semaphore taken *before*
the global one so the cap cannot stall the other sources.

**Detection.** A new `app/rate_limit.py` with `rate_limit_status(exc)`, which walks
`__cause__`/`__context__`/`.cause`/`.exc_info` for a `yt_dlp.networking.exceptions.HTTPError` with
`status == 429` (yt-dlp's YouTube extractor never retries one, and ignores `Retry-After`), plus
`retry_after_seconds` and `is_bot_check`, which matches YouTube's "sign in to confirm you're not a bot" clause
narrowly.

**Lanes.** One lane per source host (`youtube`, `soundcloud`, `bandcamp`), each open or held until T with a
consecutive-429 count, persisted in a new `lanes` table (`PRAGMA user_version` 4) so a restart honours a
remaining hold. A 429 holds the whole lane for 30/60/120/240/480 s with ±20 % jitter, never shorter than a
`Retry-After`. The waiting job stays `downloading`, keeps its *lane* slot and gives back its
*download* slot (so other sources keep downloading), shows `job.detail` plus an absolute `retry_at` the
frontend counts down from — both now always on the wire and null when empty, because a note that can be taken
back needs "there is none" to be distinguishable from "this event does not mention it" — and does not spend its wait against `DOWNLOAD_TIMEOUT_SECONDS` — one
`asyncio.wait_for` per attempt, the wait and the slot re-acquisition outside it. The note is re-emitted
whenever the hold moves, and says "retry N of M" only for a job that has spent attempts of its own. Cancel
during a wait is immediate. When a hold lapses one job (the oldest waiter) goes first as a canary and the rest
say so (no `retry_at`, nothing to count down to); its first progress or info dict opens the lane, its 429
extends the hold and costs only its own attempt. A job that was merely waiting when the process stopped is
re-queued at boot without spending a restart attempt. `RATE_LIMIT_ATTEMPTS` (default 5) attempts, then that job
ends `error` with the lane still held. A lane held for over an hour fails everything *parked* on it — a job
that is downloading is on the lane but not waiting for it, so it is neither elected canary nor failed by the
ceiling. A hold that lapses with nothing parked clears itself (the watchdog settles every held lane, so the
banner never outlives the hold) and keeps its consecutive count, since no request went out to say the limiter
had let go. A bot check fails its job immediately with no attempt spent, pauses the lane, and waits for
Resume now.

**Surfacing.** A notice per held lane through the existing `GET /notices` + `notices` SSE, carrying a new
optional `action: {label, method, path}` the banner renders as a button — `POST /queue/lanes/{host}/resume`,
which clears the hold and releases one canary. The notice also carries `hold_until`/`reason`/`held_since` and
its *message* carries no countdown, so the banner ticks by itself and the notice is raised afresh only on a
real transition — a re-raise is what un-dismisses a banner, so a per-second one would make dismissal
meaningless. `POST /download/probe` never backs off: a 429 there is a 400 with "YouTube is rate limiting this
server, try again in N s" and starts the hold, and a probe of a held host is refused before any request goes
out — for a Spotify artist URL that is the *YouTube* lane, since that is who the probe actually talks to.
A `ytmusicapi` 429 (`"Server returned HTTP 429"`, no status attribute) is recognised in both the channel and
the Spotify branch instead of being reported as "YouTube Music is unreachable".

**PO tokens.** A `pot-provider` sidecar (`brainicism/bgutil-ytdlp-pot-provider`, port 4416, compose network
only) plus its `bgutil-ytdlp-pot-provider` PyPI plugin in the backend image, pointed at the sidecar with the
`youtubepot-bgutilhttp:base_url` extractor arg from `POT_PROVIDER_URL`. The loaded providers are logged at
boot next to the yt-dlp version. Cookies stay as the documented last resort, untouched.

### Acceptance criteria

- [ ] One yt-dlp extraction per downloaded track; the already-in-library check still refuses a duplicate
      before anything is fetched.
- [ ] `YTDLP_SLEEP_REQUESTS`, `RATE_LIMIT_ATTEMPTS` and `POT_PROVIDER_URL` are in `docker-compose.yml`,
      `.env.example` and the README Configuration table with their defaults.
- [ ] A 429 leaves the job `downloading` with a countdown, not `error`; it succeeds on a later attempt without
      touching the persisted `attempts` counter, and the wait does not consume the download timeout.
- [ ] Cancel during a wait ends the job within a moment; a manual Retry gets a fresh attempt budget.
- [ ] A held lane shows one banner with a working **Resume now** that counts itself down; dismissing it hides
      it until the hold actually changes, and a watchdog tick emits no notices event and keeps the id.
- [ ] A hold that lapses — with a waiter, so a canary is elected, or with none, so the lane clears — is
      announced exactly once; an idle lapse keeps `consecutive` and drops `held_since`, so two refused probes
      an hour apart never arm the ceiling.
- [ ] A job that is downloading is not on the lane's waiting list: it is never elected canary, and the ceiling
      never fails it.
- [ ] A job waiting behind someone else's rate limit says "waiting N s", re-announces when the hold moves, and
      says what it is waiting for while the canary is in flight.
- [ ] With `MAX_CONCURRENT_DOWNLOADS=2` and two YouTube jobs waiting out a hold, a SoundCloud job still
      starts.
- [ ] A job that spends its whole budget fails with "gave up after 5 attempts over N min" and leaves the lane
      held; queued jobs behind it spend nothing; an hour of holding fails them all.
- [ ] A bot check fails one job, pauses the lane, and names the README section.
- [ ] `POST /download/probe` answers 400 with the rate-limit sentence and sends no requests while the lane is
      held.
- [ ] At most two YouTube downloads run at once with `MAX_CONCURRENT_DOWNLOADS=5`; other hosts fill the rest.
- [ ] A notice's action path is validated on both ways in (SSE and `GET /notices`) and on the backend, so it
      can only ever be an absolute path on this API.
- [ ] `pot-provider` has a healthcheck and the backend depends on it with `required: false`, so an unhealthy
      sidecar warns and never blocks startup.
- [ ] The backend boot log names the PO-token providers it loaded, and a `-v` download shows a token being
      fetched from the sidecar.
- [ ] A public playlist of 40+ tracks downloads to completion on the compose stack.
