# Navidrome and Lidarr APIs, and tag-lookup options

Ticket: `wayfinder/tickets/09-navidrome-lidarr-and-tagging.md`
Date: 2026-09-03
Sources: Navidrome docs and source (`master`, v0.63.x era), Lidarr source (`develop`) and its `openapi.json`,
Servarr wiki, subsonic.org API spec, MusicBrainz/AcoustID/Cover Art Archive docs, Deezer and iTunes live calls,
mutagen and beets docs. Live calls were made from this machine on 2026-09-03; results are quoted where useful.

Context from the code: the backend already depends on `mutagen>=1.47` and `ffmpeg` is in the image
(`backend/Dockerfile`). `download_audio` in `backend/app/downloader.py` runs yt-dlp with `FFmpegExtractAudio`
(flac), `FFmpegMetadata` and `EmbedThumbnail`, so every file already carries yt-dlp's TITLE/ARTIST/ALBUM/DATE
guess plus the video thumbnail as a picture block. "Fix tags" means overwriting those.

---

## 1. Navidrome

### 1.1 How to trigger a scan

There is exactly one supported way from outside: the Subsonic API `startScan` endpoint. The native `/api`
router (`server/nativeapi/native_api.go`) registers no scan route; Navidrome's own web UI calls the Subsonic
endpoint (`ui/src/subsonic/index.js`: `startScan = (options) => httpClient(url('startScan', null, options))`).
The native API is explicitly undocumented and unstable, so do not build on it.

`startScan` is admin-only (`server/subsonic/api.go`: `h(r.With(adminOnly), "startScan", api.StartScan)`).
`getScanStatus` is available to any user.

Parameters (`server/subsonic/library_scanning.go`):

| Param | Meaning |
|---|---|
| `fullScan` | `true` forces a full rescan; default `false` = quick scan (only folders whose mtime changed). |
| `target` | Optional, repeatable, newer versions only (0.59+ "selective folder scanning"). Format is `libraryID` or `libraryID:folderPath`. Omit it to scan everything. |

Response (`getScanStatus` and `startScan` both return `scanStatus`): `scanning` (bool), `count`, `folderCount`,
`lastScan` (RFC3339), `scanType`, `elapsedTime`, `error` (last error string). `startScan` triggers the scan
asynchronously and returns the current status; poll `getScanStatus` until `scanning` is `false`.

Navidrome is the one scan you actually need to trigger. Quick scan of a large library takes seconds because
it only opens folders whose mtime changed; the app writes into `Artist/Album/`, which bumps that folder's mtime.

### 1.2 Auth

Navidrome has no API keys (issue #1606 closed "not planned"; no release note through v0.63.2 adds one).
`server/subsonic/middlewares.go` accepts, in this order:

1. Reverse-proxy header auth (`ReverseProxyUserHeader`, only when the request comes from `ReverseProxyWhitelist`). Not useful here.
2. Query params `u` + one of: `jwt` (UI session token), `p` (plaintext, or `enc:<hex>`), or `t` + `s` where
   `t = md5(password + salt)` hex. Token/salt is what the UI itself uses and is the right choice for a server-side caller.

Every call also needs `v` (protocol version, `1.16.1` is fine), `c` (client name) and `f=json`.

Python (stdlib only):

```python
import hashlib, secrets, requests

def nd_params(user, password, client="music-for-arr"):
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    return {"u": user, "t": token, "s": salt, "v": "1.16.1", "c": client, "f": "json"}

base = "http://navidrome:4533"
r = requests.get(f"{base}/rest/startScan", params={**nd_params(u, pw), "fullScan": "false"}, timeout=10)
body = r.json()["subsonic-response"]            # {"status": "ok", "scanStatus": {...}} or {"status": "failed", "error": {"code": 50, ...}}
status = requests.get(f"{base}/rest/getScanStatus", params=nd_params(u, pw), timeout=10).json()
```

curl equivalent (password `sesame`, salt `c19b2d`, token from the Subsonic spec example):

```
curl 'http://navidrome:4533/rest/startScan?u=admin&t=26719a1196d2a940705a59634eb18eab&s=c19b2d&v=1.16.1&c=music-for-arr&f=json&fullScan=false'
```

Subsonic error codes worth mapping: `40` wrong credentials, `50` user not authorized (non-admin calling
`startScan`), `70` not found (bad `target`).

### 1.3 Env vars / config this app needs

For the app (its own container):

| Var | Purpose |
|---|---|
| `NAVIDROME_URL` | e.g. `http://navidrome:4533` (no trailing `/rest`) |
| `NAVIDROME_USER` | must be an admin user; create a dedicated one in the Navidrome UI |
| `NAVIDROME_PASSWORD` | plaintext; the app derives token+salt per request |
| (optional) `NAVIDROME_FULL_SCAN` | default false |

If any is unset, disable the Navidrome step and report "not configured" rather than fail the metadata job.

Navidrome-side settings that matter (config key -> env var, `ND_` prefix, dots become underscores, uppercase):

| Key | Env | Default | Note |
|---|---|---|---|
| `Scanner.Enabled` | `ND_SCANNER_ENABLED` | `true` | `false` disables watcher and schedule; `startScan` still works |
| `Scanner.Schedule` | `ND_SCANNER_SCHEDULE` | `"0"` (off) | cron or `@every 1h` |
| `Scanner.WatcherWait` | `ND_SCANNER_WATCHERWAIT` | `5s` | debounce after the fs watcher sees a change |
| `Scanner.ScanOnStartup` | `ND_SCANNER_SCANONSTARTUP` | `true` | |
| `Scanner.PurgeMissing` | `ND_SCANNER_PURGEMISSING` | `never` | `never` / `always` / `full` ; affects what happens after this app deletes files |
| `CoverArtPriority` | `ND_COVERARTPRIORITY` | `cover.*, folder.*, front.*, embedded, external` | see 1.5 |
| `EnableExternalServices` | `ND_ENABLEEXTERNALSERVICES` | `true` | `external` art means Last.fm and needs `LastFM.ApiKey` |
| `MusicFolder` | `ND_MUSICFOLDER` | `./music` | must be the same tree as `DOWNLOAD_PATH` (or a parent) |

### 1.4 Does Navidrome notice new files by itself?

Mostly yes. Since the 0.55 scanner rewrite, Navidrome runs an fsnotify-based watcher on `MusicFolder`
(all libraries) whenever `Scanner.Enabled` is true, regardless of `Scanner.Schedule`; a change triggers a
quick scan after `Scanner.WatcherWait` of quiet. The watcher does not fire reliably on network mounts
(NFS/SMB/CIFS, some Docker bind-mount setups on macOS/Windows), which is exactly the "change detection stopped
after moving to Docker" report in discussion #4021. The explicit `startScan` call is therefore a cheap
insurance policy, not a duplicate: fire it after the tag rewrite, and it is a no-op-fast quick scan if the
watcher already got there.

Consequence for tag rewriting: rewriting tags changes the file mtime but not necessarily the folder mtime.
A quick scan re-reads a folder only when the folder mtime changed, so after an in-place tag fix either
touch the album folder (`os.utime(folder)`) or call `startScan?fullScan=true`. Touching the folder is cheaper.

### 1.5 Tags Navidrome reads (FLAC / Vorbis comments)

From `resources/mappings.yaml` (the FLAC aliases, matched case-insensitively):

| Navidrome field | Vorbis comment names accepted | Used for |
|---|---|---|
| title | `TITLE` | track title |
| album | `ALBUM` | album name |
| artist | `ARTIST` (multi-valued `ARTISTS` preferred if present) | track artist; split on ` / `, ` feat. `, ` ft. ` only when `ARTISTS` absent |
| albumartist | `ALBUMARTIST`, `ALBUM ARTIST`, `ALBUM_ARTIST` (`ALBUMARTISTS`) | artist the album is filed under; falls back to ARTIST, then "[Unknown Artist]" |
| tracknumber | `TRACKNUMBER`, `TRACK` (accepts `n` or `n/total`) | ordering |
| discnumber | `DISCNUMBER`, `DISC` | ordering |
| releasedate / recordingdate / originaldate | `RELEASEDATE`, `YEAR` / `DATE` / `ORIGINALDATE`, `ORIGINALYEAR` | year shown, part of album PID fallback |
| genre | `GENRE` | |
| compilation | `COMPILATION` (=1) | groups various-artists albums |
| musicbrainz_recordingid | `MUSICBRAINZ_TRACKID` | track PID (note the historic naming: this is the recording id) |
| musicbrainz_trackid | `MUSICBRAINZ_RELEASETRACKID` | |
| musicbrainz_albumid | `MUSICBRAINZ_ALBUMID` | album PID (first choice) |
| musicbrainz_artistid | `MUSICBRAINZ_ARTISTID` | |
| musicbrainz_albumartistid | `MUSICBRAINZ_ALBUMARTISTID` | |
| musicbrainz_releasegroupid | `MUSICBRAINZ_RELEASEGROUPID` | |
| releasetype | `RELEASETYPE`, `MUSICBRAINZ_ALBUMTYPE` | album/single/EP filter |
| albumversion | `ALBUMVERSION`, `MUSICBRAINZ_ALBUMCOMMENT` | disambiguation |

Grouping rules (`docs/usage/configuration/persistent-ids`), defaults:

- `PID.Album = "musicbrainz_albumid|albumartistid,album,albumversion,releasedate"`: if `MUSICBRAINZ_ALBUMID`
  is set it wins; otherwise album artist + album + version + release date.
- `PID.Track = "musicbrainz_trackid|albumid,discnumber,tracknumber,title"`.
- Pipe = fallback, comma = concatenate.

Practical implications for this app:

- Tracks split into several "albums" when `ALBUMARTIST` differs between tracks (typical after yt-dlp, whose
  ARTIST is the uploader/channel). Always write the same `ALBUMARTIST` for every track of an album.
- If you write `MUSICBRAINZ_ALBUMID`, all tracks in that album must carry the *same* release id, otherwise
  Navidrome creates one album per id. Writing MBIDs for some tracks and not others of the same folder splits it too.
  Safer policy: write MB ids only when the whole album was matched to one release; otherwise write text tags only.
- `DATE` mismatches across tracks also split an album when no MBID is present (releasedate is part of the fallback key).
- Cover art: default priority looks for `cover.*`, `folder.*`, `front.*` files in the album folder first, then
  the embedded picture block, then Last.fm. Writing `cover.jpg` into `Artist/Album/` is the most robust choice
  (it also fixes the "one track has the video thumbnail" problem); embedding the picture per file is also fine.
  `ArtistArtPriority` default `artist.*, album/artist.*, external`.

---

## 2. Lidarr

### 2.1 Auth and command API

API key in header `X-Api-Key` or query `?apikey=` (`Lidarr.Http/Authentication/AuthenticationBuilderExtensions.cs`
and `openapi.json` `securitySchemes`). The key is in Settings > General.

Commands are `POST /api/v1/command` with a JSON body whose `name` is the C# command class name minus `Command`.
Response is a `CommandResource` (`id`, `name`, `status` = queued/started/completed/failed, `started`, `ended`,
`exception`); poll `GET /api/v1/command/{id}`.

| Command | Body fields (from the command classes) | What it does |
|---|---|---|
| `RescanFolders` | `folders: [string]`, `filter: "none"|"matched"|"known"` (default `known`), `addNewArtists: bool` (default true), `artistIds: [int]` | Disk-scans the given root folders (or all if omitted). This is what the UI "Rescan Artist Folders" task runs. |
| `RefreshArtist` | `artistId: int` or `artistIds: [int]`, `isNewArtist` | Re-pulls MusicBrainz metadata for the artist and, when `rescanAfterRefresh` is `always` (default), rescans the artist folder afterwards. |
| `RescanArtist` | `artistId` | Older, kept for third-party apps; disk scan of one artist folder without the MB refresh. |
| `DownloadedAlbumsScan` | `path: string`, `downloadClientId`, `importMode: "auto"|"move"|"copy"` | Import a *download* folder (outside the root folder). |
| `ManualImport` | `files: [ManualImportFile]`, `importMode`, `replaceExistingFiles` | Commit choices made with `GET /api/v1/manualimport`. |

Examples:

```bash
curl -X POST http://lidarr:8686/api/v1/command -H "X-Api-Key: $LIDARR_API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"RescanFolders","folders":["/music"],"filter":"known","addNewArtists":true}'

curl -X POST http://lidarr:8686/api/v1/command -H "X-Api-Key: $LIDARR_API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"RefreshArtist","artistIds":[12]}'

# find an artist id by name / MBID
curl "http://lidarr:8686/api/v1/artist?apikey=$LIDARR_API_KEY"                      # list, match on artistName or path
curl "http://lidarr:8686/api/v1/artist?mbId=056e4f3e-d505-4dad-8ec1-d04f521cbb56&apikey=$LIDARR_API_KEY"

# unmapped files (things Lidarr saw on disk but could not match to a track)
curl "http://lidarr:8686/api/v1/trackfile?unmapped=true&apikey=$LIDARR_API_KEY"

# root folders
curl "http://lidarr:8686/api/v1/rootfolder?apikey=$LIDARR_API_KEY"                  # [{id, path, accessible, ...}]
```

Python:

```python
import requests
H = {"X-Api-Key": os.environ["LIDARR_API_KEY"]}
base = os.environ["LIDARR_URL"].rstrip("/")
cmd = requests.post(f"{base}/api/v1/command", json={"name": "RescanFolders", "folders": [root]}, headers=H, timeout=10).json()
# poll
st = requests.get(f"{base}/api/v1/command/{cmd['id']}", headers=H, timeout=10).json()["status"]
```

Env vars for the app: `LIDARR_URL`, `LIDARR_API_KEY`, optionally `LIDARR_ROOT_FOLDER` (defaults to the first
entry from `GET /api/v1/rootfolder`; the app can verify it equals or contains `DOWNLOAD_PATH` as seen from Lidarr's
container, which may be a different mount path).

### 2.2 Folder layout and files placed by another tool

Lidarr's model: root folder > `{Artist Name}` folder (default `ArtistFolderFormat`) > files named by
`StandardTrackFormat`, default `{Album Title} ({Release Year})/{Artist Name} - {Album Title} - {track:00} - {Track Title}`.
`RenameTracks` defaults to `false`, so Lidarr does not rename files it did not import unless told to.

The wiki is blunt: "Lidarr owns its root folder. It is the only thing that should be placing files there.
Copying or moving files directly into the root folder -- or into an artist subfolder inside it -- bypasses the
import pipeline, and Lidarr will not pick them up." The supported path is to put files elsewhere (a download
folder) and use Wanted > Manual Import (`GET /api/v1/manualimport?folder=...` then `POST /api/v1/manualimport`
or the `ManualImport` command), after which Lidarr moves them into the root folder itself.

What actually happens on a disk scan of an artist folder (`DiskScanService.Scan`): every audio file under the
artist folder is listed, new files go through `ImportDecisionMaker` with `IncludeExisting = true`, and files that
cannot be matched are still inserted as `TrackFile` rows without an album/track link ("Inserted N new unmatched
trackfiles"). Those are the "Unmapped Files" in the UI (`GET /api/v1/trackfile?unmapped=true`). Matching uses
tags first (MusicBrainz Recording/Release ids weighted 10.0 and 5.0, then artist/album/title/track number/
duration distance), then AcoustID fingerprinting if `allowFingerprinting` permits (default `newFiles`, so
fingerprinting is *not* used for files found during a library rescan), then the Munkres assignment against
MusicBrainz release track lists. A file can only map when:

1. the artist already exists in Lidarr (or `addNewArtists` is true and the folder name resolves to an MB artist), and
2. the album (MB release group) exists in Lidarr's database for that artist (Lidarr only knows albums it has
   refreshed from MusicBrainz for that artist), and
3. the file's tags/duration line up with one release of that album closely enough.

So for this app's `Artist/Album/track.flac` layout: the layout itself is compatible (artist folder inside the
root, an album subfolder, free file naming since renaming is off by default), and Lidarr will see the files on
`RescanFolders`/`RefreshArtist`. Whether they become *mapped* depends entirely on tags. Single YouTube rips of a
song with `ALBUM` = "Unknown Album" or a made-up album name will stay unmapped forever; a full album folder with
correct `ALBUM`, `ALBUMARTIST`, `TRACKNUMBER`, `TITLE` and ideally `MUSICBRAINZ_ALBUMID` maps cleanly. Mapped
files show as "downloaded" for that album and stop Lidarr from grabbing it again; unmapped ones are harmless
but do not count.

Also: Lidarr tracks files by absolute path (`TrackFile.path`). If this app moves or deletes a file that Lidarr
had mapped, Lidarr flags the track as missing on its next scan and (if the album is monitored) may search for
it again. Trigger `RescanFolders` after moves/deletes too, and consider unmonitoring via the API if needed.

### 2.3 Lidarr writing tags

Settings > Metadata: "Tag Audio Files with Metadata" = `writeAudioTags` on `/api/v1/config/metadataprovider`,
values `no` (default) / `newFiles` / `allFiles` / `sync`; `scrubAudioTags` (default false); `embedCoverArt`.
When enabled Lidarr writes (via TagLib, `AudioTagService`/`AudioTag.cs`) `TITLE`, `ARTIST` (Performers),
`ALBUMARTIST`, `ALBUM`, track/disc numbers and totals, `DATE`, `ORIGINALDATE`, label, genres, `MEDIA`,
`MUSICBRAINZ_ALBUMID`, `MUSICBRAINZ_ARTISTID`, `MUSICBRAINZ_ALBUMARTISTID`, `MUSICBRAINZ_RELEASEGROUPID`,
`MUSICBRAINZ_TRACKID`, `MUSICBRAINZ_RELEASETRACKID`, release status/type/country, `MUSICBRAINZ_ALBUMCOMMENT`,
and optionally the cover. With `scrubAudioTags` it calls `RemoveAllTags` first and rewrites from scratch.

Conflict analysis: it only writes to files it has *mapped*; unmapped files are never touched. With the default
`no` there is no conflict at all. With `allFiles`/`sync` Lidarr will overwrite the app's tags on mapped files
with MusicBrainz canonical values, which is usually an upgrade, not a fight, since both sides target the same
MusicBrainz ids. The only real hazard is `scrubAudioTags=true`, which would strip any extra fields the app writes
(for example a `COMMENT` with the source URL). Recommendation: leave Lidarr at `no` or `newFiles`, never scrub,
and let this app be the tagger for its own downloads. The app can read the setting via
`GET /api/v1/config/metadataprovider` and warn if `scrubAudioTags` is on.

---

## 3. Tag lookup from Python for a FLAC with only a title (and maybe artist)

Reality check on the input: yt-dlp titles are things like `Artist - Song (Official Video)`, `Song | Artist`,
`Song (Lyrics)`, `Artist - Album (Full Album)`, with `ARTIST` = channel name (`ArtistVEVO`, `Topic` channels
are decent: "Artist - Topic" plus a proper `album` field from YouTube Music). Any text search benefits from a
cleanup pass first: strip bracketed `(Official ...)`, `[HD]`, `Lyrics`, `feat.` normalisation, split on
` - ` / ` | ` to guess artist/title, and use `duration` (already in `TrackMetadata`) as a tiebreaker. Every
service below returns a duration; a match whose length differs by more than ~5 s is almost always wrong.

### 3.1 Comparison

| Route | Needs | Rate limit | Match quality for YouTube titles | Cover art | Notes |
|---|---|---|---|---|---|
| MusicBrainz search (`musicbrainzngs`) | User-Agent with contact (mandatory, `UsageError` otherwise). No key. | 1 req/s per IP (hard; 503 on burst). Lib enforces 1/s by default. | Fair. Lucene search: `search_recordings(recording=..., artist=..., dur=...)`. Returns many recordings per song (every release), so pick by score + duration + `release-group primary-type == Album`. Weak on remixes/live and on typos. | Cover Art Archive: `https://coverartarchive.org/release/{mbid}/front-500` (307 redirect, no rate limit). Many releases have no art; `release-group/{rgid}/front` is a better fallback. Verified: `/release-group/{rgid}/front-500` 404 when RG has no art. | Gives every MBID Navidrome and Lidarr want. This is the only route that produces ids. |
| AcoustID (`pyacoustid` + `fpcalc`) | Free application API key from acoustid.org; `apt install libchromaprint-tools` (provides `fpcalc`, ~2 MB, depends on ffmpeg libs already present). | 3 req/s (lib enforces). | Best when the audio is the studio recording: fingerprint match is independent of the title text. Weak for live/covers/fan uploads/sped-up versions (which is a feature: it tells you the file is not the canonical track). Returns MB recording id + `score`; you then hit MusicBrainz for release/album details, so MB rate limit still applies. | via MusicBrainz ids as above | `acoustid.match(key, path)` yields `(score, recording_id, title, artist)`; use `meta="recordings releasegroups"` on the raw `lookup` for album info. |
| Deezer public search | Nothing. | 50 req / 5 s, error code 4 `Quota limit exceeded`. | Good fuzzy text search, tolerant of noise. `q=artist:"x" track:"y"` for strict, plain `q=` for fuzzy. **Caveat found today**: from this network every search returned `{"data":[],"total":37}`, i.e. results exist but are withheld (region/IP gating); `/album/{id}` worked and returned full data. Test from the homelab's egress before relying on it. | `album.cover_xl` (1000x1000 jpg), `album.cover_big` (500) directly in the search result; `/album/{id}` also gives `release_date`, `nb_tracks`, `upc`, `label`, `genres`. | No MusicBrainz ids. ISRC is on `/track/{id}` and can be used as `search_recordings(isrc=...)` to bridge to MB with a very precise hit. |
| iTunes Search API | Nothing. | "approximately 20 calls per minute" (documented), so at most one lookup per 3 s. | Good fuzzy search, ranks popular studio tracks first; `entity=song&media=music&limit=5`. Verified today: `daft punk one more time` -> Discovery, track 1/14, disc 1/1, releaseDate, genre, 320357 ms. | `artworkUrl100` -> replace `100x100bb` with `600x600bb` or `1200x1200bb` in the URL (undocumented but stable). Apple's terms say art is for display next to a store badge; caching it into your files is a licence grey area. | No MBIDs. Track numbers and album totals are accurate. Rate limit is the blocker for bulk. |
| Last.fm | API key (free). | Unpublished, error 29 on abuse. | `track.search` is fuzzy; `track.getInfo(autocorrect=1)` fixes spelling. Album data is thin (only the one album Last.fm associates). | `album.image` small/medium/large (~300 px), often the wrong or a generic image. | Sometimes returns `mbid` fields, but they are stale. Not worth a dependency here. |

### 3.2 Concrete calls

MusicBrainz (verified today: score 100, recording `aed95205-f79f-4181-b2f7-2c2cb226f5bc`, length 320000):

```python
import musicbrainzngs as mb
mb.set_useragent("music-for-arr", "0.2", "https://github.com/<you>/music-for-arr")   # required
mb.set_rate_limit(1.0, 1)                                                              # default; keep it

res = mb.search_recordings(recording="One More Time", artist="Daft Punk", dur=320000, limit=10)
for rec in res["recording-list"]:
    # rec["ext:score"], rec["id"], rec["title"], rec["length"] (ms), rec["artist-credit"], rec["release-list"]
    ...
# pick a release: prefer release-group primary-type Album, status Official, earliest date, country matching your locale
rel = mb.get_release_by_id(release_id, includes=["artists", "release-groups", "recordings", "media"])["release"]
# rel["id"] -> MUSICBRAINZ_ALBUMID ; rel["release-group"]["id"] -> MUSICBRAINZ_RELEASEGROUPID
# rel["artist-credit"][0]["artist"]["id"] -> MUSICBRAINZ_ALBUMARTISTID ; medium-list gives disc/track numbers
# artwork:
try:
    img = mb.get_image_front(release_id, size=500)           # bytes (jpeg); raises ResponseError 404 if none
except mb.ResponseError:
    img = mb.get_release_group_image_front(rg_id, size=500)  # fallback
```

Raw HTTP equivalent: `GET https://musicbrainz.org/ws/2/recording/?query=recording:"one more time" AND artist:"daft punk"&fmt=json&limit=5`
with header `User-Agent: music-for-arr/0.2 ( contact@example )`.

AcoustID:

```python
import acoustid   # pip install pyacoustid ; apt-get install libchromaprint-tools (fpcalc)
duration, fp = acoustid.fingerprint_file(path)                      # shells out to fpcalc (or FPCALC env var)
resp = acoustid.lookup(os.environ["ACOUSTID_API_KEY"], fp, duration, meta="recordings releasegroups")
for result in resp["results"]:                                       # result["score"] 0..1, result["recordings"][..]["id"]
    ...
# or the shortcut:
for score, recording_id, title, artist in acoustid.match(key, path): ...
```

Deezer / iTunes:

```python
requests.get("https://api.deezer.com/search", params={"q": 'artist:"Daft Punk" track:"One More Time"', "limit": 5})
# -> data[i]: title, duration (s), artist.name, album.id, album.title, album.cover_xl, link
requests.get("https://itunes.apple.com/search", params={"term": "daft punk one more time", "media": "music", "entity": "song", "limit": 5, "country": "DE"})
# -> results[i]: artistName, collectionName, trackName, trackNumber, trackCount, discNumber, releaseDate, primaryGenreName, trackTimeMillis, artworkUrl100
```

### 3.3 Writing the tags with mutagen (FLAC)

Vorbis comments are a dict of lists; keys are case-insensitive but write them uppercase. Field names below are
the Picard standard, which is what Navidrome, Lidarr (TagLib) and beets all read.

| Field | Vorbis key | Value | Read by Navidrome | Read by Lidarr |
|---|---|---|---|---|
| Title | `TITLE` | text | yes | yes |
| Track artist | `ARTIST` (+ `ARTISTS` one per credit) | text | yes | yes |
| Album artist | `ALBUMARTIST` | text, identical on all tracks of the album | yes (grouping key) | yes |
| Album | `ALBUM` | text | yes | yes |
| Date | `DATE` | `YYYY-MM-DD` or `YYYY` | yes (recordingdate) | yes |
| Original date | `ORIGINALDATE` / `ORIGINALYEAR` | | yes | yes |
| Track number | `TRACKNUMBER` | `n` (not `n/total`) | yes | yes |
| Track total | `TRACKTOTAL` (Picard also writes `TOTALTRACKS`) | | | yes |
| Disc number / total | `DISCNUMBER` / `DISCTOTAL` | | yes | yes |
| Genre | `GENRE` | | yes | yes |
| Compilation | `COMPILATION` | `1` | yes | |
| Recording id | `MUSICBRAINZ_TRACKID` | recording MBID (yes, the name is historical) | yes (track PID) | yes (weight 10) |
| Release track id | `MUSICBRAINZ_RELEASETRACKID` | | yes | yes |
| Release id | `MUSICBRAINZ_ALBUMID` | release MBID, identical across the album | yes (album PID) | yes (weight 5) |
| Release group id | `MUSICBRAINZ_RELEASEGROUPID` | | yes | yes |
| Artist id | `MUSICBRAINZ_ARTISTID` | | yes | yes |
| Album artist id | `MUSICBRAINZ_ALBUMARTISTID` | | yes | yes |
| Release type | `RELEASETYPE` | `album`/`single`/`ep` | yes | yes |
| ISRC | `ISRC` | | | |
| Source URL (own) | `COMMENT` or `PURL` (yt-dlp already writes `PURL`) | keep for provenance | | |
| Cover | FLAC picture block (`METADATA_BLOCK_PICTURE`), type 3 | jpeg/png bytes | yes (`embedded`) | yes |

```python
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

f = FLAC(path)
f.update({                       # values must be str or list[str]
    "TITLE": title, "ARTIST": artist, "ALBUMARTIST": album_artist, "ALBUM": album,
    "DATE": date, "TRACKNUMBER": str(track_no), "TRACKTOTAL": str(track_total),
    "DISCNUMBER": str(disc_no), "DISCTOTAL": str(disc_total),
    "MUSICBRAINZ_TRACKID": recording_mbid, "MUSICBRAINZ_ALBUMID": release_mbid,
    "MUSICBRAINZ_RELEASEGROUPID": rg_mbid, "MUSICBRAINZ_ARTISTID": artist_mbid,
    "MUSICBRAINZ_ALBUMARTISTID": album_artist_mbid, "RELEASETYPE": "album",
})
for k in ("DESCRIPTION", "SYNOPSIS"):    # yt-dlp/ffmpeg dump the video description here; drop it
    f.pop(k, None)
if cover_bytes:
    pic = Picture(); pic.type = PictureType.COVER_FRONT; pic.mime = "image/jpeg"
    pic.data = cover_bytes; pic.width = pic.height = 500; pic.depth = 24
    f.clear_pictures(); f.add_picture(pic)
f.save()
(path.parent / "cover.jpg").write_bytes(cover_bytes)   # what Navidrome checks first
os.utime(path.parent)                                    # bump folder mtime so Navidrome quick scan re-reads it
```

Notes: `f.save()` rewrites the file in place; on big FLACs mutagen may need to rewrite the whole file if the
padding is too small (still under a second). Keep `pictures` to one front cover; yt-dlp's thumbnail is often
a 16:9 video frame, so replace it rather than append. Only write MB ids when the whole album resolved to a
single release; for a lone single write text tags plus `RELEASETYPE=single` and leave `MUSICBRAINZ_ALBUMID` off.

---

## 4. beets as an embedded library?

Technically possible, practically the wrong tool for a FastAPI container:

- beets has no stable public Python API. The maintainers' answer (beets-users thread, Adrian Sampson) is to
  subclass `beets.importer.ImportSession` and replace the terminal I/O pieces, as their own `TestImportSession`
  does; everything else (`beets.ui._open_library`, `beets.ui.commands.import_files`) prints to stdout and
  expects a config file in the user's home. The `dev/api` docs page is not published for current versions
  (404 on both readthedocs and docs.beets.io), only the plugin guide is.
- It insists on owning a library: an SQLite `library.db`, `directory` it copies/moves into
  (`import.copy` default yes, `import.move` no), path templates it renames by, and `import.write` (default yes).
  You would have to set `copy: no`, `move: no`, `autotag: yes`, `quiet: yes`, `quiet_fallback: skip`, and accept
  a second database that duplicates the app's persistent queue/collection state.
- It is album-oriented; singles go through the `singletons` path with weaker matching. Its strength (interactive
  album matching with `chroma`, `fetchart`, `lastgenre`, `scrub`, `replaygain`) is the interactive part.
- Dependency weight: beets + plugins pulls in `pyacoustid`, `requests`, `PyYAML`, `confuse`, `mediafile`,
  `munkres`, `unidecode`, `musicbrainzngs`, so you install everything you would hand-roll anyway, plus the CLI machinery.
- The MusicBrainz rate-limit page singles out the `beets` User-Agent for the throttled 50 req/s pool, which
  suggests how noisy it is when run unattended.

Where beets does help: if the user later wants a one-shot "clean up my whole existing library" pass, running
`beet import -A` / `beet import` interactively against `DOWNLOAD_PATH` from a sidecar container is simpler
than reimplementing album-level matching. For per-download tag fixing inside the API, hand-rolled is simpler:
roughly 150 lines (title cleanup, MB search with duration filter, optional AcoustID, CAA/iTunes art, mutagen write).

---

## 5. Recommendation

1. **Scan triggers.** After any write/move/delete under `DOWNLOAD_PATH`: touch the affected album folder,
   call Navidrome `GET /rest/startScan?fullScan=false` (token+salt auth, dedicated admin user), and Lidarr
   `POST /api/v1/command {"name":"RescanFolders","folders":[root]}` (or `RefreshArtist` with the artist id when
   the artist is known, since that also pulls the album list needed for mapping). Poll `getScanStatus` /
   `command/{id}` only to report status; never block the queue on them. Both steps are optional and skipped
   when the env vars are absent.
2. **Env vars.** `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD`, `LIDARR_URL`, `LIDARR_API_KEY`,
   optional `LIDARR_ROOT_FOLDER`, optional `ACOUSTID_API_KEY`, and `MUSICBRAINZ_CONTACT` (email or URL for the
   User-Agent). No key for MusicBrainz, CAA, iTunes or Deezer.
3. **Lookup pipeline** (best effort, first confident hit wins):
   a. Title cleanup + duration from yt-dlp metadata.
   b. If `ACOUSTID_API_KEY` set and `fpcalc` present: AcoustID -> recording MBID (score >= 0.9).
   c. Else MusicBrainz `search_recordings(recording, artist, dur)`; accept when score >= 90 and |length - duration| <= 5 s.
   d. Resolve release: prefer Album > EP > Single, Official, earliest date. Art from CAA release, then release group.
   e. If MB gives no art, iTunes `artworkUrl100` -> `600x600bb` (one call, respects the 20/min budget). Deezer only
      if a probe from the deployment network returns non-empty `data`.
   f. Write tags with mutagen per the table; write `cover.jpg` in the album folder; MB ids only for whole-album matches.
   g. Surface a "matched / unmatched / low confidence" state per file so the UI can show what happened (feeds ticket 10).
4. **Lidarr folder question (from MAP "Not yet specified").** No conflict: keep `Artist/Album/track.flac`. Lidarr
   reads it fine with `RenameTracks=false` (default) and files map when tags are right; unmapped files are
   harmless. Advise users to set Lidarr "Tag Audio Files" to `no` or `newFiles` and never `scrubAudioTags`, and
   have the app warn (via `GET /api/v1/config/metadataprovider`) if scrubbing is on.
5. **Skip beets** as a library dependency. Add `musicbrainzngs`, `pyacoustid` (optional) and
   `libchromaprint-tools` to the image; `mutagen` and `ffmpeg` are already there.
6. **Open risks.** Deezer geo-gating (verify from the homelab); Navidrome watcher not firing on network mounts
   (the explicit `startScan` covers it); MusicBrainz 1 req/s makes bulk-artist tag fixing slow (100 tracks ~ 2-3 min
   with release lookups), so run tag fixes through the same persistent queue as downloads rather than inline.
