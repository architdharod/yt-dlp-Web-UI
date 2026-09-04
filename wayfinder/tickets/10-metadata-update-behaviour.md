# Metadata update behaviour

Label: `wayfinder:grilling`
Status: closed (2026-09-03)
Assignee: claude (2026-09-03)
Blocked by: 03, 09

## Question

What does the metadata button do? Decide: per-track, per-album, per-artist, or whole library trigger; which lookup source and in what order; whether user-provided artist and album ever get overwritten; whether a fixed tag also moves the file to the corrected folder; whether the Navidrome and Lidarr rescan calls fire automatically after each download; how progress and failures are shown.

## Resolution

Grilled with the user on 2026-09-03. Builds on [Domain model](03-domain-model.md) and the research in
[research/navidrome-lidarr-and-tagging.md](../research/navidrome-lidarr-and-tagging.md).

**Two things, one hook.** "Update metadata" is (a) a tag fix inside the FLAC and (b) a rescan request to
Navidrome and Lidarr. The rescan is not tied to the button: one debounced **rescan hook** fires after any
file change (download done, tag fix, move, delete, restore, empty trash). It waits a few seconds of quiet,
`utime`-touches every album folder that changed, then calls Navidrome `startScan` (quick scan) and Lidarr
`RescanFolders` once. Each service is skipped, not failed, when its env vars are unset.

**Triggers.** Per track (row action) and per album (album tile action). No per-artist or whole-library button.
Tag fixing also runs **automatically after every download**, per track, as a `tagging` state on the download
job itself, after the file is already usable. The album pass (track numbers, cover art) runs only from the
album button, never automatically. Only FLAC files take part (domain model rule).

**Lookup.** MusicBrainz text search only (`musicbrainzngs`, mandatory User-Agent, 1 req/s). No AcoustID, no
iTunes, no Deezer. Query: cleaned title (bracketed `(Official Video)`, `[Lyrics]`, `- Topic` and similar noise
stripped, ` - ` / ` | ` split to guess artist and title), artist = folder name, `dur` = file duration.

**Match bar.** A recording counts as matched only when duration is within 5 s, the normalised title equals
the recording title, and the artist credit matches the artist folder (case-insensitive, normalised). Below
that, nothing in the file changes; the rescan still fires.

**Fields written on a match.** `TITLE` (recording title), `ARTIST` (MusicBrainz artist credit, so
featuring credits appear). yt-dlp's `DESCRIPTION`, `PURL`-style and other junk fields are removed.
`SOURCEID` and `SOURCEURL` are always preserved. `ALBUMARTIST` and `ALBUM` are **never overwritten** by the
lookup: they stay what the folders say, so tags and folders can never disagree. No `DATE`, no `GENRE`.
**No MusicBrainz ids are ever written.** Navidrome therefore groups on `ALBUMARTIST + ALBUM`.

**Album pass** (album button only). Every track in the folder is looked up; if all of them map to one
MusicBrainz release, the pass also writes `TRACKNUMBER` and `DISCNUMBER` from that release and fetches
`cover.jpg` into the album folder from Cover Art Archive (release front, then release-group front; the release
id is used transiently, never written; an existing `cover.jpg` is never overwritten; embedded thumbnails are
left alone). If only some tracks match one release, the pass falls back to per-track fixes (title and artist
for whatever matched), writes no numbers and no cover, and reports `partial: 9 of 12`.

**Loose Singles** (`Artist/track.flac`): title and artist only. `ALBUM` untouched, no cover art at all.

**Jobs and progress.** Manual runs are `tagging` Jobs in the in-flight queue, with N of M progress for album
runs, disappearing when done like any job; failures stay as an error job with the reason and a Dismiss. One
dedicated tagging worker, separate from the download slots, serialises all MusicBrainz traffic; tagging jobs
are cancellable like downloads. No match memory: nothing is stored about matched/unmatched tracks, re-runs
always re-query, and the library view shows no marker.

**Failures.** A download job always completes even if its automatic tag fix fails; the job detail then says
"tags not fixed". Manual tagging jobs go to error. Rescan hook failures (unreachable, bad credentials, non-admin
Navidrome user) surface once as a dismissible banner, not per file, and are logged.

**Service config.** `NAVIDROME_URL`, `NAVIDROME_USER` (admin), `NAVIDROME_PASSWORD` (token+salt derived per
request); `LIDARR_URL`, `LIDARR_API_KEY`, optional `LIDARR_ROOT_FOLDER` (default: first root folder). Lidarr
is called with `filter=known`, `addNewArtists=false`, so only artists already in Lidarr are rescanned. At
startup the app reads Lidarr's metadata-provider config and warns in the banner if `scrubAudioTags` is on.
README documents `ND_SCANNER_PURGEMISSING` (from the delete ticket) and the admin-user requirement.

Consequences: the persistent queue ticket (11) needs a `tagging` job kind, a `tagging` state on download jobs,
and the tagging worker; the plan (12) must schedule the rescan hook before the library actions that call it.
