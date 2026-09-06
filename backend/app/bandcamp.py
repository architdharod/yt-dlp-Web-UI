"""Read a Bandcamp seller's *display name* off their page, and nothing else.

Bandcamp is a source -- yt-dlp downloads from it -- but its flat listing of an
artist or label page carries no display name anywhere.  ``BandcampUserIE``
builds the whole answer out of the subdomain: the playlist's title is literally
``"Discography of <subdomain>"`` and every entry is a bare URL.  So a probe of
``https://amelielens.bandcamp.com/`` used to suggest the artist "amelielens",
which is what the files were filed under and what every MusicBrainz lookup then
asked about -- and no lookup matches "amelielens".

The page itself knows better, and says so three times over.  Checked live
against ``https://amelielens.bandcamp.com/``:

* ``data-band``, an attribute on the page's ``<body>``-level element holding
  the band record as JSON -- ``{"id": …, "name": "Amelie Lens", "subdomain":
  "amelielens", …}``.  This is the one that is unambiguously the *name*, so it
  is read first;
* ``<meta property="og:site_name" content="Amelie Lens">``;
* ``<title>Music | Amelie Lens</title>``.

One GET answers all three, so that is what this does: one request per *page*,
never one per track.  Everything about how that request is made lives in
:mod:`app.fetch` -- timeout, a cap on the body, no redirect followed.

Nothing here raises for a Bandcamp that is slow, broken or has moved its
markup on: the answer is None and the probe keeps the subdomain, which is what
it used to show anyway.  The exception is the caller's own deadline, which
propagates: it is the probe's answer, not Bandcamp's.
"""

import json
import logging
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests

from app.fetch import FetchFailed, get_text

logger = logging.getLogger(__name__)

# The one Bandcamp host.  A seller lives on a subdomain of it; ``bandcamp.com``
# itself is the marketing site, the radio and the discovery pages, none of
# which name an artist.
BANDCAMP_HOST = "bandcamp.com"

# Subdomains that are not a seller.  ``www`` is the site itself and the empty
# string is the apex.
_NOT_A_SELLER = frozenset({"", "www"})

# The paths ``BandcampUserIE`` matches -- the seller's root and its music tab.
# Only these get a name lookup: an ``/album/`` or ``/track/`` page is a full
# extraction that yt-dlp already reads the artist off.
_ARTIST_PATHS = frozenset({"", "/", "/music", "/music/"})

# Bandcamp's page titles for a seller root are "Music | <Name>".
_TITLE_PREFIX = "Music | "


def artist_page_url(url: str) -> str | None:
    """The seller page to read a name from, or None if *url* is not one.

    Parsing only, no network.  The URL that comes back is rebuilt from the
    parsed host rather than carried over from the paste, so no query, fragment
    or path a user copied is sent back to Bandcamp.  ``None`` for anything that
    is not a seller's root or ``/music`` -- an album, a track, the radio, the
    apex domain -- because those are not pages whose name the probe is missing.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").casefold()
    if not host.endswith("." + BANDCAMP_HOST):
        return None
    subdomain = host[: -len("." + BANDCAMP_HOST)]
    if subdomain in _NOT_A_SELLER or "." in subdomain:
        return None
    if parts.path.casefold() not in _ARTIST_PATHS:
        return None
    return f"https://{subdomain}.{BANDCAMP_HOST}/"


def resolve_artist_name(
    page_url: str,
    *,
    session: requests.Session | None = None,
    check_deadline: Callable[[], None] | None = None,
) -> str | None:
    """The display name on *page_url*, or None if the page did not give one.

    *page_url* must be one :func:`artist_page_url` returned.  One GET, three
    sources read out of the one body in decreasing order of how certainly they
    are the artist's name (see the module docstring).

    Never raises for anything Bandcamp did: an unreachable host, a non-200, a
    body whose markup has moved on and a ``data-band`` that is not JSON all
    come back as None, and the probe keeps the subdomain.  Whatever
    *check_deadline* raises does propagate -- it is the caller's answer.
    """
    close = session is None
    http = session if session is not None else requests.Session()
    try:
        html = get_text(http, page_url, label="Bandcamp", check_deadline=check_deadline)
    except FetchFailed as exc:
        logger.info("Could not read the Bandcamp page %s: %s", page_url, exc)
        return None
    finally:
        if close:
            http.close()
    if html is None:
        return None
    return artist_name_from_html(html)


def artist_name_from_html(html: str) -> str | None:
    """The display name in a seller page's markup, or None.

    Split out from the fetch so it can be tested against a saved page without a
    network of any kind, and so a probe that already has the body does not need
    a second one.
    """
    reader = _BandPageReader()
    try:
        reader.feed(html)
        reader.close()
        return (
            _band_attr_name(reader.data_band)
            or _clean(reader.site_name)
            or _title_name(reader.title)
        )
    # Inside the same ``try`` as the parse, deliberately: this module promises
    # the probe that reading a page raises nothing, and a page is whatever a
    # server chose to send.  ``BaseException`` is not caught -- a
    # ``KeyboardInterrupt``, and the probe's own deadline, are not Bandcamp's.
    except Exception:
        logger.info("Could not read a name out of a Bandcamp page", exc_info=True)
        return None


def _band_attr_name(raw: str | None) -> str | None:
    """The ``name`` out of a ``data-band`` attribute's JSON, or None."""
    if not raw:
        return None
    try:
        band = json.loads(raw)
    # ``RecursionError`` as well as ``ValueError``: ``json`` parses nesting
    # recursively, so an attribute of ten thousand open braces -- well inside
    # :data:`app.fetch.MAX_RESPONSE_BYTES` -- blows the interpreter's stack
    # rather than failing as malformed.  Both mean the same thing here.
    except (ValueError, RecursionError):
        logger.info("A Bandcamp page's data-band attribute was not usable JSON")
        return None
    if not isinstance(band, dict):
        return None
    return _clean(band.get("name"))


def _title_name(title: str | None) -> str | None:
    """``"Music | Amelie Lens"`` as ``"Amelie Lens"``, or None.

    The prefix has to be *there*: a title without it is some other Bandcamp
    page -- a 404, a client challenge -- and the whole of it is not a name.
    """
    cleaned = _clean(title)
    if cleaned is None or not cleaned.startswith(_TITLE_PREFIX):
        return None
    return _clean(cleaned[len(_TITLE_PREFIX) :])


def _clean(value: object) -> str | None:
    """*value* as a name: stripped, and blank (or not a string) is None."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


class _BandPageReader(HTMLParser):
    """Pulls ``data-band``, ``og:site_name`` and ``<title>`` out of a page.

    First of each wins.  ``HTMLParser`` unescapes attribute values itself, so
    the ``&quot;``-escaped JSON in ``data-band`` arrives as JSON and a name like
    "Simon & Garfunkel" arrives intact from any of the three.  A truncated
    document (:data:`app.fetch.MAX_RESPONSE_BYTES`) is fine: the parser simply
    never sees the tags that were cut off -- though a ``data-band`` cut in half
    is then not JSON, which is exactly the case the other two sources cover.

    Two things about ``<title>`` that a page can get wrong, and this cannot:

    * a ``<title>`` that is never closed.  ``HTMLParser`` reports no end tag
      for it, so a naive reader keeps appending and the "title" ends up being
      the whole rest of the document.  The title is closed here after its
      first run of text instead: a real one is one text node;
    * a ``<title>`` inside an ``<svg>``, which is the SVG *accessible name* of
      an icon and not the page's title at all.  Bandcamp's pages carry inline
      SVG, and an icon that happened to sit above the head title would win.
      The nesting depth is tracked so those are skipped.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.data_band: str | None = None
        self.site_name: str | None = None
        self._title_parts: list[str] = []
        self._in_title = False
        self._title_done = False
        self._svg_depth = 0

    @property
    def title(self) -> str | None:
        return "".join(self._title_parts) or None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.casefold(): value for name, value in attrs}
        if self.data_band is None and values.get("data-band"):
            self.data_band = values["data-band"]
        if tag == "svg":
            self._svg_depth += 1
            return
        if tag == "title":
            if not self._title_done and self._svg_depth == 0:
                self._in_title = True
            return
        if tag == "meta" and self.site_name is None:
            key = values.get("property") or values.get("name") or ""
            if key.casefold() == "og:site_name" and values.get("content"):
                self.site_name = values["content"]

    def handle_endtag(self, tag: str) -> None:
        if tag == "svg":
            self._svg_depth = max(0, self._svg_depth - 1)
            return
        if tag == "title" and self._in_title:
            self._close_title()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
            # Closed here rather than on ``</title>``: an unclosed one would
            # otherwise swallow every text node after it.  A page whose title
            # arrives split across two data chunks loses the tail, which is a
            # far smaller wrong answer than the whole document.
            self._close_title()

    def _close_title(self) -> None:
        self._in_title = False
        self._title_done = True
