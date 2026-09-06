"""Read an artist's *name* off a Spotify URL, and nothing else.

Spotify is not a source: yt-dlp cannot download from it, and this app holds no
Spotify credentials of any kind.  What a pasted ``open.spotify.com/artist/…``
is good for is the one thing a public page gives away for free -- the artist's
name -- which :mod:`app.probe` then searches YouTube Music for, so the preview
that comes back is the *YouTube Music* discography of the artist the user was
looking at on Spotify.

So this module answers two questions and stops:

* :func:`spotify_url_target` -- is this a Spotify URL, and what does it name?
  Parsing alone, no network.  A ``/track/``, ``/album/`` or ``/playlist/`` is
  recognised as a Spotify URL of a kind we do not support, which is a better
  answer than "not Spotify at all": the probe can then say what *is* supported
  rather than handing the URL to a downloader that has never heard of it;
* :func:`resolve_artist_name` -- what is that artist called?  The public oEmbed
  endpoint (``/oembed?url=…``) answers with the display name in ``title`` and
  needs no auth, no token and no client id; when it does not answer, the artist
  page itself carries the name twice, in ``og:title`` and in
  ``<title>Name | Spotify</title>``.

The module is deliberately ignorant of the probe -- plain dataclasses out, the
probe adapts them -- exactly like :mod:`app.ytmusic`, so the two can be tested
apart.  Everything here is blocking and is called from the probe's worker
thread.

Both requests go through :func:`app.fetch.get_text`, which is where the
timeout, the cap on how much of a body is read, the refusal to follow a
redirect and the deadline check between socket reads all live; see that
module for why the body is read the way it is.

Both requests are made against a URL *we* build from the parsed id
(:attr:`SpotifyTarget.canonical_url`) rather than against the string the user
pasted, and neither follows redirects.  Spotify answers the canonical artist
URL with a 200 directly (checked live: 200, no redirects; a nonexistent id is a
404), so nothing legitimate is lost, and refusing to follow a 3xx is the
tightest policy available -- there is no hop to a consent wall, a locale
variant or another host for this code to be talked into making.
"""

import json
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import requests

from app.fetch import FetchFailed, get_text

logger = logging.getLogger(__name__)

# The one Spotify host.  ``spotify.com`` itself is deliberately *not* what the
# API's allowlist widens to: the artist pages, and the oEmbed endpoint, are all
# on this one host, and every other subdomain (accounts, open-graph redirects,
# the marketing site) is nothing this app should be fetching.
SPOTIFY_HOST = "open.spotify.com"

# The kind of Spotify URL this app can do anything with.
ARTIST_KIND = "artist"

# The kinds a Spotify URL can name.  Only :data:`ARTIST_KIND` is supported; the
# rest are listed so that a pasted track or album is recognised *as Spotify* and
# told what is supported, rather than falling through to a downloader that has
# no Spotify extractor and would fail with something unreadable.
_KINDS = frozenset(
    {
        ARTIST_KIND,
        "album",
        "track",
        "playlist",
        "show",
        "episode",
        "user",
        "audiobook",
        "chapter",
        "concert",
        "prerelease",
    }
)

# A Spotify id: 22 characters of base62.  Matched rather than assumed so a
# ``/artist/`` path that is not one cannot become a URL we fetch.
_SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")

# The locale segment Spotify puts in front of a shared link when the sharer's
# app was not in English: ``/intl-de/artist/…``, ``/intl-pt-br/artist/…``.  It
# is dropped rather than passed on -- the canonical URL is what gets fetched.
_LOCALE_SEGMENT = re.compile(r"^intl-[A-Za-z]{2}(?:-[A-Za-z]{2})?$")

# What the probe tells a user who pasted a Spotify URL that is not an artist
# page (or is not a readable one).  Names what *is* supported, because "we
# cannot read this" without that is a dead end.
UNSUPPORTED_KIND_MESSAGE = (
    "Only Spotify artist URLs are supported; paste the artist page, or a "
    "YouTube / YouTube Music / SoundCloud / Bandcamp link for a track, album "
    "or playlist"
)

# Spotify's page titles are "<Name> | Spotify".
_TITLE_SUFFIX = " | Spotify"


class SpotifyUnavailable(Exception):
    """Spotify could not be reached, so nothing was learned.

    Distinct from :func:`resolve_artist_name` returning None, which means the
    page answered and held no name.  The probe turns both into an error -- there
    is no fallback for a Spotify URL, since yt-dlp cannot read one -- but they
    are different events and only this one is worth retrying.
    """


@dataclass(frozen=True)
class SpotifyTarget:
    """A URL recognised as Spotify's, and what it names.

    ``kind`` is :data:`ARTIST_KIND` for the one shape this app supports and the
    entity name (``"track"``, ``"album"``, …) for the ones it does not.
    """

    kind: str
    id: str

    @property
    def canonical_url(self) -> str:
        """The URL this target is fetched by: no locale, no query, no fragment.

        Rebuilt from the parsed parts rather than carried over from the paste,
        so nothing a user copied out of a share sheet (``?si=``, a tracking
        fragment, a locale prefix) is sent back to Spotify or reaches the
        HTTP layer as syntax.
        """
        return f"https://{SPOTIFY_HOST}/{self.kind}/{self.id}"

    @property
    def is_artist(self) -> bool:
        return self.kind == ARTIST_KIND


# ---------------------------------------------------------------------------
# URL recognition
# ---------------------------------------------------------------------------


def is_spotify_url(url: str) -> bool:
    """Whether *url* is an http(s) URL on :data:`SPOTIFY_HOST`.

    Asked before :func:`spotify_url_target`, and separately from it, because
    the two answers differ: a Spotify URL this module cannot parse -- the site
    root, a search page, a mangled id -- is still a Spotify URL, and the probe
    must refuse it with the message naming what is supported rather than pass
    it to yt-dlp.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").casefold()
    return host == SPOTIFY_HOST or host.endswith("." + SPOTIFY_HOST)


def spotify_url_target(url: str) -> SpotifyTarget | None:
    """What *url* names on Spotify, or None if it names nothing recognisable.

    Parsing only -- no network.  Accepts the shapes a share sheet produces: a
    locale prefix (``/intl-de/artist/…``), a query string (``?si=…``) and a
    fragment are all ignored.  ``spotify:artist:<id>`` URIs are not accepted:
    they are not http(s) URLs and never reach the API, whose validator rejects
    them for their scheme first.
    """
    if not is_spotify_url(url):
        return None
    parts = urlsplit(url.strip())
    segments = [segment for segment in parts.path.split("/") if segment]
    if segments and _LOCALE_SEGMENT.match(segments[0]):
        segments = segments[1:]
    if len(segments) != 2:
        # One segment is the site root or a listing page; three or more is
        # something nested (an artist's ``/discography/all``) that this app has
        # no use for either way.
        return None
    kind, identifier = segments[0].casefold(), segments[1]
    if kind not in _KINDS or not _SPOTIFY_ID.match(identifier):
        return None
    return SpotifyTarget(kind=kind, id=identifier)


# ---------------------------------------------------------------------------
# Reading the name
# ---------------------------------------------------------------------------


def resolve_artist_name(
    target: SpotifyTarget,
    *,
    session: requests.Session | None = None,
    check_deadline: Callable[[], None] | None = None,
) -> str | None:
    """*target*'s display name, or None if the answers held none.

    Two sources, in order of how much has to go right for them to be read:

    * ``/oembed?url=…``, the public embed endpoint.  It is JSON, it needs no
      credentials of any kind, and its ``title`` is the display name (checked
      live: ``{"title": "Radiohead", …}``).  A 404 -- which is what a
      nonexistent id gets -- falls through rather than failing;
    * the artist page, whose ``og:title`` is the bare name and whose
      ``<title>`` is ``"<Name> | Spotify"``.  Read with a bounded parser, so a
      page whose markup has moved on gives None rather than an exception.

    *check_deadline* is the probe's own deadline check, called after each
    returned chunk; whatever it raises propagates, since it is the caller's
    answer and not Spotify's.

    Raises:
        SpotifyUnavailable: The request never got an answer.  Raised for a
            transport failure on *either* fetch: both go to the same host, so
            one refusing to connect says the second would too.
    """
    close = session is None
    http = session if session is not None else requests.Session()
    try:
        return _oembed_name(http, target, check_deadline=check_deadline) or _page_name(
            http, target, check_deadline=check_deadline
        )
    finally:
        if close:
            http.close()


def _oembed_name(
    session: requests.Session,
    target: SpotifyTarget,
    *,
    check_deadline: Callable[[], None] | None = None,
) -> str | None:
    """The name from the oEmbed endpoint, or None if it did not give one."""
    payload = _get_json(
        session,
        f"https://{SPOTIFY_HOST}/oembed",
        params={"url": target.canonical_url},
        check_deadline=check_deadline,
    )
    if payload is None:
        return None
    return _clean_name(payload.get("title"))


def _page_name(
    session: requests.Session,
    target: SpotifyTarget,
    *,
    check_deadline: Callable[[], None] | None = None,
) -> str | None:
    """The name from the artist page's own ``og:title``/``<title>``."""
    html = _get_text(session, target.canonical_url, check_deadline=check_deadline)
    if html is None:
        return None
    reader = _TitleReader()
    try:
        reader.feed(html)
        reader.close()
    except Exception:  # pragma: no cover - HTMLParser is forgiving by design
        logger.info("Could not parse the Spotify page for %s", target.canonical_url)
    return _clean_name(reader.og_title) or _clean_name(reader.title)


def _clean_name(value: object) -> str | None:
    """A page's title as an artist name: suffix off, blank is None.

    ``" | Spotify"`` comes off ``og:title`` too, not only ``<title>``: it is
    absent there today, and stripping a suffix that is not present costs
    nothing while stripping one that appears later saves a wrong folder name.
    """
    if not isinstance(value, str):
        return None
    name = value.strip()
    if name.endswith(_TITLE_SUFFIX):
        name = name[: -len(_TITLE_SUFFIX)].strip()
    return name or None


class _TitleReader(HTMLParser):
    """Pulls ``og:title`` and ``<title>`` out of a page, first of each wins.

    ``convert_charrefs`` leaves ``&amp;`` as ``&`` in the text, and
    ``HTMLParser`` unescapes attribute values itself, so a name like
    "Simon & Garfunkel" arrives intact from either source.  A truncated
    document (see :data:`app.fetch.MAX_RESPONSE_BYTES`) is fine: the parser simply never
    sees the tags that were cut off.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og_title: str | None = None
        self._title_parts: list[str] = []
        self._in_title = False
        self._title_done = False

    @property
    def title(self) -> str | None:
        return "".join(self._title_parts) or None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            if not self._title_done:
                self._in_title = True
            return
        if tag != "meta" or self.og_title is not None:
            return
        values = {name.casefold(): value for name, value in attrs}
        key = values.get("property") or values.get("name") or ""
        if key.casefold() == "og:title" and values.get("content"):
            self.og_title = values["content"]

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)


# ---------------------------------------------------------------------------
# Bounded HTTP
# ---------------------------------------------------------------------------


def _get_json(
    session: requests.Session,
    url: str,
    params: dict[str, str],
    *,
    check_deadline: Callable[[], None] | None = None,
) -> dict[str, Any] | None:
    """One GET whose body is parsed as a JSON object, or None.

    None covers everything short of an answer we can read: a non-200 (the
    oEmbed endpoint 404s an id it does not know), a body that is not JSON, and
    JSON that is not an object.  Every one of those means "ask the page
    instead", which is a better answer than an error.
    """
    body = _get_text(session, url, params=params, check_deadline=check_deadline)
    if body is None:
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        logger.info("Spotify's oEmbed answer for %s was not JSON", params.get("url"))
        return None
    return payload if isinstance(payload, dict) else None


def _get_text(
    session: requests.Session,
    url: str,
    params: dict[str, str] | None = None,
    *,
    check_deadline: Callable[[], None] | None = None,
) -> str | None:
    """One bounded GET, with the transport failure named as Spotify's.

    Everything about how the request is made lives in :mod:`app.fetch`; this
    only relabels its failure, because there is no fallback for a Spotify URL
    and the probe's message has to say which site went quiet.

    Raises:
        SpotifyUnavailable: The request never got an answer.
    """
    try:
        return get_text(
            session,
            url,
            params,
            label="Spotify",
            check_deadline=check_deadline,
        )
    except FetchFailed as exc:
        raise SpotifyUnavailable(str(exc)) from exc
