"""Classify a pasted URL, and enumerate a collection behind it.

``POST /download/probe`` asks one question -- "is this one track or a list of
them?" -- and, when it is a list, has to come back with the checklist the user
picks from.  Both answers come from a *flat* yt-dlp extraction
(``extract_flat="in_playlist"``), which is the cheap pass: 1-6 s for an artist
page against the ~1-3 s **per track** a full extraction costs (source
enumeration research).  The consequence is that a row's metadata is thin, and
deliberately so: a child job resolves its own title, duration and thumbnail
when it runs, and the preview only has to be good enough to tick boxes in.

Two URL shapes do not go through yt-dlp at all.  A Spotify artist URL
(``open.spotify.com/artist/…``) is not a *source* -- yt-dlp cannot download
from Spotify and this app holds no Spotify credentials -- so it is read for the
artist's name alone (:mod:`app.spotify`), searched for on YouTube Music, and
enumerated from there, with a notice saying which artist it matched.  And a
YouTube channel
(``/channel/UC…``, ``/@handle``, ``music.youtube.com/browse/…``) is asked of
YouTube Music first, through :mod:`app.ytmusic`, because YouTube Music holds
the same artist as a *discography* -- albums, EPs and singles with track lists
-- where the channel holds uploads.  A channel YouTube Music does not know as
an artist falls through to the flat pass below, which is what everything else
does from the start.

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
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import urlsplit

import yt_dlp

from app.downloader import _YtDlpLogger, base_opts, ytdl
from app.models import (
    MAX_COLLECTION_TRACKS,
    MAX_CONCURRENT_PROBES,
    MAX_FOLDER_NAME,
    MAX_PATH_LENGTH,
    MAX_REASON,
    MAX_SOURCE_ID,
    MAX_SUBCOLLECTIONS,
    MAX_TRACK_TITLE,
)
from app.spotify import (
    UNSUPPORTED_KIND_MESSAGE,
    SpotifyUnavailable,
    is_spotify_url,
    resolve_artist_name,
    spotify_url_target,
)
from app.ytmusic import (
    NON_RELEASE_TABS,
    SINGLE_RELEASE_TYPE,
    YTMusicArtist,
    YouTubeMusicUnavailable,
    canonical_channel_url,
    channel_url_target,
    fetch_artist,
    resolve_channel_id,
    search_artist,
    source_id as ytmusic_source_id,
    watch_url,
)

logger = logging.getLogger(__name__)

DEFAULT_PROBE_TIMEOUT_SECONDS = 120

# :data:`~app.models.MAX_CONCURRENT_PROBES` slots.  Taken *before* the deadline
# is set: a probe that waited 30 s for a slot still gets its whole
# ``PROBE_TIMEOUT_SECONDS`` to do the work.
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

# Said when a channel URL was offered to YouTube Music and the request never
# got an answer.  The preview that follows is the flat yt-dlp listing, which
# for a music artist is the wrong shape -- clips and live sets rather than
# releases -- and the user can act on knowing why.
YTMUSIC_UNREACHABLE_NOTICE = (
    "YouTube Music could not be reached, so this channel's uploads were "
    "listed instead of its releases."
)

# Said when a Spotify artist URL was matched to a YouTube Music artist.  The
# match is the top search hit for the name Spotify gave, taken without a picker
# (Spotify URLs ticket), so the preview has to name what it matched and say
# that the two catalogues are not the same catalogue.
SPOTIFY_MATCH_NOTICE = (
    'Matched to the YouTube Music artist "{name}"; its discography may differ '
    "from the Spotify one. Edit the artist above if it is wrong."
)

# The three ways a Spotify artist URL fails, as sentences.  Each is the whole
# 400 body: there is no fallback to yt-dlp for a Spotify URL -- it has no
# extractor for one -- so an error here is the end of the probe, and it has to
# say what to do next.
SPOTIFY_NAME_MESSAGE = (
    "Could not read the artist name from Spotify. Check the link, or paste "
    "the artist's YouTube Music page instead."
)

SPOTIFY_NO_MATCH_MESSAGE = (
    "No YouTube Music artist matches '{name}'. Paste the artist's YouTube "
    "Music page instead."
)

SPOTIFY_YTMUSIC_UNAVAILABLE_MESSAGE = (
    "YouTube Music could not be reached, so '{name}' could not be looked up. "
    "Try again in a moment."
)

SPOTIFY_NOTHING_PLAYABLE_MESSAGE = (
    "The YouTube Music artist matching '{name}' has no tracks available to "
    "download."
)

SPOTIFY_UNREADABLE_MESSAGE = (
    "The YouTube Music discography for '{name}' could not be read. Try again "
    "in a moment, or paste the artist's YouTube Music page instead."
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


class SpotifyProbeError(ProbeError):
    """A Spotify URL could not become a preview, with a sentence saying why.

    Its own class because its message is written for the user and reaches them
    unchanged: the route prefixes a plain :class:`ProbeError` with "Failed to
    probe", which is yt-dlp's voice and wrong for a URL yt-dlp was never asked
    about.
    """


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

    ``album_final`` says the source read the *release* this track is on, so
    ``album`` is the whole answer and ``None`` means deliberately no album --
    a loose Single.  Only the YouTube Music pass can say that; the flat pass
    reads a listing that often carries no album at all, and its ``None`` means
    "not known", which is what leaves yt-dlp's own album in play when the
    child job runs.
    """

    id: str
    url: str
    source_id: str | None = None
    title: str | None = None
    album: str | None = None
    duration: float | None = None
    thumbnail_url: str | None = None
    unavailable_reason: str | None = None
    album_final: bool = False


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


def _unreadable_notice(count: int, noun: str) -> str | None:
    """"3 tracks could not be read and were left out", or None for zero."""
    if not count:
        return None
    plural = noun if count == 1 else f"{noun}s"
    verb = "could not be read and was" if count == 1 else "could not be read and were"
    return f"{count} {plural} {verb} left out"

def _tab_notice(tab: str | None) -> str | None:
    """"…the Videos tab was not enumerated", when a tab was asked for.

    A user who pasted ``/@artist/videos`` asked for that tab and is looking at
    a discography instead.  For a music artist that is the better answer --
    the Videos tab is where the clips and visualisers are -- but it is not the
    answer they asked for, so it is said out loud.  ``/releases`` and the bare
    channel root get nothing: those *are* the discography.
    """
    if tab is None or tab not in NON_RELEASE_TABS:
        return None
    return (
        f"Showing this artist's releases from YouTube Music; the "
        f"{tab.capitalize()} tab was not enumerated."
    )


def _unplayable_notice(count: int) -> str | None:
    """"2 tracks are listed on YouTube Music but are not available to download".

    Not the same thing as :func:`_unreadable_notice`: nothing failed.  YouTube
    Music lists an unreleased or withdrawn track by name with no video behind
    it at all, so there is nothing to tick and nothing to download.
    """
    if not count:
        return None
    if count == 1:
        return "1 track is listed on YouTube Music but is not available to download"
    return f"{count} tracks are listed on YouTube Music but are not available to download"


def _with_notices(enumeration: Enumeration, *notices: str | None) -> Enumeration:
    """*enumeration* with *notices* appended, skipping the Nones."""
    extra = tuple(notice for notice in notices if notice)
    if not extra:
        return enumeration
    return replace(enumeration, notices=enumeration.notices + extra)


def _enumeration_from_artist(
    artist: YTMusicArtist, url: str, walk: "_Walk"
) -> Enumeration | None:
    """*artist*'s discography as preview rows, or None if none can be played.

    The one conversion from :class:`~app.ytmusic.YTMusicArtist` to
    :class:`Enumeration`, shared by the two ways an artist is reached: a
    channel URL (:func:`_ytmusic_enumeration`) and a Spotify artist URL matched
    to one (:func:`_spotify_enumeration`).  It caches nothing and logs nothing
    -- the callers differ on both, because a channel that comes back empty
    falls through to yt-dlp while a Spotify URL has nowhere to fall.

    None means "there is nothing here to tick": no rows at all, or rows that
    are every one of them unavailable.  The second is the same answer as the
    first -- :func:`probe` would raise ``EmptyCollection`` on such a preview --
    and it is better said here, where the caller can still do something else.

    *walk* carries the deadline and the row cap; it is the caller's, and is
    expected to be a fresh one, so that rows added here are never counted
    against a fallback listing.
    """
    # Tracks YouTube Music lists with no video behind them.  Counted apart
    # from ``walk.unreadable`` -- which is a failure -- because this one is
    # the catalogue being honest about an unreleased track.
    unplayable = 0
    for release in artist.releases:
        walk.check_deadline()
        # A Single gets no album: its tracks become loose Singles under the
        # artist rather than a folder each (see SINGLE_RELEASE_TYPE).
        album = (
            None
            if release.release_type == SINGLE_RELEASE_TYPE
            else _cap(release.title, MAX_FOLDER_NAME)
        )
        for track in release.tracks:
            if track.video_id is None:
                unplayable += 1
                continue
            track_url = watch_url(track.video_id)
            row_source_id = _drop_if_over(ytmusic_source_id(track.video_id), MAX_SOURCE_ID)
            walk.add(
                EnumeratedTrack(
                    id=row_source_id or track_url,
                    url=track_url,
                    source_id=row_source_id,
                    title=_cap(track.title, MAX_TRACK_TITLE),
                    album=album,
                    duration=track.duration,
                    thumbnail_url=_drop_if_over(release.thumbnail_url, MAX_PATH_LENGTH),
                    unavailable_reason=(
                        None if track.available else _cap("not available", MAX_REASON)
                    ),
                    # The release was read: a null album here is a Single with
                    # no album, not an album nobody knew.
                    album_final=True,
                )
            )

    if not walk.rows or all(row.unavailable_reason for row in walk.rows):
        return None

    notices = [
        notice
        for notice in (
            _unreadable_notice(artist.unreadable_releases, "release"),
            _unplayable_notice(unplayable),
            (
                f"Only the first {MAX_SUBCOLLECTIONS} releases were read; use "
                "a narrower URL."
                if artist.over_cap
                else None
            ),
        )
        if notice
    ]
    return Enumeration(
        url=url,
        title=_cap(artist.name, MAX_TRACK_TITLE),
        artist=_cap(artist.name, MAX_FOLDER_NAME),
        source="youtube",
        rows=tuple(walk.rows),
        notices=tuple(notices),
    )


def _ytmusic_enumeration(
    url: str, deadline: float
) -> tuple[Enumeration | None, tuple[str, ...]]:
    """*url*'s discography from YouTube Music, and notices for the flat pass.

    The first half is None for every URL that is not a channel, for a channel
    whose handle could not be resolved, and -- the case that matters -- for a
    channel YouTube Music does not know as an artist: a podcast, a label's
    talking-head uploads, anything with no releases behind it.  The probe then
    carries on exactly as it did before this existed, so the fallback is the
    old behaviour rather than an error.

    The second half is what the flat pass should say about having been used.
    It is empty for all of the above -- uploads are the right answer for a
    channel that is not an artist -- and carries a notice when YouTube Music
    could not be *reached*, because then the listing is uploads for a reason
    the user can act on (try again) rather than because that is what the
    channel holds.

    The enumeration is cached a second time, under the *canonical* channel
    URL, because ``/@artist``, ``/@artist/videos`` and ``/channel/UC…`` are one
    discography with three spellings and only the first of them should pay for
    it.  ``Enumeration.url`` stays the URL the user pasted: it is what the bulk
    submit sends back and what the parent job shows.  For the same reason the
    tab notice is added *after* the cache, never into it: the cached
    enumeration is shared by all three spellings and only one of them asked
    for a tab.
    """
    target = channel_url_target(url)
    if target is None:
        return None, ()
    tab_notice = _tab_notice(target.tab)
    # Its own walk: the flat pass gets a clean one if this returns None, so a
    # track skipped here can never be counted twice in the fallback's notice.
    walk = _Walk(deadline=deadline)
    channel_id = resolve_channel_id(target)
    if channel_id is None:
        return None, ()

    walk.check_deadline()
    canonical = canonical_channel_url(channel_id)
    cached = _cache_get(canonical)
    if cached is not None:
        logger.info("Probe cache hit for %s via %s", url, canonical)
        return _with_notices(replace(cached, url=url), tab_notice), ()

    try:
        artist = fetch_artist(channel_id, check_deadline=walk.check_deadline)
    except YouTubeMusicUnavailable as exc:
        logger.warning(
            "YouTube Music is unreachable (%s); falling back to the flat "
            "listing for %s",
            exc,
            url,
        )
        return None, (YTMUSIC_UNREACHABLE_NOTICE,)
    except (ProbeTimeout, ProbeError):
        # The deadline (and the row cap, which ``fetch_artist`` cannot raise
        # but ``walk.check_deadline`` shares a path with) are this probe's own
        # answers, not YouTube Music's: they must reach the route.
        raise
    except Exception:
        # Last resort, around this call only.  ``ytmusicapi`` parses YouTube's
        # internal JSON, and the shapes it does not expect surface as whatever
        # the library happens to raise; the flat listing is a worse preview
        # than the discography but an infinitely better answer than a 500.
        logger.exception(
            "Reading %s's discography from YouTube Music failed unexpectedly; "
            "falling back to the flat listing for %s",
            channel_id,
            url,
        )
        return None, ()
    if artist is None:
        return None, ()

    enumeration = _enumeration_from_artist(artist, url, walk)
    if enumeration is None:
        # A parsed artist page with nothing playable behind it.  yt-dlp may
        # still find uploads on the channel, so this is a fallback rather than
        # an empty preview.
        logger.info("YouTube Music had no playable tracks for %s; using yt-dlp", url)
        return None, ()
    _cache_put(canonical, enumeration)
    if artist.channel_id and artist.channel_id != channel_id:
        # ``get_artist`` takes either id -- the one in the pasted URL or the
        # one on the page's subscribe button -- and the page answers with the
        # latter, which is also the id yt-dlp resolves a ``/@handle`` to.
        # Remembering both spellings is what makes a later handle probe free.
        _cache_put(canonical_channel_url(artist.channel_id), enumeration)
    logger.info(
        "Probed %s through YouTube Music: %d row(s) over %d release(s), "
        "%d unreadable release(s)",
        url,
        len(enumeration.rows),
        len(artist.releases),
        artist.unreadable_releases,
    )
    return _with_notices(enumeration, tab_notice), ()


def _spotify_enumeration(url: str, deadline: float) -> Enumeration:
    """*url*'s artist, matched to a YouTube Music discography.

    Spotify is not a source -- yt-dlp has no extractor for it and this app
    holds no Spotify credentials -- so a Spotify URL is a *name lookup*: the
    public page gives up the artist's display name (:mod:`app.spotify`), the
    artist-filtered YouTube Music search turns that into a channel id, and the
    discography behind it is the preview.  ``Enumeration.artist`` is the name
    Spotify gave, because that is the artist folder the user came here to fill
    and the field they are about to edit; the notice names the YouTube Music
    artist that was actually enumerated, since the two catalogues are not the
    same catalogue.

    Every failure raises: there is nothing to fall back *to*.  A flat pass over
    a Spotify URL would be refused by the extractor allowlist and, if it were
    not, would say something in yt-dlp's voice about a site it cannot read.

    The enumeration is cached twice, exactly as the channel branch caches:
    under the canonical channel URL(s) -- *without* the Spotify name or notice,
    so a later probe of the YouTube Music page gets the plain discography and
    not somebody else's Spotify framing -- and under the *canonical* Spotify
    URL with both, so the same artist pasted with another ``?si=`` or a locale
    prefix is served without a second lookup.  As in the channel branch, the
    entry stored under the channel keys carries whatever ``url`` was pasted,
    and every reader re-stamps it -- :func:`probe`, :func:`_ytmusic_enumeration`
    and this function -- so the URL a preview comes back with is always the one
    the user is looking at.

    Raises:
        SpotifyProbeError: The URL is not a Spotify artist page, its name could
            not be read, no YouTube Music artist matched it, or the artist that
            did has nothing playable.
        ProbeTimeout: The probe ran past its deadline.
    """
    target = spotify_url_target(url)
    if target is None or not target.is_artist:
        # Includes the URL shapes this module cannot parse at all: they are
        # still Spotify URLs, and "here is what is supported" is the only
        # useful thing to say about any of them.
        raise SpotifyProbeError(UNSUPPORTED_KIND_MESSAGE)

    # Under the *canonical* Spotify URL, because the same artist pasted with
    # another ``?si=`` or a locale prefix is the same artist and should not pay
    # for the lookup twice.  This entry carries the Spotify framing -- the
    # Spotify name and the match notice -- while the plain discography stays on
    # the canonical channel keys, so a later probe of the YouTube Music page
    # still gets it unframed.
    cached_view = _cache_get(target.canonical_url)
    if cached_view is not None:
        logger.info("Probe cache hit for %s via %s", url, target.canonical_url)
        return replace(cached_view, url=url)

    walk = _Walk(deadline=deadline)
    walk.check_deadline()
    try:
        name = resolve_artist_name(target, check_deadline=walk.check_deadline)
    except SpotifyUnavailable as exc:
        logger.warning("Could not reach Spotify for %s: %s", url, exc)
        raise SpotifyProbeError(SPOTIFY_NAME_MESSAGE) from exc
    if name is None:
        logger.info("Spotify gave no artist name for %s", url)
        raise SpotifyProbeError(SPOTIFY_NAME_MESSAGE)
    # Capped once, here, rather than only on the folder it ends up as: a page
    # title is bounded by nothing but the response byte cap, and the
    # folder-name cap is the bound every other artist string in this module
    # honours.  It has to apply to the search query and to the error sentences
    # below as much as to the folder.
    name = _cap(name, MAX_FOLDER_NAME)

    walk.check_deadline()
    try:
        match = search_artist(name, check_deadline=walk.check_deadline)
        if match is None:
            raise SpotifyProbeError(SPOTIFY_NO_MATCH_MESSAGE.format(name=name))
        channel_id, matched_name = match

        canonical = canonical_channel_url(channel_id)
        enumeration = _cache_get(canonical)
        if enumeration is not None:
            logger.info("Probe cache hit for %s via %s", url, canonical)
        else:
            artist = fetch_artist(channel_id, check_deadline=walk.check_deadline)
            if artist is None:
                # The search called it an artist and ``get_artist`` disagreed;
                # from here that is the same answer as no match at all.
                raise SpotifyProbeError(SPOTIFY_NO_MATCH_MESSAGE.format(name=name))
            enumeration = _enumeration_from_artist(artist, url, walk)
            if enumeration is None:
                raise SpotifyProbeError(
                    SPOTIFY_NOTHING_PLAYABLE_MESSAGE.format(name=name)
                )
            _cache_put(canonical, enumeration)
            if artist.channel_id and artist.channel_id != channel_id:
                _cache_put(canonical_channel_url(artist.channel_id), enumeration)
    except YouTubeMusicUnavailable as exc:
        logger.warning("YouTube Music is unreachable (%s); %s cannot be matched", exc, url)
        raise SpotifyProbeError(
            SPOTIFY_YTMUSIC_UNAVAILABLE_MESSAGE.format(name=name)
        ) from exc
    except (ProbeTimeout, ProbeError):
        # The deadline, the row cap and this function's own errors are answers
        # in their own right and must reach the route unchanged.
        raise
    except Exception as exc:
        # Last resort, the same one the channel branch takes: ``ytmusicapi``
        # parses YouTube's internal JSON and the shapes it does not expect
        # surface as whatever the library happens to raise.  There is no flat
        # listing to fall back to here, so it becomes a 400 rather than a 500.
        logger.exception("Matching %s to a YouTube Music artist failed unexpectedly", url)
        # Its own sentence, not the no-match one: an artist *was* found and the
        # library then fell over, so telling the user nothing matched their
        # name would be a lie they cannot act on.
        raise SpotifyProbeError(SPOTIFY_UNREADABLE_MESSAGE.format(name=name)) from exc

    view = _with_notices(
        replace(
            enumeration,
            url=url,
            artist=name,
        ),
        SPOTIFY_MATCH_NOTICE.format(name=enumeration.artist or matched_name),
    )
    # Under the canonical Spotify URL as well as the canonical channel one:
    # ``probe`` caches what it returns under the raw paste, and this is the
    # entry that carries the Spotify framing for every spelling of the paste.
    _cache_put(target.canonical_url, view)
    logger.info(
        "Probed %s through Spotify: %r matched %r (%s), %d row(s)",
        url,
        name,
        matched_name,
        channel_id,
        len(view.rows),
    )
    return view


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

    # A Spotify URL never reaches yt-dlp: there is no extractor for one, and
    # what a Spotify artist page is good for is its *name*, which YouTube Music
    # is then searched for.  Raises rather than falling through, because a flat
    # pass over the same URL has nothing to offer.
    if is_spotify_url(url):
        return _spotify_enumeration(url, deadline)

    # YouTube Music first for a channel URL: it knows the same artist as a
    # discography rather than as a pile of uploads.  Anything else -- and any
    # channel it does not hold -- falls through to the flat pass unchanged,
    # carrying whatever the attempt has to say for itself.
    from_ytmusic, ytmusic_notices = _ytmusic_enumeration(url, deadline)
    if from_ytmusic is not None:
        return from_ytmusic

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

    notices: list[str] = list(ytmusic_notices)
    if source == "bandcamp":
        notices.append(BANDCAMP_NOTICE)
    unreadable = _unreadable_notice(walk.unreadable, "track")
    if unreadable:
        notices.append(unreadable)

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
        # The entry may have been stored under a channel's canonical URL by a
        # probe of a different spelling of it, so the URL is re-stamped: it is
        # what the bulk submit sends back, and it has to be the one the user
        # is looking at.
        return replace(cached, url=url)

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
