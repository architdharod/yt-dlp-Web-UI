"""Tests for the Spotify hand-off: a name off a Spotify page, matched on YouTube Music.

Nothing here touches the network.  The Spotify fetches go through a stand-in
``requests.Session`` (:class:`FakeSession`) whose canned answers are the shapes
the live endpoints really return -- the oEmbed JSON was recorded from
``open.spotify.com/oembed?url=…`` for Radiohead, and the page markup is the
``og:title`` / ``<title>Name | Spotify</title>`` pair the artist page carries --
and the YouTube Music calls go through the same ``FakeYTMusic`` the discography
tests use.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
import urllib3
from pydantic import ValidationError

from app import probe as probe_module
from app.models import (
    ALLOWED_URL_HOSTS,
    MAX_FOLDER_NAME,
    SPOTIFY_ARTIST_MESSAGE,
    DownloadRequest,
    ProbeRequest,
)
from app.probe import (
    SPOTIFY_MATCH_NOTICE,
    SPOTIFY_NAME_MESSAGE,
    SPOTIFY_NO_MATCH_MESSAGE,
    SPOTIFY_NOTHING_PLAYABLE_MESSAGE,
    SPOTIFY_UNREADABLE_MESSAGE,
    SPOTIFY_YTMUSIC_UNAVAILABLE_MESSAGE,
    SpotifyProbeError,
    clear_cache,
    probe,
)
from app.spotify import (
    MAX_RESPONSE_BYTES,
    _TIMEOUT,
    SPOTIFY_HOST,
    UNSUPPORTED_KIND_MESSAGE,
    USER_AGENT,
    SpotifyTarget,
    SpotifyUnavailable,
    is_spotify_url,
    resolve_artist_name,
    spotify_url_target,
)
from app.ytmusic import YouTubeMusicUnavailable, canonical_channel_url, search_artist

from tests.test_probe import fake_ytdl
from tests.test_routes import client, fresh_app  # noqa: F401  (pytest fixtures)
from tests.test_ytmusic import FakeYTMusic, fake_client

ARTIST_ID = "4Z8W4fKeB5YxbusRsdQVPb"
ARTIST_URL = f"https://open.spotify.com/artist/{ARTIST_ID}"
TRACK_URL = "https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp"

# The Glass Beams ids the recorded YouTube Music fixtures were captured under.
GLASS_BEAMS_PAGE_ID = "UCcT6dA3rQ_EhHlG5Wu8mitg"
GLASS_BEAMS_CHANNEL_ID = "UCz2BbywXgCxZcpAiXPlKT4Q"

FIXTURES = Path(__file__).parent / "fixtures" / "ytmusic"


@pytest.fixture(autouse=True)
def _empty_probe_cache():
    """The enumeration cache and the probe semaphore are process-wide."""
    clear_cache()
    probe_module._probe_slots = asyncio.Semaphore(probe_module.MAX_CONCURRENT_PROBES)
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# A stand-in for requests.Session
# ---------------------------------------------------------------------------


class FakeRaw:
    """The urllib3 response body, read the way :func:`_get_text` reads it.

    ``read1`` hands back at most *amt* bytes and an empty ``bytes`` at the end,
    which is what the loop breaks on.
    """

    def __init__(self, body: bytes):
        self._body = body
        self._offset = 0
        self.reads = 0

    def read1(self, amt=-1, decode_content=True):
        self.reads += 1
        size = len(self._body) - self._offset if amt is None or amt < 0 else amt
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class FakeResponse:
    """One canned answer.

    *content_type* is the ``Content-Type`` header verbatim, so a test can leave
    the charset undeclared -- which is the case Spotify actually serves -- or
    declare one.  ``encoding`` is deliberately absent: :func:`_get_text` reads
    the header rather than ``requests``' ISO-8859-1 guess.
    """

    def __init__(self, status_code=200, body=b"", content_type=None):
        self.status_code = status_code
        self.raw = FakeRaw(body)
        self.headers = {} if content_type is None else {"Content-Type": content_type}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


class FakeSession:
    """``requests.Session`` with canned answers, keyed by URL.

    A value that is an ``Exception`` is raised, which is how a timeout is
    written; anything else is the response.  Every call is recorded so the
    tests can assert on the request itself -- the timeout, the redirect policy
    and the User-Agent are as much of the contract as the body is.
    """

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        answer = self.responses.get(url)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return FakeResponse(status_code=404)
        return answer


def oembed(name: str) -> FakeResponse:
    """The oEmbed answer, in the shape the live endpoint returns."""
    return FakeResponse(
        body=json.dumps(
            {
                "html": "<iframe …></iframe>",
                "iframe_url": f"https://{SPOTIFY_HOST}/embed/artist/{ARTIST_ID}",
                "width": 456,
                "height": 352,
                "version": "1.0",
                "provider_name": "Spotify",
                "provider_url": "https://spotify.com",
                "type": "rich",
                "title": name,
                "thumbnail_url": "https://image-cdn-ak.spotifycdn.com/image/abc",
            }
        ).encode()
    )


def page(og: str | None = None, title: str | None = None, tail: bytes = b"") -> FakeResponse:
    """An artist page carrying whichever of the two names is asked for."""
    head = "<!DOCTYPE html><html><head>"
    if og is not None:
        head += f'<meta property="og:title" content="{og}"/>'
    if title is not None:
        head += f"<title>{title}</title>"
    head += "</head><body>"
    return FakeResponse(body=head.encode() + tail + b"</body></html>")


def _raw_failure(error: Exception) -> FakeResponse:
    """A 200 whose body dies partway through, the way a dropped stream does."""
    response = FakeResponse(body=b"<html>")

    class DyingRaw:
        def __init__(self):
            self.reads = 0

        def read1(self, amt=-1, decode_content=True):
            self.reads += 1
            if self.reads > 1:
                raise error
            return b"<html>"

    response.raw = DyingRaw()
    return response


OEMBED_URL = f"https://{SPOTIFY_HOST}/oembed"


def _session(oembed_answer=None, page_answer=None) -> FakeSession:
    return FakeSession({OEMBED_URL: oembed_answer, ARTIST_URL: page_answer})


# ---------------------------------------------------------------------------
# URL recognition
# ---------------------------------------------------------------------------


class TestUrlRecognition:
    @pytest.mark.parametrize(
        "url",
        [
            ARTIST_URL,
            f"{ARTIST_URL}?si=8c3f1b2d4e5f6a7b",
            f"{ARTIST_URL}#play",
            f"https://open.spotify.com/intl-de/artist/{ARTIST_ID}",
            f"https://open.spotify.com/intl-pt-br/artist/{ARTIST_ID}?si=x",
            f"http://OPEN.SPOTIFY.COM/artist/{ARTIST_ID}",
            f"  {ARTIST_URL}  ",
        ],
    )
    def test_an_artist_url_is_recognised_however_it_was_shared(self, url):
        target = spotify_url_target(url)

        assert target == SpotifyTarget(kind="artist", id=ARTIST_ID)
        assert target.is_artist
        # Whatever was pasted, one URL is fetched: no locale, no query.
        assert target.canonical_url == ARTIST_URL

    @pytest.mark.parametrize(
        "url, kind",
        [
            (TRACK_URL, "track"),
            ("https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3", "album"),
            ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "playlist"),
            ("https://open.spotify.com/show/4rOoJ6Egrf8K2IrywzwOMk", "show"),
            (
                f"https://open.spotify.com/intl-fr/episode/{'a' * 22}",
                "episode",
            ),
        ],
    )
    def test_the_other_kinds_are_recognised_as_spotify_and_not_as_artists(
        self, url, kind
    ):
        """Recognised *as Spotify*, so the probe can name what is supported."""
        target = spotify_url_target(url)

        assert target is not None
        assert target.kind == kind
        assert not target.is_artist

    @pytest.mark.parametrize(
        "url",
        [
            "https://open.spotify.com/artist/short",
            f"https://open.spotify.com/artist/{'a' * 23}",
            "https://open.spotify.com/artist/../../etc/passwd",
            f"https://open.spotify.com/artist/{ARTIST_ID}/related",
            "https://open.spotify.com/",
            "https://open.spotify.com/search/radiohead",
            f"https://open.spotify.com/nonsense/{ARTIST_ID}",
        ],
    )
    def test_an_unparseable_spotify_url_is_still_a_spotify_url(self, url):
        assert spotify_url_target(url) is None
        assert is_spotify_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            f"https://spotify.com/artist/{ARTIST_ID}",
            f"https://accounts.spotify.com/artist/{ARTIST_ID}",
            f"https://open.spotify.com.evil.test/artist/{ARTIST_ID}",
            f"spotify:artist:{ARTIST_ID}",
            "https://youtube.com/@bonobo",
        ],
    )
    def test_other_hosts_and_schemes_are_not_spotify(self, url):
        assert not is_spotify_url(url)
        assert spotify_url_target(url) is None


# ---------------------------------------------------------------------------
# Reading the name
# ---------------------------------------------------------------------------


class TestResolveArtistName:
    target = SpotifyTarget(kind="artist", id=ARTIST_ID)

    def test_the_oembed_title_is_the_name(self):
        session = _session(oembed_answer=oembed("Radiohead"))

        assert resolve_artist_name(self.target, session=session) == "Radiohead"
        # One call: the page is never fetched when oEmbed answered.
        assert [call["url"] for call in session.calls] == [OEMBED_URL]
        assert session.calls[0]["params"] == {"url": ARTIST_URL}

    def test_the_request_is_bounded_and_follows_nothing(self):
        session = _session(oembed_answer=oembed("Radiohead"))
        resolve_artist_name(self.target, session=session)

        call = session.calls[0]
        assert call["allow_redirects"] is False
        assert call["timeout"] == _TIMEOUT
        assert call["stream"] is True
        assert call["headers"]["User-Agent"] == USER_AGENT

    def test_a_404_from_oembed_falls_through_to_the_page(self):
        session = _session(oembed_answer=None, page_answer=page(og="Radiohead"))

        assert resolve_artist_name(self.target, session=session) == "Radiohead"
        assert [call["url"] for call in session.calls] == [OEMBED_URL, ARTIST_URL]

    def test_the_page_title_suffix_comes_off(self):
        session = _session(page_answer=page(title="Glass Beams | Spotify"))

        assert resolve_artist_name(self.target, session=session) == "Glass Beams"

    def test_og_title_wins_over_the_page_title(self):
        session = _session(page_answer=page(og="Bonobo", title="Bonobo | Spotify"))

        assert resolve_artist_name(self.target, session=session) == "Bonobo"

    @pytest.mark.parametrize(
        "markup, expected",
        [
            ({"title": "Simon &amp; Garfunkel | Spotify"}, "Simon & Garfunkel"),
            ({"og": "Simon &amp; Garfunkel"}, "Simon & Garfunkel"),
            ({"title": "Sigur R&oacute;s | Spotify"}, "Sigur Rós"),
        ],
    )
    def test_html_entities_are_unescaped(self, markup, expected):
        session = _session(page_answer=page(**markup))

        assert resolve_artist_name(self.target, session=session) == expected

    def test_a_non_json_oembed_answer_falls_through(self):
        session = FakeSession(
            {
                OEMBED_URL: FakeResponse(body=b"<html>nope</html>"),
                ARTIST_URL: page(og="Radiohead"),
            }
        )

        assert resolve_artist_name(self.target, session=session) == "Radiohead"

    def test_a_page_with_no_name_in_it_is_none(self):
        session = _session(page_answer=page())

        assert resolve_artist_name(self.target, session=session) is None

    def test_a_redirect_is_not_followed_and_holds_no_name(self):
        """A 3xx is an answer we refuse to chase, not a hop to another host."""
        moved = FakeResponse(status_code=302)
        session = FakeSession({OEMBED_URL: moved, ARTIST_URL: moved})

        assert resolve_artist_name(self.target, session=session) is None

    def test_only_the_first_bytes_of_a_page_are_read(self):
        """A name past the cap is not found, and the read stops there."""
        session = _session(
            page_answer=page(tail=b"x" * MAX_RESPONSE_BYTES + b"<title>Late | Spotify</title>")
        )

        assert resolve_artist_name(self.target, session=session) is None

    def test_a_name_inside_the_cap_survives_a_huge_page(self):
        session = _session(
            page_answer=page(og="Radiohead", tail=b"x" * (MAX_RESPONSE_BYTES * 2))
        )

        assert resolve_artist_name(self.target, session=session) == "Radiohead"

    def test_a_timeout_is_unavailable_rather_than_no_name(self):
        session = FakeSession({OEMBED_URL: requests.Timeout("timed out")})

        with pytest.raises(SpotifyUnavailable):
            resolve_artist_name(self.target, session=session)

    def test_a_connection_error_on_the_page_is_unavailable_too(self):
        session = FakeSession(
            {OEMBED_URL: None, ARTIST_URL: requests.ConnectionError("refused")}
        )

        with pytest.raises(SpotifyUnavailable):
            resolve_artist_name(self.target, session=session)

    @pytest.mark.parametrize(
        "error",
        [
            urllib3.exceptions.ProtocolError("connection broken: truncated body"),
            urllib3.exceptions.ReadTimeoutError(None, "u", "Read timed out"),
            urllib3.exceptions.DecodeError("corrupt gzip"),
        ],
    )
    def test_a_raw_read_failure_is_unavailable_rather_than_a_500(self, error):
        """``requests`` never sees these: we read ``raw`` ourselves.

        ``requests`` only translates urllib3's errors inside ``iter_content``,
        so a body that dies mid-read raises the urllib3 error verbatim.  It is
        not a ``RequestException``, and left uncaught it would reach the route
        as a 500 rather than as "Spotify could not be reached".
        """
        session = _session(oembed_answer=_raw_failure(error))

        with pytest.raises(SpotifyUnavailable):
            resolve_artist_name(self.target, session=session)

    def test_a_raw_read_failure_on_the_page_is_unavailable_too(self):
        """The second fetch reads its body the same way, so it fails the same."""
        session = FakeSession(
            {
                OEMBED_URL: None,
                ARTIST_URL: _raw_failure(
                    urllib3.exceptions.ReadTimeoutError(None, "u", "Read timed out")
                ),
            }
        )

        with pytest.raises(SpotifyUnavailable):
            resolve_artist_name(self.target, session=session)

    def test_an_undeclared_charset_is_utf_8_not_latin_1(self):
        """``requests`` would call this ISO-8859-1 and hand back ``Ã\x81rtist``."""
        session = _session(page_answer=page(og="Ártist"))

        assert resolve_artist_name(self.target, session=session) == "Ártist"

    def test_a_declared_charset_is_honoured(self):
        session = _session(
            page_answer=FakeResponse(
                body="<html><head><title>Ártist | Spotify</title></head>".encode(
                    "iso-8859-1"
                ),
                content_type="text/html; charset=iso-8859-1",
            )
        )

        assert resolve_artist_name(self.target, session=session) == "Ártist"

    def test_an_unknown_charset_falls_back_to_utf_8(self):
        """A header naming a charset Python has never heard of."""
        session = _session(
            page_answer=FakeResponse(
                body="<html><head><title>Ártist | Spotify</title></head>".encode(),
                content_type="text/html; charset=nonsense",
            )
        )

        assert resolve_artist_name(self.target, session=session) == "Ártist"

    def test_the_deadline_is_checked_after_every_read(self):
        """A dribbling server is bounded by the probe, not by the read timeout.

        The read timeout resets on every socket read, so a server handing over
        one byte at a time can keep a single 8 KiB chunk in flight for hours.
        Checking after each read is what stops it, and whatever the check
        raises is the probe's answer -- not a ``requests`` exception, so it
        must come out untouched rather than as ``SpotifyUnavailable``.
        """

        class Dribble:
            def __init__(self):
                self.reads = 0

            def read1(self, amt=-1, decode_content=True):
                self.reads += 1
                return b"x"

        class Stalled(Exception):
            pass

        def check_deadline():
            if raw.reads >= 3:
                raise Stalled("probe deadline")

        response = FakeResponse()
        raw = Dribble()
        response.raw = raw
        session = FakeSession({OEMBED_URL: response})

        with pytest.raises(Stalled):
            resolve_artist_name(
                self.target, session=session, check_deadline=check_deadline
            )

        # It stopped where the deadline said so, nowhere near the byte cap.
        assert raw.reads == 3


# ---------------------------------------------------------------------------
# Searching YouTube Music by name
# ---------------------------------------------------------------------------


class FakeSearchClient(FakeYTMusic):
    """``FakeYTMusic`` that also answers ``search``."""

    def __init__(self, results=None, **kwargs):
        super().__init__(**kwargs)
        self.results = results
        self.searches: list[tuple[str, str | None, int]] = []

    def search(self, query, filter=None, limit=20):
        self.searches.append((query, filter, limit))
        if isinstance(self.results, Exception):
            raise self.results
        return self.results if self.results is not None else []


def _artist_row(name="Glass Beams", browse_id=GLASS_BEAMS_PAGE_ID):
    """One row of ``search(filter="artists")``, in the live shape."""
    return {
        "category": "Artists",
        "resultType": "artist",
        "artist": name,
        "shuffleId": "RDAOz2vRvq9X4bJfQaHpPPzBQw",
        "radioId": "RDEMz2vRvq9X4bJfQaHpPPzBQw",
        "browseId": browse_id,
        "thumbnails": [{"url": "https://yt3.googleusercontent.com/x", "width": 60}],
    }


class TestSearchArtist:
    def test_the_top_row_is_the_match(self):
        client = FakeSearchClient(results=[_artist_row(), _artist_row("Glass Animals", "UC" + "b" * 22)])

        assert search_artist("Glass Beams", client=client) == (
            GLASS_BEAMS_PAGE_ID,
            "Glass Beams",
        )
        # Artist-filtered, and only the row that is going to be read is asked
        # for: there is no picker to fill.
        assert client.searches == [("Glass Beams", "artists", 1)]

    def test_no_results_is_no_match(self):
        assert search_artist("Nobody At All", client=FakeSearchClient(results=[])) is None

    def test_a_row_without_a_channel_id_is_no_match(self):
        """Not a reason to take the second row: that would be a silent picker."""
        client = FakeSearchClient(
            results=[{"resultType": "artist", "browseId": "MPREb_notachannel"}, _artist_row()]
        )

        assert search_artist("Glass Beams", client=client) is None

    def test_a_blank_name_is_never_searched_for(self):
        client = FakeSearchClient(results=[_artist_row()])

        assert search_artist("   ", client=client) is None
        assert client.searches == []

    def test_an_unparseable_answer_is_no_match(self):
        client = FakeSearchClient(results=KeyError("musicShelfRenderer"))

        assert search_artist("Glass Beams", client=client) is None

    def test_an_unreachable_search_is_raised(self):
        client = FakeSearchClient(results=requests.ConnectionError("refused"))

        with pytest.raises(YouTubeMusicUnavailable):
            search_artist("Glass Beams", client=client)

    def test_the_name_falls_back_to_the_query_when_the_row_has_none(self):
        client = FakeSearchClient(results=[{"browseId": GLASS_BEAMS_PAGE_ID}])

        assert search_artist("Glass Beams", client=client) == (
            GLASS_BEAMS_PAGE_ID,
            "Glass Beams",
        )


# ---------------------------------------------------------------------------
# The probe hand-off
# ---------------------------------------------------------------------------


def _glass_beams_client(**kwargs):
    """The recorded Glass Beams discography, answering a search for it."""
    artist = json.loads((FIXTURES / "glass_beams_artist.json").read_text())
    albums = {
        path.name.removeprefix("glass_beams_album_").removesuffix(".json"): json.loads(
            path.read_text()
        )
        for path in FIXTURES.glob("glass_beams_album_*.json")
    }
    return FakeSearchClient(
        results=[_artist_row()],
        artists={GLASS_BEAMS_PAGE_ID: artist},
        albums=albums,
        **kwargs,
    )


def _spotify(session):
    """Make the probe's Spotify fetches use *session*."""
    return patch(
        "app.probe.resolve_artist_name",
        lambda target, **kwargs: resolve_artist_name(target, session=session, **kwargs),
    )


class TestProbeHandoff:
    async def test_a_spotify_artist_url_previews_the_youtube_music_discography(self):
        client = _glass_beams_client()
        session = _session(oembed_answer=oembed("Glass Beams"))

        with _spotify(session), fake_client(client), fake_ytdl({}) as ydl:
            result = await probe(ARTIST_URL)

        # yt-dlp is never asked about a Spotify URL: it has no extractor for
        # one, and the children carry YouTube watch URLs instead.
        assert ydl.calls == []
        # The pasted URL is what comes back: it is what the bulk submit sends.
        assert result.url == ARTIST_URL
        # The editable field carries the name Spotify gave.
        assert result.artist == "Glass Beams"
        assert [(row.title, row.album) for row in result.rows] == [
            ("Horizon", "Mahal"),
            ("Mahal", "Mahal"),
            ("Orb", "Mahal"),
            ("Snake Oil", "Mahal"),
            ("Black Sand", "Mahal"),
        ]
        # The release was read, so a null album would mean "no album" rather
        # than "not known".
        assert all(row.album_final for row in result.rows)
        assert SPOTIFY_MATCH_NOTICE.format(name="Glass Beams") in result.notices
        # yt-dlp was never asked about the Spotify URL.
        assert client.searches == [("Glass Beams", "artists", 1)]

    async def test_the_name_from_spotify_is_what_the_form_shows(self):
        """Even when YouTube Music spells the artist differently."""
        client = _glass_beams_client()
        session = _session(oembed_answer=oembed("Glass  Beams"))

        with _spotify(session), fake_client(client), fake_ytdl({}):
            result = await probe(ARTIST_URL)

        assert result.artist == "Glass  Beams"
        # The notice names the artist actually enumerated, not the paste.
        assert SPOTIFY_MATCH_NOTICE.format(name="Glass Beams") in result.notices

    async def test_an_absurd_page_title_is_capped_before_it_is_used(self):
        """A page title is bounded by nothing but the response byte cap."""
        client = _glass_beams_client()
        session = _session(oembed_answer=oembed("A" * 5000))

        with _spotify(session), fake_client(client), fake_ytdl({}):
            result = await probe(ARTIST_URL)

        # The query that went to YouTube Music, not only the folder it becomes.
        assert len(client.searches[0][0]) <= MAX_FOLDER_NAME
        assert len(result.artist) <= MAX_FOLDER_NAME

    async def test_an_absurd_page_title_is_capped_in_the_error_too(self):
        client = FakeSearchClient(results=[])
        session = _session(oembed_answer=oembed("A" * 5000))

        with _spotify(session), fake_client(client), fake_ytdl({}):
            with pytest.raises(SpotifyProbeError) as excinfo:
                await probe(ARTIST_URL)

        assert "A" * 5000 not in str(excinfo.value)
        assert str(excinfo.value) == SPOTIFY_NO_MATCH_MESSAGE.format(
            name="A" * MAX_FOLDER_NAME
        )

    async def test_the_enumeration_is_shared_with_the_youtube_music_url(self):
        client = _glass_beams_client()
        session = _session(oembed_answer=oembed("Glass Beams"))

        with _spotify(session), fake_client(client), fake_ytdl({}):
            first = await probe(ARTIST_URL)
            # Both spellings of the channel: the one searched for and the one
            # the artist page answered with.
            second = await probe(canonical_channel_url(GLASS_BEAMS_PAGE_ID))
            third = await probe(canonical_channel_url(GLASS_BEAMS_CHANNEL_ID))

        assert client.artist_calls == [GLASS_BEAMS_PAGE_ID]
        assert first.rows == second.rows == third.rows
        # The Spotify framing stays on the Spotify URL: a channel probe that
        # hit the shared cache gets the plain discography.
        assert second.artist == third.artist == "Glass Beams"
        assert second.notices == third.notices == ()

    async def test_the_spotify_url_is_answered_from_the_cache_a_second_time(self):
        client = _glass_beams_client()
        session = _session(oembed_answer=oembed("Glass Beams"))

        with _spotify(session), fake_client(client), fake_ytdl({}):
            first = await probe(ARTIST_URL)
            second = await probe(ARTIST_URL)

        assert first == second
        assert len(session.calls) == 1
        assert client.searches == [("Glass Beams", "artists", 1)]

    @pytest.mark.parametrize(
        "url",
        [
            TRACK_URL,
            "https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3",
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "https://open.spotify.com/",
            "https://open.spotify.com/artist/short",
        ],
    )
    async def test_anything_but_an_artist_page_names_what_is_supported(self, url):
        with fake_ytdl({}):
            with pytest.raises(SpotifyProbeError) as excinfo:
                await probe(url)

        assert str(excinfo.value) == UNSUPPORTED_KIND_MESSAGE

    async def test_an_unreadable_name_is_an_error_rather_than_an_empty_preview(self):
        session = _session(page_answer=page())

        with _spotify(session), fake_ytdl({}):
            with pytest.raises(SpotifyProbeError) as excinfo:
                await probe(ARTIST_URL)

        assert str(excinfo.value) == SPOTIFY_NAME_MESSAGE

    async def test_an_unreachable_spotify_is_an_error(self):
        session = FakeSession({OEMBED_URL: requests.Timeout("timed out")})

        with _spotify(session), fake_ytdl({}):
            with pytest.raises(SpotifyProbeError) as excinfo:
                await probe(ARTIST_URL)

        assert str(excinfo.value) == SPOTIFY_NAME_MESSAGE

    async def test_no_youtube_music_match_names_the_artist(self):
        client = FakeSearchClient(results=[])
        session = _session(oembed_answer=oembed("Glass Beams"))

        with _spotify(session), fake_client(client), fake_ytdl({}):
            with pytest.raises(SpotifyProbeError) as excinfo:
                await probe(ARTIST_URL)

        assert str(excinfo.value) == SPOTIFY_NO_MATCH_MESSAGE.format(name="Glass Beams")

    async def test_a_match_that_is_not_an_artist_page_is_no_match(self):
        """The search said artist and ``get_artist`` disagreed."""
        client = FakeSearchClient(results=[_artist_row()], artists={})
        session = _session(oembed_answer=oembed("Glass Beams"))

        with _spotify(session), fake_client(client), fake_ytdl({}):
            with pytest.raises(SpotifyProbeError) as excinfo:
                await probe(ARTIST_URL)

        assert str(excinfo.value) == SPOTIFY_NO_MATCH_MESSAGE.format(name="Glass Beams")

    async def test_an_unreachable_youtube_music_is_an_error_not_a_flat_listing(self):
        """There is no fallback: yt-dlp cannot read a Spotify URL at all."""
        client = FakeSearchClient(results=requests.ConnectionError("refused"))
        session = _session(oembed_answer=oembed("Glass Beams"))

        with _spotify(session), fake_client(client), fake_ytdl({}):
            with pytest.raises(SpotifyProbeError) as excinfo:
                await probe(ARTIST_URL)

        assert str(excinfo.value) == SPOTIFY_YTMUSIC_UNAVAILABLE_MESSAGE.format(
            name="Glass Beams"
        )

    async def test_an_artist_with_nothing_playable_is_an_error(self):
        artist = {
            "name": "Glass Beams",
            "channelId": GLASS_BEAMS_PAGE_ID,
            "albums": {"results": [{"browseId": "MPREb_empty", "title": "Mahal"}]},
        }
        album = {"title": "Mahal", "type": "EP", "tracks": [{"videoId": None, "title": "Ghost"}]}
        client = FakeSearchClient(
            results=[_artist_row()],
            artists={GLASS_BEAMS_PAGE_ID: artist},
            albums={"MPREb_empty": album},
        )
        session = _session(oembed_answer=oembed("Glass Beams"))

        with _spotify(session), fake_client(client), fake_ytdl({}):
            with pytest.raises(SpotifyProbeError) as excinfo:
                await probe(ARTIST_URL)

        assert str(excinfo.value) == SPOTIFY_NOTHING_PLAYABLE_MESSAGE.format(
            name="Glass Beams"
        )

    async def test_an_unexpected_library_failure_is_a_400_not_a_500(self):
        client = _glass_beams_client()
        session = _session(oembed_answer=oembed("Glass Beams"))

        def boom(*args, **kwargs):
            raise RuntimeError("ytmusicapi changed shape")

        with _spotify(session), fake_client(client), fake_ytdl({}), patch(
            "app.probe.fetch_artist", boom
        ):
            with pytest.raises(SpotifyProbeError) as excinfo:
                await probe(ARTIST_URL)

        # Its own sentence: an artist *was* matched, so "nothing matches this
        # name" would send the user looking for a problem that is not there.
        assert str(excinfo.value) == SPOTIFY_UNREADABLE_MESSAGE.format(
            name="Glass Beams"
        )

    async def test_the_same_artist_pasted_three_ways_is_looked_up_once(self):
        """``?si=`` and a locale prefix are the same artist, and pay once."""
        client = _glass_beams_client()
        session = _session(oembed_answer=oembed("Glass Beams"))
        spellings = [
            ARTIST_URL,
            f"{ARTIST_URL}?si=abcdef",
            f"https://{SPOTIFY_HOST}/intl-de/artist/{ARTIST_ID}",
        ]

        with _spotify(session), fake_client(client), fake_ytdl({}):
            results = [await probe(url) for url in spellings]

        assert len(session.calls) == 1
        assert len(client.searches) == 1
        notice = SPOTIFY_MATCH_NOTICE.format(name="Glass Beams")
        for url, result in zip(spellings, results):
            # The cached view keeps the Spotify framing, and each answer keeps
            # the URL that was actually pasted.
            assert result.url == url
            assert result.artist == "Glass Beams"
            assert notice in result.notices


# ---------------------------------------------------------------------------
# The API edge
# ---------------------------------------------------------------------------


class TestApiEdge:
    def test_the_allowlist_carries_the_one_spotify_host(self):
        assert SPOTIFY_HOST in ALLOWED_URL_HOSTS
        assert "spotify.com" not in ALLOWED_URL_HOSTS

    def test_the_probe_accepts_a_spotify_artist_url(self):
        assert ProbeRequest(url=ARTIST_URL).url == ARTIST_URL

    def test_a_single_download_refuses_a_spotify_artist_url(self):
        with pytest.raises(ValidationError) as excinfo:
            DownloadRequest(url=ARTIST_URL)

        (error,) = excinfo.value.errors()
        assert error["type"] == "spotify_artist_url"
        assert error["msg"] == SPOTIFY_ARTIST_MESSAGE

    def test_a_single_download_refuses_every_other_spotify_url(self):
        with pytest.raises(ValidationError) as excinfo:
            DownloadRequest(url=TRACK_URL)

        (error,) = excinfo.value.errors()
        assert error["type"] == "spotify_url"
        assert error["msg"] == UNSUPPORTED_KIND_MESSAGE

    def test_post_download_answers_422_without_touching_yt_dlp(self, client):
        with patch("app.main.extract_metadata") as extract:
            resp = client.post("/download", json={"url": ARTIST_URL})

        assert resp.status_code == 422
        (error,) = [e for e in resp.json()["detail"] if e["loc"][-1] == "url"]
        assert error["msg"] == SPOTIFY_ARTIST_MESSAGE
        assert "Value error" not in error["msg"]
        extract.assert_not_called()

    def test_post_probe_answers_400_with_the_message_unprefixed(self, client):
        resp = client.post("/download/probe", json={"url": TRACK_URL})

        assert resp.status_code == 400
        assert resp.json()["detail"] == UNSUPPORTED_KIND_MESSAGE

    def test_post_probe_answers_400_when_the_name_cannot_be_read(self, client):
        session = _session(page_answer=page())

        with _spotify(session), fake_ytdl({}):
            resp = client.post("/download/probe", json={"url": ARTIST_URL})

        assert resp.status_code == 400
        assert resp.json()["detail"] == SPOTIFY_NAME_MESSAGE

    def test_post_probe_previews_the_matched_discography(self, client):
        ytmusic = _glass_beams_client()
        session = _session(oembed_answer=oembed("Glass Beams"))

        with _spotify(session), fake_client(ytmusic), fake_ytdl({}):
            resp = client.post("/download/probe", json={"url": ARTIST_URL})

        assert resp.status_code == 200
        preview = resp.json()["preview"]
        assert preview["artist"] == "Glass Beams"
        assert preview["url"] == ARTIST_URL
        assert len(preview["rows"]) == 5
        assert SPOTIFY_MATCH_NOTICE.format(name="Glass Beams") in preview["notices"]

    def test_the_bulk_parent_may_carry_the_spotify_url(self, client):
        """It is the parent's display URL; the children carry watch URLs."""
        resp = client.post(
            "/download/bulk",
            json={
                "url": ARTIST_URL,
                "artist": "Glass Beams",
                "tracks": [
                    {
                        "id": "youtube:wLjq5oUrc7Q",
                        "url": "https://music.youtube.com/watch?v=wLjq5oUrc7Q",
                        "title": "Horizon",
                        "album": "Mahal",
                    }
                ],
            },
        )

        assert resp.status_code == 200
        assert resp.json()["url"] == ARTIST_URL

    def test_a_bulk_child_may_not(self, client):
        """A child is handed to yt-dlp exactly as a single download is."""
        body = {
            "url": ARTIST_URL,
            "artist": "Glass Beams",
            "tracks": [{"url": TRACK_URL, "title": "Horizon"}],
        }

        resp = client.post("/download/bulk", json=body)

        assert resp.status_code == 422
        (error,) = [e for e in resp.json()["detail"] if e["loc"][-1] == "url"]
        assert error["loc"] == ["body", "tracks", 0, "url"]
        assert error["type"] == "spotify_url"

        body["tracks"] = [
            {"url": "https://music.youtube.com/watch?v=wLjq5oUrc7Q", "title": "Horizon"}
        ]
        resp = client.post("/download/bulk", json=body)

        assert resp.status_code == 200
        assert resp.json()["url"] == ARTIST_URL
