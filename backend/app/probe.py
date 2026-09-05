"""Classify a pasted URL, and enumerate a collection behind it.

``POST /download/probe`` asks one question -- "is this one track or a list of
them?" -- and, when it is a list, has to come back with the checklist the user
picks from.  Both answers come from a *flat* yt-dlp extraction
(``extract_flat="in_playlist"``), which is the cheap pass: 1-6 s for an artist
page against the ~1-3 s **per track** a full extraction costs (source
enumeration research).  The consequence is that a row's metadata is thin, and
deliberately so: a child job resolves its own title, duration and thumbnail
when it runs, and the preview only has to be good enough to tick boxes in.

What this module does, in order:

* one flat extraction of the pasted URL.  ``_type`` missing or ``"video"`` is a
  single track and the route answers ``{"type": "track"}``; ``"playlist"`` or
  ``"multi_video"`` is a collection;
* a walk of the entries.  yt-dlp already materialises nested playlists (a
  YouTube channel root comes back as one playlist per tab), so those are simply
  descended into.  An entry that *points* at a collection -- a YouTube
  ``playlist?list=`` / ``ie_key="YoutubeTab"`` row from a ``/releases`` tab, a
  SoundCloud ``/sets/`` row, a Bandcamp ``/album/`` row -- costs one more flat
  extraction each, and exactly one: depth is capped at 1 so a channel of
  playlists of playlists cannot turn a preview into a crawl;
* album grouping only where the source actually says "album" (OLAK playlists,
  SoundCloud sets carrying ``album``/``album_type``, Bandcamp ``/album/``
  pages).  A plain playlist or a channel tab is a bag of tracks, and its rows
  get no album at all -- they become loose Singles under the artist, which is
  what the domain model says an album-less track is;
* the caps: :data:`~app.models.MAX_COLLECTION_TRACKS` rows stops the whole
  thing with an error asking for a narrower URL, and everything is bounded by
  ``PROBE_TIMEOUT_SECONDS`` end to end.

Dedup status is deliberately *not* part of the enumeration: it is recomputed on
every probe (from the library scan cache, so it is nearly free) and the
enumeration alone is what the cache below holds.  A user who downloads half a
preview and re-probes must see the other half marked "in library".
"""

import asyncio
import logging
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

import yt_dlp

from app.downloader import _YtDlpLogger, base_opts, ytdl
from app.models import (
    MAX_COLLECTION_TRACKS,
    MAX_FOLDER_NAME,
    MAX_PATH_LENGTH,
    MAX_REASON,
    MAX_SOURCE_ID,
    MAX_TRACK_TITLE,
)

logger = logging.getLogger(__name__)

DEFAULT_PROBE_TIMEOUT_SECONDS = 120

# How many sub-collections one probe may expand.  A YouTube ``/releases`` tab
# is 50-odd albums and a SoundCloud ``/sets`` page about the same; 200 is far
# past any real discography and keeps the worst case at 200 flat calls rather
# than at "however many rows the page had".
MAX_SUBCOLLECTIONS = 200

# How many probes may hold an executor thread at once.  A probe is a long
# blocking call on the default executor, and the executor is shared with the
# rest of the app; two at a time is enough for a user with a second tab open
# and leaves the pool for everything else.  A third probe waits here rather
# than queueing invisibly behind a thread, which is also why the slot is taken
# *before* the deadline is set: a probe that waited 30 s for a slot still gets
# its whole ``PROBE_TIMEOUT_SECONDS`` to do the work.
MAX_CONCURRENT_PROBES = 2
_probe_slots = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

# The enumeration cache.  Per URL, for the session: re-opening a preview after
# submitting half of it must not re-crawl a channel, and the bulk submit that
# follows a preview reads no cache at all -- it works from the rows the client
# sends back.
CACHE_TTL_SECONDS = 30 * 60
CACHE_MAX_ENTRIES = 32

# yt-dlp fields that mean "this entry exists but cannot be downloaded".
_UNAVAILABLE_AVAILABILITY = {
    "premium_only": "premium only",
    "subscriber_only": "subscribers only",
    "needs_auth": "needs an account",
}

Source = Literal["youtube", "soundcloud", "bandcamp", "other"]

# The one notice this phase raises about a source itself.  Bandcamp's streams
# really are 128 kbps MP3 (enumeration research); the pipeline wraps them in
# FLAC like everything else, and a user who sees "FLAC" in the library deserves
# to know it is not a lossless master.
BANDCAMP_NOTICE = (
    "Bandcamp streams are 128 kbps MP3; downloads are converted to FLAC but "
    "are not lossless"
)

TOO_LARGE_MESSAGE = (
    f"This collection has more than {MAX_COLLECTION_TRACKS} tracks. "
    "Use a narrower URL -- a single album or playlist rather than a whole "
    "artist or channel."
)

EMPTY_MESSAGE = "This collection has no downloadable tracks"

# What a swallowed failure is called when yt-dlp did not say anything useful.
NOTHING_RETURNED_MESSAGE = "yt-dlp returned nothing for this URL"

# yt-dlp colours its console output when it thinks it has a terminal.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# YouTube's auto-generated album playlists.  The only place YouTube admits, in
# the flat pass, that a list of videos is an album.
_OLAK_PREFIX = "OLAK5uy_"

# YouTube's auto-generated artist channels are "<Artist> - Topic".
_TOPIC_SUFFIX = " - Topic"

# SoundCloud names a user's own listing pages "<user> (All)", "(Tracks)",
# "(Albums)", "(Sets)"...  The suffix is the page, not the artist.
_SOUNDCLOUD_PAGE_SUFFIXES = (
    " (All)",
    " (Tracks)",
    " (Albums)",
    " (Sets)",
    " (Reposts)",
    " (Likes)",
    " (Spotlight)",
)


def probe_timeout_seconds() -> int:
    """How long a whole probe may take, from the env or the default.

    docker compose substitutes an unset variable with an empty string, so
    ``""`` counts as unset rather than crashing ``int()``.
    """
    raw = os.environ.get("PROBE_TIMEOUT_SECONDS")
    return int(raw) if raw else DEFAULT_PROBE_TIMEOUT_SECONDS


class ProbeError(Exception):
    """The URL could not be enumerated.  Routes map this to 400."""


class CollectionTooLarge(ProbeError):
    """More than :data:`~app.models.MAX_COLLECTION_TRACKS` rows would result."""

    def __init__(self) -> None:
        super().__init__(TOO_LARGE_MESSAGE)


class EmptyCollection(ProbeError):
    """The URL is a collection, but nothing in it can be downloaded."""

    def __init__(self) -> None:
        super().__init__(EMPTY_MESSAGE)


class ProbeTimeout(Exception):
    """The probe ran past its deadline.  Routes map this to 504."""


@dataclass(frozen=True)
class SingleTrack:
    """What the flat pass learned about a URL that is one track."""

    title: str | None
    duration: float | None
    thumbnail_url: str | None
    artist: str | None
    album: str | None


@dataclass(frozen=True)
class EnumeratedTrack:
    """One row of a collection, as the flat pass saw it.

    ``unavailable_reason`` is the whole of the availability answer: ``None``
    means the row can be downloaded, and anything else is yt-dlp's own words
    for why it cannot, shown to the user as-is.
    """

    id: str
    url: str
    source_id: str | None = None
    title: str | None = None
    album: str | None = None
    duration: float | None = None
    thumbnail_url: str | None = None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class Enumeration:
    """Every row behind a collection URL, plus what to say about the source."""

    url: str
    title: str | None
    artist: str | None
    source: Source
    rows: tuple[EnumeratedTrack, ...] = ()
    notices: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# The enumeration cache
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    stored_at: float
    enumeration: Enumeration


_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    """Forget every cached enumeration.  For tests and for a reconfigured app."""
    with _cache_lock:
        _cache.clear()


def _cache_get(url: str) -> Enumeration | None:
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(url)
        if entry is None:
            return None
        if now - entry.stored_at > CACHE_TTL_SECONDS:
            _cache.pop(url, None)
            return None
        return entry.enumeration


def _cache_put(url: str, enumeration: Enumeration) -> None:
    with _cache_lock:
        _cache[url] = _CacheEntry(time.monotonic(), enumeration)
        while len(_cache) > CACHE_MAX_ENTRIES:
            # Insertion-ordered, so the first key is the oldest write.  A plain
            # bound rather than an LRU: the cache exists so re-opening *this*
            # preview is cheap, not to keep a working set warm.
            _cache.pop(next(iter(_cache)))


# ---------------------------------------------------------------------------
# yt-dlp
# ---------------------------------------------------------------------------


def _flat_opts() -> dict:
    """The options every extraction in this module uses.

    Built on :func:`~app.downloader.base_opts` so the host allowlist
    (``allowed_extractors``, which keeps the generic extractor -- and with it
    the whole internal network -- out of reach) and ``noplaylist`` are the same
    ones the downloader runs with.  ``noplaylist`` matters here for the reason
    it matters there: a URL copied while a mix was open carries a ``list=``
    parameter, and the probe must call that a *track*, exactly as
    ``POST /download`` would.

    ``ignoreerrors`` is what turns one dead entry into a ``None`` in the list
    instead of an exception that loses the other 200 rows, and
    ``playlist_items`` stops pagination one past the cap so "more than 2000"
    can be detected without fetching a 20,000-video channel.  ``playlistend``
    would do the same but is deprecated in favour of ``playlist_items``.
    """
    return {
        **base_opts(),
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "skip_download": True,
        "extractor_retries": 1,
        "playlist_items": f"1:{MAX_COLLECTION_TRACKS + 1}",
    }


class _CapturingLogger(_YtDlpLogger):
    """The downloader's logger, keeping the last ``ERROR:`` line for the user.

    ``ignoreerrors`` is what stops one dead entry from losing 200 good rows,
    but it swallows a *top-level* failure too: yt-dlp logs "This channel does
    not have a videos tab" and hands back ``None``, and without this the user
    would be told only that "yt-dlp returned nothing for this URL".  Everything
    still goes to the ``yt_dlp`` log through the base class; the last error is
    merely also remembered, so :func:`_extract` can put it in the 400.
    """

    def __init__(self) -> None:
        self.last_error: str | None = None

    def error(self, msg: str) -> None:
        text = _clean_yt_dlp_message(msg)
        if text:
            self.last_error = text
        super().error(msg)


def _clean_yt_dlp_message(msg: object) -> str:
    """One of yt-dlp's console lines as a sentence fit for an API error."""
    text = _ANSI_ESCAPE.sub("", str(msg)).strip()
    if text.startswith("ERROR:"):
        text = text[len("ERROR:") :].strip()
    return text


def _extract(url: str) -> dict:
    """One flat extraction, with yt-dlp's failures turned into ProbeError."""
    log = _CapturingLogger()
    try:
        with ytdl({**_flat_opts(), "logger": log}) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.YoutubeDLError as exc:
        raise ProbeError(str(exc)) from exc
    if info is None:
        # ``ignoreerrors`` turned the failure into a None; the reason only ever
        # reached the logger.
        raise ProbeError(log.last_error or NOTHING_RETURNED_MESSAGE)
    return info


def is_collection(info: dict) -> bool:
    """Whether *info* describes a collection rather than a single item.

    ``_type`` is the stable signal (enumeration research): missing or
    ``"video"`` is one item, ``"playlist"``/``"multi_video"`` is a list.
    """
    return info.get("_type") in ("playlist", "multi_video")


# ---------------------------------------------------------------------------
# Reading a flat entry
# ---------------------------------------------------------------------------


def flat_source_id(entry: dict) -> str | None:
    """The ``<extractor>:<id>`` provenance string for a *flat* entry.

    Must produce exactly what :func:`app.downloader._source_id` writes into
    ``SOURCEID`` when the same track is downloaded, or dedup by provenance
    would never match anything.  The one difference between the two dicts is
    the key: a fully extracted info dict carries ``extractor`` (``"youtube"``),
    while a flat entry carries ``ie_key`` (``"Youtube"``) -- the extractor's
    class name rather than its ``IE_NAME``.  Case-folding both halves' first
    part is what makes them equal, and it is the same fold ``_source_id`` does.

    Returns None when either half is missing, which is better than an
    identifier that only looks like one.
    """
    extractor = entry.get("ie_key") or entry.get("extractor_key") or entry.get("extractor")
    source_id = entry.get("id")
    if not extractor or not source_id:
        return None
    return f"{str(extractor).lower()}:{source_id}"


def _unavailable_reason(entry: dict) -> str | None:
    """Why this row cannot be downloaded, in yt-dlp's words, or None.

    Only what the *flat* pass can see.  SoundCloud's DRM, in particular, is
    reported by the full extraction -- most of a label-signed artist's
    catalogue raises "This video is DRM protected" the moment yt-dlp asks for
    formats (enumeration research) -- and the flat listing happily includes
    those tracks.  So a row is marked unavailable when the flat pass says so
    and left available otherwise; a DRM track that slipped through fails in its
    child job, with the same message, where the user can see and dismiss it.
    """
    error = entry.get("error") or entry.get("_error")
    if isinstance(error, str) and error.strip():
        text = error.strip()
        return "DRM protected" if "drm" in text.casefold() else text
    if entry.get("has_drm") or entry.get("drm"):
        return "DRM protected"
    availability = entry.get("availability")
    if isinstance(availability, str) and availability in _UNAVAILABLE_AVAILABILITY:
        return _UNAVAILABLE_AVAILABILITY[availability]
    if entry.get("live_status") == "is_upcoming":
        return "not released yet"
    return None


def _entry_url(entry: dict) -> str | None:
    """The URL a child job would download for this entry."""
    url = entry.get("url") or entry.get("webpage_url") or entry.get("original_url")
    return url if isinstance(url, str) and url else None


def _thumbnail(entry: dict) -> str | None:
    """The best thumbnail a flat entry offers, if it offers any."""
    thumbnail = entry.get("thumbnail")
    if isinstance(thumbnail, str) and thumbnail:
        return thumbnail
    thumbnails = entry.get("thumbnails")
    if isinstance(thumbnails, list) and thumbnails:
        last = thumbnails[-1]
        if isinstance(last, dict):
            url = last.get("url")
            if isinstance(url, str) and url:
                return url
    return None


def _duration(entry: dict) -> float | None:
    value = entry.get("duration")
    return float(value) if isinstance(value, (int, float)) else None


def _text(value: object) -> str | None:
    """A yt-dlp field as a non-empty string, or None.

    Nothing in an info dict is guaranteed to be the type its name suggests --
    an extractor can hand back a number, a dict or a list where a title
    belongs -- and these values go straight into ``str | None`` response
    fields, where Pydantic would answer 500 rather than accept them.  The
    string is not coerced: a title that is not text is no title.
    """
    return value.strip() or None if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# Telling a track entry from a collection entry
# ---------------------------------------------------------------------------


def _looks_like_track(url: str) -> bool:
    """Whether *url* names one track, from its shape alone.

    Only asked of entries yt-dlp left unlabelled (Bandcamp gives an artist's
    entries as bare URLs with no ``ie_key`` at all), so the shapes listed here
    are the ones that actually turn up: Bandcamp ``/track/``, a YouTube watch
    URL, and a ``youtu.be`` short link.
    """
    lowered = url.casefold()
    if "/track/" in lowered:
        return True
    if "watch?v=" in lowered or "youtu.be/" in lowered:
        return True
    return False


def _is_subcollection(entry: dict) -> bool:
    """Whether this entry points at another collection worth one flat call.

    The four shapes the sources actually produce (enumeration research):
    YouTube ``/releases`` rows (``ie_key="YoutubeTab"``, a ``playlist?list=``
    URL), SoundCloud ``/sets/`` rows, Bandcamp ``/album/`` rows, and -- the
    catch-all -- an entry yt-dlp did not label at all whose URL is not a track,
    which is how Bandcamp's discography and SoundCloud's ``/albums`` page come
    back.
    """
    url = _entry_url(entry) or ""
    ie_key = str(entry.get("ie_key") or entry.get("extractor_key") or "")
    lowered = url.casefold()
    if ie_key == "YoutubeTab" or "playlist?list=" in lowered:
        return True
    if "/sets/" in lowered:
        return True
    if "/album/" in lowered:
        return True
    if not ie_key and url and not _looks_like_track(url):
        return True
    return False


def _album_for(info: dict, url: str) -> str | None:
    """The album name this collection gives its tracks, if it gives one.

    Grouping is only ever taken from a source that *says* "album"; a plain
    playlist or a channel tab is a bag of videos, and inventing an album from
    its title would file a hundred loose uploads under a record that does not
    exist.

    * YouTube: an ``OLAK5uy_`` playlist id is an auto-generated album playlist
      and exists only for distributed music, so its title is the album;
    * SoundCloud: a set carrying ``album``/``album_type`` is a release, and its
      ``album`` field is the name;
    * Bandcamp: an ``/album/`` page is an album by construction.
    """
    playlist_id = str(info.get("id") or "")
    if playlist_id.startswith(_OLAK_PREFIX):
        return _text(info.get("title"))
    album = info.get("album")
    if isinstance(album, str) and album.strip():
        return album.strip()
    if info.get("album_type") and info.get("title"):
        return str(info["title"])
    if "/album/" in url.casefold():
        return _text(info.get("title"))
    return None


def _source_of(info: dict, url: str) -> Source:
    """Which of the sources we say per-source things about this is."""
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").casefold()
    host = (urlsplit(url).hostname or "").casefold()
    if extractor.startswith("youtube") or "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if extractor.startswith("soundcloud") or "soundcloud.com" in host:
        return "soundcloud"
    if extractor.startswith("bandcamp") or "bandcamp.com" in host:
        return "bandcamp"
    return "other"


def entry_artist(entry: dict) -> str | None:
    """The artist one *entry* of a collection names, minus ``- Topic``.

    First non-empty of ``artist``/``channel``/``uploader``, which is the order
    of decreasing trust: ``artist`` is the release metadata where a source has
    any, and the other two are whoever uploaded it.
    """
    for key in ("artist", "channel", "uploader"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            if name.endswith(_TOPIC_SUFFIX):
                name = name[: -len(_TOPIC_SUFFIX)].strip()
            if name:
                return name
    return None


def _is_channel_id(playlist_id: str) -> bool:
    """Whether a YouTube list id names a *channel* rather than a playlist.

    Channel ids are ``UC…`` and handles are ``@name``; a playlist id is
    ``PL…``, ``OLAK5uy_…`` and friends.  Only channels get to keep a name that
    begins with "By ", because only a playlist page's channel field is the
    ``"by <Artist>"`` credit line yt-dlp lifts off the page.
    """
    return playlist_id.startswith("UC") or playlist_id.startswith("@")


def _top_level_artist(info: dict, source: Source) -> str | None:
    """The artist the collection's *own* metadata names, or None.

    For YouTube a plain playlist page has no channel of its own, so yt-dlp
    fills ``channel``/``uploader`` from the page's credit line -- literally
    ``"by Blender"``.  That prefix comes off, but only for a playlist: a
    channel really called "By The Rivers" must survive.
    """
    for key in ("artist", "album_artist", "channel", "uploader"):
        value = info.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        name = value.strip()
        if name.endswith(_TOPIC_SUFFIX):
            name = name[: -len(_TOPIC_SUFFIX)].strip()
        if source == "youtube" and not _is_channel_id(str(info.get("id") or "")):
            if name.startswith("by "):
                name = name[len("by ") :].strip()
        return name or None
    return None


def _suggested_artist(
    info: dict,
    url: str,
    source: Source,
    entry_artists: Counter[str] | None = None,
) -> str | None:
    """The artist the UI offers, from whatever the source called it.

    Every source names this differently and none of them names it well, so the
    answer is a suggestion the user edits before submitting -- it is never
    written to disk without passing through the form.

    * YouTube: the artist its *entries* agree on first, because the collection
      itself routinely does not have one -- an ``OLAK`` album playlist carries
      no channel at all (its title is the album), and a plain playlist carries
      the page's ``"by <Artist>"`` credit line.  The rows, meanwhile, each name
      the uploading channel.  Only when no entry names anybody does the
      collection's own ``artist``/``channel``/``uploader`` get used, minus the
      ``- Topic`` suffix and, for a playlist page, the ``by `` prefix;
    * SoundCloud: the user page's title is "<user> (All)"/"(Tracks)"/..., so
      the page suffix comes off;
    * Bandcamp: the title is "Discography of <subdomain>" and carries no
      display name at all, so the subdomain is the best there is.

    The title is the last resort everywhere except a YouTube ``OLAK`` playlist,
    whose title is the *album*: filing a whole record under an artist folder
    named after the record is worse than offering nothing.
    """
    if source == "youtube" and entry_artists:
        # Ties go to whichever name appeared first: Counter preserves insertion
        # order and ``most_common`` sorts stably.
        return entry_artists.most_common(1)[0][0]

    top_level = _top_level_artist(info, source)
    if top_level:
        return top_level

    title = info.get("title")
    if source == "youtube" and str(info.get("id") or "").startswith(_OLAK_PREFIX):
        return None
    if source == "soundcloud" and isinstance(title, str):
        for suffix in _SOUNDCLOUD_PAGE_SUFFIXES:
            if title.endswith(suffix):
                return title[: -len(suffix)].strip() or None
    if source == "bandcamp":
        host = (urlsplit(url).hostname or "").casefold()
        subdomain = host.split(".")[0]
        if subdomain and subdomain != "bandcamp":
            return subdomain
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


@dataclass
class _Walk:
    """Mutable bookkeeping for one enumeration."""

    deadline: float
    rows: list[EnumeratedTrack] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    unreadable: int = 0
    subcollections: int = 0
    notices: list[str] = field(default_factory=list)
    # How often each artist name appeared on an *entry*, expanded
    # sub-collections included.  ``_suggested_artist`` reads it: a collection
    # rarely names its artist well, and its rows nearly always do.
    entry_artists: Counter[str] = field(default_factory=Counter)

    def check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            raise ProbeTimeout("The probe took too long")

    def note_artist(self, entry: dict) -> None:
        """Count the artist this track entry names, if it names one."""
        name = entry_artist(entry)
        if name:
            self.entry_artists[name] += 1

    def add(self, row: EnumeratedTrack) -> None:
        # The same track can appear twice: SoundCloud's ``/sets`` page and its
        # ``/albums`` page overlap, and a YouTube channel lists a video in both
        # its Videos tab and an album playlist.  Keyed on the row id, which is
        # the source id where there is one.
        if row.id in self.seen:
            return
        self.seen.add(row.id)
        self.rows.append(row)
        if len(self.rows) > MAX_COLLECTION_TRACKS:
            raise CollectionTooLarge()


def _cap(value: str | None, limit: int) -> str | None:
    """*value* trimmed to *limit* characters, or None if nothing is left.

    A preview row travels back to ``POST /download/bulk`` verbatim, so a field
    longer than the matching :class:`~app.models.BulkTrack` bound would 422 the
    whole submission over one over-long album name.  Truncating here keeps the
    row lossy but usable; the child job resolves its own metadata anyway.
    """
    if value is None:
        return None
    return value[:limit].rstrip() or None


def _drop_if_over(value: str | None, limit: int) -> str | None:
    """*value* if it fits *limit*, None if it does not.

    For the fields where half a value is worse than none.  A truncated
    ``source_id`` can never equal the uncapped ``<extractor>:<id>``
    :func:`app.downloader._source_id` writes to ``SOURCEID``, so it would never
    dedup and, sharing a prefix with a sibling, would silently collapse two
    rows into one in :meth:`_Walk.add`; a truncated thumbnail URL sticks as a
    broken image, where ``None`` lets the child job fill in the real one.
    """
    if value is None:
        return None
    return value if len(value) <= limit else None


def _row_for(entry: dict, album: str | None) -> EnumeratedTrack | None:
    """Turn one flat track entry into a preview row, or None if it has no URL."""
    url = _entry_url(entry)
    if url is None:
        return None
    source_id = _drop_if_over(flat_source_id(entry), MAX_SOURCE_ID)
    entry_album = entry.get("album")
    return EnumeratedTrack(
        # The stable key the frontend and the dedup answer both use: the source
        # id where the source gave one, the URL otherwise (Bandcamp's artist
        # entries carry nothing else).
        id=source_id or url,
        url=url,
        source_id=source_id,
        title=_cap(_text(entry.get("title")), MAX_TRACK_TITLE),
        album=_cap(
            entry_album.strip()
            if isinstance(entry_album, str) and entry_album.strip()
            else album,
            MAX_FOLDER_NAME,
        ),
        duration=_duration(entry),
        thumbnail_url=_drop_if_over(_thumbnail(entry), MAX_PATH_LENGTH),
        unavailable_reason=_cap(_unavailable_reason(entry), MAX_REASON),
    )


def _walk(info: dict, url: str, album: str | None, walk: _Walk, depth: int) -> None:
    """Add every track under *info* to *walk*, expanding sub-collections once.

    *depth* is how many extractions deep this call already is.  Materialised
    nested playlists (a YouTube channel root's tabs) are walked without
    counting, because yt-dlp already paid for them; only an entry that needs a
    *new* extraction is bounded, at one level and at
    :data:`MAX_SUBCOLLECTIONS` calls.
    """
    entries = info.get("entries") or []
    for entry in entries:
        walk.check_deadline()
        if entry is None:
            # ``ignoreerrors`` turns an entry yt-dlp could not read into a
            # None.  There is no URL to offer and nothing to tick, so it is
            # counted for the notice rather than shown as a row.
            walk.unreadable += 1
            continue
        if not isinstance(entry, dict):  # pragma: no cover - defensive
            walk.unreadable += 1
            continue

        if is_collection(entry):
            # Already materialised by yt-dlp (channel tabs): free to descend.
            entry_url = _entry_url(entry) or url
            _walk(entry, entry_url, _album_for(entry, entry_url) or album, walk, depth)
            continue

        if depth < 1 and _is_subcollection(entry):
            entry_url = _entry_url(entry)
            if entry_url is None:
                walk.unreadable += 1
                continue
            if walk.subcollections >= MAX_SUBCOLLECTIONS:
                logger.warning(
                    "Probe stopped expanding sub-collections after %d; %s was "
                    "left out",
                    MAX_SUBCOLLECTIONS,
                    entry_url,
                )
                continue
            walk.subcollections += 1
            walk.check_deadline()
            try:
                sub = _extract(entry_url)
            except ProbeError as exc:
                logger.warning("Probe could not read %s: %s", entry_url, exc)
                walk.unreadable += 1
                continue
            if not is_collection(sub):
                # It resolved to a single track after all (a Bandcamp URL that
                # was neither an album nor a /track/ link).  Take it as a row.
                row = _row_for(sub, album)
                if row is not None:
                    walk.note_artist(sub)
                    walk.add(row)
                continue
            _walk(sub, entry_url, _album_for(sub, entry_url) or album, walk, depth + 1)
            continue

        row = _row_for(entry, album)
        if row is None:
            walk.unreadable += 1
            continue
        walk.note_artist(entry)
        walk.add(row)


def _enumerate(url: str, deadline: float) -> SingleTrack | Enumeration:
    """The whole blocking probe: one extraction, then the walk.

    Runs on a worker thread.  *deadline* is a ``time.monotonic()`` stamp, not a
    duration: ``asyncio.wait_for`` bounds the caller's *wait*, but the thread
    would carry on making yt-dlp calls long after that, so it checks the same
    deadline between every one of them -- including before the first one, since
    the caller may have given up while this call sat in the executor queue.
    """
    walk = _Walk(deadline=deadline)
    walk.check_deadline()
    info = _extract(url)

    if not is_collection(info):
        return SingleTrack(
            title=_text(info.get("title")),
            duration=_duration(info),
            thumbnail_url=_thumbnail(info),
            artist=_pick_track_artist(info),
            album=_text(info.get("album")),
        )

    source = _source_of(info, url)
    _walk(info, url, _album_for(info, url), walk, 0)

    notices: list[str] = []
    if source == "bandcamp":
        notices.append(BANDCAMP_NOTICE)
    if walk.unreadable == 1:
        notices.append("1 track could not be read and was left out")
    elif walk.unreadable:
        notices.append(
            f"{walk.unreadable} tracks could not be read and were left out"
        )

    enumeration = Enumeration(
        url=url,
        # Both travel back through ``CollectionPreview``, whose bounds match
        # these: a channel with a novel-length title, or a suggested artist
        # lifted from one, must not 500 the preview on the response model.
        title=_cap(_text(info.get("title")), MAX_TRACK_TITLE),
        artist=_cap(
            _suggested_artist(info, url, source, walk.entry_artists),
            MAX_FOLDER_NAME,
        ),
        source=source,
        rows=tuple(walk.rows),
        notices=tuple(notices),
    )
    logger.info(
        "Probed %s: %d row(s), %d sub-collection(s), %d unreadable",
        url,
        len(enumeration.rows),
        walk.subcollections,
        walk.unreadable,
    )
    return enumeration


def _pick_track_artist(info: dict) -> str | None:
    """The artist for a single-track answer, the same way the downloader picks."""
    artist = (
        info.get("artist")
        or info.get("creator")
        or info.get("channel")
        or info.get("uploader")
    )
    if isinstance(artist, str) and artist.endswith(_TOPIC_SUFFIX):
        artist = artist[: -len(_TOPIC_SUFFIX)]
    return _text(artist)


async def probe(url: str, timeout: int | None = None) -> SingleTrack | Enumeration:
    """Classify *url* and, for a collection, enumerate it.

    At most :data:`MAX_CONCURRENT_PROBES` of these run at once; the rest wait
    for a slot, so a handful of open tabs cannot fill the shared executor with
    minute-long yt-dlp calls.

    The extraction runs on the default executor -- it is yt-dlp making HTTP
    calls, and the event loop has SSE streams to serve -- and is bounded by
    ``PROBE_TIMEOUT_SECONDS`` on both sides: ``asyncio.wait_for`` for the
    request, and the same deadline inside the thread so an abandoned walk stops
    making calls instead of crawling a channel nobody is waiting for any more.

    A collection is answered from the in-process cache when one was enumerated
    for the same URL in the last :data:`CACHE_TTL_SECONDS`; a single track is
    never cached, because it costs one call and its metadata is what the form
    is about to show.

    Raises:
        ProbeError: The URL could not be read, or is a collection with nothing
            downloadable in it, or has more than
            :data:`~app.models.MAX_COLLECTION_TRACKS` tracks.
        ProbeTimeout: The probe ran past its deadline.
    """
    cached = _cache_get(url)
    if cached is not None:
        logger.info("Probe cache hit for %s (%d rows)", url, len(cached.rows))
        return cached

    seconds = timeout if timeout is not None else probe_timeout_seconds()
    loop = asyncio.get_running_loop()
    async with _probe_slots:
        # Inside the slot: the deadline is what this probe has to *work* with,
        # not how long it queued.
        deadline = time.monotonic() + seconds
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _enumerate, url, deadline),
                timeout=seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ProbeTimeout(
                f"Reading this URL took longer than {seconds} seconds"
            ) from exc

    if isinstance(result, Enumeration):
        if not result.rows or all(row.unavailable_reason for row in result.rows):
            raise EmptyCollection()
        _cache_put(url, result)
    return result
