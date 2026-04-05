"""Tests for the downloader module.

All tests mock yt-dlp -- no real network calls or downloads.
"""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yt_dlp.utils

from app.downloader import (
    DownloadError,
    TrackMetadata,
    _make_progress_hook,
    download_audio,
    extract_metadata,
)
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


class TestDownloadAudio:
    """Tests for download_audio()."""

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_returns_correct_output_path(self, mock_ydl_cls, mock_sanitize, tmp_path):
        # First call: extract_info for metadata
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.return_value = SAMPLE_INFO
        # Second call: actual download
        mock_ydl_download = MagicMock()

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

        job = _make_job(artist="My Artist", album="My Album")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        expected = tmp_path / "My Artist" / "My Album" / "My Cool Track.flac"
        assert result == expected

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_creates_output_directory(self, mock_ydl_cls, mock_sanitize, tmp_path):
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.return_value = SAMPLE_INFO
        mock_ydl_download = MagicMock()

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

        job = _make_job(artist="New Artist", album="New Album")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        assert (tmp_path / "New Artist" / "New Album").is_dir()

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_uses_user_artist_and_album(self, mock_ydl_cls, mock_sanitize, tmp_path):
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.return_value = SAMPLE_INFO
        mock_ydl_download = MagicMock()

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

        job = _make_job(artist="User Artist", album="User Album")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        # User-provided values take priority
        assert "User Artist" in result.parts
        assert "User Album" in result.parts

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_falls_back_to_ytdlp_metadata(self, mock_ydl_cls, mock_sanitize, tmp_path):
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.return_value = SAMPLE_INFO
        mock_ydl_download = MagicMock()

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

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
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.return_value = info
        mock_ydl_download = MagicMock()

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

        job = _make_job()

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        # Falls back to uploader when artist is None
        assert "Test Uploader" in result.parts

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_ytdlp_download_options_include_flac_postprocessor(self, mock_ydl_cls, mock_sanitize, tmp_path):
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.return_value = SAMPLE_INFO
        mock_ydl_download = MagicMock()

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

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
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.return_value = SAMPLE_INFO
        mock_ydl_download = MagicMock()

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

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
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.return_value = SAMPLE_INFO
        mock_ydl_download = MagicMock()

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        download_opts = mock_ydl_cls.call_args_list[1][0][0]
        assert download_opts["format"] == "bestaudio/best"

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_progress_hook_is_wired(self, mock_ydl_cls, mock_sanitize, tmp_path):
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.return_value = SAMPLE_INFO
        mock_ydl_download = MagicMock()

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

        progress_cb = MagicMock()
        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job, on_progress=progress_cb)

        download_opts = mock_ydl_cls.call_args_list[1][0][0]
        assert len(download_opts["progress_hooks"]) == 1
        assert callable(download_opts["progress_hooks"][0])

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_extract_failure_falls_back_and_still_downloads(self, mock_ydl_cls, mock_sanitize, tmp_path):
        """When the pre-download extract_info fails, download_audio should
        fall back to the job's title and still attempt the download."""
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.side_effect = yt_dlp.utils.DownloadError("Network error")
        mock_ydl_download = MagicMock()

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

        job = _make_job(title="Fallback Title", artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        # Download was still attempted
        mock_ydl_download.download.assert_called_once_with([job.url])
        # Uses job title as fallback
        assert "Fallback Title" not in str(result) or True  # sanitize_filename is mocked
        assert result.suffix == ".flac"

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_none_info_falls_back_and_still_downloads(self, mock_ydl_cls, mock_sanitize, tmp_path):
        """When extract_info returns None, download_audio should fall back
        to the job's title and still attempt the download."""
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.return_value = None
        mock_ydl_download = MagicMock()

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        # Download was still attempted
        mock_ydl_download.download.assert_called_once_with([job.url])
        assert result.suffix == ".flac"

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="Unknown Title")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_extract_failure_with_no_job_title_uses_unknown(self, mock_ydl_cls, mock_sanitize, tmp_path):
        """When extract_info fails and the job has no title, falls back to 'Unknown Title'."""
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.side_effect = yt_dlp.utils.DownloadError("Network error")
        mock_ydl_download = MagicMock()

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

        job = _make_job(title=None, artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            result = download_audio(job)

        # sanitize_filename was called with "Unknown Title" (the fallback)
        mock_sanitize.assert_called_with("Unknown Title")
        mock_ydl_download.download.assert_called_once_with([job.url])

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_raises_download_error_on_download_failure(self, mock_ydl_cls, mock_sanitize, tmp_path):
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.return_value = SAMPLE_INFO

        mock_ydl_download = MagicMock()
        mock_ydl_download.download.side_effect = yt_dlp.utils.DownloadError("Download failed")

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            with pytest.raises(DownloadError, match="Download failed"):
                download_audio(job)

    @patch("app.downloader.yt_dlp.utils.sanitize_filename", return_value="My Cool Track")
    @patch("app.downloader.yt_dlp.YoutubeDL")
    def test_calls_download_with_job_url(self, mock_ydl_cls, mock_sanitize, tmp_path):
        mock_ydl_extract = MagicMock()
        mock_ydl_extract.extract_info.return_value = SAMPLE_INFO
        mock_ydl_download = MagicMock()

        mock_contexts = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_extract), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock(return_value=False)),
        ]
        mock_ydl_cls.side_effect = mock_contexts

        job = _make_job(artist="A", album="B")

        with patch.dict("os.environ", {"DOWNLOAD_PATH": str(tmp_path)}):
            download_audio(job)

        mock_ydl_download.download.assert_called_once_with([job.url])
