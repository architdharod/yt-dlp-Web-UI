"""Tests for the debounced rescan hook, its two clients, and the notice board.

Every HTTP call goes through ``httpx.MockTransport``: the suite never touches a
real Navidrome or Lidarr, and each test can assert on the exact request the
client built.  The debounce is turned down to milliseconds so the tests do not
sit through the production quiet period.
"""

import asyncio
import hashlib
import logging
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app.queue_manager import QueueManager
from app.rescan import (
    LidarrClient,
    LidarrConfig,
    NavidromeClient,
    NavidromeConfig,
    NoticeBoard,
    RescanConfig,
    RescanFailure,
    RescanHook,
    SCRUB_NOTICE_KEY,
    album_folder,
    load_config,
)

# A URL httpx cannot parse: ``notaport`` is not a port.  httpx raises
# InvalidURL for it, which is *not* an httpx.HTTPError -- the case that used to
# escape the clients and, through the startup task, break shutdown.
BAD_URL_LIDARR = LidarrConfig(
    url="http://lidarr:notaport", api_key="key", root_folder="/music"
)

# A quiet period short enough to await, long enough that three notifies issued
# back to back land inside it.
TEST_DEBOUNCE = 0.05

NAVIDROME = NavidromeConfig(url="http://navidrome:4533", user="admin", password="sesame")
LIDARR = LidarrConfig(url="http://lidarr:8686", api_key="key", root_folder="/music")


class Recorder:
    """A MockTransport handler that records requests and answers from a script.

    ``responses`` maps a URL path to either an ``httpx.Response`` or a callable
    taking the request; anything not listed is a 404, which shows up as a
    notice rather than as a silent pass.
    """

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.responses.get(request.url.path)
        if handler is None:
            return httpx.Response(404, json={"error": "no route in test"})
        if callable(handler):
            return handler(request)
        return handler

    def paths(self) -> list[str]:
        return [str(request.url.path) for request in self.requests]

    def count(self, path: str) -> int:
        return self.paths().count(path)


def client_for(recorder: Recorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder))


def subsonic_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "subsonic-response": {
                "status": "ok",
                "version": "1.16.1",
                "scanStatus": {"scanning": True, "count": 1},
            }
        },
    )


def subsonic_error(code: int, message: str = "boom") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "subsonic-response": {
                "status": "failed",
                "version": "1.16.1",
                "error": {"code": code, "message": message},
            }
        },
    )


def both_services_ok() -> Recorder:
    # The metadata-provider route is here because every successful Lidarr rescan
    # re-reads the tag-scrub setting; a healthy Lidarr answers "off".
    return Recorder(
        {
            "/rest/startScan": subsonic_ok(),
            "/api/v1/command": httpx.Response(201, json={"id": 1, "name": "RescanFolders"}),
            "/api/v1/config/metadataprovider": httpx.Response(
                200, json={"writeAudioTags": "newFiles", "scrubAudioTags": False}
            ),
        }
    )


def make_hook(recorder: Recorder, root, **kwargs) -> RescanHook:
    config = kwargs.pop("config", RescanConfig(navidrome=NAVIDROME, lidarr=LIDARR))
    return RescanHook(
        config,
        client_for(recorder),
        kwargs.pop("notices", NoticeBoard()),
        root=root,
        debounce_seconds=kwargs.pop("debounce_seconds", TEST_DEBOUNCE),
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_empty_environment_configures_nothing(self):
        config = load_config({})
        assert config.navidrome is None
        assert config.lidarr is None
        assert config.warnings == ()
        assert config.any_configured is False

    def test_empty_strings_count_as_unset(self):
        # docker compose substitutes an unset variable with "".
        config = load_config({"NAVIDROME_URL": "", "LIDARR_API_KEY": ""})
        assert config.navidrome is None
        assert config.lidarr is None
        assert config.warnings == ()

    def test_full_config_is_parsed_and_trailing_slashes_go(self):
        config = load_config(
            {
                "NAVIDROME_URL": "http://nd:4533/",
                "NAVIDROME_USER": "admin",
                "NAVIDROME_PASSWORD": "pw",
                "LIDARR_URL": "http://lidarr:8686//",
                "LIDARR_API_KEY": "abc",
                "LIDARR_ROOT_FOLDER": "/music",
            }
        )
        assert config.navidrome == NavidromeConfig("http://nd:4533", "admin", "pw")
        assert config.lidarr == LidarrConfig("http://lidarr:8686", "abc", "/music")

    def test_partial_navidrome_warns_and_stays_off(self):
        config = load_config({"NAVIDROME_URL": "http://nd:4533", "NAVIDROME_USER": "a"})
        assert config.navidrome is None
        assert len(config.warnings) == 1
        assert "NAVIDROME_PASSWORD" in config.warnings[0]

    def test_partial_lidarr_warns_and_stays_off(self):
        config = load_config({"LIDARR_API_KEY": "abc"})
        assert config.lidarr is None
        assert config.warnings == (
            "Lidarr is only half configured and will be skipped; unset: LIDARR_URL",
        )

    def test_root_folder_alone_is_not_a_lidarr_config(self):
        # LIDARR_ROOT_FOLDER is optional, so it must not make Lidarr look
        # half-configured when the two required vars are both absent.
        config = load_config({"LIDARR_ROOT_FOLDER": "/music"})
        assert config.lidarr is None
        assert config.warnings == ()


# ---------------------------------------------------------------------------
# Notice board
# ---------------------------------------------------------------------------


class TestNoticeBoard:
    def test_first_failure_raises_and_broadcasts_the_open_list(self):
        seen = []
        board = NoticeBoard(on_change=seen.append)
        notice = board.raise_notice("navidrome", "error", "bad password")
        assert notice is not None
        assert seen == [[notice]]
        assert board.open_notices() == [notice]

    def test_identical_failure_does_not_raise_twice(self):
        seen = []
        board = NoticeBoard(on_change=seen.append)
        board.raise_notice("navidrome", "error", "bad password")
        assert board.raise_notice("navidrome", "error", "bad password") is None
        assert len(seen) == 1
        assert len(board.open_notices()) == 1

    def test_a_clear_broadcasts_what_is_left(self):
        seen = []
        board = NoticeBoard(on_change=seen.append)
        board.raise_notice("navidrome", "error", "bad password")
        lidarr = board.raise_notice("lidarr", "error", "bad key")
        seen.clear()
        board.clear("navidrome")
        assert seen == [[lidarr]]

    def test_a_clear_with_nothing_to_remove_says_nothing(self):
        seen = []
        board = NoticeBoard(on_change=seen.append)
        board.raise_notice("lidarr", "error", "bad key")
        seen.clear()
        board.clear("navidrome")
        board.clear("navidrome")
        assert seen == []

    def test_variable_messages_with_one_key_are_one_notice(self):
        # Two connection errors from the same service differ only in the
        # exception text, and must not stack up two banners.
        board = NoticeBoard()
        first = board.raise_notice(
            "lidarr", "error", "Could not reach Lidarr: refused", key="unreachable"
        )
        second = board.raise_notice(
            "lidarr", "error", "Could not reach Lidarr: timed out", key="unreachable"
        )
        assert second is None
        # The first message is the one that stays open.
        assert board.open_notices() == [first]

    def test_a_sticky_notice_survives_a_clear(self):
        board = NoticeBoard()
        sticky = board.raise_notice("lidarr", "warning", "scrubbing is on", sticky=True)
        board.raise_notice("lidarr", "error", "bad key")
        board.clear("lidarr")
        assert board.open_notices() == [sticky]

    def test_a_clear_that_only_meets_sticky_notices_says_nothing(self):
        seen = []
        board = NoticeBoard(on_change=seen.append)
        board.raise_notice("lidarr", "warning", "scrubbing is on", sticky=True)
        seen.clear()
        board.clear("lidarr")
        assert seen == []

    def test_retract_removes_a_sticky_notice_and_broadcasts(self):
        seen = []
        board = NoticeBoard(on_change=seen.append)
        board.raise_notice(
            "lidarr", "warning", "scrubbing is on", key="scrub", sticky=True
        )
        seen.clear()
        board.retract("lidarr", "scrub")
        assert board.open_notices() == []
        assert seen == [[]]

    def test_retract_removes_a_plain_notice(self):
        board = NoticeBoard()
        board.raise_notice("lidarr", "error", "bad key")
        board.retract("lidarr", "bad key")
        assert board.open_notices() == []

    def test_retracting_nothing_says_nothing(self):
        seen = []
        board = NoticeBoard(on_change=seen.append)
        board.retract("lidarr", "scrub")
        assert seen == []

    def test_a_retracted_notice_can_be_raised_again_with_a_new_id(self):
        board = NoticeBoard()
        first = board.raise_notice(
            "lidarr", "warning", "scrubbing is on", key="scrub", sticky=True
        )
        board.retract("lidarr", "scrub")
        second = board.raise_notice(
            "lidarr", "warning", "scrubbing is on", key="scrub", sticky=True
        )
        assert second is not None and second.id != first.id
        # Still sticky the second time round.
        board.clear("lidarr")
        assert board.open_notices() == [second]

    def test_a_different_failure_is_its_own_notice(self):
        board = NoticeBoard()
        board.raise_notice("navidrome", "error", "bad password")
        board.raise_notice("navidrome", "error", "not an admin")
        assert len(board.open_notices()) == 2

    def test_success_clears_only_that_source(self):
        board = NoticeBoard()
        board.raise_notice("navidrome", "error", "bad password")
        board.raise_notice("lidarr", "error", "bad key")
        board.clear("navidrome")
        assert [n.source for n in board.open_notices()] == ["lidarr"]

    def test_repeat_after_a_clear_is_a_new_notice(self):
        board = NoticeBoard()
        first = board.raise_notice("navidrome", "error", "bad password")
        board.clear("navidrome")
        second = board.raise_notice("navidrome", "error", "bad password")
        assert second is not None
        assert second.id != first.id


# ---------------------------------------------------------------------------
# Album folder derivation
# ---------------------------------------------------------------------------


class TestAlbumFolder:
    def test_track_gives_its_album_folder(self, tmp_path):
        (tmp_path / "Bonobo" / "Fragments").mkdir(parents=True)
        assert album_folder("Bonobo/Fragments/01.flac", tmp_path) == "Bonobo/Fragments"

    def test_loose_single_gives_the_artist_folder(self, tmp_path):
        (tmp_path / "Bonobo").mkdir()
        assert album_folder("Bonobo/Kiara.flac", tmp_path) == "Bonobo"

    def test_a_folder_path_is_used_as_it_is(self, tmp_path):
        (tmp_path / "Bonobo" / "Fragments").mkdir(parents=True)
        assert album_folder("Bonobo/Fragments", tmp_path) == "Bonobo/Fragments"

    def test_root_level_file_gives_the_root(self, tmp_path):
        assert album_folder("stray.flac", tmp_path) == ""

    def test_a_path_that_escapes_the_root_is_dropped(self, tmp_path):
        assert album_folder("../etc/passwd", tmp_path) is None


# ---------------------------------------------------------------------------
# Navidrome client
# ---------------------------------------------------------------------------


class TestNavidromeClient:
    async def test_start_scan_sends_token_salt_auth(self):
        recorder = Recorder({"/rest/startScan": subsonic_ok()})
        async with client_for(recorder) as http:
            await NavidromeClient(NAVIDROME, http).start_scan()

        params = recorder.requests[0].url.params
        assert params["u"] == "admin"
        assert params["v"] == "1.16.1"
        assert params["c"] == "music-for-arr"
        assert params["f"] == "json"
        assert params["fullScan"] == "false"
        # t is md5(password + salt) for the salt that was sent, and the
        # password itself never appears in the URL.
        expected = hashlib.md5(("sesame" + params["s"]).encode()).hexdigest()
        assert params["t"] == expected
        assert "sesame" not in str(recorder.requests[0].url)

    async def test_salt_is_fresh_per_request(self):
        recorder = Recorder({"/rest/startScan": subsonic_ok()})
        async with client_for(recorder) as http:
            client = NavidromeClient(NAVIDROME, http)
            await client.start_scan()
            await client.start_scan()
        salts = {request.url.params["s"] for request in recorder.requests}
        assert len(salts) == 2

    @pytest.mark.parametrize(
        ("code", "expected"),
        [(40, "rejected the credentials"), (50, "is not an admin")],
    )
    async def test_known_error_codes_get_a_readable_message(self, code, expected):
        recorder = Recorder({"/rest/startScan": subsonic_error(code)})
        async with client_for(recorder) as http:
            with pytest.raises(RescanFailure) as exc:
                await NavidromeClient(NAVIDROME, http).start_scan()
        assert expected in str(exc.value)

    async def test_unknown_error_code_is_reported_verbatim(self):
        recorder = Recorder({"/rest/startScan": subsonic_error(70, "not found")})
        async with client_for(recorder) as http:
            with pytest.raises(RescanFailure, match="error 70: not found"):
                await NavidromeClient(NAVIDROME, http).start_scan()

    async def test_http_error_is_a_failure(self):
        recorder = Recorder({"/rest/startScan": httpx.Response(502, text="bad gateway")})
        async with client_for(recorder) as http:
            with pytest.raises(RescanFailure, match="502"):
                await NavidromeClient(NAVIDROME, http).start_scan()

    async def test_connection_error_is_a_failure(self):
        def boom(request):
            raise httpx.ConnectError("no route to host", request=request)

        async with client_for(Recorder({"/rest/startScan": boom})) as http:
            with pytest.raises(RescanFailure, match="Could not reach Navidrome"):
                await NavidromeClient(NAVIDROME, http).start_scan()

    async def test_an_unparseable_url_is_an_unreachable_failure(self):
        # InvalidURL is not an httpx.HTTPError; a bad port in NAVIDROME_URL has
        # to read as unreachable rather than escaping the client.
        bad = NavidromeConfig(
            url="http://navidrome:notaport", user="admin", password="sesame"
        )
        recorder = Recorder({})
        async with client_for(recorder) as http:
            with pytest.raises(RescanFailure) as caught:
                await NavidromeClient(bad, http).start_scan()

        assert caught.value.category == "unreachable"
        assert recorder.requests == []

    async def test_non_subsonic_body_is_a_failure(self):
        recorder = Recorder({"/rest/startScan": httpx.Response(200, text="<html>")})
        async with client_for(recorder) as http:
            with pytest.raises(RescanFailure, match="Subsonic response"):
                await NavidromeClient(NAVIDROME, http).start_scan()

    async def test_a_redirect_is_a_failure_and_is_not_followed(self):
        # A redirect means the URL is wrong; following it would carry the
        # Subsonic token to whatever host the Location header names.
        recorder = Recorder(
            {
                "/rest/startScan": httpx.Response(
                    302, headers={"Location": "https://elsewhere/rest/startScan"}
                )
            }
        )
        async with client_for(recorder) as http:
            with pytest.raises(RescanFailure, match="NAVIDROME_URL"):
                await NavidromeClient(NAVIDROME, http).start_scan()

        assert len(recorder.requests) == 1


# ---------------------------------------------------------------------------
# Lidarr client
# ---------------------------------------------------------------------------


class TestLidarrClient:
    async def test_rescan_posts_the_known_filter_command(self):
        recorder = Recorder({"/api/v1/command": httpx.Response(201, json={"id": 1})})
        async with client_for(recorder) as http:
            await LidarrClient(LIDARR, http).rescan()

        request = recorder.requests[0]
        assert request.method == "POST"
        assert request.headers["X-Api-Key"] == "key"
        import json

        assert json.loads(request.content) == {
            "name": "RescanFolders",
            "folders": ["/music"],
            "filter": "known",
            "addNewArtists": False,
        }

    async def test_root_folder_is_discovered_and_cached(self):
        recorder = Recorder(
            {
                "/api/v1/rootfolder": httpx.Response(
                    200,
                    json=[
                        {"id": 1, "path": "/data/music", "accessible": True},
                        {"id": 2, "path": "/other", "accessible": True},
                    ],
                ),
                "/api/v1/command": httpx.Response(201, json={"id": 1}),
            }
        )
        config = LidarrConfig(url="http://lidarr:8686", api_key="key", root_folder=None)
        async with client_for(recorder) as http:
            client = LidarrClient(config, http)
            await client.rescan()
            await client.rescan()

        import json

        bodies = [
            json.loads(r.content) for r in recorder.requests if r.method == "POST"
        ]
        assert [body["folders"] for body in bodies] == [["/data/music"]] * 2
        # Discovered once, then remembered.
        assert recorder.count("/api/v1/rootfolder") == 1

    async def test_configured_root_folder_skips_discovery(self):
        recorder = Recorder({"/api/v1/command": httpx.Response(201, json={"id": 1})})
        async with client_for(recorder) as http:
            await LidarrClient(LIDARR, http).rescan()
        assert recorder.count("/api/v1/rootfolder") == 0

    async def test_no_root_folders_is_a_failure(self):
        recorder = Recorder({"/api/v1/rootfolder": httpx.Response(200, json=[])})
        config = LidarrConfig(url="http://lidarr:8686", api_key="key", root_folder=None)
        async with client_for(recorder) as http:
            with pytest.raises(RescanFailure, match="no root folder"):
                await LidarrClient(config, http).rescan()

    async def test_a_redirect_is_a_failure_and_is_not_followed(self):
        # Following would replay the POST as a GET against another host, with
        # X-Api-Key attached; a 3xx is a wrong scheme or base path instead.
        recorder = Recorder(
            {
                "/api/v1/command": httpx.Response(
                    301, headers={"Location": "https://lidarr:8686/api/v1/command"}
                )
            }
        )
        async with client_for(recorder) as http:
            with pytest.raises(RescanFailure, match="LIDARR_URL"):
                await LidarrClient(LIDARR, http).rescan()

        assert len(recorder.requests) == 1

    async def test_an_html_200_is_not_an_accepted_command(self):
        # A login page or a proxy can answer 200 with HTML; only a command
        # resource with an id means Lidarr queued the rescan.
        recorder = Recorder({"/api/v1/command": httpx.Response(200, text="<html>")})
        async with client_for(recorder) as http:
            with pytest.raises(RescanFailure, match="did not answer with a command"):
                await LidarrClient(LIDARR, http).rescan()

    async def test_a_json_answer_without_an_id_is_a_failure(self):
        recorder = Recorder({"/api/v1/command": httpx.Response(200, json={"ok": True})})
        async with client_for(recorder) as http:
            with pytest.raises(RescanFailure, match="did not accept the rescan"):
                await LidarrClient(LIDARR, http).rescan()

    async def test_bad_api_key_is_a_readable_failure(self):
        recorder = Recorder({"/api/v1/command": httpx.Response(401, text="Unauthorized")})
        async with client_for(recorder) as http:
            with pytest.raises(RescanFailure, match="LIDARR_API_KEY"):
                await LidarrClient(LIDARR, http).rescan()

    async def test_an_unparseable_url_is_an_unreachable_failure(self):
        # httpx raises InvalidURL, not an HTTPError, for a URL with a bad port.
        # It has to come out as the same "could not reach" notice as a refused
        # connection rather than escaping the client.
        recorder = Recorder({})
        async with client_for(recorder) as http:
            with pytest.raises(RescanFailure) as caught:
                await LidarrClient(BAD_URL_LIDARR, http).rescan()

        assert caught.value.category == "unreachable"
        assert "Could not reach Lidarr" in str(caught.value)
        assert recorder.requests == []

    async def test_scrub_warning_when_scrubbing_is_on(self):
        recorder = Recorder(
            {
                "/api/v1/config/metadataprovider": httpx.Response(
                    200, json={"writeAudioTags": "no", "scrubAudioTags": True}
                )
            }
        )
        async with client_for(recorder) as http:
            warning = await LidarrClient(LIDARR, http).scrub_warning()
        assert warning is not None
        assert "scrub" in warning.lower()

    async def test_no_scrub_warning_when_it_is_off(self):
        recorder = Recorder(
            {
                "/api/v1/config/metadataprovider": httpx.Response(
                    200, json={"writeAudioTags": "newFiles", "scrubAudioTags": False}
                )
            }
        )
        async with client_for(recorder) as http:
            assert await LidarrClient(LIDARR, http).scrub_warning() is None


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


class TestRescanHook:
    async def test_one_change_calls_each_service_once(self, tmp_path):
        recorder = both_services_ok()
        hook = make_hook(recorder, tmp_path)
        try:
            hook.notify(["Bonobo/Fragments/01.flac"])
            await hook.flush()
        finally:
            await hook.aclose()

        assert recorder.count("/rest/startScan") == 1
        assert recorder.count("/api/v1/command") == 1

    async def test_three_quick_changes_collapse_into_one_run(self, tmp_path):
        recorder = both_services_ok()
        hook = make_hook(recorder, tmp_path)
        try:
            hook.notify(["Bonobo/Fragments/01.flac"])
            hook.notify(["Bonobo/Fragments/02.flac"])
            hook.notify(["Bonobo/Fragments/03.flac"])
            await hook.flush()
        finally:
            await hook.aclose()

        assert recorder.count("/rest/startScan") == 1
        assert recorder.count("/api/v1/command") == 1

    async def test_a_later_change_gets_its_own_run(self, tmp_path):
        recorder = both_services_ok()
        hook = make_hook(recorder, tmp_path)
        try:
            hook.notify(["a/b/1.flac"])
            await hook.flush()
            hook.notify(["a/b/2.flac"])
            await hook.flush()
        finally:
            await hook.aclose()

        assert recorder.count("/rest/startScan") == 2

    async def test_unconfigured_services_are_skipped_silently(self, tmp_path, caplog):
        recorder = both_services_ok()
        hook = make_hook(recorder, tmp_path, config=RescanConfig())
        with caplog.at_level(logging.WARNING):
            try:
                hook.notify(["Bonobo/Fragments/01.flac"])
                await hook.flush()
            finally:
                await hook.aclose()

        assert recorder.requests == []
        assert caplog.records == []

    async def test_changed_folders_are_touched(self, tmp_path):
        album = tmp_path / "Bonobo" / "Fragments"
        album.mkdir(parents=True)
        before = album.stat().st_mtime_ns
        os.utime(album, ns=(before - 10_000_000_000, before - 10_000_000_000))
        stale = album.stat().st_mtime_ns

        recorder = both_services_ok()
        hook = make_hook(recorder, tmp_path, config=RescanConfig())
        try:
            hook.notify(["Bonobo/Fragments/01.flac"])
            await hook.flush()
        finally:
            await hook.aclose()

        assert album.stat().st_mtime_ns > stale

    async def test_a_vanished_folder_is_not_an_error(self, tmp_path, caplog):
        recorder = both_services_ok()
        hook = make_hook(recorder, tmp_path)
        with caplog.at_level(logging.WARNING):
            try:
                hook.notify(["Gone/Album/01.flac"])
                await hook.flush()
            finally:
                await hook.aclose()

        assert recorder.count("/rest/startScan") == 1
        assert caplog.records == []

    async def test_no_paths_still_runs(self, tmp_path):
        recorder = both_services_ok()
        hook = make_hook(recorder, tmp_path)
        try:
            hook.notify([])
            await hook.flush()
        finally:
            await hook.aclose()

        assert recorder.count("/rest/startScan") == 1

    async def test_navidrome_failure_raises_one_notice_per_problem(self, tmp_path):
        recorder = Recorder(
            {
                "/rest/startScan": subsonic_error(40),
                "/api/v1/command": httpx.Response(201, json={"id": 1}),
            }
        )
        board = NoticeBoard()
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            for _ in range(3):
                hook.notify(["a/b/1.flac"])
                await hook.flush()
        finally:
            await hook.aclose()

        assert recorder.count("/rest/startScan") == 3
        notices = board.open_notices()
        assert len(notices) == 1
        assert notices[0].source == "navidrome"
        assert notices[0].level == "error"
        assert "credentials" in notices[0].message

    async def test_a_success_clears_the_notice(self, tmp_path):
        state = {"fail": True}

        def start_scan(request):
            return subsonic_error(40) if state["fail"] else subsonic_ok()

        recorder = Recorder(
            {
                "/rest/startScan": start_scan,
                "/api/v1/command": httpx.Response(201, json={"id": 1}),
            }
        )
        seen: list[list] = []
        board = NoticeBoard(on_change=seen.append)
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            hook.notify(["a/b/1.flac"])
            await hook.flush()
            assert len(board.open_notices()) == 1

            state["fail"] = False
            hook.notify(["a/b/1.flac"])
            await hook.flush()
            # The recovery is broadcast as the now-empty open list.
            assert seen[-1] == []
            emitted = len(seen)

            # A service that keeps working has nothing new to say.
            hook.notify(["a/b/1.flac"])
            await hook.flush()
        finally:
            await hook.aclose()

        assert board.open_notices() == []
        assert len(seen) == emitted

    async def test_a_success_does_not_clear_the_startup_scrub_warning(self, tmp_path):
        recorder = Recorder(
            {
                "/rest/startScan": subsonic_ok(),
                "/api/v1/command": httpx.Response(201, json={"id": 1}),
                "/api/v1/config/metadataprovider": httpx.Response(
                    200, json={"scrubAudioTags": True}
                ),
            }
        )
        board = NoticeBoard()
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            await hook.check_lidarr_config()
            assert len(board.open_notices()) == 1

            hook.notify(["a/b/1.flac"])
            await hook.flush()
        finally:
            await hook.aclose()

        # The warning is about a Lidarr setting, which a working rescan says
        # nothing about, so it must still be there.
        notices = board.open_notices()
        assert [n.source for n in notices] == ["lidarr"]
        assert "scrub" in notices[0].message

    async def test_two_unreachable_failures_are_one_notice(self, tmp_path):
        errors = iter(["connection refused", "timed out"])

        def boom(request):
            raise httpx.ConnectError(next(errors), request=request)

        recorder = Recorder(
            {"/rest/startScan": subsonic_ok(), "/api/v1/command": boom}
        )
        board = NoticeBoard()
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            for _ in range(2):
                hook.notify(["a/b/1.flac"])
                await hook.flush()
        finally:
            await hook.aclose()

        # Both failures carry different exception text; keyed on the kind of
        # failure they are one open notice, showing the first message.
        notices = board.open_notices()
        assert [n.source for n in notices] == ["lidarr"]
        assert "connection refused" in notices[0].message

    async def test_lidarr_401_raises_a_notice_and_navidrome_still_runs(self, tmp_path):
        recorder = Recorder(
            {
                "/rest/startScan": subsonic_ok(),
                "/api/v1/command": httpx.Response(401, text="Unauthorized"),
            }
        )
        board = NoticeBoard()
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            hook.notify(["a/b/1.flac"])
            await hook.flush()
        finally:
            await hook.aclose()

        assert recorder.count("/rest/startScan") == 1
        notices = board.open_notices()
        assert [n.source for n in notices] == ["lidarr"]
        assert "LIDARR_API_KEY" in notices[0].message

    async def test_startup_check_raises_a_warning_notice(self, tmp_path):
        recorder = Recorder(
            {
                "/api/v1/config/metadataprovider": httpx.Response(
                    200, json={"scrubAudioTags": True}
                )
            }
        )
        board = NoticeBoard()
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            await hook.check_lidarr_config()
        finally:
            await hook.aclose()

        notices = board.open_notices()
        assert len(notices) == 1
        assert notices[0].level == "warning"
        assert notices[0].source == "lidarr"

    async def test_startup_check_is_quiet_when_lidarr_is_unreachable(self, tmp_path, caplog):
        def boom(request):
            raise httpx.ConnectError("refused", request=request)

        recorder = Recorder({"/api/v1/config/metadataprovider": boom})
        board = NoticeBoard()
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            with caplog.at_level(logging.WARNING):
                await hook.check_lidarr_config()
        finally:
            await hook.aclose()

        # A Lidarr that is down at boot is logged, not banner-worthy: the next
        # rescan will raise the notice if it is still down then.
        assert board.open_notices() == []
        assert any("Could not read Lidarr" in r.message for r in caplog.records)

    async def test_a_rescan_retracts_the_warning_once_scrubbing_is_off(
        self, tmp_path
    ):
        # The warning is sticky, so nothing a plain success does can take it
        # down.  Only the re-check that runs after a successful rescan, having
        # read the setting as off, may retract it.
        scrubbing = {"on": True}

        def metadata(request):
            return httpx.Response(200, json={"scrubAudioTags": scrubbing["on"]})

        recorder = Recorder(
            {
                "/rest/startScan": subsonic_ok(),
                "/api/v1/command": httpx.Response(201, json={"id": 1}),
                "/api/v1/config/metadataprovider": metadata,
            }
        )
        seen: list[list] = []
        board = NoticeBoard(on_change=seen.append)
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            await hook.check_lidarr_config()
            assert len(board.open_notices()) == 1

            scrubbing["on"] = False
            seen.clear()
            hook.notify(["a/b/1.flac"])
            await hook.flush()
        finally:
            await hook.aclose()

        assert board.open_notices() == []
        # The retraction is broadcast, as the now-empty open list.
        assert seen and seen[-1] == []

    async def test_the_warning_comes_back_with_a_new_id_when_scrubbing_returns(
        self, tmp_path
    ):
        scrubbing = {"on": True}

        def metadata(request):
            return httpx.Response(200, json={"scrubAudioTags": scrubbing["on"]})

        recorder = Recorder(
            {
                "/rest/startScan": subsonic_ok(),
                "/api/v1/command": httpx.Response(201, json={"id": 1}),
                "/api/v1/config/metadataprovider": metadata,
            }
        )
        board = NoticeBoard()
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            await hook.check_lidarr_config()
            first = board.open_notices()[0]

            scrubbing["on"] = False
            hook.notify(["a/b/1.flac"])
            await hook.flush()
            assert board.open_notices() == []

            scrubbing["on"] = True
            hook.notify(["a/b/2.flac"])
            await hook.flush()
        finally:
            await hook.aclose()

        notices = board.open_notices()
        assert len(notices) == 1
        assert notices[0].id != first.id
        assert "scrub" in notices[0].message

    async def test_a_re_raised_warning_survives_the_clear_that_follows_it(
        self, tmp_path
    ):
        # _call clears the source after the coroutine returns, and the re-check
        # runs inside that coroutine -- so the freshly raised warning has to be
        # sticky enough to outlive the clear that comes right after it.
        recorder = Recorder(
            {
                "/rest/startScan": subsonic_ok(),
                "/api/v1/command": httpx.Response(201, json={"id": 1}),
                "/api/v1/config/metadataprovider": httpx.Response(
                    200, json={"scrubAudioTags": True}
                ),
            }
        )
        board = NoticeBoard()
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            hook.notify(["a/b/1.flac"])
            await hook.flush()
        finally:
            await hook.aclose()

        notices = board.open_notices()
        assert [n.source for n in notices] == ["lidarr"]
        assert "scrub" in notices[0].message

    async def test_a_failing_re_check_leaves_the_warning_alone(self, tmp_path):
        # Not being able to read the setting is not the same as reading it as
        # off, so a re-check that fails must change nothing.
        state = {"readable": True}

        def metadata(request):
            if not state["readable"]:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"scrubAudioTags": True})

        recorder = Recorder(
            {
                "/rest/startScan": subsonic_ok(),
                "/api/v1/command": httpx.Response(201, json={"id": 1}),
                "/api/v1/config/metadataprovider": metadata,
            }
        )
        board = NoticeBoard()
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            await hook.check_lidarr_config()
            warning = board.open_notices()[0]

            state["readable"] = False
            hook.notify(["a/b/1.flac"])
            await hook.flush()
        finally:
            await hook.aclose()

        # The rescan itself still succeeded, so no error notice either.
        assert board.open_notices() == [warning]

    async def test_a_failed_rescan_does_not_run_the_re_check(self, tmp_path):
        recorder = Recorder(
            {
                "/rest/startScan": subsonic_ok(),
                "/api/v1/command": httpx.Response(401, text="Unauthorized"),
                "/api/v1/config/metadataprovider": httpx.Response(
                    200, json={"scrubAudioTags": False}
                ),
            }
        )
        board = NoticeBoard()
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            hook.notify(["a/b/1.flac"])
            await hook.flush()
        finally:
            await hook.aclose()

        assert recorder.count("/api/v1/config/metadataprovider") == 0
        assert [n.source for n in board.open_notices()] == ["lidarr"]

    async def test_the_startup_check_survives_an_unparseable_lidarr_url(
        self, tmp_path, caplog
    ):
        # A LIDARR_URL httpx cannot parse raises InvalidURL, which is not an
        # HTTPError.  The startup probe runs as a background task whose failure
        # would surface at shutdown, so it has to swallow this too.
        recorder = Recorder({})
        board = NoticeBoard()
        hook = make_hook(
            recorder,
            tmp_path,
            notices=board,
            config=RescanConfig(lidarr=BAD_URL_LIDARR),
        )
        try:
            with caplog.at_level(logging.WARNING):
                await hook.check_lidarr_config()
        finally:
            await hook.aclose()

        assert board.open_notices() == []
        assert any("Lidarr" in record.message for record in caplog.records)

    async def test_the_startup_check_swallows_an_unexpected_error(
        self, tmp_path, caplog
    ):
        def boom(request):
            raise RuntimeError("something nobody predicted")

        recorder = Recorder({"/api/v1/config/metadataprovider": boom})
        board = NoticeBoard()
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            with caplog.at_level(logging.WARNING):
                await hook.check_lidarr_config()
        finally:
            await hook.aclose()

        assert board.open_notices() == []
        assert any(
            "Lidarr configuration check failed" in record.message
            for record in caplog.records
        )

    async def test_the_scrub_warning_uses_the_named_key(self, tmp_path):
        recorder = Recorder(
            {
                "/api/v1/config/metadataprovider": httpx.Response(
                    200, json={"scrubAudioTags": True}
                )
            }
        )
        board = NoticeBoard()
        hook = make_hook(recorder, tmp_path, notices=board)
        try:
            await hook.check_lidarr_config()
        finally:
            await hook.aclose()

        assert len(board.open_notices()) == 1
        board.retract("lidarr", SCRUB_NOTICE_KEY)
        assert board.open_notices() == []

    async def test_startup_check_does_nothing_without_lidarr(self, tmp_path):
        recorder = both_services_ok()
        hook = make_hook(recorder, tmp_path, config=RescanConfig(navidrome=NAVIDROME))
        try:
            await hook.check_lidarr_config()
        finally:
            await hook.aclose()
        assert recorder.requests == []

    async def test_aclose_drops_a_pending_run_without_blocking(self, tmp_path):
        recorder = both_services_ok()
        hook = make_hook(recorder, tmp_path, debounce_seconds=30)
        hook.notify(["a/b/1.flac"])
        await asyncio.wait_for(hook.aclose(), timeout=1)
        assert recorder.requests == []


# ---------------------------------------------------------------------------
# Wiring into the app
# ---------------------------------------------------------------------------


@pytest.fixture()
def main_module():
    """The app module with a clean notice board, restored afterwards.

    ``notice_board`` is a module singleton so ``GET /notices`` can read it
    without the route reaching into the lifespan, which means a notice raised
    by one test would otherwise show up in the next.
    """
    import app.main as module

    original = module.notice_board
    module.notice_board = NoticeBoard(on_change=module._on_notices_changed)
    try:
        yield module
    finally:
        module.notice_board = original


class TestAppWiring:
    def test_notices_route_is_empty_by_default(self, main_module):
        with TestClient(main_module.app) as client:
            assert client.get("/notices").json() == []

    def test_notices_route_returns_open_notices(self, main_module):
        with TestClient(main_module.app) as client:
            main_module.notice_board.raise_notice("lidarr", "warning", "scrubbing is on")
            body = client.get("/notices").json()

        assert len(body) == 1
        assert body[0]["source"] == "lidarr"
        assert body[0]["level"] == "warning"
        assert body[0]["message"] == "scrubbing is on"
        assert body[0]["id"] and body[0]["created_at"]

    async def test_a_notice_reaches_a_connected_sse_client(self, main_module):
        queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        async with main_module._sse_clients_lock:
            main_module._sse_clients.append(queue)
        original_loop = main_module._loop
        main_module._loop = asyncio.get_running_loop()
        try:
            main_module.notice_board.raise_notice("navidrome", "error", "bad password")
            event = await asyncio.wait_for(queue.get(), timeout=2)
        finally:
            main_module._loop = original_loop
            async with main_module._sse_clients_lock:
                main_module._sse_clients.remove(queue)

        assert event.event == "notices"
        assert event.job_id is None
        assert [n["source"] for n in event.data["notices"]] == ["navidrome"]
        assert event.data["notices"][0]["message"] == "bad password"

    async def test_a_clear_reaches_a_connected_sse_client_as_an_empty_list(
        self, main_module
    ):
        queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        async with main_module._sse_clients_lock:
            main_module._sse_clients.append(queue)
        original_loop = main_module._loop
        main_module._loop = asyncio.get_running_loop()
        try:
            main_module.notice_board.raise_notice("navidrome", "error", "bad password")
            await asyncio.wait_for(queue.get(), timeout=2)
            main_module.notice_board.clear("navidrome")
            event = await asyncio.wait_for(queue.get(), timeout=2)
        finally:
            main_module._loop = original_loop
            async with main_module._sse_clients_lock:
                main_module._sse_clients.remove(queue)

        assert event.event == "notices"
        assert event.data == {"notices": []}

    async def test_emit_library_changed_drives_the_hook(self, main_module, tmp_path):
        """The one public emitter is also the one trigger.

        Later phases (move, trash, tag) end by calling
        ``emit_library_changed``; this is what gets them a rescan for free.
        """
        recorder = both_services_ok()
        hook = make_hook(recorder, tmp_path)
        original_hook = main_module.rescan_hook
        original_loop = main_module._loop
        main_module.rescan_hook = hook
        main_module._loop = asyncio.get_running_loop()
        try:
            manager = QueueManager(on_event=main_module._on_queue_event)
            manager.emit_library_changed(["Bonobo/Fragments/01.flac"], job_id="job-1")
            # _on_queue_event hands the work to the loop, so let it land.
            await asyncio.sleep(0.05)
            await hook.flush()
        finally:
            main_module.rescan_hook = original_hook
            main_module._loop = original_loop
            await hook.aclose()

        assert recorder.count("/rest/startScan") == 1
        assert recorder.count("/api/v1/command") == 1
