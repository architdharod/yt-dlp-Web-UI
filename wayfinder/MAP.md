# Wayfinder map: music-for-arr v2

Label: `wayfinder:map`
Tracker: local markdown. Tickets live in `wayfinder/tickets/`, one file each.
A ticket is claimed by filling its `Assignee:` line. Blocking is the `Blocked by:` line.
The frontier is every ticket with `Status: open`, empty assignee, and all blockers closed.

## Destination

A locked spec and an ordered implementation plan for music-for-arr v2: library browser with move and delete,
in-flight-only queue, persistent queue, artist bulk download with dedup, and a metadata update trigger.
The code-review fixes and a GitHub Actions pipeline that publishes images to GHCR (so the homelab runs `docker compose` with pulled images only)
are carried out on the map itself; the features are built afterwards from the plan.

## Notes

- Domain: single-user homelab tool. FastAPI + yt-dlp backend, React/Vite frontend, nginx proxy, Docker Compose.
  Files land in `DOWNLOAD_PATH/Artist/Album/track.flac`. Navidrome and Lidarr read that folder.
- Execution override: the fixes from the code review and the CI/CD pipeline (tickets 02 and 14) are carried out on the map
  (the user asked for these to be done, not only decided). Everything else stays a decision until the plan exists.
- Deployment target: the homelab runs the stack with `docker compose` only, pulling images built by GitHub Actions and published to GHCR (added 2026-09-03).
- Skills each session should consult: `grill-me` for grilling tickets; `code-review` / `security-review` for the review ticket.
- Standing decisions from charting (2026-09-03):
  - The library view shows the `DOWNLOAD_PATH` tree only, not Navidrome's or Lidarr's library.
  - The queue area shows in-flight jobs only (queued, downloading, converting, error). Done jobs disappear into the collection.
  - Bulk artist download sources: anything yt-dlp can enumerate (YouTube channel and YouTube Music artist, SoundCloud, Bandcamp, others) plus Spotify artists, whose tracks must be matched elsewhere.
  - Dedup means: skip tracks already present in the artist folder on disk, and pre-untick them in the selection list.
  - The queue must be persisted so a restart resumes it.
  - "Metadata update" is a best-effort combination: fix tags in the files and ask Navidrome/Lidarr to rescan.
- Research findings are written to `wayfinder/research/<name>.md` and linked from the ticket.

## Decisions so far

<!-- one line per closed ticket: [title](tickets/file.md): gist -->
- [Code review findings](tickets/01-code-review-findings.md): 22 ranked findings; the must-fix set is path traversal, template injection, zombie thread on timeout, playlist URLs, compose env fallbacks, and frontend queue drift. Detail in [research/code-review-findings.md](research/code-review-findings.md).
- [Apply the code review fixes](tickets/02-apply-code-review-fixes.md): all 22 findings addressed or explicitly left; URL host allowlist lives in `ALLOWED_URL_HOSTS` and must widen when bulk sources land; backend now fails fast on a bad `DOWNLOAD_PATH`; converting is a real state; 220 backend tests, frontend builds. Compose and nginx still need a `docker compose config` run on the homelab.
- [Navidrome and Lidarr APIs, and tag-lookup options](tickets/09-navidrome-lidarr-and-tagging.md): Navidrome rescan is Subsonic `startScan` with user/password; Lidarr is `X-Api-Key` plus `RescanFolders`/`RefreshArtist`; our `Artist/Album/track` layout is Lidarr-compatible; tag lookup via MusicBrainz first, AcoustID and iTunes as fallbacks, written with mutagen; skip beets.
- [Source enumeration: what yt-dlp yields for an artist page, and options for Spotify](tickets/07-source-enumeration.md): artist pages are `_type == "playlist"`; YouTube, SoundCloud, Bandcamp, Audius enumerate natively but with thin flat fields; keyless `ytmusicapi` is the best artist-to-tracks path; Spotify only as an opt-in metadata seed matched to YouTube Music with spotdl-style fuzzy title, artist, and duration scoring.

- [Domain model: how the app identifies tracks, albums, and artists on disk](tickets/03-domain-model.md): identity is the POSIX path relative to `DOWNLOAD_PATH`, sent as a JSON `path` field; Library/Artist/Album/Track/Job vocabulary; any audio file at depth 3 is a Track, misplaced files show in synthetic Unknown buckets; filename stays `<title>.flac`; new downloads get `SOURCEID`/`SOURCEURL` tags and dedup matches on those, then on normalised title; never overwrite; Job stores `result_path`. Amended by the prototype: loose tracks at `Artist/track.flac` are legitimate and shown as Singles.
- [Library view and move UX](tickets/04-collection-view-ux.md): (amended by the prototype: grid of artist tiles > album tiles > track list, in tabs Download / Library / Trash) originally a collapsible tree with a filter box; rows show title, duration, format badge, hover actions; move via a dialog with artist/album comboboxes (free text creates), track and album moves plus artist rename, album merge with all-or-nothing 409 on collisions, multi-select within an album; moves rewrite ALBUMARTIST/ARTIST/ALBUM; empty folders auto-removed; frontend adopts TanStack Query with SSE events invalidating the library query.
- [Delete semantics](tickets/05-delete-semantics.md): delete moves to `DOWNLOAD_PATH/.trash/<timestamp>/...` with Restore and Empty trash, works on track, album, artist, and multi-selected tracks; single confirm dialog with name and count; restore refuses 409 on collision and offers the move dialog; same rescan hook as any file change with README guidance for `ND_SCANNER_PURGEMISSING` and Lidarr monitoring; trash invisible to dedup; in-flight jobs get Cancel (owned by the persistent queue ticket). Amended by the prototype: Trash is a tab shown only when non-empty.
- [Library view prototype](tickets/06-collection-view-prototype.md): revision 2 locked in as the UI reference; artist grid > album grid > track list with cover art (embedded picture, then cover.jpg, then placeholder), tabs with Trash hidden when empty, badge only for non-FLAC, optional album on move (blank = loose Singles track). Mock: https://claude.ai/code/artifact/6e0217d3-e02f-4075-a920-af7be1f89668 and `wayfinder/prototypes/library-mock.html`.
- [Artist bulk download flow](tickets/08-artist-bulk-download-flow.md): same URL box, backend probe opens a preview for any collection URL (artists, albums, playlists, sets, Spotify artists); YouTube artists enumerate via `ytmusicapi` albums/singles/EPs, other sources via flat yt-dlp, Spotify resolves to the top YouTube Music artist match with no credentials or picker; flat checklist with album column, everything selected except in-library duplicates, warn at 500 and stop at 2000; artist editable once at the top, album from source; one parent Job with N of M progress over child Jobs; Bandcamp, SoundCloud DRM, and Spotify-match notices.
- [Metadata update behaviour](tickets/10-metadata-update-behaviour.md): per-track and per-album triggers plus automatic per-track fix after each download; MusicBrainz text search only with a strict duration, title, and artist bar; writes TITLE and ARTIST, strips junk, never touches ALBUMARTIST/ALBUM, never writes MBIDs; album pass adds TRACKNUMBER/DISCNUMBER and cover.jpg only when the whole folder maps to one release; loose singles get no art; tagging Jobs on one dedicated worker; a debounced rescan hook after every file change calls Navidrome startScan and Lidarr RescanFolders (known artists only), failures as a banner.

- [Persistent queue and job model](tickets/11-persistent-queue-and-job-model.md): stdlib sqlite3 at `DATA_PATH/queue.db` on its own volume, write-through from the in-memory dispatcher with download slots plus one tagging slot; one `jobs` table with `kind` (download, bulk, tagging), `parent_id`, `result_path`, `attempts`; parent status derived from children; interrupted jobs re-queued at boot (error after 3); Cancel via progress hook during download and by killing our own ffmpeg subprocess during converting (yt-dlp postprocessors dropped, mutagen writes tags and art); cancel in tagging keeps the file; Retry only on error, Dismiss deletes the row (parent cascades); done and cancelled pruned after 7 days, error kept; duplicate URL rejected only while in-flight; `GET /queue` returns in-flight and error only.

- [CI/CD and image delivery from GitHub to the homelab](tickets/13-cicd-and-image-delivery.md): amd64 only; private GHCR images `music-for-arr-backend`/`-frontend` pulled with a fine-grained `read:packages` PAT; one workflow where PRs and `main` run pytest plus frontend build, only passing `main` pushes publish `latest` + `sha-<short>`, a weekly cron republishes `latest` + `weekly-<date>` with `yt-dlp>=2026.8.19`; `docker-compose.yml` pulls and a committed override builds for dev; `queue.db` in named volume at `/config`; homelab copies compose + `.env` by hand and updates with `pull && up -d`.

- [Build the GitHub Actions pipeline and GHCR-based compose](tickets/14-build-the-pipeline.md): built, uncommitted: `.github/workflows/ci.yml` (tests gate, publish on `main`, weekly rebuild, tags `latest`/`sha-`/`weekly-`), `docker-compose.yml` pulls GHCR images with the `/config` volume, committed override builds for dev, `yt-dlp>=2026.8.19`, README deploy section; both compose sets pass `config`, 220 tests pass, frontend builds. Live push still needed to confirm images publish.

- [Write the ordered implementation plan](tickets/12-write-the-plan.md): `plans/music-for-arr-v2.md`, decisions header plus 14 ordered vertical slices (amended after review on 2026-09-04) from persistent queue through Spotify artists and a docs sweep. **Destination reached: the map is complete (2026-09-04).**

## Not yet specified

- (nothing at present)

## Out of scope

- Authentication and access control. The app is LAN-only by design (README). Delete and move raise the stakes, so revisit as a fresh effort if the app is ever exposed.
- Browsing Navidrome's or Lidarr's imported library. The user chose the download tree as the single source of truth.
- Lossy output formats. FLAC-only stays.
