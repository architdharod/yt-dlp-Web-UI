# yt-dlp Web UI

This is a project for educational purpose, to learn the usage of the library yt-dlp to test download of royalty free content from different sources.

## Screenshot

![Web UI Screenshot](web%20ui.png)

## How It Works

1. Paste a YouTube, SoundCloud or Bandcamp URL into the web UI, optionally specifying artist and album names.
   Submitting first asks `POST /download/probe` what is behind the URL.
2. A single track queues as before: the backend extracts its metadata (title, thumbnail, duration) via yt-dlp and
   returns it immediately. A playlist, album or artist page opens a checklist instead — **Select all** / **Select
   none**, rows already in the library unticked, unavailable rows greyed out and unselectable, and an editable
   artist that re-checks the library as you change it. Above 500 rows nothing is preselected, and 2000 rows is a
   hard stop asking for a narrower URL. The ticked rows go to `POST /download/bulk`, which queues one parent job
   with a child per track.
3. The job enters an async queue and runs a three-stage pipeline: yt-dlp fetches the best audio stream and the
   thumbnail (no postprocessing), an ffmpeg subprocess of ours converts that stream to FLAC, and Mutagen writes the
   tags, the cover art, and the `SOURCEID`/`SOURCEURL` fields that record where the track came from.
4. Files are saved to `DOWNLOAD_PATH/Artist/Album/track.flac`, falling back to "Unknown Artist" when no artist is
   known. A track with no album is a Single and lands at `DOWNLOAD_PATH/Artist/track.flac` with no `ALBUM` tag.
5. With the track filed the download slot is freed and the job moves to **tagging**, queueing for the one tagging
   worker, which asks MusicBrainz about the cleaned title, the artist folder, and the duration. On a confident
   match (duration within 5 s, same title, same artist) it rewrites `TITLE` and `ARTIST` and strips yt-dlp's
   leftovers; below that bar it changes nothing. `ALBUMARTIST`, `ALBUM`, `SOURCEID`, `SOURCEURL` and the cover art
   are never touched, and no MusicBrainz ids are written. Either way the job then reaches **done** — a lookup that
   failed says "tags not fixed" in the job's detail and never fails the download.

Real-time progress is streamed to the browser via Server-Sent Events — no polling. The UI is split into tabs:
**Download** (the form and the in-flight queue, with a badge counting the jobs still working — an open collection
checklist takes over this tab until it is submitted or dismissed) and **Library**, with a **Trash** tab that appears
once something is in it.

Queue rows carry two actions. **Cancel** stops a job that is queued, downloading or converting: running ffmpeg is our
own process, so it is killed rather than waited out, and every partial and temporary file is removed. Cancelled jobs
leave the queue and are not retried — resubmit the URL instead. Cancelling a job that is already **tagging** stops
only the tag fix: the track is in the library, so the job finishes as done with "tags not fixed: cancelled". A
MusicBrainz request that is already open cannot be interrupted, so the cancel lands when that request returns.
**Dismiss** removes a failed job, which is otherwise kept until you have seen it.

### Move and rename

Anything filed in the wrong place can be moved from the Library tab: tick tracks and use **Move selected**, use
**Move** on a single row, **Move album** to hand an album to another artist, or **Rename** an artist. The dialog's
Artist and Album fields suggest what the library already has and accept a new name, which creates the folder;
leaving Album blank files the track loose under the artist as a Single. A move rewrites `ALBUMARTIST`, `ARTIST` and
`ALBUM` to match the new folders and leaves every other tag alone, folders left without audio are removed, and a
move is all-or-nothing — if anything is already in the way the whole move is refused and the conflicting paths are
listed in the dialog.

### Delete and the trash

Delete is offered on a track, a multi-selection of tracks, a whole album, or a whole artist. One dialog names
what is going and how many tracks it holds. Nothing is erased: the item moves, as a single entry, to
`DOWNLOAD_PATH/.trash/<UTC timestamp>/<its original path>`. That is a rename on the same filesystem, so it is
instant whatever the size, and an album or artist keeps its folder intact, `cover.jpg` included. Folders left
without audio are cleaned up afterwards, as they are after a move.

The **Trash** tab appears with a count as soon as something is in it. Each entry lists its original path, when it
was deleted, and its track count, with **Restore** to put it back where it came from. Restore never overwrites:
if something has taken the old path in the meantime the restore is refused and the move dialog opens so you can
file the entry elsewhere. **Empty trash** confirms with the total, then removes the contents for good.

Nothing in the trash expires on its own — it sits there until you empty it, and it is invisible everywhere else.
It never shows in the Library tab, and a trashed track no longer counts as a duplicate, so you can download it
again without the "already in library" refusal. Deleting something an in-flight job is about to write is refused
until that job finishes. Navidrome and Lidarr both skip `.trash`; see below.

### Update metadata

**Update metadata** is offered on a track row and on an album header. Both queue a **tagging** job, which appears in
the in-flight queue next to the downloads, can be cancelled while it runs, retried when it fails, and dismissed
afterwards. There is deliberately no per-artist or whole-library button.

A track run redoes the fix a download does automatically: MusicBrainz is asked about the cleaned title, the artist
folder and the duration, and on a confident match `TITLE` and `ARTIST` are rewritten. An album run looks every track
in the folder up and shows its progress as *N of M*. When all of them match and all of them map to one believable
MusicBrainz release — not a bootleg, not a tribute, not a greatest-hits compilation, all of which carry the same
recordings the album does — it also writes `TRACKNUMBER` and `DISCNUMBER` from that release's tracklist and fetches
`cover.jpg` from the Cover Art Archive — the release's front image, else the release group's. Otherwise it applies
the per-track fixes on their own, writes no numbers and no cover, and the job says `partial: 9 of 12`.

Finding that one release takes two goes. MusicBrainz lists a popular recording once per release that duplicated it,
so on a heavily duplicated album no single release shows up in *every* track's search results. When every track
matched but no believable release is common to all of them, the pass ranks the releases the folder points at — by
the same three tests, dropping anything credited to another artist, anything that is not an official release, and
anything the release group calls a compilation — and reads up to three of those tracklists, taking the first that
holds every track in the folder at the same title/length/artist bar. One of those three that cannot be read is
skipped rather than failing the job. That costs one to three extra MusicBrainz requests, and none at all on a
folder the first go already resolved.

An existing `cover.jpg` (or `folder.jpg`) is never overwritten, embedded artwork is left alone, no MusicBrainz id is
ever written, and `ALBUM`/`ALBUMARTIST` stay whatever the folders say. A loose Single gets title and artist only:
never a track number, never an `ALBUM` tag, never cover art. Non-FLAC files count towards the album's total but are
never touched — and because they can never match, a single non-FLAC file in a folder keeps that album off the
numbers-and-cover path for good; it always reports `partial: 11 of 12`. Nothing is remembered about what matched, so
running the pass again asks again.

Unlike a download — which always finishes, saying "tags not fixed" in its detail when the lookup failed — a tagging
job whose whole purpose was to fix tags **fails** when MusicBrainz cannot be reached, when the lookup times out, or
when a file cannot be written, and stays in the queue with a Retry and a Dismiss. "No match" is not a failure: the
job finishes with `tags not fixed: no match`. Cancelling mid-album leaves the tracks already written as they are and
the rest untouched. Tagging a folder an in-flight download is about to write into is refused until that job
finishes, as a move or a delete would be.

## Architecture

```
Browser (HTTPS) ──> Reverse Proxy ──> nginx (:3033)
                                        ├── /         -> serve React SPA
                                        └── /api/*    -> proxy to backend (:8000)
```

The frontend's nginx container proxies all `/api/` requests to the backend container over the internal Docker network. The backend is not exposed to the host — all traffic flows through the frontend. This allows the application to work behind an HTTPS reverse proxy without mixed-content issues.

## Tech Stack

| Layer    | Technology                                       |
| -------- | ------------------------------------------------ |
| Frontend | React 19, TypeScript, Vite, TanStack Query, Tailwind, shadcn/ui |
| Backend  | Python 3.12, FastAPI, yt-dlp, ffmpeg, Mutagen    |
| Infra    | Docker Compose, nginx                            |

## Getting Started (development)

This is the path for working on the code locally. It builds both images from
source: `docker-compose.override.yml` is merged automatically by Compose and
supplies the `build:` blocks.

```bash
# Clone the repository
git clone <repo-url> && cd music-for-arr

# Configure environment
cp .env.example .env
# Edit .env with your settings (see Configuration below)

# Build and start the stack
docker compose up --build -d
```

The application is available at `http://localhost:3033` (configurable via `FRONTEND_PORT`). Place a reverse proxy in front for HTTPS access.

## Deploying to the homelab

The homelab runs the prebuilt images that CI publishes to GHCR. It never builds
anything, and it never needs a checkout of this repository.

**Images are private.** They live at `ghcr.io/architdharod/music-for-arr-backend`
and `ghcr.io/architdharod/music-for-arr-frontend`, so the host must authenticate
before it can pull.

1. Create a fine-grained personal access token on GitHub under
   *Settings > Developer settings > Personal access tokens > Fine-grained tokens*.
   Give it access to this repository and the single permission
   **Packages: Read-only** (`read:packages`). Nothing else is required.
2. Log in on the homelab host:

   ```bash
   docker login ghcr.io -u <github-user>
   # paste the token as the password
   ```

3. Copy **only** `docker-compose.yml` and your `.env` to the host. Do not clone
   the repo, and do not copy `docker-compose.override.yml` — its `build:` blocks
   would make Compose try to build from sources that are not there.
4. First start:

   ```bash
   docker compose pull && docker compose up -d
   ```

Updating is the same two commands. Every push to `main` republishes `latest`,
and a scheduled job rebuilds both images every Monday morning so `latest` always
carries a recent yt-dlp — the pin in `backend/requirements.txt` is a floor
(`yt-dlp>=...`), so each rebuild picks up the newest release. That weekly rebuild
is the mechanism that keeps downloads working when YouTube changes something.

**Rollback.** Every build is also tagged immutably: `sha-<short commit sha>` for
pushes to `main`, and `weekly-<YYYYMMDD>` for the Monday rebuilds. To go back,
edit the `image:` line in `docker-compose.yml` on the host to the tag you want
and re-run `docker compose up -d`:

```yaml
image: ghcr.io/architdharod/music-for-arr-backend:weekly-20260817
```

**Data.** The backend gets a named volume `music-for-arr-data` mounted at
`/config`, pointed at by `DATA_PATH`. This is where `queue.db` lives: the job
queue is persisted to SQLite, so restarting or updating the backend does not
lose it. Queued jobs come back queued; a job that was mid-download when the
process stopped has its partial files cleaned up and is retried automatically
(three interrupted attempts and it is marked failed instead). Finished and
cancelled jobs are pruned after seven days; failed ones stay until dismissed.
The volume is separate from `DOWNLOAD_PATH` on purpose, so app state never sits
in the tree Navidrome and Lidarr scan. Downloads themselves are unaffected:
they go to the `DOWNLOAD_PATH` bind mount.

## Configuration

All configuration is via environment variables in `.env`:

| Variable                   | Default                 | Description                                              |
| -------------------------- | ----------------------- | -------------------------------------------------------- |
| `FRONTEND_PORT`            | `3033`                  | Host port for the web UI                                 |
| `DOWNLOAD_PATH`            | `/data/music/downloads` | Directory where FLAC files are saved                     |
| `DOWNLOAD_TIMEOUT_SECONDS` | `900`                   | Per-job timeout in seconds (15 min)                      |
| `DATA_PATH`                | `/config`               | Directory holding `queue.db`, the persistent job queue; leave as-is under compose, it is a named volume |
| `MAX_CONCURRENT_DOWNLOADS` | `2`                     | Maximum simultaneous downloads                           |
| `PROBE_TIMEOUT_SECONDS`    | `120`                   | How long `POST /download/probe` may spend enumerating a collection before it answers 504 |
| `PUID`                     | `1000`                  | UID the backend container runs as                        |
| `PGID`                     | `1000`                  | GID the backend container runs as                        |
| `CORS_ORIGINS`             | unset                   | Dev only: comma-separated browser origins allowed to call the API directly. Not needed for the compose stack, where the UI is same-origin behind nginx. |
| `NAVIDROME_URL`            | empty (disabled)        | Navidrome base URL, e.g. `http://navidrome:4533`, without a trailing `/rest` |
| `NAVIDROME_USER`           | empty (disabled)        | Navidrome user to scan as; must be an admin                             |
| `NAVIDROME_PASSWORD`       | empty (disabled)        | That user's password; the app derives a token and salt per request      |
| `LIDARR_URL`               | empty (disabled)        | Lidarr base URL, e.g. `http://lidarr:8686`                              |
| `LIDARR_API_KEY`           | empty (disabled)        | Lidarr API key, from Settings > General                                 |
| `LIDARR_ROOT_FOLDER`       | Lidarr's first          | The root folder to rescan, as Lidarr sees it                            |
| `TAG_FIX_ENABLED`          | `true`                  | Look every finished download up on MusicBrainz and correct its `TITLE`/`ARTIST`. `false` skips the lookup, and no request leaves the container |
| `MUSICBRAINZ_CONTACT`      | this repository's URL   | Contact (email or URL) in the User-Agent MusicBrainz requires           |
| `TAG_FIX_TIMEOUT_SECONDS`  | `60`                    | How long one tag lookup may take before the job finishes without it     |

These are the effective defaults: `docker-compose.yml` substitutes them when a
variable is unset or empty, so an incomplete `.env` no longer breaks the stack.
`.env.example` ships the same values.

`DOWNLOAD_PATH` must already exist on the host and be writable by `PUID:PGID`
(default `1000:1000`). The backend checks this at startup and **refuses to
start** if the directory is missing or not writable, rather than failing on the
first download. If Docker creates the directory for you it will be owned by
`root`, so create it yourself and `chown` it first.

`DATA_PATH` (the queue database) is checked the same way. The backend image
pre-creates `/config` world-writable, so the named volume compose mounts there
inherits that mode and is writable whatever `PUID:PGID` you run as. If you
replace the named volume with a bind mount of a host directory, that directory
must exist and be writable by `PUID:PGID` yourself.

## Navidrome and Lidarr

After every change to the library the backend waits five seconds for the writing
to stop, touches the album folders that changed, and then asks Navidrome and
Lidarr to rescan — once, however many tracks landed. Each service is skipped
when its variables are empty, and a failure never fails a download: it shows as
a dismissible banner in the UI and a line in the log.

**Navidrome.** `startScan` is admin-only, so `NAVIDROME_USER` must be an admin;
create a dedicated user for it rather than reusing your own. Set
`ND_SCANNER_PURGEMISSING=always` on the Navidrome container (Navidrome 0.56.0 or
newer), or tracks this app deletes stay in Navidrome's database as unplayable
entries. The trash needs no configuration of its own: Navidrome skips dot-prefixed
folders, and the app drops an empty `.ndignore` file into `.trash` as well, so the
folder stays skipped even if that behaviour ever changes. Navidrome's own
filesystem watcher usually notices new files by itself; the explicit scan is
insurance for the setups where it does not (network mounts, some bind mounts).

**Lidarr.** The rescan is sent with `filter=known` and `addNewArtists=false`, so
only artists Lidarr already tracks are rescanned and nothing this app downloads
is added to Lidarr's library behind your back. Three things on the Lidarr side are
worth knowing:

- Albums you delete here that Lidarr monitors will be searched for again.
  Unmonitor the album in Lidarr if you meant the delete to stick.
- The trash needs no configuration either, and Lidarr has no folder ignore list to
  put it in. Lidarr's scanner skips any dot-prefixed folder beneath a root folder,
  so it never sees `.trash`. Verified against Lidarr **3.1.0.4875** with two
  identical FLAC files, one in the library and one under `.trash`: every
  `RescanFolders` found one file and the trashed path appears nowhere in the logs.
  The test and the source references are in
  `wayfinder/research/lidarr-hidden-folders.md`.
- Leave *Settings > Metadata > Tag Audio Files with Metadata* at `no` or
  `newFiles`, and leave *Scrub Existing Tags* off. Scrubbing strips the
  `SOURCEID` and `SOURCEURL` tags this app writes, which is what its duplicate
  detection reads. The app checks this at startup and again after each rescan,
  and shows a banner while scrubbing is on; turn it off and the banner clears
  after the next rescan.

## Limitations

- **The collection preview is a flat extraction** — it is cheap, and it is thin: a row can only be marked unavailable when the flat pass says so. SoundCloud DRM is invisible to it (yt-dlp only meets the DRM in a full extraction), so such a track previews as available and then fails in its own child job with yt-dlp's DRM message. `POST /download` itself still takes a single track and rejects playlist and channel URLs; collections go through `POST /download/probe` and `POST /download/bulk`.
- **YouTube, SoundCloud and Bandcamp only** — enforced: the backend accepts `http`/`https` URLs on `youtube.com`, `youtu.be`, `soundcloud.com`, `bandcamp.com` and their subdomains, and rejects everything else with a validation error. No Spotify or other sources.
- **No duplicate submissions** — a URL that is already queued or in progress is refused until that job finishes.
- **Never overwrites** — a download whose target `Artist/Album/track.flac` already exists is stopped and shown as "already in library"; nothing in the library is replaced. A move onto an occupied path is refused the same way.
- **No authentication** — designed for private/internal networks.
- **FLAC only** — lossy sources are losslessly wrapped in FLAC for consistent output. Only FLAC files are tagged.
- **Text-search tagging only** — the automatic fix asks MusicBrainz by title, artist and duration; there is no
  audio fingerprinting, so a live version, a cover, or a sped-up upload is left with the tags it came with. Album
  track numbers and cover art are never part of the *automatic* pass; they come only from the album's
  **Update metadata** button.
