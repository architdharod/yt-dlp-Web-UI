# Source enumeration: what yt-dlp yields for an artist page, and options for Spotify

Label: `wayfinder:research`
Status: closed (2026-09-03)
Assignee: research subagent (2026-09-03)
Blocked by: 

## Question

For each source (YouTube channel, YouTube Music artist page, SoundCloud user, Bandcamp artist, others yt-dlp lists natively), what does `extract_info` with `extract_flat` return for an artist URL: track list, album grouping, artist and album fields, duration, and how long does it take? Which URL shapes are recognised? For Spotify artists: what are the options to list an artist's tracks (Spotify Web API with client credentials, spotdl, scraping) and what does each need from the user? Findings go to `wayfinder/research/source-enumeration.md`.

## Resolution

Findings: [research/source-enumeration.md](../research/source-enumeration.md). Verified live with yt-dlp 2026.08.19.
- Collection-vs-track test: artist/channel pages return `_type == "playlist"` with `entries`; single tracks lack `_type`. `extractor_key` (YoutubeTab, SoundcloudUser, BandcampUser, ...) and `ie.suitable(url)` classify offline.
- YouTube: `/@handle/videos` is a flat list (id, title, url, duration; no artist/album) at roughly 55 entries/s; `/releases` gives album stubs needing one more flat call each. `music.youtube.com` URLs fall back to plain YouTube tabs, so keyless `ytmusicapi` is the way to get album-grouped, duration-tagged artist tracks (about 3 s per artist) with playlist ids yt-dlp can download.
- SoundCloud: root URL mixes tracks and sets; `/tracks`, `/albums`, `/sets` split them; flat entries carry id, title, url only; sets carry album fields. Signed artists mostly raise "DRM protected".
- Bandcamp: artist page lists only urls; per-track extraction gives the richest tags (artist, album, track number, date) at about 1.2 s each.
- Use `extract_flat="in_playlist"`, `lazy_playlist` with `playlist_items` for fast first page, `ignoreerrors=True` for crawls. Mixcloud is unusably slow; Audius works; Audiomack, Jamendo artist pages, Spotify, Apple, Deezer, Tidal are unsupported.
- Spotify: needs a Developer app (client id/secret) and, since early 2026, a Premium account; limits cut to 10 per page, top-tracks removed, client-credentials reportedly failing for metadata. spotdl matches on YouTube Music with `"{artist} - {title}"` plus fuzzy title, artist, and a duration score (about 5 s tolerance).
- Recommendation: Spotify is a metadata seed only. Default keyless path is ytmusicapi to resolve and enumerate an artist, yt-dlp to download. Opt-in Spotify API integration with client credentials then PKCE. No open.spotify.com scraping.
