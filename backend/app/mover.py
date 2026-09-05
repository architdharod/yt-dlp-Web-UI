"""Moving and renaming things inside the Library.

Three shapes, one endpoint (``POST /library/move``):

* **tracks** -- one or more audio files that share a parent folder, moved to
  any artist and an optional album.  A blank album makes them loose Singles at
  ``Artist/<filename>`` and clears their ``ALBUM`` tag.
* **album** -- a folder at depth 2 moved to another artist, optionally under a
  new name, merging into a folder that is already there.
* **artist rename** -- a folder at depth 1 renamed.

Every one of them is all-or-nothing: the whole set of target paths is checked
before a single file is renamed, and a collision refuses the request with the
list of conflicting paths rather than moving what happens to fit.  Nothing is
ever overwritten and nothing is ever copied -- these are ``os.rename`` calls
within one filesystem, which is what makes a whole album move instant.

FLAC tags follow the folders: ``ALBUMARTIST`` and ``ALBUM`` are rewritten to
the names the file now lives under (``ALBUM`` removed for a Single).
``ARTIST`` is the one that does not follow unconditionally -- it only changes
when it agreed with the artist folder the file is leaving, so a compilation's
per-track credits and a guest artist survive a move exactly as they survive a
rename.  Every other field, ``SOURCEID``/``SOURCEURL`` and the embedded
pictures included, is left exactly as it was.  Other formats are moved without
a tag rewrite.

The path helpers, the tag rewrite, the cleanup and both guards are shared with
delete and restore and live in :mod:`app.library_ops`; this module is the three
moves and nothing else.

Blocking: the route calls :func:`move_library_entry` through
``asyncio.to_thread``, holding :data:`~app.library_ops.LIBRARY_WRITE_LOCK` so
that two writers can never interleave their checks and their renames.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from app.file_organizer import FALLBACK_ARTIST
from app.library import (
    LibraryNotFound,
    LibraryPathError,
    validate_library_path,
)
from app.library_ops import (
    LIBRARY_WRITE_LOCK,
    LibraryConflict,
    check_in_flight,
    check_not_being_tagged,
    check_inside,
    check_resolved,
    check_type_conflicts,
    cleanup_upwards,
    folder_name,
    folders_of,
    is_reserved,
    merge_pairs,
    placement_tags,
    rel_path,
    rename_files,
    rename_folder,
    resolve_destination,
    rewrite_tags,
    same_file,
    walk_files,
)

logger = logging.getLogger(__name__)

# Re-exported: ``main`` has imported both from here since the move endpoint was
# written, and both are as much a part of moving as they are of deleting.
__all__ = [
    "LIBRARY_WRITE_LOCK",
    "LibraryConflict",
    "MoveOutcome",
    "move_library_entry",
]


@dataclass
class MoveOutcome:
    """What a move did, in the vocabulary the API answers in.

    ``moved`` is one entry per file that changed path; ``removed`` are the
    folders emptied by the move and cleaned up; ``changed`` are the paths the
    rescan hook and the ``library_changed`` event are told about (source and
    destination folders, not every file).

    ``destination`` is the folder the album or artist now lives at, which is
    what the dialog navigates to once the move returns.  It is ``None`` for a
    track move -- the tracks went to a folder, not the folder itself -- and for
    a move that turned out to be a no-op.
    """

    moved: list[dict[str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    destination: str | None = None


# ---------------------------------------------------------------------------
# The three moves
# ---------------------------------------------------------------------------


def _move_tracks(
    sources: list[Path],
    artist: str,
    album: str | None,
    root: Path,
    in_flight: Iterable[str],
    unresolved: int = 0,
    unresolved_jobs: Iterable[str] = (),
    tagging: Iterable[str] = (),
) -> MoveOutcome:
    """Move audio files that share a parent to ``artist``/``album``."""
    parents = {source.parent for source in sources}
    if len(parents) != 1:
        raise LibraryPathError(
            "all tracks in one move must come from the same folder"
        )
    (parent,) = parents

    artist_name, album_name, destination = resolve_destination(root, artist, album)

    # Both ends are guarded.  The destination because a download filing into a
    # folder this move is merging into would land after the collision check;
    # the source folder because it may be emptied and cleaned up here, and a
    # job aiming into it would find it gone.  Only the source folder itself --
    # the artist folder above it is not what this move touches.
    parent_rel = "" if parent == root else rel_path(parent, root)
    check_in_flight([parent_rel, rel_path(destination, root)], in_flight)
    # The files being moved, not the folder they came out of: a tagging job on
    # one track of an album has no claim on its siblings.  The destination
    # stays guarded, because a pass running there would have this move landing
    # files in a folder it is halfway through rewriting.  Containment still
    # refuses a move out of an album that is *itself* being tagged: every
    # source sits inside the tagged path.
    check_not_being_tagged(
        [*(rel_path(source, root) for source in sources), rel_path(destination, root)],
        tagging,
    )

    pairs: list[tuple[Path, Path]] = []
    conflicts: list[str] = []
    for source in sources:
        target = destination / source.name
        if same_file(target, source):
            # Already where it is going, spelling differences included: a move
            # to "bonobo" on a filesystem that already has "Bonobo".
            continue
        if os.path.lexists(target):
            # ``lexists``, not ``exists``: a dangling symlink occupies the name
            # just as firmly as a file does.
            conflicts.append(rel_path(target, root))
        else:
            pairs.append((source, target))

    if conflicts:
        raise LibraryConflict(
            f"{len(conflicts)} file(s) already exist at the destination; nothing was moved",
            conflicts,
        )

    outcome = MoveOutcome()
    if not pairs:
        return outcome

    # A file where one of the destination's folders has to go: reported as the
    # collision it is rather than left to fail inside ``mkdir(parents=True)``.
    check_type_conflicts(pairs[0][1], root, root)
    check_resolved(unresolved, unresolved_jobs)

    rename_files(pairs)
    tags = placement_tags(artist_name, album_name)
    # The artist folder the files are leaving, or None for tracks loose in the
    # library root: those sit under no artist folder at all, so there is
    # nothing their ARTIST could have agreed with and the new artist is
    # written unconditionally -- which is what a user filing a stray track
    # under an artist is asking for.
    previous_artist = parent_rel.split("/")[0] if parent_rel else None
    for source, target in pairs:
        rewrite_tags(target, tags, previous_artist=previous_artist)
        outcome.moved.append({"from": rel_path(source, root), "to": rel_path(target, root)})

    outcome.changed = folders_of(
        [source for source, _ in pairs] + [target for _, target in pairs], root
    )
    cleanup_upwards(parent, root, outcome.removed, in_flight)
    return outcome


def _move_album(
    source: Path,
    artist: str,
    album: str | None,
    root: Path,
    in_flight: Iterable[str],
    unresolved: int = 0,
    unresolved_jobs: Iterable[str] = (),
    tagging: Iterable[str] = (),
) -> MoveOutcome:
    """Move an album folder to another artist, merging where one exists."""
    # An album folder cannot become a Single, so a blank album means "same
    # album, new artist" -- the folder's own name.
    artist_name, album_name, destination = resolve_destination(
        root, artist, album, album_fallback=source.name
    )

    source_rel = rel_path(source, root)
    check_in_flight([source_rel, rel_path(destination, root)], in_flight)
    check_not_being_tagged([source_rel, rel_path(destination, root)], tagging)

    outcome = MoveOutcome()
    if destination == source:
        return outcome

    check_resolved(unresolved, unresolved_jobs)
    # The artist folder has to be a folder before either branch below can run.
    check_type_conflicts(destination, root, root)

    merging = destination.is_dir() and not same_file(destination, source)
    previous_artist = source.parent.name
    if not merging:
        # Nothing in the way: one rename carries the folder, its subfolders and
        # its `cover.jpg` across, and is atomic in a way a file-by-file merge
        # can never be.
        rename_folder(source, destination, root)
        # Enumerated from the *destination*, after the rename, not from the
        # source before it.  On the virtiofs and CIFS bind mounts this runs on
        # in a container, any lookup of a differently-cased spelling of a path
        # (a resolve, a stat, an is_dir -- and a case-only rename does several)
        # displaces the cached dentry, after which ``os.walk`` of the old path
        # yields nothing at all.  A case-only rename would then rewrite no tags
        # and report ``moved: []`` while the folder really had moved.  The
        # destination is the name the filesystem has just been told about, so
        # walking it is always right.
        tags = placement_tags(artist_name, album_name)
        for target in walk_files(destination):
            previous = source / target.relative_to(destination)
            rewrite_tags(target, tags, previous_artist=previous_artist)
            outcome.moved.append(
                {"from": rel_path(previous, root), "to": rel_path(target, root)}
            )
        outcome.changed = [source_rel, rel_path(destination, root)]
        outcome.destination = rel_path(destination, root)
        cleanup_upwards(source.parent, root, outcome.removed, in_flight)
        return outcome

    pairs, conflicts = merge_pairs(source, destination, root)
    if conflicts:
        raise LibraryConflict(
            f"{len(conflicts)} file(s) already exist in {rel_path(destination, root)}; "
            "nothing was moved",
            conflicts,
        )

    rename_files(pairs)
    tags = placement_tags(artist_name, album_name)
    for previous, target in pairs:
        rewrite_tags(target, tags, previous_artist=previous_artist)
        outcome.moved.append({"from": rel_path(previous, root), "to": rel_path(target, root)})

    outcome.changed = [source_rel, rel_path(destination, root)]
    outcome.destination = rel_path(destination, root)
    cleanup_upwards(source, root, outcome.removed, in_flight)
    return outcome


def _rename_artist(
    source: Path,
    artist: str,
    root: Path,
    in_flight: Iterable[str],
    unresolved: int = 0,
    unresolved_jobs: Iterable[str] = (),
    tagging: Iterable[str] = (),
) -> MoveOutcome:
    """Rename an artist folder and follow it in every FLAC below."""
    # ``adopt_existing=False``: renaming "bonobo" to "Bonobo" is a real request
    # and adopting the existing spelling would make it a silent no-op.
    destination = root / folder_name(
        root, artist, FALLBACK_ARTIST, "artist", adopt_existing=False
    )
    if is_reserved(destination, root):
        raise LibraryPathError("that destination is reserved")
    check_inside(destination, root)

    source_rel = rel_path(source, root)
    outcome = MoveOutcome()
    if destination == source:
        return outcome

    check_in_flight([source_rel, rel_path(destination, root)], in_flight)
    check_not_being_tagged([source_rel, rel_path(destination, root)], tagging)
    check_resolved(unresolved, unresolved_jobs)
    check_type_conflicts(destination, root, root)

    previous_name = source.name
    rename_folder(source, destination, root)

    new_name = destination.name
    # Walked from the destination after the rename, not from the source before
    # it: on the virtiofs and CIFS bind mounts this runs on in a container, a
    # lookup of a differently-cased spelling of a path displaces the cached
    # dentry, and a case-only rename does several -- after which ``os.walk``
    # of the old path yields nothing and the rename rewrites no tags at all
    # while reporting ``moved: []``.
    for target in walk_files(destination):
        previous = source / target.relative_to(destination)
        # ALBUMARTIST follows the folder unconditionally -- it is what
        # Navidrome and Lidarr group by, and it is the folder's own name.
        # ARTIST only follows when it agreed with the old folder name, which
        # rewrite_tags decides from the file it has already opened.
        rewrite_tags(
            target,
            {"ALBUMARTIST": new_name, "ARTIST": new_name},
            previous_artist=previous_name,
        )
        outcome.moved.append({"from": rel_path(previous, root), "to": rel_path(target, root)})

    outcome.changed = [source_rel, rel_path(destination, root)]
    outcome.destination = rel_path(destination, root)
    return outcome


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def move_library_entry(
    root: Path,
    artist: str,
    album: str | None = None,
    path: str | None = None,
    paths: list[str] | None = None,
    in_flight: Iterable[str] = (),
    unresolved: int = 0,
    unresolved_jobs: Iterable[str] = (),
    tagging: Iterable[str] = (),
) -> MoveOutcome:
    """Perform one move and return what it did.

    Exactly one of *path* and *paths* is given.  *paths* is always a set of
    tracks; *path* is a track when it names a file, an album when it names a
    folder at depth 2, and an artist rename when it names a folder at depth 1.

    Blocking, and not re-entrant: hold :data:`LIBRARY_WRITE_LOCK` around it.

    Raises:
        LibraryPathError: a malformed path or name, or a mixed selection (400).
        LibraryNotFound: a source that is not on disk (404).
        LibraryConflict: a target that is occupied, a download in flight
            into one of the folders involved -- *unresolved* being non-zero
            counts as in flight into an unknown folder -- or a tagging job
            rewriting one of them, named in *tagging* (409).
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
        sources = [validate_library_path(one, base) for one in paths]
        for source in sources:
            # Checked for every source, file or folder, before anything is
            # dispatched: ``.tmp`` holds a half-written download and ``.trash``
            # holds what the user has already deleted, and neither is part of
            # the library the move endpoint speaks about.
            if is_reserved(source, base):
                raise LibraryPathError("that folder is not part of the library")
            if not source.is_file():
                raise LibraryNotFound("no such track")
        return _move_tracks(
            sources, artist, album, base, in_flight, unresolved, unresolved_jobs,
            tagging,
        )

    assert path is not None
    source = validate_library_path(path, base)
    # Before the file/folder split, not inside the folder branch: a *file*
    # under ``.tmp`` or ``.trash`` was movable, which handed the user a
    # half-written download or lifted a track back out of the trash.
    if is_reserved(source, base):
        raise LibraryPathError("that folder is not part of the library")
    if source.is_file():
        return _move_tracks(
            [source], artist, album, base, in_flight, unresolved, unresolved_jobs,
            tagging,
        )
    if not source.is_dir():
        raise LibraryNotFound("no such album or artist")

    depth = len(source.relative_to(base).parts)
    if depth == 1:
        return _rename_artist(
            source, artist, base, in_flight, unresolved, unresolved_jobs, tagging
        )
    if depth == 2:
        return _move_album(
            source, artist, album, base, in_flight, unresolved, unresolved_jobs,
            tagging,
        )
    raise LibraryPathError(
        "only a track, an album folder, or an artist folder can be moved"
    )
