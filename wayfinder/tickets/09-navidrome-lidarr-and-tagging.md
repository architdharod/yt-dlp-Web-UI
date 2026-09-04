# Navidrome and Lidarr APIs, and tag-lookup options

Label: `wayfinder:research`
Status: closed (2026-09-03)
Assignee: research subagent (2026-09-03)
Blocked by: 

## Question

What API calls trigger a rescan in Navidrome (Subsonic `startScan`) and Lidarr (rescan, refresh artist), what auth each needs, and what env vars this app would need. What tag-lookup routes exist to fix tags in a FLAC from Python: MusicBrainz via `musicbrainzngs`, AcoustID fingerprinting via `pyacoustid` plus `chromaprint`, Deezer or iTunes search, and what each needs (API key, ffmpeg, rate limits). What tags does Navidrome read to group albums and artists, and which does Lidarr require? Findings go to `wayfinder/research/navidrome-lidarr-and-tagging.md`.

## Resolution

Findings: [research/navidrome-lidarr-and-tagging.md](../research/navidrome-lidarr-and-tagging.md).
- Navidrome: only scan trigger is Subsonic `GET /rest/startScan` (admin only), polled with `getScanStatus`. No API keys; auth is `u` + salted md5 token. App needs `NAVIDROME_URL/USER/PASSWORD`. Navidrome also watches the folder via fsnotify, but not reliably on Docker/network mounts; quick scans only re-read folders whose mtime changed, so touch the album folder after in-place tag rewrites.
- Navidrome grouping key: `musicbrainz_albumid` else `albumartist + album + albumversion + releasedate`. `cover.jpg` in the album folder beats embedded art.
- Lidarr: `X-Api-Key`, `POST /api/v1/command` with `RescanFolders`, `RefreshArtist`, `DownloadedAlbumsScan`, `ManualImport`; `GET /api/v1/trackfile?unmapped=true` lists unmatched files. `Artist/Album/track.flac` is compatible; externally placed files show as unmapped until tags/duration match a MusicBrainz release. Lidarr's tag writing defaults to off; only `scrubAudioTags` is a hazard.
- Lookups: MusicBrainz (no key, User-Agent required, 1 req/s) is the only route that yields MBIDs; AcoustID fingerprinting (free key, `libchromaprint-tools`) best for studio audio; iTunes search is a keyless fallback for art and track numbers; Deezer must be probed from the homelab (geo-gated here); Last.fm not worth it.
- Write tags with mutagen using Picard-standard Vorbis keys; drop yt-dlp's DESCRIPTION; write MB ids only for whole-album matches.
- beets: no stable library API, wants its own DB; skip and hand-roll roughly 150 lines.
