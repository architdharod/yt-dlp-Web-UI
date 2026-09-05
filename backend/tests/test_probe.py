"""Tests for the collection probe and the on-disk dedup rule.

Every yt-dlp call is stubbed with fixtures shaped like the ones in
``wayfinder/research/source-enumeration.md``: what a flat extraction really
returns per source, down to which fields are missing (Bandcamp entries carry a
URL and nothing else; SoundCloud sets carry ``album`` but no ``title``).
Nothing here touches the network.
"""

import asyncio
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest
import yt_dlp
from mutagen.flac import FLAC

from app import probe as probe_module
from app.dedup import DedupCandidate, find_in_library
from app.downloader import _source_id
from app.models import (
    MAX_COLLECTION_TRACKS,
    MAX_FOLDER_NAME,
    MAX_PATH_LENGTH,
    MAX_REASON,
    MAX_SOURCE_ID,
    MAX_TRACK_TITLE,
    BulkDownloadRequest,
    BulkTrack,
    CollectionPreview,
    PreviewRow,
    ProbeRequest,
)
from app.probe import (
    BANDCAMP_NOTICE,
    CollectionTooLarge,
    EmptyCollection,
    Enumeration,
    ProbeError,
    ProbeTimeout,
    SingleTrack,
    clear_cache,
    flat_source_id,
    probe,
)
from tests.conftest import minimal_flac_bytes


@pytest.fixture(autouse=True)
def _empty_probe_cache():
    """The enumeration cache is process-wide; no test may inherit another's.

    The concurrency semaphore is process-wide too, and an ``asyncio.Semaphore``
    binds itself to the first event loop that *contends* for it.  The app has
    one loop for its whole life; the test suite has one per test, so the
    semaphore is replaced between tests rather than carried across loops.
    """
    clear_cache()
    probe_module._probe_slots = asyncio.Semaphore(probe_module.MAX_CONCURRENT_PROBES)
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# A stand-in for yt-dlp
# ---------------------------------------------------------------------------


class _FakeYoutubeDL:
    """``yt_dlp.YoutubeDL`` with a dict of canned answers.

    ``responses`` maps a URL to the info dict it extracts to; an ``Exception``
    value is raised instead, and a callable is called (which is how the timeout
    test makes one extraction slow).
    """

    responses: dict = {}
    calls: list = []
    options: list = []

    def __init__(self, opts):
        type(self).options.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        type(self).calls.append(url)
        value = type(self).responses.get(url)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value


@contextmanager
def fake_ytdl(responses: dict):
    """Replace yt-dlp for the duration of the block."""
    _FakeYoutubeDL.responses = dict(responses)
    _FakeYoutubeDL.calls = []
    _FakeYoutubeDL.options = []
    with patch("app.probe.yt_dlp.YoutubeDL", _FakeYoutubeDL):
        yield _FakeYoutubeDL


# ---------------------------------------------------------------------------
# Fixtures shaped like the research doc
# ---------------------------------------------------------------------------

SINGLE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
SINGLE_INFO = {
    "id": "dQw4w9WgXcQ",
    "extractor": "youtube",
    "title": "Kiara",
    "duration": 213.0,
    "thumbnail": "https://img.youtube.com/thumb.jpg",
    "uploader": "Bonobo - Topic",
    "album": "Black Sands",
}


def _youtube_entry(index: int, **overrides) -> dict:
    entry = {
        "_type": "url",
        "ie_key": "Youtube",
        "id": f"vid{index}",
        "title": f"Track {index}",
        "url": f"https://www.youtube.com/watch?v=vid{index}",
        "duration": 100 + index,
        "thumbnails": [{"url": f"https://img/{index}.jpg"}],
    }
    entry.update(overrides)
    return entry


PLAYLIST_URL = "https://www.youtube.com/playlist?list=PL123"
PLAYLIST_INFO = {
    "_type": "playlist",
    "id": "PL123",
    "extractor_key": "YoutubeTab",
    "title": "Chill mix",
    "channel": "Bonobo - Topic",
    "entries": [_youtube_entry(1), _youtube_entry(2)],
}

OLAK_URL = "https://www.youtube.com/playlist?list=OLAK5uy_album"
# The real shape (yt-dlp 2026.08.19): an auto-generated album playlist has no
# top-level channel/uploader at all, its title is the *album*, and the artist
# is only on the entries, as the "- Topic" channel.
OLAK_INFO = {
    "_type": "playlist",
    "id": "OLAK5uy_album",
    "extractor_key": "YoutubeTab",
    "title": "Black Sands",
    "channel": None,
    "uploader": None,
    "channel_id": None,
    "entries": [
        _youtube_entry(1, channel="Bonobo - Topic"),
        _youtube_entry(2, channel="Bonobo - Topic"),
    ],
}


# ===========================================================================
# Single track vs collection
# ===========================================================================


class TestProbeClassification:
    async def test_a_single_video_is_a_track(self):
        with fake_ytdl({SINGLE_URL: SINGLE_INFO}):
            result = await probe(SINGLE_URL)

        assert isinstance(result, SingleTrack)
        assert result.title == "Kiara"
        assert result.duration == 213.0
        assert result.thumbnail_url == "https://img.youtube.com/thumb.jpg"
        # The "- Topic" suffix of an auto-generated artist channel is not part
        # of the artist's name.
        assert result.artist == "Bonobo"
        assert result.album == "Black Sands"

    async def test_a_playlist_is_a_collection_with_one_row_per_entry(self):
        with fake_ytdl({PLAYLIST_URL: PLAYLIST_INFO}):
            result = await probe(PLAYLIST_URL)

        assert isinstance(result, Enumeration)
        assert result.source == "youtube"
        assert result.title == "Chill mix"
        assert result.artist == "Bonobo"
        assert [row.title for row in result.rows] == ["Track 1", "Track 2"]
        assert [row.duration for row in result.rows] == [101, 102]
        assert [row.thumbnail_url for row in result.rows] == [
            "https://img/1.jpg",
            "https://img/2.jpg",
        ]

    async def test_a_plain_playlist_groups_nothing(self):
        """A bag of videos is not an album; its rows become loose Singles."""
        with fake_ytdl({PLAYLIST_URL: PLAYLIST_INFO}):
            result = await probe(PLAYLIST_URL)

        assert [row.album for row in result.rows] == [None, None]

    async def test_an_olak_playlist_is_an_album(self):
        with fake_ytdl({OLAK_URL: OLAK_INFO}):
            result = await probe(OLAK_URL)

        assert [row.album for row in result.rows] == ["Black Sands", "Black Sands"]

    async def test_a_failed_extraction_is_a_probe_error(self):
        with fake_ytdl({PLAYLIST_URL: yt_dlp.utils.DownloadError("Video unavailable")}):
            with pytest.raises(ProbeError, match="Video unavailable"):
                await probe(PLAYLIST_URL)

    async def test_a_collection_with_nothing_downloadable_is_refused(self):
        """A Bandcamp subdomain that is not a discography answers with zero
        entries rather than an error; "0 tracks" is not a preview."""
        empty = {"_type": "playlist", "id": "x", "title": "Discography of x", "entries": []}
        with fake_ytdl({PLAYLIST_URL: empty}):
            with pytest.raises(EmptyCollection):
                await probe(PLAYLIST_URL)


# ===========================================================================
# source_id
# ===========================================================================


class TestSuggestedArtist:
    """The artist the preview form is pre-filled with.

    Measured against live yt-dlp output: a YouTube collection almost never
    names its artist in a usable way, and its entries almost always do.
    """

    async def test_an_olak_album_takes_its_artist_from_the_entries(self):
        with fake_ytdl({OLAK_URL: OLAK_INFO}):
            result = await probe(OLAK_URL)

        assert result.artist == "Bonobo"

    async def test_an_olak_album_with_no_artist_anywhere_offers_nothing(self):
        """Its title is the album, so it must never become the artist folder."""
        info = {
            **OLAK_INFO,
            "entries": [_youtube_entry(1), _youtube_entry(2)],
        }
        with fake_ytdl({OLAK_URL: info}):
            result = await probe(OLAK_URL)

        assert result.title == "Black Sands"
        assert result.artist is None

    async def test_a_playlists_by_credit_line_loses_its_prefix(self):
        """A plain playlist's channel is the page's "by <Artist>" credit."""
        info = {
            "_type": "playlist",
            "id": "PLblender",
            "extractor_key": "YoutubeTab",
            "title": "Open Movies",
            "channel": "by Blender",
            "uploader": "by Blender",
            "channel_id": "UCSMOQeBJ2RAnuFungnQOxLg",
            "entries": [
                _youtube_entry(1, channel="Blender"),
                _youtube_entry(2, channel="Blender"),
            ],
        }
        with fake_ytdl({PLAYLIST_URL: info}):
            result = await probe(PLAYLIST_URL)

        assert result.artist == "Blender"

    async def test_a_channel_keeps_a_name_that_starts_with_by(self):
        """"By The Rivers" is a band; the prefix only comes off a playlist."""
        url = "https://www.youtube.com/@bytherivers/videos"
        info = {
            "_type": "playlist",
            "id": "UCbytherivers",
            "extractor_key": "YoutubeTab",
            "title": "By The Rivers - Videos",
            "channel": "By The Rivers",
            "uploader": "By The Rivers",
            "entries": [_youtube_entry(1), _youtube_entry(2)],
        }
        with fake_ytdl({url: info}):
            result = await probe(url)

        assert result.artist == "By The Rivers"

    async def test_the_most_common_entry_artist_wins(self):
        """A guest upload does not rename the whole playlist's artist."""
        info = {
            "_type": "playlist",
            "id": "PLmixed",
            "extractor_key": "YoutubeTab",
            "title": "Mix",
            "entries": [
                _youtube_entry(1, channel="Bonobo - Topic"),
                _youtube_entry(2, channel="Someone Else"),
                _youtube_entry(3, channel="Bonobo - Topic"),
            ],
        }
        with fake_ytdl({PLAYLIST_URL: info}):
            result = await probe(PLAYLIST_URL)

        assert result.artist == "Bonobo"


class TestFlatSourceId:
    def test_a_flat_entry_and_a_downloaded_track_agree(self):
        """Dedup by provenance is worthless unless these two are equal.

        The flat entry names the extractor's *class* (``ie_key="Youtube"``) and
        the downloaded info dict names its ``IE_NAME`` (``"youtube"``); the
        SOURCEID tag has to come out the same either way.
        """
        flat = {"ie_key": "Youtube", "id": "dQw4w9WgXcQ"}
        full = {"extractor": "youtube", "id": "dQw4w9WgXcQ"}
        assert flat_source_id(flat) == _source_id(full) == "youtube:dQw4w9WgXcQ"

    def test_soundcloud_agrees_too(self):
        assert flat_source_id({"ie_key": "Soundcloud", "id": "123"}) == _source_id(
            {"extractor": "soundcloud", "id": "123"}
        )

    def test_a_half_identified_entry_has_no_source_id(self):
        assert flat_source_id({"url": "https://x.bandcamp.com/track/y"}) is None


# ===========================================================================
# Sub-collections and album grouping
# ===========================================================================


class TestSubCollections:
    async def test_a_releases_tab_expands_each_album_once(self):
        releases_url = "https://www.youtube.com/@bonobo/releases"
        releases = {
            "_type": "playlist",
            "id": "UC123",
            "extractor_key": "YoutubeTab",
            "title": "Bonobo - Releases",
            "channel": "Bonobo",
            "entries": [
                {
                    "_type": "url",
                    "ie_key": "YoutubeTab",
                    "id": "OLAK5uy_album",
                    "title": "Black Sands",
                    "url": OLAK_URL,
                }
            ],
        }
        with fake_ytdl({releases_url: releases, OLAK_URL: OLAK_INFO}) as ydl:
            result = await probe(releases_url)

        # A ``/@handle`` URL is offered to YouTube Music first (phase 12), so
        # the channel root is resolved before the flat pass runs.  Here the
        # resolution finds nothing and the walk proceeds exactly as it did.
        assert ydl.calls == [
            "https://www.youtube.com/@bonobo",
            releases_url,
            OLAK_URL,
        ]
        assert [row.album for row in result.rows] == ["Black Sands", "Black Sands"]

    async def test_a_soundcloud_set_carries_its_album(self):
        user_url = "https://soundcloud.com/bonobo"
        set_url = "https://soundcloud.com/bonobo/sets/days-to-come"
        user = {
            "_type": "playlist",
            "id": "9999",
            "extractor_key": "SoundcloudUser",
            "title": "Bonobo (All)",
            "entries": [{"_type": "url", "id": "1", "url": set_url}],
        }
        soundcloud_set = {
            "_type": "playlist",
            "id": "set1",
            "extractor_key": "SoundcloudSet",
            "title": "Days To Come",
            "album": "Days To Come",
            "album_type": "album",
            "entries": [
                {
                    "_type": "url_transparent",
                    "ie_key": "Soundcloud",
                    "id": "111",
                    "url": "https://soundcloud.com/bonobo/kiara",
                    "album": "Days To Come",
                    "album_type": "album",
                }
            ],
        }
        with fake_ytdl({user_url: user, set_url: soundcloud_set}):
            result = await probe(user_url)

        assert result.source == "soundcloud"
        # "<user> (All)" is the page's name, not the artist's.
        assert result.artist == "Bonobo"
        assert [row.album for row in result.rows] == ["Days To Come"]
        assert [row.source_id for row in result.rows] == ["soundcloud:111"]

    async def test_a_bandcamp_artist_expands_albums_and_keeps_loose_tracks(self):
        artist_url = "https://zoekeating.bandcamp.com"
        album_url = "https://zoekeating.bandcamp.com/album/into-the-trees"
        track_url = "https://zoekeating.bandcamp.com/track/optimist"
        artist = {
            "_type": "playlist",
            "id": "zoekeating",
            "extractor_key": "BandcampUser",
            "title": "Discography of zoekeating",
            "entries": [
                {"_type": "url", "url": album_url},
                {"_type": "url", "url": track_url},
            ],
        }
        album = {
            "_type": "playlist",
            "id": "into-the-trees",
            "extractor_key": "BandcampAlbum",
            "title": "Into The Trees",
            "entries": [
                {
                    "_type": "url",
                    "ie_key": "Bandcamp",
                    "id": "77",
                    "title": "Escape Artist",
                    "url": "https://zoekeating.bandcamp.com/track/escape-artist",
                }
            ],
        }
        with fake_ytdl({artist_url: artist, album_url: album}) as ydl:
            result = await probe(artist_url)

        assert ydl.calls == [artist_url, album_url]
        assert result.source == "bandcamp"
        # No display name anywhere on the page; the subdomain is all there is.
        assert result.artist == "zoekeating"
        assert [row.album for row in result.rows] == ["Into The Trees", None]
        # A loose /track/ URL is not expanded: its title costs a full
        # extraction, and the child job resolves it when it runs.
        assert result.rows[1].title is None
        assert result.rows[1].id == track_url

    async def test_bandcamp_gets_its_lossless_notice(self):
        artist_url = "https://zoekeating.bandcamp.com"
        artist = {
            "_type": "playlist",
            "id": "zoekeating",
            "extractor_key": "BandcampUser",
            "title": "Discography of zoekeating",
            "entries": [
                {
                    "_type": "url",
                    "ie_key": "Bandcamp",
                    "id": "1",
                    "title": "Optimist",
                    "url": "https://zoekeating.bandcamp.com/track/optimist",
                }
            ],
        }
        with fake_ytdl({artist_url: artist}):
            result = await probe(artist_url)

        assert BANDCAMP_NOTICE in result.notices


# ===========================================================================
# Unavailable rows and unreadable entries
# ===========================================================================


class TestUnavailableRows:
    async def test_the_flat_pass_flags_what_it_can_see(self):
        info = {
            "_type": "playlist",
            "id": "PL123",
            "extractor_key": "YoutubeTab",
            "title": "Mixed",
            "channel": "Someone",
            "entries": [
                _youtube_entry(1),
                _youtube_entry(2, availability="premium_only"),
                _youtube_entry(3, live_status="is_upcoming"),
                _youtube_entry(4, error="This video is DRM protected"),
            ],
        }
        with fake_ytdl({PLAYLIST_URL: info}):
            result = await probe(PLAYLIST_URL)

        reasons = [row.unavailable_reason for row in result.rows]
        assert reasons == [None, "premium only", "not released yet", "DRM protected"]

    async def test_entries_ignoreerrors_dropped_become_a_notice(self):
        info = {
            "_type": "playlist",
            "id": "PL123",
            "extractor_key": "YoutubeTab",
            "title": "Mixed",
            "channel": "Someone",
            "entries": [_youtube_entry(1), None, None],
        }
        with fake_ytdl({PLAYLIST_URL: info}):
            result = await probe(PLAYLIST_URL)

        assert len(result.rows) == 1
        assert "2 tracks could not be read and were left out" in result.notices


# ===========================================================================
# Caps and the cache
# ===========================================================================


class TestCapsAndCache:
    async def test_more_than_two_thousand_tracks_stops_the_probe(self):
        info = {
            "_type": "playlist",
            "id": "PL123",
            "extractor_key": "YoutubeTab",
            "title": "Everything",
            "channel": "NPR Music",
            "entries": [
                _youtube_entry(index) for index in range(MAX_COLLECTION_TRACKS + 1)
            ],
        }
        with fake_ytdl({PLAYLIST_URL: info}):
            with pytest.raises(CollectionTooLarge) as excinfo:
                await probe(PLAYLIST_URL)

        assert str(excinfo.value).startswith(
            f"This collection has more than {MAX_COLLECTION_TRACKS} tracks"
        )

    async def test_exactly_the_cap_is_allowed(self):
        info = {
            "_type": "playlist",
            "id": "PL123",
            "extractor_key": "YoutubeTab",
            "title": "Everything",
            "channel": "NPR Music",
            "entries": [_youtube_entry(index) for index in range(MAX_COLLECTION_TRACKS)],
        }
        with fake_ytdl({PLAYLIST_URL: info}):
            result = await probe(PLAYLIST_URL)

        assert len(result.rows) == MAX_COLLECTION_TRACKS

    async def test_the_extraction_asks_for_one_row_past_the_cap(self):
        """The stop rule needs to *see* the 2001st row, so pagination stops
        one past the cap rather than at it."""
        with fake_ytdl({PLAYLIST_URL: PLAYLIST_INFO}) as ydl:
            await probe(PLAYLIST_URL)

        opts = ydl.options[0]
        assert opts["playlist_items"] == f"1:{MAX_COLLECTION_TRACKS + 1}"
        assert opts["extract_flat"] == "in_playlist"
        assert opts["ignoreerrors"] is True
        # Inherited from the downloader, and both matter: a watch?v=&list= URL
        # must still be one track, and the generic extractor stays out of reach.
        assert opts["noplaylist"] is True
        assert opts["allowed_extractors"] == ["default", "-generic"]

    async def test_a_second_probe_of_the_same_url_uses_the_cache(self):
        with fake_ytdl({PLAYLIST_URL: PLAYLIST_INFO}) as ydl:
            first = await probe(PLAYLIST_URL)
            second = await probe(PLAYLIST_URL)

        assert ydl.calls == [PLAYLIST_URL]
        assert first == second

    async def test_clearing_the_cache_makes_the_next_probe_extract_again(self):
        with fake_ytdl({PLAYLIST_URL: PLAYLIST_INFO}) as ydl:
            await probe(PLAYLIST_URL)
            clear_cache()
            await probe(PLAYLIST_URL)

        assert ydl.calls == [PLAYLIST_URL, PLAYLIST_URL]

    async def test_only_two_probes_run_at_once(self):
        """A third probe waits for a slot instead of taking a third thread."""
        started: list[str] = []
        started_lock = threading.Lock()
        release = threading.Event()

        def slow_extract(url):
            with started_lock:
                started.append(url)
            release.wait(5)
            return PLAYLIST_INFO

        urls = [f"{PLAYLIST_URL}&n={index}" for index in range(3)]
        with patch("app.probe._extract", slow_extract):
            tasks = [asyncio.create_task(probe(url)) for url in urls]
            try:
                # Long enough for two executor threads to have picked their
                # work up, short enough to keep the test quick.
                await asyncio.sleep(0.3)
                with started_lock:
                    assert len(started) == 2
            finally:
                release.set()
            results = await asyncio.gather(*tasks)

        assert len(started) == 3
        assert all(len(result.rows) == 2 for result in results)

    async def test_a_slow_probe_times_out(self):
        def slow():
            time.sleep(1.0)
            return PLAYLIST_INFO

        with fake_ytdl({PLAYLIST_URL: slow}):
            with pytest.raises((ProbeTimeout, asyncio.TimeoutError)):
                await probe(PLAYLIST_URL, timeout=0)


# ===========================================================================
# Row field caps
# ===========================================================================


class TestRowFieldCaps:
    """A preview row must always fit the bulk submit that sends it back.

    The frontend copies rows verbatim into ``POST /download/bulk``, so a field
    longer than :class:`~app.models.BulkTrack` allows would 422 the whole
    selection over one over-long name from the source.
    """

    async def test_an_over_long_title_and_album_come_back_capped(self):
        info = {
            **PLAYLIST_INFO,
            "entries": [
                _youtube_entry(
                    1,
                    title="T" * 1001,
                    album="A" * 201,
                    thumbnails=[{"url": "https://img/" + "p" * MAX_PATH_LENGTH}],
                )
            ],
        }
        with fake_ytdl({PLAYLIST_URL: info}):
            result = await probe(PLAYLIST_URL)

        (row,) = result.rows
        assert len(row.title) == MAX_TRACK_TITLE
        assert len(row.album) == MAX_FOLDER_NAME
        # An over-long thumbnail URL is dropped rather than truncated: the
        # child job fills ``thumbnail_url or info["thumbnail"]`` in, so None
        # lets the real one through where half a URL would stick as a broken
        # image.
        assert row.thumbnail_url is None

        # The whole point: the capped row is a legal bulk track.
        track = BulkTrack(
            url=row.url,
            title=row.title,
            album=row.album,
            duration=row.duration,
            thumbnail_url=row.thumbnail_url,
            source_id=row.source_id,
        )
        assert track.title == row.title
        assert track.album == row.album

    async def test_truncation_does_not_leave_trailing_whitespace(self):
        info = {
            **PLAYLIST_INFO,
            "entries": [_youtube_entry(1, title="T" * 999 + "  tail")],
        }
        with fake_ytdl({PLAYLIST_URL: info}):
            result = await probe(PLAYLIST_URL)

        (row,) = result.rows
        assert row.title == "T" * 999

    async def test_an_over_long_source_id_is_dropped_not_truncated(self):
        # A capped source id could never equal the uncapped one the downloader
        # writes to SOURCEID, and two ids sharing a 200-char prefix would
        # collapse into one row in the dedup on row id.
        long_id = "x" * MAX_SOURCE_ID
        info = {
            **PLAYLIST_INFO,
            "entries": [
                _youtube_entry(1, id=long_id + "a"),
                _youtube_entry(2, id=long_id + "b"),
            ],
        }
        with fake_ytdl({PLAYLIST_URL: info}):
            result = await probe(PLAYLIST_URL)

        first, second = result.rows
        assert first.source_id is None
        assert second.source_id is None
        assert first.id == first.url
        assert second.id == second.url
        assert first.url != second.url

    async def test_an_over_long_unavailable_reason_is_capped(self):
        info = {
            **PLAYLIST_INFO,
            "entries": [
                _youtube_entry(1, error="E" * (MAX_REASON + 50)),
                # A collection of nothing but unavailable rows is refused, so
                # one good row keeps the preview alive.
                _youtube_entry(2),
            ],
        }
        with fake_ytdl({PLAYLIST_URL: info}):
            result = await probe(PLAYLIST_URL)

        row, _ok = result.rows
        assert len(row.unavailable_reason) == MAX_REASON

        # The whole point: it still fits the response model.
        assert (
            PreviewRow(
                id=row.id,
                url=row.url,
                status="unavailable",
                reason=row.unavailable_reason,
            ).reason
            == row.unavailable_reason
        )

    async def test_an_over_long_suggested_artist_and_title_are_capped(self):
        # The suggestion is echoed straight back into the form, which posts it
        # to /download/probe and then to /download/bulk; an uncapped channel
        # name would 422 both of them.
        long_name = "A" * 250
        info = {
            **PLAYLIST_INFO,
            "title": "T" * (MAX_TRACK_TITLE + 50),
            "entries": [
                _youtube_entry(1, artist=long_name),
                _youtube_entry(2, artist=long_name),
            ],
        }
        with fake_ytdl({PLAYLIST_URL: info}):
            result = await probe(PLAYLIST_URL)

        assert len(result.artist) == MAX_FOLDER_NAME
        assert len(result.title) == MAX_TRACK_TITLE

        # The whole point: both fit every model the suggestion travels through.
        assert (
            CollectionPreview(
                url=result.url,
                title=result.title,
                artist=result.artist,
                source=result.source,
                rows=[],
                total=0,
                in_library=0,
                unavailable=0,
                large=False,
            ).artist
            == result.artist
        )
        assert ProbeRequest(url=PLAYLIST_URL, artist=result.artist).artist == result.artist
        assert (
            BulkDownloadRequest(
                url=PLAYLIST_URL,
                artist=result.artist,
                title=result.title,
                tracks=[BulkTrack(url=result.rows[0].url)],
            ).artist
            == result.artist
        )


# ===========================================================================
# Dedup
# ===========================================================================


def _write_flac(path, title=None, tags=None):
    """Write a minimal FLAC at *path* with the given Vorbis comments."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(minimal_flac_bytes())
    audio = FLAC(path)
    if title is not None:
        audio["TITLE"] = [title]
    for key, value in (tags or {}).items():
        audio[key] = [value]
    audio.save()


class TestDedup:
    def test_a_matching_sourceid_is_already_in_the_library(self, isolated_paths):
        download_dir, _ = isolated_paths
        _write_flac(
            download_dir / "Bonobo" / "Black Sands" / "Kiara.flac",
            title="Kiara",
            tags={"SOURCEID": "youtube:vid1"},
        )

        matches = find_in_library(
            "Bonobo",
            [DedupCandidate(id="row-1", url="https://x/1", source_id="youtube:vid1")],
            root=download_dir,
        )

        assert matches == {"row-1": "Bonobo/Black Sands/Kiara.flac"}

    def test_a_legacy_purl_matches_a_youtube_url(self, isolated_paths):
        """Files written before SOURCEID existed carry yt-dlp's PURL."""
        download_dir, _ = isolated_paths
        _write_flac(
            download_dir / "Bonobo" / "Kiara.flac",
            title="Kiara",
            tags={"PURL": "https://www.youtube.com/watch?v=vid1"},
        )

        matches = find_in_library(
            "Bonobo",
            [
                DedupCandidate(
                    id="row-1",
                    url="https://youtu.be/vid1",
                    source_id="youtube:vid1",
                    title="Something Else Entirely",
                )
            ],
            root=download_dir,
        )

        assert matches == {"row-1": "Bonobo/Kiara.flac"}

    def test_a_normalised_title_matches_without_any_tag(self, isolated_paths):
        download_dir, _ = isolated_paths
        _write_flac(download_dir / "Bonobo" / "Black Sands" / "Kiara.flac", title="Kiara")

        matches = find_in_library(
            "Bonobo",
            [
                DedupCandidate(
                    id="row-1",
                    url="https://www.youtube.com/watch?v=other",
                    source_id="youtube:other",
                    title="Kiara (Official Video)",
                )
            ],
            root=download_dir,
        )

        assert matches == {"row-1": "Bonobo/Black Sands/Kiara.flac"}

    def test_the_artist_folder_is_matched_case_insensitively(self, isolated_paths):
        download_dir, _ = isolated_paths
        _write_flac(download_dir / "BONOBO" / "Kiara.flac", title="Kiara")

        matches = find_in_library(
            "bonobo",
            [DedupCandidate(id="row-1", url="https://x/1", title="Kiara")],
            root=download_dir,
        )

        assert matches == {"row-1": "BONOBO/Kiara.flac"}

    def test_a_different_artist_folder_is_not_searched(self, isolated_paths):
        download_dir, _ = isolated_paths
        _write_flac(download_dir / "Someone Else" / "Kiara.flac", title="Kiara")

        matches = find_in_library(
            "Bonobo",
            [DedupCandidate(id="row-1", url="https://x/1", title="Kiara")],
            root=download_dir,
        )

        assert matches == {}

    def test_the_trash_is_invisible(self, isolated_paths):
        """A deleted track must not stop the user downloading it again."""
        download_dir, _ = isolated_paths
        _write_flac(
            download_dir / ".trash" / "2026-09-05T00:00:00Z" / "Bonobo" / "Kiara.flac",
            title="Kiara",
            tags={"SOURCEID": "youtube:vid1"},
        )

        matches = find_in_library(
            "Bonobo",
            [
                DedupCandidate(
                    id="row-1", url="https://x/1", source_id="youtube:vid1", title="Kiara"
                )
            ],
            root=download_dir,
        )

        assert matches == {}

    def test_a_track_nested_deeper_than_an_album_still_counts(self, isolated_paths):
        download_dir, _ = isolated_paths
        _write_flac(
            download_dir / "Bonobo" / "Live" / "Disc 1" / "Kiara.flac", title="Kiara"
        )

        matches = find_in_library(
            "Bonobo",
            [DedupCandidate(id="row-1", url="https://x/1", title="Kiara")],
            root=download_dir,
        )

        assert matches == {"row-1": "Bonobo/Live/Disc 1/Kiara.flac"}

    def test_an_untagged_mp3_matches_on_its_filename(self, isolated_paths):
        """Non-FLAC formats take part in title dedup, not in tag dedup."""
        download_dir, _ = isolated_paths
        target = download_dir / "Bonobo" / "Kiara.mp3"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xff\xfb\x90\x00" + bytes(64))

        matches = find_in_library(
            "Bonobo",
            [DedupCandidate(id="row-1", url="https://x/1", title="Kiara")],
            root=download_dir,
        )

        assert matches == {"row-1": "Bonobo/Kiara.mp3"}

    def test_nothing_matches_an_empty_library(self, isolated_paths):
        download_dir, _ = isolated_paths
        assert (
            find_in_library(
                "Bonobo",
                [DedupCandidate(id="row-1", url="https://x/1", title="Kiara")],
                root=download_dir,
            )
            == {}
        )

    def test_a_different_youtube_video_is_not_a_match(self, isolated_paths):
        """One stored YouTube FLAC must not mark every YouTube row a duplicate.

        The two watch URLs differ only in their query string, which is exactly
        what the canonical-URL reduction drops -- so YouTube is excluded from
        that path and compared by video id alone.
        """
        download_dir, _ = isolated_paths
        _write_flac(
            download_dir / "Bonobo" / "Kiara.flac",
            title="Kiara",
            tags={
                "SOURCEID": "youtube:aaaaaaaaaaa",
                "SOURCEURL": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
            },
        )

        matches = find_in_library(
            "Bonobo",
            [
                DedupCandidate(
                    id="row-1",
                    url="https://www.youtube.com/watch?v=bbbbbbbbbbb",
                    title="Something Else Entirely",
                )
            ],
            root=download_dir,
        )

        assert matches == {}

    @pytest.mark.parametrize(
        "url,source_id",
        [
            ("https://music.youtube.com/watch?v=aaaaaaaaaaa", None),
            ("https://youtu.be/aaaaaaaaaaa", None),
            ("https://example.invalid/whatever", "youtube:aaaaaaaaaaa"),
        ],
    )
    def test_the_same_youtube_video_still_matches(self, isolated_paths, url, source_id):
        """Every shape the same video's id can arrive in finds the same file."""
        download_dir, _ = isolated_paths
        _write_flac(
            download_dir / "Bonobo" / "Kiara.flac",
            title="Kiara",
            tags={"SOURCEURL": "https://www.youtube.com/watch?v=aaaaaaaaaaa"},
        )

        matches = find_in_library(
            "Bonobo",
            [
                DedupCandidate(
                    id="row-1",
                    url=url,
                    source_id=source_id,
                    title="Something Else Entirely",
                )
            ],
            root=download_dir,
        )

        assert matches == {"row-1": "Bonobo/Kiara.flac"}

    def test_a_soundcloud_url_still_matches_through_the_canonical_path(
        self, isolated_paths
    ):
        """Non-YouTube sources keep the host+path reduction they rely on."""
        download_dir, _ = isolated_paths
        _write_flac(
            download_dir / "Bonobo" / "Kiara.flac",
            title="Kiara",
            tags={"SOURCEURL": "https://soundcloud.com/bonobo/kiara?in=x"},
        )

        matches = find_in_library(
            "Bonobo",
            [
                DedupCandidate(
                    id="row-1",
                    url="http://www.soundcloud.com/bonobo/kiara/",
                    title="Something Else Entirely",
                )
            ],
            root=download_dir,
        )

        assert matches == {"row-1": "Bonobo/Kiara.flac"}
