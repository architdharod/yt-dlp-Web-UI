# yt-dlp Web UI

This is a project for educational purpose, to learn the usage of the library yt-dlp to test download of royalty free content from different sources.

## Screenshot

![Web UI Screenshot](web%20ui.png)

## How It Works

1. Paste a YouTube or SoundCloud URL into the web UI, optionally specifying artist and album names.
2. The backend extracts metadata (title, thumbnail, duration) via yt-dlp and returns it immediately.
3. The job enters an async queue and runs a three-stage pipeline: yt-dlp fetches the best audio stream and the
   thumbnail (no postprocessing), an ffmpeg subprocess of ours converts that stream to FLAC, and Mutagen writes the
   tags, the cover art, and the `SOURCEID`/`SOURCEURL` fields that record where the track came from.
4. Files are saved to `DOWNLOAD_PATH/Artist/Album/track.flac`, falling back to "Unknown Artist"/"Unknown Album" when metadata isn't available.
Real-time progress is streamed to the browser via Server-Sent Events — no polling. The UI is split into tabs:
**Download** (the form and the in-flight queue, with a badge counting the jobs still working) and **Library**, with a
**Trash** tab that appears once something is in it.

Queue rows carry two actions. **Cancel** stops a job that is queued, downloading or converting: running ffmpeg is our
own process, so it is killed rather than waited out, and every partial and temporary file is removed. Cancelled jobs
leave the queue and are not retried — resubmit the URL instead. **Dismiss** removes a failed job, which is otherwise
kept until you have seen it.

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
| `PUID`                     | `1000`                  | UID the backend container runs as                        |
| `PGID`                     | `1000`                  | GID the backend container runs as                        |
| `CORS_ORIGINS`             | unset                   | Dev only: comma-separated browser origins allowed to call the API directly. Not needed for the compose stack, where the UI is same-origin behind nginx. |
| `NAVIDROME_URL`            | empty (disabled)        | Navidrome base URL, e.g. `http://navidrome:4533`, without a trailing `/rest` |
| `NAVIDROME_USER`           | empty (disabled)        | Navidrome user to scan as; must be an admin                             |
| `NAVIDROME_PASSWORD`       | empty (disabled)        | That user's password; the app derives a token and salt per request      |
| `LIDARR_URL`               | empty (disabled)        | Lidarr base URL, e.g. `http://lidarr:8686`                              |
| `LIDARR_API_KEY`           | empty (disabled)        | Lidarr API key, from Settings > General                                 |
| `LIDARR_ROOT_FOLDER`       | Lidarr's first          | The root folder to rescan, as Lidarr sees it                            |

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
entries. Navidrome's own
filesystem watcher usually notices new files by itself; the explicit scan is
insurance for the setups where it does not (network mounts, some bind mounts).

**Lidarr.** The rescan is sent with `filter=known` and `addNewArtists=false`, so
only artists Lidarr already tracks are rescanned and nothing this app downloads
is added to Lidarr's library behind your back. Two settings on the Lidarr side
matter:

- Albums you delete here that Lidarr monitors will be searched for again.
  Unmonitor the album in Lidarr if you meant the delete to stick.
- Leave *Settings > Metadata > Tag Audio Files with Metadata* at `no` or
  `newFiles`, and leave *Scrub Existing Tags* off. Scrubbing strips the
  `SOURCEID` and `SOURCEURL` tags this app writes, which is what its duplicate
  detection reads. The app checks this at startup and again after each rescan,
  and shows a banner while scrubbing is on; turn it off and the banner clears
  after the next rescan.

## Limitations

- **Single tracks only** — playlist and channel URLs are rejected with an error; only the single track behind a URL is ever downloaded.
- **YouTube and SoundCloud only** — enforced: the backend accepts `http`/`https` URLs on `youtube.com`, `youtu.be`, `soundcloud.com` and their subdomains, and rejects everything else with a validation error. No Spotify, Bandcamp, or other sources.
- **No duplicate submissions** — a URL that is already queued or in progress is refused until that job finishes.
- **Never overwrites** — a download whose target `Artist/Album/track.flac` already exists is stopped and shown as "already in library"; nothing in the library is replaced.
- **No authentication** — designed for private/internal networks.
- **FLAC only** — lossy sources are losslessly wrapped in FLAC for consistent output.
