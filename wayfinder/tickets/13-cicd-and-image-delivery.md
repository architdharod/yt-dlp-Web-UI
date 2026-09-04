# CI/CD and image delivery from GitHub to the homelab

Label: `wayfinder:grilling`
Status: closed (2026-09-03)
Assignee: claude (2026-09-03)
Blocked by: 

## Question

The homelab should run music-for-arr with `docker compose` only, pulling prebuilt images rather than building locally. Decide the shape of the GitHub Actions pipeline and the image delivery:
which events run it (every push, `main` only, tags); what gates the build (backend pytest, frontend typecheck and build, anything else); where images go (GHCR under the repo, one image per service);
tag scheme (`latest`, git sha, semver tags) and which tag the homelab compose pins; CPU architecture of the homelab host (amd64 only, or arm64 too, which decides buildx and QEMU); package visibility (public images need no login on the homelab, private ones need a PAT or fine-grained token in the Docker config);
how `docker-compose.yml` is split between "pull from GHCR" for the homelab and "build locally" for development (a `compose.override.yml`, profiles, or two files); how updates reach the homelab (manual `compose pull && up -d`, Watchtower, or a scheduled pull) and whether yt-dlp staleness (finding 14) should trigger a periodic rebuild on a cron schedule.

Note (2026-09-03, from [Persistent queue and job model](11-persistent-queue-and-job-model.md)): the backend gains a `DATA_PATH` env var (default `/config`) holding `queue.db`, which needs its own volume in the homelab compose file alongside `DOWNLOAD_PATH`.

## Resolution

Grilled with the user on 2026-09-03. Builds on the `DATA_PATH` decision in
[Persistent queue and job model](11-persistent-queue-and-job-model.md) and finding 14 (yt-dlp staleness) from
[Code review findings](01-code-review-findings.md).

**Host and visibility.** The homelab is amd64 only: one native build, no buildx/QEMU multi-arch. The repo and
the GHCR packages stay private. The homelab logs in once with a fine-grained personal access token scoped to
`read:packages` only (`docker login ghcr.io`); the README documents creation and renewal.

**Image names.** `ghcr.io/architdharod/music-for-arr-backend` and `ghcr.io/architdharod/music-for-arr-frontend`.
The package name follows the project, not the `yt-dlp-Web-UI` repo name. The frontend image is built with
`VITE_API_BASE_URL=/api` baked in, exactly as the compose build arg does today.

**Triggers and gates.** One workflow under `.github/workflows/`. Pull requests and every push to `main` run
the gates: backend `pytest` in `backend/` and frontend `npm ci && npm run build` (which includes `tsc -b`).
Only pushes to `main` that pass both gates build and push images. No new lint tooling is added.
A weekly `schedule:` run (Monday early morning UTC) re-runs the same gates and rebuilds/pushes from `main` so the
backend picks up the newest yt-dlp. `concurrency` cancels superseded runs on the same ref. Docker layer cache
via the GitHub Actions cache backend. Auth to GHCR uses the workflow's own `GITHUB_TOKEN` with `packages: write`.

**Tags.** Push-to-main runs publish `latest` and `sha-<7-char short sha>` for both images. Scheduled runs
publish `latest` and `weekly-<YYYYMMDD>` (agent's choice: overwriting the `sha-` tag on a rebuild would
destroy the rollback point it exists for). The homelab compose pins `latest`; rollback is editing the tag to a
`sha-`/`weekly-` value and re-running `up -d`.

**yt-dlp pin.** `requirements.txt` changes `yt-dlp==2026.8.19` to `yt-dlp>=2026.8.19`. All other packages stay
exact. The workflow logs `yt-dlp --version` from the built image so each run records what it shipped.

**Compose split.** `docker-compose.yml` becomes the homelab file: `image:` from GHCR for both services, no
`build:`. A committed `docker-compose.override.yml` adds the `build:` blocks (and the `VITE_API_BASE_URL` arg);
Compose merges it automatically, so development is `docker compose up --build` and needs no flags. The homelab
copies only `docker-compose.yml` and `.env` by hand (README shows both); it never clones the repo, so the
override can never leak in. Existing `container_name`, `restart`, `user`, and homepage labels are kept.

**Volumes.** `DOWNLOAD_PATH` bind mount stays as today. `queue.db` lives in a Docker named volume
(`music-for-arr-data`) mounted at `/config`, which is the `DATA_PATH` default; nothing new in `.env`.

**Updates.** Manual: `docker compose pull && docker compose up -d` on the homelab. No Watchtower, no host cron.

**Handed to the build ticket.** [Build the GitHub Actions pipeline and GHCR-based compose](14-build-the-pipeline.md)
implements all of the above and the README deploy section. The `docker compose config` sanity check left over from
the code-review fixes is folded into that ticket's done criteria.
