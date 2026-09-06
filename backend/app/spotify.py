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

The body of each answer is read with ``response.raw.read1`` in a loop rather
than with ``requests``' ``iter_content``.  The read timeout in
:data:`_TIMEOUT` is per *socket read*, not per response: a server that
dribbles a byte at a time resets it on every read, so a single 8 KiB
``iter_content`` chunk can take hours to assemble and no deadline check
between chunks would ever run.  Reading one raw read at a time is what lets
the probe's deadline be checked after each returned chunk (at most a few
socket reads on a compressed body), so the overrun is normally bounded by a
small multiple of one read timeout rather than by the whole body.  The check
only runs *between* ``read1`` returns, though, and urllib3's ``read1`` with
``decode_content=True`` loops internally until the decoder yields bytes: a
pathological content-encoding that decodes to nothing hands back no chunk to
check between.  Accepted, because the host is pinned and no redirect is
followed -- the only server that can do it is Spotify itself.

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
from email.message import Message
from html.parser import HTMLParser
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import requests
import urllib3

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

# How long one request to Spotify may take: (connect, read).  Two of these run
# inside a probe that has its own deadline, so they are short.
_TIMEOUT = (5, 10)

# How much of a response is read before the rest is thrown away.  The oEmbed
# JSON is under a kilobyte and an artist page is ~300 KB whose ``<title>`` and
# ``og:title`` are both in the first few, so this only ever truncates the tail
# of the page -- but it is what stops a hostile or broken response from being
# read into memory without bound.
MAX_RESPONSE_BYTES = 256 * 1024

# Sent because Spotify serves a different (and sometimes no) page to a client
# that does not look like a browser.  Honest about what it is: this is a
# self-hosted app reading a public page, not a crawler pretending otherwise.
USER_AGENT = (
    "Mozilla/5.0 (compatible; music-for-arr/1.0; +https://github.com/) "
    "python-requests"
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
    document (see :data:`MAX_RESPONSE_BYTES`) is fine: the parser simply never
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


def _charset(content_type: str | None) -> str | None:
    """The charset a Content-Type explicitly declares, or None.

    Not ``response.encoding``: ``requests`` answers ISO-8859-1 for any
    ``text/*`` that declares nothing, which would mangle a UTF-8 name into
    ``Ã\x81rtist``.  An undeclared body is utf-8 here -- what Spotify sends,
    and what JSON is by spec.
    """
    if not content_type:
        return None
    message = Message()
    message["Content-Type"] = content_type
    charset = message.get_param("charset")
    return charset if isinstance(charset, str) else None


def _get_text(
    session: requests.Session,
    url: str,
    params: dict[str, str] | None = None,
    *,
    check_deadline: Callable[[], None] | None = None,
) -> str | None:
    """At most :data:`MAX_RESPONSE_BYTES` of *url*'s body, or None.

    ``allow_redirects=False`` -- see the module docstring: the canonical URLs
    answer 200 directly, so a 3xx here is something this code should not be
    following, and it is reported as "no answer" rather than chased.  The body
    is read one socket read at a time and cut at the cap instead of being read
    whole, so a response that never ends cannot become memory *or* time: see
    the module docstring for why ``raw.read1`` rather than ``iter_content``.

    Whatever *check_deadline* raises is left alone on the way out.  A probe's
    ``ProbeTimeout`` is neither a ``urllib3`` error nor an ``OSError``, so the
    ``except`` below does not catch it and it reaches the probe as the deadline
    answer it is, rather than being reported as Spotify being unreachable.  A
    *check_deadline* that raised an ``OSError`` subclass would be swallowed as
    Spotify being unreachable; nothing in this tree raises one.
    """
    try:
        with session.get(
            url,
            params=params,
            timeout=_TIMEOUT,
            allow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            stream=True,
        ) as response:
            if response.status_code != 200:
                logger.info("Spotify answered %s for %s", response.status_code, url)
                return None
            body = b""
            while len(body) < MAX_RESPONSE_BYTES:
                # ``read1`` (urllib3 2.x, which is what is pinned) returns
                # whatever it has rather than waiting to fill a buffer, so the
                # deadline below is checked after each returned chunk (at most
                # a few socket reads on a compressed body): the overrun is
                # normally bounded by a small multiple of one read timeout
                # rather than by the whole body.  Only *between* returns,
                # though -- with ``decode_content=True`` this loops inside
                # urllib3 until the decoder yields bytes, so a content-encoding
                # that decodes to nothing never comes back here to be checked.
                # Accepted: the host is pinned and nothing redirects.
                chunk = response.raw.read1(8192, decode_content=True)
                if not chunk:
                    break
                body += chunk
                if check_deadline is not None:
                    check_deadline()
            body = body[:MAX_RESPONSE_BYTES]
            # Read inside the ``with``: the connection is released on the way
            # out and nothing about the response should be touched after that.
            encoding = _charset(response.headers.get("Content-Type")) or "utf-8"
    # ``requests`` only translates urllib3's errors into its own inside
    # ``iter_content``/``.content``; reading ``raw`` ourselves means a
    # ``ReadTimeoutError``, a ``ProtocolError`` on a truncated body or a
    # ``DecodeError`` on corrupt gzip arrives raw and would escape to the route
    # as a 500.  Both arms are needed: ``RequestException`` is an ``OSError``,
    # so that half still covers the connect-time failures, and
    # ``urllib3.exceptions.HTTPError`` is not an ``OSError`` at all.
    except (urllib3.exceptions.HTTPError, OSError) as exc:
        raise SpotifyUnavailable(f"{type(exc).__name__}: {exc}") from exc
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:  # a header naming an unknown charset
        return body.decode("utf-8", errors="replace")
