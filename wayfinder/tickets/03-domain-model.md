# Domain model: how the app identifies tracks, albums, and artists on disk

Label: `wayfinder:grilling`
Status: closed (2026-09-03)
Assignee: claude (2026-09-03)
Blocked by: 

## Question

The new features (browse, move, delete, tag update, dedup) all need a stable way to name a file and a folder. Decide: is the identity a path relative to `DOWNLOAD_PATH`, or an id in a database? How are artist and album folders represented and validated so that `..`, absolute paths, and odd characters can never escape the root? How does dedup compare a source track to a file on disk (filename, embedded tags, source URL stored in a tag)? What is the vocabulary: Library, Artist, Album, Track, Job, Download?

## Resolution

Grilled with the user on 2026-09-03. Decisions:

**Vocabulary.** `Library` is the `DOWNLOAD_PATH` tree. `Artist` and `Album` are folders (depth 1 and 2).
`Track` is an audio file at depth 3. `Job` is a queue entry (single, or a child of a bulk request).
"Download" is only a verb. UI copy that says "collection" changes to "library".

**Identity.** A Track, Album, or Artist is identified by its path relative to `DOWNLOAD_PATH`, as a POSIX
string (`Bonobo/Black Sands/Kiara.flac`). The filesystem is the only source of truth for the Library; there is
no library table. A move changes the id and the UI re-reads the tree. SQLite (persistent queue ticket) holds Jobs only.

**API shape.** The id travels as a JSON body field `path`. `GET /library` returns the tree; actions are
`POST /library/move`, `POST /library/delete`, etc. with paths in the body. No paths in URL segments.

**Validation.** Every incoming path is split on `/`; each segment must be non-empty, not `.` or `..`,
contain no `\` or NUL, and the resolved absolute path must satisfy `is_relative_to(DOWNLOAD_PATH.resolve())`
(same belt-and-braces check `file_organizer.get_output_path` already does). New folder names created by a move
or by bulk placement go through `sanitize_component`. Existing folder names, however odd, are accepted as
targets exactly as they are on disk. Symlinks are resolved and must still land under the root.

**What is a Track.** Any file at depth 3 with an audio extension (flac, mp3, m4a, ogg, opus, wav). Only FLAC
takes part in tag fixing and tag-based dedup; other formats get browse, move, and delete only.

**Wrong depth.** Files at the root show under `Unknown Artist / Unknown Album`; files directly under an Artist
show under `Unknown Album`; folders deeper than depth 2 are flattened into their Album. These are synthetic
buckets in the API response (flagged `synthetic: true`), never created on disk by the scanner. Move is the fix;
nothing is auto-tidied.

**Filename.** Stays `<sanitized title>.flac`, no track number. Ordering comes from `TRACKNUMBER` tags.

**Source tag.** Every new download writes Vorbis fields `SOURCEID=<extractor>:<id>` (e.g. `youtube:dQw4w9WgXcQ`)
and `SOURCEURL=<webpage_url>` with mutagen after yt-dlp finishes. Files downloaded before this read yt-dlp's `PURL`
field as the source URL and derive the id from it where the URL shape is known.

**Dedup.** Given a candidate source item and a target Artist (folder matched case-insensitively) plus the target
Album if the user chose one: (1) match on `SOURCEID` / `PURL` across every FLAC under that Artist; (2) if no tag
match, compare normalised titles (lowercase, punctuation and whitespace collapsed, trailing noise such as
`(Official Video)`, `[Lyrics]`, `- Topic` stripped) across every audio file under that Artist. A match on either
means "already present": the row is pre-unticked in the bulk selection and, if submitted anyway, is skipped.

**Collisions.** Never overwrite. A move whose target exists returns 409 with the conflicting path. A download whose
target filename exists is treated as a duplicate and skipped with a visible reason.

**Job to Track.** On completion a Job stores `result_path` (relative, same string as the Library id). One-way
pointer, no foreign key. Bulk children each store their own.

Consequences for other tickets: the persistent queue (11) needs `result_path` and a `parent_id`; the bulk flow (8)
uses the dedup rule above; the metadata ticket (10) must preserve `SOURCEID`/`SOURCEURL` when rewriting tags and
the Lidarr `scrubAudioTags` warning from the tagging research applies.

## Amendment (2026-09-03, from [Library view prototype](06-collection-view-prototype.md))

Loose tracks at depth 2 (`Artist/track.flac`) are a legitimate placement, not a wrong-depth case. The scanner
reports them as the artist's loose tracks (shown as "Singles"); the synthetic `Unknown Album` bucket is no longer
needed. Root-level files still show under `Unknown Artist`. Dedup already scans every audio file under the artist,
so loose tracks are covered.
