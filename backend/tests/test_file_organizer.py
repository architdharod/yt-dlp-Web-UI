"""Tests for the file_organizer module.

Covers all combinations of the artist/album priority chain:
    1. User-provided values
    2. yt-dlp metadata fallback
    3. "Unknown Artist" / "Unknown Album" final fallback
"""

from pathlib import Path

from app.file_organizer import (
    DEFAULT_DOWNLOAD_PATH,
    FALLBACK_ALBUM,
    FALLBACK_ARTIST,
    get_output_path,
)


TRACK = "song.flac"
DOWNLOAD = "/music/downloads"


class TestUserProvidedValues:
    """User-provided artist and album are used when present."""

    def test_both_user_artist_and_album(self):
        result = get_output_path(
            TRACK,
            user_artist="Radiohead",
            user_album="OK Computer",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / "Radiohead" / "OK Computer" / TRACK

    def test_user_artist_only_album_from_ytdlp(self):
        result = get_output_path(
            TRACK,
            user_artist="Radiohead",
            ytdlp_album="OK Computer",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / "Radiohead" / "OK Computer" / TRACK

    def test_user_album_only_artist_from_ytdlp(self):
        result = get_output_path(
            TRACK,
            user_album="OK Computer",
            ytdlp_artist="Radiohead",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / "Radiohead" / "OK Computer" / TRACK

    def test_user_values_take_priority_over_ytdlp(self):
        """User-provided values override yt-dlp metadata even when both exist."""
        result = get_output_path(
            TRACK,
            user_artist="User Artist",
            user_album="User Album",
            ytdlp_artist="YT Artist",
            ytdlp_album="YT Album",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / "User Artist" / "User Album" / TRACK


class TestYtdlpFallback:
    """yt-dlp metadata is used when user values are not provided."""

    def test_both_from_ytdlp(self):
        result = get_output_path(
            TRACK,
            ytdlp_artist="Radiohead",
            ytdlp_album="OK Computer",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / "Radiohead" / "OK Computer" / TRACK

    def test_ytdlp_artist_only_album_fallback(self):
        result = get_output_path(
            TRACK,
            ytdlp_artist="Radiohead",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / "Radiohead" / FALLBACK_ALBUM / TRACK

    def test_ytdlp_album_only_artist_fallback(self):
        result = get_output_path(
            TRACK,
            ytdlp_album="OK Computer",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / "OK Computer" / TRACK


class TestFullFallback:
    """Falls back to Unknown Artist / Unknown Album when nothing is provided."""

    def test_no_metadata_at_all(self):
        result = get_output_path(TRACK, download_path=DOWNLOAD)
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / FALLBACK_ALBUM / TRACK

    def test_all_none_explicitly(self):
        result = get_output_path(
            TRACK,
            user_artist=None,
            user_album=None,
            ytdlp_artist=None,
            ytdlp_album=None,
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / FALLBACK_ALBUM / TRACK


class TestEmptyAndWhitespaceHandling:
    """Empty strings and whitespace-only strings are treated as missing."""

    def test_empty_user_artist_falls_back_to_ytdlp(self):
        result = get_output_path(
            TRACK,
            user_artist="",
            ytdlp_artist="Radiohead",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / "Radiohead" / FALLBACK_ALBUM / TRACK

    def test_whitespace_user_artist_falls_back_to_ytdlp(self):
        result = get_output_path(
            TRACK,
            user_artist="   ",
            ytdlp_artist="Radiohead",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / "Radiohead" / FALLBACK_ALBUM / TRACK

    def test_empty_user_album_falls_back_to_ytdlp(self):
        result = get_output_path(
            TRACK,
            user_album="",
            ytdlp_album="OK Computer",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / "OK Computer" / TRACK

    def test_whitespace_user_album_falls_back_to_ytdlp(self):
        result = get_output_path(
            TRACK,
            user_album="   ",
            ytdlp_album="OK Computer",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / "OK Computer" / TRACK

    def test_empty_ytdlp_values_fall_back_to_unknown(self):
        result = get_output_path(
            TRACK,
            ytdlp_artist="",
            ytdlp_album="",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / FALLBACK_ALBUM / TRACK

    def test_whitespace_ytdlp_values_fall_back_to_unknown(self):
        result = get_output_path(
            TRACK,
            ytdlp_artist="  \t  ",
            ytdlp_album="  \n  ",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / FALLBACK_ALBUM / TRACK

    def test_empty_user_and_ytdlp_both_fall_back(self):
        result = get_output_path(
            TRACK,
            user_artist="",
            user_album="",
            ytdlp_artist="",
            ytdlp_album="",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / FALLBACK_ALBUM / TRACK

    def test_whitespace_values_are_stripped(self):
        """Leading/trailing whitespace is stripped from valid values."""
        result = get_output_path(
            TRACK,
            user_artist="  Radiohead  ",
            user_album="  OK Computer  ",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / "Radiohead" / "OK Computer" / TRACK


class TestOutputStructure:
    """Output always matches DOWNLOAD_PATH/Artist/Album/track.flac structure."""

    def test_path_has_four_components(self):
        """Path is always download_path / artist / album / filename."""
        result = get_output_path(
            TRACK,
            user_artist="Radiohead",
            user_album="OK Computer",
            download_path=DOWNLOAD,
        )
        # The path relative to download_path should be exactly Artist/Album/track
        relative = result.relative_to(DOWNLOAD)
        assert len(relative.parts) == 3  # Artist, Album, track filename
        assert relative.parts[0] == "Radiohead"
        assert relative.parts[1] == "OK Computer"
        assert relative.parts[2] == TRACK

    def test_default_download_path(self):
        """When download_path is not specified, uses the default."""
        result = get_output_path(TRACK, user_artist="Artist", user_album="Album")
        assert result == Path(DEFAULT_DOWNLOAD_PATH) / "Artist" / "Album" / TRACK

    def test_custom_download_path(self):
        result = get_output_path(
            TRACK,
            user_artist="Artist",
            user_album="Album",
            download_path="/custom/path",
        )
        assert result == Path("/custom/path") / "Artist" / "Album" / TRACK

    def test_returns_path_object(self):
        result = get_output_path(TRACK, download_path=DOWNLOAD)
        assert isinstance(result, Path)

    def test_fallback_path_still_has_correct_structure(self):
        """Even with all fallbacks, structure is maintained."""
        result = get_output_path(TRACK, download_path=DOWNLOAD)
        relative = result.relative_to(DOWNLOAD)
        assert len(relative.parts) == 3
        assert relative.parts[0] == FALLBACK_ARTIST
        assert relative.parts[1] == FALLBACK_ALBUM
        assert relative.parts[2] == TRACK

    def test_preserves_track_filename(self):
        """The track filename is passed through as-is."""
        fancy_name = "01 - Paranoid Android.flac"
        result = get_output_path(
            fancy_name,
            user_artist="Radiohead",
            user_album="OK Computer",
            download_path=DOWNLOAD,
        )
        assert result.name == fancy_name
