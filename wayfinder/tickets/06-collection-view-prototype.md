# Library view prototype

Label: `wayfinder:prototype`
Status: closed (2026-09-03)
Assignee: claude (2026-09-03)
Blocked by: 04, 05

## Question

Build a rough clickable mock of the library tree with the move dialog, multi-select, and delete, using the decisions from the UX and delete tickets, so the layout can be judged before the plan is written. Link the mock from this ticket.

## Assets

- Clickable mock: https://claude.ai/code/artifact/6e0217d3-e02f-4075-a920-af7be1f89668 (source copy: [prototypes/library-mock.html](../prototypes/library-mock.html))

## Resolution

Two revisions on 2026-09-03; revision 2 locked in by the user. The mock (link under Assets) is the reference
for the library UI in the plan. What the prototype changed versus the earlier decisions, each recorded as an
amendment on the ticket that owns it:

- **Layout is a grid, not a tree** (amends [Library view and move UX](04-collection-view-ux.md)): artist tiles
  with cover art, then album tiles, then a numbered track list under an album header with art. Breadcrumb
  navigates back; artist-level actions (Rename, Delete artist) and album-level actions (Move album, Delete album)
  sit on the breadcrumb row. The search box returns flat results across artists, albums, and tracks.
- **Pages are tabs** (amends 04): Download (form plus in-flight queue, with an active-job count badge), Library,
  and Trash. The Trash tab is hidden when empty and appears with an item count once something is deleted
  (amends [Delete semantics](05-delete-semantics.md), which had Trash as a section).
- **Cover art**: `GET /library/cover?path=Artist/Album` serves the embedded picture of the first track, else
  `cover.jpg` in the folder, else a generated placeholder; cached on disk. Artist tiles use the first album's art.
- **Format badge only for non-FLAC** tracks; FLAC rows show no badge (amends 04).
- **Loose tracks at depth 2 are legitimate** (amends [Domain model](03-domain-model.md)): a track may live at
  `Artist/track.flac`. The artist page shows them in a "Singles" section. The move dialog's Album field is
  optional; blank means loose, and the `ALBUM` tag is cleared. Files at the library root still show under
  `Unknown Artist` as needing sorting.

Everything else in the mock matches the earlier decisions: move dialog with comboboxes and free text, 409 shown
inline, multi-select within one album or within Singles, delete confirm with name and count, Restore with 409
on conflict, Empty trash confirm, Cancel on in-flight jobs.
