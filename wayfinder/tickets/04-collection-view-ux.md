# Library view and move UX

Label: `wayfinder:grilling`
Status: closed (2026-09-03)
Assignee: claude (2026-09-03)
Blocked by: 03

## Question

How does the user see the whole collection and move a miscategorised file? Decide: tree versus flat list; what each row shows (title, duration, size, tags); move at track level only or also album level; how a move is performed (picker of existing artists, free text creating a new folder, drag and drop); whether moving also rewrites the artist tag in the file; what happens to empty folders after a move; how the view refreshes after downloads finish.

## Resolution

Grilled with the user on 2026-09-03. Uses the vocabulary and path identity from
[Domain model](03-domain-model.md).

**Layout.** A collapsible tree, Artist > Album > Track, on the same page below the in-flight queue. Artists
are collapsed by default and show album and track counts; albums show track counts. One filter box above the
tree filters artists, albums, and tracks by name client-side and auto-expands matching branches. Synthetic
`Unknown Artist` / `Unknown Album` buckets render like any other node but carry a marker so they read as
"needs sorting".

**Track row.** Title (tag, falling back to filename), duration, a small format badge (FLAC, MP3, ...), and
hover/focus actions: Move, Delete. Size and the full tag set live in a detail popover opened from the row.
The backend supplies title and duration from a cheap tag read during the scan; nothing heavier.

**Move levels.** Tracks move to any artist/album. Albums move to another artist. Artists are renamed, not moved.
A move whose target parent is unchanged and whose name differs is a rename, so rename shares the move dialog.

**Picker.** "Move" on a row opens a dialog with an Artist combobox (existing folder names, type to filter,
typing a new name creates the folder) and an Album combobox scoped to the chosen artist. Artist rename shows
only the Artist field. No drag and drop in v2.

**Multi-select.** Checkboxes on track rows within one album; "Move selected" and "Delete selected" act on the
set. Selection clears on refetch. No cross-album selection.

**Tags on move.** A move or rename rewrites `ALBUMARTIST`, `ARTIST`, and `ALBUM` in every affected file to the
new folder names, preserving every other field including `SOURCEID` / `SOURCEURL`. Non-FLAC files are moved
without a tag rewrite (mutagen can do MP3/M4A later if wanted; not in the plan).

**Album merge.** Moving an album onto an artist that already has an album with that name merges: files that do
not exist in the target are moved; if any filename already exists the whole operation is refused with 409
listing the conflicts, so nothing is half-moved. Same all-or-nothing rule for multi-select moves.

**Empty folders.** After any move or delete, an album folder with no audio files left is removed together with
non-audio leftovers (cover.jpg, .nfo); then the same check runs on the artist folder.

**Data layer and refresh.** The frontend adopts TanStack Query. `GET /library` is a query; move, delete, and
rename are mutations that invalidate it. The existing SSE stream keeps flowing: job events patch the `queue`
query via `setQueryData`, and a job reaching `done` (or a `library_changed` event the backend emits after any
file write, move, or delete) invalidates the `library` query. `refetchOnWindowFocus` covers changes made
outside the app. The hand-rolled `useSSE` state and its reconciliation refetch are replaced by the query cache;
`GET /queue` becomes the query's fetcher. No polling.

Consequences: the delete ticket (05) inherits the empty-folder rule, multi-select, and the 409-style errors;
the prototype ticket (06) should mock the tree, the move dialog, and multi-select; the plan (12) gets a
"TanStack Query migration" slice that lands before the library UI; the metadata ticket (10) should note that
move already rewrites the three attribution tags.

## Amendment (2026-09-03, from [Library view prototype](06-collection-view-prototype.md))

The tree is replaced by a grid: artist tiles (cover art, name, counts) > album tiles (cover art) > numbered track
list under an album header. Breadcrumb for navigation; artist and album actions sit on the breadcrumb row.
The single page becomes tabs: Download (form plus in-flight queue with an active-count badge), Library, and
Trash (hidden when empty). Search returns flat results across all three levels. Format badge shows only on
non-FLAC tracks. The move dialog's Album field is optional: blank places the track loose under the artist and
clears the `ALBUM` tag. Cover art is served by `GET /library/cover?path=...` from the embedded picture, then
`cover.jpg`, then a generated placeholder, cached on disk. TanStack Query and SSE invalidation are unchanged.
