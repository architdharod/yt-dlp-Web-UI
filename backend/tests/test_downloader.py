"""Tests for the downloader module.

All tests mock yt-dlp -- no real network calls or downloads.
"""

import os
import threading
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yt_dlp.utils

from app.downloader import (
    DownloadError,
    _YtDlpLogger,
    TrackMetadata,
    _make_postprocessor_hook,
    _make_progress_hook,
    download_audio,
    extract_metadata,
    job_temp_dir,
    remove_job_temp_dir,
    remove_orphan_temp_dirs,
    track_filename_for,
)
from app.file_organizer import UnsafePathError
from app.models import Job


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
):
    """Wire a mocked ``YoutubeDL`` class for the two contexts download_audio opens.

    The first context answers the metadata-only ``extract_info``; the second
    answers ``extract_info(url, download=True)``.  Unless *create_file* is False
    the second one also creates the file yt-dlp would have written -- in
    ``paths["home"]``, named after the ``outtmpl`` -- and reports it back in
    ``requested_downloads[0]["filepath"]``, which is where download_audio looks
    for the produced file.

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
        target = Path(opts["paths"]["home"]) / (stem + ".flac")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"FLAC")
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
    def test_ytdlp_download_options_include_flac_postprocessor(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        # The second YoutubeDL call is for downloading
        download_opts = mock_ydl_cls.call_args_list[1][0][0]
        postprocessors = download_opts["postprocessors"]

        flac_pp = [pp for pp in postprocessors if pp["key"] == "FFmpegExtractAudio"]
        assert len(flac_pp) == 1
        assert flac_pp[0]["preferredcodec"] == "flac"

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_ytdlp_download_options_include_metadata_and_thumbnail(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        download_opts = mock_ydl_cls.call_args_list[1][0][0]
        postprocessors = download_opts["postprocessors"]
        pp_keys = [pp["key"] for pp in postprocessors]

        assert "FFmpegMetadata" in pp_keys
        assert "EmbedThumbnail" in pp_keys
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
    def test_postprocessor_hook_is_wired(self, mock_ydl_cls, mock_sanitize, tmp_path):
        _install_ydl_mocks(mock_ydl_cls)

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job, on_phase=MagicMock())

        download_opts = mock_ydl_cls.call_args_list[1][0][0]
        assert len(download_opts["postprocessor_hooks"]) == 1
        assert callable(download_opts["postprocessor_hooks"][0])

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
    def test_an_existing_target_flac_is_replaced(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """Re-downloading a track overwrites it, as yt-dlp's own move did."""
        _install_ydl_mocks(mock_ydl_cls)
        existing = tmp_path / "A" / "B" / "My Cool Track.flac"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"OLD")

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        assert result == existing
        assert existing.read_bytes() == b"FLAC"

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
    def test_a_non_flac_result_is_refused_rather_than_renamed(
        self, mock_ydl_cls, mock_sanitize, tmp_path
    ):
        """The move renames to `.flac`, so the produced suffix must be checked.

        If FFmpegExtractAudio were skipped, raw `.m4a` audio would otherwise be
        filed in the library under a `.flac` name.
        """
        _, download_ydl = _install_ydl_mocks(mock_ydl_cls)

        def produce_m4a(url, download=False):
            result = dict(SAMPLE_INFO)
            opts = mock_ydl_cls.call_args_list[-1][0][0]
            stem = opts["outtmpl"][: -len(".%(ext)s")]
            target = Path(opts["paths"]["home"]) / (stem + ".m4a")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"M4A")
            result["requested_downloads"] = [{"filepath": str(target)}]
            return result

        download_ydl.extract_info.side_effect = produce_m4a
        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError, match=r"\.m4a file, not FLAC"):
                download_audio(job)

        assert not (tmp_path / "A").exists()

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
    told to stop, so a set cancel event must abort it."""

    def test_set_cancel_event_aborts_download(self):
        cancel_event = threading.Event()
        cancel_event.set()
        callback = MagicMock()
        hook = _make_progress_hook(callback, cancel_event)

        with pytest.raises(yt_dlp.utils.DownloadCancelled):
            hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})

        callback.assert_not_called()

    def test_unset_cancel_event_lets_progress_through(self):
        cancel_event = threading.Event()
        callback = MagicMock()
        hook = _make_progress_hook(callback, cancel_event)

        hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})

        callback.assert_called_once_with(50.0)


class TestMakePostprocessorHook:
    """The 'converting' phase must come from ffmpeg actually starting."""

    def test_extract_audio_start_reports_converting(self):
        on_phase = MagicMock()
        hook = _make_postprocessor_hook(on_phase)

        hook({"status": "started", "postprocessor": "ExtractAudio"})

        on_phase.assert_called_once_with("converting")

    def test_other_postprocessor_events_are_ignored(self):
        on_phase = MagicMock()
        hook = _make_postprocessor_hook(on_phase)

        hook({"status": "finished", "postprocessor": "ExtractAudio"})
        hook({"status": "started", "postprocessor": "EmbedThumbnail"})
        hook({"status": "processing", "postprocessor": "FFmpegMetadata"})

        on_phase.assert_not_called()

    def test_none_callback_does_not_error(self):
        hook = _make_postprocessor_hook(None)

        hook({"status": "started", "postprocessor": "ExtractAudio"})


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
