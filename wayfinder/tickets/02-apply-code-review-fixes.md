# Apply the code review fixes

Label: `wayfinder:task`
Status: closed (2026-09-03)
Assignee: claude (2026-09-03)
Blocked by: 01

## Question

Fix every finding from the code review that is a bug or a security issue, and the rough edges that are cheap. Keep behaviour otherwise unchanged. Backend tests in `backend/tests` must pass; frontend must typecheck and build. Record what was fixed and what was deliberately left, with reasons.

## Resolution

Applied on 2026-09-03 (uncommitted working tree; backend 220 tests pass, up from 145; frontend typechecks and builds).

Fixed, by finding number:
- 1 path traversal: `file_organizer.py` sanitises artist, album, and filename with yt-dlp's `sanitize_filename`, maps `.`/`..`/empty to fallbacks (this yt-dlp leaves `..` untouched, so the guard is explicit), and refuses any resolved path outside `DOWNLOAD_PATH` (`UnsafePathError`, surfaced as a job error).
- 2 template injection: `%` in the output path is escaped to `%%` before it becomes yt-dlp's `outtmpl`.
- 3 zombie thread: each run carries a `threading.Event`; timeout sets it, the progress hook raises `DownloadCancelled`, post-cancel progress is ignored, and retry is refused with a 400 while the old thread is still alive.
- 4 playlists: `noplaylist` everywhere and `_type == "playlist"` rejected with a clear error in both probes.
- 5 compose fallbacks: `${VAR:-default}` in compose, `PUID`/`PGID`, and every env read treats `""` as unset.
- 6 queue drift: every SSE event carries a job snapshot; the frontend refetches `/queue` on an unknown job id and on reconnect, dedupes `addJob`, and shows connection state. No `job_added` event was added; the refetch covers it.
- 7 slow clients: progress emitted only when the whole percentage changes; a full client queue drops the oldest event instead of the newest.
- 8 slow probe: `POST /download` bounds metadata extraction at 20 s (socket timeout 15 s, one retry) and enqueues without metadata on timeout; nginx `proxy_read_timeout 300s`.
- 9 SSRF: `DownloadRequest.url` must be http(s) on youtube.com, youtu.be, soundcloud.com or a subdomain, no IP literals (`ALLOWED_URL_HOSTS` in `models.py`, a one-line change when v2 adds Bandcamp and others); yt-dlp runs with `allowed_extractors=["default","-generic"]`.
- 10 converting: set from yt-dlp's postprocessor hook when `ExtractAudio` starts; the fake transition is gone, so a job may go downloading -> done.
- 11 metadata: `download_audio` backfills title/duration/thumbnail and a `metadata` event is emitted.
- 12 SSE leak: client removal no longer awaits the lock.
- 13 Docker: `depends_on`, `HOME=/tmp`, `HEALTHCHECK`, and the backend refuses to start when `DOWNLOAD_PATH` is missing or not writable.
- 14 pinning: `yt-dlp==2026.8.19`, `mutagen==1.47.0`, `npm ci`, `node:20.19-alpine`, `shadcn` moved to devDependencies, yt-dlp version logged at startup.
- 15 `--forwarded-allow-ips=*`. 16 CORS off by default, `CORS_ORIGINS` env for `vite dev`, credentials off. 17 keeps the newest 200 finished jobs. 18 duplicate in-flight URL -> 409. 19 null title, `YoutubeDLError`/`OSError` wrapped, missing output file is an error. 20 nginx no-ops removed, `index.html` no-cache. 21 README corrected. 22 stale comments and inline imports removed, `tsconfig.tsbuildinfo` untracked.
- Rough edges: queued jobs sort with active ones, `converting` shows a spinner without a percentage, API error `detail` is parsed for display, artist prefers `artist`/`creator`/`channel` and strips " - Topic".

Deliberately left, with reasons:
- Same-title collisions under one album (finding 18, second half): needs the identity decision from the domain-model ticket; the in-flight URL dedup covers the concurrent-write case.
- Three `extract_info` calls per job (finding 22 last bullet): the findings list it as not worth fixing until rate limiting bites; merging needs the info dict on the Job model.
- Cancel API, persistence, auth, module globals in `main.py`, executor thread count, committed `web ui.png`: listed as not worth fixing or owned by other tickets (11, out of scope).
- Docker and nginx configs were validated as YAML/text only; Docker is not available on this machine, so `docker compose config` and `nginx -t` still need a run on the homelab.
