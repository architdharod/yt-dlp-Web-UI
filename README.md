# yt-dlp Web UI

This is a project for educational purpose, to learn the usage of the library yt-dlp to test download of royalty free content from different sources.

## Screenshot

![Web UI Screenshot](web%20ui.png)

## How It Works

1. Paste a YouTube or SoundCloud URL into the web UI, optionally specifying artist and album names.
2. The backend extracts metadata (title, thumbnail, duration) via yt-dlp and returns it immediately.
3. The job enters an async queue. yt-dlp downloads the audio, ffmpeg converts it to FLAC, and metadata is embedded.
4. Files are saved to `DOWNLOAD_PATH/Artist/Album/track.flac`, falling back to "Unknown Artist"/"Unknown Album" when metadata isn't available.
Real-time progress is streamed to the browser via Server-Sent Events — no polling.

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
| Frontend | React 18, TypeScript, Vite, Tailwind, shadcn/ui  |
| Backend  | Python 3.12, FastAPI, yt-dlp, ffmpeg, Mutagen    |
| Infra    | Docker Compose, nginx                            |

## Getting Started

```bash
# Clone the repository
git clone <repo-url> && cd yt-dlp-web-ui

# Configure environment
cp .env.example .env
# Edit .env with your settings (see Configuration below)

# Start the stack
docker compose up -d
```

The application is available at `http://localhost:3033` (configurable via `FRONTEND_PORT`). Place a reverse proxy in front for HTTPS access.

## Configuration

All configuration is via environment variables in `.env`:

| Variable                   | Default                 | Description                                |
| -------------------------- | ----------------------- | ------------------------------------------ |
| `FRONTEND_PORT`            | `3033`                  | Host port for the web UI                   |
| `DOWNLOAD_PATH`            | `/data/music/downloads` | Directory where FLAC files are saved       |
| `DOWNLOAD_TIMEOUT_SECONDS` | `900`                   | Per-job timeout in seconds (15 min default)|
| `MAX_CONCURRENT_DOWNLOADS` | `2`                     | Maximum simultaneous downloads             |

The `DOWNLOAD_PATH` must be writable by the backend container. The container runs as UID/GID 1000 by default.

## Limitations

- **Single tracks only** — no playlist or album URL support.
- **YouTube and SoundCloud only** — no Spotify, Bandcamp, or other sources.
- **No authentication** — designed for private/internal networks.
- **No persistent queue** — job state lives in memory; restarting the backend clears it.
- **FLAC only** — lossy sources are losslessly wrapped in FLAC for consistent output.
