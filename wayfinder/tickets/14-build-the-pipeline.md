# Build the GitHub Actions pipeline and GHCR-based compose

Label: `wayfinder:task`
Status: closed (2026-09-04)
Assignee: claude (2026-09-04)
Blocked by: 02, 13 (both closed)

## Question

Implement the decisions from the CI/CD ticket: the workflow file(s) under `.github/workflows/`, the image build and push to GHCR for backend and frontend, the compose file(s) so the homelab pulls images and development still builds locally, and a README section on deploying. Done when a push to the chosen branch produces pullable images and `docker compose pull && docker compose up -d` on the homelab runs them.

Note (2026-09-03, from [CI/CD and image delivery](13-cicd-and-image-delivery.md)): concrete shape decided there:
amd64 only; private GHCR packages `ghcr.io/architdharod/music-for-arr-{backend,frontend}`; gates are pytest and
`npm run build`; push on `main` only (PRs test only); tags `latest` + `sha-<short>`, weekly cron adds `weekly-<date>`;
`yt-dlp>=2026.8.19`; `docker-compose.yml` pulls, committed `docker-compose.override.yml` builds; named volume
`music-for-arr-data` at `/config` for `queue.db`; README section covering the fine-grained `read:packages` PAT login,
copying the compose file and `.env`, and `pull && up -d`. Also run `docker compose config` on both file sets as part of done.

## Resolution

Built on 2026-09-04 (implementation by an Opus subagent, verified in the main session). Nothing committed; the
changes sit in the working tree alongside the code-review fixes.

**Files.**
- `.github/workflows/ci.yml` (new): one `CI` workflow. Triggers: `pull_request`, `push` to `main`, `schedule`
  Monday 04:00 UTC, `workflow_dispatch`. `concurrency` cancels superseded runs per ref. Jobs `backend-tests`
  (Python 3.12, ffmpeg, `pip install -r requirements-dev.txt`, `pytest`) and `frontend-build` (Node 20, `npm ci`,
  `npm run build` with `VITE_API_BASE_URL=/api`) gate `publish`, which runs only on non-PR events on `main`:
  buildx, GHCR login with `GITHUB_TOKEN` (`packages: write`), a matrix over backend/frontend,
  `docker/metadata-action` tags (`latest` always; `sha-<7>` on push only; `weekly-<YYYYMMDD>` on schedule and
  manual dispatch), `docker/build-push-action` amd64 with gha cache scoped per service, then a step that pulls the
  backend image and logs `yt-dlp --version`.
- `docker-compose.yml`: homelab file, `image: ghcr.io/architdharod/music-for-arr-{backend,frontend}:latest`, no
  `build:`; backend gains `DATA_PATH=/config` and the named volume `music-for-arr-data:/config`. Everything else
  unchanged.
- `docker-compose.override.yml` (new, committed): the two `build:` blocks and the `VITE_API_BASE_URL` arg, so
  `docker compose up --build` builds locally with no flags.
- `backend/requirements.txt`: `yt-dlp>=2026.8.19`; other pins exact.
- `README.md`: "Getting Started (development)" plus a "Deploying to the homelab" section (private images,
  fine-grained `read:packages` PAT, `docker login ghcr.io`, copy only compose + `.env`, `pull && up -d`, weekly
  rebuild, rollback via `sha-`/`weekly-` tags, the `/config` volume) and a `DATA_PATH` row in the config table.

**Verified.** `docker compose -f docker-compose.yml config` (homelab set) and `docker compose config` (merged dev
set) both succeed; the workflow parses as YAML; backend suite 220 passed in a fresh venv, which resolved yt-dlp
2026.08.19 under the new floor; frontend `npm run build` succeeds.

**Not yet verified.** The "push to `main` produces pullable images" criterion needs a commit and push, which this
session did not do. First live run will confirm the metadata-action tag lines, especially the `workflow_dispatch`
`weekly-` tag, which is the least exercised. `DATA_PATH` is inert until the persistent queue is built; the volume
is provisioned ahead of it.
