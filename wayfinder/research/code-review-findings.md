# Code review findings

Resolves `wayfinder/tickets/01-code-review-findings.md`. Every finding below was verified by reading the code; the ones marked "(verified by script)" were also reproduced with a throwaway script against the real backend modules (backend test suite: 145 passed on Python 3.10 with `requirements-dev.txt`).

Line numbers refer to the files as of commit `8deb9cc`.

## Ranked findings

### 1. critical — Path traversal / arbitrary write location via `artist` and `album`
- **Where:** `backend/app/file_organizer.py:67` (`Path(download_path) / artist / album / track_filename`), fed from `backend/app/downloader.py:171-178` and `backend/app/main.py:176-177`.
- **What:** User-supplied `artist`/`album` go straight into the path. `pathlib` does not normalise `..`, and joining an absolute component *replaces* the base. `download_audio` then does `target_dir.mkdir(parents=True)` and yt-dlp writes there. Only whitespace is stripped (`_resolve`, lines 33-38); `sanitize_filename` is applied to the title only, never to artist/album.
- **Scenario (verified by script):** `artist="../../etc"`, `album="cron.d"` → `/data/music/../../etc/cron.d/t.flac` (= `/etc/cron.d/t.flac`); `artist="/tmp/evil"` → `/tmp/evil/x/t.flac`. Inside the container the process is uid 1000 so `/etc` is not writable, but anything the bind-mounted volume or uid 1000 can reach is, and `mkdir -p` creates arbitrary directory trees. The tests in `test_file_organizer.py` never exercise separators or `..`.
- **Fix:** Run `yt_dlp.utils.sanitize_filename(value, restricted=False)` on artist and album (it turns `/` into `⧸` and leaves `..` harmless as a name), then assert `output.resolve().is_relative_to(Path(download_path).resolve())` and raise `DownloadError` otherwise.

### 2. high — yt-dlp output-template injection through the track title (and artist/album)
- **Where:** `backend/app/downloader.py:169,193` — `outtmpl = str(output_path.with_suffix(".%(ext)s"))`.
- **What:** The whole path (download root, artist, album, sanitised title) is passed to yt-dlp as a *template*, so any `%` in it is interpreted. `sanitize_filename` does not touch `%`.
- **Scenario (verified by script):** a video titled `Track %(title)s` produces the file `Track <title>.webm`; a title or album containing `%(id)s`, `%(uploader)s`, or a stray `%` followed by an unexpected character either writes to an unintended name or makes yt-dlp fail with a template error. Titles with `%` ("100% Love") are common on YouTube.
- **Fix:** Escape literal percent signs before building the template (`str(path).replace("%", "%%")`), or better, pass `outtmpl` as `{"default": ...}` with `paths` set and let yt-dlp own the template while you own the directory. Add a test with `%` in title/artist/album.

### 3. high — Timeout does not stop the download thread; it keeps writing files and emitting events for an `error` job, and frees the concurrency slot while still running
- **Where:** `backend/app/queue_manager.py:148-164,186-191`.
- **What:** `asyncio.wait_for` cancels the awaiting task, but `run_in_executor` cannot cancel the yt-dlp thread. The `async with self._semaphore` block exits, so a new job starts while the old one keeps downloading and converting.
- **Scenario (verified by script):** with `timeout=1` and a 3 s download, the job is `ERROR` after 1 s, the thread is still alive, and the `on_progress` closure keeps emitting `progress` events with `data.status="error"` and increasing `progress` (5 more events observed). The UI ends up with an "Error" badge whose retry button is enabled; pressing Retry starts a second yt-dlp run into the *same* output path while the zombie is still writing to it (two ffmpeg processes on one file). With `MAX_CONCURRENT_DOWNLOADS=2` and a few timeouts, real concurrency is unbounded (only limited by the default executor's thread count), and a stuck thread also blocks graceful shutdown (`loop.shutdown_default_executor`).
- **Fix:** Give yt-dlp a cancel signal (a `threading.Event` checked in the progress hook that raises `yt_dlp.utils.DownloadCancelled`), set it on timeout, and ignore `on_progress` calls once the job is no longer `DOWNLOADING`. Reject retry while a previous run for the job is still alive.

### 4. high — Playlist URLs are downloaded in full, into one file
- **Where:** `backend/app/downloader.py:60-64,144,191-207` — no `noplaylist: True` anywhere.
- **What:** README says "single tracks only", but nothing enforces it. A YouTube URL copied while a playlist/mix is open (`watch?v=...&list=...`) makes yt-dlp iterate the whole list.
- **Scenario:** `extract_info` returns a playlist dict (`title` is the playlist name, no `duration`), the job shows the playlist title, then `ydl.download` downloads every entry to the same `outtmpl` (each `.flac` overwrites the previous), progress jumps 0→100 repeatedly, and the job usually ends in "timed out". `/download` itself blocks while yt-dlp resolves every entry, which with nginx's 60 s `proxy_read_timeout` yields a 504 (see finding 8).
- **Fix:** Add `"noplaylist": True` and `"extract_flat": False` to all three `YoutubeDL` option dicts; reject `info.get("_type") == "playlist"` with a clear `DownloadError`.

### 5. high — Any `.env` that omits a variable crashes the backend at import time
- **Where:** `docker-compose.yml:29-31` (`${DOWNLOAD_TIMEOUT_SECONDS}` with no `:-default`), `backend/app/queue_manager.py:51-58` (`int(os.environ.get(...))`), `backend/app/main.py:86` (singleton built at import).
- **What:** Compose substitutes an unset variable with an empty string, so the env var *exists* but is `""`. `os.environ.get("X", default)` returns `""`, and `int("")` raises.
- **Scenario (verified by script):** `ValueError: invalid literal for int() with base 10: ''` on `uvicorn` start; the container restart-loops (`restart: unless-stopped`) with no hint about which variable. An empty `DOWNLOAD_PATH` produces an invalid `:` volume spec and a compose error. The README's "Default" column (900 / 2) is therefore misleading: those defaults are never reached through compose.
- **Fix:** Use `${DOWNLOAD_TIMEOUT_SECONDS:-900}`, `${MAX_CONCURRENT_DOWNLOADS:-2}`, `${DOWNLOAD_PATH:-/data/music/downloads}` in compose, and make the Python side treat empty strings as unset (`os.environ.get("X") or default`).

### 6. high — SSE events for unknown job IDs are dropped, so the UI drifts from `GET /queue`
- **Where:** `frontend/src/hooks/useSSE.ts:23-24` (`if (idx === -1) return prev;`), `frontend/src/hooks/useSSE.ts:81-83` (`addJob` only from the local form), no reconciliation anywhere.
- **What:** `GET /queue` is fetched exactly once on mount; afterwards only events for jobs already in local state are applied. The backend never sends a "job created" event.
- **Scenarios:**
  - Two tabs/devices: jobs submitted in tab A never appear in tab B until a reload.
  - Slow metadata extraction: nginx times out the `POST /download` after 60 s (`proxy_read_timeout` default; `frontend/nginx.conf:8-22` sets none) → browser shows "Download submission failed", but the backend still enqueues and downloads the job; it is invisible in the UI. Same for any transient fetch failure after the server accepted the job.
  - Race on submit: `_process_job` is scheduled with `create_task` inside the request handler (`queue_manager.py:88`), so the `downloading` `status_change` (and, for a job whose metadata already failed, the `error` event) is emitted in the same loop tick as the response is flushed. If the SSE frame reaches the browser before the fetch promise resolves, it is dropped and the card is created as `queued`; for a fast failure that card stays "Queued" forever with no retry button.
  - `EventSource` auto-reconnects after a drop, but events during the gap are lost and nothing refetches; `connected`/`error` from the hook are never rendered and `connected` is never set back to `true` (no `onopen`), so there is no indication either.
- **Fix:** On an unknown `job_id`, or on `EventSource` `onopen` after an error, call `getQueue()` and replace state; optionally have the backend emit a `job_added` event carrying the full job (or have every event carry the full job snapshot).

### 7. medium — Slow SSE clients silently lose `status_change`/`error` events because progress events share the same bounded queue
- **Where:** `backend/app/main.py:48-55,197` (`maxsize=256`, `put_nowait` drop), `backend/app/queue_manager.py:186-188` (one event per yt-dlp hook call).
- **What:** yt-dlp fires the progress hook for every chunk, so a fast download produces hundreds of `progress` events per second, each becoming a coroutine on the loop and a JSON frame per client. Once a client's queue is full, *any* event is dropped, including the terminal `done`/`error`.
- **Scenario:** A browser on a slow link or a throttled background tab (Chrome throttles timers/streams for hidden tabs) fills its 256-slot queue during a large download; the `converting`/`done` events are dropped with only a server-side warning, and the card shows "Downloading 100%" forever (no reconciliation, see 6).
- **Fix:** Throttle progress in `on_progress` (emit only when the integer percentage changes or every ~250 ms); when a queue overflows, put a `resync` marker in it instead of dropping, and have the client refetch `/queue` on that event.

### 8. medium — `POST /download` has no timeout and runs network I/O inline; nginx cuts it at 60 s
- **Where:** `backend/app/main.py:163`, `backend/app/downloader.py:60-68` (no `socket_timeout`, default retries), `frontend/nginx.conf:8-22` (no `proxy_read_timeout`).
- **Scenario:** An unreachable host, a slow generic-extractor page, or a playlist URL keeps `extract_metadata` busy for minutes (yt-dlp retries 10× by default). nginx returns 504, the browser shows an error, and the job is still enqueued when the call eventually returns (feeds finding 6). Meanwhile the default executor thread is occupied, and the same pool is shared with downloads, so several bad submissions can starve real downloads.
- **Fix:** `asyncio.wait_for(loop.run_in_executor(...), 20)` around extraction with `socket_timeout` and `extractor_retries`/`retries` set low for the probe; on timeout enqueue without metadata (already supported) and return immediately.

### 9. medium — Server-side request forgery / arbitrary-site download via the generic extractor
- **Where:** `backend/app/models.py:26` (`url: str`, only `min_length=1`), `backend/app/downloader.py:67-68`.
- **What:** Any URL is handed to yt-dlp, whose generic extractor fetches it from inside the container network. README claims "YouTube and SoundCloud only".
- **Scenario:** `http://yt-dlp-web-ui-backend:8000/health`, `http://192.168.1.1/`, cloud metadata endpoints, or any internal service: the response is fetched and the error message (which includes the yt-dlp error text and URL) is returned in the job's `error` field. `file://` URLs are refused by yt-dlp by default, so local file read is not exposed.
- **Fix:** Validate `url` with a Pydantic `HttpUrl` and an allowlist of hosts (`youtube.com`, `youtu.be`, `music.youtube.com`, `soundcloud.com`, `on.soundcloud.com`); pass `allowed_extractors: ["youtube", "soundcloud.*"]` to yt-dlp as a second layer.

### 10. medium — `CONVERTING` is a fake state; the UI shows "Downloading 100%" during the real ffmpeg conversion
- **Where:** `backend/app/queue_manager.py:148-157`.
- **What:** `_update_status(CONVERTING)` runs *after* `wait_for` returns, i.e. after yt-dlp has downloaded, converted to FLAC, embedded metadata and thumbnail. The two `status_change` events are emitted back-to-back with no await in between; clients see "converting" for a single frame if at all. The state-machine docstring, `test_queue_manager.py:155`, and the frontend all treat it as real.
- **Scenario:** A 2-hour DJ set: download finishes in 30 s, FLAC encode takes minutes. The card shows "Downloading 100%" the whole time, which looks hung.
- **Fix:** Add a `postprocessor_hooks` entry in `download_audio` and call a second callback (`on_phase("converting")`) when a hook with `status == "started"` and `postprocessor == "FFmpegExtractAudio"` fires; have `QueueManager` set `CONVERTING` from that hook and drop the fake transition.

### 11. medium — `error`/`done` jobs created without metadata show "Loading metadata..." forever
- **Where:** `backend/app/main.py:167-168` (extraction failure tolerated), `backend/app/downloader.py:151-160` (title/artist re-extracted but never written back to `job`), `backend/app/queue_manager.py:207-217` (`_emit_event` only sends status/progress/error), `frontend/src/components/QueueDisplay.tsx:126` (null title → "Loading metadata...").
- **What:** The backend never emits the `metadata` event the frontend handles (`useSSE.ts:62-72`), and `download_audio` discards the title/artist/album it resolves.
- **Scenario:** Metadata probe fails transiently during `POST /download`, the download later succeeds; the card reads "Loading metadata... --:-- Done" indefinitely, and `GET /queue` returns `title: null` too.
- **Fix:** In `download_audio`, set `job.title/duration/thumbnail_url` when `info` is available and have `QueueManager` emit a `metadata` event (or send full job snapshots in every event).

### 12. medium — SSE client leak when disconnect coincides with a broadcast
- **Where:** `backend/app/main.py:209-213`.
- **What:** The `finally` block acquires `_sse_clients_lock` with `async with` while the task is being cancelled (sse-starlette cancels the generator on client disconnect). If the lock is contended at that moment (a `_broadcast_event` is mid-iteration, which happens many times per second during a download), the awaited `acquire()` re-raises `CancelledError` and `_sse_clients.remove` never runs.
- **Scenario:** Browser tab closes during a download; the orphan queue stays in `_sse_clients`, fills to 256, and every subsequent broadcast logs "SSE client queue full" forever. Memory and log growth over weeks of uptime.
- **Fix:** Use `asyncio.shield` around the cleanup, or avoid the lock entirely (append/remove on a `set` from the loop thread is already atomic; the lock protects nothing that `put_nowait` needs).

### 13. medium — Docker: `user: 1000:1000` with no matching user, no `HOME`, unchecked bind-mount ownership, no `depends_on`
- **Where:** `docker-compose.yml:26,32-33`, `backend/Dockerfile` (no `USER`, no `HEALTHCHECK`), `frontend/nginx.conf:9` (`proxy_pass http://yt-dlp-web-ui-backend:8000/`).
- **What / scenarios:**
  - If `DOWNLOAD_PATH` does not exist on the host, Docker creates it owned by root:root 755; the backend logs a warning at startup (`main.py:112-117`) but every download then dies with `Unexpected error: [Errno 13] Permission denied` from `mkdir` (`downloader.py:183`), which is a `PermissionError`, not a `DownloadError`, so the log line is a full traceback per job.
  - uid 1000 has no passwd entry in `python:3.12-slim`, so `HOME` is unset/`/`; yt-dlp cannot write its cache (`~/.cache/yt-dlp`) and warns on every run; ffmpeg/ytdlp temp files fall back to the output dir. Harmless but noisy.
  - nginx resolves the upstream hostname at config-load time. Without `depends_on: [backend]`, if the frontend container starts first it exits with "host not found in upstream" and relies on `restart: unless-stopped` to recover; if the backend is ever renamed the frontend breaks silently.
  - UID/GID are hard-coded; NAS setups commonly need PUID/PGID.
- **Fix:** `depends_on: backend`; make uid/gid `${PUID:-1000}:${PGID:-1000}`; set `HOME=/tmp` (or create the user in the Dockerfile); add a startup check that *fails fast* if `DOWNLOAD_PATH` is not writable rather than logging a warning; add a `HEALTHCHECK` hitting `/health`.

### 14. medium — Dependency pinning is inconsistent and yt-dlp goes stale inside the image
- **Where:** `backend/requirements.txt:3-4` (`yt-dlp>=2024.12.23`, `mutagen>=1.47.0` are floors, everything else is `==`), `frontend/Dockerfile:10-11` (`npm install`, `package-lock.json*` optional), `frontend/package.json` (all caret ranges; `shadcn` CLI in runtime `dependencies`).
- **What:** yt-dlp breaks against YouTube every few weeks and is only refreshed when the image is rebuilt without cache; there is no "update yt-dlp" path at runtime and the version is not logged. Frontend builds are not reproducible (`npm install` may rewrite the lockfile; `node:20-alpine` is unpinned).
- **Scenario:** Six weeks after deploy every job fails with `Failed to extract metadata: ... Sign in to confirm you're not a bot` and the operator has no indication that a rebuild fixes it.
- **Fix:** Pin `yt-dlp==<date>` and bump it on a schedule (Renovate/Dependabot), log `yt_dlp.version.__version__` at startup, and use `npm ci` with the lockfile required. Move `shadcn` to devDependencies.

### 15. low — Missing `uvicorn` proxy-header trust, so logs show the nginx container IP
- **Where:** `backend/Dockerfile:20` (no `--forwarded-allow-ips`), `frontend/nginx.conf:11-13` sets the headers.
- **What:** uvicorn's `proxy_headers` defaults to on but `forwarded_allow_ips` defaults to `127.0.0.1`; the nginx container is not on loopback, so `X-Forwarded-For` is ignored. Only affects access-log client IPs and `request.client`.
- **Fix:** `--forwarded-allow-ips='*'` (or the compose network CIDR) in the `CMD`, or `FORWARDED_ALLOW_IPS` env.

### 16. low — CORS wide open with `allow_credentials=True`
- **Where:** `backend/app/main.py:127-133`.
- **What:** Irrelevant while the backend is only reachable through nginx (same origin), but if a `ports:` line is ever added to the backend service, any website can drive the API from a visitor's browser (no auth; see README). Starlette handles `*` + credentials by echoing the origin, which is the worst combination.
- **Fix:** Drop the CORS middleware (same-origin via nginx) or restrict `allow_origins` to an env-configured list and set `allow_credentials=False`.

### 17. low — Unbounded in-memory growth (jobs and events)
- **Where:** `backend/app/queue_manager.py:63,79` (`_jobs` never pruned), `frontend/src/hooks/useSSE.ts:15`.
- **Scenario:** A long-running instance accumulates thousands of `done` jobs; every `GET /queue` returns all of them and the React list renders all of them (no virtualisation, sorted every render).
- **Fix:** Keep the last N (e.g. 200) terminal jobs, or prune `done` jobs older than 24 h.

### 18. low — Same-title collisions and duplicate submissions overwrite each other
- **Where:** `backend/app/downloader.py:169-178,193`.
- **Scenario:** Two different tracks with the same title (very common: "Intro", "Untitled") under the same artist/album, or the same URL submitted twice, target the same `.flac`; concurrent runs write the same file at the same time.
- **Fix:** De-duplicate in-flight URLs in `add_job`, and include the yt-dlp `id` in the filename or refuse to overwrite an existing `.flac` (return it as `done` with a note).

### 19. low — Error surface gaps in `download_audio`
- **Where:** `backend/app/downloader.py:152,169,213-215,217-226`.
- **What:** `info.get("title", "Unknown Title")` returns `None` when the key exists with a null value → `sanitize_filename(None)` raises `TypeError`. Only `yt_dlp.utils.DownloadError` is caught; `OSError`/`PermissionError`/`ENAMETOOLONG` from long titles surface as "Unexpected error" with a traceback. "Download reported success but output file not found" is logged as a warning while the job is marked `done`.
- **Fix:** `info.get("title") or "Unknown Title"`; catch `yt_dlp.utils.YoutubeDLError` and `OSError` and wrap them; treat a missing output file as `DownloadError`.

### 20. low — nginx config nits
- **Where:** `frontend/nginx.conf:20-21,24-32`.
- **What:** `add_header X-Accel-Buffering no` is a *response* header nginx reads from the upstream, adding it to the client response does nothing; `chunked_transfer_encoding off` is unnecessary for SSE. `index.html` has no `Cache-Control: no-cache`, so after a redeploy browsers can keep a stale shell pointing at hashed assets that no longer exist (the `/assets/` block caches 1y, correctly, but the shell should not be cached). `proxy_read_timeout` is not set for the SSE location (sse-starlette pings every 15 s so the stream survives, but `POST /download` does not, see 8).
- **Fix:** Remove the two no-op lines; add `location = /index.html { add_header Cache-Control "no-cache"; }`; set `proxy_read_timeout 300s` on `/api/`.

### 21. low — README inaccuracies
- **Where:** `README.md:39,55-62,66-67`.
- **What:** `cd yt-dlp-web-ui` but the repo is `music-for-arr`; the Default column says 900 s / 2 while `.env.example` ships 180 s / 5 and compose passes no fallback (finding 5); "Single tracks only" and "YouTube and SoundCloud only" describe intent, not behaviour (findings 4, 9); "The container runs as UID/GID 1000 by default" is a compose setting, not a Dockerfile one, and the ownership requirement of `DOWNLOAD_PATH` is not explained (finding 13). `docker-compose.yml:12-19` carries `homepage.*` labels with personal hostnames that are not mentioned anywhere.
- **Fix:** Correct the clone path, document `.env.example` as the effective defaults, and either implement the limits or word them as "not tested/enforced".

### 22. low — Dead code and stale comments
- `frontend/src/hooks/useSSE.ts:16-17,150` — `connected` and `error` are computed and returned but never rendered (`main.tsx:9` ignores them).
- `frontend/src/hooks/useSSE.ts:62-72` and `frontend/src/lib/api.ts:69` — `metadata` event handler for an event the backend never sends.
- `backend/app/queue_manager.py:41-42` — docstring says "Task 6 will wire this to the SSE stream" (it is wired).
- `backend/app/queue_manager.py:139` — "in case job was modified (e.g. cancelled)" but there is no cancel API.
- `backend/app/main.py:97,109` — imports inside `lifespan` for no reason.
- `frontend/tsconfig.tsbuildinfo` is committed build output; `frontend/src/lib/api.ts:5` comment mentions Traefik, which is not part of the stack.
- `backend/app/downloader.py:143-145` extracts metadata a second time (third counting `POST /download`), tripling YouTube requests per job; the first probe's result is thrown away except for `title`.

## Cheap rough edges worth fixing

- `queue_manager.py:186-188`: throttle progress events to integer-percentage changes (one-line guard). Cuts SSE traffic by ~100× and largely defuses finding 7.
- `downloader.py`: add `"noplaylist": True` to the three option dicts (finding 4) and `"socket_timeout": 15`.
- `docker-compose.yml`: `:-default` fallbacks and `depends_on` (findings 5, 13).
- `useSSE.ts:24`: on unknown `job_id`, trigger a `getQueue()` refetch instead of `return prev` (fixes the multi-tab and 504 cases of finding 6 in ~5 lines).
- `QueueDisplay.tsx:9-20`: `queued` jobs are sorted among `done` ones by date; put them in the "active" group so the queue reads top-down.
- `DownloadForm.tsx:49`/`api.ts:22-23`: the error shown to the user is the raw JSON body (`{"detail":[...]}`); parse `detail` when present.
- `downloader.py:153`: `info.get("artist") or info.get("uploader")` yields channel names like "Artist - Topic" or "VEVO" as the artist folder; prefer `info.get("artist") or info.get("creator") or info.get("channel")` and strip the ` - Topic` suffix.
- `main.py:112-117`: turn the DOWNLOAD_PATH writability warning into a hard startup failure (or a degraded `/health`) so misconfiguration is visible before the first job fails.
- Log `yt_dlp.version.__version__` at startup.
- `backend/Dockerfile`: add `HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"`.

## Deliberately not worth fixing

- **No authentication / persistence / cancel API.** All documented as limitations in the README and consistent with a single-user LAN tool. Adding persistence would mean a datastore and migrations for a queue that is fine to lose on restart.
- **Global module state in `main.py` (`_sse_clients`, `_loop`, `queue_manager` singleton).** It makes the routes untestable in isolation, but the existing `test_routes.py` already works around it and the app has four endpoints. Refactoring to app-state/dependency injection is churn without a user-visible payoff.
- **`except asyncio.CancelledError: pass` in the SSE generator (`main.py:209-210`).** Swallowing cancellation is a smell, but the generator returns immediately afterwards so nothing keeps running; fixing finding 12 makes this moot.
- **`chunked_transfer_encoding off` / `X-Accel-Buffering` cargo cult in nginx.** Harmless; only listed under nits.
- **Three separate `extract_info` calls per job.** Wasteful but each is cheap next to the download; merging them requires passing the info dict through the `Job` model. Do it only if YouTube rate-limiting becomes a problem.
- **Committed `tsconfig.tsbuildinfo` and `web ui.png`.** Cosmetic.
- **Pydantic `progress` bounds not enforced on assignment (`models.py:40`).** The hook already clamps to 100 and the frontend clamps again.
- **Executor thread count.** The default (`min(32, cpu+4)`) is plenty for `MAX_CONCURRENT_DOWNLOADS` ≤ 5 plus metadata probes, once findings 3 and 8 stop threads from being stranded.
