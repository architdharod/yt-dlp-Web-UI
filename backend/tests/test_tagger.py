"""Tests for the MusicBrainz tag fix.

Nothing here touches the network: :func:`app.tagger.fix_track` takes its search
function as an argument, so every test hands it a stub -- a list of candidates,
or an exception to raise.  The FLACs are built from
:func:`tests.conftest.minimal_flac_bytes`, which can be asked for a specific
duration because the match bar compares one.
"""

from pathlib import Path

import musicbrainzngs
import pytest

from app import tagger
from app.tagger import (
    Candidate,
    Match,
    NOTE_CANCELLED,
    NOTE_FILE_MISSING,
    NOTE_NO_MATCH,
    NOTE_NOT_FLAC,
    NOTE_UNAVAILABLE,
    apply_fix,
    clean_title,
    fix_track,
    normalise,
    pick_match,
)
from mutagen.flac import FLAC, Picture

from tests.conftest import TINY_JPEG, minimal_flac_bytes

DURATION = 183.0


def _write_track(
    path: Path,
    *,
    duration: float = DURATION,
    tags: dict[str, str] | None = None,
    picture: bool = False,
) -> Path:
    """Write a FLAC of *duration* seconds carrying *tags* (and maybe art)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(minimal_flac_bytes(int(44100 * duration)))
    audio = FLAC(path)
    for key, value in (tags or {}).items():
        audio[key] = value
    if picture:
        block = Picture()
        block.type = 3
        block.mime = "image/jpeg"
        block.data = TINY_JPEG
        audio.add_picture(block)
    audio.save()
    return path


def _candidate(
    title: str = "Kerala",
    credit: str = "Bonobo",
    names: tuple[str, ...] = ("Bonobo",),
    seconds: float = DURATION,
) -> Candidate:
    return Candidate(
        title=title,
        artist_credit=credit,
        artist_names=names,
        length_ms=int(seconds * 1000),
    )


# ===========================================================================
# clean_title
# ===========================================================================


class TestCleanTitle:
    """The cleaner strips what YouTube adds and nothing that identifies a
    recording."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Kerala (Official Video)", "Kerala"),
            ("Kerala [Official Music Video]", "Kerala"),
            ("Kerala (Official Audio)", "Kerala"),
            ("Kerala (Audio)", "Kerala"),
            ("Kerala [Lyrics]", "Kerala"),
            ("Kerala (Lyric Video)", "Kerala"),
            ("Kerala (Visualizer)", "Kerala"),
            ("Kerala [HD]", "Kerala"),
            ("Kerala (Official Video) [4K]", "Kerala"),
            ("Kerala - Topic", "Kerala"),
            ("Kerala | Ninja Tune", "Kerala"),
            ('"Kerala"', "Kerala"),
            ("“Kerala”", "Kerala"),
            ("Kerala (feat. Andreya Triana)", "Kerala"),
            ("Kerala ft. Andreya Triana", "Kerala"),
            ("Kerala featuring Andreya Triana", "Kerala"),
            ("Bonobo - Kerala", "Kerala"),
            ("  Kerala   (Official   Video)  ", "Kerala"),
        ],
    )
    def test_noise_is_stripped(self, raw, expected):
        assert clean_title(raw) == expected

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Kerala [Official Video HD]", "Kerala"),
            ("Kerala (Official Music Video 4K)", "Kerala"),
            ("Kerala (Official Audio HQ)", "Kerala"),
            ("Kerala (Official Video 1080p)", "Kerala"),
            ("Kerala (HD, Official Video)", "Kerala"),
        ],
    )
    def test_a_bracket_of_several_noise_phrases_goes_whole(self, raw, expected):
        """"Official Video HD" is two keywords back to back, not one, and the
        keyword list has to be read as a sequence for the group to drop."""
        assert clean_title(raw) == expected

    @pytest.mark.parametrize(
        "raw, expected",
        [
            # "ft"/"feat" inside a word: the marker needs a word boundary or
            # "Lift Off" cleans down to "Li".
            ("Lift Off", "Lift Off"),
            ("Left Behind", "Left Behind"),
            ("Feature Presentation", "Feature Presentation"),
            ("Soft Cell - Tainted Love", "Tainted Love"),
            # A bare "with" is part of far more titles than it is a credit.
            ("Dancing With Myself", "Dancing With Myself"),
            ("Gone with the Wind", "Gone with the Wind"),
            ("Stuck with U", "Stuck with U"),
            ("With or Without You", "With or Without You"),
        ],
    )
    def test_a_title_is_not_truncated_by_a_false_featuring_marker(
        self, raw, expected
    ):
        assert clean_title(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "Somebody That I Used To Know (with Kimbra)",
            "Somebody That I Used To Know [with Kimbra]",
        ],
    )
    def test_a_bracketed_with_is_still_a_featuring_credit(self, raw):
        """Bracketed, "with" is a credit -- MusicBrainz keeps it in the artist
        credit, so it must come out of the title."""
        assert clean_title(raw) == "Somebody That I Used To Know"

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Kerala (feat. Andreya Triana) (Live at Wembley)", "Kerala (Live at Wembley)"),
            ("Kerala [feat. Andreya Triana] [Remastered]", "Kerala [Remastered]"),
            ("Kerala (with Kimbra) (Live)", "Kerala (Live)"),
            ("Kerala (feat. A) (feat. B)", "Kerala"),
        ],
    )
    def test_a_bracketed_credit_takes_only_its_own_group(self, raw, expected):
        """The credit goes, the qualifier stays: "(Live at Wembley)" names a
        different recording, and eating it would match the studio original."""
        assert clean_title(raw) == expected

    def test_a_bare_credit_still_runs_to_the_end(self):
        """Documented: unbracketed, there is no closing bracket to say where
        the credit stops, so everything after it goes -- qualifier included."""
        assert clean_title("Kerala feat. Andreya Triana (Live at Wembley)") == "Kerala"

    def test_a_nested_credit_leaves_no_stray_bracket(self):
        """_FEAT stops at the first closing bracket, so the outer group's
        closer is orphaned; the final pass drops closers nothing opened."""
        assert clean_title("Kerala (feat. Andreya (Bad))") == "Kerala"

    @pytest.mark.parametrize(
        "raw, expected",
        [
            # Abbreviated markers need their dot, or a title loses its tail.
            ("A Great Feat In History", "A Great Feat In History"),
            # Documented cost of that rule: a dotless credit survives, which
            # is only a lookup that finds nothing.
            ("Kerala feat Andreya Triana", "Kerala feat Andreya Triana"),
            # Dotted and spelled-out markers still go.
            ("Kerala feat. Andreya Triana", "Kerala"),
            ("Kerala ft. Andreya Triana", "Kerala"),
            ("Kerala featuring Andreya Triana", "Kerala"),
        ],
    )
    def test_an_abbreviated_featuring_marker_needs_its_dot(self, raw, expected):
        assert clean_title(raw) == expected

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Kerala ((Official Video))", "Kerala"),
            ("Kerala [[HD]]", "Kerala"),
            ("Kerala ()", "Kerala"),
            ("Kerala (Live) ()", "Kerala (Live)"),
        ],
    )
    def test_nested_and_empty_brackets_go(self, raw, expected):
        """Dropping the inner group leaves an empty one, and an empty group
        holds nothing that identifies a recording."""
        assert clean_title(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "Kerala (Live at Glastonbury)",
            "Kerala (Acoustic Version)",
            "Kerala (Radio Edit)",
            "Kerala (Bonobo Remix)",
            # "video" is a keyword, "games" is not, so no cut covers the group.
            "Kerala (Video Games)",
            "Kerala (Official Video Live)",
            "(I Can't Get No) Satisfaction",
        ],
    )
    def test_brackets_that_name_a_recording_survive(self, raw):
        """These are different recordings, not noise: dropping the bracket
        would match the studio original instead."""
        assert clean_title(raw) == raw

    def test_the_artist_prefix_goes_when_it_is_the_folder_artist(self):
        assert clean_title("Bonobo - Kerala", "Bonobo") == "Kerala"

    def test_a_prefix_that_is_not_the_artist_stays(self):
        """"Careless - Whisper" is not "Whisper" by anybody."""
        assert clean_title("Careless - Whisper", "Wham!") == "Careless - Whisper"

    def test_the_artist_prefix_is_matched_loosely(self):
        assert clean_title("BONOBO - Kerala", "Bonobo") == "Kerala"

    def test_two_separators_are_left_alone_without_a_folder_artist(self):
        assert (
            clean_title("Bonobo - Kerala - Live") == "Bonobo - Kerala - Live"
        )

    def test_a_title_that_cleans_to_nothing_keeps_its_original(self):
        assert clean_title("(Official Video)") == "(Official Video)"

    def test_empty_stays_empty(self):
        assert clean_title("") == ""


# ===========================================================================
# normalise
# ===========================================================================


class TestNormalise:
    @pytest.mark.parametrize(
        "left, right",
        [
            ("Beyoncé", "Beyonce"),
            ("Don’t Stop", "Don't stop"),
            ("Simon & Garfunkel", "Simon and Garfunkel"),
            ("  Kerala  ", "kerala"),
            ("Sigur Rós", "SIGUR ROS"),
            ("Mother-Daughter", "Mother Daughter"),
        ],
    )
    def test_equal_after_normalising(self, left, right):
        assert normalise(left) == normalise(right)

    def test_different_titles_stay_different(self):
        assert normalise("Kerala") != normalise("Keralas")

    def test_none_and_empty(self):
        assert normalise(None) == ""
        assert normalise("") == ""


# ===========================================================================
# pick_match
# ===========================================================================


class TestPickMatch:
    """All three conditions are hard; each of them is tested at its edge."""

    def test_a_clean_match_is_returned(self):
        match = pick_match([_candidate()], "Kerala", "Bonobo", DURATION)
        assert match == Match(title="Kerala", artist="Bonobo")

    @pytest.mark.parametrize("delta", [0.0, 4.9, -4.9])
    def test_inside_the_five_second_window(self, delta):
        candidate = _candidate(seconds=DURATION + delta)
        assert pick_match([candidate], "Kerala", "Bonobo", DURATION) is not None

    @pytest.mark.parametrize("delta", [5.1, -5.1, 30.0])
    def test_outside_the_five_second_window(self, delta):
        candidate = _candidate(seconds=DURATION + delta)
        assert pick_match([candidate], "Kerala", "Bonobo", DURATION) is None

    def test_a_candidate_of_unknown_length_never_matches(self):
        candidate = Candidate(title="Kerala", artist_credit="Bonobo", length_ms=None)
        assert pick_match([candidate], "Kerala", "Bonobo", DURATION) is None

    def test_a_file_of_unknown_duration_never_matches(self):
        assert pick_match([_candidate()], "Kerala", "Bonobo", None) is None

    def test_a_different_title_does_not_match(self):
        candidate = _candidate(title="Kiara")
        assert pick_match([candidate], "Kerala", "Bonobo", DURATION) is None

    def test_the_title_comparison_is_normalised(self):
        candidate = _candidate(title="Kérala")
        assert pick_match([candidate], "kerala", "Bonobo", DURATION) is not None

    def test_a_different_artist_does_not_match(self):
        candidate = _candidate(credit="Four Tet", names=("Four Tet",))
        assert pick_match([candidate], "Kerala", "Bonobo", DURATION) is None

    def test_a_featuring_credit_matches_the_folder_artist(self):
        """The folder says "Bonobo"; MusicBrainz says "Bonobo feat. Andreya
        Triana".  The credit phrase does not match, one credited artist does,
        and the ARTIST written is the whole phrase."""
        candidate = _candidate(
            credit="Bonobo feat. Andreya Triana",
            names=("Bonobo", "Andreya Triana"),
        )
        match = pick_match([candidate], "Kerala", "Bonobo", DURATION)
        assert match == Match(
            title="Kerala", artist="Bonobo feat. Andreya Triana"
        )

    def test_the_first_candidate_over_the_bar_wins(self):
        candidates = [
            _candidate(title="Kiara"),  # wrong title
            _candidate(seconds=DURATION + 60),  # wrong length
            _candidate(credit="Bonobo", names=("Bonobo",)),
        ]
        assert pick_match(candidates, "Kerala", "Bonobo", DURATION) is not None

    def test_no_folder_artist_means_no_match(self):
        assert pick_match([_candidate()], "Kerala", None, DURATION) is None

    def test_no_candidates(self):
        assert pick_match([], "Kerala", "Bonobo", DURATION) is None


# ===========================================================================
# apply_fix
# ===========================================================================


class TestApplyFix:
    def test_it_writes_title_and_artist(self, tmp_path):
        path = _write_track(
            tmp_path / "track.flac",
            tags={"TITLE": "Kerala (Official Video)", "ARTIST": "BonoboOfficial"},
        )

        assert apply_fix(path, Match("Kerala", "Bonobo feat. Andreya Triana"))

        audio = FLAC(path)
        assert audio["TITLE"] == ["Kerala"]
        assert audio["ARTIST"] == ["Bonobo feat. Andreya Triana"]

    def test_it_preserves_everything_the_folders_decide(self, tmp_path):
        path = _write_track(
            tmp_path / "track.flac",
            tags={
                "TITLE": "Kerala (Official Video)",
                "ARTIST": "BonoboOfficial",
                "ALBUMARTIST": "Bonobo",
                "ALBUM": "Migration",
                "SOURCEID": "youtube:abc123",
                "SOURCEURL": "https://www.youtube.com/watch?v=abc123",
                "DATE": "2017",
                "TRACKNUMBER": "3",
            },
            picture=True,
        )

        apply_fix(path, Match("Kerala", "Bonobo"))

        audio = FLAC(path)
        assert audio["ALBUMARTIST"] == ["Bonobo"]
        assert audio["ALBUM"] == ["Migration"]
        assert audio["SOURCEID"] == ["youtube:abc123"]
        assert audio["SOURCEURL"] == ["https://www.youtube.com/watch?v=abc123"]
        assert audio["DATE"] == ["2017"]
        assert audio["TRACKNUMBER"] == ["3"]
        assert len(audio.pictures) == 1
        assert audio.pictures[0].data == TINY_JPEG

    def test_it_writes_no_musicbrainz_ids(self, tmp_path):
        path = _write_track(tmp_path / "track.flac", tags={"TITLE": "Kerala"})

        apply_fix(path, Match("Kerala", "Bonobo"))

        assert not [key for key in FLAC(path).keys() if "musicbrainz" in key.lower()]

    def test_it_strips_the_yt_dlp_junk(self, tmp_path):
        junk = {name: "noise" for name in tagger.JUNK_TAGS}
        path = _write_track(
            tmp_path / "track.flac", tags={"TITLE": "Kerala (Audio)", **junk}
        )

        assert apply_fix(path, Match("Kerala", "Bonobo"))

        remaining = {key.upper() for key in FLAC(path).keys()}
        assert not (remaining & tagger.JUNK_TAGS)

    def test_junk_is_matched_case_insensitively(self, tmp_path):
        path = _write_track(
            tmp_path / "track.flac",
            tags={"TITLE": "Kerala", "description": "a wall of text"},
        )

        assert apply_fix(path, Match("Kerala", "Bonobo"))
        assert "description" not in FLAC(path)

    def test_junk_alone_still_counts_as_a_change(self, tmp_path):
        path = _write_track(
            tmp_path / "track.flac",
            tags={"TITLE": "Kerala", "ARTIST": "Bonobo", "DESCRIPTION": "a wall"},
        )

        assert apply_fix(path, Match("Kerala", "Bonobo")) is True

    @pytest.mark.parametrize("tag", ["PURL", "COMMENT"])
    def test_the_provenance_fallbacks_survive(self, tmp_path, tag):
        """The domain model reads PURL (and COMMENT) as the source URL for
        files downloaded before SOURCEID existed; a fix over an imported file
        must not throw that away."""
        path = _write_track(
            tmp_path / "track.flac",
            tags={"TITLE": "Kerala", "ARTIST": "Bonobo", tag: "https://y.t/x"},
        )

        apply_fix(path, Match("Kerala", "Bonobo"))

        assert FLAC(path)[tag] == ["https://y.t/x"]

    def test_a_file_whose_only_extra_tag_is_purl_is_left_alone(self, tmp_path):
        path = _write_track(
            tmp_path / "track.flac",
            tags={"TITLE": "Kerala", "ARTIST": "Bonobo", "PURL": "https://y.t/x"},
        )
        before = path.read_bytes()

        assert apply_fix(path, Match("Kerala", "Bonobo")) is False
        assert path.read_bytes() == before

    def test_a_no_op_leaves_the_bytes_alone(self, tmp_path):
        path = _write_track(
            tmp_path / "track.flac", tags={"TITLE": "Kerala", "ARTIST": "Bonobo"}
        )
        before = path.read_bytes()

        assert apply_fix(path, Match("Kerala", "Bonobo")) is False
        assert path.read_bytes() == before


# ===========================================================================
# fix_track
# ===========================================================================


def _search(candidates):
    """A stub search that always answers with *candidates*."""

    def search(title, artist, duration):
        return list(candidates)

    return search


def _raising_search(error):
    def search(title, artist, duration):
        raise error

    return search


class TestFixTrack:
    def test_a_match_rewrites_the_file(self, tmp_path):
        path = _write_track(
            tmp_path / "Bonobo" / "Migration" / "track.flac",
            tags={
                "TITLE": "Bonobo - Kerala (Official Video)",
                "ARTIST": "BonoboOfficial",
                "ALBUM": "Migration",
                "ALBUMARTIST": "Bonobo",
                "DESCRIPTION": "Listen to the new album...",
            },
        )

        result = fix_track(path, "Bonobo", search=_search([_candidate()]))

        assert result.matched and result.changed and result.note is None
        audio = FLAC(path)
        assert audio["TITLE"] == ["Kerala"]
        assert audio["ARTIST"] == ["Bonobo"]
        assert "DESCRIPTION" not in audio
        assert audio["ALBUM"] == ["Migration"]

    def test_the_search_is_asked_for_the_cleaned_title(self, tmp_path):
        path = _write_track(
            tmp_path / "track.flac", tags={"TITLE": "Bonobo - Kerala (Official Video)"}
        )
        seen = []

        def search(title, artist, duration):
            seen.append((title, artist, duration))
            return []

        fix_track(path, "Bonobo", search=search)

        assert seen == [("Kerala", "Bonobo", DURATION)]

    def test_the_filename_stands_in_for_a_missing_title(self, tmp_path):
        path = _write_track(tmp_path / "Kerala (Official Video).flac")
        seen = []

        def search(title, artist, duration):
            seen.append(title)
            return []

        fix_track(path, "Bonobo", search=search)

        assert seen == ["Kerala"]

    def test_a_candidate_below_the_bar_changes_no_bytes(self, tmp_path):
        path = _write_track(
            tmp_path / "track.flac",
            tags={"TITLE": "Kerala (Official Video)", "ARTIST": "BonoboOfficial"},
        )
        before = path.read_bytes()

        result = fix_track(
            path, "Bonobo", search=_search([_candidate(seconds=DURATION + 40)])
        )

        assert result == tagger.TagFixResult(
            matched=False, changed=False, note=NOTE_NO_MATCH
        )
        assert path.read_bytes() == before

    def test_a_match_that_changes_nothing_reports_no_change(self, tmp_path):
        path = _write_track(
            tmp_path / "track.flac", tags={"TITLE": "Kerala", "ARTIST": "Bonobo"}
        )
        before = path.read_bytes()

        result = fix_track(path, "Bonobo", search=_search([_candidate()]))

        assert result.matched is True
        assert result.changed is False
        assert path.read_bytes() == before

    @pytest.mark.parametrize(
        "error",
        [
            musicbrainzngs.NetworkError("no route to host"),
            musicbrainzngs.ResponseError("bad xml"),
            musicbrainzngs.WebServiceError("503 rate limited"),
            OSError("timed out"),
            ValueError("unparseable"),
        ],
    )
    def test_musicbrainz_failures_become_a_note(self, tmp_path, error):
        path = _write_track(tmp_path / "track.flac", tags={"TITLE": "Kerala"})
        before = path.read_bytes()

        result = fix_track(path, "Bonobo", search=_raising_search(error))

        assert result.note == NOTE_UNAVAILABLE
        assert result.matched is False and result.changed is False
        assert path.read_bytes() == before

    def test_a_missing_file(self, tmp_path):
        result = fix_track(tmp_path / "gone.flac", "Bonobo", search=_search([]))
        assert result.note == NOTE_FILE_MISSING

    def test_only_flac_takes_part(self, tmp_path):
        path = tmp_path / "track.mp3"
        path.write_bytes(b"not a flac")

        result = fix_track(path, "Bonobo", search=_search([_candidate()]))

        assert result.note == NOTE_NOT_FLAC

    def test_an_unreadable_flac(self, tmp_path):
        path = tmp_path / "track.flac"
        path.write_bytes(b"not really a flac")

        result = fix_track(path, "Bonobo", search=_search([_candidate()]))

        assert result.note == tagger.NOTE_UNREADABLE

    def test_a_file_with_no_duration_never_reaches_the_search(self, tmp_path):
        path = _write_track(tmp_path / "track.flac", duration=0)
        called = []

        def search(title, artist, duration):
            called.append(title)
            return []

        result = fix_track(path, "Bonobo", search=search)

        assert result.note == NOTE_NO_MATCH
        assert called == []

    def test_cancel_before_the_lookup(self, tmp_path):
        path = _write_track(tmp_path / "track.flac", tags={"TITLE": "Kerala"})
        called = []

        def search(title, artist, duration):
            called.append(title)
            return [_candidate()]

        result = fix_track(
            path, "Bonobo", search=search, should_cancel=lambda: True
        )

        assert result.note == NOTE_CANCELLED
        assert called == []

    def test_a_cancel_wins_over_a_file_that_cannot_be_read(self, tmp_path):
        """The album pass checks before every lookup; so does this one now.

        Reporting "file could not be read" for a job the user cancelled would
        offer a Retry for a run nobody asked to finish.
        """
        path = tmp_path / "track.flac"
        path.write_bytes(b"not a flac at all")

        result = fix_track(path, "Bonobo", should_cancel=lambda: True)

        assert result.note == NOTE_CANCELLED

    def test_cancel_between_the_lookup_and_the_write(self, tmp_path):
        path = _write_track(
            tmp_path / "track.flac", tags={"TITLE": "Kerala (Official Video)"}
        )
        before = path.read_bytes()
        checkpoints = []

        def should_cancel() -> bool:
            checkpoints.append(len(checkpoints))
            # Three checkpoints: before the read, before the lookup, and after
            # it.  Only the last one says "cancelled".
            return len(checkpoints) > 2

        result = fix_track(
            path,
            "Bonobo",
            search=_search([_candidate()]),
            should_cancel=should_cancel,
        )

        assert result.matched is True
        assert result.changed is False
        assert result.note == NOTE_CANCELLED
        assert path.read_bytes() == before

    def test_the_lookup_can_be_switched_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TAG_FIX_ENABLED", "false")
        path = _write_track(tmp_path / "track.flac", tags={"TITLE": "Kerala"})
        called = []

        result = fix_track(
            path, "Bonobo", search=lambda *a: called.append(a) or []
        )

        assert result.note == tagger.NOTE_DISABLED
        assert called == []

    @pytest.mark.parametrize("value", ["", "true", "1", "yes", "on"])
    def test_the_switch_defaults_to_on(self, monkeypatch, value):
        monkeypatch.setenv("TAG_FIX_ENABLED", value)
        assert tagger.tag_fix_enabled() is True


# ===========================================================================
# MusicBrainz configuration
# ===========================================================================


class TestMusicBrainzConfiguration:
    """The User-Agent is mandatory and the rate limit is the service's rule,
    so both are asserted rather than assumed."""

    def test_it_sets_a_useragent_and_a_rate_limit(self, monkeypatch):
        monkeypatch.setattr(tagger, "_configured", False)
        monkeypatch.setenv("MUSICBRAINZ_CONTACT", "me@example.com")
        agents, limits = [], []
        monkeypatch.setattr(
            tagger.musicbrainzngs,
            "set_useragent",
            lambda *args: agents.append(args),
        )
        monkeypatch.setattr(
            tagger.musicbrainzngs,
            "set_rate_limit",
            lambda **kwargs: limits.append(kwargs),
        )

        tagger.configure_musicbrainz()
        tagger.configure_musicbrainz()  # once per process, not once per call

        assert agents == [("music-for-arr", tagger.MB_APP_VERSION, "me@example.com")]
        assert limits == [{"limit_or_interval": 1.0, "new_requests": 1}]

    def test_the_contact_falls_back_to_the_repository(self, monkeypatch):
        monkeypatch.delenv("MUSICBRAINZ_CONTACT", raising=False)
        assert tagger.musicbrainz_contact() == tagger.DEFAULT_MB_CONTACT

    def test_the_search_asks_for_recording_artist_and_duration(self, monkeypatch):
        monkeypatch.setattr(tagger, "_configured", True)
        seen = {}

        def fake_search(**kwargs):
            seen.update(kwargs)
            return {
                "recording-list": [
                    {
                        "title": "Kerala",
                        "length": "183000",
                        "artist-credit-phrase": "Bonobo",
                        "artist-credit": [{"artist": {"name": "Bonobo"}}],
                    },
                    {"no": "title"},
                ]
            }

        monkeypatch.setattr(
            tagger.musicbrainzngs, "search_recordings", fake_search
        )

        candidates = tagger.search_recordings("Kerala", "Bonobo", 183.0)

        assert seen == {
            "recording": "Kerala",
            "artist": "Bonobo",
            "dur": 183000,
            "limit": tagger.SEARCH_LIMIT,
        }
        assert candidates == [
            Candidate(
                title="Kerala",
                artist_credit="Bonobo",
                artist_names=("Bonobo",),
                length_ms=183000,
            )
        ]

    def test_a_credit_with_a_join_phrase_is_read(self, monkeypatch):
        monkeypatch.setattr(tagger, "_configured", True)
        monkeypatch.setattr(
            tagger.musicbrainzngs,
            "search_recordings",
            lambda **kwargs: {
                "recording-list": [
                    {
                        "title": "Kerala",
                        "length": 183000,
                        "artist-credit": [
                            {"artist": {"name": "Bonobo"}},
                            " feat. ",
                            {"artist": {"name": "Andreya Triana"}},
                        ],
                    }
                ]
            },
        )

        [candidate] = tagger.search_recordings("Kerala", "Bonobo", 183.0)

        assert candidate.artist_names == ("Bonobo", "Andreya Triana")
        assert candidate.artist_credit == "Bonobo, Andreya Triana"
