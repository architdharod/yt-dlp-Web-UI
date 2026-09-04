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

Blocking: the route calls :func:`move_library_entry` through
``asyncio.to_thread``, holding :data:`LIBRARY_WRITE_LOCK` so that two writers
can never interleave their checks and their renames.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from mutagen.flac import FLAC

from app.file_organizer import (
    FALLBACK_ARTIST,
    NO_ALBUM,
    TEMP_DIRNAME,
    TRASH_DIRNAME,
    sanitize_component,
)
from app.library import (
    AUDIO_EXTENSIONS,
    LibraryNotFound,
    LibraryPathError,
    validate_library_path,
)

logger = logging.getLogger(__name__)

# One lock over every write to the library tree.  Move (this module), and from
# Phase 7 delete, restore and empty-trash, all check a set of target paths and
# then act on them; two of those running at once could each find a free path
# and then race for it.  Held by the route around the whole ``to_thread`` call,
# so the check and the renames are one critical section.
#
# Module-level and created without a running loop, which asyncio.Lock has
# allowed since 3.10 -- it binds to whichever loop first awaits it.
LIBRARY_WRITE_LOCK = asyncio.Lock()

# Folders no cleanup may ever remove and no move may ever target.
_RESERVED_TOP_LEVEL = frozenset({TEMP_DIRNAME, TRASH_DIRNAME})


class LibraryConflict(Exception):
    """A move refused because something is already at the target path.

    Carries every conflicting path rather than the first one, so the dialog can
    show the user the whole list and they can decide once instead of retrying
    into one collision after another.  The in-flight guard raises this too: a
    download about to write into the folder being moved is the same kind of
    "not now, and here is what is in the way".
    """

    def __init__(self, message: str, conflicts: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.conflicts = list(conflicts)


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
# Small path helpers
# ---------------------------------------------------------------------------


def _rel(path: Path, root: Path) -> str:
    """*path* as the POSIX string the API uses as an identity."""
    return path.relative_to(root).as_posix()


def _is_audio(path: Path) -> bool:
    return path.suffix.casefold() in AUDIO_EXTENSIONS


def _existing_dir_name(parent: Path, name: str) -> str | None:
    """The name a folder already in *parent* actually carries for *name*.

    An exact match wins; failing that, the first name that matches when
    case-folded.  This is what keeps the app from ever writing a second
    spelling of a folder the filesystem cannot tell apart: on macOS and
    Windows, creating ``bonobo`` next to ``Bonobo`` silently files tracks into
    the existing folder while the tags and the response claim the new
    spelling.  Scanning is sorted so the answer does not depend on directory
    order.

    ``None`` when nothing matches, and also when *parent* cannot be read --
    the caller then falls back to sanitising, which is the safe direction.
    """
    try:
        with os.scandir(parent) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
        folded = name.casefold()
        fallback: str | None = None
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name == name:
                return entry.name
            if fallback is None and entry.name.casefold() == folded:
                fallback = entry.name
        return fallback
    except OSError:
        return None


def _folder_name(
    parent: Path,
    requested: str,
    fallback: str,
    what: str,
    *,
    adopt_existing: bool = True,
) -> str:
    """The folder name to use under *parent* for the requested name.

    The domain model's two halves: a name that matches a folder already on disk
    is taken exactly as it is -- existing names are accepted however odd,
    because they are what the library is -- and a new name goes through
    ``sanitize_component``, which is what keeps it safe on every filesystem we
    might be on.  "AC/DC" is a real band, and comes out as one folder with a
    look-alike separator rather than as two.

    "Matches" is case-insensitive, because half the filesystems this runs on
    are: asked to move a track to ``bonobo`` while ``Bonobo`` exists, the
    answer is ``Bonobo``, the folder the file would really land in.

    *adopt_existing* is what a rename has to turn off.  Renaming ``A`` to ``a``
    is a real request on a case-insensitive filesystem, and adopting the
    existing spelling would turn it into a silent no-op.

    ``.``, ``..``, ``.tmp``, ``.trash`` and anything else starting with a dot
    are refused rather than sanitised: they mean something to the filesystem or
    to this app, or they are hidden from every reader of this library, and a
    user who typed one deserves to be told so instead of finding a folder with
    a made-up name -- or none they can see at all.
    """
    cleaned = requested.strip()
    if not cleaned:
        raise LibraryPathError(f"{what} must not be empty")
    if "\x00" in cleaned:
        raise LibraryPathError(f"{what} contains a NUL byte")
    if cleaned in (".", ".."):
        raise LibraryPathError(f"{what} must not be '.' or '..'")
    if cleaned.casefold() in (TEMP_DIRNAME, TRASH_DIRNAME):
        raise LibraryPathError(f"{what} is a reserved name")
    # A dot-prefixed folder is hidden from everything that reads this library:
    # the scanner skips it (``library._is_hidden``), and so do Navidrome and
    # Lidarr.  Honouring the name would answer 200 while the tracks vanished
    # from every view the user has of them, so it is a 400 instead.
    if cleaned.startswith("."):
        raise LibraryPathError(f"{what} must not start with a dot")
    # A name carrying a separator can never be the name of a folder in
    # *parent*, and testing it would look outside *parent* -- so it skips
    # straight to sanitisation.
    if "/" not in cleaned and "\\" not in cleaned:
        existing = _existing_dir_name(parent, cleaned)
        if existing is not None and (adopt_existing or existing == cleaned):
            return existing
    name = sanitize_component(cleaned, fallback)
    # A name made only of characters the sanitiser deletes -- a lone control
    # character, ``.\x7f`` -- comes back empty, which every caller reads as
    # "no album": the move would quietly turn an album into a Single, or leave
    # the folder under its old name, and answer 200.  It is a name we cannot
    # honour, so it is a 400.  A genuinely blank album never gets here; the
    # callers short-circuit to NO_ALBUM before calling this.
    if not name:
        raise LibraryPathError(f"{what} is not a usable folder name")
    return name


def _same_file(one: Path, other: Path) -> bool:
    """Whether the two paths name the same file, spelling differences included.

    Plain equality first, because most calls are two paths this module built
    itself; ``samefile`` after it, because a case-insensitive filesystem hands
    back two unequal strings for one inode.  A path that is not there is not
    the same file as anything.
    """
    if one == other:
        return True
    try:
        return one.samefile(other)
    except OSError:
        return False


def _walk_files(folder: Path) -> Iterator[Path]:
    """Every file below *folder*, however deep.

    An album folder may hold a ``Disc 1`` subfolder (the scanner flattens those
    into the album), so a move has to carry the tree, not just the top level.
    """
    for dirpath, _dirnames, filenames in os.walk(folder):
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _has_audio(folder: Path) -> bool:
    """Whether any audio file lives below *folder*."""
    for dirpath, _dirnames, filenames in os.walk(folder):
        for name in filenames:
            if Path(dirpath, name).suffix.casefold() in AUDIO_EXTENSIONS:
                return True
    return False


def _is_reserved(path: Path, root: Path) -> bool:
    """Whether *path* is, or lives under, ``.trash`` or ``.tmp``."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    parts = relative.parts
    # Case-folded: on a case-insensitive filesystem ``.TRASH/x`` is the very
    # same folder as ``.trash/x``, and a source spelled that way would
    # otherwise walk straight past every guard -- including the one that stops
    # the cleanup from climbing into, and removing, the trash root itself.
    return bool(parts) and parts[0].casefold() in _RESERVED_TOP_LEVEL


def _check_inside(destination: Path, root: Path) -> None:
    """Refuse a destination that resolves outside the library root.

    ``validate_library_path`` does this for the *source*, but the destination
    is built from names rather than parsed from a path, and a name that matches
    an existing folder is adopted as it stands -- and that folder may be a
    symlink pointing anywhere.  Without this, "move these tracks to Escape"
    where ``Escape`` is a symlink to ``/etc`` would write outside the library.

    Resolved non-strictly, so a destination whose folders do not exist yet
    still gets checked; a symlink that stays inside the root is fine, because
    what it lands on is still the library.

    Raises:
        LibraryPathError: The destination is outside *root*.
    """
    if not destination.resolve().is_relative_to(root.resolve()):
        raise LibraryPathError("that destination is outside the library")


def _check_ancestors_are_dirs(target: Path, stop: Path, root: Path) -> list[str]:
    """The ancestors of *target* below *stop* that exist but are not folders.

    A file (or a dangling symlink) sitting where a move needs a folder is a
    collision like any other, and the user can resolve it -- but only if they
    are told.  Left unchecked it surfaces as the ``FileExistsError`` from
    ``mkdir(parents=True)``, which the route can only turn into a 500.

    Returned outermost first, as relative POSIX strings; empty when every
    ancestor is a folder or is not there at all.
    """
    problems: list[str] = []
    current = target.parent
    while current != stop and current.is_relative_to(stop):
        if os.path.lexists(current) and not current.is_dir():
            problems.append(_rel(current, root))
        current = current.parent
    problems.reverse()
    return problems


def _check_type_conflicts(target: Path, stop: Path, root: Path) -> None:
    """Raise :class:`LibraryConflict` for any non-folder ancestor of *target*."""
    problems = _check_ancestors_are_dirs(target, stop, root)
    if problems:
        raise LibraryConflict(
            f"{problems[0]} is a file, not a folder", problems
        )


# ---------------------------------------------------------------------------
# Tag rewriting
# ---------------------------------------------------------------------------


def _rewrite_tags(
    path: Path,
    updates: dict[str, str | None],
    previous_artist: str | None = None,
) -> None:
    """Apply *updates* to a FLAC's Vorbis comments, deleting on a ``None``.

    *previous_artist* is the artist folder the file is leaving, and it guards
    the ``ARTIST`` update alone: it is applied only when the file's current
    ``ARTIST`` agreed with that folder.  A compilation's per-track credits and
    an album's guest artist are the whole point -- they disagreed with the
    folder before the move and must still disagree after it.  ``ALBUMARTIST``
    and ``ALBUM`` are unconditional: they are the folder's own name, and
    Navidrome and Lidarr group by them.  ``None`` (the default) means the file
    was under no artist folder at all -- loose in the library root, the
    scanner's synthetic bucket -- so there is no previous folder its ``ARTIST``
    could have agreed or disagreed with, and the proposal is applied
    unconditionally like the other two.

    Only the named fields are touched: mutagen writes the whole comment block
    back, but everything it did not have to change -- ``SOURCEID``,
    ``SOURCEURL``, ``TRACKNUMBER``, the embedded pictures -- comes out
    byte-identical.  Non-FLAC files are left alone, which the UX ticket calls
    for: mutagen could write MP3/M4A tags, but a move must not be the first
    thing that ever rewrites a file the user brought in themselves.

    A file that cannot be tagged is logged, not raised on.  It has already been
    moved, and failing the request now would report a move that did happen as
    an error the user cannot act on.
    """
    if path.suffix.casefold() != ".flac":
        return
    try:
        audio = FLAC(path)
    except Exception as exc:
        logger.warning("Could not open %s to rewrite its tags: %s", path.name, exc)
        return

    if previous_artist is not None and "ARTIST" in updates:
        current = audio.get("ARTIST") or []
        if not any(value.casefold() == previous_artist.casefold() for value in current):
            updates = {key: value for key, value in updates.items() if key != "ARTIST"}

    changed = False
    for key, value in updates.items():
        if value is None:
            if key in audio:
                del audio[key]
                changed = True
        elif audio.get(key) != [value]:
            audio[key] = value
            changed = True

    if not changed:
        return
    try:
        audio.save()
    except Exception as exc:
        logger.warning("Could not write tags to %s: %s", path.name, exc)


def _placement_tags(artist: str, album: str) -> dict[str, str | None]:
    """The three attribution tags for a file now living under *artist*/*album*.

    A blank album is a loose Single, whose ``ALBUM`` tag is removed rather than
    set to anything: an invented album name is what scatters a library across
    one-track albums in Navidrome.

    ``ARTIST`` is in here as a *proposal*: :func:`_rewrite_tags` drops it
    unless the file's current one agreed with the folder it is leaving, which
    is what the caller passes as ``previous_artist``.
    """
    return {
        "ALBUMARTIST": artist,
        "ARTIST": artist,
        "ALBUM": album if album else None,
    }


# ---------------------------------------------------------------------------
# Moving
# ---------------------------------------------------------------------------


def _rename_files(pairs: list[tuple[Path, Path]]) -> None:
    """Rename every pair, putting the already-renamed ones back on a failure.

    The targets were all checked as free before this ran, so a failure here is
    a filesystem problem (a full disk, a permission), not a collision -- and
    the all-or-nothing promise means the caller must not be left with half the
    album in the new place.

    ``os.rename`` would overwrite an existing target, hence the check first.
    The microseconds between that check and this call are the same window the
    downloader's duplicate check lives with, and closing it properly needs an
    atomic create that rename cannot give us; the write lock closes it against
    this app's own writers, which is what actually happens in practice.
    """
    done: list[tuple[Path, Path]] = []
    try:
        for source, target in pairs:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, target)
            done.append((source, target))
    except OSError:
        for source, target in reversed(done):
            try:
                os.rename(target, source)
            except OSError:
                logger.exception("Could not put %s back after a failed move", target)
        raise


def _rename_folder(source: Path, target: Path, root: Path) -> None:
    """Rename a whole folder, refusing a target that is something else.

    A case-only rename ("bonobo" -> "Bonobo") on a case-insensitive filesystem
    finds the target "already existing" while being the very folder we are
    renaming, so it goes through a temporary name instead of being refused.
    """
    if source == target:
        return
    if target.exists() or os.path.lexists(target):
        if not (target.exists() and target.samefile(source)):
            # A dangling symlink or a file where the folder should go is
            # ``lexists`` without ``exists``; it still occupies the name, and
            # ``os.rename`` onto it would either fail or destroy it.
            raise LibraryConflict(
                f"{_rel(target, root)} already exists", [_rel(target, root)]
            )
        # Same folder under a different spelling: two renames, because the
        # filesystem considers the one-step version a no-op.
        staging = target.with_name(f".{target.name}.renaming-{os.getpid()}")
        os.rename(source, staging)
        os.rename(staging, target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, target)


def _remove_leftovers(folder: Path) -> bool:
    """Delete *folder* and its non-audio contents, bottom-up, or nothing at all.

    Deliberately not ``shutil.rmtree``: between the "no audio below this"
    check and the removal, a download can file a track into exactly this
    folder -- the guard covers the jobs the queue knows about, but a job that
    finishes filing in that window is past it.  ``rmtree`` would delete the
    track.  Unlinking file by file and refusing the moment an audio file turns
    up cannot: the worst case is a folder left standing with one track in it,
    which the next move or the next scan tidies.

    Returns ``False`` -- having removed whatever it already had, all of it
    non-audio -- when it met an audio file or the filesystem refused
    something.  The caller stops climbing either way.
    """
    try:
        for dirpath, dirnames, filenames in os.walk(folder, topdown=False):
            for name in sorted(filenames):
                path = Path(dirpath, name)
                if _is_audio(path):
                    logger.info(
                        "Leaving %s in place: an audio file appeared in it", folder
                    )
                    return False
                path.unlink()
            for name in dirnames:
                child = Path(dirpath, name)
                # ``os.walk`` lists a symlink to a directory among the
                # dirnames, and ``rmdir`` on one raises ENOTDIR -- which would
                # abort the whole cleanup and leave the emptied album folder
                # standing.  Unlinking removes the link and nothing else; the
                # scanner never follows a symlinked directory, so nothing the
                # library could see is lost.
                if child.is_symlink():
                    child.unlink()
                else:
                    child.rmdir()
        folder.rmdir()
    except OSError as exc:
        logger.warning("Could not remove the empty folder %s: %s", folder, exc)
        return False
    return True


def _cleanup_upwards(
    folder: Path, root: Path, removed: list[str], in_flight: Iterable[str]
) -> None:
    """Remove *folder* and then its parent while they hold no audio.

    "Empty" means no audio file anywhere below, not an empty directory: the
    UX ticket is explicit that an album folder whose last track has left goes
    away together with its `cover.jpg` and `.nfo` leftovers, and then the
    artist folder gets the same test.  The library root, ``.trash`` and
    ``.tmp`` are never candidates.

    A folder an in-flight job is aiming at, or aiming inside, is left standing
    and stops the climb: the move itself was allowed because the job was not
    aiming into what moved, but the artist folder above it is exactly where
    the next download is about to appear.
    """
    in_flight = list(in_flight)
    current = folder
    while current != root and current.is_relative_to(root):
        if _is_reserved(current, root):
            return
        if _targets_inside(_rel(current, root), in_flight):
            return
        if current.is_dir():
            if _has_audio(current):
                return
            if not _remove_leftovers(current):
                return
            removed.append(_rel(current, root))
        current = current.parent


def _folders_of(paths: Iterable[Path], root: Path) -> list[str]:
    """The distinct folders *paths* live in, as relative POSIX strings."""
    seen: list[str] = []
    for path in paths:
        folder = path.parent
        rel = "" if folder == root else _rel(folder, root)
        if rel not in seen:
            seen.append(rel)
    return seen


# ---------------------------------------------------------------------------
# The in-flight guard
# ---------------------------------------------------------------------------


def _targets_inside(folder: str, in_flight: Iterable[str]) -> list[str]:
    """The in-flight targets that are *folder* itself or sit below it.

    *folder* is a relative POSIX path; the empty string is the library root,
    which is inside nothing and matches nothing -- guarding it would refuse
    every move while any download at all was running.
    """
    if not folder:
        return []
    prefix = folder + "/"
    return [
        target
        for target in in_flight
        if target == folder or target.startswith(prefix)
    ]


def _check_in_flight(guarded: Iterable[str], in_flight: Iterable[str]) -> None:
    """Refuse the move when a running download is aiming inside it.

    *guarded* are the folders the move is about to change (its sources and its
    destination); *in_flight* are the folders non-terminal jobs will file their
    track into.  A job whose folder is one of those, or sits inside one, would
    otherwise land in a folder that has just been renamed out from under it, or
    into a merge that has already counted its files.

    One conflict is reported, not all of them: they are all the same job's
    doing, and "wait for this download" is the whole message.
    """
    in_flight = list(in_flight)
    for folder in guarded:
        blocking = _targets_inside(folder, in_flight)
        if blocking:
            raise LibraryConflict(
                f"a download is in progress into {blocking[0]}; "
                "try again when it finishes",
                blocking[:1],
            )


def _check_resolved(unresolved: int, jobs: Iterable[str] = ()) -> None:
    """Refuse the move while a download has not said where it is going.

    A job between "queued" and its first metadata probe has no destination yet,
    so no guard can name the folder it is about to create.  Refusing for the
    second or two that lasts is the only honest answer; the alternative is
    renaming a folder a download is about to appear in.

    *jobs* describes the downloads being waited on -- a title, or the id when
    the probe has not returned one yet.  They travel out in ``conflicts`` the
    way an occupied path does, so the dialog can say *which* download the user
    is waiting for instead of leaving them to guess at the queue.
    """
    if unresolved:
        jobs = list(jobs)
        detail = f": {', '.join(jobs)}" if jobs else ""
        raise LibraryConflict(
            "a download has not resolved its destination yet"
            f"{detail}; try again in a moment",
            jobs,
        )


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
) -> MoveOutcome:
    """Move audio files that share a parent to ``artist``/``album``."""
    parents = {source.parent for source in sources}
    if len(parents) != 1:
        raise LibraryPathError(
            "all tracks in one move must come from the same folder"
        )
    (parent,) = parents

    artist_name = _folder_name(root, artist, FALLBACK_ARTIST, "artist")
    artist_dir = root / artist_name
    album_name = (
        _folder_name(artist_dir, album, NO_ALBUM, "album")
        if album is not None and album.strip()
        else NO_ALBUM
    )
    destination = artist_dir / album_name if album_name else artist_dir
    if _is_reserved(destination, root):
        raise LibraryPathError("that destination is reserved")
    _check_inside(destination, root)

    # Both ends are guarded.  The destination because a download filing into a
    # folder this move is merging into would land after the collision check;
    # the source folder because it may be emptied and cleaned up here, and a
    # job aiming into it would find it gone.  Only the source folder itself --
    # the artist folder above it is not what this move touches.
    parent_rel = "" if parent == root else _rel(parent, root)
    _check_in_flight([parent_rel, _rel(destination, root)], in_flight)

    pairs: list[tuple[Path, Path]] = []
    conflicts: list[str] = []
    for source in sources:
        target = destination / source.name
        if _same_file(target, source):
            # Already where it is going, spelling differences included: a move
            # to "bonobo" on a filesystem that already has "Bonobo".
            continue
        if os.path.lexists(target):
            # ``lexists``, not ``exists``: a dangling symlink occupies the name
            # just as firmly as a file does.
            conflicts.append(_rel(target, root))
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
    _check_type_conflicts(pairs[0][1], root, root)
    _check_resolved(unresolved, unresolved_jobs)

    _rename_files(pairs)
    tags = _placement_tags(artist_name, album_name)
    # The artist folder the files are leaving, or None for tracks loose in the
    # library root: those sit under no artist folder at all, so there is
    # nothing their ARTIST could have agreed with and the new artist is
    # written unconditionally -- which is what a user filing a stray track
    # under an artist is asking for.
    previous_artist = parent_rel.split("/")[0] if parent_rel else None
    for source, target in pairs:
        _rewrite_tags(target, tags, previous_artist=previous_artist)
        outcome.moved.append({"from": _rel(source, root), "to": _rel(target, root)})

    outcome.changed = _folders_of(
        [source for source, _ in pairs] + [target for _, target in pairs], root
    )
    _cleanup_upwards(parent, root, outcome.removed, in_flight)
    return outcome


def _move_album(
    source: Path,
    artist: str,
    album: str | None,
    root: Path,
    in_flight: Iterable[str],
    unresolved: int = 0,
    unresolved_jobs: Iterable[str] = (),
) -> MoveOutcome:
    """Move an album folder to another artist, merging where one exists."""
    artist_name = _folder_name(root, artist, FALLBACK_ARTIST, "artist")
    artist_dir = root / artist_name
    album_name = (
        _folder_name(artist_dir, album, NO_ALBUM, "album")
        if album is not None and album.strip()
        else NO_ALBUM
    ) or source.name
    destination = artist_dir / album_name
    if _is_reserved(destination, root):
        raise LibraryPathError("that destination is reserved")
    _check_inside(destination, root)

    source_rel = _rel(source, root)
    _check_in_flight([source_rel, _rel(destination, root)], in_flight)

    outcome = MoveOutcome()
    if destination == source:
        return outcome

    _check_resolved(unresolved, unresolved_jobs)
    # The artist folder has to be a folder before either branch below can run.
    _check_type_conflicts(destination, root, root)

    merging = destination.is_dir() and not _same_file(destination, source)
    previous_artist = source.parent.name
    if not merging:
        # Nothing in the way: one rename carries the folder, its subfolders and
        # its `cover.jpg` across, and is atomic in a way a file-by-file merge
        # can never be.
        _rename_folder(source, destination, root)
        # Enumerated from the *destination*, after the rename, not from the
        # source before it.  On the virtiofs and CIFS bind mounts this runs on
        # in a container, any lookup of a differently-cased spelling of a path
        # (a resolve, a stat, an is_dir -- and a case-only rename does several)
        # displaces the cached dentry, after which ``os.walk`` of the old path
        # yields nothing at all.  A case-only rename would then rewrite no tags
        # and report ``moved: []`` while the folder really had moved.  The
        # destination is the name the filesystem has just been told about, so
        # walking it is always right.
        tags = _placement_tags(artist_name, album_name)
        for target in _walk_files(destination):
            previous = source / target.relative_to(destination)
            _rewrite_tags(target, tags, previous_artist=previous_artist)
            outcome.moved.append(
                {"from": _rel(previous, root), "to": _rel(target, root)}
            )
        outcome.changed = [source_rel, _rel(destination, root)]
        outcome.destination = _rel(destination, root)
        _cleanup_upwards(source.parent, root, outcome.removed, in_flight)
        return outcome

    pairs: list[tuple[Path, Path]] = []
    conflicts: list[str] = []
    for previous in _walk_files(source):
        target = destination / previous.relative_to(source)
        # A subfolder of the source whose name is taken by a file in the
        # destination is a collision too, and one ``mkdir`` would only report
        # as a 500.
        conflicts.extend(_check_ancestors_are_dirs(target, destination, root))
        if not target.exists() and not os.path.lexists(target):
            pairs.append((previous, target))
        elif target.is_dir() or _is_audio(previous):
            conflicts.append(_rel(target, root))
        # A non-audio file that already exists in the target -- the album's own
        # `cover.jpg`, a `.nfo` -- is left behind rather than refused: the
        # destination album already has one, and losing a duplicate sidecar is
        # not worth failing a move over.  It goes with the source folder when
        # the cleanup removes it.

    if conflicts:
        # Distinct, order kept: an ancestor is reported once however many of
        # the album's files sit below it.
        conflicts = list(dict.fromkeys(conflicts))
        raise LibraryConflict(
            f"{len(conflicts)} file(s) already exist in {_rel(destination, root)}; "
            "nothing was moved",
            conflicts,
        )

    _rename_files(pairs)
    tags = _placement_tags(artist_name, album_name)
    for previous, target in pairs:
        _rewrite_tags(target, tags, previous_artist=previous_artist)
        outcome.moved.append({"from": _rel(previous, root), "to": _rel(target, root)})

    outcome.changed = [source_rel, _rel(destination, root)]
    outcome.destination = _rel(destination, root)
    _cleanup_upwards(source, root, outcome.removed, in_flight)
    return outcome


def _rename_artist(
    source: Path,
    artist: str,
    root: Path,
    in_flight: Iterable[str],
    unresolved: int = 0,
    unresolved_jobs: Iterable[str] = (),
) -> MoveOutcome:
    """Rename an artist folder and follow it in every FLAC below."""
    # ``adopt_existing=False``: renaming "bonobo" to "Bonobo" is a real request
    # and adopting the existing spelling would make it a silent no-op.
    destination = root / _folder_name(
        root, artist, FALLBACK_ARTIST, "artist", adopt_existing=False
    )
    if _is_reserved(destination, root):
        raise LibraryPathError("that destination is reserved")
    _check_inside(destination, root)

    source_rel = _rel(source, root)
    outcome = MoveOutcome()
    if destination == source:
        return outcome

    _check_in_flight([source_rel, _rel(destination, root)], in_flight)
    _check_resolved(unresolved, unresolved_jobs)
    _check_type_conflicts(destination, root, root)

    previous_name = source.name
    _rename_folder(source, destination, root)

    new_name = destination.name
    # Walked from the destination after the rename, not from the source before
    # it: on the virtiofs and CIFS bind mounts this runs on in a container, a
    # lookup of a differently-cased spelling of a path displaces the cached
    # dentry, and a case-only rename does several -- after which ``os.walk``
    # of the old path yields nothing and the rename rewrites no tags at all
    # while reporting ``moved: []``.
    for target in _walk_files(destination):
        previous = source / target.relative_to(destination)
        # ALBUMARTIST follows the folder unconditionally -- it is what
        # Navidrome and Lidarr group by, and it is the folder's own name.
        # ARTIST only follows when it agreed with the old folder name, which
        # _rewrite_tags decides from the file it has already opened.
        _rewrite_tags(
            target,
            {"ALBUMARTIST": new_name, "ARTIST": new_name},
            previous_artist=previous_name,
        )
        outcome.moved.append({"from": _rel(previous, root), "to": _rel(target, root)})

    outcome.changed = [source_rel, _rel(destination, root)]
    outcome.destination = _rel(destination, root)
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
) -> MoveOutcome:
    """Perform one move and return what it did.

    Exactly one of *path* and *paths* is given.  *paths* is always a set of
    tracks; *path* is a track when it names a file, an album when it names a
    folder at depth 2, and an artist rename when it names a folder at depth 1.

    Blocking, and not re-entrant: hold :data:`LIBRARY_WRITE_LOCK` around it.

    Raises:
        LibraryPathError: a malformed path or name, or a mixed selection (400).
        LibraryNotFound: a source that is not on disk (404).
        LibraryConflict: a target that is occupied, or a download in flight
            into one of the folders involved -- *unresolved* being non-zero
            counts as in flight into an unknown folder (409).
    """
    base = root.resolve()
    in_flight = list(in_flight)

    if (path is None) == (paths is None):
        raise LibraryPathError("give either 'path' or 'paths', not both")

    if paths is not None:
        if not paths:
            raise LibraryPathError("'paths' must name at least one track")
        sources = [validate_library_path(one, base) for one in paths]
        for source in sources:
            # Checked for every source, file or folder, before anything is
            # dispatched: ``.tmp`` holds a half-written download and ``.trash``
            # holds what the user has already deleted, and neither is part of
            # the library the move endpoint speaks about.
            if _is_reserved(source, base):
                raise LibraryPathError("that folder is not part of the library")
            if not source.is_file():
                raise LibraryNotFound("no such track")
        return _move_tracks(
            sources, artist, album, base, in_flight, unresolved, unresolved_jobs
        )

    assert path is not None
    source = validate_library_path(path, base)
    # Before the file/folder split, not inside the folder branch: a *file*
    # under ``.tmp`` or ``.trash`` was movable, which handed the user a
    # half-written download or lifted a track back out of the trash.
    if _is_reserved(source, base):
        raise LibraryPathError("that folder is not part of the library")
    if source.is_file():
        return _move_tracks(
            [source], artist, album, base, in_flight, unresolved, unresolved_jobs
        )
    if not source.is_dir():
        raise LibraryNotFound("no such album or artist")

    depth = len(source.relative_to(base).parts)
    if depth == 1:
        return _rename_artist(
            source, artist, base, in_flight, unresolved, unresolved_jobs
        )
    if depth == 2:
        return _move_album(
            source, artist, album, base, in_flight, unresolved, unresolved_jobs
        )
    raise LibraryPathError(
        "only a track, an album folder, or an artist folder can be moved"
    )
