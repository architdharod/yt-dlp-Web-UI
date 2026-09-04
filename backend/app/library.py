"""Read-only scanner for the Library — the ``DOWNLOAD_PATH`` tree.

The filesystem is the only source of truth (domain model): an Artist is a
folder at depth 1, an Album a folder at depth 2, a Track an audio file at
depth 3.  Audio files directly under an Artist are that artist's Singles;
audio files at the root belong to a synthetic ``Unknown Artist`` bucket that
is never created on disk; folders deeper than depth 2 flatten into their
Album, keeping their real relative path.

Nothing here writes to the library.  The only thing this module creates on
disk is the cover cache under ``DATA_PATH/covers``.

Two caches make a repeat ``GET /library`` cheap:

* a per-file tag cache keyed by ``(relative path, size, mtime_ns)``, so a
  second scan with no changes stats every file but parses none of them;
* a disk cover cache keyed by the album path plus a version stamp covering
  the folder, its sidecar images and its audio files, so a cover hit never
  parses an audio file and an overwritten cover is still noticed.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import mutagen
from mutagen.flac import Picture

from app.file_organizer import DEFAULT_DOWNLOAD_PATH

logger = logging.getLogger(__name__)

# Extensions the domain model calls a Track.  Compared case-folded.
AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}
)

# The synthetic artist that root-level files show under.  It carries
# ``synthetic: true`` and an empty path so the UI can mark it as needing
# sorting, and so it can never be confused with a real folder of the same
# name sitting next to it.
SYNTHETIC_ARTIST_NAME = "Unknown Artist"

# Sidecar cover filenames, in the order they are preferred.  Matched
# case-insensitively against the album folder's own entries.
COVER_FILENAMES: tuple[str, ...] = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg")

# Tag keys whose values are (or may be) binary picture data.  They are dropped
# from the tag dump rather than str()-ed into a wall of bytes.
_PICTURE_TAG_PREFIXES: tuple[str, ...] = (
    "apic",
    "covr",
    "metadata_block_picture",
    "coverart",
    "coverartmime",
    "pic",
)

# Tracks with no TRACKNUMBER sort after every numbered one rather than before.
_UNNUMBERED = 1 << 30

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")
_RIFF_MAGIC = b"RIFF"
_WEBP_MAGIC = b"WEBP"


class LibraryPathError(ValueError):
    """Raised when a client-supplied library path is malformed or escapes the root."""


class LibraryNotFound(LookupError):
    """Raised when a well-formed library path does not exist on disk."""


def get_download_path() -> Path:
    """Return the configured library root.

    Read the same way ``main.py`` reads it: docker compose substitutes an unset
    variable with an empty string, so ``or`` rather than a dict default.
    """
    return Path(os.environ.get("DOWNLOAD_PATH") or DEFAULT_DOWNLOAD_PATH)


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def validate_library_path(rel: str, root: Path | None = None) -> Path:
    """Return the absolute path *rel* names inside the library, or raise.

    Validation is exactly the domain model's rule: split on ``/``; every
    segment must be non-empty and neither ``.`` nor ``..``; no backslash and no
    NUL anywhere; and the path, with symlinks followed, must still land under
    ``DOWNLOAD_PATH.resolve()``.

    Phases 6 and 7 reuse this for move, delete, restore and tag paths, which is
    why it lives here rather than inside a route.

    Raises:
        LibraryPathError: the path is malformed or resolves outside the root.
    """
    if not isinstance(rel, str) or not rel:
        raise LibraryPathError("path must be a non-empty string")
    if "\x00" in rel:
        raise LibraryPathError("path contains a NUL byte")
    if "\\" in rel:
        raise LibraryPathError("path contains a backslash")

    segments = rel.split("/")
    for segment in segments:
        if not segment:
            raise LibraryPathError("path has an empty segment")
        if segment in (".", ".."):
            raise LibraryPathError("path contains '.' or '..'")

    base = (root if root is not None else get_download_path()).resolve()
    resolved = (base / rel).resolve()
    if not resolved.is_relative_to(base):
        raise LibraryPathError("path escapes the library root")
    return resolved


# ---------------------------------------------------------------------------
# Per-file tag reading, with a cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackInfo:
    """Everything a scan knows about one audio file, as read from its tags."""

    title: str
    artist: str | None
    album: str | None
    album_artist: str | None
    track_number: int | None
    disc_number: int | None
    duration: float | None
    bitrate: int | None
    sample_rate: int | None
    has_embedded_art: bool
    tags: dict[str, list[str]]
    error: str | None

    @property
    def sort_key(self) -> tuple[int, int, str]:
        return (
            self.disc_number or 1,
            self.track_number if self.track_number is not None else _UNNUMBERED,
            self.title.casefold(),
        )


@dataclass
class _CacheState:
    """The module's mutable state, all of it behind :data:`_lock`."""

    root: str | None = None
    entries: dict[tuple[str, int, int], TrackInfo] = field(default_factory=dict)
    tag_reads: int = 0
    last_result: dict[str, Any] | None = None
    last_finished_at: float = 0.0


_state = _CacheState()
_lock = threading.Lock()


def invalidate() -> None:
    """Drop every cached tag read and the last scan result.

    Later phases call this after a move, a delete, or a tag write, when the
    tree changed in ways a stat-only comparison would miss (a folder rename
    leaves every file's size and mtime alone).
    """
    with _lock:
        _state.root = None
        _state.entries.clear()
        _state.last_result = None
        _state.last_finished_at = 0.0


def tag_read_count() -> int:
    """Return how many files have had their tags parsed since the last reset.

    A test hook: the acceptance criterion "a second call with no changes reads
    no tags" is exactly this counter not moving.
    """
    with _lock:
        return _state.tag_reads


def reset_tag_read_count() -> None:
    """Zero the counter :func:`tag_read_count` reports."""
    with _lock:
        _state.tag_reads = 0


def _parse_number(value: str | None) -> int | None:
    """Parse a TRACKNUMBER/DISCNUMBER tag, including the ``3/12`` form."""
    if value is None:
        return None
    head = str(value).split("/", 1)[0].strip()
    if not head:
        return None
    try:
        return int(head)
    except ValueError:
        return None


def _tags_dump(tags_obj: Any) -> dict[str, list[str]]:
    """Return a JSON-safe copy of *tags_obj*, without any picture data.

    For FLAC and the Ogg family mutagen's easy view *is* the Vorbis comment
    dict, so this is the full raw tag set.  For ID3 and MP4 it is the
    easy-mapped view, which carries no binary frames to begin with; the picture
    filter below is belt and braces for formats (WAVE, AIFF) that have no easy
    wrapper and fall through to raw ID3.
    """
    if tags_obj is None:
        return {}
    dump: dict[str, list[str]] = {}
    try:
        items = list(tags_obj.items())
    except Exception:  # pragma: no cover - a tag object that will not iterate
        return {}
    for key, value in items:
        name = str(key)
        folded = name.casefold()
        if any(
            folded == prefix or folded.startswith(prefix + ":")
            for prefix in _PICTURE_TAG_PREFIXES
        ):
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        cleaned = [str(item) for item in values if not isinstance(item, (bytes, bytearray))]
        if cleaned:
            dump[name] = cleaned
    return dump


def _first(tags: dict[str, list[str]], *names: str) -> str | None:
    """Return the first value of the first present key among *names*."""
    for name in names:
        values = tags.get(name)
        if values:
            return values[0]
    return None


def _has_embedded_art(audio: Any) -> bool:
    """Whether *audio* (a non-easy mutagen file) carries a picture.

    Covers the four shapes in play: FLAC PICTURE blocks, MP4 ``covr`` atoms,
    ID3 ``APIC:`` frames, and the base64 ``metadata_block_picture`` comment
    the Ogg family uses.
    """
    pictures = getattr(audio, "pictures", None)
    if pictures:
        return True
    tags_obj = getattr(audio, "tags", None)
    if tags_obj is None:
        return False
    try:
        keys = [str(key).casefold() for key in tags_obj.keys()]
    except Exception:  # pragma: no cover - defensive
        return False
    return any(
        key == "covr" or key == "metadata_block_picture" or key.startswith("apic")
        for key in keys
    )


def _read_track_info(path: Path) -> TrackInfo:
    """Parse *path* with mutagen and return what the library shows about it.

    A file mutagen cannot read is still a Track: it is listed with its filename
    as the title, nulls everywhere else, and a short ``error`` so the UI can
    say why rather than silently hiding a file that is really there.
    """
    stem = path.stem
    try:
        easy = mutagen.File(path, easy=True)
        audio = mutagen.File(path)
    except Exception as exc:
        reason = type(exc).__name__ if not str(exc) else str(exc).split("\n", 1)[0]
        # The absolute path is deliberately only in the log, never in the model.
        logger.warning("Could not read tags from %s: %s", path, exc)
        return _unreadable(stem, _short_reason(reason, path))
    if easy is None or audio is None:
        logger.warning("Unrecognised audio file %s", path)
        return _unreadable(stem, "unrecognised audio file")

    tags = _tags_dump(easy.tags)
    info = getattr(audio, "info", None)
    duration = getattr(info, "length", None)
    bitrate = getattr(info, "bitrate", None)
    sample_rate = getattr(info, "sample_rate", None)

    # Lower-case keys are the easy view and the Vorbis comment dict; the
    # upper-case ID3 frame ids are the fall-back for the formats mutagen has no
    # easy wrapper for (WAVE, AIFF), which come back as raw ID3.
    title = _first(tags, "title", "TITLE", "TIT2") or stem
    return TrackInfo(
        title=title,
        artist=_first(tags, "artist", "ARTIST", "TPE1"),
        album=_first(tags, "album", "ALBUM", "TALB"),
        album_artist=_first(tags, "albumartist", "ALBUMARTIST", "album artist", "TPE2"),
        track_number=_parse_number(_first(tags, "tracknumber", "TRACKNUMBER", "TRCK")),
        disc_number=_parse_number(_first(tags, "discnumber", "DISCNUMBER", "TPOS")),
        duration=float(duration) if isinstance(duration, (int, float)) else None,
        bitrate=int(bitrate) if isinstance(bitrate, (int, float)) else None,
        sample_rate=int(sample_rate) if isinstance(sample_rate, (int, float)) else None,
        has_embedded_art=_has_embedded_art(audio),
        tags=tags,
        error=None,
    )


def _short_reason(reason: str, path: Path) -> str:
    """Strip anything that could leak an absolute path out of a mutagen message."""
    cleaned = reason.replace(str(path), path.name).replace(str(path.parent), "")
    cleaned = cleaned.strip().strip("'\"")
    return (cleaned[:120] or "could not be read") if cleaned else "could not be read"


def _unreadable(stem: str, reason: str) -> TrackInfo:
    return TrackInfo(
        title=stem,
        artist=None,
        album=None,
        album_artist=None,
        track_number=None,
        disc_number=None,
        duration=None,
        bitrate=None,
        sample_rate=None,
        has_embedded_art=False,
        tags={},
        error=reason,
    )


def _cached_track_info(rel: str, path: Path, size: int, mtime_ns: int) -> TrackInfo:
    """Return the cached :class:`TrackInfo` for *rel*, parsing the file if it moved on.

    The key is the triple ``(relative path, size, mtime_ns)``: the stat comes
    free from the ``os.scandir`` entry the caller already had, so an unchanged
    library costs one stat per file and no parses at all.
    """
    key = (rel, size, mtime_ns)
    with _lock:
        hit = _state.entries.get(key)
    if hit is not None:
        return hit
    info = _read_track_info(path)
    with _lock:
        _state.entries[key] = info
        _state.tag_reads += 1
    return info


# ---------------------------------------------------------------------------
# Walking the tree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AudioFile:
    """One audio file found by the walker, with the stat that found it."""

    path: Path
    rel: str
    name: str
    size: int
    mtime_ns: int


def _scandir(path: Path) -> list[os.DirEntry[str]]:
    """List *path*, returning an empty list when it cannot be read."""
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError as exc:
        logger.warning("Could not read directory %s: %s", path, exc)
        return []


def _is_hidden(name: str) -> bool:
    """Dot-prefixed entries are invisible to the library.

    This is what keeps ``.trash`` and ``.tmp`` — the trash and the per-job
    yt-dlp scratch directory — out of every scan, without special-casing their
    names anywhere but the constants that create them.
    """
    return name.startswith(".")


def _within_root(path: str | Path, root: Path) -> bool:
    """Whether *path*, with every symlink followed, still lands under *root*.

    Only ever asked of entries that are symlinks: a real file found by walking
    the tree is under the root by construction, and resolving every one of them
    would cost a syscall per file for a question already answered.
    """
    try:
        return Path(path).resolve().is_relative_to(root)
    except OSError:
        return False


def _audio_entry(entry: os.DirEntry[str], root: Path) -> _AudioFile | None:
    """Return the :class:`_AudioFile` for *entry*, or None if it is not a Track.

    A symlink is followed only while it stays inside the library: a link to a
    track in another artist's folder is a Track like any other, but one pointing
    at ``/etc/passwd.mp3`` would publish that file's tags, size and mtime
    through ``GET /library``, and hand the cover endpoint its embedded art.
    """
    if _is_hidden(entry.name):
        return None
    if Path(entry.name).suffix.casefold() not in AUDIO_EXTENSIONS:
        return None
    try:
        if not entry.is_file(follow_symlinks=True):
            return None
        if entry.is_symlink() and not _within_root(entry.path, root):
            return None
        stat = entry.stat(follow_symlinks=True)
    except OSError:
        return None
    path = Path(entry.path)
    return _AudioFile(
        path=path,
        rel=path.relative_to(root).as_posix(),
        name=entry.name,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def _is_real_dir(entry: os.DirEntry[str]) -> bool:
    """A directory that is not a symlink.

    Symlinked directories are skipped outright rather than resolved: the only
    reason to descend one would be to then prove it had not escaped the root,
    and a library that silently mirrors half of ``/`` is not worth the check.
    """
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _walk_audio(folder: Path, root: Path) -> Iterator[_AudioFile]:
    """Yield every audio file at or below *folder*, hidden entries excluded.

    Depth is not bounded: ``Artist/Album/Disc 1/x.flac`` and anything deeper
    flattens into the Album, and each track keeps its real relative path.
    """
    stack = [folder]
    while stack:
        current = stack.pop()
        for entry in _scandir(current):
            if _is_hidden(entry.name):
                continue
            if _is_real_dir(entry):
                stack.append(Path(entry.path))
                continue
            found = _audio_entry(entry, root)
            if found is not None:
                yield found


def _cover_file(
    folder: Path, root: Path, entries: Iterable[os.DirEntry[str]] | None = None
) -> Path | None:
    """Return the sidecar cover image in *folder*, matched case-insensitively.

    A symlinked ``cover.jpg`` is honoured only while its target is inside
    *root*; otherwise the endpoint would happily read and serve any file on the
    box that begins with image magic.
    """
    listing = list(entries) if entries is not None else _scandir(folder)
    by_name = {entry.name.casefold(): entry for entry in listing}
    for candidate in COVER_FILENAMES:
        entry = by_name.get(candidate)
        if entry is None:
            continue
        try:
            if not entry.is_file(follow_symlinks=True):
                continue
            if entry.is_symlink() and not _within_root(entry.path, root):
                continue
        except OSError:
            continue
        return Path(entry.path)
    return None


def _track_payload(found: _AudioFile, info: TrackInfo, mtime_ns: int) -> dict[str, Any]:
    """Build one ``LibraryTrack`` payload."""
    return {
        "path": found.rel,
        "name": found.name,
        "title": info.title,
        "artist": info.artist,
        "album": info.album,
        "album_artist": info.album_artist,
        "track_number": info.track_number,
        "disc_number": info.disc_number,
        "duration": info.duration,
        "format": Path(found.name).suffix.casefold().lstrip("."),
        "bitrate": info.bitrate,
        "sample_rate": info.sample_rate,
        "size": found.size,
        "mtime": datetime.fromtimestamp(mtime_ns / 1_000_000_000, timezone.utc).isoformat(),
        "has_embedded_art": info.has_embedded_art,
        "tags": info.tags,
        "error": info.error,
    }


def _read_tracks(files: Iterable[_AudioFile], seen: set[tuple[str, int, int]]) -> list[dict[str, Any]]:
    """Read every file in *files* and return sorted track payloads."""
    rows: list[tuple[tuple[int, int, str], dict[str, Any]]] = []
    for found in files:
        info = _cached_track_info(found.rel, found.path, found.size, found.mtime_ns)
        seen.add((found.rel, found.size, found.mtime_ns))
        rows.append((info.sort_key, _track_payload(found, info, found.mtime_ns)))
    rows.sort(key=lambda row: row[0])
    return [payload for _, payload in rows]


def _folder_mtime_ns(folder: Path) -> int:
    try:
        return folder.stat().st_mtime_ns
    except OSError:
        return 0


def _cover_version(
    folder: Path,
    root: Path,
    entries: Iterable[os.DirEntry[str]] | None = None,
    audio_mtimes: Iterable[int] | None = None,
) -> int:
    """Return the version stamp for whatever cover *folder* currently resolves to.

    An opaque change stamp, not a timestamp: it happens to be a nanosecond
    mtime today, but nothing may read a date out of it.  Because it is derived
    from mtimes it can repeat if one is rewound -- restoring a backup with
    ``cp -p`` over a newer file, say.  The server-side cache is unaffected (it
    is keyed by the same stamp, so the older entry is simply reused or
    rewritten); only a browser still holding the recurring ``v`` could keep
    showing the previous art until its cache is cleared.

    The folder's own mtime is not enough: overwriting ``cover.jpg`` in place, or
    re-tagging a track's embedded art, leaves the directory entry untouched on
    every filesystem that only bumps a folder's mtime when its listing changes.
    A cache keyed on the folder alone would then serve the previous image
    forever.  So the stamp is the newest of the folder, every sidecar image, and
    every audio file at or below the folder -- exactly the inputs the cover
    fallback chain reads.

    *entries* and *audio_mtimes* let a caller that has already listed the folder
    and stat-ed its audio files hand those in, so a scan pays no extra syscalls.
    """
    newest = _folder_mtime_ns(folder)

    listing = list(entries) if entries is not None else _scandir(folder)
    by_name = {entry.name.casefold(): entry for entry in listing}
    for candidate in COVER_FILENAMES:
        entry = by_name.get(candidate)
        if entry is None:
            continue
        try:
            if entry.is_symlink() and not _within_root(entry.path, root):
                continue
            newest = max(newest, entry.stat(follow_symlinks=True).st_mtime_ns)
        except OSError:
            continue

    mtimes = (
        audio_mtimes
        if audio_mtimes is not None
        else (found.mtime_ns for found in _walk_audio(folder, root))
    )
    for mtime_ns in mtimes:
        newest = max(newest, mtime_ns)
    return newest


def _scan_album(folder: Path, root: Path, seen: set[tuple[str, int, int]]) -> dict[str, Any]:
    """Scan one album folder into a ``LibraryAlbum`` payload."""
    entries = _scandir(folder)
    files = list(_walk_audio(folder, root))
    tracks = _read_tracks(files, seen)
    # "Any track", not "the first track": the cover endpoint falls back to the
    # first track that actually carries a picture, and a flag that disagreed
    # with what the endpoint serves would make the grid show a placeholder for
    # an album whose art loads fine.
    has_cover = any(track["has_embedded_art"] for track in tracks) or (
        _cover_file(folder, root, entries) is not None
    )
    return {
        "name": folder.name,
        "path": folder.relative_to(root).as_posix(),
        "track_count": len(tracks),
        # The frontend appends this to the cover URL, so anything that could
        # change the art -- a new or overwritten sidecar, re-tagged embedded
        # art, a track added or removed -- busts the browser cache too.
        "cover_version": _cover_version(
            folder, root, entries, [found.mtime_ns for found in files]
        ),
        "has_cover": has_cover,
        "tracks": tracks,
    }


def _artist_sort_key(artist: dict[str, Any]) -> tuple[int, str]:
    """Case-insensitive by name, with the synthetic bucket always last."""
    return (1 if artist["synthetic"] else 0, artist["name"].casefold())


def _build_artist(
    name: str,
    path: str,
    synthetic: bool,
    albums: list[dict[str, Any]],
    singles: list[dict[str, Any]],
) -> dict[str, Any]:
    albums.sort(key=lambda album: album["name"].casefold())
    # An album folder holding nothing but a cover.jpg (a half-finished download,
    # or art dropped in ahead of the music) would otherwise win the artist tile
    # and make the artist look like it leads with an empty album.
    cover_album = next(
        (album for album in albums if album["has_cover"] and album["track_count"] > 0),
        None,
    ) or next((album for album in albums if album["has_cover"]), None)
    return {
        "name": name,
        "path": path,
        "synthetic": synthetic,
        "album_count": len(albums),
        "track_count": sum(album["track_count"] for album in albums) + len(singles),
        "albums": albums,
        "singles": singles,
        "cover_album_path": cover_album["path"] if cover_album else None,
    }


def _scan(root: Path) -> dict[str, Any]:
    """Walk *root* once and build the whole ``LibraryResponse`` payload."""
    seen: set[tuple[str, int, int]] = set()
    artists: list[dict[str, Any]] = []
    root_files: list[_AudioFile] = []

    for entry in sorted(_scandir(root), key=lambda item: item.name):
        if _is_hidden(entry.name):
            continue
        if _is_real_dir(entry):
            artist_dir = Path(entry.path)
            albums: list[dict[str, Any]] = []
            singles_files: list[_AudioFile] = []
            for child in _scandir(artist_dir):
                if _is_hidden(child.name):
                    continue
                if _is_real_dir(child):
                    albums.append(_scan_album(Path(child.path), root, seen))
                    continue
                found = _audio_entry(child, root)
                if found is not None:
                    singles_files.append(found)
            singles = _read_tracks(singles_files, seen)
            artists.append(
                _build_artist(
                    entry.name,
                    artist_dir.relative_to(root).as_posix(),
                    False,
                    albums,
                    singles,
                )
            )
            continue
        found = _audio_entry(entry, root)
        if found is not None:
            root_files.append(found)

    if root_files:
        # Only ever emitted when there really are loose files at the root, and
        # kept distinct from a real folder of the same name by ``synthetic``.
        artists.append(
            _build_artist(SYNTHETIC_ARTIST_NAME, "", True, [], _read_tracks(root_files, seen))
        )

    artists.sort(key=_artist_sort_key)

    # Files that disappeared since the previous scan stop paying for cache
    # space; without this the dict grows once per edit for the process's life.
    with _lock:
        _state.entries = {key: value for key, value in _state.entries.items() if key in seen}

    return {
        "artists": artists,
        "artist_count": len(artists),
        "album_count": sum(artist["album_count"] for artist in artists),
        "track_count": sum(artist["track_count"] for artist in artists),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


# Held for the duration of a scan; _lock only ever guards short dict updates,
# so the two must be different locks or a scan would block the cache.
_lock_scan = threading.Lock()


def scan_library(root: Path | None = None) -> dict[str, Any]:
    """Scan the library and return the ``LibraryResponse`` payload.

    Blocking: the route calls this through ``asyncio.to_thread``.  One lock
    serialises scans, and a caller that arrived before a scan that has since
    finished is handed that result instead of walking the tree again — two
    browser tabs refreshing together do one scan, not two.
    """
    # Resolved, so ``_within_root`` compares like with like: an unresolved base
    # (a /tmp that is really /private/tmp on macOS) would reject every symlink.
    base = (root if root is not None else get_download_path()).resolve()
    key = str(base)
    arrived = time.monotonic()

    with _lock:
        if _state.root != key:
            # A different root (a new test, or a reconfigured deployment) shares
            # nothing with the previous one: relative paths would collide.
            _state.root = key
            _state.entries.clear()
            _state.last_result = None
            _state.last_finished_at = 0.0

    with _lock_scan:
        with _lock:
            fresh = (
                _state.last_result is not None
                and _state.root == key
                and _state.last_finished_at > arrived
            )
            if fresh:
                return _state.last_result  # type: ignore[return-value]
        result = _scan(base)
        with _lock:
            _state.last_result = result
            _state.last_finished_at = time.monotonic()
        return result


# ---------------------------------------------------------------------------
# Cover art
# ---------------------------------------------------------------------------


def _sniff_image_mime(data: bytes) -> str | None:
    """Return the image type *data* really is, or None if it is not an image.

    The only thing allowed to decide a cover's ``Content-Type``.  A tag's
    declared MIME and a file's suffix are both attacker-controlled -- a download
    can arrive carrying a PICTURE block that claims ``text/html`` around a
    payload of script -- and echoing either would turn the cover endpoint into a
    way to serve arbitrary active content from the app's own origin.
    """
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data.startswith(_GIF_MAGICS):
        return "image/gif"
    if data.startswith(_RIFF_MAGIC) and data[8:12] == _WEBP_MAGIC:
        return "image/webp"
    return None


def _sniffed(data: bytes | None) -> tuple[bytes, str] | None:
    """Return ``(data, content_type)`` when *data* is recognisably an image."""
    if not data:
        return None
    mime = _sniff_image_mime(data)
    return (data, mime) if mime is not None else None


def _embedded_picture(path: Path) -> tuple[bytes, str] | None:
    """Return ``(data, content_type)`` of *path*'s front cover, if it has one.

    A picture whose bytes are not a recognised image format is skipped rather
    than served, so the caller falls through to the next track, then the
    sidecar, then the placeholder.
    """
    try:
        audio = mutagen.File(path)
    except Exception as exc:
        logger.warning("Could not read cover art from %s: %s", path, exc)
        return None
    if audio is None:
        return None

    for picture in getattr(audio, "pictures", None) or ():
        found = _sniffed(bytes(picture.data or b""))
        if found is not None:
            return found

    tags_obj = getattr(audio, "tags", None)
    if tags_obj is None:
        return None

    # MP4 ``covr``: a list of MP4Cover, whose imageformat says JPEG or PNG.
    try:
        covr = tags_obj.get("covr") if hasattr(tags_obj, "get") else None
    except Exception:
        covr = None
    for cover in covr or ():
        found = _sniffed(bytes(cover))
        if found is not None:
            return found

    # ID3 ``APIC:<desc>`` frames.
    for key in list(getattr(tags_obj, "keys", lambda: [])()):
        if str(key).casefold().startswith("apic"):
            frame = tags_obj[key]
            found = _sniffed(bytes(getattr(frame, "data", b"") or b""))
            if found is not None:
                return found

    # Ogg/Opus: base64-encoded FLAC PICTURE blocks in a Vorbis comment.
    try:
        blocks = tags_obj.get("metadata_block_picture") if hasattr(tags_obj, "get") else None
    except Exception:
        blocks = None
    for block in blocks or ():
        try:
            picture = Picture(base64.b64decode(block))
        except Exception as exc:
            logger.warning("Could not decode METADATA_BLOCK_PICTURE in %s: %s", path, exc)
            continue
        found = _sniffed(bytes(picture.data or b""))
        if found is not None:
            return found
    return None


def placeholder_svg(rel: str, label: str | None = None) -> bytes:
    """Return a deterministic SVG cover for *rel*.

    Deterministic on purpose: the same album gets the same colour on every
    request and in every browser tab, so a library of placeholders still reads
    as a grid of distinct things rather than a wall of identical grey.
    """
    name = label if label is not None else (rel.rsplit("/", 1)[-1] or SYNTHETIC_ARTIST_NAME)
    words = [word for word in name.replace("_", " ").split() if word]
    initials = "".join(word[0] for word in words[:2]).upper() or "?"
    digest = hashlib.sha256(rel.encode("utf-8")).hexdigest()
    hue = int(digest[:8], 16) % 360
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" '
        'viewBox="0 0 512 512" role="img" aria-label="No cover art">'
        f'<rect width="512" height="512" fill="hsl({hue},42%,34%)"/>'
        f'<text x="256" y="256" fill="hsl({hue},38%,88%)" font-size="200" '
        'font-family="system-ui,-apple-system,Segoe UI,Helvetica,Arial,sans-serif" '
        'text-anchor="middle" dominant-baseline="central">'
        f"{_escape_xml(initials)}</text></svg>"
    )
    return svg.encode("utf-8")


def _escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_EXTENSION_FOR_TYPE = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}
_TYPE_FOR_EXTENSION = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}


def cover_cache_dir(data_path: Path) -> Path:
    return Path(data_path) / "covers"


def _cache_key(rel: str) -> str:
    return hashlib.sha256(rel.encode("utf-8")).hexdigest()


def _write_cache_atomically(target: Path, data: bytes) -> None:
    """Write *data* to *target* via a temp file and a rename.

    A half-written cover would be served as a truncated image forever, so the
    file only appears at its final name once every byte is on disk.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError as exc:
        logger.warning("Could not cache cover %s: %s", target.name, exc)
        tmp.unlink(missing_ok=True)


def _drop_stale_cache(directory: Path, prefix: str, keep: str) -> None:
    """Remove this album's earlier cover cache entries."""
    try:
        for entry in directory.glob(f"{prefix}-*"):
            if entry.name != keep:
                entry.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - defensive
        logger.debug("Could not prune stale cover cache for %s: %s", prefix, exc)


@dataclass(frozen=True)
class Cover:
    """A cover image ready to be served, plus the cache identity to tag it with."""

    data: bytes
    content_type: str
    etag: str


def _album_cover_bytes(folder: Path, rel: str, root: Path) -> tuple[bytes, str]:
    """Resolve *folder*'s cover through the three fallbacks.

    *root* is the library root, so the tag cache is keyed by the same relative
    paths a scan uses and the two share their reads instead of each filling the
    cache with its own view of the same file.
    """
    entries = _scandir(folder)
    tracks: list[tuple[tuple[int, int, str], Path]] = []
    for found in _walk_audio(folder, root):
        info = _cached_track_info(found.rel, found.path, found.size, found.mtime_ns)
        # ``has_embedded_art`` and ``_embedded_picture`` test exactly the same
        # four picture sources, so a False here is a structural guarantee that
        # opening the file would find nothing -- and the tag cache already
        # answered it.  Skipping keeps a sidecar-only album's cover miss to
        # zero audio parses.
        if info.has_embedded_art:
            tracks.append((info.sort_key, found.path))
    tracks.sort(key=lambda row: row[0])

    for _, track_path in tracks:
        picture = _embedded_picture(track_path)
        if picture is not None:
            return picture

    sidecar = _cover_file(folder, root, entries)
    if sidecar is not None:
        try:
            data = sidecar.read_bytes()
        except OSError as exc:
            logger.warning("Could not read %s: %s", sidecar, exc)
        else:
            # The suffix says nothing about the content: a ``cover.jpg`` that is
            # really a GIF is served as a GIF, and one that is really HTML is
            # not served at all.
            found = _sniffed(data)
            if found is not None:
                return found
            logger.warning("Ignoring %s: not a recognised image", sidecar.name)

    # The one content type not sniffed, because it is the one we generate.
    return placeholder_svg(rel), "image/svg+xml"


def get_album_cover(rel: str, root: Path | None = None, data_path: Path | None = None) -> Cover:
    """Return the cover for the album at *rel*, using and filling the disk cache.

    Blocking: the route calls this through ``asyncio.to_thread``.

    Raises:
        LibraryPathError: *rel* is malformed or escapes the root.
        LibraryNotFound: *rel* is not an existing folder.
    """
    if not rel:
        # The synthetic root bucket owns no folder, so there is nothing to read
        # a cover out of and nothing whose mtime could invalidate one.
        data = placeholder_svg("", SYNTHETIC_ARTIST_NAME)
        return Cover(data, "image/svg+xml", f'"{_cache_key("")}-0.svg"')

    base = (root if root is not None else get_download_path()).resolve()
    folder = validate_library_path(rel, base)
    if not folder.is_dir():
        raise LibraryNotFound("no such album")

    if "/" not in rel:
        # Depth 1 is an Artist, and an artist has no cover of its own: the tile
        # asks for its ``cover_album_path`` instead.  Answering with the first
        # album's art here would make the two disagree whenever an artist's
        # albums sort differently from their folders.
        return Cover(placeholder_svg(rel), "image/svg+xml", f'"{_cache_key(rel)}-0.svg"')

    prefix = _cache_key(rel)
    # The same stamp the scan puts on ``cover_version``, so a cache entry is
    # only reused while every input the fallback chain reads is unchanged.
    version = _cover_version(folder, base)
    directory = cover_cache_dir(data_path if data_path is not None else _default_data_path())

    for extension, content_type in _TYPE_FOR_EXTENSION.items():
        candidate = directory / f"{prefix}-{version}.{extension}"
        if candidate.is_file():
            try:
                return Cover(candidate.read_bytes(), content_type, f'"{candidate.name}"')
            except OSError:
                break

    data, content_type = _album_cover_bytes(folder, rel, base)
    extension = _EXTENSION_FOR_TYPE.get(content_type.casefold(), "jpg")
    name = f"{prefix}-{version}.{extension}"
    _write_cache_atomically(directory / name, data)
    _drop_stale_cache(directory, prefix, name)
    return Cover(data, content_type, f'"{name}"')


def _default_data_path() -> Path:
    # Imported lazily: job_store owns the DATA_PATH contract and importing it at
    # module scope would make this module depend on the queue for a cache dir.
    from app.job_store import get_data_path

    return Path(get_data_path())
