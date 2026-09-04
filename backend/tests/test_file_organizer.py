"""Tests for the file_organizer module.

Covers all combinations of the artist/album priority chain:
    1. User-provided values
    2. yt-dlp metadata fallback
    3. "Unknown Artist" for the artist; no album at all for the album

A track nobody named an album for is a loose Single at ``Artist/<title>.flac``
(domain model), not a track under an invented "Unknown Album" folder, so the
album half of the chain ends in :data:`NO_ALBUM` and the path is one component
shorter.
"""

from pathlib import Path

import pytest

from app.file_organizer import (
    DEFAULT_DOWNLOAD_PATH,
    FALLBACK_ARTIST,
    NO_ALBUM,
    get_output_path,
    resolve_artist_album,
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

    def test_ytdlp_artist_only_is_a_loose_single(self):
        result = get_output_path(
            TRACK,
            ytdlp_artist="Radiohead",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / "Radiohead" / TRACK

    def test_ytdlp_album_only_artist_fallback(self):
        result = get_output_path(
            TRACK,
            ytdlp_album="OK Computer",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / "OK Computer" / TRACK


class TestFullFallback:
    """Falls back to Unknown Artist, and to a loose Single, when nothing is given."""

    def test_no_metadata_at_all(self):
        result = get_output_path(TRACK, download_path=DOWNLOAD)
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / TRACK

    def test_all_none_explicitly(self):
        result = get_output_path(
            TRACK,
            user_artist=None,
            user_album=None,
            ytdlp_artist=None,
            ytdlp_album=None,
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / TRACK


class TestEmptyAndWhitespaceHandling:
    """Empty strings and whitespace-only strings are treated as missing."""

    def test_empty_user_artist_falls_back_to_ytdlp(self):
        result = get_output_path(
            TRACK,
            user_artist="",
            ytdlp_artist="Radiohead",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / "Radiohead" / TRACK

    def test_whitespace_user_artist_falls_back_to_ytdlp(self):
        result = get_output_path(
            TRACK,
            user_artist="   ",
            ytdlp_artist="Radiohead",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / "Radiohead" / TRACK

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
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / TRACK

    def test_whitespace_ytdlp_values_fall_back_to_unknown(self):
        result = get_output_path(
            TRACK,
            ytdlp_artist="  \t  ",
            ytdlp_album="  \n  ",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / TRACK

    def test_empty_user_and_ytdlp_both_fall_back(self):
        result = get_output_path(
            TRACK,
            user_artist="",
            user_album="",
            ytdlp_artist="",
            ytdlp_album="",
            download_path=DOWNLOAD,
        )
        assert result == Path(DOWNLOAD) / FALLBACK_ARTIST / TRACK

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

    def test_fallback_path_is_a_loose_single_under_the_fallback_artist(self):
        """With no album anywhere, the track sits directly under the artist."""
        result = get_output_path(TRACK, download_path=DOWNLOAD)
        relative = result.relative_to(DOWNLOAD)
        assert len(relative.parts) == 2
        assert relative.parts[0] == FALLBACK_ARTIST
        assert relative.parts[1] == TRACK

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


class TestPathTraversalIsImpossible:
    """User-supplied artist/album/filename must never escape DOWNLOAD_PATH.

    ``pathlib`` does not normalise ``..`` and an absolute component silently
    replaces the base, so every component is sanitised and the final path is
    checked against the root.
    """

    def _assert_contained(self, result: Path) -> None:
        relative = result.relative_to(DOWNLOAD)
        # Three components under an album, two for a loose Single.
        assert len(relative.parts) in (2, 3)
        assert ".." not in relative.parts
        assert "." not in relative.parts
        assert not any(part.startswith("/") for part in relative.parts)
        # And it really stays inside the root once symlinks/.. are resolved.
        assert result.resolve().is_relative_to(Path(DOWNLOAD).resolve())

    def test_dot_dot_traversal_in_artist_and_album(self):
        result = get_output_path(
            TRACK,
            user_artist="../../etc",
            user_album="/tmp/evil",
            download_path=DOWNLOAD,
        )
        self._assert_contained(result)

    def test_bare_parent_artist_falls_back(self):
        result = get_output_path(TRACK, user_artist="..", download_path=DOWNLOAD)
        assert result.parts[-2] == FALLBACK_ARTIST
        self._assert_contained(result)

    def test_bare_dot_artist_falls_back(self):
        result = get_output_path(TRACK, user_artist=".", download_path=DOWNLOAD)
        assert result.parts[-2] == FALLBACK_ARTIST
        self._assert_contained(result)

    def test_bare_parent_album_becomes_no_album_at_all(self):
        result = get_output_path(TRACK, user_album="..", download_path=DOWNLOAD)
        assert result.relative_to(DOWNLOAD).parts == (FALLBACK_ARTIST, TRACK)
        self._assert_contained(result)

    def test_separator_becomes_a_single_component(self):
        result = get_output_path(
            TRACK, user_artist="a/b", user_album="Album", download_path=DOWNLOAD
        )
        relative = result.relative_to(DOWNLOAD)
        assert len(relative.parts) == 3
        assert "/" not in relative.parts[0]
        assert relative.parts[0] != "a"
        self._assert_contained(result)

    def test_traversal_in_ytdlp_values_is_also_sanitised(self):
        """Site metadata is no more trustworthy than user input."""
        result = get_output_path(
            TRACK,
            ytdlp_artist="../../../root",
            ytdlp_album="..",
            download_path=DOWNLOAD,
        )
        assert result.relative_to(DOWNLOAD).parts[-1] == TRACK
        self._assert_contained(result)

    def test_parent_filename_falls_back(self):
        result = get_output_path("..", user_artist="A", user_album="B", download_path=DOWNLOAD)
        assert result.name == "Unknown Title.flac"
        self._assert_contained(result)

    def test_filename_with_separator_stays_one_component(self):
        result = get_output_path(
            "../../evil.flac", user_artist="A", user_album="B", download_path=DOWNLOAD
        )
        self._assert_contained(result)


class TestHousekeepingDirectoriesAreReserved:
    """``.tmp`` and ``.trash`` are swept by the app, so no album may live there.

    ``sanitize_filename`` passes them straight through, and the boot sweep
    rmtrees whatever it finds in ``.tmp`` -- an artist called ".tmp" would have
    its albums deleted.
    """

    @pytest.mark.parametrize("name", [".tmp", ".TMP", ".Tmp", ".trash", ".TRASH"])
    def test_reserved_artist_falls_back(self, name):
        result = get_output_path(TRACK, user_artist=name, download_path=DOWNLOAD)
        assert result.parts[-2] == FALLBACK_ARTIST

    @pytest.mark.parametrize("name", [".tmp", ".TMP", ".trash"])
    def test_reserved_album_leaves_the_track_loose(self, name):
        result = get_output_path(
            TRACK, user_artist="A", user_album=name, download_path=DOWNLOAD
        )
        assert result.relative_to(DOWNLOAD).parts == ("A", TRACK)

    def test_a_leading_dot_is_stripped_from_the_folder_name(self):
        """"...And You Will Know Us by the Trail of Dead" is a real band.

        Its name is kept, but not its leading dots: a dot-prefixed folder is
        hidden from the scanner, from Navidrome and from Lidarr, so filing the
        download verbatim would put it where the user cannot see it in any of
        the three.  The tags still carry the band's name in full.
        """
        artist = "...And You Will Know Us by the Trail of Dead"
        result = get_output_path(TRACK, user_artist=artist, download_path=DOWNLOAD)
        assert result.parts[-2] == "And You Will Know Us by the Trail of Dead"

    def test_a_name_of_nothing_but_dots_falls_back(self):
        """Stripping the dots off "...." leaves nothing to name a folder."""
        result = get_output_path(TRACK, user_artist="....", download_path=DOWNLOAD)
        assert result.parts[-2] == FALLBACK_ARTIST


# ===========================================================================
# resolve_artist_album
# ===========================================================================


class TestResolveArtistAlbum:
    """The tagger and the path builder must not resolve names separately: a
    FLAC whose ALBUMARTIST disagrees with its folder is filed twice by
    Navidrome."""

    def test_it_returns_what_get_output_path_files_under(self):
        artist, album = resolve_artist_album(
            user_artist="Bonobo", ytdlp_album="Black Sands"
        )
        path = get_output_path(
            "Kiara.flac",
            user_artist="Bonobo",
            ytdlp_album="Black Sands",
            download_path="/music",
        )

        assert (artist, album) == ("Bonobo", "Black Sands")
        assert path.parent == Path("/music") / artist / album

    def test_user_values_still_win_over_yt_dlp(self):
        assert resolve_artist_album(
            user_artist="Mine", user_album="Ours", ytdlp_artist="Theirs",
            ytdlp_album="Yours",
        ) == ("Mine", "Ours")

    def test_missing_values_fall_back(self):
        assert resolve_artist_album() == (FALLBACK_ARTIST, NO_ALBUM)

    def test_names_are_sanitised_the_same_way_the_folders_are(self):
        artist, album = resolve_artist_album(user_artist="AC/DC", user_album="..")

        assert "/" not in artist
        assert album == NO_ALBUM
