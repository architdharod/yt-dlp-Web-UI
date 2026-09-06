"""Tests for the downloader module.

All tests mock yt-dlp -- no real network calls or downloads -- and every ffmpeg
run goes through the ``fake_ffmpeg`` fixture in ``conftest``, because ffmpeg is
a separate binary that is not installed where the suite runs.  mutagen, by
contrast, is exercised for real against the minimal FLAC that fake ffmpeg
writes: tag writing is the one stage with no external process in it.
"""

import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yt_dlp.utils
from mutagen.flac import FLAC

from app.downloader import (
    ALREADY_IN_LIBRARY_PREFIX,
    BANDCAMP_NO_STREAM_MESSAGE,
    CANCELLED_MESSAGE,
    CancelToken,
    DownloadError,
    FiledTrack,
    _YtDlpLogger,
    TrackMetadata,
    _make_progress_hook,
    _missing_output_dirs,
    _run_ffmpeg,
    _terminate_process,
    download_audio,
    unfile_track,
    extract_metadata,
    job_temp_dir,
    remove_job_temp_dir,
    remove_orphan_temp_dirs,
    track_filename_for,
)
from app.file_organizer import UnsafePathError, get_output_path
from app.models import Job
from tests.conftest import TINY_JPEG, TINY_WEBP


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

SAMPLE_INFO = {
    "title": "My Cool Track",
    "thumbnail": "https://img.youtube.com/vi/abc123/maxresdefault.jpg",
    "duration": 245.0,
    "artist": "Test Artist",
    "uploader": "Test Uploader",
    "album": "Test Album",
}


def _make_job(**overrides) -> Job:
    """Create a Job with sensible defaults, overriding any field."""
    defaults = {
        "id": "job-1",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    }
    defaults.update(overrides)
    return Job(**defaults)


# ===========================================================================
# extract_metadata
# ===========================================================================


class TestExtractMetadata:
    """Tests for extract_metadata()."""

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_extracts_title_thumbnail_duration(self, mock_ydl_cls):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = SAMPLE_INFO
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = extract_metadata("https://www.youtube.com/watch?v=abc123")

        assert isinstance(result, TrackMetadata)
        assert result.title == "My Cool Track"
        assert result.thumbnail_url == "https://img.youtube.com/vi/abc123/maxresdefault.jpg"
        assert result.duration == 245.0

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_missing_thumbnail_returns_none(self, mock_ydl_cls):
        info = {"title": "No Thumb Track", "duration": 120.0}
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = info
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = extract_metadata("https://example.com/track")

        assert result.thumbnail_url is None

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_missing_duration_returns_none(self, mock_ydl_cls):
        info = {"title": "No Duration Track", "thumbnail": "https://example.com/thumb.jpg"}
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = info
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = extract_metadata("https://example.com/track")

        assert result.duration is None

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_missing_title_falls_back_to_unknown(self, mock_ydl_cls):
        info = {"duration": 60.0}
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = info
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = extract_metadata("https://example.com/track")

        assert result.title == "Unknown Title"

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_raises_download_error_on_ytdlp_failure(self, mock_ydl_cls):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError("Video unavailable")
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(DownloadError, match="Failed to extract metadata"):
            extract_metadata("https://example.com/unavailable")


# ---------------------------------------------------------------------------
# Streaming turned off on Bandcamp
# ---------------------------------------------------------------------------


# What yt-dlp really says for a Bandcamp track whose seller sells it without
# streaming it, captured from a live run against
# https://amelielens.bandcamp.com/track/theory-of-relativity.
_BANDCAMP_NO_FORMATS = (
    "ERROR: [Bandcamp] 3456873933: No video formats found!; please report this "
    "issue on  https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the "
    "appropriate issue template. Confirm you are on the latest version using  "
    "yt-dlp -U"
)


class TestBandcampStreamingDisabled:
    """A track with no stream fails with a sentence, not with a bug report.

    The failure lands in ``extract_metadata`` -- yt-dlp picks formats before it
    downloads anything, so the metadata pass is where it notices -- but the
    download path maps it the same way, because which of the two sees it first
    is yt-dlp's business and not something a user's error message should
    depend on.
    """

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_metadata_extraction_says_streaming_is_off(self, mock_ydl_cls, caplog):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(
            _BANDCAMP_NO_FORMATS
        )
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        with caplog.at_level(logging.ERROR, logger="app.downloader"):
            with pytest.raises(DownloadError) as caught:
                extract_metadata(
                    "https://amelielens.bandcamp.com/track/theory-of-relativity"
                )

        assert str(caught.value) == BANDCAMP_NO_STREAM_MESSAGE
        # The original is not lost: it is what anybody diagnosing this needs.
        assert "No video formats found" in caplog.text

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_the_download_path_maps_it_too(self, mock_ydl_cls):
        # The pre-download metadata pass is advisory inside ``download_audio``
        # -- it is logged and carried on from -- so the failure that reaches
        # the user comes out of the downloading extraction, and that arm has to
        # map the message as well.
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(
            _BANDCAMP_NO_FORMATS
        )
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        job = _make_job(
            url="https://amelielens.bandcamp.com/track/theory-of-relativity",
            artist="Amelie Lens",
            title="Theory of Relativity",
        )
        with pytest.raises(DownloadError) as caught:
            download_audio(job)

        assert str(caught.value) == BANDCAMP_NO_STREAM_MESSAGE

    @pytest.mark.parametrize(
        "message",
        [
            # The same yt-dlp complaint from a different extractor is a
            # different failure and keeps yt-dlp's own words.
            "ERROR: [youtube] abc123: No video formats found!",
            # A Bandcamp failure that is not this one keeps them too.
            "ERROR: [Bandcamp] 123: Unable to download webpage",
        ],
        ids=["other-extractor", "other-bandcamp-error"],
    )
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_nothing_else_is_reworded(self, mock_ydl_cls, message):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(message)
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(DownloadError) as caught:
            extract_metadata("https://example.com/track")

        assert str(caught.value) == f"Failed to extract metadata: {message}"

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_an_album_extractor_counts_as_bandcamp(self, mock_ydl_cls):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(
            "ERROR: [Bandcamp:album] 9: No video formats found!"
        )
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(DownloadError) as caught:
            extract_metadata("https://amelielens.bandcamp.com/album/exhale")

        assert str(caught.value) == BANDCAMP_NO_STREAM_MESSAGE

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_raises_download_error_on_none_info(self, mock_ydl_cls):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = None
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(DownloadError, match="yt-dlp returned no metadata"):
            extract_metadata("https://example.com/track")

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_passes_correct_options_to_ytdlp(self, mock_ydl_cls):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = SAMPLE_INFO
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        extract_metadata("https://example.com/track")

        opts = mock_ydl_cls.call_args[0][0]
        assert opts["quiet"] is True
        assert opts["no_warnings"] is True
        assert opts["skip_download"] is True

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_calls_extract_info_with_download_false(self, mock_ydl_cls):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = SAMPLE_INFO
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        extract_metadata("https://example.com/track")

        mock_ydl.extract_info.assert_called_once_with("https://example.com/track", download=False)


    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_playlist_info_raises_download_error(self, mock_ydl_cls):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = {"_type": "playlist", "title": "My Mix"}
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(DownloadError, match="playlist"):
            extract_metadata("https://www.youtube.com/watch?v=abc&list=PL123")

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_options_guard_against_playlists_and_hangs(self, mock_ydl_cls):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = SAMPLE_INFO
        mock_ydl_cls.return_value.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl_cls.return_value.__exit__ = MagicMock(return_value=False)

        extract_metadata("https://www.youtube.com/watch?v=abc")

        opts = mock_ydl_cls.call_args[0][0]
        assert opts["noplaylist"] is True
        assert opts["allowed_extractors"] == ["default", "-generic"]
        assert opts["socket_timeout"] > 0
        # A submit-time probe must not retry for minutes.
        assert opts["retries"] == 1
        assert opts["extractor_retries"] == 1


# ===========================================================================
# _make_progress_hook
# ===========================================================================


class TestMakeProgressHook:
    """Tests for the progress hook translation logic."""

    def test_downloading_with_total_bytes(self):
        callback = MagicMock()
        hook = _make_progress_hook(callback)

        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})

        callback.assert_called_once_with(50.0)

    def test_downloading_with_total_bytes_estimate(self):
        callback = MagicMock()
        hook = _make_progress_hook(callback)

        hook({"status": "downloading", "downloaded_bytes": 25, "total_bytes_estimate": 100})

        callback.assert_called_once_with(25.0)

    def test_total_bytes_preferred_over_estimate(self):
        callback = MagicMock()
        hook = _make_progress_hook(callback)

        hook({
            "status": "downloading",
            "downloaded_bytes": 50,
            "total_bytes": 200,
            "total_bytes_estimate": 100,
        })

        callback.assert_called_once_with(25.0)

    def test_downloading_without_total_does_not_call_back(self):
        callback = MagicMock()
        hook = _make_progress_hook(callback)

        hook({"status": "downloading", "downloaded_bytes": 50})

        callback.assert_not_called()

    def test_finished_status_reports_100(self):
        callback = MagicMock()
        hook = _make_progress_hook(callback)

        hook({"status": "finished"})

        callback.assert_called_once_with(100.0)

    def test_none_callback_does_not_error(self):
        hook = _make_progress_hook(None)

        # Should not raise
        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
        hook({"status": "finished"})

    def test_percentage_capped_at_100(self):
        callback = MagicMock()
        hook = _make_progress_hook(callback)

        # downloaded_bytes exceeds total_bytes (can happen with chunked transfers)
        hook({"status": "downloading", "downloaded_bytes": 150, "total_bytes": 100})

        callback.assert_called_once_with(100.0)

    def test_zero_total_does_not_divide_by_zero(self):
        callback = MagicMock()
        hook = _make_progress_hook(callback)

        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 0})

        callback.assert_not_called()

    def test_unknown_status_does_not_call_back(self):
        callback = MagicMock()
        hook = _make_progress_hook(callback)

        hook({"status": "processing"})

        callback.assert_not_called()


# ===========================================================================
# download_audio
# ===========================================================================


def _install_ydl_mocks(
    mock_ydl_cls,
    info=SAMPLE_INFO,
    extract_error=None,
    download_error=None,
    create_file=True,
    audio_suffix=".webm",
    thumbnail_bytes=None,
    thumbnail_suffix=".webp",
):
    """Wire a mocked ``YoutubeDL`` class for the two contexts download_audio opens.

    The first context answers the metadata-only ``extract_info``; the second
    answers ``extract_info(url, download=True)``.  Unless *create_file* is False
    the second one also creates what yt-dlp would have left in the scratch
    directory: the *raw* best-audio stream, in whatever container the site
    served (hence *audio_suffix* -- yt-dlp runs no postprocessors any more, so
    this is never a FLAC unless the site itself served one), reported back in
    ``requested_downloads[0]["filepath"]``, plus the ``writethumbnail`` sidecar
    when *thumbnail_bytes* is given.

    Returns ``(extract_ydl, download_ydl)``.
    """
    extract_ydl = MagicMock()
    if extract_error is not None:
        extract_ydl.extract_info.side_effect = extract_error
    else:
        extract_ydl.extract_info.return_value = info

    download_ydl = MagicMock()

    def fake_download(url, download=False):
        if download_error is not None:
            raise download_error
        result = dict(info or {})
        if not create_file:
            return result
        opts = mock_ydl_cls.call_args_list[-1][0][0]
        outtmpl = opts["outtmpl"]
        assert outtmpl.endswith(".%(ext)s")
        stem = outtmpl[: -len(".%(ext)s")]
        home = Path(opts["paths"]["home"])
        home.mkdir(parents=True, exist_ok=True)
        target = home / (stem + audio_suffix)
        target.write_bytes(b"raw audio")
        if thumbnail_bytes is not None:
            (home / (stem + thumbnail_suffix)).write_bytes(thumbnail_bytes)
        result["requested_downloads"] = [{"filepath": str(target)}]
        return result

    download_ydl.extract_info.side_effect = fake_download

    mock_ydl_cls.side_effect = [
        MagicMock(
            __enter__=MagicMock(return_value=extract_ydl),
            __exit__=MagicMock(return_value=False),
        ),
        MagicMock(
            __enter__=MagicMock(return_value=download_ydl),
            __exit__=MagicMock(return_value=False),
        ),
    ]
    return extract_ydl, download_ydl


class TestDownloadAudio:
    """Tests for download_audio()."""

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_returns_correct_output_path(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="My Artist", album="My Album")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        expected = tmp_path / "My Artist" / "My Album" / "My Cool Track.flac"
        assert result == expected

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_creates_output_directory(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="New Artist", album="New Album")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        assert (tmp_path / "New Artist" / "New Album").is_dir()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_uses_user_artist_and_album(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="User Artist", album="User Album")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        # User-provided values take priority
        assert "User Artist" in result.parts
        assert "User Album" in result.parts

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_falls_back_to_ytdlp_metadata(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(mock_ydl_cls)

        # No user-provided artist/album
        job = _make_job()

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        # Falls back to yt-dlp metadata (artist field from SAMPLE_INFO)
        assert "Test Artist" in result.parts
        assert "Test Album" in result.parts

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_falls_back_to_uploader_when_no_artist(self, mock_ydl_cls, mock_sanitize, tmp_path):
        info = {**SAMPLE_INFO, "artist": None}
        _install_ydl_mocks(mock_ydl_cls, info=info)

        job = _make_job()

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        # Falls back to uploader when artist is None
        assert "Test Uploader" in result.parts

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_artist_preference_prefers_channel_and_strips_topic(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """channel beats uploader, and the YouTube ' - Topic' suffix goes away."""
        info = {
            **SAMPLE_INFO,
            "artist": None,
            "creator": None,
            "channel": "Foo - Topic",
            "uploader": "x",
        }
        _install_ydl_mocks(mock_ydl_cls, info=info)

        job = _make_job()

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        assert "Foo" in result.parts
        assert "x" not in result.parts

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_ytdlp_is_given_no_postprocessors_at_all(self, mock_ydl_cls, mock_sanitize, tmp_path):
        """Conversion and tagging are ours; yt-dlp only fetches bytes."""
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        # The second YoutubeDL call is for downloading
        download_opts = mock_ydl_cls.call_args_list[1][0][0]
        assert "postprocessors" not in download_opts
        assert "postprocessor_hooks" not in download_opts

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_thumbnail_sidecar_is_still_requested(self, mock_ydl_cls, mock_sanitize, tmp_path):
        """The cover art we embed ourselves is the sidecar yt-dlp downloads."""
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        download_opts = mock_ydl_cls.call_args_list[1][0][0]
        assert download_opts["writethumbnail"] is True

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_ytdlp_download_options_format_is_bestaudio(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        download_opts = mock_ydl_cls.call_args_list[1][0][0]
        assert download_opts["format"] == "bestaudio/best"

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_both_option_dicts_guard_against_playlists_and_hangs(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        for call_args in mock_ydl_cls.call_args_list:
            opts = call_args[0][0]
            assert opts["noplaylist"] is True
            assert opts["allowed_extractors"] == ["default", "-generic"]
            assert opts["socket_timeout"] > 0

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_progress_hook_is_wired(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(mock_ydl_cls)

        progress_cb = MagicMock()
        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job, on_progress=progress_cb)

        download_opts = mock_ydl_cls.call_args_list[1][0][0]
        assert len(download_opts["progress_hooks"]) == 1
        assert callable(download_opts["progress_hooks"][0])

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_converting_is_reported_before_ffmpeg_starts(
        self, mock_ydl_cls, mock_sanitize, tmp_path, fake_ffmpeg
    ):
        """The phase change is ours now, so it cannot lag behind the encoder."""
        _install_ydl_mocks(mock_ydl_cls)

        phases: list[str] = []

        def record(phase: str) -> None:
            phases.append(phase)
            if phase == "converting":
                assert fake_ffmpeg.commands == [], "ffmpeg started before the UI knew"

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job, on_phase=record)

        assert phases == ["metadata", "converting"]

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_extract_failure_falls_back_and_still_downloads(self, mock_ydl_cls, mock_sanitize, tmp_path):
        """When the pre-download extract_info fails, download_audio should
        fall back to the job's title and still attempt the download."""
        _, download_ydl = _install_ydl_mocks(
            mock_ydl_cls, extract_error=yt_dlp.utils.DownloadError("Network error")
        )

        job = _make_job(title="Fallback Title", artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        # Download was still attempted
        download_ydl.extract_info.assert_called_once_with(job.url, download=True)
        assert result.suffix == ".flac"

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_none_info_falls_back_and_still_downloads(self, mock_ydl_cls, mock_sanitize, tmp_path):
        """When extract_info returns None, download_audio should fall back
        to the job's title and still attempt the download."""
        _, download_ydl = _install_ydl_mocks(mock_ydl_cls, info=None)

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        # Download was still attempted
        download_ydl.extract_info.assert_called_once_with(job.url, download=True)
        assert result.suffix == ".flac"

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="Unknown Title")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_extract_failure_with_no_job_title_uses_unknown(self, mock_ydl_cls, mock_sanitize, tmp_path):
        """When extract_info fails and the job has no title, falls back to 'Unknown Title'."""
        _, download_ydl = _install_ydl_mocks(
            mock_ydl_cls, extract_error=yt_dlp.utils.DownloadError("Network error")
        )

        job = _make_job(title=None, artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        # sanitize_filename was called with "Unknown Title" (the fallback)
        mock_sanitize.assert_called_with("Unknown Title")
        download_ydl.extract_info.assert_called_once_with(job.url, download=True)

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="Unknown Title")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_null_title_in_info_falls_back_to_unknown(self, mock_ydl_cls, mock_sanitize, tmp_path):
        """A present-but-null title must not reach sanitize_filename as None."""
        _install_ydl_mocks(mock_ydl_cls, info={**SAMPLE_INFO, "title": None})

        job = _make_job(title=None, artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        mock_sanitize.assert_called_with("Unknown Title")
        assert result.name == "Unknown Title.flac"

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_raises_download_error_on_download_failure(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(
            mock_ydl_cls, download_error=yt_dlp.utils.DownloadError("Download failed")
        )

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError, match="Download failed"):
                download_audio(job)

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_os_error_becomes_download_error(self, mock_ydl_cls, mock_sanitize, tmp_path):
        """A filesystem error must not escape as a bare OSError traceback."""
        _install_ydl_mocks(
            mock_ydl_cls, download_error=OSError("File name too long")
        )

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError, match="File name too long"):
                download_audio(job)

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_missing_output_file_raises_download_error(self, mock_ydl_cls, mock_sanitize, tmp_path):
        """A "successful" download that wrote nothing is a failure, not a done job."""
        _install_ydl_mocks(mock_ydl_cls, create_file=False)

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError, match="Output file not found"):
                download_audio(job)

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_unsafe_path_becomes_download_error(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with patch(
                "app.downloader.get_output_path",
                side_effect=UnsafePathError("escapes download root"),
            ):
                with pytest.raises(DownloadError, match="unsafe output path"):
                    download_audio(job)

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_calls_download_with_job_url(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _, download_ydl = _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        download_ydl.extract_info.assert_called_once_with(job.url, download=True)

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_percent_in_title_needs_no_escaping(self, mock_ydl_cls, tmp_path):
        """The title never reaches yt-dlp's template, so '%' is just a character.

        sanitize_filename is deliberately NOT mocked here: the real one keeps
        '%' untouched, which used to make yt-dlp read "100% Love" as a template.
        """
        _install_ydl_mocks(mock_ydl_cls, info={**SAMPLE_INFO, "title": "100% Love"})

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        outtmpl = mock_ydl_cls.call_args_list[1][0][0]["outtmpl"]
        assert "%%" not in outtmpl
        assert result.name == "100% Love.flac"
        assert result.exists()

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_shell_variable_in_title_stays_inside_the_library(
        self, mock_ydl_cls, tmp_path
    ):
        """yt-dlp expands $VAR in both outtmpl and paths, and an absolute
        filename beats paths["home"] -- so neither may carry a title."""
        _install_ydl_mocks(
            mock_ydl_cls, info={**SAMPLE_INFO, "title": "$HOME sweet home"}
        )

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        assert result == tmp_path / "A" / "B" / "$HOME sweet home.flac"
        assert result.exists()
        assert "$" not in mock_ydl_cls.call_args_list[1][0][0]["outtmpl"]

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_shell_variable_in_artist_stays_inside_the_library(
        self, mock_ydl_cls, tmp_path
    ):
        """paths["home"] is expanded too, so it must not carry artist/album."""
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="$HOME", album="$USER")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        assert result == tmp_path / "$HOME" / "$USER" / "My Cool Track.flac"
        assert result.exists()
        for value in mock_ydl_cls.call_args_list[1][0][0]["paths"].values():
            assert "$" not in value

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_an_existing_target_flac_is_never_overwritten(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """A track already in the library is skipped, with a visible reason."""
        _install_ydl_mocks(mock_ydl_cls)
        existing = tmp_path / "A" / "B" / "My Cool Track.flac"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"OLD")

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError) as excinfo:
                download_audio(job)

        assert str(excinfo.value) == (
            ALREADY_IN_LIBRARY_PREFIX + "A/B/My Cool Track.flac"
        )
        assert existing.read_bytes() == b"OLD"

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_an_existing_target_is_detected_before_anything_is_fetched(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """The cheap check runs first, so a duplicate costs no bandwidth."""
        _, download_ydl = _install_ydl_mocks(mock_ydl_cls)
        existing = tmp_path / "A" / "B" / "My Cool Track.flac"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"OLD")

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError, match=ALREADY_IN_LIBRARY_PREFIX):
                download_audio(job)

        download_ydl.extract_info.assert_not_called()
        assert not (tmp_path / ".tmp").exists()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_target_that_appears_during_the_download_is_not_overwritten(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """The second check is what protects the file when two jobs collide."""
        _, download_ydl = _install_ydl_mocks(mock_ydl_cls)
        existing = tmp_path / "A" / "B" / "My Cool Track.flac"
        inner = download_ydl.extract_info.side_effect

        def download_then_race(url, download=False):
            result = inner(url, download=download)
            # Another job filed the same track while this one was downloading.
            existing.parent.mkdir(parents=True, exist_ok=True)
            existing.write_bytes(b"THEIRS")
            return result

        download_ydl.extract_info.side_effect = download_then_race
        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError, match=ALREADY_IN_LIBRARY_PREFIX):
                download_audio(job)

        assert existing.read_bytes() == b"THEIRS"

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_failed_download_leaves_no_empty_album_folder(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(
            mock_ydl_cls, download_error=yt_dlp.utils.DownloadError("boom")
        )

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError):
                download_audio(job)

        assert not (tmp_path / "A").exists()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_failed_download_leaves_a_populated_album_alone(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(
            mock_ydl_cls, download_error=yt_dlp.utils.DownloadError("boom")
        )
        album = tmp_path / "A" / "B"
        album.mkdir(parents=True)
        (album / "Another Track.flac").write_bytes(b"mine")

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError):
                download_audio(job)

        assert (album / "Another Track.flac").exists()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_failed_conversion_files_nothing_and_says_why(
        self, mock_ydl_cls, mock_sanitize, tmp_path, fake_ffmpeg
    ):
        """A raw stream must never reach the library under a `.flac` name.

        With no ``FFmpegExtractAudio`` in the pipeline that can only happen if a
        failed encode were ignored, so the exit code decides and ffmpeg's own
        stderr becomes the job's reason.
        """
        _install_ydl_mocks(mock_ydl_cls)
        fake_ffmpeg.returncode = 1
        fake_ffmpeg.stderr = b"Invalid data found when processing input"

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError, match="Invalid data found"):
                download_audio(job)

        assert not (tmp_path / "A").exists()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_missing_ffmpeg_is_reported_as_such(
        self, mock_ydl_cls, mock_sanitize, tmp_path, fake_ffmpeg
    ):
        """"ffmpeg is not installed" beats a FileNotFoundError traceback."""
        _install_ydl_mocks(mock_ydl_cls)
        fake_ffmpeg.error = FileNotFoundError(2, "No such file or directory")

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError, match="not installed"):
                download_audio(job)

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_failed_download_leaves_a_pre_existing_empty_folder_alone(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """An empty `Artist/Album` the user made by hand is theirs, not debris.

        The same bookkeeping protects a concurrent job that has just created
        the album folder and has not moved its file into it yet.
        """
        _install_ydl_mocks(
            mock_ydl_cls, download_error=yt_dlp.utils.DownloadError("boom")
        )
        album = tmp_path / "A" / "B"
        album.mkdir(parents=True)

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError):
                download_audio(job)

        assert album.is_dir()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_folder_this_run_created_goes_when_the_move_fails(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """yt-dlp only ever writes into .tmp, so a failing `os.replace` is the
        one path that can leave an empty Artist/Album behind."""
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with patch("app.downloader.os.replace", side_effect=OSError("EXDEV")):
                with pytest.raises(DownloadError):
                    download_audio(job)

        assert not (tmp_path / "A").exists()

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_playlist_info_raises_download_error(self, mock_ydl_cls, tmp_path):
        _install_ydl_mocks(mock_ydl_cls, info={"_type": "playlist", "title": "My Mix"})

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError, match="playlist"):
                download_audio(job)

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_backfills_job_metadata_and_reports_phase(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="A", album="B")
        assert job.title is None

        phases = []

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job, on_phase=phases.append)

        assert job.title == "My Cool Track"
        assert job.duration == 245.0
        assert job.thumbnail_url == SAMPLE_INFO["thumbnail"]
        assert "metadata" in phases

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_backfill_does_not_overwrite_existing_metadata(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(
            title="Submitted Title",
            duration=10.0,
            thumbnail_url="https://example.com/existing.jpg",
            artist="A",
            album="B",
        )

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        assert job.title == "Submitted Title"
        assert job.duration == 10.0
        assert job.thumbnail_url == "https://example.com/existing.jpg"


# ===========================================================================
# Cancellation and phase reporting hooks
# ===========================================================================


class TestCancellation:
    """The progress hook is the only place a running yt-dlp download can be
    told to stop, so a cancelled token must abort it."""

    def test_cancelled_token_aborts_download(self):
        cancel = CancelToken()
        cancel.cancel()
        callback = MagicMock()
        hook = _make_progress_hook(callback, cancel)

        with pytest.raises(yt_dlp.utils.DownloadCancelled):
            hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})

        callback.assert_not_called()

    def test_a_live_token_lets_progress_through(self):
        cancel = CancelToken()
        callback = MagicMock()
        hook = _make_progress_hook(callback, cancel)

        hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})

        callback.assert_called_once_with(50.0)

    def test_a_plain_event_still_works_as_a_cancel_flag(self):
        """The hook only needs `is_set`, which keeps it testable in isolation."""
        cancel_event = threading.Event()
        cancel_event.set()
        hook = _make_progress_hook(MagicMock(), cancel_event)

        with pytest.raises(yt_dlp.utils.DownloadCancelled):
            hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})


class TestCancelToken:
    """The token is the only thing that can stop a running ffmpeg."""

    def test_a_registered_process_is_terminated(self):
        cancel = CancelToken()
        process = MagicMock()
        assert cancel.register_process(process) is True

        cancel.cancel()

        assert cancel.is_set() is True
        process.terminate.assert_called_once()

    def test_cancelling_never_waits_for_the_process(self):
        """The caller is the event loop: waiting out the grace here froze it.

        Escalating to SIGKILL belongs to the thread running the download, which
        is parked on ``communicate`` with the pipes open anyway.
        """
        cancel = CancelToken()
        process = MagicMock()
        cancel.register_process(process)

        cancel.cancel()

        process.wait.assert_not_called()
        process.kill.assert_not_called()

    def test_a_process_that_has_already_gone_is_not_an_error(self):
        """It can be reaped by its own thread between the two lock windows."""
        cancel = CancelToken()
        process = MagicMock()
        process.terminate.side_effect = OSError("No such process")
        cancel.register_process(process)

        cancel.cancel()

        assert cancel.is_set() is True

    def test_registering_after_a_cancel_is_refused(self):
        """Closes the window where a cancel lands between spawn and handover."""
        cancel = CancelToken()
        cancel.cancel()

        assert cancel.register_process(MagicMock()) is False

    def test_a_cleared_process_is_not_signalled(self):
        cancel = CancelToken()
        process = MagicMock()
        cancel.register_process(process)
        cancel.clear_process()

        cancel.cancel()

        process.terminate.assert_not_called()


class TestTerminateProcess:
    """Disposing of a process nobody else holds a handle to.

    The only caller is the download thread itself (a spawn that lost the race
    with a cancel), so this one does block until the child is reaped.
    """

    def test_a_process_that_ignores_sigterm_is_killed(self):
        process = MagicMock()
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5),
            0,
        ]

        _terminate_process(process)

        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_a_process_that_has_already_gone_is_not_an_error(self):
        process = MagicMock()
        process.terminate.side_effect = OSError("No such process")

        _terminate_process(process)

        process.kill.assert_not_called()


# ===========================================================================
# The ffmpeg conversion stage
# ===========================================================================


class TestFfmpegConversion:
    """What we actually ask ffmpeg to do, and what happens when it is stopped."""

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_the_raw_stream_is_converted_to_flac_in_the_scratch_directory(
        self, mock_ydl_cls, mock_sanitize, tmp_path, fake_ffmpeg
    ):
        _install_ydl_mocks(mock_ydl_cls, audio_suffix=".webm")

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        command = fake_ffmpeg.command_for(".flac")
        temp_dir = tmp_path / ".tmp" / job.id
        assert command[0] == "ffmpeg"
        assert command[command.index("-i") + 1] == str(temp_dir / f"{job.id}.webm")
        assert command[-1] == str(temp_dir / f"{job.id}.flac")

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_the_encode_drops_video_streams_and_source_metadata(
        self, mock_ydl_cls, mock_sanitize, tmp_path, fake_ffmpeg
    ):
        """Everything the finished file says about itself is written by us."""
        _install_ydl_mocks(mock_ydl_cls)

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(_make_job(artist="A", album="B"))

        command = fake_ffmpeg.command_for(".flac")
        assert "-vn" in command
        assert command[command.index("-map_metadata") + 1] == "-1"
        assert command[command.index("-c:a") + 1] == "flac"
        assert int(command[command.index("-compression_level") + 1]) in range(0, 13)

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_ffmpeg_gets_no_stdin_and_its_own_session(
        self, mock_ydl_cls, mock_sanitize, tmp_path, fake_ffmpeg
    ):
        """It must not read the backend's console or catch its signals."""
        import subprocess as subprocess_module

        _install_ydl_mocks(mock_ydl_cls)

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(_make_job(artist="A", album="B"))

        assert fake_ffmpeg.kwargs["stdin"] == subprocess_module.DEVNULL
        assert fake_ffmpeg.kwargs["start_new_session"] is True
        assert fake_ffmpeg.kwargs["stderr"] == subprocess_module.PIPE

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_source_that_is_already_flac_is_still_re_encoded(
        self, mock_ydl_cls, mock_sanitize, tmp_path, fake_ffmpeg
    ):
        """Bandcamp serves FLAC; the input must move out of the output's way."""
        _install_ydl_mocks(mock_ydl_cls, audio_suffix=".flac")

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        temp_dir = tmp_path / ".tmp" / job.id
        command = fake_ffmpeg.command_for(".flac")
        assert command[command.index("-i") + 1] == str(temp_dir / f"{job.id}.source.flac")
        assert result.exists()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_cancelling_mid_conversion_kills_ffmpeg_and_files_nothing(
        self, mock_ydl_cls, mock_sanitize, tmp_path, fake_ffmpeg
    ):
        """The whole point of running ffmpeg ourselves."""
        _install_ydl_mocks(mock_ydl_cls)
        fake_ffmpeg.gate = threading.Event()  # never set: the encode hangs

        cancel = CancelToken()
        job = _make_job(artist="A", album="B")
        failure: list[BaseException] = []

        def run() -> None:
            try:
                with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
                    download_audio(job, cancel=cancel)
            except BaseException as exc:  # noqa: BLE001 - re-raised in the assert
                failure.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        assert fake_ffmpeg.started.wait(5), "ffmpeg was never started"

        cancel.cancel()
        worker.join(10)

        assert not worker.is_alive()
        assert isinstance(failure[0], DownloadError)
        assert str(failure[0]) == CANCELLED_MESSAGE
        assert fake_ffmpeg.processes[0].terminated is True
        assert not (tmp_path / "A").exists()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_cancel_before_the_spawn_never_leaves_ffmpeg_running(
        self, mock_ydl_cls, mock_sanitize, tmp_path, fake_ffmpeg
    ):
        """A process that could not be handed over is reaped -- and closed --
        by its spawner, which is the only thread that still has the handle."""
        _install_ydl_mocks(mock_ydl_cls)
        cancel = CancelToken()

        original_call = fake_ffmpeg.__call__

        def cancel_then_spawn(command, **kwargs):
            process = original_call(command, **kwargs)
            cancel.cancel()  # lands between the spawn and register_process
            return process

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with patch("app.downloader.subprocess.Popen", new=cancel_then_spawn):
                with pytest.raises(DownloadError, match=CANCELLED_MESSAGE):
                    download_audio(_make_job(artist="A", album="B"), cancel=cancel)

        process = fake_ffmpeg.processes[0]
        assert process.terminated is True
        # Popen opened two pipes for it; nothing else will ever close them, so
        # skipping this leaks a pair of file descriptors per occurrence.
        assert process.stdout.closed is True
        assert process.stderr.closed is True
        assert not (tmp_path / "A").exists()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_an_ffmpeg_that_ignores_sigterm_is_killed_by_its_own_thread(
        self, mock_ydl_cls, mock_sanitize, tmp_path, fake_ffmpeg
    ):
        """Cancelling only signals; the escalation is the worker's to make.

        The caller of ``cancel`` is the event loop, so it has to come back at
        once even when ffmpeg sits on the signal for the whole grace period.
        """
        _install_ydl_mocks(mock_ydl_cls)
        fake_ffmpeg.gate = threading.Event()  # never set: the encode hangs
        fake_ffmpeg.ignore_terminate = True  # ... and SIGTERM does not stop it

        cancel = CancelToken()
        job = _make_job(artist="A", album="B")
        failure: list[BaseException] = []

        def run() -> None:
            try:
                with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
                    download_audio(job, cancel=cancel)
            except BaseException as exc:  # noqa: BLE001 - re-raised in the assert
                failure.append(exc)

        with patch("app.downloader.FFMPEG_TERMINATE_GRACE_SECONDS", 0.2):
            worker = threading.Thread(target=run)
            worker.start()
            assert fake_ffmpeg.started.wait(5), "ffmpeg was never started"

            before = time.monotonic()
            cancel.cancel()
            elapsed = time.monotonic() - before

            worker.join(10)

        assert elapsed < 0.1, f"cancel blocked its caller for {elapsed:.2f}s"
        assert not worker.is_alive()
        process = fake_ffmpeg.processes[0]
        assert process.terminated is True
        assert process.killed is True, "the grace expired and nobody escalated"
        assert isinstance(failure[0], DownloadError)
        assert str(failure[0]) == CANCELLED_MESSAGE
        assert not (tmp_path / "A").exists()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_cancel_after_the_encode_still_files_nothing(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """The stop button is re-read immediately before the move."""
        _install_ydl_mocks(mock_ydl_cls)
        cancel = CancelToken()

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with patch("app.downloader._write_tags", side_effect=lambda *a, **k: cancel.cancel()):
                with pytest.raises(DownloadError, match=CANCELLED_MESSAGE):
                    download_audio(job, cancel=cancel)

        assert not (tmp_path / "A").exists()

    def test_a_wait_that_raises_reaps_the_process_and_closes_its_pipes(
        self, fake_ffmpeg
    ):
        """Nothing else holds the handle, so an exception here would leak both."""
        with patch("app.downloader._await_ffmpeg", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                _run_ffmpeg(["-i", "in.webm", "out.flac"], None, "FLAC conversion")

        process = fake_ffmpeg.processes[0]
        assert process.terminated
        assert process.stdout.closed
        assert process.stderr.closed


# ===========================================================================
# The mutagen tagging stage
# ===========================================================================


class TestAlbumLessDownloadsAreLooseSingles:
    """A track no source named an album for lands at ``Artist/<title>.flac``.

    The domain model calls that a loose Single; earlier versions filed it under
    an invented ``Unknown Album`` folder, and the duplicate check still has to
    recognise a library written that way.
    """

    NO_ALBUM_INFO = {key: value for key, value in SAMPLE_INFO.items() if key != "album"}

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_it_lands_directly_under_the_artist(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(mock_ydl_cls, info=self.NO_ALBUM_INFO)

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(_make_job(artist="A"))

        assert result == tmp_path / "A" / "My Cool Track.flac"
        assert not (tmp_path / "A" / "Unknown Album").exists()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_it_gets_no_album_tag(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(mock_ydl_cls, info=self.NO_ALBUM_INFO)

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(_make_job(artist="A"))

        tags = FLAC(result)
        assert "ALBUM" not in tags
        assert tags["ALBUMARTIST"] == ["A"]

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_the_legacy_unknown_album_copy_still_counts_as_a_duplicate(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(mock_ydl_cls, info=self.NO_ALBUM_INFO)
        legacy = tmp_path / "A" / "Unknown Album" / "My Cool Track.flac"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"OLD")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError) as excinfo:
                download_audio(_make_job(artist="A"))

        assert str(excinfo.value) == (
            ALREADY_IN_LIBRARY_PREFIX + "A/Unknown Album/My Cool Track.flac"
        )
        assert legacy.read_bytes() == b"OLD"
        assert not (tmp_path / "A" / "My Cool Track.flac").exists()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_legacy_copy_under_another_album_is_not_a_duplicate(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """Only the ``Unknown Album`` twin counts; a real album is a real album."""
        _install_ydl_mocks(mock_ydl_cls, info=self.NO_ALBUM_INFO)
        other = tmp_path / "A" / "Some Album" / "My Cool Track.flac"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"OLD")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(_make_job(artist="A"))

        assert result == tmp_path / "A" / "My Cool Track.flac"


class TestABulkChildTrustsItsEnumeration:
    """A child job's title and album come from the preview, not from yt-dlp.

    The collection probe read the *release*; the child's own metadata pass
    reads one video, which is titled for YouTube ("Artist - 'Track' (Official
    Audio)") and can name an album the preview deliberately did not.  Where
    the two disagree the enumeration wins, so the row the user ticked is the
    file that lands.
    """

    # What yt-dlp says about the video behind a track on Glass Beams' EP.
    VIDEO_INFO = {
        **SAMPLE_INFO,
        "title": "Glass Beams - 'Horizon' (Official Audio)",
        "artist": "Glass Beams",
        "album": "Mahal",
    }

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_the_enumerated_title_names_the_file_and_the_tag(
        self, mock_ydl_cls, tmp_path
    ):
        _install_ydl_mocks(mock_ydl_cls, info=self.VIDEO_INFO)
        job = _make_job(
            parent_id="parent-1", title="Horizon", artist="Glass Beams", album="Mahal"
        )

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        assert result == tmp_path / "Glass Beams" / "Mahal" / "Horizon.flac"
        assert FLAC(result)["TITLE"] == ["Horizon"]

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_single_stays_album_less_however_yt_dlp_tags_the_video(
        self, mock_ydl_cls, tmp_path
    ):
        """A YouTube Music row's empty album is an answer, not a gap to fill."""
        _install_ydl_mocks(mock_ydl_cls, info=self.VIDEO_INFO)
        job = _make_job(
            parent_id="parent-1",
            title="Horizon",
            artist="Glass Beams",
            album_final=True,
        )
        targets: list[str] = []

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job, on_target=targets.append)

        assert result == tmp_path / "Glass Beams" / "Horizon.flac"
        assert "ALBUM" not in FLAC(result)
        assert targets == ["Glass Beams"]

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_flat_enumerated_child_still_takes_yt_dlps_album(
        self, mock_ydl_cls, tmp_path
    ):
        """The flat pass never read a release, so its empty album is a gap.

        Only ``album_final`` promises "no album, deliberately"; a child of a
        plain playlist whose listing carried no album keeps the old chain,
        where yt-dlp fills the blank in.
        """
        _install_ydl_mocks(mock_ydl_cls, info=self.VIDEO_INFO)
        job = _make_job(parent_id="parent-1", title="Horizon", artist="Glass Beams")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        assert result == tmp_path / "Glass Beams" / "Mahal" / "Horizon.flac"
        assert FLAC(result)["ALBUM"] == ["Mahal"]

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_plain_download_job_still_takes_both_from_yt_dlp(
        self, mock_ydl_cls, tmp_path
    ):
        """No parent means no enumeration, so the old chain is unchanged."""
        _install_ydl_mocks(mock_ydl_cls, info=self.VIDEO_INFO)

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(_make_job(artist="Glass Beams"))

        assert result == (
            tmp_path
            / "Glass Beams"
            / "Mahal"
            / "Glass Beams - 'Horizon' (Official Audio).flac"
        )
        assert FLAC(result)["ALBUM"] == ["Mahal"]


class TestTagging:
    """What ends up in the finished FLAC's Vorbis comments and PICTURE block."""

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_standard_tags_match_the_folders_the_file_is_filed_under(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(mock_ydl_cls)

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(_make_job(artist="A", album="B"))

        tags = FLAC(result)
        assert tags["TITLE"] == ["My Cool Track"]
        assert tags["ARTIST"] == ["A"]
        assert tags["ALBUMARTIST"] == ["A"]
        assert tags["ALBUM"] == ["B"]
        assert result.parent == tmp_path / "A" / "B"

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_source_id_and_url_record_where_the_track_came_from(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """Dedup and every later tag pass key on this pair."""
        _install_ydl_mocks(
            mock_ydl_cls,
            info={
                **SAMPLE_INFO,
                "extractor": "youtube",
                "id": "dQw4w9WgXcQ",
                "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            },
        )

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(_make_job(artist="A", album="B"))

        tags = FLAC(result)
        assert tags["SOURCEID"] == ["youtube:dQw4w9WgXcQ"]
        assert tags["SOURCEURL"] == [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        ]

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_the_extractor_half_of_the_source_id_is_lower_cased(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(
            mock_ydl_cls,
            info={**SAMPLE_INFO, "extractor": "SoundCloud", "id": "12345"},
        )

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(_make_job(artist="A", album="B"))

        assert FLAC(result)["SOURCEID"] == ["soundcloud:12345"]

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_half_missing_source_id_is_not_written_at_all(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """A tag that only looks like an identifier is worse than none."""
        _install_ydl_mocks(mock_ydl_cls, info={**SAMPLE_INFO, "extractor": "youtube"})

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(_make_job(artist="A", album="B"))

        assert "SOURCEID" not in FLAC(result)

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_the_job_url_is_the_source_url_when_yt_dlp_gives_none(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(mock_ydl_cls)
        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        assert FLAC(result)["SOURCEURL"] == [job.url]

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_date_and_track_number_appear_only_when_the_source_has_them(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(mock_ydl_cls)

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            bare = download_audio(_make_job(artist="A", album="B"))

        tags = FLAC(bare)
        assert "DATE" not in tags
        assert "TRACKNUMBER" not in tags

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_release_date_is_written_as_an_iso_date(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(
            mock_ydl_cls,
            info={**SAMPLE_INFO, "release_date": "20240513", "track_number": 4},
        )

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(_make_job(artist="A", album="B"))

        tags = FLAC(result)
        assert tags["DATE"] == ["2024-05-13"]
        assert tags["TRACKNUMBER"] == ["4"]

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_an_upload_date_is_never_mistaken_for_a_release_date(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """When a video was posted says nothing about when the track came out."""
        _install_ydl_mocks(
            mock_ydl_cls, info={**SAMPLE_INFO, "upload_date": "20240513"}
        )

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(_make_job(artist="A", album="B"))

        assert "DATE" not in FLAC(result)

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_jpeg_thumbnail_is_embedded_as_the_front_cover(
        self, mock_ydl_cls, mock_sanitize, tmp_path, fake_ffmpeg
    ):
        _install_ydl_mocks(
            mock_ydl_cls, thumbnail_bytes=TINY_JPEG, thumbnail_suffix=".jpg"
        )

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(_make_job(artist="A", album="B"))

        pictures = FLAC(result).pictures
        assert len(pictures) == 1
        assert pictures[0].type == 3  # id3 PictureType.COVER_FRONT
        assert pictures[0].mime == "image/jpeg"
        assert pictures[0].data == TINY_JPEG
        # Already embeddable, so no second ffmpeg run.
        assert len(fake_ffmpeg.commands) == 1

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_webp_thumbnail_is_converted_before_it_is_embedded(
        self, mock_ydl_cls, mock_sanitize, tmp_path, fake_ffmpeg
    ):
        """YouTube serves WebP, which FLAC players do not decode."""
        _install_ydl_mocks(
            mock_ydl_cls, thumbnail_bytes=TINY_WEBP, thumbnail_suffix=".webp"
        )

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        cover_command = fake_ffmpeg.command_for(".jpg")
        assert cover_command[cover_command.index("-i") + 1].endswith(f"{job.id}.webp")
        pictures = FLAC(result).pictures
        assert len(pictures) == 1
        assert pictures[0].mime == "image/jpeg"

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_track_with_no_thumbnail_is_still_filed(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(mock_ydl_cls)

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(_make_job(artist="A", album="B"))

        assert FLAC(result).pictures == []

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_cover_art_that_cannot_be_converted_does_not_fail_the_download(
        self, mock_ydl_cls, mock_sanitize, tmp_path, fake_ffmpeg
    ):
        """A finished track is worth more than its picture."""
        _install_ydl_mocks(
            mock_ydl_cls, thumbnail_bytes=TINY_WEBP, thumbnail_suffix=".webp"
        )

        real_call = fake_ffmpeg.__call__

        def fail_the_cover_run(command, **kwargs):
            if command[-1].endswith(".jpg"):
                fake_ffmpeg.returncode = 1
            return real_call(command, **kwargs)

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with patch("app.downloader.subprocess.Popen", new=fail_the_cover_run):
                result = download_audio(_make_job(artist="A", album="B"))

        assert result.exists()
        assert FLAC(result).pictures == []


# ===========================================================================
# Partial-file cleanup (restart recovery)
# ===========================================================================


class TestTempDirectoryCleanup:
    """Restart recovery removes a scratch directory, never library files."""

    def test_temp_dir_is_under_a_hidden_folder_in_the_library_root(self, tmp_path):
        assert job_temp_dir("job-1", download_path=str(tmp_path)) == (
            tmp_path / ".tmp" / "job-1"
        )

    def test_temp_dir_defaults_to_the_configured_download_path(self, tmp_path):
        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            assert job_temp_dir("job-1") == tmp_path / ".tmp" / "job-1"

    def test_the_whole_temp_dir_goes(self, tmp_path):
        temp_dir = tmp_path / ".tmp" / "job-1"
        (temp_dir / "nested").mkdir(parents=True)
        (temp_dir / "Test Track.webm.part").write_text("x")
        (temp_dir / "nested" / "Test Track.webp").write_text("x")

        assert remove_job_temp_dir("job-1", download_path=str(tmp_path)) is True
        assert not temp_dir.exists()

    def test_a_missing_temp_dir_is_harmless(self, tmp_path):
        assert remove_job_temp_dir("job-1", download_path=str(tmp_path)) is False

    def test_the_temp_root_goes_with_the_last_job(self, tmp_path):
        """`.tmp` should not sit in the library root until the next boot."""
        (tmp_path / ".tmp" / "job-1").mkdir(parents=True)

        assert remove_job_temp_dir("job-1", download_path=str(tmp_path)) is True
        assert not (tmp_path / ".tmp").exists()

    def test_the_temp_root_stays_while_another_job_is_using_it(self, tmp_path):
        (tmp_path / ".tmp" / "job-1").mkdir(parents=True)
        other = tmp_path / ".tmp" / "job-2"
        other.mkdir()

        remove_job_temp_dir("job-1", download_path=str(tmp_path))

        assert other.is_dir()

    def test_a_removed_temp_root_is_recreated_by_the_next_download(self, tmp_path):
        """The rmdir races a concurrent job's mkdir, harmlessly: `parents=True`
        puts `.tmp` back if it vanished in between."""
        (tmp_path / ".tmp" / "job-1").mkdir(parents=True)
        remove_job_temp_dir("job-1", download_path=str(tmp_path))

        job_temp_dir("job-2", download_path=str(tmp_path)).mkdir(
            parents=True, exist_ok=True
        )

        assert (tmp_path / ".tmp" / "job-2").is_dir()

    def test_library_files_are_never_reachable_from_a_job_id(self, tmp_path):
        """The path is derived from the id alone, so no album folder is scanned."""
        album = tmp_path / "An Artist" / "An Album"
        album.mkdir(parents=True)
        for name in ("Test Track.mp3", "Test Track.flac", "Test Track.lrc"):
            (album / name).write_text("mine")
        (tmp_path / ".tmp" / "job-1").mkdir(parents=True)

        remove_job_temp_dir("job-1", download_path=str(tmp_path))

        assert {path.name for path in album.iterdir()} == {
            "Test Track.mp3",
            "Test Track.flac",
            "Test Track.lrc",
        }

    def test_orphans_go_and_known_jobs_stay(self, tmp_path):
        known, orphan_a, orphan_b = (str(uuid.uuid4()) for _ in range(3))
        for job_id in (known, orphan_a, orphan_b):
            (tmp_path / ".tmp" / job_id).mkdir(parents=True)

        removed = remove_orphan_temp_dirs({known}, download_path=str(tmp_path))

        assert sorted(removed) == sorted([orphan_a, orphan_b])
        assert (tmp_path / ".tmp" / known).is_dir()

    def test_the_temp_root_is_removed_once_it_is_empty(self, tmp_path):
        (tmp_path / ".tmp" / str(uuid.uuid4())).mkdir(parents=True)

        remove_orphan_temp_dirs(set(), download_path=str(tmp_path))

        assert not (tmp_path / ".tmp").exists()

    def test_a_missing_temp_root_is_harmless(self, tmp_path):
        assert remove_orphan_temp_dirs({"known"}, download_path=str(tmp_path)) == []

    def test_a_non_uuid_directory_survives_the_sweep(self, tmp_path):
        """Defence in depth: `.tmp` is a legal artist name, so if it ever became
        one, its albums must not be mistaken for scratch directories."""
        album = tmp_path / ".tmp" / "An Album"
        album.mkdir(parents=True)
        (album / "Track.flac").write_text("mine")

        assert remove_orphan_temp_dirs(set(), download_path=str(tmp_path)) == []
        assert (album / "Track.flac").exists()

    @pytest.mark.parametrize("job_id", ["", ".", "..", "a/b", "a\\b", "a\x00b"])
    def test_a_job_id_that_is_not_a_path_segment_is_refused(self, job_id, tmp_path):
        """`remove_job_temp_dir("..")` would otherwise rmtree the library."""
        with pytest.raises(ValueError, match="path segment"):
            job_temp_dir(job_id, download_path=str(tmp_path))
        with pytest.raises(ValueError, match="path segment"):
            remove_job_temp_dir(job_id, download_path=str(tmp_path))


class TestDownloadWritesNothingUnfinishedIntoTheLibrary:
    """yt-dlp's scratch files must land in .tmp, not next to the album."""

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_every_path_yt_dlp_gets_is_the_job_temp_dir(self, mock_ydl_cls, tmp_path):
        """yt-dlp writes only into the scratch directory; we do the final move."""
        _install_ydl_mocks(mock_ydl_cls)
        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        opts = mock_ydl_cls.call_args_list[1][0][0]
        temp_dir = str(tmp_path / ".tmp" / job.id)
        assert opts["paths"] == {
            "home": temp_dir,
            "temp": temp_dir,
            "thumbnail": temp_dir,
        }
        assert result == tmp_path / "A" / "B" / result.name

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_outtmpl_is_the_job_id(self, mock_ydl_cls, tmp_path):
        """The template is expanded as a shell path, so only the job id -- which
        cannot contain a `$` or a separator -- may appear in it."""
        _install_ydl_mocks(mock_ydl_cls)
        job = _make_job(id=str(uuid.uuid4()), artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        outtmpl = mock_ydl_cls.call_args_list[1][0][0]["outtmpl"]
        assert outtmpl == f"{job.id}.%(ext)s"
        assert not Path(outtmpl).is_absolute()
        assert os.sep not in outtmpl

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_yt_dlp_output_is_routed_into_logging(self, mock_ydl_cls, tmp_path):
        """`quiet` alone leaves the progress bar and ERROR lines on the console."""
        _install_ydl_mocks(mock_ydl_cls)
        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        for opts in (call[0][0] for call in mock_ydl_cls.call_args_list):
            assert opts["noprogress"] is True
            assert isinstance(opts["logger"], _YtDlpLogger)

    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_the_temp_dir_exists_before_yt_dlp_runs(self, mock_ydl_cls, tmp_path):
        _, download_ydl = _install_ydl_mocks(mock_ydl_cls)
        real_download = download_ydl.extract_info.side_effect
        seen: list[bool] = []

        def check(url, download=False):
            seen.append((tmp_path / ".tmp" / "job-1").is_dir())
            return real_download(url, download=download)

        download_ydl.extract_info.side_effect = check

        job = _make_job(id="job-1", artist="A", album="B")
        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        assert seen == [True]


# ===========================================================================
# The pre-download probe
# ===========================================================================


class TestPreDownloadProbe:
    """download_audio's own metadata pass: it decides where the file will go,
    so it runs before a byte is fetched -- and a cancel during it must count."""

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_the_probe_downloads_nothing_and_gives_up_on_a_dead_url_quickly(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(mock_ydl_cls)

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(_make_job(artist="A", album="B"))

        probe_opts = mock_ydl_cls.call_args_list[0][0][0]
        assert probe_opts["skip_download"] is True
        assert probe_opts["extractor_retries"] == 1
        # `retries` is deliberately untouched: it governs the file downloaders,
        # which only the real download below uses and still wants persistent.
        assert "retries" not in probe_opts

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_cancel_during_the_probe_fetches_nothing(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """The probe has no progress hook, so nothing else would notice it."""
        cancel = CancelToken()
        extract_ydl, download_ydl = _install_ydl_mocks(mock_ydl_cls)

        def cancel_then_answer(url, download=False):
            cancel.cancel()
            return SAMPLE_INFO

        extract_ydl.extract_info.side_effect = cancel_then_answer

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError, match=CANCELLED_MESSAGE):
                download_audio(_make_job(artist="A", album="B"), cancel=cancel)

        download_ydl.extract_info.assert_not_called()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_cancel_between_the_probe_and_the_download_fetches_nothing(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """Resolving the output path and creating the scratch directory both
        happen after the probe and before the first hook exists."""
        cancel = CancelToken()
        _, download_ydl = _install_ydl_mocks(mock_ydl_cls)

        def cancel_then_resolve(**kwargs):
            cancel.cancel()
            return get_output_path(**kwargs)

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with patch(
                "app.downloader.get_output_path", side_effect=cancel_then_resolve
            ):
                with pytest.raises(DownloadError, match=CANCELLED_MESSAGE):
                    download_audio(_make_job(artist="A", album="B"), cancel=cancel)

        download_ydl.extract_info.assert_not_called()


# ===========================================================================
# Taking a track back out of the library
# ===========================================================================


class TestFiledTrack:
    """What download_audio reports having filed, and how that move is undone.

    The one caller is the queue's timeout path: a job that was failed from the
    event loop while its thread was still moving the file.
    """

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_the_move_is_reported_with_the_folders_it_created(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(mock_ydl_cls)
        filed: list[FiledTrack] = []

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(
                _make_job(artist="A", album="B"), on_filed=filed.append
            )

        assert filed[0].path == result
        assert filed[0].created_dirs == {tmp_path / "A", tmp_path / "A" / "B"}

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_folder_that_was_already_there_is_not_reported_as_created(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        _install_ydl_mocks(mock_ydl_cls)
        (tmp_path / "A").mkdir()
        filed: list[FiledTrack] = []

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(_make_job(artist="A", album="B"), on_filed=filed.append)

        assert filed[0].created_dirs == {tmp_path / "A" / "B"}

    def test_unfiling_removes_the_track_and_the_folders_this_run_created(
        self, tmp_path
    ):
        album = tmp_path / "A" / "B"
        album.mkdir(parents=True)
        track = album / "Track.flac"
        track.write_text("flac")

        unfile_track(FiledTrack(track, frozenset({tmp_path / "A", album})))

        assert not track.exists()
        assert not (tmp_path / "A").exists()

    def test_unfiling_leaves_a_folder_the_user_already_had(self, tmp_path):
        album = tmp_path / "A" / "B"
        album.mkdir(parents=True)
        track = album / "Track.flac"
        track.write_text("flac")

        unfile_track(FiledTrack(track, frozenset()))

        assert not track.exists()
        assert album.is_dir()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_a_track_unfiled_by_its_caller_still_finishes_the_download(
        self, mock_ydl_cls, mock_sanitize, tmp_path, caplog
    ):
        """The closing size log must not be able to fail a finished download.

        The queue's timeout hand-off removes the track from inside ``on_filed``,
        so by the time the size is read the file can legitimately be gone.
        """
        _install_ydl_mocks(mock_ydl_cls)

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with caplog.at_level(logging.INFO, logger="app.downloader"):
                result = download_audio(
                    _make_job(artist="A", album="B"),
                    on_filed=lambda filed: filed.path.unlink(),
                )

        assert not result.exists()
        assert "size unavailable" in caplog.text

    def test_unfiling_a_track_that_is_already_gone_is_harmless(self, tmp_path):
        album = tmp_path / "A" / "B"
        album.mkdir(parents=True)

        unfile_track(FiledTrack(album / "Track.flac", frozenset({album})))

        assert album.is_dir()


class TestMissingOutputDirs:
    """The library root is never a directory the cleanup may remove."""

    def test_the_root_is_never_recorded_as_created(self, tmp_path):
        """A loose Single's grandparent *is* the root, existing or not.

        On a first run into an empty volume the root does not exist yet, and
        recording it here would let one failed download ``rmdir`` the whole
        library root.
        """
        root = tmp_path / "not-yet-there"
        output = root / "Lone" / "Airglow.flac"

        created = _missing_output_dirs(output, str(root))

        assert created == frozenset({root / "Lone"})

    def test_an_album_records_both_folders_it_has_to_create(self, tmp_path):
        root = tmp_path / "music"
        root.mkdir()
        output = root / "Lone" / "Reality Testing" / "Airglow.flac"

        created = _missing_output_dirs(output, str(root))

        assert created == frozenset(
            {root / "Lone", root / "Lone" / "Reality Testing"}
        )
