"""Tests for reading a Bandcamp seller's display name off their page.

The fixture in ``tests/fixtures/bandcamp/artist-page.html`` is the head of the
real ``https://amelielens.bandcamp.com/`` (trimmed to the ``<head>`` plus the
one element carrying ``data-band``), so the three sources are tested against
the markup Bandcamp actually serves rather than against a guess at it.
Nothing here touches the network.
"""

import json
from html import escape
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
import urllib3

from app.bandcamp import (
    artist_name_from_html,
    artist_page_url,
    resolve_artist_name,
)
from app.fetch import TIMEOUT, USER_AGENT
from tests.conftest import FakeResponse, FakeSession

FIXTURE = Path(__file__).parent / "fixtures" / "bandcamp" / "artist-page.html"

ARTIST_URL = "https://amelielens.bandcamp.com/"


def live_page() -> str:
    return FIXTURE.read_text()


def page(
    band: dict | None = None,
    site_name: str | None = None,
    title: str | None = None,
) -> FakeResponse:
    """A minimal seller page carrying whichever of the three names is asked for."""
    head = "<!DOCTYPE html><html><head>"
    if site_name is not None:
        head += f'<meta property="og:site_name" content="{site_name}"/>'
    if title is not None:
        head += f"<title>{title}</title>"
    head += "</head><body>"
    if band is not None:
        attr = json.dumps(band).replace("&", "&amp;").replace('"', "&quot;")
        head += f'<div id="pgBd" data-band="{attr}">'
    return FakeResponse(body=(head + "</body></html>").encode())


# ===========================================================================
# Which URLs get a name lookup at all
# ===========================================================================


class TestArtistPageUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://amelielens.bandcamp.com",
            "https://amelielens.bandcamp.com/",
            "https://amelielens.bandcamp.com/music",
            "https://amelielens.bandcamp.com/music/",
            "http://amelielens.bandcamp.com/",
            "https://AmelieLens.Bandcamp.com/music",
        ],
        ids=["bare", "root", "music", "music-slash", "http", "mixed-case"],
    )
    def test_a_seller_page_is_rebuilt_canonically(self, url):
        assert artist_page_url(url) == ARTIST_URL

    def test_a_query_and_fragment_are_dropped(self):
        assert (
            artist_page_url("https://amelielens.bandcamp.com/?from=share#top")
            == ARTIST_URL
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://amelielens.bandcamp.com/track/theory-of-relativity",
            "https://amelielens.bandcamp.com/album/exhale",
            "https://amelielens.bandcamp.com/merch",
            "https://bandcamp.com/",
            "https://www.bandcamp.com/",
            "https://bandcamp.com/radio?show=1",
            "https://notbandcamp.com/",
            "https://evil.com/x.bandcamp.com/",
            "ftp://amelielens.bandcamp.com/",
        ],
        ids=[
            "track",
            "album",
            "other-path",
            "apex",
            "www",
            "radio",
            "other-host",
            "host-in-path",
            "not-http",
        ],
    )
    def test_everything_else_gets_no_lookup(self, url):
        assert artist_page_url(url) is None

    def test_a_nested_subdomain_is_not_a_seller(self):
        # Sellers live one label deep; anything deeper is not a shape Bandcamp
        # serves and is not a URL this app should be building requests to.
        assert artist_page_url("https://a.b.bandcamp.com/") is None


# ===========================================================================
# Reading the name out of the markup
# ===========================================================================


class TestParsingTheName:
    def test_the_real_page_names_its_artist(self):
        assert artist_name_from_html(live_page()) == "Amelie Lens"

    def test_data_band_wins(self):
        html = (
            '<html><head><meta property="og:site_name" content="Second"/>'
            "<title>Music | Third</title></head>"
            '<body><div data-band="{&quot;name&quot;: &quot;First&quot;}"></div>'
            "</body></html>"
        )
        assert artist_name_from_html(html) == "First"

    def test_og_site_name_is_next(self):
        html = (
            '<html><head><meta property="og:site_name" content="Second"/>'
            "<title>Music | Third</title></head><body></body></html>"
        )
        assert artist_name_from_html(html) == "Second"

    def test_the_title_is_last(self):
        assert artist_name_from_html("<title>Music | Third</title>") == "Third"

    def test_a_title_without_the_prefix_is_not_a_name(self):
        # "Client Challenge" and Bandcamp's 404 page both have a title; neither
        # of them is what the artist is called.
        assert artist_name_from_html("<title>Client Challenge</title>") is None

    def test_a_data_band_that_is_not_json_falls_through(self):
        html = (
            '<html><head><title>Music | Amelie Lens</title></head>'
            '<body><div data-band="{truncated"></div></body></html>'
        )
        assert artist_name_from_html(html) == "Amelie Lens"

    def test_a_data_band_without_a_name_falls_through(self):
        html = (
            '<html><head><meta property="og:site_name" content="Amelie Lens"/>'
            "</head><body>"
            '<div data-band="{&quot;subdomain&quot;: &quot;amelielens&quot;}">'
            "</div></body></html>"
        )
        assert artist_name_from_html(html) == "Amelie Lens"

    def test_an_ampersand_survives_every_source(self):
        assert (
            artist_name_from_html(
                '<div data-band="{&quot;name&quot;: &quot;Simon &amp; Garfunkel'
                '&quot;}"></div>'
            )
            == "Simon & Garfunkel"
        )
        assert (
            artist_name_from_html(
                '<meta property="og:site_name" content="Simon &amp; Garfunkel"/>'
            )
            == "Simon & Garfunkel"
        )
        assert (
            artist_name_from_html("<title>Music | Simon &amp; Garfunkel</title>")
            == "Simon & Garfunkel"
        )

    def test_a_blank_name_is_no_name(self):
        assert artist_name_from_html('<div data-band="{&quot;name&quot;: &quot;  &quot;}">') is None
        assert artist_name_from_html("<title>Music |   </title>") is None

    def test_a_deeply_nested_data_band_falls_through(self):
        """``json`` parses nesting recursively, so this is a stack overflow.

        Ten thousand open braces is well inside the 256 KiB read cap, and the
        ``RecursionError`` it raises is not a ``ValueError``: unguarded it
        escapes the probe as a 500 rather than falling back to the subdomain.
        """
        payload = escape("{" + '"a":{' * 17000)
        html = (
            "<html><head><title>Music | Amelie Lens</title></head>"
            f'<body><div data-band="{payload}"></div></body></html>'
        )
        assert artist_name_from_html(html) == "Amelie Lens"

    def test_an_unclosed_title_does_not_eat_the_page(self):
        # Without closing the title on its first text node, everything after it
        # is appended and the "name" is the whole rest of the document.
        html = "<html><head><title>Music | Amelie Lens<body>and then some junk"
        assert artist_name_from_html(html) == "Amelie Lens"

    @pytest.mark.parametrize(
        "svg",
        [
            "<svg><title>Play</title></svg>",
            "<svg><g><title>Play</title></g></svg>",
        ],
        ids=["flat", "nested"],
    )
    def test_an_svg_title_is_not_the_page_title(self, svg):
        # ``<title>`` inside an ``<svg>`` is an icon's accessible name.
        html = f"<html><head>{svg}<title>Music | Amelie Lens</title></head></html>"
        assert artist_name_from_html(html) == "Amelie Lens"

    def test_an_unclosed_svg_gives_up_on_the_title(self):
        # Nothing says where an unclosed ``<svg>`` ends, so every later
        # ``<title>`` is treated as being inside it.  ``og:site_name`` is the
        # source that still answers, which is why there are three of them.
        html = (
            '<html><head><meta property="og:site_name" content="Amelie Lens"/>'
            "<svg><title>Play</title>"
            "<title>Music | Somebody Else</title></head></html>"
        )
        assert artist_name_from_html(html) == "Amelie Lens"

    def test_markup_with_nothing_in_it_is_no_name(self):
        assert artist_name_from_html("") is None
        assert artist_name_from_html("<html><body>nothing here</body></html>") is None


# ===========================================================================
# The fetch
# ===========================================================================


class TestResolveArtistName:
    def test_one_get_reads_the_name(self):
        session = FakeSession({ARTIST_URL: page(band={"name": "Amelie Lens"})})
        assert resolve_artist_name(ARTIST_URL, session=session) == "Amelie Lens"
        assert [call["url"] for call in session.calls] == [ARTIST_URL]

    def test_the_request_is_the_conservative_one(self):
        session = FakeSession({ARTIST_URL: page(site_name="Amelie Lens")})
        resolve_artist_name(ARTIST_URL, session=session)
        call = session.calls[0]
        assert call["timeout"] == TIMEOUT
        assert call["allow_redirects"] is False
        assert call["stream"] is True
        assert call["headers"]["User-Agent"] == USER_AGENT

    @pytest.mark.parametrize(
        "answer",
        [
            FakeResponse(status_code=404),
            FakeResponse(status_code=301),
            FakeResponse(status_code=503),
            requests.exceptions.ConnectTimeout("no route"),
            urllib3.exceptions.ReadTimeoutError(None, ARTIST_URL, "too slow"),
            FakeResponse(body=b"<html><body>nothing</body></html>"),
        ],
        ids=["404", "redirect", "503", "timeout", "urllib3", "no-name"],
    )
    def test_nothing_readable_is_no_name_and_no_exception(self, answer):
        # A custom domain answers the subdomain with a 301, which is the
        # "redirect" case and is real: zoekeating.bandcamp.com does it today.
        session = FakeSession({ARTIST_URL: answer})
        assert resolve_artist_name(ARTIST_URL, session=session) is None

    def test_the_callers_deadline_is_not_swallowed(self):
        class Overdue(Exception):
            pass

        def check():
            raise Overdue()

        session = FakeSession({ARTIST_URL: page(band={"name": "Amelie Lens"})})
        with pytest.raises(Overdue):
            resolve_artist_name(ARTIST_URL, session=session, check_deadline=check)

    def test_a_session_it_opened_itself_is_closed(self):
        # The probe passes no session, so the module opens one -- and must not
        # leak it, whether the fetch worked or not.
        opened = FakeSession({ARTIST_URL: page(band={"name": "Amelie Lens"})})
        with patch("app.bandcamp.requests.Session", return_value=opened):
            assert resolve_artist_name(ARTIST_URL) == "Amelie Lens"
        assert opened.closed is True

    def test_a_session_it_was_handed_is_left_open(self):
        session = FakeSession({ARTIST_URL: page(band={"name": "Amelie Lens"})})
        resolve_artist_name(ARTIST_URL, session=session)
        assert session.closed is False
