# Code review findings

Label: `wayfinder:research`
Status: closed (2026-09-03)
Assignee: research subagent (2026-09-03)
Blocked by: 

## Question

What bugs, security issues, and rough edges exist in the current backend, frontend, and Docker setup? Produce a ranked findings list with file and line references. Known suspects to verify: user-supplied artist/album flow into the output path unsanitised (`file_organizer.get_output_path`); the CONVERTING state is emitted after the download already finished; the per-job timeout cancels the awaiting task but not the yt-dlp thread; retry re-enters the queue without a concurrency check; SSE queue drop on slow clients; the frontend job list only updates from SSE and can drift from `GET /queue`; nginx proxies `/api/` but the backend has no prefix awareness; missing `uvicorn` proxy headers; pinned versions of yt-dlp; container runs as 1000 but download dir permissions unchecked at build. Findings go to `wayfinder/research/code-review-findings.md`.

## Resolution

Findings: [research/code-review-findings.md](../research/code-review-findings.md). 22 ranked items. Top ones:
path traversal through artist/album (critical), yt-dlp output-template injection via `%` in titles (high),
timeout leaves a zombie yt-dlp thread and unbounded concurrency (high), no `noplaylist` so `&list=` URLs pull a whole playlist (high),
compose passes env vars without fallbacks so a missing `.env` key crash-loops the backend (high),
frontend drops SSE events for unknown job ids and never refetches `/queue` (high), SSE fan-out can drop terminal events (medium),
CONVERTING state is emitted after ffmpeg already finished and `metadata` events are never sent (medium), URL is unvalidated (SSRF-ish, medium),
Docker permissions/HOME/depends_on/dependency floors (medium). Not bugs: retry does respect the semaphore; nginx strips `/api/` correctly.
Backend tests pass (145) in a scratch venv.
