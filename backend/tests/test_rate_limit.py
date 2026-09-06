"""Tests for the rate-limit lanes: detection, the state machine, and the queue.

The 429 exceptions here are *constructed* rather than mocked, in the exact shape
yt-dlp builds them (``DownloadError`` -> ``exc_info`` -> ``ExtractorError`` ->
``.cause`` -> ``HTTPError``) wrapped in our own ``DownloadError`` the way the
downloader wraps it.  That is the whole point of testing the detector: if yt-dlp
changes that shape, these fail rather than the app silently deciding that no
429 has ever happened.
"""

import asyncio
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

import pytest
import yt_dlp
from fastapi.testclient import TestClient

from app.downloader import DownloadError
from app.job_store import JobStore
from app.models import Job, JobStatus
from app.queue_manager import LANE_CONCURRENCY, QueueManager
from app.rate_limit import (
    BACKOFF_SECONDS,
    CEILING_SECONDS,
    NOTICE_ESCALATE_SECONDS,
    REASON_BOT_CHECK,
    REASON_RATE_LIMIT,
    LaneManager,
    LaneRecord,
    is_bot_check,
    lane_for_url,
    rate_limit_status,
    retry_after_seconds,
    source_for_host,
)

EPOCH = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Building the exceptions yt-dlp actually raises
# ---------------------------------------------------------------------------


class _FakeResponse:
    """The two attributes ``HTTPError`` reads, plus the headers we read."""

    def __init__(self, status: int = 429, headers: dict | None = None) -> None:
        self.status = status
        self.reason = "Too Many Requests"
        self.headers = headers or {}

    def close(self) -> None:
        pass


def _http_error(status: int = 429, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    return yt_dlp.networking.exceptions.HTTPError(_FakeResponse(status, headers))


def youtube_error(status: int = 429, retry_after=None, message=None) -> DownloadError:
    """A 429 as it reaches the queue: four exceptions deep, chained our way."""
    if message is None:
        message = f"HTTP Error {status}: Too Many Requests"
    try:
        raise _http_error(status, retry_after)
    except yt_dlp.networking.exceptions.HTTPError as http:
        extractor = yt_dlp.utils.ExtractorError(message, cause=http)
    try:
        raise extractor
    except yt_dlp.utils.ExtractorError:
        ytdlp_error = yt_dlp.utils.DownloadError(f"ERROR: {message}", sys.exc_info())
    ours = DownloadError(f"Download failed: {message}")
    ours.__cause__ = ytdlp_error
    return ours


def bot_check_error() -> DownloadError:
    """The sign-in wall, as YouTube words it and yt-dlp passes it on."""
    message = (
        "Sign in to confirm you’re not a bot. Use --cookies-from-browser or "
        "--cookies for the authentication."
    )
    try:
        raise yt_dlp.utils.ExtractorError(message, expected=True)
    except yt_dlp.utils.ExtractorError:
        ytdlp_error = yt_dlp.utils.DownloadError(f"ERROR: {message}", sys.exc_info())
    ours = DownloadError(f"Download failed: {message}")
    ours.__cause__ = ytdlp_error
    return ours


# ===========================================================================
# Detection
# ===========================================================================


class TestRateLimitStatus:
    def test_a_real_429_is_found_through_the_whole_chain(self):
        assert rate_limit_status(youtube_error()) == 429

    def test_a_bare_http_error_is_found(self):
        assert rate_limit_status(_http_error()) == 429

    def test_a_403_is_not_a_rate_limit(self):
        assert rate_limit_status(youtube_error(status=403)) is None

    def test_an_ordinary_failure_is_not_a_rate_limit(self):
        assert rate_limit_status(DownloadError("Video unavailable")) is None
        assert rate_limit_status(None) is None

    def test_youtubes_soft_rate_limit_counts_even_without_a_status(self):
        """A 200 whose player response says the video is unavailable.

        yt-dlp itself calls this a rate limit in the message it builds, so
        treating it as one is not a guess.
        """
        exc = DownloadError(
            "This content isn't available, try again later. The current session "
            "has been rate-limited by YouTube for up to an hour."
        )
        assert rate_limit_status(exc) == 429

    def test_a_message_only_429_still_counts(self):
        """``ignoreerrors`` eats the chain and leaves only yt-dlp's sentence."""
        assert rate_limit_status(DownloadError("HTTP Error 429: Too Many Requests")) == 429

    def test_a_context_chain_is_walked_too(self):
        try:
            try:
                raise _http_error()
            except Exception:
                raise DownloadError("something else")
        except DownloadError as exc:
            assert rate_limit_status(exc) == 429

    def test_a_cycle_in_the_chain_terminates(self):
        first = DownloadError("a")
        second = DownloadError("b")
        first.__cause__ = second
        second.__cause__ = first
        assert rate_limit_status(first) is None


class TestRetryAfter:
    def test_a_delta_in_seconds_is_read(self):
        assert retry_after_seconds(youtube_error(retry_after=90)) == 90.0

    def test_an_http_date_is_read(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=120)
        seconds = retry_after_seconds(youtube_error(retry_after=format_datetime(when)))
        assert seconds is not None and 110 < seconds <= 121

    def test_a_date_in_the_past_is_ignored(self):
        when = datetime.now(timezone.utc) - timedelta(hours=1)
        assert retry_after_seconds(youtube_error(retry_after=format_datetime(when))) is None

    def test_nonsense_is_ignored(self):
        assert retry_after_seconds(youtube_error(retry_after="soon")) is None

    def test_no_header_is_none(self):
        assert retry_after_seconds(youtube_error()) is None


class TestBotCheck:
    def test_youtubes_wording_is_recognised(self):
        assert is_bot_check(bot_check_error()) is True

    def test_a_straight_apostrophe_is_recognised_too(self):
        assert is_bot_check(DownloadError("Sign in to confirm you're not a bot")) is True

    def test_the_age_wall_is_a_different_problem(self):
        assert is_bot_check(DownloadError("Sign in to confirm your age")) is False

    def test_a_rate_limit_is_not_a_bot_check(self):
        assert is_bot_check(youtube_error()) is False


class TestHostClassification:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.youtube.com/watch?v=x", "youtube"),
            ("https://music.youtube.com/watch?v=x", "youtube"),
            ("https://youtu.be/x", "youtube"),
            ("https://soundcloud.com/a/b", "soundcloud"),
            ("https://glassbeams.bandcamp.com/album/x", "bandcamp"),
            ("https://vimeo.com/1", "other"),
            ("not a url", "other"),
        ],
    )
    def test_urls_land_in_the_right_lane(self, url, expected):
        assert source_for_host(url) == expected
        assert lane_for_url(url) == (expected if expected != "other" else None)

    def test_the_probe_agrees_with_the_queue(self):
        """The two classifiers must not disagree, or a lane could be held for a
        host the queue never puts jobs on."""
        from app.probe import _source_of

        for url in (
            "https://music.youtube.com/watch?v=x",
            "https://soundcloud.com/a/b",
            "https://x.bandcamp.com/track/y",
            "https://vimeo.com/1",
        ):
            assert _source_of({}, url) == source_for_host(url)


# ===========================================================================
# The state machine
# ===========================================================================


class _Clock:
    def __init__(self, now: datetime = EPOCH) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def manager(clock):
    return LaneManager(clock=clock)


class TestLaneStateMachine:
    def test_a_fresh_lane_is_open(self, manager):
        assert manager.is_held("youtube") is False
        assert manager.hold_message("youtube") is None
        assert manager.state("youtube").consecutive == 0

    def test_a_429_holds_the_lane_for_the_first_backoff(self, manager, clock):
        wait = manager.note_rate_limit("youtube")
        assert BACKOFF_SECONDS[0] * 0.8 <= wait <= BACKOFF_SECONDS[0] * 1.2
        assert manager.is_held("youtube") is True
        record = manager.state("youtube")
        assert record.reason == REASON_RATE_LIMIT
        assert record.consecutive == 1
        assert record.held_since == EPOCH

    def test_further_429s_lengthen_the_wait(self, manager, clock):
        waits = []
        for _ in range(6):
            waits.append(manager.note_rate_limit("youtube"))
            clock.advance(600)
        # The schedule doubles and then plateaus at the last entry.
        assert waits[0] < waits[1] < waits[2] < waits[3] < waits[4]
        assert waits[5] == pytest.approx(waits[4], rel=0.5)
        assert manager.state("youtube").consecutive == 6

    def test_a_longer_retry_after_wins(self, manager):
        wait = manager.note_rate_limit("youtube", retry_after=600)
        assert wait == pytest.approx(600, abs=1)

    def test_a_shorter_retry_after_does_not_shorten_the_schedule(self, manager):
        wait = manager.note_rate_limit("youtube", retry_after=1)
        assert wait >= BACKOFF_SECONDS[0] * 0.8

    def test_the_hold_elapses_and_the_lane_probes(self, manager, clock):
        manager.note_rate_limit("youtube")
        clock.advance(BACKOFF_SECONDS[0] * 1.5)
        lane = manager.lane("youtube")
        lane.park("first")
        lane.park("second")
        # Only the oldest waiter goes: the canary.
        assert lane.may_run("first", clock.now) is True
        assert lane.may_run("second", clock.now) is False

    def test_the_canarys_success_opens_the_lane_for_everyone(self, manager, clock):
        manager.note_rate_limit("youtube")
        clock.advance(BACKOFF_SECONDS[0] * 1.5)
        lane = manager.lane("youtube")
        lane.park("first")
        lane.park("second")
        assert lane.may_run("second", clock.now) is False
        manager.note_success("youtube")
        assert lane.may_run("second", clock.now) is True
        assert manager.state("youtube").consecutive == 0

    def test_the_canarys_429_holds_the_lane_again_for_longer(self, manager, clock):
        first = manager.note_rate_limit("youtube")
        clock.advance(first + 1)
        lane = manager.lane("youtube")
        lane.park("first")
        assert lane.may_run("first", clock.now) is True
        second = manager.note_rate_limit("youtube")
        assert second > first
        assert lane.may_run("first", clock.now) is False

    def test_a_canary_that_leaves_stands_down(self, manager, clock):
        manager.note_rate_limit("youtube")
        clock.advance(BACKOFF_SECONDS[0] * 1.5)
        lane = manager.lane("youtube")
        lane.park("first")
        lane.park("second")
        assert lane.may_run("first", clock.now) is True
        manager.leave("youtube", "first")
        assert lane.may_run("second", clock.now) is True

    def test_the_bot_check_holds_for_the_whole_ceiling_with_no_canary(
        self, manager, clock
    ):
        manager.note_bot_check("youtube")
        record = manager.state("youtube")
        assert record.reason == REASON_BOT_CHECK
        assert record.hold_until == EPOCH + timedelta(seconds=CEILING_SECONDS)
        clock.advance(CEILING_SECONDS - 1)
        lane = manager.lane("youtube")
        lane.park("first")
        assert lane.may_run("first", clock.now) is False

    def test_the_ceiling_is_reached_after_an_hour_of_trouble(self, manager, clock):
        manager.note_rate_limit("youtube")
        assert manager.lane("youtube").ceiling_reached(clock.now) is False
        # Five extensions of two minutes are still an hour of being limited.
        for _ in range(20):
            clock.advance(180)
            manager.note_rate_limit("youtube")
        assert manager.lane("youtube").ceiling_reached(clock.now) is True

    def test_the_ceiling_calls_back_and_resets_the_lane(self, manager, clock):
        seen = []
        manager.set_callbacks(on_ceiling=lambda host, reason: seen.append((host, reason)))
        manager.note_rate_limit("youtube")
        clock.advance(CEILING_SECONDS + 1)
        manager.fire_ceiling("youtube")
        assert seen == [("youtube", REASON_RATE_LIMIT)]
        assert manager.is_held("youtube") is False
        assert manager.state("youtube").held_since is None

    def test_resume_clears_the_hold_and_elects_one_canary(self, manager, clock):
        manager.note_rate_limit("youtube")
        lane = manager.lane("youtube")
        lane.park("first")
        lane.park("second")
        manager.resume("youtube")
        assert manager.is_held("youtube") is False
        assert lane.may_run("first", clock.now) is True
        assert lane.may_run("second", clock.now) is False
        assert manager.state("youtube").consecutive == 0

    def test_resume_on_an_empty_lane_just_opens_it(self, manager, clock):
        manager.note_bot_check("youtube")
        manager.resume("youtube")
        assert manager.is_held("youtube") is False
        assert manager.lane("youtube").may_run("anyone", clock.now) is True

    def test_lanes_do_not_interfere(self, manager):
        manager.note_rate_limit("youtube")
        assert manager.is_held("soundcloud") is False
        assert manager.is_held("bandcamp") is False


# ===========================================================================
# Persistence
# ===========================================================================


class TestLanePersistence:
    def test_a_hold_round_trips_through_the_store(self, tmp_path, clock):
        store = JobStore(tmp_path / "queue.db")
        try:
            first = LaneManager(clock=clock)
            first.attach_store(store)
            first.note_rate_limit("youtube", retry_after=300)

            second = LaneManager(clock=clock)
            second.attach_store(store)
            assert second.is_held("youtube") is True
            record = second.state("youtube")
            assert record.reason == REASON_RATE_LIMIT
            assert record.consecutive == 1
            assert record.held_since == EPOCH
        finally:
            store.close()

    def test_a_hold_that_lapsed_while_the_process_was_down_is_dropped(
        self, tmp_path, clock
    ):
        store = JobStore(tmp_path / "queue.db")
        try:
            first = LaneManager(clock=clock)
            first.attach_store(store)
            first.note_rate_limit("youtube")

            later = _Clock(EPOCH + timedelta(hours=2))
            second = LaneManager(clock=later)
            second.attach_store(store)
            assert second.is_held("youtube") is False
            assert store.load_lanes() == []
        finally:
            store.close()

    def test_a_success_clears_the_row(self, tmp_path, clock):
        store = JobStore(tmp_path / "queue.db")
        try:
            manager = LaneManager(clock=clock)
            manager.attach_store(store)
            manager.note_rate_limit("youtube")
            assert len(store.load_lanes()) == 1
            manager.note_success("youtube")
            assert store.load_lanes() == []
        finally:
            store.close()

    def test_the_lanes_table_is_created_by_the_migration(self, tmp_path):
        """A queue.db from before phase 15 gains the table rather than a rebuild."""
        path = tmp_path / "queue.db"
        first = JobStore(path)
        first._conn.execute("DROP TABLE lanes")
        first._conn.execute("PRAGMA user_version = 3")
        first._conn.close()

        second = JobStore(path)
        try:
            second.save_lane(LaneRecord(host="youtube", consecutive=2))
            assert [r.host for r in second.load_lanes()] == ["youtube"]
        finally:
            second.close()

    def test_delete_lane_is_forgiving(self, tmp_path):
        store = JobStore(tmp_path / "queue.db")
        try:
            store.delete_lane("youtube")  # no row, no error
            assert store.load_lanes() == []
        finally:
            store.close()


# ===========================================================================
# The download stage
# ===========================================================================


def _make_job(**overrides) -> Job:
    defaults = {
        "id": "job-1",
        "status": JobStatus.QUEUED,
        "title": "Test Track",
    }
    defaults.update(overrides)
    defaults.setdefault("url", f"https://www.youtube.com/watch?v={defaults['id']}")
    return Job(**defaults)


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached in time")


@pytest.fixture
def fast_backoff():
    """Shrink the schedule so a whole five-attempt budget runs in a moment."""
    with patch("app.rate_limit.BACKOFF_SECONDS", (0.05, 0.06, 0.07, 0.08, 0.09)):
        yield


class TestDownloadStageBackoff:
    @patch("app.queue_manager.download_audio")
    async def test_a_429_is_waited_out_and_the_job_finishes(
        self, mock_download, fast_backoff, tmp_path
    ):
        calls = []
        details = []

        def flaky(job, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise youtube_error()
            target = tmp_path / "Artist" / "Album" / "t.flac"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            return target

        mock_download.side_effect = flaky
        qm = QueueManager(max_concurrent=2, timeout=30)
        qm.add_job(_make_job())
        await _wait_until(lambda: qm.get_job("job-1").status is JobStatus.DONE, 10)

        job = qm.get_job("job-1")
        assert len(calls) == 2
        # The automatic attempt is not a manual retry and must not look like one.
        assert job.attempts == 0
        assert job.retry_at is None

    @patch("app.queue_manager.download_audio")
    async def test_the_wait_shows_a_countdown_on_the_job(
        self, mock_download, tmp_path
    ):
        """`detail` and `retry_at` are set for the duration of the wait."""
        seen: list[tuple[str | None, object]] = []

        def always_limited(job, *args, **kwargs):
            raise youtube_error()

        mock_download.side_effect = always_limited
        with patch("app.rate_limit.BACKOFF_SECONDS", (5, 5, 5, 5, 5)):
            qm = QueueManager(max_concurrent=2, timeout=30)
            qm.add_job(_make_job())
            await _wait_until(lambda: qm.get_job("job-1").retry_at is not None, 10)
            job = qm.get_job("job-1")
            seen.append((job.detail, job.retry_at))
            qm.cancel_job("job-1")
            await _wait_until(
                lambda: qm.get_job("job-1").status is JobStatus.CANCELLED, 10
            )

        detail, retry_at = seen[0]
        # The seconds carry +/-20%% jitter, so the shape is what is asserted.
        assert re.fullmatch(r"YouTube rate limit, retry 2 of 5 in [456] s", detail)
        assert retry_at is not None and retry_at > datetime.now(timezone.utc)

    @patch("app.queue_manager.download_audio")
    async def test_the_budget_runs_out_and_the_lane_stays_held(
        self, mock_download, fast_backoff
    ):
        attempts = []

        def always_limited(job, *args, **kwargs):
            attempts.append(1)
            raise youtube_error()

        mock_download.side_effect = always_limited
        qm = QueueManager(max_concurrent=2, timeout=30)
        qm.add_job(_make_job())
        await _wait_until(lambda: qm.get_job("job-1").status is JobStatus.ERROR, 10)

        job = qm.get_job("job-1")
        assert len(attempts) == 5
        assert job.error.startswith("YouTube rate limit: gave up after 5 attempts over")
        assert qm._lanes.is_held("youtube") is True

    @patch("app.queue_manager.download_audio")
    async def test_a_bot_check_fails_at_once_and_pauses_the_lane(self, mock_download):
        attempts = []

        def walled(job, *args, **kwargs):
            attempts.append(1)
            raise bot_check_error()

        mock_download.side_effect = walled
        qm = QueueManager(max_concurrent=2, timeout=30)
        qm.add_job(_make_job())
        await _wait_until(lambda: qm.get_job("job-1").status is JobStatus.ERROR, 10)

        assert len(attempts) == 1  # no attempt is spent retrying a wall
        error = qm.get_job("job-1").error
        assert "not a bot" in error
        assert "README" in error
        assert qm._lanes.state("youtube").reason == REASON_BOT_CHECK

    @patch("app.queue_manager.download_audio")
    async def test_a_cancel_during_the_wait_is_immediate(self, mock_download):
        def always_limited(job, *args, **kwargs):
            raise youtube_error()

        mock_download.side_effect = always_limited
        with patch("app.rate_limit.BACKOFF_SECONDS", (300, 300, 300, 300, 300)):
            qm = QueueManager(max_concurrent=2, timeout=30)
            qm.add_job(_make_job())
            await _wait_until(lambda: qm.get_job("job-1").retry_at is not None, 10)
            qm.cancel_job("job-1")
            # Five minutes of hold, but the cancel does not wait for it.
            await _wait_until(
                lambda: qm.get_job("job-1").status is JobStatus.CANCELLED, 5
            )

    @patch("app.queue_manager.download_audio")
    async def test_the_wait_does_not_come_out_of_the_download_timeout(
        self, mock_download, tmp_path
    ):
        """One `wait_for` per attempt: a wait longer than the timeout is fine."""
        calls = []

        def flaky(job, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise youtube_error()
            target = tmp_path / "Artist" / "Album" / "t.flac"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            return target

        mock_download.side_effect = flaky
        # The hold is twice the timeout; the job must still finish `done`.
        with patch("app.rate_limit.BACKOFF_SECONDS", (0.4, 0.4, 0.4, 0.4, 0.4)):
            qm = QueueManager(max_concurrent=2, timeout=1)
            qm.add_job(_make_job())
            await _wait_until(lambda: qm.get_job("job-1").status is JobStatus.DONE, 10)

    @patch("app.queue_manager.download_audio")
    async def test_a_non_429_failure_is_still_a_plain_failure(self, mock_download):
        mock_download.side_effect = DownloadError("Video unavailable")
        qm = QueueManager(max_concurrent=2, timeout=30)
        qm.add_job(_make_job())
        await _wait_until(lambda: qm.get_job("job-1").status is JobStatus.ERROR, 10)
        assert qm.get_job("job-1").error == "Video unavailable"
        assert qm._lanes.is_held("youtube") is False


class TestCanaryAndCeiling:
    @patch("app.queue_manager.download_audio")
    async def test_only_the_canary_spends_an_attempt(self, mock_download, tmp_path):
        """Two jobs, one hold: the second must not burn its budget waiting."""
        seen: list[str] = []
        first_done = asyncio.Event()

        def flaky(job, *args, **kwargs):
            seen.append(job.id)
            if job.id == "job-1" and seen.count("job-1") == 1:
                raise youtube_error()
            target = tmp_path / "Artist" / "Album" / f"{job.id}.flac"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            return target

        mock_download.side_effect = flaky
        with patch("app.rate_limit.BACKOFF_SECONDS", (0.2, 0.2, 0.2, 0.2, 0.2)):
            qm = QueueManager(max_concurrent=2, timeout=30)
            qm.add_job(_make_job(id="job-1"))
            qm.add_job(_make_job(id="job-2"))
            await _wait_until(
                lambda: all(
                    qm.get_job(i).status is JobStatus.DONE for i in ("job-1", "job-2")
                ),
                10,
            )
        # job-2 was never rate limited, so it only ever ran once.
        assert seen.count("job-2") == 1
        assert first_done.is_set() is False

    @patch("app.queue_manager.download_audio")
    async def test_the_ceiling_fails_waiting_and_queued_jobs_alike(
        self, mock_download
    ):
        def always_limited(job, *args, **kwargs):
            raise youtube_error()

        mock_download.side_effect = always_limited
        with patch("app.rate_limit.BACKOFF_SECONDS", (300, 300, 300, 300, 300)):
            qm = QueueManager(max_concurrent=5, timeout=30)
            for index in range(4):
                qm.add_job(_make_job(id=f"job-{index}"))
            await _wait_until(
                lambda: any(
                    qm.get_job(f"job-{i}").retry_at is not None for i in range(4)
                ),
                10,
            )
            # An hour has gone by, as far as the lane is concerned.
            lane = qm._lanes.lane("youtube")
            lane.held_since = datetime.now(timezone.utc) - timedelta(
                seconds=CEILING_SECONDS + 1
            )
            qm._lanes.fire_ceiling("youtube")
            await _wait_until(
                lambda: all(
                    qm.get_job(f"job-{i}").status is JobStatus.ERROR for i in range(4)
                ),
                10,
            )
        for index in range(4):
            assert (
                qm.get_job(f"job-{index}").error
                == "YouTube rate limited for over an hour"
            )

    @patch("app.queue_manager.download_audio")
    async def test_resume_releases_a_waiting_job_at_once(self, mock_download, tmp_path):
        calls = []

        def flaky(job, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise youtube_error()
            target = tmp_path / "Artist" / "Album" / "t.flac"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            return target

        mock_download.side_effect = flaky
        with patch("app.rate_limit.BACKOFF_SECONDS", (600, 600, 600, 600, 600)):
            qm = QueueManager(max_concurrent=2, timeout=30)
            qm.add_job(_make_job())
            await _wait_until(lambda: qm.get_job("job-1").retry_at is not None, 10)
            qm.resume_lane("youtube")
            await _wait_until(lambda: qm.get_job("job-1").status is JobStatus.DONE, 10)


class TestYouTubeConcurrencyCap:
    def test_the_cap_is_two(self):
        assert LANE_CONCURRENCY == {"youtube": 2}

    @patch("app.queue_manager.download_audio")
    async def test_at_most_two_youtube_downloads_run_at_once(
        self, mock_download, tmp_path
    ):
        running = 0
        peak = 0
        gate = asyncio.Event()
        loop = asyncio.get_running_loop()

        def slow(job, *args, **kwargs):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            # Park until the test releases everything at once.
            asyncio.run_coroutine_threadsafe(gate.wait(), loop).result(5)
            running -= 1
            target = tmp_path / "Artist" / "Album" / f"{job.id}.flac"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            return target

        mock_download.side_effect = slow
        qm = QueueManager(max_concurrent=5, timeout=30)
        for index in range(5):
            qm.add_job(_make_job(id=f"job-{index}"))
        await _wait_until(lambda: running == 2, 5)
        await asyncio.sleep(0.2)
        assert running == 2
        loop.call_soon_threadsafe(gate.set)
        await _wait_until(
            lambda: all(
                qm.get_job(f"job-{i}").status is JobStatus.DONE for i in range(5)
            ),
            10,
        )
        assert peak == 2

    @patch("app.queue_manager.download_audio")
    async def test_other_hosts_fill_the_remaining_slots(self, mock_download, tmp_path):
        """A capped YouTube lane must not stall SoundCloud."""
        running: set[str] = set()
        gate = asyncio.Event()
        loop = asyncio.get_running_loop()

        def slow(job, *args, **kwargs):
            running.add(job.id)
            asyncio.run_coroutine_threadsafe(gate.wait(), loop).result(5)
            running.discard(job.id)
            target = tmp_path / "Artist" / "Album" / f"{job.id}.flac"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            return target

        mock_download.side_effect = slow
        qm = QueueManager(max_concurrent=5, timeout=30)
        for index in range(4):
            qm.add_job(_make_job(id=f"yt-{index}"))
        for index in range(2):
            qm.add_job(
                _make_job(
                    id=f"sc-{index}", url=f"https://soundcloud.com/a/track-{index}"
                )
            )
        await _wait_until(lambda: len(running) == 4, 5)
        assert {job_id for job_id in running if job_id.startswith("sc-")} == {
            "sc-0",
            "sc-1",
        }
        assert len([j for j in running if j.startswith("yt-")]) == 2
        loop.call_soon_threadsafe(gate.set)


# ===========================================================================
# The probe
# ===========================================================================


class TestProbeUnderARateLimit:
    """`POST /download/probe` never backs off: a person is watching the form."""

    async def test_a_429_becomes_a_400_and_starts_the_hold(self):
        from app import probe as probe_module
        from app.probe import RateLimitedProbeError, probe

        url = "https://www.youtube.com/playlist?list=PLtest"

        def raise_429(url, deadline):
            raise probe_module._probe_error(
                "HTTP Error 429: Too Many Requests", url, youtube_error()
            )

        with patch.object(probe_module, "_enumerate", raise_429):
            with pytest.raises(RateLimitedProbeError) as caught:
                await probe(url)

        assert "YouTube is rate limiting this server, try again in" in str(caught.value)
        assert probe_module.rate_limit.lanes.is_held("youtube") is True

    async def test_a_held_lane_refuses_without_a_single_request(self):
        from app import probe as probe_module
        from app.probe import RateLimitedProbeError, probe

        probe_module.rate_limit.lanes.note_rate_limit("youtube")
        called = []

        def should_not_run(url, deadline):
            called.append(url)
            raise AssertionError("the probe should not have run")

        with patch.object(probe_module, "_enumerate", should_not_run):
            with pytest.raises(RateLimitedProbeError) as caught:
                await probe("https://www.youtube.com/playlist?list=PLtest")

        assert called == []
        assert "rate limiting this server" in str(caught.value)

    async def test_a_bot_check_hold_says_so_instead(self):
        from app import probe as probe_module
        from app.probe import RateLimitedProbeError, probe

        probe_module.rate_limit.lanes.note_bot_check("youtube")
        with patch.object(probe_module, "_enumerate", lambda *a: None):
            with pytest.raises(RateLimitedProbeError) as caught:
                await probe("https://www.youtube.com/playlist?list=PLtest")
        assert "not a bot" in str(caught.value)

    async def test_another_hosts_hold_does_not_block_this_one(self):
        from app import probe as probe_module
        from app.probe import SingleTrack, probe

        probe_module.rate_limit.lanes.note_rate_limit("youtube")
        answer = SingleTrack(
            title="t", duration=1.0, thumbnail_url=None, artist=None, album=None
        )
        with patch.object(probe_module, "_enumerate", lambda *a: answer):
            assert await probe("https://soundcloud.com/a/b") is answer

    def test_the_message_reaches_the_route_unprefixed(self):
        """A rate limit is not "Failed to probe": nothing of ours failed."""
        import app.main as main_module
        from app.probe import RateLimitedProbeError

        message = "YouTube is rate limiting this server, try again in 30 s"
        with TestClient(main_module.app) as client:
            with patch.object(
                main_module, "probe", side_effect=RateLimitedProbeError(message)
            ):
                response = client.post(
                    "/download/probe",
                    json={"url": "https://www.youtube.com/playlist?list=PLtest"},
                )
        assert response.status_code == 400
        assert response.json()["detail"] == message


# ===========================================================================
# The banner and the Resume button
# ===========================================================================


class TestLaneNoticeAndResume:
    @pytest.fixture()
    def client(self):
        import app.main as main_module

        with TestClient(main_module.app) as client:
            yield client
        main_module.notice_board.retract("youtube", "rate_limit:youtube")

    def test_a_hold_raises_one_notice_with_a_resume_action(self, client):
        import app.main as main_module

        main_module.rate_limit_lanes.note_rate_limit("youtube")
        notices = client.get("/notices").json()
        lane_notices = [n for n in notices if n["source"] == "youtube"]
        assert len(lane_notices) == 1
        notice = lane_notices[0]
        assert notice["level"] == "warning"
        assert "rate limiting this server" in notice["message"]
        assert notice["action"] == {
            "label": "Resume now",
            "method": "POST",
            "path": "/queue/lanes/youtube/resume",
        }

    def test_a_bot_check_notice_is_an_error_pointing_at_the_readme(self, client):
        import app.main as main_module

        main_module.rate_limit_lanes.note_bot_check("youtube")
        notice = [
            n for n in client.get("/notices").json() if n["source"] == "youtube"
        ][0]
        assert notice["level"] == "error"
        assert "README" in notice["message"]

    def test_the_notice_gets_a_new_id_when_the_hold_changes(self, client):
        import app.main as main_module

        main_module.rate_limit_lanes.note_rate_limit("youtube")
        first = [n for n in client.get("/notices").json() if n["source"] == "youtube"][0]
        main_module.rate_limit_lanes.note_rate_limit("youtube")
        second = [n for n in client.get("/notices").json() if n["source"] == "youtube"][0]
        assert second["id"] != first["id"]

    def test_the_notice_goes_away_when_the_lane_opens(self, client):
        import app.main as main_module

        main_module.rate_limit_lanes.note_rate_limit("youtube")
        assert any(n["source"] == "youtube" for n in client.get("/notices").json())
        main_module.rate_limit_lanes.note_success("youtube")
        assert not any(n["source"] == "youtube" for n in client.get("/notices").json())

    def test_resume_clears_the_hold(self, client):
        import app.main as main_module

        main_module.rate_limit_lanes.note_rate_limit("youtube")
        response = client.post("/queue/lanes/youtube/resume")
        assert response.status_code == 200
        assert response.json() == {
            "host": "youtube",
            "held": False,
            "hold_until": None,
            "reason": None,
            "consecutive": 0,
        }
        assert main_module.rate_limit_lanes.is_held("youtube") is False

    def test_resume_on_an_open_lane_is_a_clean_no_op(self, client):
        response = client.post("/queue/lanes/soundcloud/resume")
        assert response.status_code == 200
        assert response.json()["held"] is False

    def test_an_unknown_host_is_a_404(self, client):
        assert client.post("/queue/lanes/vimeo/resume").status_code == 404


# ===========================================================================
# Prevention: the options every session starts from
# ===========================================================================


class TestYtDlpPacingAndPoTokens:
    def test_requests_are_paced_by_default(self):
        from app.downloader import DEFAULT_SLEEP_INTERVAL_REQUESTS, base_opts

        with patch.dict("os.environ", {}, clear=True):
            assert (
                base_opts()["sleep_interval_requests"]
                == DEFAULT_SLEEP_INTERVAL_REQUESTS
                == 0.75
            )

    def test_the_pacing_is_configurable(self):
        from app.downloader import base_opts

        with patch.dict("os.environ", {"YTDLP_SLEEP_REQUESTS": "2.5"}, clear=True):
            assert base_opts()["sleep_interval_requests"] == 2.5

    def test_zero_turns_the_pacing_off_entirely(self):
        from app.downloader import base_opts

        with patch.dict("os.environ", {"YTDLP_SLEEP_REQUESTS": "0"}, clear=True):
            assert "sleep_interval_requests" not in base_opts()

    def test_nonsense_falls_back_to_the_default(self):
        from app.downloader import base_opts

        with patch.dict("os.environ", {"YTDLP_SLEEP_REQUESTS": "soon"}, clear=True):
            assert base_opts()["sleep_interval_requests"] == 0.75

    def test_the_probe_is_paced_too(self):
        """The flat enumeration is the burstiest thing this app does."""
        from app.probe import _flat_opts

        with patch.dict("os.environ", {}, clear=True):
            assert _flat_opts()["sleep_interval_requests"] == 0.75

    def test_the_sidecar_is_wired_through_the_plugins_documented_key(self):
        from app.downloader import base_opts

        with patch.dict(
            "os.environ", {"POT_PROVIDER_URL": "http://pot-provider:4416"}, clear=True
        ):
            assert base_opts()["extractor_args"] == {
                "youtubepot-bgutilhttp": {"base_url": ["http://pot-provider:4416"]}
            }

    def test_no_sidecar_leaves_the_plugin_at_its_own_default(self):
        from app.downloader import base_opts

        with patch.dict("os.environ", {"POT_PROVIDER_URL": ""}, clear=True):
            assert "extractor_args" not in base_opts()

    def test_the_provider_listing_never_raises(self):
        """It reads a private yt-dlp registry; a yt-dlp that moves it costs a
        log line, never a boot."""
        from app.downloader import describe_pot_providers

        assert isinstance(describe_pot_providers(), list)


class TestRateLimitAttemptsConfig:
    def test_the_default_is_five(self):
        from app.rate_limit import rate_limit_attempts

        with patch.dict("os.environ", {}, clear=True):
            assert rate_limit_attempts() == 5

    def test_the_env_wins(self):
        from app.rate_limit import rate_limit_attempts

        with patch.dict("os.environ", {"RATE_LIMIT_ATTEMPTS": "8"}, clear=True):
            assert rate_limit_attempts() == 8

    def test_zero_attempts_is_raised_to_one(self):
        from app.rate_limit import rate_limit_attempts

        with patch.dict("os.environ", {"RATE_LIMIT_ATTEMPTS": "0"}, clear=True):
            assert rate_limit_attempts() == 1

    def test_nonsense_falls_back(self):
        from app.rate_limit import rate_limit_attempts

        with patch.dict("os.environ", {"RATE_LIMIT_ATTEMPTS": "lots"}, clear=True):
            assert rate_limit_attempts() == 5


# ===========================================================================
# Review round: the notice does not churn, and a waiter is never stale
# ===========================================================================


class TestTheBannerDoesNotChurn:
    """A watchdog tick is not news, so it must not re-raise the notice.

    Raising a notice afresh is what un-dismisses a banner and gives it a new
    id, so a tick that re-raised would bring a dismissed banner back every
    fifteen seconds. The countdown is the banner's own job, from the
    ``hold_until`` the notice carries.
    """

    async def test_a_watchdog_tick_on_a_held_lane_says_nothing(self, clock):
        seen: list[LaneRecord] = []
        manager = LaneManager(clock=clock)
        manager.set_callbacks(on_change=lambda host, record: seen.append(record))
        with patch("app.rate_limit.WATCHDOG_INTERVAL_SECONDS", 0.01):
            manager.note_rate_limit("youtube")
            assert len(seen) == 1
            await asyncio.sleep(0.08)  # several ticks
        assert len(seen) == 1
        manager.close()

    async def test_the_escalation_is_announced_exactly_once(self, clock):
        seen: list[LaneRecord] = []
        manager = LaneManager(clock=clock)
        manager.set_callbacks(on_change=lambda host, record: seen.append(record))
        with patch("app.rate_limit.WATCHDOG_INTERVAL_SECONDS", 0.01):
            manager.note_rate_limit("youtube", retry_after=CEILING_SECONDS - 60)
            clock.advance(NOTICE_ESCALATE_SECONDS + 1)
            await asyncio.sleep(0.08)
        assert len(seen) == 2  # the hold, then the escalation
        manager.close()

    def test_the_message_carries_no_countdown(self):
        from app.rate_limit import notice_message

        text = notice_message("youtube", REASON_RATE_LIMIT, 0)
        assert text == (
            "YouTube is rate limiting this server. Downloads from YouTube are paused."
        )
        assert " s." not in text

    def test_the_escalated_message_points_at_the_readme(self):
        from app.rate_limit import notice_message

        text = notice_message("youtube", REASON_RATE_LIMIT, NOTICE_ESCALATE_SECONDS)
        assert "30 minutes" in text and "README" in text

    def test_the_notice_carries_the_instant_the_banner_counts_down_from(self):
        import app.main as main_module

        with TestClient(main_module.app) as client:
            main_module.rate_limit_lanes.note_rate_limit("youtube")
            notice = [
                n for n in client.get("/notices").json() if n["source"] == "youtube"
            ][0]
            assert notice["reason"] == REASON_RATE_LIMIT
            assert notice["hold_until"] is not None
            assert notice["held_since"] is not None
        main_module.notice_board.retract("youtube", "rate_limit:youtube")


class TestTheWaitNoteStaysCurrent:
    def test_a_pure_waiter_is_not_told_it_is_on_retry_one(self):
        from app.rate_limit import wait_detail

        assert wait_detail("youtube", 45.0) == "YouTube rate limit, waiting 45 s"

    def test_a_job_that_has_spent_attempts_gets_the_numbers(self):
        from app.rate_limit import wait_detail

        assert (
            wait_detail("youtube", 45.0, attempt=2, total=5)
            == "YouTube rate limit, retry 2 of 5 in 45 s"
        )

    def test_a_probing_lane_has_nothing_to_count_down_to(self):
        from app.rate_limit import wait_detail

        assert wait_detail("youtube", None) == (
            "YouTube rate limit, waiting for the first download to get through"
        )

    @patch("app.queue_manager.download_audio")
    async def test_a_waiter_re_announces_when_the_canary_is_limited_again(
        self, mock_download
    ):
        """The second job must not be left showing an instant that has passed.

        One download slot, so the roles are fixed: job-1 meets the limiter and
        becomes the canary, job-2 only ever waits behind it.
        """

        def only_the_first_is_limited(job, *args, **kwargs):
            if job.id == "job-1":
                raise youtube_error()
            raise DownloadError("Video unavailable")

        mock_download.side_effect = only_the_first_is_limited
        with patch("app.rate_limit.BACKOFF_SECONDS", (0.3, 5, 5, 5, 5)):
            qm = QueueManager(max_concurrent=1, timeout=30)
            qm.add_job(_make_job(id="job-1"))
            qm.add_job(_make_job(id="job-2"))
            await _wait_until(lambda: qm.get_job("job-2").retry_at is not None, 10)
            first = qm.get_job("job-2").retry_at
            first_detail = qm.get_job("job-2").detail
            # The canary comes back, is limited again, and extends the hold.
            await _wait_until(
                lambda: qm.get_job("job-2").retry_at not in (None, first), 10
            )
            second = qm.get_job("job-2").retry_at
            qm.cancel_job("job-1")
            qm.cancel_job("job-2")
            await _wait_until(
                lambda: qm.get_job("job-2").status is JobStatus.CANCELLED, 10
            )

        assert second > first
        # And it never claims an attempt this job did not make.
        assert first_detail.startswith("YouTube rate limit, waiting ")

    @patch("app.queue_manager.download_audio")
    async def test_a_waiter_says_so_while_the_canary_is_in_flight(
        self, mock_download
    ):
        """A hold that has elapsed leaves nothing to count down to."""
        parked = asyncio.Event()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()
        calls: list[str] = []

        def handler(job, *args, **kwargs):
            if job.id != "job-1":
                raise DownloadError("Video unavailable")
            calls.append(job.id)
            if len(calls) == 1:
                raise youtube_error()
            # The canary's second run parks, so the lane stays probing.
            loop.call_soon_threadsafe(parked.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(10)
            raise DownloadError("Video unavailable")

        mock_download.side_effect = handler
        with patch("app.rate_limit.BACKOFF_SECONDS", (0.2, 0.2, 0.2, 0.2, 0.2)):
            qm = QueueManager(max_concurrent=1, timeout=30)
            qm.add_job(_make_job(id="job-1"))
            qm.add_job(_make_job(id="job-2"))
            await asyncio.wait_for(parked.wait(), 10)
            await _wait_until(
                lambda: qm.get_job("job-2").detail
                == "YouTube rate limit, waiting for the first download to get through",
                10,
            )
            assert qm.get_job("job-2").retry_at is None
            loop.call_soon_threadsafe(release.set)
            await _wait_until(
                lambda: qm.get_job("job-2").status is JobStatus.ERROR, 10
            )


class TestTheWaitingJobFreesItsDownloadSlot:
    @patch("app.queue_manager.download_audio")
    async def test_another_source_downloads_while_youtube_waits(
        self, mock_download, tmp_path
    ):
        """Two parked YouTube jobs must not stop a SoundCloud job for 8 minutes."""
        started: set[str] = set()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def handler(job, *args, **kwargs):
            started.add(job.id)
            if job.id.startswith("yt-"):
                raise youtube_error()
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(10)
            target = tmp_path / "A" / "B" / f"{job.id}.flac"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            return target

        mock_download.side_effect = handler
        # Both download slots are what the two YouTube jobs would hold if they
        # kept them while waiting.
        with patch("app.rate_limit.BACKOFF_SECONDS", (300, 300, 300, 300, 300)):
            qm = QueueManager(max_concurrent=2, timeout=30)
            qm.add_job(_make_job(id="yt-0"))
            qm.add_job(_make_job(id="yt-1"))
            await _wait_until(
                lambda: all(
                    qm.get_job(f"yt-{i}").retry_at is not None for i in range(2)
                ),
                10,
            )
            qm.add_job(_make_job(id="sc-0", url="https://soundcloud.com/a/b"))
            await _wait_until(lambda: "sc-0" in started, 5)
            loop.call_soon_threadsafe(release.set)
            await _wait_until(
                lambda: qm.get_job("sc-0").status is JobStatus.DONE, 10
            )
            qm.cancel_job("yt-0")
            qm.cancel_job("yt-1")

    @patch("app.queue_manager.download_audio")
    async def test_the_slot_comes_back_before_the_next_attempt(
        self, mock_download, tmp_path
    ):
        calls = []

        def flaky(job, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise youtube_error()
            target = tmp_path / "A" / "B" / "t.flac"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            return target

        mock_download.side_effect = flaky
        with patch("app.rate_limit.BACKOFF_SECONDS", (0.05, 0.05, 0.05, 0.05, 0.05)):
            qm = QueueManager(max_concurrent=1, timeout=30)
            qm.add_job(_make_job())
            await _wait_until(lambda: qm.get_job("job-1").status is JobStatus.DONE, 10)
        # The slot was handed back and re-taken, and is free again at the end.
        assert qm._semaphore._value == 1


class TestRestartWhileWaiting:
    async def test_a_job_parked_on_a_hold_spends_no_restart_attempt(self, tmp_path):
        store = JobStore(tmp_path / "queue.db")
        try:
            job = _make_job(status=JobStatus.DOWNLOADING)
            store.upsert(job)

            manager = LaneManager()
            manager.note_rate_limit("youtube")
            qm = QueueManager(max_concurrent=1, timeout=10, store=store, lanes=manager)
            restored = qm.restore_from_store()

            recovered = restored[0]
            assert recovered.status is JobStatus.QUEUED
            assert recovered.attempts == 0
            assert recovered.restart_attempts == 0
        finally:
            store.close()

    async def test_a_job_interrupted_on_an_open_lane_still_spends_one(self, tmp_path):
        store = JobStore(tmp_path / "queue.db")
        try:
            store.upsert(_make_job(status=JobStatus.DOWNLOADING))
            qm = QueueManager(
                max_concurrent=1, timeout=10, store=store, lanes=LaneManager()
            )
            recovered = qm.restore_from_store()[0]
            assert recovered.status is JobStatus.QUEUED
            assert recovered.attempts == 1
            assert recovered.restart_attempts == 1
        finally:
            store.close()


class TestProbeLaneForSpotify:
    async def test_a_spotify_url_is_refused_while_youtube_is_held(self):
        from app import probe as probe_module
        from app.probe import RateLimitedProbeError, probe

        probe_module.rate_limit.lanes.note_rate_limit("youtube")
        called = []

        with patch.object(
            probe_module, "_enumerate", lambda *a: called.append(a) or None
        ):
            with pytest.raises(RateLimitedProbeError):
                await probe("https://open.spotify.com/artist/0TnOYISbd1XYRBk9myaseg")
        assert called == []

    def test_the_probe_lane_for_spotify_is_youtube(self):
        from app.probe import _lane_for_probe

        assert _lane_for_probe("https://open.spotify.com/artist/x") == "youtube"
        assert _lane_for_probe("https://soundcloud.com/a/b") == "soundcloud"


class TestYouTubeMusicRateLimits:
    """`ytmusicapi` has no status attribute, so the message is all there is."""

    def _server_error(self):
        from app.ytmusic import YouTubeMusicUnavailable

        try:
            raise Exception("Server returned HTTP 429: Too Many Requests.")
        except Exception as exc:
            return YouTubeMusicUnavailable(f"YTMusicServerError: {exc}")

    def test_ytmusicapis_wording_is_recognised(self):
        assert rate_limit_status(self._server_error()) == 429

    def test_the_channel_branch_raises_instead_of_falling_back(self):
        from app import probe as probe_module
        from app.probe import RateLimitedProbeError

        with (
            patch.object(probe_module, "resolve_channel_id", return_value="UC123"),
            patch.object(
                probe_module, "fetch_artist", side_effect=self._server_error()
            ),
        ):
            with pytest.raises(RateLimitedProbeError) as caught:
                probe_module._ytmusic_enumeration(
                    "https://www.youtube.com/@artist", time.monotonic() + 30
                )
        assert caught.value.host == "youtube"

    def test_the_spotify_branch_raises_instead_of_a_generic_message(self):
        from app import probe as probe_module
        from app.probe import RateLimitedProbeError

        with (
            patch.object(probe_module, "resolve_artist_name", return_value="Bonobo"),
            patch.object(
                probe_module, "search_artist", side_effect=self._server_error()
            ),
        ):
            with pytest.raises(RateLimitedProbeError) as caught:
                probe_module._spotify_enumeration(
                    "https://open.spotify.com/artist/0TnOYISbd1XYRBk9myaseg",
                    time.monotonic() + 30,
                )
        assert caught.value.host == "youtube"


class TestNoticeActionPath:
    def test_a_backslash_path_is_refused(self):
        from app.models import NoticeAction

        with pytest.raises(Exception):
            NoticeAction(label="Go", method="POST", path="/\\evil.example/x")

    def test_an_absolute_url_is_refused(self):
        from app.models import NoticeAction

        with pytest.raises(Exception):
            NoticeAction(label="Go", method="POST", path="https://evil.example/x")

    def test_the_resume_route_passes(self):
        from app.models import NoticeAction

        action = NoticeAction(
            label="Resume now", method="POST", path="/queue/lanes/youtube/resume"
        )
        assert action.path == "/queue/lanes/youtube/resume"


# ===========================================================================
# Review round 2: a hold that lapses, and who is actually waiting
# ===========================================================================


class TestALapsedHoldIsAnnounced:
    """A hold only lapses when somebody looks at it, and the watchdog looks."""

    async def test_an_idle_lapse_announces_once_and_clears_the_lane(self, clock):
        seen: list[LaneRecord] = []
        manager = LaneManager(clock=clock)
        manager.set_callbacks(on_change=lambda host, record: seen.append(record))
        with patch("app.rate_limit.WATCHDOG_INTERVAL_SECONDS", 0.01):
            manager.note_rate_limit("youtube")
            clock.advance(BACKOFF_SECONDS[0] * 2)
            await asyncio.sleep(0.1)  # several ticks
        # The hold, then the lapse -- and nothing after it.
        assert len(seen) == 2
        assert seen[-1].hold_until is None
        assert manager.is_held("youtube") is False
        manager.close()

    async def test_an_idle_lapse_keeps_the_streak_but_drops_held_since(self, clock):
        """The ladder is not reset by a hold nobody tested."""
        manager = LaneManager(clock=clock)
        with patch("app.rate_limit.WATCHDOG_INTERVAL_SECONDS", 0.01):
            manager.note_rate_limit("youtube")
            manager.note_rate_limit("youtube")
            clock.advance(BACKOFF_SECONDS[1] * 2)
            await asyncio.sleep(0.1)
        record = manager.state("youtube")
        assert record.consecutive == 2  # no request went out, nothing was learned
        assert record.held_since is None  # nobody is waiting, so nothing is ageing
        manager.close()

    async def test_an_hour_of_idle_gaps_does_not_arm_the_ceiling(self, clock):
        """Two probes an hour apart are not "an hour of being rate limited"."""
        fired: list[str] = []
        manager = LaneManager(clock=clock)
        manager.set_callbacks(on_ceiling=lambda host, reason: fired.append(host))
        with patch("app.rate_limit.WATCHDOG_INTERVAL_SECONDS", 0.01):
            manager.note_rate_limit("youtube")
            clock.advance(CEILING_SECONDS - 1)
            await asyncio.sleep(0.08)
            manager.note_rate_limit("youtube")
            await asyncio.sleep(0.08)
        assert fired == []
        manager.close()

    async def test_a_lapse_with_a_waiter_announces_the_canary_election(self, clock):
        seen: list[LaneRecord] = []
        manager = LaneManager(clock=clock)
        manager.set_callbacks(on_change=lambda host, record: seen.append(record))
        with patch("app.rate_limit.WATCHDOG_INTERVAL_SECONDS", 0.01):
            manager.note_rate_limit("youtube")
            manager.lane("youtube").park("waiter")
            clock.advance(BACKOFF_SECONDS[0] * 2)
            await asyncio.sleep(0.1)
        assert len(seen) == 2
        lane = manager.lane("youtube")
        assert lane.canary == "waiter"
        assert lane.held_since is not None  # somebody is waiting, so it ages
        manager.close()

    async def test_a_settled_lane_is_not_re_announced_every_tick(self, clock):
        seen: list[LaneRecord] = []
        manager = LaneManager(clock=clock)
        manager.set_callbacks(on_change=lambda host, record: seen.append(record))
        with patch("app.rate_limit.WATCHDOG_INTERVAL_SECONDS", 0.01):
            manager.note_rate_limit("youtube")
            manager.lane("youtube").park("waiter")
            clock.advance(BACKOFF_SECONDS[0] * 2)
            await asyncio.sleep(0.05)
            before = len(seen)
            await asyncio.sleep(0.08)  # more ticks, nothing new to say
        assert len(seen) == before
        manager.close()

    def test_the_restored_escalation_is_primed(self, tmp_path, clock):
        """A hold that comes back already escalated must not be re-raised."""
        store = JobStore(tmp_path / "queue.db")
        try:
            store.save_lane(
                LaneRecord(
                    host="youtube",
                    hold_until=EPOCH + timedelta(seconds=600),
                    consecutive=4,
                    reason=REASON_RATE_LIMIT,
                    held_since=EPOCH - timedelta(seconds=NOTICE_ESCALATE_SECONDS + 60),
                )
            )
            manager = LaneManager(clock=clock)
            manager.attach_store(store)
            assert manager.lane("youtube").escalated is True
            manager.close()
        finally:
            store.close()

    def test_a_fresh_restored_hold_is_not_primed(self, tmp_path, clock):
        store = JobStore(tmp_path / "queue.db")
        try:
            store.save_lane(
                LaneRecord(
                    host="youtube",
                    hold_until=EPOCH + timedelta(seconds=60),
                    consecutive=1,
                    reason=REASON_RATE_LIMIT,
                    held_since=EPOCH - timedelta(seconds=30),
                )
            )
            manager = LaneManager(clock=clock)
            manager.attach_store(store)
            assert manager.lane("youtube").escalated is False
            manager.close()
        finally:
            store.close()


class TestOnlyParkedJobsAreOnTheLane:
    def test_a_job_that_never_blocked_is_not_parked(self, manager, clock):
        """The fast path must leave nothing behind to be elected canary."""
        lane = manager.lane("youtube")
        assert lane.may_run("downloading-job", clock.now) is True
        assert lane.parked == []
        assert manager.waiting("youtube") == []

    def test_the_oldest_parked_job_stays_the_canary_across_attempts(
        self, manager, clock
    ):
        """A canary that is rate limited again must not go to the back."""
        manager.note_rate_limit("youtube")
        lane = manager.lane("youtube")
        lane.park("first")
        lane.park("second")
        clock.advance(BACKOFF_SECONDS[0] * 2)
        assert lane.may_run("first", clock.now) is True
        # It takes its attempt, so it leaves the parked list...
        lane.unpark("first")
        manager.note_rate_limit("youtube")
        clock.advance(BACKOFF_SECONDS[1] * 2)
        # ...and comes back behind "second", but is still the older of the two.
        lane.park("first")
        assert lane.may_run("second", clock.now) is False
        assert lane.canary == "first"

    @patch("app.queue_manager.download_audio")
    async def test_a_downloading_job_is_never_elected_canary(
        self, mock_download, tmp_path
    ):
        """The repro: A downloading, B rate limited, the hold lapses.

        With A on the lane's waiting list, A would be elected canary and B
        would sit behind a job that was not waiting for anything.
        """
        started = asyncio.Event()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()
        limited: list[str] = []

        def handler(job, *args, **kwargs):
            if job.id == "job-a":
                loop.call_soon_threadsafe(started.set)
                asyncio.run_coroutine_threadsafe(release.wait(), loop).result(10)
                target = tmp_path / "A" / "B" / "a.flac"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
                return target
            limited.append(job.id)
            if len(limited) == 1:
                raise youtube_error()
            target = tmp_path / "A" / "B" / "b.flac"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            return target

        mock_download.side_effect = handler
        with patch("app.rate_limit.BACKOFF_SECONDS", (0.2, 0.2, 0.2, 0.2, 0.2)):
            qm = QueueManager(max_concurrent=2, timeout=30)
            qm.add_job(_make_job(id="job-a"))
            await asyncio.wait_for(started.wait(), 10)
            qm.add_job(_make_job(id="job-b"))
            # B is rate limited, waits, and gets through on its own -- without
            # waiting for A, which is still parked in `handler`.
            await _wait_until(
                lambda: qm.get_job("job-b").status is JobStatus.DONE, 10
            )
            assert qm.get_job("job-a").status is JobStatus.DOWNLOADING
            loop.call_soon_threadsafe(release.set)
            await _wait_until(
                lambda: qm.get_job("job-a").status is JobStatus.DONE, 10
            )

    @patch("app.queue_manager.download_audio")
    async def test_the_ceiling_leaves_a_downloading_job_alone(
        self, mock_download, tmp_path
    ):
        started = asyncio.Event()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def handler(job, *args, **kwargs):
            if job.id == "job-a":
                loop.call_soon_threadsafe(started.set)
                asyncio.run_coroutine_threadsafe(release.wait(), loop).result(10)
                target = tmp_path / "A" / "B" / "a.flac"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
                return target
            raise youtube_error()

        mock_download.side_effect = handler
        with patch("app.rate_limit.BACKOFF_SECONDS", (300, 300, 300, 300, 300)):
            qm = QueueManager(max_concurrent=3, timeout=30)
            qm.add_job(_make_job(id="job-a"))
            await asyncio.wait_for(started.wait(), 10)
            qm.add_job(_make_job(id="job-b"))
            await _wait_until(lambda: qm.get_job("job-b").retry_at is not None, 10)

            qm._lanes.lane("youtube").held_since = datetime.now(
                timezone.utc
            ) - timedelta(seconds=CEILING_SECONDS + 1)
            qm._lanes.fire_ceiling("youtube")
            await _wait_until(
                lambda: qm.get_job("job-b").status is JobStatus.ERROR, 10
            )

            # The healthy download was never on the ceiling's list.
            assert qm.get_job("job-a").status is JobStatus.DOWNLOADING
            loop.call_soon_threadsafe(release.set)
            await _wait_until(
                lambda: qm.get_job("job-a").status is JobStatus.DONE, 10
            )


class TestCancelDuringSlotReacquisition:
    @patch("app.queue_manager.download_audio")
    async def test_a_cancel_is_not_stuck_behind_a_busy_semaphore(
        self, mock_download, tmp_path
    ):
        """The one slot is held by another job when the hold lapses."""
        holder_started = asyncio.Event()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def handler(job, *args, **kwargs):
            if job.id == "sc-hold":
                loop.call_soon_threadsafe(holder_started.set)
                asyncio.run_coroutine_threadsafe(release.wait(), loop).result(15)
                target = tmp_path / "A" / "B" / "s.flac"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
                return target
            raise youtube_error()

        mock_download.side_effect = handler
        with patch("app.rate_limit.BACKOFF_SECONDS", (0.2, 0.2, 0.2, 0.2, 0.2)):
            qm = QueueManager(max_concurrent=1, timeout=30)
            qm.add_job(_make_job(id="yt-1"))
            await _wait_until(lambda: qm.get_job("yt-1").retry_at is not None, 10)
            # The slot yt-1 gave back is taken by a job that will not let go.
            qm.add_job(_make_job(id="sc-hold", url="https://soundcloud.com/a/b"))
            await asyncio.wait_for(holder_started.wait(), 10)

            # yt-1's hold lapses and it is queueing for a slot it cannot have.
            await asyncio.sleep(0.4)
            qm.cancel_job("yt-1")
            await _wait_until(
                lambda: qm.get_job("yt-1").status is JobStatus.CANCELLED, 5
            )

            loop.call_soon_threadsafe(release.set)
            await _wait_until(
                lambda: qm.get_job("sc-hold").status is JobStatus.DONE, 10
            )
        # The permit the cancelled acquire was waiting for was never lost.
        assert qm._semaphore._value == 1


# ===========================================================================
# Review round 3: one announcement per transition, and the sign-in wall's hold
# ===========================================================================


class TestOneAnnouncementPerTransition:
    """`dirty` is "the watchdog still has to say this", and saying it clears it.

    A settle can be triggered by any waiter's poll, so the flag exists to let
    the watchdog find out.  But a transition that some *other* path has already
    announced -- a 429 arriving right after the hold lapsed -- must not be
    announced a second time, or the banner would come back a tick after the
    user dismissed it.
    """

    async def test_a_transition_announced_by_a_429_is_not_repeated(self, clock):
        seen: list[LaneRecord] = []
        manager = LaneManager(clock=clock)
        manager.set_callbacks(on_change=lambda host, record: seen.append(record))
        with patch("app.rate_limit.WATCHDOG_INTERVAL_SECONDS", 0.01):
            manager.note_rate_limit("youtube")  # announcement 1
            lane = manager.lane("youtube")
            lane.park("waiter")
            clock.advance(BACKOFF_SECONDS[0] * 2)
            # A waiter's own poll settles the lane: the hold lapses and it is
            # elected canary, which marks the lane dirty.
            assert lane.may_run("waiter", clock.now) is True
            assert lane.dirty is True
            # The canary is rate limited again before the watchdog gets there.
            manager.note_rate_limit("youtube")  # announcement 2
            assert lane.dirty is False
            await asyncio.sleep(0.08)  # several ticks, nothing new to say
        assert len(seen) == 2
        manager.close()

    async def test_a_fresh_episode_announces_once_per_transition(self, clock):
        seen: list[LaneRecord] = []
        manager = LaneManager(clock=clock)
        manager.set_callbacks(on_change=lambda host, record: seen.append(record))
        with patch("app.rate_limit.WATCHDOG_INTERVAL_SECONDS", 0.01):
            manager.note_rate_limit("youtube")  # 1: held
            manager.note_success("youtube")  # 2: open again
            await asyncio.sleep(0.05)
            manager.note_rate_limit("youtube")  # 3: held again
            await asyncio.sleep(0.05)
        assert len(seen) == 3
        assert seen[0].hold_until is not None
        assert seen[1].hold_until is None
        assert seen[2].hold_until is not None
        manager.close()

    def test_a_manager_with_no_banner_still_clears_the_flag(self, clock):
        """Otherwise a unit test's manager would drift from the app's."""
        manager = LaneManager(clock=clock)
        manager.note_rate_limit("youtube")
        assert manager.lane("youtube").dirty is False


class TestTheSignInWallsHold:
    def test_the_hold_runs_a_full_ceiling_from_now(self, manager, clock):
        """Not from `held_since`: after an hour of 429s that would already
        be in the past, and the next job would walk straight into the wall."""
        manager.note_rate_limit("youtube")
        clock.advance(NOTICE_ESCALATE_SECONDS)  # half an hour of 429s
        manager.note_bot_check("youtube")

        record = manager.state("youtube")
        assert record.hold_until == clock.now + timedelta(seconds=CEILING_SECONDS)
        # The ceiling still measures from the original trouble.
        assert record.held_since == EPOCH
        assert manager.is_held("youtube") is True

    def test_the_banner_is_raised_even_when_the_ceiling_is_imminent(self, clock):
        seen: list[LaneRecord] = []
        manager = LaneManager(clock=clock)
        manager.set_callbacks(on_change=lambda host, record: seen.append(record))
        manager.note_rate_limit("youtube")
        clock.advance(CEILING_SECONDS - 6)  # 59.9 minutes in
        manager.note_bot_check("youtube")

        assert seen[-1].reason == REASON_BOT_CHECK
        assert seen[-1].hold_until is not None  # so the banner renders
        manager.close()

    async def test_the_ceiling_still_fires_from_the_original_trouble(self, clock):
        fired: list[tuple[str, str | None]] = []
        seen: list[LaneRecord] = []
        manager = LaneManager(clock=clock)
        manager.set_callbacks(
            on_change=lambda host, record: seen.append(record),
            on_ceiling=lambda host, reason: fired.append((host, reason)),
        )
        with patch("app.rate_limit.WATCHDOG_INTERVAL_SECONDS", 0.01):
            manager.note_rate_limit("youtube")
            clock.advance(CEILING_SECONDS - 6)
            manager.note_bot_check("youtube")
            clock.advance(7)
            await asyncio.sleep(0.08)

        # The hold has another hour to run, but the trouble is an hour old.
        assert fired == [("youtube", REASON_BOT_CHECK)]
        # And the banner is retracted rather than left standing.
        assert seen[-1].hold_until is None
        assert manager.is_held("youtube") is False
        manager.close()
