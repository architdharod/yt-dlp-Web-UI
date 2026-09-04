# Write the ordered implementation plan

Label: `wayfinder:task`
Status: closed (2026-09-04)
Assignee: claude (2026-09-04)
Blocked by: 02, 04, 05, 06, 08, 10, 11, 14 (all closed)

## Question

With every decision closed, write the spec and the ordered implementation plan (vertical slices, each shippable) to `plans/`. This is the destination; closing it closes the map.

## Resolution

Written 2026-09-04 to [plans/music-for-arr-v2.md](../../plans/music-for-arr-v2.md) using the `prd-to-plan`
shape: an architectural-decisions header gisted from every closed ticket (vocabulary, path identity and
validation, routes, SSE events, SQLite schema and states, workers, download pipeline, rescan hook, tag fixing,
trash, move, frontend data layer and tabs, bulk sources), the five ordering constraints the tickets imposed,
and 14 vertical slices, each with acceptance criteria:

1. Persistent queue that survives a restart
2. Cancel, Dismiss, and the in-house ffmpeg and tagging pipeline
3. TanStack Query, tabs, and the `library_changed` event
4. Read-only Library browser
5. Rescan hook and Navidrome/Lidarr configuration
6. Move and rename
7. Delete, Trash tab, Restore, Empty trash
8. Automatic tag fix after every download
9. Manual tagging jobs and the album pass
10. Collection probe and bulk jobs (backend and queue UI)
11. Collection preview UI
12. YouTube and YouTube Music artist enumeration via `ytmusicapi`
13. Spotify artist URLs
14. Docs and limitations sweep

One new route decision made while writing, since no ticket fixed it: the collection flow is
`POST /download/probe` (returns `{type: "track"}` or `{type: "collection", preview}`) plus
`POST /download/bulk`, leaving `POST /download` unchanged for single tracks. Granularity is open to the user's
review; splitting or merging phases edits the plan file, not the map.

## Amendment (2026-09-04, plan review)
The plan was reviewed against the tickets and amended: `library_changed` is emitted after tag writes; an
mtime-keyed scan cache and cover cache; an in-flight guard (409 on move/delete/restore touching an in-flight job's
target folder); a single-download collision criterion; `.trash/.ndignore` plus a live check of Lidarr's hidden-folder
handling; dependency bumps (FastAPI/Pydantic in Phase 1, React 19/Vite 7 in Phase 3); and the old Phase 10 split
into backend (10) and preview UI (11), so there are 14 slices.
