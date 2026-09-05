"""Shared filesystem operations over the Library tree.

Move (:mod:`app.mover`), delete and restore (:mod:`app.trash`) do the same
handful of things to the same tree: check that a set of target paths is free,
rename files and folders inside one filesystem, rewrite the three attribution
tags in the FLACs that moved, and tidy away the folders the operation emptied.
They also share the two guards that decide whether a write may happen at all --
the reserved dot-folders (``.tmp``, ``.trash``) and the in-flight downloads --
and the single lock that serialises every writer.

None of that is specific to moving, so it lives here rather than in one of the
two callers importing the other's private names.  What stays in each caller is
its own shape: the three moves, and delete/restore/empty-trash.

Blocking: every function here does real filesystem work and is called through
``asyncio.to_thread`` while :data:`LIBRARY_WRITE_LOCK` is held.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
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
    LibraryPathError,
)

logger = logging.getLogger(__name__)

# One lock over every write to the library tree.  Move, delete, restore and
# empty-trash all check a set of target paths and then act on them; two of
# those running at once could each find a free path and then race for it.
# Held by the route around the whole ``to_thread`` call, so the check and the
# renames are one critical section.
#
# Module-level and created without a running loop, which asyncio.Lock has
# allowed since 3.10 -- it binds to whichever loop first awaits it.
LIBRARY_WRITE_LOCK = asyncio.Lock()

# Folders no cleanup may ever remove and no move may ever target.
RESERVED_TOP_LEVEL = frozenset({TEMP_DIRNAME, TRASH_DIRNAME})


class LibraryConflict(Exception):
    """A write refused because something is already at the target path.

    Carries every conflicting path rather than the first one, so the dialog can
    show the user the whole list and they can decide once instead of retrying
    into one collision after another.  The in-flight guard raises this too: a
    download about to write into the folder being moved or trashed is the same
    kind of "not now, and here is what is in the way".
    """

    def __init__(self, message: str, conflicts: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.conflicts = list(conflicts)


class PartialRenameError(OSError):
    """A rename failed and the rollback could not put every file back.

    An ``OSError`` so every route keeps catching it where it already catches
    the filesystem saying no, and a class of its own because it means something
    much more specific than "the disk is full": the operation is *not*
    all-or-nothing any more, and the files in :attr:`stranded` are sitting
    somewhere the user did not put them.  :attr:`detail` is the sentence the
    500 carries -- written by whoever knows where those files ended up.
    """

    def __init__(self, stranded: Iterable[Path], detail: str | None = None) -> None:
        self.stranded = list(stranded)
        self.detail = detail or (
            f"{len(self.stranded)} file(s) could not be put back where they were"
        )
        super().__init__(self.detail)


# ---------------------------------------------------------------------------
# Small path helpers
# ---------------------------------------------------------------------------


def rel_path(path: Path, root: Path) -> str:
    """*path* as the POSIX string the API uses as an identity."""
    return path.relative_to(root).as_posix()


def is_audio(path: Path) -> bool:
    return path.suffix.casefold() in AUDIO_EXTENSIONS


def existing_dir_name(parent: Path, name: str) -> str | None:
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


def folder_name(
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
        existing = existing_dir_name(parent, cleaned)
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


def same_file(one: Path, other: Path) -> bool:
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


def walk_files(folder: Path) -> Iterator[Path]:
    """Every file below *folder*, however deep.

    An album folder may hold a ``Disc 1`` subfolder (the scanner flattens those
    into the album), so a move has to carry the tree, not just the top level.
    """
    for dirpath, _dirnames, filenames in os.walk(folder):
        for name in sorted(filenames):
            yield Path(dirpath) / name


def has_audio(folder: Path) -> bool:
    """Whether any audio file lives below *folder*."""
    for dirpath, _dirnames, filenames in os.walk(folder):
        for name in filenames:
            if Path(dirpath, name).suffix.casefold() in AUDIO_EXTENSIONS:
                return True
    return False


def is_reserved(path: Path, root: Path) -> bool:
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
    return bool(parts) and parts[0].casefold() in RESERVED_TOP_LEVEL


def check_inside(destination: Path, root: Path) -> None:
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


def check_ancestors_are_dirs(target: Path, stop: Path, root: Path) -> list[str]:
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
            problems.append(rel_path(current, root))
        current = current.parent
    problems.reverse()
    return problems


def check_type_conflicts(target: Path, stop: Path, root: Path) -> None:
    """Raise :class:`LibraryConflict` for any non-folder ancestor of *target*."""
    problems = check_ancestors_are_dirs(target, stop, root)
    if problems:
        raise LibraryConflict(
            f"{problems[0]} is a file, not a folder", problems
        )


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------


def resolve_destination(
    root: Path,
    artist: str,
    album: str | None,
    *,
    album_fallback: str = NO_ALBUM,
) -> tuple[str, str, Path]:
    """The artist name, album name and folder an "artist/album" request means.

    The one place the naming rules live, because a move, a track restore and an
    album restore have to agree on them exactly: an existing folder is matched
    case-insensitively and adopted as it is spelled on disk, a new one goes
    through ``sanitize_component``, and a blank album means *album_fallback* --
    :data:`~app.file_organizer.NO_ALBUM` for tracks, which puts them loose in
    the artist folder, and the album's own folder name where a folder is being
    moved and cannot become a Single.

    Returns ``(artist name, album name, destination)``; the album name is ``""``
    for a Single, in which case the destination is the artist folder itself.

    Raises:
        LibraryPathError: an unusable name, or a destination that is reserved
            or resolves outside the library.
    """
    artist_name = folder_name(root, artist, FALLBACK_ARTIST, "artist")
    artist_dir = root / artist_name
    album_name = (
        folder_name(artist_dir, album, NO_ALBUM, "album")
        if album is not None and album.strip()
        else NO_ALBUM
    ) or album_fallback
    destination = artist_dir / album_name if album_name else artist_dir
    if is_reserved(destination, root):
        raise LibraryPathError("that destination is reserved")
    check_inside(destination, root)
    return artist_name, album_name, destination


# ---------------------------------------------------------------------------
# Tag rewriting
# ---------------------------------------------------------------------------


def rewrite_tags(
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


def placement_tags(artist: str, album: str) -> dict[str, str | None]:
    """The three attribution tags for a file now living under *artist*/*album*.

    A blank album is a loose Single, whose ``ALBUM`` tag is removed rather than
    set to anything: an invented album name is what scatters a library across
    one-track albums in Navidrome.

    ``ARTIST`` is in here as a *proposal*: :func:`rewrite_tags` drops it
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


def rename_files(pairs: list[tuple[Path, Path]]) -> None:
    """Rename every pair, putting the already-renamed ones back on a failure.

    The targets were all checked as free before this ran, so a failure here is
    a filesystem problem (a full disk, a permission), not a collision -- and
    the all-or-nothing promise means the caller must not be left with half the
    album in the new place.

    A rollback that cannot itself put a file back raises
    :class:`PartialRenameError` instead of the original error, naming the
    sources it could not restore: the all-or-nothing promise has been broken,
    and a caller that assumed otherwise would go on to tidy away the place
    those files now live.

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
        stranded: list[Path] = []
        for source, target in reversed(done):
            try:
                os.rename(target, source)
            except OSError:
                logger.exception("Could not put %s back after a failed move", target)
                stranded.append(source)
        if stranded:
            # The promise is broken and the caller has to know it: those files
            # are still at their targets, and a caller that went on to tidy
            # away the place they now live would unlink the user's audio.
            raise PartialRenameError(stranded)
        raise


def merge_pairs(
    source: Path, destination: Path, root: Path
) -> tuple[list[tuple[Path, Path]], list[str]]:
    """The renames that merge *source*'s tree into the existing *destination*.

    Returns ``(pairs, conflicts)``: every file below *source* paired with where
    it goes, and the target paths that are already taken.  A non-empty
    ``conflicts`` means the caller renames nothing at all -- merging half a
    folder is the outcome neither a move nor a restore may leave.

    A subfolder of *source* whose name is taken by a *file* in *destination* is
    a collision like any other, reported here rather than left to surface from
    inside ``mkdir(parents=True)`` as a 500.  A non-audio file that already
    exists at its target -- the album's own ``cover.jpg``, a ``.nfo`` -- is
    neither moved nor refused: the destination already has one, and losing a
    duplicate sidecar is not worth failing over.  It is left where it is, for
    the caller's cleanup to take away with the source folder.
    """
    pairs: list[tuple[Path, Path]] = []
    conflicts: list[str] = []
    for previous in walk_files(source):
        target = destination / previous.relative_to(source)
        conflicts.extend(check_ancestors_are_dirs(target, destination, root))
        if not target.exists() and not os.path.lexists(target):
            pairs.append((previous, target))
        elif target.is_dir() or is_audio(previous):
            conflicts.append(rel_path(target, root))
    # Distinct, order kept: an ancestor is reported once however many of the
    # folder's files sit below it.
    return pairs, list(dict.fromkeys(conflicts))


def rename_folder(source: Path, target: Path, root: Path) -> None:
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
                f"{rel_path(target, root)} already exists", [rel_path(target, root)]
            )
        # Same folder under a different spelling: two renames, because the
        # filesystem considers the one-step version a no-op.
        #
        # The staging name is deliberately *not* dot-prefixed.  It used to be,
        # and a second rename that failed (a full disk, a permission, a
        # process killed between the two) then left the whole folder hidden
        # from the scanner, from Navidrome and from Lidarr -- the user's album
        # had silently vanished from every view they have of it.  Under this
        # name a half-finished rename shows up as a folder with an obviously
        # temporary suffix, which is something they can see and put right.  A
        # scan that catches it mid-rename lists one oddly-named album for a
        # moment; both renames happen under the write lock, so the window is
        # microseconds and the next scan has the real name.
        staging = target.with_name(f"{target.name}.moving-{uuid.uuid4().hex}")
        os.rename(source, staging)
        os.rename(staging, target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, target)


def remove_leftovers(folder: Path) -> bool:
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
                if is_audio(path):
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


def cleanup_upwards(
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
        if is_reserved(current, root):
            return
        if targets_inside(rel_path(current, root), in_flight):
            return
        if current.is_dir():
            if has_audio(current):
                return
            if not remove_leftovers(current):
                return
            removed.append(rel_path(current, root))
        current = current.parent


def folders_of(paths: Iterable[Path], root: Path) -> list[str]:
    """The distinct folders *paths* live in, as relative POSIX strings."""
    seen: list[str] = []
    for path in paths:
        folder = path.parent
        rel = "" if folder == root else rel_path(folder, root)
        if rel not in seen:
            seen.append(rel)
    return seen


# ---------------------------------------------------------------------------
# The in-flight guard
# ---------------------------------------------------------------------------


def targets_inside(folder: str, in_flight: Iterable[str]) -> list[str]:
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


def check_in_flight(guarded: Iterable[str], in_flight: Iterable[str]) -> None:
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
        blocking = targets_inside(folder, in_flight)
        if blocking:
            raise LibraryConflict(
                f"a download is in progress into {blocking[0]}; "
                "try again when it finishes",
                blocking[:1],
            )


def check_not_being_tagged(guarded: Iterable[str], tagging: Iterable[str]) -> None:
    """Refuse the write while a tagging job is working on one of *guarded*.

    *guarded* are the folders (or files) the move, delete or restore is about
    to change; *tagging* are the paths in-flight tagging jobs are rewriting.
    A tagging pass reads a folder, asks MusicBrainz about every track in it and
    then writes those files back, minutes later -- moving or deleting them in
    between leaves the pass writing to paths that are gone, or writing tags
    into a folder the user has just emptied.

    Containment counts in *both* directions, unlike a download's target, which
    is only ever a folder a write lands *in*.  Deleting ``Artist`` while
    ``Artist/Album`` is being tagged takes the tagged folder away; deleting
    ``Artist/Album/track.flac`` while ``Artist/Album`` is being tagged takes a
    file the pass is about to write.  A sibling -- ``Artist/Other`` -- is
    untouched by either and is not refused.

    One conflict is reported, as :func:`check_in_flight` does: they are all the
    same job's doing and "wait for this tag fix" is the whole message.
    """
    tagging = [one for one in tagging if one]
    for folder in guarded:
        if not folder:
            # The library root: guarding it would refuse every write while any
            # tagging job at all was running.
            continue
        for path in tagging:
            if (
                path == folder
                or path.startswith(folder + "/")
                or folder.startswith(path + "/")
            ):
                raise LibraryConflict(
                    f"{path} is being tagged; try again when it finishes",
                    [path],
                )


def check_resolved(unresolved: int, jobs: Iterable[str] = ()) -> None:
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
