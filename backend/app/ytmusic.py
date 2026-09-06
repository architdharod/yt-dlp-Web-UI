"""Read a YouTube Music artist's discography, for the collection probe.

A YouTube channel enumerated through the flat yt-dlp pass (``app.probe``) is a
bag of *uploads*: the Videos tab, the Shorts tab, live sets, visualisers, and
-- somewhere in there -- the actual records.  YouTube Music knows the same
artist as a discography instead, and answers with it for free: keyless
``ytmusicapi`` returns the albums, EPs and singles, each with its track list,
its durations and its availability.  That is the preview the bulk flow wants,
so a channel URL is asked of YouTube Music first and only falls back to the
flat pass when the channel turns out not to be a music artist at all -- or
when YouTube Music could not be reached, which is a different answer
(:class:`YouTubeMusicUnavailable`) because the preview should say so.

The module is deliberately ignorant of the probe: it hands back plain
dataclasses (:class:`YTMusicArtist` and friends) and ``app.probe`` turns them
into preview rows, so the two can be tested apart and neither imports the
other.

Four things happen here:

* :func:`channel_url_target` recognises the URL shapes, by parsing alone.  A
  ``/channel/UC…`` (on ``youtube.com`` or ``music.youtube.com``) and a
  ``music.youtube.com/browse/…`` carry the channel id already; a ``/@handle``,
  ``/c/…`` or ``/user/…`` does not;
* :func:`resolve_channel_id` turns one of those handles into its ``UC…`` id
  with a single flat yt-dlp call that fetches *no* entries (``playlist_items``
  is ``"0"``): the channel page's own metadata is all that is wanted;
* :func:`fetch_artist` reads the discography.  ``get_artist`` gives the album
  and single *sections*; a section that carries ``params`` has a continuation
  behind it (the full discography rather than the ten YouTube Music chose to
  show), and each release then costs one ``get_album`` call for its tracks.
  Those run on a small thread pool, because they are independent HTTP round
  trips and a well-catalogued artist has fifty of them.  Releases come back
  albums first and EPs ahead of Singles, because one recording is often on two
  of them and the probe keeps whichever it saw first.

* :func:`search_artist` goes the other way, from a *name* to a channel id,
  for the Spotify hand-off: a Spotify artist page gives up its display name and
  nothing else, and the artist-filtered search answers with the ``UC…`` id
  :func:`fetch_artist` then reads.

Everything here is blocking and is called from the probe's worker thread.
"""

import functools
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

import requests
import yt_dlp
import ytmusicapi
from requests.adapters import HTTPAdapter
from ytmusicapi.exceptions import YTMusicError

from app.downloader import base_opts, ytdl
from app.models import MAX_CONCURRENT_PROBES, MAX_SUBCOLLECTIONS

logger = logging.getLogger(__name__)

# How many ``get_album`` calls may be in flight at once.  Each is one HTTPS
# round trip that spends its time waiting, and an artist with a long catalogue
# needs fifty of them; six keeps a big discography inside the "a few seconds"
# the phase asks for without hammering an API we are using without a key.
MAX_ALBUM_WORKERS = 6

# How long one ``ytmusicapi`` HTTP call may take.  This is the timeout the
# library puts on a session it builds itself; a session we hand it is left
# exactly as we hand it over (``YTMusic._prepare_session`` returns a
# caller-supplied session unmodified), so :func:`shared_client` has to apply it
# again or every call would block forever on a hung socket.
REQUEST_TIMEOUT_SECONDS = 30

# How many rows :func:`search_artist` asks for.  Only the first is ever read
# -- there is no picker -- and ``ytmusicapi`` pages the search with
# continuation requests until it has ``limit`` results, so asking for one is
# both all we need and the cheapest thing to ask for.
ARTIST_SEARCH_LIMIT = 1

# A channel id: ``UC`` and 22 more characters of base64url.  Matched rather
# than assumed so a ``/channel/`` path that is not one -- or a crafted URL
# aimed at ``get_artist`` -- is simply not treated as a channel at all.
_CHANNEL_ID = re.compile(r"^UC[0-9A-Za-z_-]{22}$")

# YouTube Music's browse ids for an artist page prefix the channel id.
_BROWSE_PREFIX = "MPLA"

# The hosts a channel URL may be on.  ``music.youtube.com`` is the YouTube
# Music front end; the rest are YouTube proper.
_YOUTUBE_HOSTS = ("youtube.com", "music.youtube.com")

# Channel tabs that are not the discography.  A user who pasted one of these
# asked for *those* uploads and gets the releases instead, which is the right
# answer for a music artist but has to be said out loud; the probe turns
# :attr:`ChannelTarget.tab` into that notice.
NON_RELEASE_TABS = frozenset(
    {"videos", "shorts", "streams", "live", "community", "podcasts", "playlists"}
)

# Tab segments a channel URL may end with.  A user copies the page they are
# looking at, which is rarely the bare channel root; anything *not* in here is
# left alone, so a URL shape this module does not understand falls through to
# the flat pass rather than being resolved to the wrong thing.
_CHANNEL_TABS = NON_RELEASE_TABS | {
    "",
    "featured",
    "releases",
    "about",
    "music",
    "songs",
    "albums",
}

# The sections of ``get_artist`` that hold *releases*.  ``videos`` is left out
# on purpose: it is the uploads -- clips, visualisers, live sets -- which is
# exactly what enumerating through YouTube Music exists to avoid.
_RELEASE_SECTIONS = ("albums", "singles")

# What a release without an album folder is.  YouTube Music types every
# release Album, EP or Single; a Single is one track (occasionally a b-side or
# two) and giving it a folder of its own would file half a discography under
# one-track albums, so its tracks become loose Singles under the artist, which
# is what the domain model calls an album-less track.
SINGLE_RELEASE_TYPE = "Single"

# The type of release that gets a folder of its own but is not an album.  An
# EP outranks a Single in :func:`_rank` so that a track on both lands under
# the EP.
EP_RELEASE_TYPE = "EP"

# The type of a full-length release.  Only named here for the ranking: an
# album needs no special handling anywhere else.
ALBUM_RELEASE_TYPE = "Album"

# What a release is worth, for the first-wins ordering: an album beats an EP
# beats a Single, because a recording on two of them should be filed under the
# fuller one.  A release whose type YouTube Music did not give ranks with the
# albums -- it has a title and a track list and nothing says it is a Single, so
# it gets a folder, and that folder is the better home.
_RELEASE_RANK = {ALBUM_RELEASE_TYPE: 0, EP_RELEASE_TYPE: 1, SINGLE_RELEASE_TYPE: 2}

# What a *parse* failure looks like.  The library walks YouTube's internal
# JSON, so a channel that is not a music artist -- and a renderer YouTube
# changed this week -- surface as a plain ``KeyError`` rather than as anything
# typed.  This is the "not an artist" answer: ask yt-dlp instead.
_NOT_AN_ARTIST = (KeyError, IndexError, TypeError, ValueError, AttributeError)

# What a *transport* failure looks like: the request never got an answer, or
# got one that was not JSON.  Nothing was learned about the channel, so the
# fallback has to say so rather than imply YouTube Music was asked and
# shrugged.  ``json.JSONDecodeError`` is a ``ValueError``, so every ``except``
# below takes this tuple first.
_UNREACHABLE = (YTMusicError, requests.RequestException, json.JSONDecodeError)

# For the calls where either kind is survivable and the answer is "carry on
# with what we have": a release whose track list would not load, a
# continuation that would not page.
_YTMUSIC_FAILURES = _UNREACHABLE + _NOT_AN_ARTIST


class YouTubeMusicUnavailable(Exception):
    """YouTube Music could not be reached, so nothing was learned.

    Distinct from :func:`fetch_artist` returning None, which means the answer
    came back and said "this channel is not a music artist".  The probe
    falls back to yt-dlp for both, but only this one is worth telling the user
    about: the listing they are looking at is uploads because the discography
    could not be read, not because there is none.
    """


class YouTubeMusicClient(Protocol):
    """The four ``ytmusicapi`` calls this module makes.

    Declared so tests can pass a stand-in and never touch the network; the
    real implementation is :class:`ytmusicapi.YTMusic`.
    """

    def get_artist(self, channelId: str) -> dict[str, Any]: ...

    def get_artist_albums(
        self, channelId: str, params: str, limit: int | None = 100
    ) -> list[dict[str, Any]]: ...

    def get_album(self, browseId: str) -> dict[str, Any]: ...

    def search(
        self, query: str, filter: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]: ...


_client: YouTubeMusicClient | None = None
_client_lock = threading.Lock()


def _session() -> requests.Session:
    """The HTTPS session every ``ytmusicapi`` call in this process shares.

    Two things the library's own session does not do for us:

    * a connection pool wide enough for the concurrency we actually run.  Up
      to :data:`~app.models.MAX_CONCURRENT_PROBES` probes each drive
      :data:`MAX_ALBUM_WORKERS` ``get_album`` calls, and urllib3's default
      pool of ten would discard the connections over that and re-handshake TLS
      for each one (with a warning per call);
    * the request timeout.  ``YTMusic`` only applies its own to a session it
      built, so ours would have none at all and a hung socket would hold a
      probe thread past its deadline forever.
    """
    session = requests.Session()
    session.mount(
        "https://", HTTPAdapter(pool_maxsize=MAX_ALBUM_WORKERS * MAX_CONCURRENT_PROBES)
    )
    session.request = functools.partial(  # type: ignore[method-assign]
        session.request, timeout=REQUEST_TIMEOUT_SECONDS
    )
    return session


def shared_client() -> YouTubeMusicClient:
    """The one ``YTMusic`` this process uses, built on first need.

    It holds a ``requests`` session and the visitor id YouTube hands out, so
    building one per probe would pay for both every time.  ``language="en"``
    fixes the section headings ``ytmusicapi`` parses on, which are localised
    and would otherwise depend on where the container sits.

    The visitor id is fetched lazily, on the first read of ``base_headers``;
    it is warmed here, under the lock, so that two probes starting together
    cannot each make that request.  The warm is best-effort: ``base_headers``
    is a ``cached_property``, so a failed read caches nothing and the next call
    that needs it simply tries again.  The client is therefore kept either way
    -- rebuilding it on every probe because one warm request failed would cost
    a fresh session and a fresh visitor-id attempt each time.
    """
    global _client
    with _client_lock:
        if _client is None:
            client = ytmusicapi.YTMusic(language="en", requests_session=_session())
            try:
                # One extra HTTP call, once per process, rather than a race
                # between the first two probes to make it.
                getattr(client, "base_headers", None)
            except _UNREACHABLE as exc:
                logger.info(
                    "Could not warm the YouTube Music visitor id (%s: %s); it "
                    "will be fetched on the first call that needs it",
                    type(exc).__name__,
                    exc,
                )
            _client = client
        return _client


@dataclass(frozen=True)
class ChannelTarget:
    """A URL recognised as a YouTube channel.

    ``channel_id`` is set when the URL carried it; otherwise ``root_url`` is
    the channel page to ask yt-dlp about.  ``tab`` is the tab segment that was
    stripped off on the way (``"videos"``, ``"releases"``, …), or None when the
    URL named the channel root: the enumeration is the same either way, but a
    caller that asked for the Videos tab deserves to be told it did not get
    one.
    """

    channel_id: str | None
    root_url: str
    tab: str | None = None


@dataclass(frozen=True)
class YTMusicTrack:
    """One track of a release, as YouTube Music lists it."""

    video_id: str | None
    title: str | None
    duration: float | None
    available: bool


@dataclass(frozen=True)
class YTMusicRelease:
    """One album, EP or single, with its tracks."""

    title: str | None
    release_type: str | None
    thumbnail_url: str | None
    tracks: tuple[YTMusicTrack, ...]


@dataclass(frozen=True)
class YTMusicArtist:
    """An artist's discography: every release, in the order YouTube gave them.

    ``unreadable_releases`` counts the releases whose track list could not be
    fetched, so the preview can say so rather than quietly showing a short
    discography.  ``over_cap`` says the discography was longer than
    :data:`~app.models.MAX_SUBCOLLECTIONS` and was cut off, which is the same
    thing for a different reason.
    """

    name: str | None
    channel_id: str | None
    releases: tuple[YTMusicRelease, ...]
    unreadable_releases: int = 0
    over_cap: bool = False


# ---------------------------------------------------------------------------
# URL recognition
# ---------------------------------------------------------------------------


def channel_url_target(url: str) -> ChannelTarget | None:
    """Whether *url* names a YouTube channel, and what is known about it.

    Parsing only -- no network.  Returns ``None`` for every other YouTube URL
    (a watch link, a playlist, a Music album page), which is what makes the
    probe fall through to the flat pass unchanged.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").casefold()
    if not any(host == allowed or host.endswith("." + allowed) for allowed in _YOUTUBE_HOSTS):
        return None

    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments:
        return None
    head, rest = segments[0], segments[1:]

    if head in ("channel", "browse"):
        if not rest:
            return None
        channel_id = _channel_id(rest[0])
        if channel_id is None:
            return None
        known, tab = _tab(rest[1:])
        if not known:
            return None
        return ChannelTarget(
            channel_id=channel_id, root_url=_channel_url(channel_id), tab=tab
        )

    if head.startswith("@") and len(head) > 1:
        name = head
    elif head in ("c", "user") and rest:
        name, rest = f"{head}/{rest[0]}", rest[1:]
    else:
        return None

    known, tab = _tab(rest)
    if not known:
        return None
    # Always resolved against youtube.com: music.youtube.com has no handle
    # URLs of its own, and the flat extractor knows the YouTube channel page.
    return ChannelTarget(
        channel_id=None, root_url=f"https://www.youtube.com/{name}", tab=tab
    )


def _tab(rest: list[str]) -> tuple[bool, str | None]:
    """Whether what follows the channel is at most one known tab, and which.

    The two halves are separate answers: "no tab" (the channel root, or a
    trailing slash) is a channel URL with nothing to say about it, while "not a
    known tab" is not a channel URL at all and falls through to the flat pass.
    """
    if not rest:
        return True, None
    if len(rest) > 1:
        return False, None
    tab = rest[0].casefold()
    if tab not in _CHANNEL_TABS:
        return False, None
    return True, tab or None


def _channel_id(segment: str) -> str | None:
    """*segment* as a channel id, ``MPLA`` prefix and all, or None."""
    candidate = segment[len(_BROWSE_PREFIX) :] if segment.startswith(_BROWSE_PREFIX) else segment
    return candidate if _CHANNEL_ID.match(candidate) else None


def canonical_channel_url(channel_id: str) -> str:
    """The one URL an artist's enumeration is remembered under.

    ``/@handle``, ``/@handle/videos`` and ``/channel/UC…`` are the same
    discography; keying the probe's cache on this means the second of them is
    free even though the user pasted a different string.
    """
    return _channel_url(channel_id)


def _channel_url(channel_id: str) -> str:
    return f"https://music.youtube.com/channel/{channel_id}"


def watch_url(video_id: str) -> str:
    """The URL a child job downloads for *video_id*.

    ``music.youtube.com`` rather than ``www.youtube.com``: yt-dlp's YouTube
    extractor recognises both, and the Music host makes it pick the
    ``web_music`` client, which is the one that has the track.
    """
    return f"https://music.youtube.com/watch?v={video_id}"


def source_id(video_id: str) -> str:
    """The ``<extractor>:<id>`` provenance string a download of *video_id* gets.

    Must equal what :func:`app.downloader._source_id` writes into ``SOURCEID``
    -- yt-dlp's ``extractor`` for a Music watch URL is still ``youtube`` --
    because that string is what the dedup pass matches a preview row on.
    """
    return f"youtube:{video_id}"


# ---------------------------------------------------------------------------
# Resolving a handle
# ---------------------------------------------------------------------------


def resolve_channel_id(target: ChannelTarget) -> str | None:
    """The ``UC…`` id behind *target*, or None if it cannot be had.

    Costs nothing when the URL carried the id.  Otherwise it is one flat
    extraction of the channel root with ``playlist_items="0"``: yt-dlp reads
    the page's metadata, from which ``channel_id`` comes, and fetches not one
    entry.  ``base_opts`` is shared with the downloader so the extractor
    allowlist is the same one everything else runs under.
    """
    if target.channel_id is not None:
        return target.channel_id
    opts = {
        **base_opts(),
        "extract_flat": True,
        "skip_download": True,
        "ignoreerrors": True,
        "extractor_retries": 1,
        "playlist_items": "0",
    }
    try:
        with ytdl(opts) as ydl:
            info = ydl.extract_info(target.root_url, download=False)
    except (yt_dlp.utils.YoutubeDLError, OSError) as exc:
        logger.info("Could not resolve %s to a channel id: %s", target.root_url, exc)
        return None
    if not isinstance(info, dict):
        return None
    for key in ("channel_id", "uploader_id", "id"):
        value = info.get(key)
        if isinstance(value, str):
            channel_id = _channel_id(value)
            if channel_id is not None:
                return channel_id
    return None


# ---------------------------------------------------------------------------
# Reading the discography
# ---------------------------------------------------------------------------


def fetch_artist(
    channel_id: str,
    *,
    client: YouTubeMusicClient | None = None,
    check_deadline: Callable[[], None] | None = None,
) -> YTMusicArtist | None:
    """*channel_id*'s albums, EPs and singles, or None if it is not an artist.

    ``None`` is the "ask yt-dlp instead" answer and covers both ways a channel
    turns out not to be an artist: ``get_artist`` failing to parse -- a channel
    that is not in YouTube Music has no ``musicImmersiveHeaderRenderer`` and
    comes back as a bare ``KeyError`` -- and a page that parsed but holds no
    releases, which is what a podcast or a talking-head channel looks like from
    here.

    Raises:
        YouTubeMusicUnavailable: If the request never got an answer.  The
            caller falls back to yt-dlp for this too, but it is a different
            thing from "not an artist" and the preview says so.

    *check_deadline* is the probe's own deadline check, called between rounds
    of HTTP calls so an abandoned probe stops fetching.  It raises to stop the
    walk, and that exception is deliberately not caught here.
    """
    _check(check_deadline)
    try:
        # Inside the try: building the client is itself a network act (the
        # visitor id), so its transport failures are the same answer as
        # ``get_artist``'s -- nothing was learned about the channel.
        ytmusic = client if client is not None else shared_client()
        artist = ytmusic.get_artist(channel_id)
    except _UNREACHABLE as exc:
        # Not logged here: the probe logs the fallback it makes of this, and
        # one event should not be two warnings.
        raise YouTubeMusicUnavailable(f"{type(exc).__name__}: {exc}") from exc
    except _NOT_AN_ARTIST as exc:
        logger.info("%s is not a YouTube Music artist: %s: %s", channel_id, type(exc).__name__, exc)
        return None
    if not isinstance(artist, dict):
        return None

    entries, over_cap = _release_entries(ytmusic, artist, check_deadline)
    if not entries:
        logger.info("%s has no albums or singles on YouTube Music", channel_id)
        return None

    releases, unreadable = _fetch_releases(ytmusic, entries, check_deadline)
    # Ranked on the *release's* own type -- the album page's, which is what the
    # probe reads to decide album-or-loose -- so that the ordering the probe's
    # first-wins dedup relies on is decided by the same field as the decision
    # itself.  Stable, so YouTube Music's order survives inside each rank.
    releases.sort(key=_rank)
    return YTMusicArtist(
        name=_text(artist.get("name")),
        channel_id=_text(artist.get("channelId")),
        releases=tuple(releases),
        unreadable_releases=unreadable,
        over_cap=over_cap,
    )


def _release_entries(
    ytmusic: YouTubeMusicClient,
    artist: dict[str, Any],
    check_deadline: Callable[[], None] | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Every release on the artist page, best-filed first, and whether it fit.

    A section holds the handful of releases YouTube Music shows on the page
    plus, when it has a ``params``, the continuation that is the *whole*
    discography.  The continuation is preferred and its failure is not fatal:
    ``get_artist_albums`` is the most fragile call in the library (it raises
    ``KeyError`` on artists whose section is laid out unusually), and ten
    albums beat none.

    The order here is the *pre-fetch* one, and it decides which releases
    survive the cap rather than which copy of a track wins: the tile is all
    that is known before ``get_album`` runs, so :func:`_ep_first` ranks on the
    tile's ``type``, which a tile may not carry at all.  The final ordering --
    the one first-wins dedup reads -- is :func:`_rank` over the fetched
    releases, in :func:`fetch_artist`.  Albums still come before singles here
    because the sections are read in that order.

    At most :data:`~app.models.MAX_SUBCOLLECTIONS` releases are returned; the
    second half of the answer says the discography was longer than that and
    the rest was dropped.  There is no early stop on a track count: a release
    tile carries no track count, and the count only becomes known once the
    release has been fetched.
    """
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    over_cap = False
    for name in _RELEASE_SECTIONS:
        section = artist.get(name)
        if not isinstance(section, dict):
            continue
        results = section.get("results")
        results = list(results) if isinstance(results, list) else []
        params, browse_id = section.get("params"), section.get("browseId")
        if params and browse_id:
            _check(check_deadline)
            try:
                full = ytmusic.get_artist_albums(browse_id, params, limit=None)
            except _YTMUSIC_FAILURES as exc:
                logger.warning(
                    "Could not read the full %s list for %s (%s: %s); using the "
                    "%d shown on the artist page",
                    name,
                    browse_id,
                    type(exc).__name__,
                    exc,
                    len(results),
                )
            else:
                if isinstance(full, list) and full:
                    results = full
        if name == "singles":
            # Stable, so YouTube Music's own order survives inside each rank.
            results = sorted(results, key=_ep_first)
        for entry in results:
            if not isinstance(entry, dict):
                continue
            browse = entry.get("browseId")
            if not isinstance(browse, str) or not browse or browse in seen:
                continue
            if len(entries) >= MAX_SUBCOLLECTIONS:
                over_cap = True
                break
            seen.add(browse)
            entries.append(entry)
        if over_cap:
            logger.warning(
                "Stopped reading %s's discography at %d releases",
                artist.get("channelId"),
                MAX_SUBCOLLECTIONS,
            )
            break
    return entries, over_cap


def _ep_first(entry: object) -> int:
    """Sort key putting an EP tile ahead of a Single tile.

    The pre-fetch tiebreak only: it ranks on the artist page's tile, which is
    what exists before the album pages are read, and its job is to decide what
    fits under :data:`~app.models.MAX_SUBCOLLECTIONS` (see
    :func:`_release_entries`).  Which copy of a shared recording wins is
    :func:`_rank`'s answer, on the release itself.
    """
    release_type = entry.get("type") if isinstance(entry, dict) else None
    return 0 if release_type == EP_RELEASE_TYPE else 1


def _rank(release: YTMusicRelease) -> int:
    """Where *release* sorts: Album (and untyped) first, then EP, then Single.

    One recording is often on two releases -- Glass Beams' "Mahal" is both the
    EP and the single of that name -- and the probe keeps the row it saw
    first, so this order is what puts the track in ``Glass Beams/Mahal/``
    rather than loose.  It reads ``release_type``, the field
    :func:`app.probe._ytmusic_enumeration` also decides album-or-loose on, so
    the two can never disagree about which release is the fuller one.
    """
    return _RELEASE_RANK.get(release.release_type or "", 0)


def _fetch_releases(
    ytmusic: YouTubeMusicClient,
    entries: list[dict[str, Any]],
    check_deadline: Callable[[], None] | None,
) -> tuple[list[YTMusicRelease], int]:
    """Each entry's track list, in the order the entries came in.

    The calls go out on a small pool and the results are collected in release
    order, so the preview reads like the discography does -- newest album
    first, singles after -- however the responses came back.

    The deadline is honest about the pending calls only.  ``cancel_futures``
    stops the ones that have not started; a ``get_album`` already in flight
    runs to its own end, and :data:`REQUEST_TIMEOUT_SECONDS` bounds each
    *socket read* rather than the whole response, so a server that dribbles
    can overrun it.  That is accepted here: the pool is small, the answers are
    YouTube's own, and the cost is a worker thread staying busy after the
    probe has already answered.
    """
    _check(check_deadline)
    releases: list[YTMusicRelease] = []
    unreadable = 0
    pool = ThreadPoolExecutor(max_workers=MAX_ALBUM_WORKERS)
    try:
        futures = [pool.submit(_fetch_album, ytmusic, entry) for entry in entries]
        for entry, future in zip(entries, futures):
            # Between results rather than only up front: the deadline is what
            # stops a probe nobody is waiting for, and the calls that have not
            # started yet are cancelled on the way out.
            _check(check_deadline)
            album = future.result()
            if album is None:
                unreadable += 1
                continue
            releases.append(_release(entry, album))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return releases, unreadable


def _fetch_album(
    ytmusic: YouTubeMusicClient, entry: dict[str, Any]
) -> dict[str, Any] | None:
    try:
        album = ytmusic.get_album(entry["browseId"])
    except _YTMUSIC_FAILURES as exc:
        logger.warning(
            "Could not read release %s: %s: %s", entry.get("browseId"), type(exc).__name__, exc
        )
        return None
    return album if isinstance(album, dict) else None


def _release(entry: dict[str, Any], album: dict[str, Any]) -> YTMusicRelease:
    """One release, preferring the album page's own title and type.

    The artist page and the album page can disagree (the section entry is the
    tile, the album page is the record); the album page wins, and the tile is
    the fallback for a field it left out.
    """
    tracks = album.get("tracks")
    return YTMusicRelease(
        title=_text(album.get("title")) or _text(entry.get("title")),
        release_type=_text(album.get("type")) or _text(entry.get("type")),
        thumbnail_url=_thumbnail(album) or _thumbnail(entry),
        tracks=tuple(
            _track(track)
            for track in (tracks if isinstance(tracks, list) else [])
            if isinstance(track, dict)
        ),
    )


def _track(track: dict[str, Any]) -> YTMusicTrack:
    """One track of a release.

    ``videoType`` is deliberately *not* filtered on.  It tells apart an
    uploaded audio track (``…_ATV``) from an official music video (``…_OMV``),
    and a real album mixes the two: on *Black Sands*, "Kiara" and the title
    track are both OMV while the other ten are ATV.  Dropping non-ATV rows
    would therefore lose album tracks -- and "no videos" is already honoured by
    never reading the artist's ``videos`` section at all.

    ``isAvailable`` false, and a track with no ``videoId``, are both real:
    YouTube Music lists an unreleased or region-blocked track by name with
    nothing to play behind it.
    """
    video_id = track.get("videoId")
    duration = track.get("duration_seconds")
    return YTMusicTrack(
        video_id=video_id if isinstance(video_id, str) and video_id else None,
        title=_text(track.get("title")),
        duration=float(duration) if isinstance(duration, (int, float)) else None,
        available=track.get("isAvailable") is not False,
    )


def _thumbnail(source: dict[str, Any]) -> str | None:
    """The largest thumbnail *source* offers; they come smallest first."""
    thumbnails = source.get("thumbnails")
    if not isinstance(thumbnails, list) or not thumbnails:
        return None
    last = thumbnails[-1]
    return _text(last.get("url")) if isinstance(last, dict) else None


def _text(value: object) -> str | None:
    """A YouTube Music field as a non-empty string, or None."""
    return value.strip() or None if isinstance(value, str) else None


def _check(check_deadline: Callable[[], None] | None) -> None:
    if check_deadline is not None:
        check_deadline()


# ---------------------------------------------------------------------------
# Finding an artist by name
# ---------------------------------------------------------------------------


def search_artist(
    name: str,
    *,
    client: YouTubeMusicClient | None = None,
    check_deadline: Callable[[], None] | None = None,
) -> tuple[str, str] | None:
    """The channel id and name of the artist YouTube Music puts first for *name*.

    For the Spotify hand-off (:func:`app.probe._spotify_enumeration`): the name
    read off a Spotify artist page is all there is to go on, and
    ``search(filter="artists")`` answers with artist rows whose ``browseId`` is
    the ``UC…`` channel id :func:`fetch_artist` takes.

    The *top* row is taken and no picker is offered, which is the phase's
    decision: an artist search for a full display name is right nearly always,
    the preview says out loud which artist it matched, and the user can edit
    the artist field or paste the YouTube Music URL instead when it is wrong.
    A first row whose ``browseId`` is not a channel id is "no confident match"
    (None) rather than a reason to go looking down the list -- picking the
    second row *is* a picker, and a silent one.

    Returns None when nothing matched.  Raises:
        YouTubeMusicUnavailable: If the search never got an answer.
    """
    query = name.strip()
    if not query:
        return None
    _check(check_deadline)
    try:
        # Inside the try for the same reason ``fetch_artist``'s call is:
        # building the client fetches the visitor id, which is a network act.
        ytmusic = client if client is not None else shared_client()
        results = ytmusic.search(query, filter="artists", limit=ARTIST_SEARCH_LIMIT)
    except _UNREACHABLE as exc:
        raise YouTubeMusicUnavailable(f"{type(exc).__name__}: {exc}") from exc
    except _NOT_AN_ARTIST as exc:
        logger.info(
            "Searching YouTube Music for %r failed to parse: %s: %s",
            query,
            type(exc).__name__,
            exc,
        )
        return None
    if not isinstance(results, list) or not results:
        logger.info("YouTube Music has no artist matching %r", query)
        return None

    top = results[0]
    if not isinstance(top, dict):
        return None
    channel_id = _channel_id(str(top.get("browseId") or ""))
    if channel_id is None:
        logger.info("YouTube Music's top artist for %r carries no channel id", query)
        return None
    # ``artist`` is where the filtered search parser puts the display name;
    # the other two are what the unfiltered and top-result shapes use.
    matched = _text(top.get("artist")) or _text(top.get("title")) or _text(top.get("name"))
    return channel_id, matched or query
