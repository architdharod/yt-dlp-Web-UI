"""Tests for the YouTube Music discography enumeration.

The fixtures below are the shapes ``ytmusicapi`` 1.12 really returns, checked
against the live API for Bonobo while this was written: ``get_artist`` sections
carrying ``browseId``/``params``/``results``, release entries carrying
``browseId``/``playlistId``/``title``/``type``/``year``, and ``get_album``
returning ``tracks`` with ``videoId`` (sometimes ``None``), ``videoType``,
``isAvailable`` and ``duration_seconds``.  Nothing here touches the network:
the ``ytmusicapi`` client is injected and yt-dlp is the same stand-in
``test_probe`` uses.
"""

import asyncio
import json
import logging
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
import ytmusicapi

from app import ytmusic as ytmusic_module
from app import probe as probe_module
from app.downloader import _source_id
from app.models import (
    MAX_COLLECTION_TRACKS,
    MAX_CONCURRENT_PROBES,
    MAX_SUBCOLLECTIONS,
    ProbeRequest,
    validate_download_url,
)
from app.probe import (
    YTMUSIC_UNREACHABLE_NOTICE,
    CollectionTooLarge,
    Enumeration,
    ProbeTimeout,
    clear_cache,
    probe,
)
from app.ytmusic import (
    MAX_ALBUM_WORKERS,
    REQUEST_TIMEOUT_SECONDS,
    ChannelTarget,
    YouTubeMusicUnavailable,
    canonical_channel_url,
    channel_url_target,
    fetch_artist,
    resolve_channel_id,
    shared_client,
    source_id,
    watch_url,
)
from tests.test_probe import fake_ytdl

CHANNEL_ID = "UCgyl5xVlLLUZVpgfMGw-ETA"
# A second channel id for the same artist.  Both of these are real ids, and
# they are not interchangeable spellings of one: ``get_artist`` can be *asked*
# with the id an artist page is browsed by, and answers with the id on the
# page's subscribe button -- which is the id yt-dlp resolves a ``/@handle``
# to.  For Glass Beams those really are two different ``UC…`` strings.
ARTIST_PAGE_ID = "UCWBqhkfK4YT2OI3mJRQnNXg"
CHANNEL_URL = f"https://www.youtube.com/channel/{CHANNEL_ID}"
HANDLE_URL = "https://www.youtube.com/@bonobo"
MUSIC_URL = f"https://music.youtube.com/channel/{CHANNEL_ID}"


@pytest.fixture(autouse=True)
def _empty_probe_cache():
    """The enumeration cache and the probe semaphore are process-wide."""
    clear_cache()
    probe_module._probe_slots = asyncio.Semaphore(probe_module.MAX_CONCURRENT_PROBES)
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# A stand-in for ytmusicapi
# ---------------------------------------------------------------------------


class FakeYTMusic:
    """``ytmusicapi.YTMusic`` with canned answers.

    An ``Exception`` value anywhere is raised instead of returned, which is how
    the "this channel is not an artist" and "one album would not load" cases
    are written -- the real library raises a bare ``KeyError`` for both.
    """

    def __init__(self, artists=None, continuations=None, albums=None):
        self.artists = artists or {}
        self.continuations = continuations or {}
        self.albums = albums or {}
        self.artist_calls: list[str] = []
        self.continuation_calls: list[str] = []
        self.album_calls: list[str] = []
        self._lock = threading.Lock()

    def get_artist(self, channelId):
        with self._lock:
            self.artist_calls.append(channelId)
        return _answer(self.artists, channelId)

    def get_artist_albums(self, channelId, params, limit=100):
        with self._lock:
            self.continuation_calls.append(channelId)
        return _answer(self.continuations, channelId)

    def get_album(self, browseId):
        with self._lock:
            self.album_calls.append(browseId)
        return _answer(self.albums, browseId)


def _answer(responses, key):
    value = responses.get(key)
    if isinstance(value, Exception):
        raise value
    if value is None:
        raise KeyError(key)
    return value


def _track(index, **overrides):
    track = {
        "videoId": f"vid{index}",
        "title": f"Track {index}",
        "artists": [{"name": "Bonobo", "id": ARTIST_PAGE_ID}],
        "isAvailable": True,
        "duration_seconds": 100 + index,
        "trackNumber": index,
        "videoType": "MUSIC_VIDEO_TYPE_ATV",
    }
    track.update(overrides)
    return track


def _entry(browse_id, title, release_type="Album"):
    return {
        "browseId": browse_id,
        "playlistId": f"OLAK5uy_{browse_id}",
        "title": title,
        "type": release_type,
        "year": "2010",
        "thumbnails": [
            {"url": f"https://img/{browse_id}-s.jpg", "width": 226, "height": 226},
            {"url": f"https://img/{browse_id}.jpg", "width": 544, "height": 544},
        ],
    }


def _album(title, tracks, release_type="Album"):
    return {
        "title": title,
        "type": release_type,
        "year": "2010",
        "trackCount": len(tracks),
        "audioPlaylistId": "OLAK5uy_whatever",
        "thumbnails": [{"url": f"https://img/{title}.jpg", "width": 544, "height": 544}],
        "tracks": tracks,
    }


def _artist_page(albums_section=None, singles_section=None, **extra):
    page = {
        "name": "Bonobo",
        "channelId": CHANNEL_ID,
        "description": "…",
        "videos": {
            "browseId": "VLPL_videos",
            "params": "vids",
            "results": [
                {
                    "title": "Kiara (official video)",
                    "videoId": "clip1",
                    "playlistId": "OLAK5uy_clip",
                }
            ],
        },
    }
    if albums_section is not None:
        page["albums"] = albums_section
    if singles_section is not None:
        page["singles"] = singles_section
    page.update(extra)
    return page


def _bonobo():
    """An artist page whose albums paginate and whose singles do not.

    Both paths in one fixture, because both are real: YouTube Music gives a
    section a ``params`` when there is more behind it than the tiles it drew,
    and no ``params`` when the tiles are all there is.
    """
    artist = _artist_page(
        albums_section={
            "browseId": "MPADUC_albums",
            "params": "album-params",
            "results": [_entry("MPREb_black", "Black Sands")],
        },
        singles_section={
            "browseId": "MPADUC_singles",
            "results": [_entry("MPREb_talk", "Talk to Me", "Single")],
        },
    )
    continuations = {
        "MPADUC_albums": [
            _entry("MPREb_black", "Black Sands"),
            _entry("MPREb_ep", "Ketto", "EP"),
        ]
    }
    albums = {
        "MPREb_black": _album(
            "Black Sands",
            [
                _track(1),
                # A real album mixes ATV and OMV: on Black Sands, "Kiara" and
                # the title track are official videos and the rest are not.
                _track(2, videoType="MUSIC_VIDEO_TYPE_OMV"),
                _track(3, videoId=None, isAvailable=False, trackNumber=None),
                _track(4, isAvailable=False),
            ],
        ),
        "MPREb_ep": _album("Ketto", [_track(5)], release_type="EP"),
        "MPREb_talk": _album(
            "Talk to Me",
            # The same recording is on the single and on the album; the row is
            # keyed on the video id, so the album's copy is the one kept.
            [_track(6), _track(1)],
            release_type="Single",
        ),
    }
    return FakeYTMusic(
        artists={CHANNEL_ID: artist}, continuations=continuations, albums=albums
    )


def fake_client(client):
    """Make :func:`app.ytmusic.fetch_artist` use *client*."""
    return patch("app.ytmusic.shared_client", lambda: client)


def _channel_uploads():
    """What the flat yt-dlp pass sees when it enumerates the channel instead."""
    return {
        "_type": "playlist",
        "id": CHANNEL_ID,
        "extractor_key": "YoutubeTab",
        "title": "Bonobo",
        "entries": [
            {
                "_type": "url",
                "ie_key": "Youtube",
                "id": "live1",
                "title": "Live set",
                "url": "https://www.youtube.com/watch?v=live1",
            }
        ],
    }


# The two ids in the recorded fixtures: the artist page is browsed by the
# first and answers with the second (see ARTIST_PAGE_ID).
GLASS_BEAMS_PAGE_ID = "UCcT6dA3rQ_EhHlG5Wu8mitg"
GLASS_BEAMS_CHANNEL_ID = "UCz2BbywXgCxZcpAiXPlKT4Q"


# ===========================================================================
# URL recognition
# ===========================================================================


class TestChannelUrlTarget:
    @pytest.mark.parametrize(
        "url,tab",
        [
            (f"https://www.youtube.com/channel/{CHANNEL_ID}", None),
            (f"https://youtube.com/channel/{CHANNEL_ID}", None),
            (f"https://m.youtube.com/channel/{CHANNEL_ID}", None),
            (f"https://music.youtube.com/channel/{CHANNEL_ID}", None),
            (f"https://music.youtube.com/browse/{CHANNEL_ID}", None),
            (f"https://music.youtube.com/browse/MPLA{CHANNEL_ID}", None),
            (f"https://www.youtube.com/channel/{CHANNEL_ID}/", None),
            (f"https://www.youtube.com/channel/{CHANNEL_ID}/videos", "videos"),
            (f"https://www.youtube.com/channel/{CHANNEL_ID}/releases", "releases"),
            (f"https://music.youtube.com/channel/{CHANNEL_ID}?foo=bar", None),
        ],
    )
    def test_a_url_carrying_the_channel_id_needs_no_resolution(self, url, tab):
        target = channel_url_target(url)
        assert target == ChannelTarget(
            channel_id=CHANNEL_ID, root_url=canonical_channel_url(CHANNEL_ID), tab=tab
        )

    @pytest.mark.parametrize(
        "url,root,tab",
        [
            ("https://www.youtube.com/@bonobo", "https://www.youtube.com/@bonobo", None),
            (
                "https://youtube.com/@bonobo/videos",
                "https://www.youtube.com/@bonobo",
                "videos",
            ),
            (
                "https://www.youtube.com/@bonobo/releases",
                "https://www.youtube.com/@bonobo",
                "releases",
            ),
            (
                "https://www.youtube.com/@bonobo/featured",
                "https://www.youtube.com/@bonobo",
                "featured",
            ),
            ("https://www.youtube.com/c/Bonobo", "https://www.youtube.com/c/Bonobo", None),
            (
                "https://www.youtube.com/user/bonobomusic/videos",
                "https://www.youtube.com/user/bonobomusic",
                "videos",
            ),
        ],
    )
    def test_a_handle_url_is_recognised_and_needs_resolving(self, url, root, tab):
        assert channel_url_target(url) == ChannelTarget(
            channel_id=None, root_url=root, tab=tab
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/playlist?list=OLAK5uy_album",
            "https://music.youtube.com/playlist?list=OLAK5uy_album",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://soundcloud.com/bonobo",
            "https://bonobo.bandcamp.com/",
            "https://www.youtube.com/",
            # A /channel/ path that does not hold a channel id, so nothing is
            # handed to get_artist that did not look like one.
            "https://www.youtube.com/channel/not-a-channel",
            f"https://music.youtube.com/browse/MPREb_{CHANNEL_ID}",
            # An unknown extra segment: this module does not understand the
            # shape, so it does not claim it.
            f"https://www.youtube.com/@bonobo/video/{CHANNEL_ID}",
            f"ftp://www.youtube.com/channel/{CHANNEL_ID}",
        ],
    )
    def test_everything_else_falls_through(self, url):
        assert channel_url_target(url) is None


class TestResolveChannelId:
    def test_an_id_in_the_url_costs_no_extraction(self):
        with fake_ytdl({}) as ydl:
            assert resolve_channel_id(channel_url_target(CHANNEL_URL)) == CHANNEL_ID

        assert ydl.calls == []

    def test_a_handle_is_resolved_with_one_entry_free_extraction(self):
        info = {
            "_type": "playlist",
            "id": "@bonobo",
            "channel_id": CHANNEL_ID,
            "channel": "Bonobo",
            "entries": [],
        }
        with fake_ytdl({HANDLE_URL: info}) as ydl:
            assert resolve_channel_id(channel_url_target(HANDLE_URL)) == CHANNEL_ID

        assert ydl.calls == [HANDLE_URL]
        opts = ydl.options[0]
        # No entries at all: the channel page's own metadata is the whole
        # point of the call.
        assert opts["playlist_items"] == "0"
        assert opts["allowed_extractors"] == ["default", "-generic"]

    def test_the_id_falls_back_to_uploader_id_then_id(self):
        info = {"_type": "playlist", "uploader_id": CHANNEL_ID, "entries": []}
        with fake_ytdl({HANDLE_URL: info}):
            assert resolve_channel_id(channel_url_target(HANDLE_URL)) == CHANNEL_ID

    def test_a_page_that_names_no_channel_resolves_to_nothing(self):
        info = {"_type": "playlist", "id": "@bonobo", "uploader_id": "@bonobo"}
        with fake_ytdl({HANDLE_URL: info}):
            assert resolve_channel_id(channel_url_target(HANDLE_URL)) is None

    def test_a_failed_extraction_resolves_to_nothing(self):
        import yt_dlp

        with fake_ytdl({HANDLE_URL: yt_dlp.utils.DownloadError("no such channel")}):
            assert resolve_channel_id(channel_url_target(HANDLE_URL)) is None


# ===========================================================================
# Reading the discography
# ===========================================================================


class TestFetchArtist:
    def test_a_section_with_params_is_followed_to_the_full_discography(self):
        client = _bonobo()
        artist = fetch_artist(CHANNEL_ID, client=client)

        assert artist.name == "Bonobo"
        assert artist.channel_id == CHANNEL_ID
        assert [release.title for release in artist.releases] == [
            "Black Sands",
            "Ketto",
            "Talk to Me",
        ]
        # Albums followed the continuation; singles had no ``params`` and so
        # were taken from the tiles on the page.
        assert client.continuation_calls == ["MPADUC_albums"]

    def test_the_videos_section_is_never_read(self):
        client = _bonobo()
        artist = fetch_artist(CHANNEL_ID, client=client)

        assert "VLPL_videos" not in client.continuation_calls
        video_ids = {
            track.video_id for release in artist.releases for track in release.tracks
        }
        assert "clip1" not in video_ids

    def test_a_broken_continuation_falls_back_to_the_tiles(self):
        """``get_artist_albums`` raises on some artists; ten albums beat none."""
        client = _bonobo()
        client.continuations["MPADUC_albums"] = KeyError("musicShelfRenderer")
        artist = fetch_artist(CHANNEL_ID, client=client)

        assert [release.title for release in artist.releases] == [
            "Black Sands",
            "Talk to Me",
        ]

    def test_a_release_that_will_not_load_is_counted(self):
        client = _bonobo()
        client.albums["MPREb_ep"] = KeyError("musicResponsiveHeaderRenderer")
        artist = fetch_artist(CHANNEL_ID, client=client)

        assert artist.unreadable_releases == 1
        assert [release.title for release in artist.releases] == [
            "Black Sands",
            "Talk to Me",
        ]

    def test_a_channel_that_is_not_a_music_artist_is_not_one(self):
        client = FakeYTMusic(artists={CHANNEL_ID: KeyError("musicImmersiveHeaderRenderer")})
        assert fetch_artist(CHANNEL_ID, client=client) is None

    def test_a_page_with_no_releases_is_not_an_artist_either(self):
        client = FakeYTMusic(artists={CHANNEL_ID: _artist_page()})
        assert fetch_artist(CHANNEL_ID, client=client) is None

    def test_the_deadline_stops_the_fetch(self):
        client = _bonobo()
        calls = {"n": 0}

        def check_deadline():
            calls["n"] += 1
            if calls["n"] > 2:
                raise ProbeTimeout("too long")

        with pytest.raises(ProbeTimeout):
            fetch_artist(CHANNEL_ID, client=client, check_deadline=check_deadline)


# ===========================================================================
# The probe, end to end
# ===========================================================================


class TestProbeThroughYouTubeMusic:
    async def test_a_channel_url_enumerates_the_discography(self):
        client = _bonobo()
        with fake_client(client), fake_ytdl({}) as ydl:
            result = await probe(CHANNEL_URL)

        assert isinstance(result, Enumeration)
        assert result.source == "youtube"
        assert result.url == CHANNEL_URL
        assert result.artist == "Bonobo"
        assert result.title == "Bonobo"
        # No yt-dlp at all on this path: the channel id was in the URL and
        # YouTube Music answered the rest.
        assert ydl.calls == []

        assert [(row.title, row.album) for row in result.rows] == [
            ("Track 1", "Black Sands"),
            # An official-video track is still an album track.
            ("Track 2", "Black Sands"),
            ("Track 4", "Black Sands"),
            ("Track 5", "Ketto"),
            # A Single release gets no album: its tracks are loose Singles.
            ("Track 6", None),
        ]

    async def test_rows_carry_music_watch_urls_and_matching_source_ids(self):
        client = _bonobo()
        with fake_client(client), fake_ytdl({}):
            result = await probe(CHANNEL_URL)

        row = result.rows[0]
        assert row.url == "https://music.youtube.com/watch?v=vid1"
        assert row.source_id == "youtube:vid1"
        assert row.id == "youtube:vid1"
        # Exactly what the downloader writes into SOURCEID for the same
        # track, or dedup by provenance would never match.
        assert row.source_id == _source_id({"extractor": "youtube", "id": "vid1"})
        assert row.duration == 101
        assert row.thumbnail_url == "https://img/Black Sands.jpg"

    async def test_an_unavailable_track_is_greyed_and_a_missing_one_is_dropped(self):
        client = _bonobo()
        with fake_client(client), fake_ytdl({}):
            result = await probe(CHANNEL_URL)

        reasons = {row.title: row.unavailable_reason for row in result.rows}
        assert reasons["Track 4"] == "not available"
        assert reasons["Track 1"] is None
        # Track 3 has no videoId at all -- nothing to tick -- so it is counted
        # rather than shown.
        assert "Track 3" not in reasons
        assert result.notices == (
            "1 track is listed on YouTube Music but is not available to download",
        )

    async def test_the_same_recording_on_a_single_and_an_album_is_one_row(self):
        client = _bonobo()
        with fake_client(client), fake_ytdl({}):
            result = await probe(CHANNEL_URL)

        ids = [row.id for row in result.rows]
        assert ids.count("youtube:vid1") == 1
        # The album came first, so the album's copy is the one that stayed.
        assert result.rows[0].album == "Black Sands"

    async def test_an_unreadable_release_is_a_notice(self):
        client = _bonobo()
        client.albums["MPREb_ep"] = KeyError("musicResponsiveHeaderRenderer")
        with fake_client(client), fake_ytdl({}):
            result = await probe(CHANNEL_URL)

        assert "1 release could not be read and was left out" in result.notices

    async def test_two_unreadable_releases_read_as_plural(self):
        client = _bonobo()
        client.albums["MPREb_ep"] = KeyError("x")
        client.albums["MPREb_talk"] = KeyError("y")
        with fake_client(client), fake_ytdl({}):
            result = await probe(CHANNEL_URL)

        assert "2 releases could not be read and were left out" in result.notices

    async def test_a_handle_is_resolved_then_enumerated(self):
        client = _bonobo()
        info = {"_type": "playlist", "id": "@bonobo", "channel_id": CHANNEL_ID}
        with fake_client(client), fake_ytdl({HANDLE_URL: info}) as ydl:
            result = await probe(HANDLE_URL)

        assert ydl.calls == [HANDLE_URL]
        assert client.artist_calls == [CHANNEL_ID]
        assert result.url == HANDLE_URL
        assert len(result.rows) == 5

    async def test_more_than_two_thousand_tracks_stops_the_probe(self):
        client = FakeYTMusic(
            artists={
                CHANNEL_ID: _artist_page(
                    albums_section={
                        "browseId": "MPADUC_albums",
                        "results": [_entry("MPREb_huge", "Everything")],
                    }
                )
            },
            albums={
                "MPREb_huge": _album(
                    "Everything",
                    [_track(index) for index in range(MAX_COLLECTION_TRACKS + 1)],
                )
            },
        )
        with fake_client(client), fake_ytdl({}):
            with pytest.raises(CollectionTooLarge):
                await probe(CHANNEL_URL)

    async def test_a_slow_discography_times_out(self):
        client = _bonobo()
        with fake_client(client), fake_ytdl({}):
            with pytest.raises((ProbeTimeout, asyncio.TimeoutError)):
                await probe(CHANNEL_URL, timeout=0)


class TestFallbackToYtDlp:
    async def test_a_channel_youtube_music_does_not_know_uses_the_flat_pass(self):
        """A podcast channel is still a collection; it is just not a discography."""
        client = FakeYTMusic(artists={CHANNEL_ID: KeyError("musicImmersiveHeaderRenderer")})
        info = {
            "_type": "playlist",
            "id": CHANNEL_ID,
            "extractor_key": "YoutubeTab",
            "title": "Talking Heads",
            "channel": "Talking Heads",
            "entries": [
                {
                    "_type": "url",
                    "ie_key": "Youtube",
                    "id": "ep1",
                    "title": "Episode 1",
                    "url": "https://www.youtube.com/watch?v=ep1",
                }
            ],
        }
        with fake_client(client), fake_ytdl({CHANNEL_URL: info}) as ydl:
            result = await probe(CHANNEL_URL)

        assert ydl.calls == [CHANNEL_URL]
        assert [row.title for row in result.rows] == ["Episode 1"]
        assert result.artist == "Talking Heads"

    async def test_a_discography_with_nothing_playable_falls_back_too(self):
        client = FakeYTMusic(
            artists={
                CHANNEL_ID: _artist_page(
                    albums_section={
                        "browseId": "MPADUC_albums",
                        "results": [_entry("MPREb_unreleased", "Coming Soon")],
                    }
                )
            },
            albums={
                "MPREb_unreleased": _album(
                    "Coming Soon", [_track(1, videoId=None, isAvailable=False)]
                )
            },
        )
        info = {
            "_type": "playlist",
            "id": CHANNEL_ID,
            "extractor_key": "YoutubeTab",
            "title": "Nobody",
            "entries": [
                {
                    "_type": "url",
                    "ie_key": "Youtube",
                    "id": "live1",
                    "title": "Live set",
                    "url": "https://www.youtube.com/watch?v=live1",
                }
            ],
        }
        with fake_client(client), fake_ytdl({CHANNEL_URL: info}) as ydl:
            result = await probe(CHANNEL_URL)

        assert ydl.calls == [CHANNEL_URL]
        assert [row.title for row in result.rows] == ["Live set"]
        # The dropped track belongs to the abandoned attempt and must not be
        # counted against the flat pass that replaced it.
        assert result.notices == ()

    async def test_a_discography_nobody_can_play_falls_back_too(self):
        """Every row unavailable is as good as no rows.

        ``probe`` raises ``EmptyCollection`` on a preview whose every row is
        unavailable, so handing one back would answer 400 for a channel whose
        uploads are downloadable.
        """
        client = FakeYTMusic(
            artists={
                CHANNEL_ID: _artist_page(
                    albums_section={
                        "browseId": "MPADUC_albums",
                        "results": [_entry("MPREb_blocked", "Blocked")],
                    }
                )
            },
            albums={
                "MPREb_blocked": _album(
                    "Blocked",
                    [_track(1, isAvailable=False), _track(2, isAvailable=False)],
                )
            },
        )
        with fake_client(client), fake_ytdl({CHANNEL_URL: _channel_uploads()}) as ydl:
            result = await probe(CHANNEL_URL)

        assert ydl.calls == [CHANNEL_URL]
        assert [row.title for row in result.rows] == ["Live set"]
        assert result.notices == ()

    async def test_an_unexpected_failure_falls_back_instead_of_answering_500(
        self, caplog
    ):
        """A ``ytmusicapi`` bug is not a reason to lose the preview."""

        def boom(*args, **kwargs):
            raise RuntimeError("ytmusicapi walked off the end of a renderer")

        with patch("app.probe.fetch_artist", boom), fake_ytdl(
            {CHANNEL_URL: _channel_uploads()}
        ), caplog.at_level(logging.ERROR):
            result = await probe(CHANNEL_URL)

        assert [row.title for row in result.rows] == ["Live set"]
        assert result.notices == ()
        assert "ytmusicapi walked off the end of a renderer" in caplog.text


class TestReleaseOrder:
    """Which release a track shared by two of them is filed under.

    Glass Beams' "Mahal" is on the EP of that name and is also its own single,
    with the *same* video behind both.  The probe keeps the row it saw first,
    so the order releases come back in decides whether the track lands in
    ``Glass Beams/Mahal/`` or loose under the artist -- and the EP is the
    answer either way round, because YouTube Music does not order the section
    for us.
    """

    EP = _entry("MPREb_ep", "Mahal", "EP")
    SINGLE = _entry("MPREb_single", "Mahal", "Single")

    def _client(self, singles):
        return FakeYTMusic(
            artists={
                CHANNEL_ID: _artist_page(
                    singles_section={"browseId": None, "results": list(singles)}
                )
            },
            albums={
                "MPREb_ep": _album(
                    "Mahal",
                    [_track(1, videoId="horizon"), _track(2, videoId="mahal")],
                    release_type="EP",
                ),
                "MPREb_single": _album(
                    "Mahal",
                    [_track(2, videoId="mahal"), _track(3, videoId="mahal")],
                    release_type="Single",
                ),
            },
        )

    @pytest.mark.parametrize(
        "order", [(EP, SINGLE), (SINGLE, EP)], ids=["ep-first", "single-first"]
    )
    async def test_a_track_on_an_ep_and_a_single_is_filed_under_the_ep(self, order):
        with fake_client(self._client(order)), fake_ytdl({}):
            result = await probe(CHANNEL_URL)

        albums = {row.source_id: row.album for row in result.rows}
        assert albums == {
            source_id("horizon"): "Mahal",
            source_id("mahal"): "Mahal",
        }


    async def test_an_untyped_tile_is_ranked_by_what_its_album_page_says(self):
        """A tile whose subtitle is only a year carries no ``type`` at all.

        The pre-fetch tiebreak can only read the tile, so it leaves such a
        release where it found it; the ranking that decides which copy of a
        shared recording wins reads the *release*, which the album page typed
        as an EP.
        """
        untyped_ep = {key: value for key, value in self.EP.items() if key != "type"}
        with fake_client(self._client((self.SINGLE, untyped_ep))), fake_ytdl({}):
            result = await probe(CHANNEL_URL)

        albums = {row.source_id: row.album for row in result.rows}
        assert albums == {
            source_id("horizon"): "Mahal",
            # Under the EP, not loose: the Single that also holds it was
            # ranked behind it despite being the first tile on the page.
            source_id("mahal"): "Mahal",
        }


class TestAlbumIsFinal:
    """Rows this pass produces say their album is the whole answer."""

    async def test_every_youtube_music_row_carries_album_final(self):
        with fake_client(_bonobo()), fake_ytdl({}):
            result = await probe(CHANNEL_URL)

        assert [(row.album, row.album_final) for row in result.rows] == [
            ("Black Sands", True),
            ("Black Sands", True),
            # Track 4 is listed as unavailable, which is not the same as
            # having no album.
            ("Black Sands", True),
            ("Ketto", True),
            # The Single's own track: no album, deliberately.
            (None, True),
        ]

    async def test_a_flat_row_never_does(self):
        client = FakeYTMusic(artists={CHANNEL_ID: KeyError("header")})
        with fake_client(client), fake_ytdl({CHANNEL_URL: _channel_uploads()}):
            result = await probe(CHANNEL_URL)

        assert [row.album_final for row in result.rows] == [False]


class TestYouTubeMusicUnreachable:
    """A request that got no answer is not the same as "not a music artist"."""

    UNREACHABLE = [
        requests.ConnectionError("connection refused"),
        requests.ReadTimeout("timed out"),
        ytmusicapi.exceptions.YTMusicServerError("500"),
        json.JSONDecodeError("Expecting value", "<html>", 0),
    ]

    @pytest.mark.parametrize("exc", UNREACHABLE, ids=lambda e: type(e).__name__)
    def test_a_transport_failure_is_raised_not_swallowed(self, exc):
        client = FakeYTMusic(artists={CHANNEL_ID: exc})
        with pytest.raises(YouTubeMusicUnavailable):
            fetch_artist(CHANNEL_ID, client=client)

    @pytest.mark.parametrize(
        "exc",
        [
            KeyError("musicImmersiveHeaderRenderer"),
            IndexError(0),
            TypeError(),
            AttributeError(),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_a_parse_failure_is_still_just_not_an_artist(self, exc):
        client = FakeYTMusic(artists={CHANNEL_ID: exc})
        assert fetch_artist(CHANNEL_ID, client=client) is None

    async def test_the_flat_fallback_says_why_it_is_showing_uploads(self):
        client = FakeYTMusic(artists={CHANNEL_ID: requests.ConnectionError("no route")})
        with fake_client(client), fake_ytdl({CHANNEL_URL: _channel_uploads()}):
            result = await probe(CHANNEL_URL)

        assert [row.title for row in result.rows] == ["Live set"]
        assert result.notices == (YTMUSIC_UNREACHABLE_NOTICE,)

    async def test_a_channel_that_is_simply_not_an_artist_says_nothing(self):
        client = FakeYTMusic(artists={CHANNEL_ID: KeyError("header")})
        with fake_client(client), fake_ytdl({CHANNEL_URL: _channel_uploads()}):
            result = await probe(CHANNEL_URL)

        assert result.notices == ()


class TestTabNotice:
    """A URL that named a tab gets told the tab was not what was read."""

    async def test_a_videos_url_says_the_releases_are_what_it_shows(self):
        with fake_client(_bonobo()), fake_ytdl({}):
            result = await probe(f"{CHANNEL_URL}/videos")

        assert result.notices[-1] == (
            "Showing this artist's releases from YouTube Music; the Videos tab "
            "was not enumerated."
        )

    @pytest.mark.parametrize("suffix", ["", "/releases"])
    async def test_the_discography_tabs_say_nothing(self, suffix):
        with fake_client(_bonobo()), fake_ytdl({}):
            result = await probe(f"{CHANNEL_URL}{suffix}")

        assert not any("not enumerated" in notice for notice in result.notices)

    async def test_the_notice_is_not_cached_onto_the_other_spellings(self):
        """It belongs to the URL that asked, not to the shared enumeration."""
        client = _bonobo()
        with fake_client(client), fake_ytdl({}):
            with_tab = await probe(f"{CHANNEL_URL}/videos")
            without = await probe(CHANNEL_URL)

        assert client.artist_calls == [CHANNEL_ID]
        assert with_tab.rows == without.rows
        assert any("not enumerated" in notice for notice in with_tab.notices)
        assert not any("not enumerated" in notice for notice in without.notices)


class TestReleaseCap:
    async def test_a_discography_past_the_cap_is_cut_and_says_so(self):
        entries = [
            _entry(f"MPREb_{i}", f"Album {i}") for i in range(MAX_SUBCOLLECTIONS + 5)
        ]
        client = FakeYTMusic(
            artists={
                CHANNEL_ID: _artist_page(
                    albums_section={"browseId": None, "results": entries}
                )
            },
            albums={
                entry["browseId"]: _album(entry["title"], [_track(i)])
                for i, entry in enumerate(entries)
            },
        )
        with fake_client(client), fake_ytdl({}):
            result = await probe(CHANNEL_URL)

        assert len(client.album_calls) == MAX_SUBCOLLECTIONS
        assert (
            f"Only the first {MAX_SUBCOLLECTIONS} releases were read; use a "
            "narrower URL." in result.notices
        )


class TestUnplayableTracks:
    async def test_two_of_them_read_as_plural(self):
        client = FakeYTMusic(
            artists={
                CHANNEL_ID: _artist_page(
                    albums_section={
                        "browseId": None,
                        "results": [_entry("MPREb_soon", "Coming Soon")],
                    }
                )
            },
            albums={
                "MPREb_soon": _album(
                    "Coming Soon",
                    [_track(1), _track(2, videoId=None), _track(3, videoId=None)],
                )
            },
        )
        with fake_client(client), fake_ytdl({}):
            result = await probe(CHANNEL_URL)

        assert result.notices == (
            "2 tracks are listed on YouTube Music but are not available to download",
        )


class TestSharedClient:
    def test_it_hands_ytmusicapi_a_pooled_session_with_a_timeout(self):
        """The library only times out a session it built itself."""
        with patch.object(ytmusic_module, "_client", None), patch.object(
            ytmusic_module.ytmusicapi, "YTMusic"
        ) as ytmusic_cls:
            client = shared_client()
            again = shared_client()

        assert client is ytmusic_cls.return_value
        assert again is client
        assert ytmusic_cls.call_count == 1
        kwargs = ytmusic_cls.call_args.kwargs
        assert kwargs["language"] == "en"
        session = kwargs["requests_session"]
        adapter = session.get_adapter("https://music.youtube.com/")
        assert adapter.poolmanager.connection_pool_kw["maxsize"] == (
            MAX_ALBUM_WORKERS * MAX_CONCURRENT_PROBES
        )
        assert session.request.keywords == {"timeout": REQUEST_TIMEOUT_SECONDS}

    def test_a_warm_that_fails_still_caches_the_client(self):
        """``base_headers`` is a cached_property: a failed read caches nothing.

        So the visitor id is simply fetched again by the first call that needs
        it, and throwing the client away over it would buy nothing but a new
        session per probe.
        """
        warms: list[int] = []

        class FlakyWarm:
            def __init__(self, **kwargs):
                pass

            @property
            def base_headers(self):
                warms.append(1)
                raise requests.ConnectionError("no route to host")

        with patch.object(ytmusic_module, "_client", None), patch.object(
            ytmusic_module.ytmusicapi, "YTMusic", FlakyWarm
        ):
            first = shared_client()
            second = shared_client()

        assert first is second
        assert warms == [1]

    async def test_a_probe_after_a_failed_warm_still_reads_the_discography(self):
        canned = _bonobo()

        class FlakyWarm:
            def __init__(self, **kwargs):
                pass

            @property
            def base_headers(self):
                raise requests.ReadTimeout("timed out")

            def __getattr__(self, name):
                return getattr(canned, name)

        with patch.object(ytmusic_module, "_client", None), patch.object(
            ytmusic_module.ytmusicapi, "YTMusic", FlakyWarm
        ), fake_ytdl({}):
            result = await probe(CHANNEL_URL)
            assert ytmusic_module._client is not None

        assert [row.title for row in result.rows] == [
            "Track 1",
            "Track 2",
            "Track 4",
            "Track 5",
            "Track 6",
        ]

    async def test_a_client_that_cannot_be_built_is_unreachable(self):
        """Building one is itself a request, so its failure is the same answer."""

        def boom(**kwargs):
            raise requests.ConnectionError("no route to host")

        with patch.object(ytmusic_module, "_client", None), patch.object(
            ytmusic_module.ytmusicapi, "YTMusic", boom
        ), fake_ytdl({CHANNEL_URL: _channel_uploads()}):
            result = await probe(CHANNEL_URL)

        assert [row.title for row in result.rows] == ["Live set"]
        assert result.notices == (YTMUSIC_UNREACHABLE_NOTICE,)


class TestRecordedFixtures:
    """The same fixtures the live keyless API answered with, replayed.

    Everything else here is a hand-written shape; this is the real one, so a
    ``ytmusicapi`` release that changes what a call returns has something to
    fail against.  Glass Beams: one EP and one single, both called "Mahal",
    sharing the "Mahal" recording between them.
    """

    FIXTURES = Path(__file__).parent / "fixtures" / "ytmusic"

    def _client(self):
        artist = json.loads((self.FIXTURES / "glass_beams_artist.json").read_text())
        albums = {
            path.name.removeprefix("glass_beams_album_").removesuffix(".json"): json.loads(
                path.read_text()
            )
            for path in self.FIXTURES.glob("glass_beams_album_*.json")
        }
        return artist, FakeYTMusic(artists={GLASS_BEAMS_PAGE_ID: artist}, albums=albums)

    def test_the_recorded_discography_reads_as_one_ep_and_one_single(self):
        artist_page, client = self._client()
        artist = fetch_artist(GLASS_BEAMS_PAGE_ID, client=client)

        assert artist.name == "Glass Beams"
        # The page is browsed by one id and answers with another; both are real.
        assert artist.channel_id == GLASS_BEAMS_CHANNEL_ID
        assert artist_page["albums"] is None
        assert [(r.title, r.release_type) for r in artist.releases] == [
            ("Mahal", "EP"),
            ("Mahal", "Single"),
        ]
        ep = artist.releases[0]
        assert [track.title for track in ep.tracks] == [
            "Horizon",
            "Mahal",
            "Orb",
            "Snake Oil",
            "Black Sand",
        ]
        assert ep.tracks[0].video_id == "wLjq5oUrc7Q"
        assert ep.tracks[0].duration == 43.0
        assert all(track.available for track in ep.tracks)

    async def test_the_shared_recording_is_one_row_under_the_ep(self):
        _, client = self._client()
        with fake_client(client), fake_ytdl({}):
            result = await probe(
                f"https://music.youtube.com/channel/{GLASS_BEAMS_PAGE_ID}"
            )

        assert result.artist == "Glass Beams"
        # Five rows, not seven: the single's "Mahal" and "Mahal (Edit)" are the
        # same video as the EP's "Mahal", which is the copy already filed.
        assert [(row.title, row.album) for row in result.rows] == [
            ("Horizon", "Mahal"),
            ("Mahal", "Mahal"),
            ("Orb", "Mahal"),
            ("Snake Oil", "Mahal"),
            ("Black Sand", "Mahal"),
        ]


class TestCacheSharing:
    async def test_every_spelling_of_one_channel_shares_an_enumeration(self):
        client = _bonobo()
        info = {"_type": "playlist", "id": "@bonobo", "channel_id": CHANNEL_ID}
        with fake_client(client), fake_ytdl({HANDLE_URL: info}):
            first = await probe(HANDLE_URL)
            second = await probe(MUSIC_URL)
            third = await probe(f"{CHANNEL_URL}/videos")

        assert client.artist_calls == [CHANNEL_ID]
        assert first.rows == second.rows == third.rows
        # The URL the user pasted is what comes back, whichever cache answered.
        assert (first.url, second.url, third.url) == (
            HANDLE_URL,
            MUSIC_URL,
            f"{CHANNEL_URL}/videos",
        )

    async def test_the_id_a_page_answers_with_is_cached_as_well(self):
        """The subscribe-button channel id is what a later handle probe uses.

        ``get_artist`` was asked with the id in the pasted ``/browse/`` URL and
        answered with the page's own ``channelId``.  That second id is the one
        yt-dlp resolves ``/@artist`` to, so caching the enumeration under both
        is what makes the handle probe that follows free.
        """
        client = _bonobo()
        client.artists[ARTIST_PAGE_ID] = client.artists[CHANNEL_ID]
        with fake_client(client), fake_ytdl({}):
            first = await probe(f"https://music.youtube.com/browse/MPLA{ARTIST_PAGE_ID}")
            second = await probe(MUSIC_URL)

        assert client.artist_calls == [ARTIST_PAGE_ID]
        assert first.rows == second.rows

    async def test_clearing_the_cache_asks_youtube_music_again(self):
        client = _bonobo()
        with fake_client(client), fake_ytdl({}):
            await probe(MUSIC_URL)
            clear_cache()
            await probe(MUSIC_URL)

        assert client.artist_calls == [CHANNEL_ID, CHANNEL_ID]


class TestHostAllowlist:
    @pytest.mark.parametrize(
        "url",
        [
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            f"https://music.youtube.com/channel/{CHANNEL_ID}",
            f"https://music.youtube.com/browse/MPLA{CHANNEL_ID}",
        ],
    )
    def test_music_youtube_com_is_already_allowed(self, url):
        """``*.youtube.com`` covers the Music host; no widening was needed."""
        assert validate_download_url(url) == url
        assert ProbeRequest(url=url).url == url


class TestHelpers:
    def test_a_watch_url_and_its_source_id_agree_with_the_downloader(self):
        assert watch_url("abc123") == "https://music.youtube.com/watch?v=abc123"
        assert source_id("abc123") == _source_id({"extractor": "youtube", "id": "abc123"})

    def test_the_canonical_url_is_the_music_channel_page(self):
        assert canonical_channel_url(CHANNEL_ID) == (
            f"https://music.youtube.com/channel/{CHANNEL_ID}"
        )
