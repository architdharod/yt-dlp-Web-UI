"""On-disk duplicate detection for bulk downloads.

The domain model's rule, in one place: given a candidate source track and the
Artist folder it would be filed under, decide whether that track is *already*
in the library.  Two passes, in this order:

1. **Provenance.**  Every download this app has ever written carries
   ``SOURCEID=<extractor>:<id>`` and ``SOURCEURL=<webpage_url>`` (domain
   model); files written before that read yt-dlp's own ``PURL``.  A match on
   either is exact -- it is literally the same source item -- so it is tried
   first and only against FLACs, which are the only files this app writes and
   the only ones the model says take part in tag-based dedup.
2. **Normalised title.**  ``normalise(clean_title(title))`` of the candidate
   against the same of every audio file's ``TITLE`` tag *and* its filename
   stem, for every format.  This is what catches a track the user downloaded
   from a different source, or before this app existed.

Both passes look at every audio file at or below the Artist folder -- albums,
loose Singles, and anything nested deeper -- because the same track filed under
a different album is still the same track.

The tree comes from :func:`app.library.scan_library`, deliberately rather than
from a second tag reader: it already holds the per-file tag dump in its cache,
so a preview of 200 rows against a 5,000-file library costs one stat per file
and no tag parses at all.  It also gives the trash for free: ``.trash`` and
``.tmp`` are dot-prefixed, the scanner skips dot-prefixed entries, and a track
in the trash is therefore invisible here -- which is the ticket's rule ("trash
is invisible to dedup") with nothing to enforce it separately.

Blocking (it walks the tree on a cache miss), so routes call it through
``asyncio.to_thread``.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

from app.library import scan_library
from app.tagger import clean_title, normalise

logger = logging.getLogger(__name__)

# Tag names carrying the provenance of a file, case-folded because mutagen
# lower-cases Vorbis comment keys on the way out and ID3/MP4 do not.
_SOURCE_ID_TAGS = ("sourceid",)
_SOURCE_URL_TAGS = ("sourceurl", "purl")

# Hosts whose URLs carry a YouTube video id, and where it sits.  A ``PURL``
# written by yt-dlp is a watch URL; a row's URL may be either shape, so both
# are reduced to the bare id before they are compared.
_YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com")

# The same hosts once ``www.``/``m.`` has been stripped, plus the short-link
# domain: what :func:`_canonical_url` checks to decide that a URL's identity is
# not its path.  Kept separate from ``_YOUTUBE_HOSTS`` above, which is matched
# against a raw hostname and so has to carry the prefixed variants.
_CANONICAL_YOUTUBE_HOSTS = frozenset({"youtube.com", "music.youtube.com", "youtu.be"})


@dataclass(frozen=True)
class DedupCandidate:
    """One source track to look for in the library.

    ``id`` is only a key to answer against -- the preview row's id, or a child
    job's id -- and is never compared with anything.
    """

    id: str
    url: str
    source_id: str | None = None
    title: str | None = None


def youtube_id(url: str | None) -> str | None:
    """The YouTube video id inside *url*, for the two URL shapes we may see.

    ``watch?v=<id>`` (any YouTube host) and ``youtu.be/<id>``.  Returns None for
    anything else, including a playlist URL, so a SoundCloud or Bandcamp URL
    never accidentally compares equal to one.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - urlsplit is very forgiving
        return None
    host = (parts.hostname or "").lower()
    if host in ("youtu.be", "www.youtu.be"):
        candidate = parts.path.lstrip("/").split("/")[0]
        return candidate or None
    if host in _YOUTUBE_HOSTS:
        values = parse_qs(parts.query).get("v")
        if values and values[0]:
            return values[0]
    return None


def _canonical_url(url: str | None) -> str | None:
    """A URL reduced to what makes two of them the same track.

    Scheme, a leading ``www.``/``m.``, the query string and any trailing slash
    are dropped: ``https://soundcloud.com/bonobo/kiara?in=x`` and
    ``http://www.soundcloud.com/bonobo/kiara/`` are one track.

    YouTube has **no** canonical URL here and returns None.  Its identity lives
    in the query string, so dropping the query would collapse every watch URL
    to ``youtube.com/watch`` and one stored YouTube FLAC would mark every
    YouTube candidate under that artist as already in the library.  YouTube is
    compared solely through the ``youtube:<id>`` key that :func:`youtube_id`
    builds.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover
        return None
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    if not host:
        return None
    if host in _CANONICAL_YOUTUBE_HOSTS or youtube_id(url):
        return None
    path = parts.path.rstrip("/")
    return f"{host}{path}".casefold()


def _tag_values(tags: dict[str, list[str]], names: Iterable[str]) -> list[str]:
    """Every value of the tags in *names*, matched case-insensitively."""
    wanted = {name.casefold() for name in names}
    found: list[str] = []
    for key, values in tags.items():
        if str(key).casefold() in wanted:
            found.extend(str(value) for value in values if value)
    return found


def _artist_entry(scan: dict[str, Any], artist: str) -> dict[str, Any] | None:
    """The scanned artist whose folder name equals *artist*, case-insensitively.

    The synthetic ``Unknown Artist`` bucket is skipped: it holds files loose at
    the library root, which are not under any artist folder, and a download
    filed under a real folder of that name would never collide with them.
    """
    wanted = artist.strip().casefold()
    if not wanted:
        return None
    for entry in scan.get("artists", []):
        if entry.get("synthetic"):
            continue
        if str(entry.get("name", "")).casefold() == wanted:
            return entry
    return None


def _tracks_of(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Every audio file under one scanned artist: album tracks and Singles."""
    tracks: list[dict[str, Any]] = list(entry.get("singles") or [])
    for album in entry.get("albums") or []:
        tracks.extend(album.get("tracks") or [])
    return tracks


class _LibraryIndex:
    """The three lookup tables one dedup pass needs, built once per call."""

    def __init__(self, tracks: list[dict[str, Any]]) -> None:
        # source id -> library path, FLAC only
        self.by_source_id: dict[str, str] = {}
        # canonical source URL, and bare YouTube id -> library path, FLAC only
        self.by_source_url: dict[str, str] = {}
        # normalised title -> library path, every audio format
        self.by_title: dict[str, str] = {}

        for track in tracks:
            path = str(track.get("path") or "")
            if not path:
                continue
            name = str(track.get("name") or "")
            tags = track.get("tags") or {}
            if name.casefold().endswith(".flac"):
                for value in _tag_values(tags, _SOURCE_ID_TAGS):
                    self.by_source_id.setdefault(value.strip().casefold(), path)
                for value in _tag_values(tags, _SOURCE_URL_TAGS):
                    canonical = _canonical_url(value)
                    if canonical:
                        self.by_source_url.setdefault(canonical, path)
                    video_id = youtube_id(value)
                    if video_id:
                        self.by_source_url.setdefault(f"youtube:{video_id}", path)
            # The TITLE tag and the filename stem are both offered: a file
            # written by something else may have one and not the other, and a
            # file whose tags this app wrote has the two agreeing anyway.
            for raw in (track.get("title"), Path(name).stem):
                key = normalise(clean_title(str(raw or "")))
                if key:
                    self.by_title.setdefault(key, path)

    def match(self, candidate: DedupCandidate) -> str | None:
        """The library path *candidate* is already at, or None."""
        if candidate.source_id:
            hit = self.by_source_id.get(candidate.source_id.strip().casefold())
            if hit:
                return hit
        canonical = _canonical_url(candidate.url)
        if canonical:
            hit = self.by_source_url.get(canonical)
            if hit:
                return hit
        video_id = youtube_id(candidate.url)
        if video_id is None and candidate.source_id:
            # A row whose source id is "youtube:<id>" but whose URL yt-dlp gave
            # in some other shape still compares against a stored PURL.
            prefix, separator, tail = candidate.source_id.partition(":")
            if separator and prefix.casefold() == "youtube":
                video_id = tail
        if video_id:
            hit = self.by_source_url.get(f"youtube:{video_id}")
            if hit:
                return hit
        key = normalise(clean_title(str(candidate.title or "")))
        if key:
            return self.by_title.get(key)
        return None


def find_in_library(
    artist: str,
    candidates: Iterable[DedupCandidate],
    root: Path | None = None,
) -> dict[str, str]:
    """Which of *candidates* are already under the *artist* folder.

    Args:
        artist: The artist folder the tracks would be filed under, matched
            case-insensitively against the folder names on disk.  A folder that
            does not exist yet simply matches nothing.
        candidates: The source tracks to look for.
        root: Library root; defaults to ``DOWNLOAD_PATH``.

    Returns:
        A dict of candidate id -> the POSIX library path of the file that
        matched, holding only the candidates that matched.  The path is what
        the preview and a skipped child job show the user, so it is the
        library identity and never an absolute path.

    Blocking: walks the tree (from the scan cache) and is called through
    ``asyncio.to_thread``.
    """
    rows = list(candidates)
    if not rows:
        return {}
    scan = scan_library(root)
    entry = _artist_entry(scan, artist)
    if entry is None:
        logger.debug("Dedup: no library folder for artist %r", artist)
        return {}
    index = _LibraryIndex(_tracks_of(entry))
    matches: dict[str, str] = {}
    for candidate in rows:
        hit = index.match(candidate)
        if hit:
            matches[candidate.id] = hit
    logger.info(
        "Dedup: %d of %d candidate(s) already under %r",
        len(matches),
        len(rows),
        artist,
    )
    return matches
