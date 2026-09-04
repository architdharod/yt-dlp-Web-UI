# Artist bulk download flow

Label: `wayfinder:grilling`
Status: closed (2026-09-03)
Assignee: claude (2026-09-03)
Blocked by: 03, 07

## Question

How does a bulk download work end to end? Decide: input is a URL or a name search; the preview list (grouped by album or flat) with select all and unselect all and pre-unticked duplicates; how many tracks a preview may hold; how a Spotify track is matched to a downloadable source and how the user is told about a bad match; folder placement per track (album from source or a single album chosen by the user); how bulk jobs appear in the in-flight queue (one parent or N children).

## Resolution

Grilled with the user on 2026-09-03. Builds on the enumeration research in
[research/source-enumeration.md](../research/source-enumeration.md) and the dedup rule in
[Domain model](03-domain-model.md).

**Entry.** One URL box on the Download tab, unchanged. On submit the backend probes the URL: a single track
queues as today; anything that is a collection (artist page, album, playlist, set, Spotify artist) opens the
preview instead. No name search; the app works only from the URL the user gives.

**Enumeration by source.**
- YouTube and YouTube Music artist URLs: resolve the channel to its YouTube Music artist and enumerate
  albums, singles, and EPs through `ytmusicapi` (keyless). Videos, live clips, and visualisers are excluded.
  Downloads use the album's `audioPlaylistId` (OLAK) or per-track `videoId` through yt-dlp.
- YouTube playlists and OLAK album URLs, SoundCloud users, sets, and albums, Bandcamp artists and albums,
  and any other collection yt-dlp reports as `_type == "playlist"`: flat enumeration with
  `extract_flat="in_playlist"` and `ignoreerrors=True`; album grouping from the source where it exists.
- Spotify artist URLs: read the artist name from the page (oEmbed or title), search YouTube Music for the
  artist, take the top match without a picker, and enumerate it as a YouTube Music artist. Spotify's own
  discography is never fetched and no Spotify credentials exist. The preview names the resolved artist in the
  editable artist field and carries the notice that the match may not equal the Spotify discography.
- Playlist and album URLs go through the same preview; this closes the map's fog item.

**Preview.** A flat checklist with columns: checkbox, title, album, duration, status. Global Select all and
Select none and a selected count. Default: everything selected except tracks the dedup rule finds on disk,
which are unticked and marked "in library". Unavailable tracks (SoundCloud DRM errors) are greyed and cannot
be selected. Above 500 tracks the preview warns and starts with nothing selected; above 2000 enumeration
stops and the user is told to use a narrower URL. Source notices shown when relevant: Bandcamp streams are
128 kbps MP3 (converted to FLAC, not lossless); Spotify matches may differ from the Spotify discography.

**Placement.** Artist comes from the source and is shown once at the top in an editable field that applies to
every selected track. Album comes from the source per track and is not editable in v2. Tracks without an
album become loose Singles under the artist (`Artist/track.flac`). The URL host allowlist
(`ALLOWED_URL_HOSTS`) widens to `music.youtube.com`, `bandcamp.com`, `open.spotify.com`.

**Queue shape.** Submitting creates one parent Job and one child Job per selected track (`parent_id`). The
in-flight view shows the parent as a single row with "N of M" progress and completed, failed, in-progress
counts, expandable to child rows. Cancel on the parent cancels all remaining children. Failed children stay
under the parent with Retry until dismissed; a parent with all children done disappears into the library.
Children run through the normal concurrency limit; the flat enumeration is cached per URL for the session so
re-opening a preview is cheap, and per-track metadata is only resolved when the child job runs.

Consequences: [Persistent queue and job model](11-persistent-queue-and-job-model.md) must model parent Jobs
(aggregate status, cancel cascade, retention of a parent with failed children); the plan (12) gets a slice
for `ytmusicapi` plus the probe endpoint before the preview UI; the CI ticket is unaffected.
