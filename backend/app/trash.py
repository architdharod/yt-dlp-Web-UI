"""Delete, list, restore and empty the Library's trash.

Delete never removes anything.  A track, a multi-selection of tracks, an album
folder or an artist folder is moved -- one ``os.rename`` per item, inside the
one filesystem -- to ``DOWNLOAD_PATH/.trash/<id>/<its original relative path>``.
The dot-folder keeps the whole thing out of the library scan, out of dedup, and
out of Navidrome and Lidarr; the original path is preserved inside the entry so
Restore is a rename back to exactly where the item came from.

One delete call is one entry, whatever it moved: an album goes across as a
single folder rename, so its ``cover.jpg``, its ``Disc 1`` subfolder and its
``.nfo`` leftovers travel with it and Restore brings back the identical folder.
Nothing here ever copies, and nothing but Empty trash ever unlinks.

Each entry carries an ``entry.json`` manifest -- what the paths were, what kind
of thing it was, when it was deleted.  The manifest is a convenience, not the
source of truth: an entry whose manifest is missing or unreadable (one a user
made by hand, one from a half-written delete) is still listed and still
restorable, reconstructed from the files that are actually there.  See
:func:`_recover_entry`.

Blocking: every public function does filesystem work and is called through
``asyncio.to_thread`` while :data:`~app.library_ops.LIBRARY_WRITE_LOCK` is
held, so a delete can never interleave with a move's checks and renames.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.file_organizer import FALLBACK_ARTIST, TRASH_DIRNAME
from app.library import (
    AUDIO_EXTENSIONS,
    LibraryNotFound,
    LibraryPathError,
    validate_library_path,
)
from app.library_ops import (
    LibraryConflict,
    PartialRenameError,
    check_ancestors_are_dirs,
    check_in_flight,
    check_not_being_tagged,
    check_inside,
    check_resolved,
    cleanup_upwards,
    folder_name,
    has_audio,
    is_reserved,
    merge_pairs,
    placement_tags,
    rel_path,
    rename_files,
    resolve_destination,
    rewrite_tags,
    same_file,
    walk_files,
)

logger = logging.getLogger(__name__)

# Written into ``.trash`` itself.  Navidrome skips dot-folders today, but that
# is a default rather than a promise, and an ``.ndignore`` is the explicit
# "never index below here" it honours regardless.
NDIGNORE_NAME = ".ndignore"

# One per entry folder.  A plain name rather than a dot-file so it shows up in
# a file manager next to what it describes, and so the fallback that ignores it
# only has to know one name.
MANIFEST_NAME = "entry.json"

# The entry id, and with it the sort order of the Trash tab: a UTC timestamp
# down to the microsecond, which sorts lexicographically exactly as it sorts
# chronologically.  Two deletes inside one microsecond get ``-2``, ``-3``.
_ID_FORMAT = "%Y%m%dT%H%M%S%fZ"

# The kinds an entry can be, as the API spells them.
KIND_ARTIST = "artist"
KIND_ALBUM = "album"
KIND_TRACK = "track"
KIND_TRACKS = "tracks"
_KINDS = frozenset({KIND_ARTIST, KIND_ALBUM, KIND_TRACK, KIND_TRACKS})


@dataclass
class TrashEntry:
    """One ``.trash/<id>/`` folder, in the vocabulary the API answers in.

    ``path`` is the single thing the user deleted -- the artist, the album, the
    track -- or, for a multi-track delete, the folder they all came out of, so
    the Trash tab has one line to show per entry.  ``paths`` is everything the
    entry actually holds, which is what Restore puts back.
    """

    id: str
    path: str
    kind: str
    paths: list[str] = field(default_factory=list)
    deleted_at: str = ""
    track_count: int = 0


@dataclass
class DeleteOutcome:
    """What one delete did: the entry it made and the folders it tidied away.

    ``changed`` are the library folders the rescan hook and ``library_changed``
    are told about -- the surviving parents of what was removed, never the
    paths that have just stopped existing.
    """

    entry: TrashEntry
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


@dataclass
class RestoreOutcome:
    """What one restore put back, and the folders it changed doing so."""

    restored: list[dict[str, str]] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The trash root
# ---------------------------------------------------------------------------


def trash_root(root: Path) -> Path:
    """Where the trash lives for the library at *root*."""
    return root / TRASH_DIRNAME


def ensure_trash_root(root: Path) -> Path:
    """Create ``.trash`` and its ``.ndignore``, and return it.

    Called on every delete rather than once at boot: the folder is the user's
    to remove, and a library that has never had anything deleted should not
    grow an empty ``.trash`` just from starting the app.  The ``.ndignore`` is
    (re)created whenever it is missing, so a trash root made by an older
    version -- or by hand -- gains one at the next delete.
    """
    base = trash_root(root)
    base.mkdir(parents=True, exist_ok=True)
    marker = base / NDIGNORE_NAME
    if not marker.exists():
        try:
            marker.touch()
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Could not write %s: %s", marker, exc)
    return base


def _new_entry_dir(base: Path) -> tuple[Path, str]:
    """Make and return a fresh ``<id>`` folder under *base*.

    ``mkdir`` without ``exist_ok`` is the claim: whichever caller creates the
    name owns it, so two deletes in the same microsecond cannot end up writing
    into one entry even if the write lock were ever dropped.
    """
    stamp = datetime.now(timezone.utc).strftime(_ID_FORMAT)
    suffix = 1
    while True:
        entry_id = stamp if suffix == 1 else f"{stamp}-{suffix}"
        candidate = base / entry_id
        try:
            candidate.mkdir()
        except FileExistsError:
            suffix += 1
            continue
        return candidate, entry_id


def _iso_now() -> str:
    """The current instant as the API's ``2026-09-04T18:22:31Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _deleted_at_from_id(entry_id: str) -> str:
    """*entry_id*'s timestamp as an ISO instant, or ``""`` when it is not one.

    The fallback for an entry with no readable manifest: the folder name is
    itself a UTC timestamp, so a hand-made entry that happens to follow the
    convention still sorts and displays correctly.
    """
    head = entry_id.split("-", 1)[0]
    try:
        moment = datetime.strptime(head, _ID_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Reading an entry off the disk
# ---------------------------------------------------------------------------


def _entry_files(entry_dir: Path) -> list[str]:
    """Every file inside *entry_dir* except the manifest, entry-relative."""
    found: list[str] = []
    for path in walk_files(entry_dir):
        rel = path.relative_to(entry_dir).as_posix()
        if rel == MANIFEST_NAME:
            continue
        found.append(rel)
    return sorted(found)


def _count_audio(entry_dir: Path) -> int:
    """How many audio files the entry holds.

    Counted off the disk on every listing rather than trusted from the
    manifest: the count is what the confirm dialog and the tab badge show, and
    the disk is the only thing that cannot be stale.  A stat per file, no tag
    reads -- the Trash tab must stay cheap however much is in it.
    """
    total = 0
    for _dirpath, _dirnames, filenames in os.walk(entry_dir):
        for name in filenames:
            if Path(name).suffix.casefold() in AUDIO_EXTENSIONS:
                total += 1
    return total


def _common_parent(paths: Iterable[str]) -> str:
    """The deepest folder every one of *paths* sits below, POSIX, possibly ``""``."""
    parts: list[list[str]] | None = None
    for one in paths:
        segments = one.split("/")[:-1]
        if parts is None:
            parts = [segments]
        else:
            parts.append(segments)
    if not parts:
        return ""
    shared: list[str] = []
    for column in zip(*parts):
        first = column[0]
        if any(value != first for value in column):
            break
        shared.append(first)
    return "/".join(shared)


def _recover_entry(entry_dir: Path, entry_id: str) -> TrashEntry | None:
    """Rebuild an entry from the files in it, with no manifest to go on.

    Deliberately conservative: every file becomes its own path, so Restore puts
    them back one at a time, recreating whatever folders it needs.  It cannot
    tell a "the whole album folder was deleted" entry from a "these four tracks
    were deleted" one -- both look like files under ``Artist/Album/`` -- and
    guessing "album" would refuse the restore with a 409 whenever the album
    folder is still in the library, when restoring the files into it is exactly
    what the user wants.  Restoring file by file is right either way; only the
    ``kind`` label is a guess, and it is the harmless half.

    ``None`` for an entry holding nothing at all, which is not worth listing.
    """
    files = _entry_files(entry_dir)
    if not files:
        return None
    kind = KIND_TRACK if len(files) == 1 else KIND_TRACKS
    path = files[0] if kind == KIND_TRACK else _common_parent(files)
    return TrashEntry(
        id=entry_id,
        path=path,
        kind=kind,
        paths=files,
        deleted_at=_deleted_at_from_id(entry_id),
        track_count=_count_audio(entry_dir),
    )


def _unusable_rel(one: object) -> bool:
    """Whether *one* is anything but a plain relative path inside the entry.

    The manifest is a file on a disk the user can edit, so a ``..``, an
    absolute path or a NUL is checked for here rather than assumed away: any
    of them would otherwise become a restore target outside the library.
    """
    if not isinstance(one, str) or not one:
        return True
    if one.startswith("/") or "\\" in one or "\x00" in one:
        return True
    return any(segment in ("", ".", "..") for segment in one.split("/"))


def _read_manifest(entry_dir: Path, entry_id: str) -> TrashEntry | None:
    """The entry the manifest describes, or ``None`` when it cannot be trusted.

    Every field is checked rather than taken on faith: the manifest is a file
    on a disk the user can edit, and a ``paths`` entry with a ``..`` in it
    would otherwise become a restore target outside the library.  Anything
    wrong sends the caller to :func:`_recover_entry`, which reads the files
    themselves.
    """
    try:
        raw = json.loads((entry_dir / MANIFEST_NAME).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("No usable manifest in %s: %s", entry_dir.name, exc)
        return None
    if not isinstance(raw, dict):
        return None

    kind = raw.get("kind")
    path = raw.get("path")
    paths = raw.get("paths")
    if kind not in _KINDS or not isinstance(path, str) or not isinstance(paths, list):
        return None
    if not paths or any(_unusable_rel(one) for one in paths):
        return None
    # ``path`` is the one line the Trash tab shows for the entry, and for a
    # multi-track delete out of the library root that line is the root itself:
    # the empty string is a real value here, where in ``paths`` it never is.
    if path and _unusable_rel(path):
        return None
    if not all((entry_dir / one).exists() or os.path.lexists(entry_dir / one) for one in paths):
        # A manifest that no longer describes what is on the disk (a
        # half-finished delete, a user who moved files out by hand) is worse
        # than none: Restore would report paths it cannot move.
        return None

    deleted_at = raw.get("deleted_at")
    return TrashEntry(
        id=entry_id,
        path=path,
        kind=kind,
        paths=list(paths),
        deleted_at=deleted_at if isinstance(deleted_at, str) else _deleted_at_from_id(entry_id),
        track_count=_count_audio(entry_dir),
    )


def _read_entry(entry_dir: Path, entry_id: str) -> TrashEntry | None:
    """One entry, from its manifest when that is usable and from the disk when not."""
    entry = _read_manifest(entry_dir, entry_id)
    if entry is not None:
        return entry
    return _recover_entry(entry_dir, entry_id)


def _write_manifest(entry_dir: Path, entry: TrashEntry) -> None:
    """Record what this entry was, so Restore does not have to guess.

    A failure is logged, not raised: the files are already in the trash and the
    entry is still listable and restorable without it (see
    :func:`_recover_entry`), so failing the delete now would tell the user
    nothing happened when it did.
    """
    payload = {
        "id": entry.id,
        "path": entry.path,
        "kind": entry.kind,
        "paths": entry.paths,
        "deleted_at": entry.deleted_at,
        "track_count": entry.track_count,
    }
    try:
        (entry_dir / MANIFEST_NAME).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
        )
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("Could not write the trash manifest for %s: %s", entry.id, exc)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def _lexical(rel: str, root: Path) -> Path:
    """The path *rel* names, validated but **not** resolved.

    :func:`validate_library_path` answers with symlinks followed, which is what
    makes it a security check -- a path that lands outside the library is
    refused here.  What comes back from it is the wrong thing to rename,
    though: for ``Artist/link.flac`` it is the file the link points at, so
    trashing it would move the real track and leave a dangling link behind in
    the library.  The delete operates on the name the user asked about; the
    resolved path only decides whether they were allowed to.
    """
    validate_library_path(rel, root)
    return root / rel


def _surviving_folder(path: Path, root: Path) -> str:
    """The nearest folder at or above *path* that still exists, relative.

    What ``library_changed`` and the rescan hook are given after a delete: the
    folder a track came out of, or -- when the delete emptied that folder and
    the cleanup took it away -- whatever is left above it, down to ``""`` for
    the library root.  Naming a folder that no longer exists would give the
    hook nothing to touch.
    """
    current = path
    while current != root and current.is_relative_to(root):
        if current.is_dir():
            return rel_path(current, root)
        current = current.parent
    return ""


def delete_library_entry(
    root: Path,
    path: str | None = None,
    paths: list[str] | None = None,
    in_flight: Iterable[str] = (),
    unresolved: int = 0,
    unresolved_jobs: Iterable[str] = (),
    tagging: Iterable[str] = (),
) -> DeleteOutcome:
    """Move one track, one selection of tracks, an album or an artist to the trash.

    Exactly one of *path* and *paths* is given.  *paths* is always a set of
    tracks sharing one parent folder; *path* is a track when it names a file,
    an album when it names a folder at depth 2, and an artist at depth 1.

    Blocking, and not re-entrant: hold
    :data:`~app.library_ops.LIBRARY_WRITE_LOCK` around it.

    Raises:
        LibraryPathError: a malformed path, a path inside ``.trash``/``.tmp``,
            or a mixed selection (400).
        LibraryNotFound: a path that is not on disk (404).
        LibraryConflict: a download in flight into one of the folders being
            deleted -- *unresolved* being non-zero counts as in flight into an
            unknown folder -- or a tagging job rewriting one of them, named in
            *tagging* (409).
    """
    base = root.resolve()
    in_flight = list(in_flight)
    tagging = list(tagging)

    if path is None and paths is None:
        raise LibraryPathError("give either 'path' or 'paths'")
    if path is not None and paths is not None:
        raise LibraryPathError("give 'path' or 'paths', not both")

    if paths is not None:
        if not paths:
            raise LibraryPathError("'paths' must name at least one track")
        sources = [_lexical(one, base) for one in paths]
        for source in sources:
            if is_reserved(source, base):
                raise LibraryPathError("that folder is not part of the library")
            if not source.is_file():
                raise LibraryNotFound("no such track")
        return _delete_tracks(
            sources, base, in_flight, unresolved, unresolved_jobs, tagging
        )

    assert path is not None
    source = _lexical(path, base)
    if is_reserved(source, base):
        raise LibraryPathError("that folder is not part of the library")
    if source.is_file():
        return _delete_tracks(
            [source], base, in_flight, unresolved, unresolved_jobs, tagging
        )
    if not source.is_dir():
        raise LibraryNotFound("no such album or artist")

    depth = len(source.relative_to(base).parts)
    if depth > 2:
        raise LibraryPathError(
            "only a track, an album folder, or an artist folder can be deleted"
        )
    return _delete_folder(
        source,
        KIND_ARTIST if depth == 1 else KIND_ALBUM,
        base,
        in_flight,
        unresolved,
        unresolved_jobs,
        tagging,
    )


def _stage(
    root: Path, items: list[tuple[Path, str]]
) -> tuple[Path, str]:
    """Rename every ``(source, relative path)`` into a fresh trash entry.

    Returns the entry folder and its id.  All-or-nothing, because
    :func:`~app.library_ops.rename_files` puts back what it has already moved
    when one of them fails -- half an album in the trash and half in the
    library is the one outcome a delete must never leave.

    When even the rollback fails, the entry is what is holding the user's
    audio, so it stays and the caller is told which tracks are in it.
    """
    entry_dir, entry_id = _new_entry_dir(ensure_trash_root(root))
    try:
        rename_files([(source, entry_dir / rel) for source, rel in items])
    except PartialRenameError as exc:
        # Those tracks are in the entry and nowhere else.  It keeps them, and
        # the user gets them back from the Trash tab.
        _discard_entry(entry_dir, only_when_empty=True)
        raise PartialRenameError(
            exc.stranded,
            f"{len(exc.stranded)} track(s) could not be put back and are in "
            "the Trash tab; restore them from there",
        ) from exc
    except OSError:
        # The rollback put everything back, so the entry folder holds nothing;
        # leaving it would show as a phantom entry in the Trash tab.  Removed
        # only once that is true of it -- a delete never unlinks.
        _discard_entry(entry_dir, only_when_empty=True)
        raise
    return entry_dir, entry_id


def _delete_tracks(
    sources: list[Path],
    root: Path,
    in_flight: Iterable[str],
    unresolved: int,
    unresolved_jobs: Iterable[str],
    tagging: Iterable[str] = (),
) -> DeleteOutcome:
    """Trash audio files that share one parent folder, then tidy the folder."""
    parents = {source.parent for source in sources}
    if len(parents) != 1:
        raise LibraryPathError(
            "all tracks in one delete must come from the same folder"
        )
    (parent,) = parents

    # The parent folder alone, exactly as a track move guards it: this delete
    # may empty it and the cleanup may then take it away, and a download aiming
    # into it would find it gone.
    parent_rel = "" if parent == root else rel_path(parent, root)
    check_in_flight([parent_rel], in_flight)
    # The files themselves, not their folder.  A tagging job on one track of an
    # album has no claim on its siblings, and guarding the parent would refuse
    # a delete of ``b.flac`` while ``a.flac`` was being tagged.  Containment
    # still does the work that matters: a job tagging the *album* covers every
    # file in it and refuses this, because the guarded path sits inside the
    # tagged one.
    check_not_being_tagged((rel_path(source, root) for source in sources), tagging)
    check_resolved(unresolved, unresolved_jobs)

    relatives = [rel_path(source, root) for source in sources]
    entry_dir, entry_id = _stage(root, list(zip(sources, relatives)))

    kind = KIND_TRACK if len(relatives) == 1 else KIND_TRACKS
    entry = TrashEntry(
        id=entry_id,
        path=relatives[0] if kind == KIND_TRACK else parent_rel,
        kind=kind,
        paths=relatives,
        deleted_at=_iso_now(),
        track_count=_count_audio(entry_dir),
    )
    _write_manifest(entry_dir, entry)

    outcome = DeleteOutcome(entry=entry)
    cleanup_upwards(parent, root, outcome.removed, in_flight)
    outcome.changed = [_surviving_folder(parent, root)]
    return outcome


def _delete_folder(
    source: Path,
    kind: str,
    root: Path,
    in_flight: Iterable[str],
    unresolved: int,
    unresolved_jobs: Iterable[str],
    tagging: Iterable[str] = (),
) -> DeleteOutcome:
    """Trash a whole album or artist folder as one rename."""
    source_rel = rel_path(source, root)
    check_in_flight([source_rel], in_flight)
    check_not_being_tagged([source_rel], tagging)
    check_resolved(unresolved, unresolved_jobs)

    entry_dir, entry_id = _stage(root, [(source, source_rel)])

    entry = TrashEntry(
        id=entry_id,
        path=source_rel,
        kind=kind,
        paths=[source_rel],
        deleted_at=_iso_now(),
        track_count=_count_audio(entry_dir),
    )
    _write_manifest(entry_dir, entry)

    outcome = DeleteOutcome(entry=entry)
    if kind == KIND_ALBUM:
        # The artist folder may have held nothing but this album; an artist
        # delete has nothing above it to clean up but the library root.
        cleanup_upwards(source.parent, root, outcome.removed, in_flight)
    outcome.changed = [_surviving_folder(source.parent, root)]
    return outcome


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_trash(root: Path) -> tuple[list[TrashEntry], int]:
    """Every entry in the trash, newest first, and the total track count.

    Built from the filesystem on every call -- there is no trash table, and the
    dot-folder is the record.  Files directly inside ``.trash`` (the
    ``.ndignore``, anything a user dropped there) are not entries and are
    ignored.
    """
    base = trash_root(root)
    entries: list[TrashEntry] = []
    try:
        found = sorted(os.scandir(base), key=lambda item: item.name)
    except OSError:
        return [], 0

    for item in found:
        if not item.is_dir(follow_symlinks=False):
            continue
        entry = _read_entry(Path(item.path), item.name)
        if entry is not None:
            entries.append(entry)

    # Newest first, and the id breaks a tie: it is a timestamp itself, so two
    # entries whose manifests claim the same second still come out in the order
    # they were deleted.
    entries.sort(key=lambda entry: (entry.deleted_at, entry.id), reverse=True)
    return entries, sum(entry.track_count for entry in entries)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def _validate_entry_id(entry_id: str) -> str:
    """Refuse an id that is anything but one folder name under ``.trash``."""
    if not isinstance(entry_id, str) or not entry_id:
        raise LibraryPathError("id must be a non-empty string")
    if "/" in entry_id or "\\" in entry_id or "\x00" in entry_id:
        raise LibraryPathError("id must name one folder in the trash")
    if entry_id in (".", ".."):
        raise LibraryPathError("id must name one folder in the trash")
    return entry_id


def _entry_base_dir(entry: TrashEntry) -> str:
    """The folder the entry's paths are relative *to* when it is re-targeted.

    For a track it is the folder the track sat in, for a selection the folder
    they shared, and for an album or artist the folder itself -- so a path's
    tail below that base is what has to be preserved when the whole entry is
    restored somewhere else.
    """
    if entry.kind == KIND_TRACK:
        return entry.path.rsplit("/", 1)[0] if "/" in entry.path else ""
    if entry.kind == KIND_TRACKS:
        return entry.path
    return entry.path


def _retargeted(
    entry: TrashEntry, artist: str, album: str | None, root: Path
) -> tuple[list[tuple[str, str]], str, str | None]:
    """Where each of the entry's paths goes when it is restored elsewhere.

    Returns ``[(entry-relative source, library-relative target)]`` plus the
    artist and album folder names that were settled on -- the album is ``None``
    for an artist entry, whose ``ALBUM`` tags are none of a rename's business.

    The naming rules are the move endpoint's, because this *is* the move dialog
    the UI opens after a 409: :func:`~app.library_ops.resolve_destination` is
    the very function the mover uses.
    """
    if entry.kind == KIND_ARTIST:
        # An artist entry becomes an artist folder: it has no album half at
        # all, and its own albums keep the names they came in with.
        artist_name = folder_name(root, artist, FALLBACK_ARTIST, "artist")
        artist_dir = root / artist_name
        if is_reserved(artist_dir, root):
            raise LibraryPathError("that destination is reserved")
        check_inside(artist_dir, root)
        return [(entry.paths[0], artist_name)], artist_name, None

    if entry.kind == KIND_ALBUM:
        # No album given means "same album, new artist", which is the album's
        # own folder name -- not a Single, which an album folder cannot be.
        artist_name, album_name, destination = resolve_destination(
            root, artist, album, album_fallback=entry.path.rsplit("/", 1)[-1]
        )
        return (
            [(entry.paths[0], rel_path(destination, root))],
            artist_name,
            album_name,
        )

    artist_name, album_name, destination = resolve_destination(root, artist, album)

    base = _entry_base_dir(entry)
    prefix = base + "/" if base else ""
    pairs: list[tuple[str, str]] = []
    for one in entry.paths:
        # The tail below the folder the tracks shared, so a `Disc 1/x.flac`
        # inside a recovered entry keeps its subfolder instead of collapsing
        # onto another disc's track of the same name.
        tail = one[len(prefix) :] if prefix and one.startswith(prefix) else one.rsplit("/", 1)[-1]
        pairs.append((one, rel_path(destination / tail, root)))
    return pairs, artist_name, album_name


def restore_trash_entry(
    root: Path,
    entry_id: str,
    artist: str | None = None,
    album: str | None = None,
    in_flight: Iterable[str] = (),
    unresolved: int = 0,
    unresolved_jobs: Iterable[str] = (),
    tagging: Iterable[str] = (),
) -> RestoreOutcome:
    """Put one trash entry back, either where it came from or somewhere new.

    Without *artist* every path goes back to the path it was deleted from.
    With *artist* (and an optional *album*) the whole entry is restored under
    that artist instead and its FLAC tags are rewritten to match, exactly as a
    move would -- which is how the UI recovers from a 409 without making the
    user restore first and move afterwards.

    All-or-nothing: every target is checked before anything is renamed, and an
    occupied one refuses the whole request with the full list.

    An album or artist entry restored onto a folder that already exists is
    *merged* into it, file by file, exactly as ``POST /library/move`` merges an
    album -- this flow exists to answer a 409, and refusing it with a second
    one would leave the user nowhere to go.  Only a collision on an individual
    file refuses it then.  A plain restore, with no artist given, keeps the
    strict rule: the original path has to be free.

    Blocking, and not re-entrant: hold
    :data:`~app.library_ops.LIBRARY_WRITE_LOCK` around it.

    Raises:
        LibraryPathError: a bad id or an unusable artist/album name (400).
        LibraryNotFound: no such entry (404).
        LibraryConflict: an occupied target, a download in flight into one
            of the destination folders, or a tagging job rewriting one of them
            (409).
    """
    base = root.resolve()
    in_flight = list(in_flight)
    tagging = list(tagging)

    entry_dir = trash_root(base) / _validate_entry_id(entry_id)
    # A symlink is not an entry: the listing skips one (it scans without
    # following), and restoring through it would rename files from wherever it
    # points -- anywhere on the disk -- into the library.
    if entry_dir.is_symlink() or not entry_dir.is_dir():
        raise LibraryNotFound("no such trash entry")
    entry = _read_entry(entry_dir, entry_id)
    if entry is None:
        raise LibraryNotFound("that trash entry is empty")

    if artist is not None and artist.strip():
        pairs, artist_name, album_name = _retargeted(entry, artist, album, base)
    else:
        pairs = [(one, one) for one in entry.paths]
        artist_name = album_name = None

    # Every folder a restore is about to write into, so a download already
    # aiming at one of them cannot have an album appear underneath it
    # mid-write.  An album or artist entry guards the folder it becomes; a
    # track guards the folder it lands in, exactly as a track move does.
    whole_folder = entry.kind in (KIND_ARTIST, KIND_ALBUM)
    guarded: list[str] = []
    for _source, target in pairs:
        folder = target
        if not whole_folder:
            folder = target.rsplit("/", 1)[0] if "/" in target else ""
        if folder not in guarded:
            guarded.append(folder)
    check_in_flight(guarded, in_flight)
    check_not_being_tagged(guarded, tagging)

    conflicts: list[str] = []
    renames: list[tuple[Path, Path]] = []
    for source, target in pairs:
        source_path = entry_dir / source
        target_path = base / target
        if is_reserved(target_path, base):
            raise LibraryPathError("that destination is reserved")
        # The manifest is a file on the user's disk and an artist folder can be
        # a symlink, so where the target really lands is checked here rather
        # than assumed from the string.
        check_inside(target_path, base)
        if whole_folder and artist_name is not None and target_path.is_dir():
            # The folder the user picked already exists: merge into it rather
            # than answer the 409 this dialog was opened to resolve.  An artist
            # entry merges the same way -- ``merge_pairs`` walks the tree, so
            # its album subfolders keep their shape as they land.
            merged, clashes = merge_pairs(source_path, target_path, base)
            renames.extend(merged)
            conflicts.extend(clashes)
            continue
        # A file (or a dangling symlink) sitting where a folder has to go is
        # a collision like any other, reported here rather than left to
        # surface from inside ``mkdir(parents=True)`` as a 500.
        conflicts.extend(check_ancestors_are_dirs(target_path, base, base))
        if os.path.lexists(target_path) and not same_file(target_path, source_path):
            conflicts.append(target)
            continue
        renames.append((source_path, target_path))

    if conflicts:
        conflicts = list(dict.fromkeys(conflicts))
        raise LibraryConflict(
            f"{len(conflicts)} path(s) are already in the library; nothing was restored",
            conflicts,
        )

    # Two of the entry's files landing on one path would have the second
    # rename silently destroy the first, and the free-target check above cannot
    # see it: neither target exists yet.  A hand-made entry holding the same
    # filename under two folders, restored under one artist, is how it happens.
    targets = [target for _source, target in renames]
    if len(set(targets)) != len(targets):
        raise LibraryPathError("this trash entry maps two files onto one path")

    check_resolved(unresolved, unresolved_jobs)
    rename_files(renames)

    outcome = RestoreOutcome()
    for source, target in renames:
        outcome.restored.append(
            {
                "source": source.relative_to(trash_root(base)).as_posix(),
                "target": rel_path(target, base),
            }
        )
    if artist_name is not None:
        _retag_restored(entry, renames, artist_name, album_name)

    # The folder each file landed *in*: a whole-folder restore's target is
    # that folder itself, while a merge's targets are the individual files.
    # Read after the renames ran, so ``is_dir`` sees what is now on disk.
    folders = [
        target if target.is_dir() else target.parent for _source, target in renames
    ]
    outcome.changed = sorted(
        {rel_path(folder, base) if folder != base else "" for folder in folders}
    )
    _discard_entry(entry_dir)
    return outcome


def _retag_restored(
    entry: TrashEntry,
    renames: list[tuple[Path, Path]],
    artist_name: str,
    album_name: str | None,
) -> None:
    """Rewrite the attribution tags of everything a re-targeted restore moved.

    The same three fields the mover writes, decided the same way: ``ARTIST``
    only follows when it agreed with the artist folder the file was deleted
    from, so a compilation's per-track credits survive a restore the way they
    survive a move.  An artist entry gets the artist pair alone -- its albums
    keep their own names.
    """
    # The artist folder the files are leaving -- the *folder* they sat in, not
    # the entry's own label, so a track deleted loose from the library root
    # (whose label is the filename) comes out as None and has its ARTIST
    # written unconditionally, exactly as moving that track would.
    previous_artist = _entry_base_dir(entry).split("/")[0] or None
    if album_name is None:
        updates: dict[str, str | None] = {
            "ALBUMARTIST": artist_name,
            "ARTIST": artist_name,
        }
    else:
        updates = placement_tags(artist_name, album_name)

    for _source, target in renames:
        files = walk_files(target) if target.is_dir() else [target]
        for one in files:
            rewrite_tags(one, updates, previous_artist=previous_artist)


def _discard_entry(entry_dir: Path, *, only_when_empty: bool = False) -> None:
    """Remove an emptied entry folder, manifest and leftovers included.

    Refuses while any audio is still in there.  A restore moves everything the
    entry listed, so what is left is the manifest and whatever a partly-hand-
    edited entry held; an audio file among it means the entry was not what it
    said it was, and the user's music is worth more than a tidy trash folder.

    *only_when_empty* is the stricter rule a failed delete needs: nothing has
    been written into the entry that the user asked for yet, so *any* file in
    it is a file that exists nowhere else, audio or not.
    """
    if only_when_empty:
        leftover = next(walk_files(entry_dir), None)
        if leftover is not None:
            logger.warning(
                "Leaving the trash entry %s in place: %s is in it",
                entry_dir.name,
                leftover.name,
            )
            return
    elif has_audio(entry_dir):
        logger.warning(
            "Leaving the trash entry %s in place: audio is still in it", entry_dir.name
        )
        return
    shutil.rmtree(entry_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Empty
# ---------------------------------------------------------------------------


def empty_trash(root: Path) -> tuple[int, int]:
    """Delete everything in the trash permanently.

    Returns ``(entries removed, tracks removed)``.  This is the one place in
    the app that unlinks a user's audio, and it is the one the confirm dialog
    names a count for.  No in-flight guard: nothing in the trash is a folder a
    download can be aiming at -- the reserved-name check keeps every writer out
    of ``.trash`` in the first place.

    ``.ndignore`` survives, so the folder keeps telling Navidrome to skip it
    without waiting for the next delete to write one.
    """
    base = trash_root(root)
    if not base.is_dir():
        return 0, 0

    entries = 0
    tracks = 0
    for item in sorted(os.scandir(base), key=lambda one: one.name):
        if item.name == NDIGNORE_NAME:
            continue
        path = Path(item.path)
        if item.is_dir(follow_symlinks=False):
            # Counted exactly as the listing counts them, so the number in the
            # response is the number the confirm dialog showed: a folder the
            # listing cannot make an entry of (an empty one, left by a failed
            # delete) is still removed, but it was never an entry.
            counted = _read_entry(path, item.name) is not None
            tracks += _count_audio(path)
            shutil.rmtree(path, ignore_errors=True)
            if counted and not path.exists():
                entries += 1
        else:
            try:
                path.unlink()
            except OSError as exc:  # pragma: no cover - defensive
                logger.warning("Could not remove %s from the trash: %s", item.name, exc)

    # Recreated rather than assumed: an entry called ``.ndignore`` never
    # existed, but a user who removed the marker by hand gets it back here
    # instead of at their next delete.
    ensure_trash_root(root)
    if entries:
        logger.info("Emptied the trash: %d entr(y/ies), %d track(s)", entries, tracks)
    return entries, tracks
