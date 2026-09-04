# Delete semantics

Label: `wayfinder:grilling`
Status: closed (2026-09-03)
Assignee: claude (2026-09-03)
Blocked by: 03

## Question

What can be deleted (track, album folder, artist folder), whether deletion is permanent or goes to a trash folder inside `DOWNLOAD_PATH`, what confirmation is required, and what Navidrome and Lidarr do when files vanish under them. Also: can an in-flight job be deleted, and does that cancel it?

## Resolution

Grilled with the user on 2026-09-03. Builds on [Domain model](03-domain-model.md) and
[Library view and move UX](04-collection-view-ux.md).

**Trash, not permanent.** Delete moves the item to `DOWNLOAD_PATH/.trash/<UTC timestamp>/<original relative path>`
with a same-filesystem rename (no copy). The dot-folder is excluded from the Library scan, and Navidrome and
Lidarr ignore dot-folders by default. Nothing auto-expires. The UI has a Trash section listing entries
(original path, deleted-at, track count) with Restore per entry and one "Empty trash" button that removes
`.trash` contents permanently.

**Levels.** Delete is available on track, album, and artist nodes, and on a multi-selection of tracks within one
album. Album and artist deletes move the whole folder as one trash entry so Restore brings it back intact.
Empty-folder cleanup from the move ticket applies after track deletes.

**Confirmation.** One dialog naming the item and its track count ("Move \"Black Sands\" (12 tracks) to trash?")
with Cancel and Delete. Empty trash confirms with the total count. Restore needs no confirmation.

**Restore.** Restores to the original relative path. If that path is occupied the call returns 409 and the UI
offers the move dialog to pick another artist/album for the restored item. Never overwrite.

**Rescan.** Delete, restore, and empty-trash fire the same post-change hook as every file write: touch the
affected album folder, call Navidrome `startScan` and Lidarr `RescanFolders` when configured. The app does not
unmonitor anything in Lidarr. The README must tell the user to set `ND_SCANNER_PURGEMISSING=always` so deleted
tracks leave Navidrome, and to unmonitor Lidarr albums they do not want re-fetched.

**Dedup.** Trash is invisible to dedup; a deleted track can be re-downloaded without a warning.

**In-flight jobs.** Any queued, downloading, or converting Job gets a Cancel action: queued jobs are dropped;
running jobs have the yt-dlp thread interrupted and their temp and partial files removed; the job ends in a
`cancelled` state and leaves the in-flight view. Errored jobs get Dismiss. The mechanics (interrupting yt-dlp,
state transitions, persistence) are specified in [Persistent queue and job model](11-persistent-queue-and-job-model.md),
whose question now names cancel explicitly.

Consequences: the prototype (06) should include the confirm dialog and the Trash section; the persistent
queue ticket (11) owns cancel and the `cancelled` state; the plan (12) needs a README slice for the
Navidrome/Lidarr settings above.

## Amendment (2026-09-03, from [Library view prototype](06-collection-view-prototype.md))

Trash is its own tab, shown only when it contains items, with an item-count badge. Loose tracks
(`Artist/track.flac`) trash and restore like any other track.
