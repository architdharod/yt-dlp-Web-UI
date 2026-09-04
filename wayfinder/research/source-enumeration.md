# Source enumeration: what yt-dlp yields for an artist page, and options for Spotify

Ticket: `wayfinder/tickets/07-source-enumeration.md`
Date: 2026-09-03
Method: yt-dlp 2026.08.19 in a scratch venv (Python 3.10), calling
`yt_dlp.YoutubeDL({"extract_flat": True, "quiet": True}).extract_info(url, download=False)`
against public pages (Bonobo on YouTube/SoundCloud, Zoe Keating on Bandcamp, NPR Music for
scale, dublab on Mixcloud, Skrillex on Audius). Timings are single runs on a home connection;
treat as order-of-magnitude. Spotify section is from official docs, spotdl source, and web reports.

## 1. Summary

- Every artist/channel page yt-dlp recognises comes back as `_type == "playlist"` with an
  `entries` list; a single track comes back with `_type` absent (`None`) or `"video"`. That
  plus `extractor`/`extractor_key` is a stable "is this a collection?" test.
- `extract_flat=True` is cheap (0.5-6 s for a normal artist, ~50 s for a 2,764-video channel)
  but per-entry metadata is thin: `id`, `url`, `title` (often), `duration` (YouTube only),
  and, for YouTube, `uploader`/`channel` only inside album playlists. **No source gives
  `artist`/`album`/`track` per entry in flat mode except SoundCloud sets (`album`,
  `album_artist`, `album_type`) and Bandcamp entries carry nothing but `url`.**
- Album grouping exists natively on YouTube (`/@handle/releases` -> `OLAK5uy_...` album
  playlists), SoundCloud (`/user/albums`, `/user/sets` -> `soundcloud:set`), and Bandcamp
  (`/album/...` URLs mixed with loose `/track/...` URLs). YouTube Music artist pages
  (`music.youtube.com/channel/ID`) are handled by the ordinary `youtube:tab` extractor and
  return the **YouTube** channel tabs (Videos/Live/Shorts), not YTM's Songs/Albums; use
  `ytmusicapi` for that (see 2.2).
- Full metadata requires a second, non-flat extraction per track (~1-3 s each), which is
  where the real time goes. `extract_flat="in_playlist"` behaves the same as `True` for
  the artist-page case; it only differs when the top-level URL is itself a single item.
- Spotify: yt-dlp has no Spotify extractor at all. The only sanctioned enumeration path is
  the Web API (needs a developer app, and since Feb/Mar 2026, a Premium account); the
  practical alternative that needs nothing from the user is to resolve the artist on YouTube
  Music via `ytmusicapi` and enumerate there. Recommendation in section 4.5.

## 2. Per-source findings (yt-dlp `extract_flat=True`)

### 2.1 Table

| Source | URL shapes recognised (extractor) | Top-level result | Entries: structure | Fields per flat entry | Timing observed |
|---|---|---|---|---|---|
| YouTube channel root | `youtube.com/@handle`, `/channel/UC...`, `/c/name`, `/user/name` (`youtube:tab`, key `YoutubeTab`) | `_type=playlist`, `id=@handle` or channel id, `title`=channel name, `uploader`, `channel`, `channel_id`, `channel_url`, `description`, `channel_follower_count`, `thumbnails`, `playlist_count`=number of tabs | **Nested**: one `_type=playlist` entry per tab (Videos, Live, Shorts), each with its own `entries` (already materialised in flat mode). Bonobo: 3 tabs -> 103 videos, 0 streams, 118 shorts. Releases tab is *not* included in the root crawl. | Tab entries carry channel metadata; the video entries inside are as for `/videos` below | 4.2-6.4 s (3 tabs, ~220 items) |
| YouTube `/videos` tab | `/@handle/videos`, `/channel/ID/videos` | `_type=playlist`, `id`=channel id (`UC...`), `title`="Name - Videos", channel fields, `playlist_count` (None once `playlistend` is set) | Flat list of `_type=url`, `ie_key=Youtube` | `id`, `title`, `url` (`watch?v=`), `duration` (int seconds), `view_count`, `thumbnails`, `live_status`, `availability`, `channel_url`, `uploader_url`, `timestamp` (None), `creators`. **No `uploader`/`channel` name, no `artist`/`album`/`upload_date`.** | 103 videos: 1.7 s. NPR Music: 50 -> 1.2 s, 100 -> 2.4 s, 500 -> 8.4 s, all 2,764 -> 50 s (roughly 55/s; one InnerTube continuation per ~30 items) |
| YouTube `/releases` tab | `/@handle/releases`, `/channel/ID/releases` | `_type=playlist`, `title`="Name - Releases", channel fields, `playlist_count`=albums (Bonobo: 54) | Flat list of `_type=url`, `ie_key=YoutubeTab` pointing at album playlists `playlist?list=OLAK5uy_...` | **Only** `id` (OLAK id), `title` (album name), `url`. No year, no track count. Needs one more flat call per album. | 0.8 s for 54 albums |
| YouTube album playlist (`OLAK5uy_`) | `youtube.com/playlist?list=OLAK5uy_...` (`youtube:tab`) | `_type=playlist`, `id`=list id, `title`=album name, `uploader`/`channel` None at top level | Flat list of `_type=url`, `ie_key=Youtube` in track order | `id`, `title` (track name, or "Artist - Title (Official Video)" when the album links a video), `url`, `duration`, `uploader`, `channel`, `channel_id`, `channel_url`, `view_count`, `thumbnails`. Still **no `album`/`artist`/`track_number`** (index only from list position). | 0.8 s for 14 tracks |
| YouTube Music artist page | `music.youtube.com/channel/UC...` | Identical to the YouTube channel root: `youtube:tab`, three tabs Videos/Live/Shorts | Same nested structure as channel root | Same as channel root. YTM's Songs/Albums/Singles shelves are **not** exposed; the YTM artist channel id (`UCWBqhk...` for Bonobo) also differs from the YouTube channel id (`UCgyl5x...`). | 4.2 s |
| YouTube / YTM single video | `youtube.com/watch?v=`, `music.youtube.com/watch?v=` (`youtube`) | `_type` absent (single item), full info dict incl. `formats`, `duration`, `upload_date`, `uploader`, `channel`, `release_year`, `artist`/`album`/`track` (only for Art Tracks / auto-generated "Topic" uploads) | n/a | n/a | 1.8-2.3 s (flat flag is irrelevant for single items) |
| SoundCloud user root | `soundcloud.com/<user>` (`soundcloud:user`, key `SoundcloudUser`); also `m.`/`www.` and `api.soundcloud.com/users/<id>` (`soundcloud:user:permalink`) | `_type=playlist`, `id`=numeric user id, `title`="<user> (All)", `playlist_count`. **No uploader/channel fields at top level.** | Flat, **mixed**: track entries (`ie_key=Soundcloud`) and set/album entries (no `ie_key`, url contains `/sets/`). Bonobo: 241 = 195 tracks + 46 sets. | `id` (numeric), `title`, `url`. Nothing else: no duration, no artist, no album. | 3.3 s for 241 |
| SoundCloud `/tracks` | `soundcloud.com/<user>/tracks` | as above, `title`="<user> (Tracks)" | Flat, tracks only (`ie_key=Soundcloud`); 186 for Bonobo | `id`, `title`, `url` | 2.2 s; `playlistend=20` -> 1.4 s |
| SoundCloud `/albums`, `/sets` | `soundcloud.com/<user>/albums`, `/sets` (also `/reposts`, `/likes`, `/spotlight` match the regex) | as above, `title`="<user> (Albums)" / "(Sets)" | Flat list of set URLs (`/sets/<slug>`), no `ie_key` (resolved by URL on the next pass). Bonobo: 40 albums, 45 sets (sets ⊇ albums + playlists). | `id`, `title`, `url` | 1.0-1.3 s |
| SoundCloud set/album | `soundcloud.com/<user>/sets/<slug>` (`soundcloud:set`) | `_type=playlist`, **rich**: `album`, `album_artist`, `album_artists`, `album_type` (`album`/`single`/`ep`...), `duration`, `genre`, `license`, `release_date`, `release_timestamp`, `uploader`, `uploader_id`, `like_count`, `repost_count`, `thumbnails` | Flat list of `_type=url_transparent`, `ie_key=Soundcloud` in track order | `id`, `url`, `album`, `album_artist`, `album_type`. **No `title`, no duration.** | 0.9 s for 4 tracks |
| SoundCloud single track (non-flat) | `soundcloud.com/<user>/<slug>` (`soundcloud`) | single item: `title`, `artist`(=uploader name, not display name), `artists`, `track`, `album` (None unless via set), `uploader`, `duration` (float s), `genre`, `upload_date`, `license`, `like_count` | n/a | n/a | ~1 s. **Caveat**: most of Bonobo's catalogue (label-distributed "Go+" tracks) raises `DownloadError: This video is DRM protected` even for metadata-only extraction; flat enumeration still lists them. Expect this for any major-label artist on SoundCloud. |
| Bandcamp artist | `<artist>.bandcamp.com`, `<artist>.bandcamp.com/music` (`Bandcamp:user`, key `BandcampUser`; regex requires no path other than `/music`) | `_type=playlist`, `id`=subdomain, `title`="Discography of <subdomain>". No artist display name, no counts beyond `playlist_count`. | Flat, **mixed**: `/album/<slug>` URLs and loose `/track/<slug>` URLs, `_type=url`, **no `ie_key`**. Zoe Keating: 7 entries (5 albums + 2 tracks). | **`url` only.** No id, no title. Every entry needs a second extraction. | 0.7-1.7 s. `bonobo.bandcamp.com` returned 0 entries in ~0.5 s: a subdomain that exists but is not a discography page (label/merch stub) silently yields an empty playlist rather than an error. |
| Bandcamp album | `<artist>.bandcamp.com/album/<slug>` (`Bandcamp:album`) | `_type=playlist`, `id`=slug, `title`=album name, `uploader_id`=subdomain, `description`, `release_year` (None in test) | Flat list of `_type=url`, `ie_key=Bandcamp` in track order | `id` (numeric track id), `title` (track name), `url` | ~1.1 s |
| Bandcamp track (non-flat) | `<artist>.bandcamp.com/track/<slug>` (`Bandcamp`) | single item with **full tags**: `artist`, `artists`, `album`, `album_artist`, `track`, `track_number`, `track_id`, `duration`, `release_timestamp`, `upload_date`, `uploader`, stream `url` (128k mp3, tokenised) | n/a | n/a | ~1.2 s per track; non-flat crawl of the whole discography with `playlistend=3` took 12 s (three albums x ~4 s each) |
| Mixcloud user | `mixcloud.com/<user>/` (`mixcloud:user`, also `/uploads`, `/favorites`, `/listens`, `/stream`; `mixcloud:playlist`) | `_type=playlist`, `id`="<user>_uploads", `playlist_count` (dublab: 22,554) | Flat, `_type=url`, `ie_key=Mixcloud` | `id`, `url` only | **247 s for 50 entries**: the GraphQL pagination is extremely slow / rate-limited; not viable for a sync loop without caching |
| Audius artist | `audius.co/<handle>` (`audius:artist`, key `AudiusProfile`; also `audius:track`, `audius:playlist`) | `_type=playlist`, `id`=hashed id | Flat, `_type=url`, `ie_key=AudiusTrack`, `url="audius:<id>"` | `id`, `url` only | 14 s for 1 track (discovery-node lookup is slow) |
| Audiomack | only `audiomack.com/<artist>/song/<slug>` and `/album/<slug>` (`audiomack`, `audiomack:album`). Artist page `audiomack.com/<artist>` -> **Unsupported URL** | n/a | n/a | n/a | n/a |
| Jamendo | only `jamendo.com/track/<id>` and `/album/<id>` (`Jamendo`, `JamendoAlbum`). Artist page falls through to `generic` and returns a `_type=url` stub with no entries. | n/a | n/a | n/a | n/a |

### 2.2 YouTube Music via `ytmusicapi` (not yt-dlp, but the practical complement)

Because yt-dlp does not expose YTM artist shelves, `ytmusicapi` (unauthenticated, tested
1.x in the same venv) fills the gap:

- `YTMusic().get_artist("UC...")` (1.2 s): returns `name`, `channelId`, `songs.browseId`
  (a `VLOLAK5uy_...` playlist of all songs), `albums`/`singles` with `browseId` + `params`.
- `get_artist_albums(browseId, params, limit=None)` (0.2 s): 11 albums for Bonobo with
  `title`, `type` (Album/Single/EP), `year`, `browseId` (`MPREb_...`).
- `get_album(browseId)` (1.2 s): tracks with `title`, `videoId`, `duration_seconds`,
  `artists[{name,id}]`, `album`, `trackNumber`, `isExplicit`, plus album `year`,
  `audioPlaylistId` (`OLAK5uy_...`, the same id yt-dlp consumes).
- `get_playlist(songs.browseId, limit=None)` (1.0 s): 149 songs with `videoId`,
  `duration_seconds`, `artists`, `album{name,id}`.
- `search(q, filter="songs", limit=5)` (0.5 s): 20 results with the same shape.

So the fastest way to get an artist's *tagged* catalogue (artist, album, track number,
duration, videoId) from YouTube is `ytmusicapi` for enumeration and yt-dlp only for download.
Note the YTM artist channel id differs from the YouTube channel id; `get_artist` accepts the
YTM one (`UCWBqhk...`), and `search(q, filter="artists")` resolves a name to it.

### 2.3 `extract_flat` modes, pagination, laziness

From `YoutubeDL.py` docstring (2026.08.19):

```
extract_flat:  Whether to resolve and process url_results further
    * False:                Always process. Default for API
    * True:                 Never process
    * 'in_playlist':        Do not process inside playlist/multi_video
    * 'discard':            Always process, but don't return the result from inside playlist/multi_video
    * 'discard_in_playlist': Same as "discard", but only for playlists (not multi_video). Default for CLI
```

- `True` vs `"in_playlist"`: identical for artist/album URLs (all observed entries are
  `url`/`url_transparent` inside a playlist and are left unresolved either way). They differ
  only when the top-level URL is itself a `url` stub (e.g. a redirect or a `generic` hit):
  `True` returns the stub, `"in_playlist"` resolves it. Use `"in_playlist"` as the default so
  a pasted single-track URL still comes back fully resolved.
- Nested playlists (YouTube channel root -> tabs) are materialised even in flat mode; the
  extractor walks every tab. Prefer `/videos` or `/releases` directly to avoid Shorts/Live.
- `playlistend=N` / `playlist_items="1:N"` stop pagination after N entries (`playlist_count`
  becomes `None` when set); combined with `lazy_playlist=True` the first page returns in
  ~0.6 s. Note `playlistend` is marked deprecated in favour of `playlist_items`, but still
  works. `playlistreverse` / `playlist_items="-N:"` for "newest N" requires the whole list
  to be fetched first on YouTube (entries are yielded newest-first anyway).
- `ignoreerrors=True` is mandatory for non-flat crawls: one DRM/private/unavailable item
  otherwise aborts the entire `extract_info` with `DownloadError`.
- Deprecation warning: yt-dlp 2026.08 warns that Python 3.10 support is deprecated; target
  3.11+.

### 2.4 Detecting "artist/collection vs single track"

Stable signals, in order of preference:

1. `info.get("_type") in ("playlist", "multi_video")` -> collection; `_type` missing/`None`
   or `"video"` -> single item; `"url"`/`"url_transparent"` -> unresolved stub (only seen
   when `extract_flat=True` on a stub or `generic` fallback; treat as "unknown, resolve").
2. `info["extractor_key"]` (stable class names): `YoutubeTab` (channel, tab, playlist, album),
   `SoundcloudUser`, `SoundcloudSet`, `SoundcloudPlaylist`, `BandcampUser`, `BandcampAlbum`,
   `MixcloudUser`, `MixcloudPlaylist`, `AudiusProfile`, `AudiusPlaylist`, `AudiomackAlbum`,
   `JamendoAlbum`, versus `Youtube`, `Soundcloud`, `Bandcamp`, `Mixcloud`, `AudiusTrack`,
   `Audiomack`, `Jamendo`. `extractor` (`youtube:tab`, `soundcloud:user`, `Bandcamp:user`)
   is the human-readable equivalent.
3. Pre-flight without a network call: `yt_dlp.extractor.gen_extractor_classes()` and
   `ie.suitable(url)` / `ie.IE_NAME` pick the extractor from the URL alone. Useful for
   rejecting `Unsupported URL` (Audiomack/Jamendo artist pages) and for classifying
   channel vs album vs track (`YoutubeTab` matches all three, so combine with
   `webpage_url` path: `/@`, `/channel/`, `/c/`, `/user/` = channel; `playlist?list=OLAK5uy_`
   = album; `list=PL`/`UU`/`RD` = playlist/uploads/mix).
4. Distinguishing "artist" from "arbitrary channel/playlist" is not something yt-dlp
   knows; the closest hints are `channel_is_verified` (YouTube), `OLAK5uy_` ids (auto
   album playlists exist only for distributed music), and the `- Topic` suffix on
   auto-generated artist channels.

### 2.5 Other native music sources (brief)

| Extractor | Artist-level enumeration? | Notes |
|---|---|---|
| Mixcloud (`mixcloud:user`, `:playlist`) | Yes (uploads/favorites/listens/stream) | Very slow pagination (see table). DJ sets/radio, not albums. |
| Audius (`audius:artist`, `:playlist`, `:track`) | Yes | Slow discovery-node lookups; entries are `audius:<id>` stubs. |
| Audiomack (`audiomack`, `audiomack:album`) | **No** (album/song only) | Artist page unsupported. |
| Jamendo (`Jamendo`, `JamendoAlbum`) | **No** (track/album only) | Jamendo has a free public REST API (`api.jamendo.com/v3.0/artists/tracks`, needs a free client_id) if ever needed. |
| Yandex Music (`yandexmusic:artist:albums`, `:artist:tracks`, `:album`, `:playlist`) | Yes | Geo-restricted; needs cookies for most content. |
| Last.fm (`LastFMUser`, `LastFMPlaylist`) | User/playlist pages | Resolves to YouTube links; metadata only. |
| Bilibili audio (`BilibiliSpaceAudio`, `BilibiliAudioAlbum`) | Yes | CN. |
| HearThisAt, ReverbNation, Clyp, Freesound, Beatport, Piapro, AudioBoom | Track/playlist level | No artist crawler; Beatport is preview-only. |
| Discogs (`DiscogsReleasePlaylist`) | Release -> YouTube videos | Metadata bridge, not audio. |
| Apple Music / Deezer / Tidal / Amazon Music / Qobuz / Spotify | **None** | No extractors at all (DRM). `apple:music:connect` is a legacy stub. |

## 3. Practical consequences for a sync loop

- Enumeration cost is dominated by the second pass. A flat pass over an artist is 1-6 s;
  turning 100 flat entries into tagged tracks via non-flat `extract_info` is ~100-300 s.
  Cache flat ids and only resolve new ones.
- Per-source "album" is available cheaply only from YouTube `/releases` + OLAK playlists,
  SoundCloud `/albums` + sets, and Bandcamp `/album/` URLs (+ one flat call each).
  YouTube `/videos` is a bag of videos with no album notion (and includes trailers, live
  clips, visualisers); `/releases` is the music-shaped view.
- Bandcamp gives the best tags per track (artist, album, track number, release date) but
  streams are 128 kbps MP3 only. SoundCloud tags are weak (`artist` is the account slug) and
  much of a signed artist's catalogue is DRM-locked to metadata-only errors.
- Expect a deliberate `Unsupported URL` for Audiomack/Jamendo artist pages and an *empty*
  playlist (not an error) for a Bandcamp subdomain that is not a discography page.

## 4. Spotify artists

yt-dlp cannot read Spotify at all (no extractor; audio is DRM'd). Enumeration options:

### 4.1 Spotify Web API, client-credentials flow (official)

What the user must create: a Spotify account, then an app in the Developer Dashboard
(`developer.spotify.com/dashboard`) to obtain a **Client ID** and **Client Secret**. Since
the February 2026 platform changes, a new Development Mode app additionally requires:

- the app owner to hold an **active Spotify Premium** subscription (app stops working if it
  lapses); effective 11 Feb 2026 for new apps, 9 Mar 2026 for existing ones;
- one Development Mode Client ID per developer (raised to 25 in July 2026 per the
  migration guide), max 5 authorised users per app;
- Extended Quota Mode (which keeps the old surface and much higher rate limits) now requires
  a registered business with ~250k MAU: not obtainable for a hobby tool.

Token: `POST https://accounts.spotify.com/api/token`, header
`Authorization: Basic base64(client_id:client_secret)`, body `grant_type=client_credentials`;
returns a bearer token valid 3600 s. No user login, so only catalogue endpoints.

Endpoints for an artist's catalogue (status per the official Feb-2026 changelog):

| Endpoint | Status in dev mode | Params / notes |
|---|---|---|
| `GET /artists/{id}` | kept (loses `followers`, `popularity`) | resolve name/URL -> id |
| `GET /artists/{id}/albums` | kept | `include_groups=album,single,compilation[,appears_on]`, `market`, `limit` 1-10 (default 5), `offset`; paginate via `next`. Returns simplified albums: `id`, `name`, `album_type`, `release_date`, `total_tracks`, `artists`. `album_group` field removed. |
| `GET /albums/{id}/tracks` | kept | `limit` 1-50 (default 20), `offset`, `market`. Tracks: `id`, `name`, `duration_ms`, `track_number`, `disc_number`, `explicit`, `artists`, `is_playable`. No ISRC here; ISRC (`external_ids.isrc`) requires `GET /tracks/{id}` per track (batch `GET /tracks?ids=` was removed). |
| `GET /albums/{id}` | kept (loses `label`, `popularity`, `available_markets`) | full album incl. first 50 tracks |
| `GET /artists/{id}/top-tracks` | **removed** | |
| `GET /search` | kept, `limit` max 10 (was 50), default 5 | `q=artist:"..."`, `type=artist` to resolve ids |
| `GET /albums?ids=`, `/artists?ids=`, `/tracks?ids=` batch | **removed** | one call per item |

Rate limits: rolling 30-second window, undocumented exact number (community reports ~180
requests/30 s historically; dev-mode apps are lower than extended-quota). On 429 honour
`Retry-After` (seconds). Some users report 24-hour lockouts in dev mode after bursts. A
200-album artist costs ~20 album-list pages + 200 album-track calls; spread them out.

Open risk: the rspotify maintainer reports (issue #550) that after the March 2026 cutoff
client-credentials tokens started failing for metadata endpoints in dev-mode apps and that
Spotify is "moving away from the Client Credentials flow for metadata endpoints"; the
official changelog and migration guide say nothing about authentication changing. The
official endpoint-cutoff for *existing* apps was postponed. Treat client-credentials as
"may work, verify at runtime"; if it fails, Authorization Code + PKCE (user logs in once in a
browser, refresh token stored locally) is the fallback that is documented to keep working.

### 4.2 Libraries

- **spotipy** (Python): thin client; `Spotify(auth_manager=SpotifyClientCredentials())`,
  `artist_albums(id, include_groups=..., limit=..)`, `album_tracks(id)`, `next(page)`.
  Needs the same Client ID/Secret as above. Handles 429/`Retry-After` automatically.
- **spotdl** (Python, `spotDL/spotify-downloader`): wraps spotipy for enumeration and
  `ytmusicapi`+yt-dlp for download. Ships a shared default Client ID/Secret so users often
  need nothing, but it depends on a public credential and is subject to the same dev-mode
  restrictions; users are told to supply their own when it 429s. Artist enumeration
  (`spotdl/types/artist.py`): `artist_albums(include_groups="album,single,compilation")`
  paginated via `next`, dedupe albums by slugified name (regional duplicates), fetch each
  album's tracks, then "very aggressive" dedupe by slugified song name. It does **not**
  filter by artist id, so compilations pull in other artists' tracks.
- Rust/JS equivalents (rspotify, spotify-web-api-node) have the same dependency on a
  dashboard app.

### 4.3 Unofficial / scraping approaches

- **Web-player token scrape**: `open.spotify.com/get_access_token?reason=transport&productType=web_player`
  (anonymous) yields a short-lived token usable against the internal
  `api-partner.spotify.com/pathfinder/v1/query` GraphQL (`queryArtistDiscographyAll`,
  `getAlbum`) endpoints; `sp_dc` cookie from a logged-in browser removes the anonymous
  limits. Libraries such as `SpotifyScraper` (PyPI) and various Node packages wrap this.
  Fragile (Spotify rotates the TOTP-style `secret` used to mint tokens every few weeks;
  breakages in 2025-2026 were frequent) and a clear ToS violation (automated access to the
  service outside the developer platform).
- **Embed pages**: `open.spotify.com/embed/album/{id}` returns an HTML page with a
  `__NEXT_DATA__` JSON blob containing the album's track list (name, duration, artists) with
  no auth. Works per album, but there is no embed listing an artist's whole discography;
  one still needs the artist -> album ids from somewhere.
- **Hosted scrapers** (Apify "spotify-scraper" actors etc.): paid, third-party, same ToS
  exposure, and adds a network dependency; not appropriate for a self-hosted *arr tool.
- **Cross-catalogue lookup instead of Spotify**: MusicBrainz (free, no key,
  1 req/s, `artist/{mbid}?inc=release-groups`) and Deezer's public API
  (`api.deezer.com/artist/{id}/albums`, no key) list discographies with ISRCs; Lidarr already
  builds on MusicBrainz. A Spotify artist URL can be bridged to these by name search.

### 4.4 Matching a Spotify track to YouTube / YouTube Music (how spotdl does it)

- Query shape: `create_search_query` defaults to `"{artist} - {title}"`, where `{artist}` is
  the comma-joined artist list (`create_song_title` = `"A, B - Name"`), lower-cased; the
  user can override with `--search-query "{artists} {title} {album}"` etc.
- Search backend: `ytmusicapi.search` twice, `{"filter": "songs", "ignore_spelling": True,
  "limit": 50}` then `{"filter": "videos", ...}`; the ISRC path (`search(isrc)`) is present
  but the ISRC-specific filtering is currently commented out, results are only flagged
  `isrc_search`.
- Scoring (`spotdl/utils/matching.py`): fuzzy `name_match` (skip if <= 60, minus 15 per
  "forbidden word" such as remix/live/acoustic/cover/instrumental present on one side only),
  `artists_match` (reject if < 70), `album_match` only counted for verified YTM "song"
  results, explicit-flag mismatch -5. Duration: `time_match = 100 * exp(-0.1 * |dt|)` in
  seconds, rejected below 25 (about 14 s off), so a 3-5 s tolerance keeps ~60-75 points.
  Final score averages artist and name (50/50) then folds in time/album; best of the top 8
  candidates wins, view count breaks ties.
- Practical takeaways for this project: search YTM `songs` first (tagged, duration-exact,
  usually the same OLAK album), fall back to `videos`; require |duration delta| <= ~5 s;
  penalise live/remix/cover words; prefer results whose `artists[].id` equals the resolved
  YTM artist channel id. yt-dlp's own `ytsearch:`/`ytmsearch:` prefixes work but return
  plain video search without the songs/videos distinction, so `ytmusicapi` is the better
  matcher.

### 4.5 Recommendation

1. Treat Spotify as a **metadata seed, never a media source**. A Spotify artist URL should be
   resolved to an artist name (+ optional id) and then enumerated on a source yt-dlp can
   download from.
2. Default path requiring nothing from the user: resolve the artist on **YouTube Music via
   `ytmusicapi`** (`search(name, filter="artists")` -> `get_artist` -> `get_artist_albums` /
   `get_playlist(songs)`), which yields album-grouped, duration-tagged tracks with
   `audioPlaylistId` (OLAK) that yt-dlp downloads directly. It is fast (~3 s per artist),
   keyless, and avoids the Spotify ToS problem entirely. Confirm the artist match by
   comparing the Spotify artist name with the YTM artist name and one or two album titles.
3. Optional, opt-in **Spotify Web API** integration for users who want Spotify's exact
   discography as the source of truth: settings fields for Client ID/Secret, client
   credentials first, PKCE fallback if the token is rejected; use `artists/{id}/albums`
   (limit 10, paginate) + `albums/{id}/tracks` (limit 50) and match each track to YTM with
   the 4.4 rules. Document the Premium requirement and the 1-app/5-user cap plainly.
4. Do not ship scraping of open.spotify.com or third-party scraper services; too fragile and
   a ToS problem for a redistributable tool. If a keyless discography source is wanted for
   validation, prefer MusicBrainz/Deezer public APIs.
5. For all sources, use `extract_flat="in_playlist"` + `ignoreerrors=True` for enumeration,
   cache entry ids, and resolve only new entries non-flat; for YouTube prefer `/releases`
   over `/videos` when the goal is albums.

## Sources

- yt-dlp 2026.08.19 `YoutubeDL.py` docstring and extractor `_VALID_URL` patterns (local install)
- https://developer.spotify.com/documentation/web-api/tutorials/client-credentials-flow
- https://developer.spotify.com/documentation/web-api/reference/get-an-artists-albums
- https://developer.spotify.com/documentation/web-api/reference/get-an-albums-tracks
- https://developer.spotify.com/documentation/web-api/concepts/rate-limits
- https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide
- https://developer.spotify.com/documentation/web-api/references/changes/february-2026
- https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security
- https://github.com/ramsayleung/rspotify/issues/550
- https://github.com/spotDL/spotify-downloader (`spotdl/utils/matching.py`, `spotdl/utils/formatter.py`, `spotdl/providers/audio/base.py`, `spotdl/providers/audio/ytmusic.py`, `spotdl/types/artist.py`)
- https://ytmusicapi.readthedocs.io/en/stable/reference/browsing.html
- https://www.neowin.net/news/spotify-now-requires-premium-accounts-for-developer-mode-api-access/
