"""Tests for the album metadata pass.

Nothing here touches the network or a thread: every function in
``app.album_tagger`` takes its external dependency as an argument, and
:func:`app.album_tagger.tag_album` takes the "run this blocking step" hook the
queue normally supplies.  :func:`_run` below is that hook with the thread and
the timeout taken out, which is what lets the whole pass be driven from a test
with a plain ``await``.
"""

from pathlib import Path

import httpx
import musicbrainzngs
import pytest
from mutagen.flac import FLAC

from app import album_tagger
from app.album_tagger import (
    COVER_FILENAME,
    MAX_RELEASE_FETCHES,
    NOTE_NO_RELEASE,
    AlbumTagResult,
    ReleaseTrack,
    TagStepFailed,
    TrackLookup,
    album_tracks,
    apply_numbers,
    choose_release,
    fetch_cover,
    has_sidecar_cover,
    lookup_track,
    match_tracklist,
    partial_note,
    rank_release_candidates,
    release_tracks,
    store_cover,
    tag_album,
    track_numbers,
    write_cover,
)
from app.tagger import (
    NOTE_NOT_FLAC,
    NOTE_NO_MATCH,
    NOTE_UNAVAILABLE,
    Candidate,
    Match,
    ReleaseRef,
)

from tests.conftest import TINY_JPEG, TINY_PNG, minimal_flac_bytes

DURATION = 183.0
LENGTH_MS = int(DURATION * 1000)

# Captured before the suite-wide guard in conftest replaces it, so the fetch
# can still be exercised against an in-process transport.
REAL_HTTPX_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def no_real_downscale(monkeypatch):
    """Keep the cover downscaler out of the way of the fake ffmpeg.

    ``conftest``'s :class:`FakeFfmpeg` replaces ``subprocess.Popen`` for the
    whole process, and ``cover_art`` reaches ffmpeg through ``subprocess.run``,
    which the fake's ``communicate`` signature cannot serve.  The downscaler
    has tests of its own (``test_cover_art``); here it is the identity, and the
    one test that cares that it is *called* installs its own stub over this.
    """
    monkeypatch.setattr(album_tagger, "downscale_cover", lambda data, *a, **k: data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run(step):
    """The queue's step hook, minus the thread and the timeout."""
    return step()


def _write_track(path: Path, *, title: str, duration: float = DURATION, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(minimal_flac_bytes(int(44100 * duration)))
    audio = FLAC(path)
    audio["TITLE"] = title
    for key, value in tags.items():
        audio[key.upper()] = value
    audio.save()
    return path


def _release(
    release_id: str = "rel-1",
    *,
    track_count: int | None = 2,
    status: str = "Official",
    artist_phrase: str = "",
    secondary: tuple[str, ...] = (),
) -> ReleaseRef:
    """An ordinary release: the plausibility filter has no reason to drop it.

    ``status`` is spelled out only so a test can say what it is: an absent
    status is unknown, not disqualifying, and a release that carries none is
    plausible.  What the filter drops is a status that is present and says
    something other than "Official" -- a bootleg or a pseudo-release.
    """
    return ReleaseRef(
        id=release_id,
        title="Migration",
        release_group_id=f"group-of-{release_id}",
        track_count=track_count,
        status=status,
        artist_credit_phrase=artist_phrase,
        secondary_types=secondary,
    )


def _candidate(title: str, *, recording_id: str, releases=(), artist="Bonobo"):
    return Candidate(
        title=title,
        artist_credit=artist,
        artist_names=(artist,),
        length_ms=LENGTH_MS,
        recording_id=recording_id,
        releases=tuple(releases),
    )


def _searcher(by_title: dict[str, Candidate | None]):
    """A search stub keyed on the cleaned title the pass asks about.

    A value may be one candidate, ``None`` for "no results", or a list of
    candidates in MusicBrainz's order -- which is what a real search returns
    for a recording MusicBrainz has duplicated across releases.
    """

    def search(title, artist, duration):
        candidate = by_title.get(title, "missing")
        if candidate == "missing":
            raise AssertionError(f"unexpected lookup for {title!r}")
        if candidate is None:
            return []
        if isinstance(candidate, list):
            return list(candidate)
        return [candidate]

    return search


def _release_payload(pairs: list[tuple[str, int]], disc: int = 1) -> dict:
    """A ``get_release_by_id`` response listing *pairs* of (recording id, pos)."""
    return {
        "release": {
            "id": "rel-1",
            "medium-list": [
                {
                    "position": str(disc),
                    "track-list": [
                        {
                            "id": f"track-{position}",
                            "position": str(position),
                            "number": str(position),
                            "recording": {"id": recording_id},
                        }
                        for recording_id, position in pairs
                    ],
                }
            ],
        }
    }


def _ranked_release(
    release_id: str,
    *,
    track_count: int | None = 2,
    status: str = "Official",
    artist_phrase: str = "",
    secondary: tuple[str, ...] = (),
) -> ReleaseRef:
    """A search result's release entry, with the fields the fallback filters on."""
    return ReleaseRef(
        id=release_id,
        title=f"Album {release_id}",
        release_group_id=f"group-of-{release_id}",
        track_count=track_count,
        artist_credit_phrase=artist_phrase,
        status=status,
        secondary_types=secondary,
    )


def _tracklist_payload(
    rows: list[tuple[str, int, str, int]],
    *,
    disc: int = 1,
    credit: str = "Bonobo",
    names: tuple[str, ...] | None = None,
) -> dict:
    """A ``get_release_by_id`` response with ``recordings`` + ``artist-credits``.

    *rows* are ``(recording id, position, title, length in ms)`` -- everything
    the release-first fallback checks a folder's tracks against.
    """
    credited = (credit,) if names is None else names
    return {
        "release": {
            "id": "rel-1",
            "medium-list": [
                {
                    "position": str(disc),
                    "track-list": [
                        {
                            "id": f"track-{position}",
                            "position": str(position),
                            "number": str(position),
                            "recording": {
                                "id": recording_id,
                                "title": title,
                                "length": str(length),
                                "artist-credit-phrase": credit,
                                "artist-credit": [
                                    {"artist": {"id": f"artist-{name}", "name": name}}
                                    for name in credited
                                ],
                            },
                        }
                        for recording_id, position, title, length in rows
                    ],
                }
            ],
        }
    }


def _two_track_album(root: Path) -> Path:
    folder = root / "Bonobo" / "Migration"
    _write_track(folder / "01 Migration.flac", title="Migration (Official Video)")
    _write_track(folder / "02 Kerala.flac", title="Kerala [Lyrics]")
    return folder


def _full_match_pass(folder: Path, **overrides):
    """Everything ``tag_album`` needs for a folder that matches one release."""
    release = _release()
    kwargs = {
        "run": _run,
        "search": _searcher(
            {
                "Migration": _candidate(
                    "Migration", recording_id="rec-1", releases=[release]
                ),
                "Kerala": _candidate(
                    "Kerala", recording_id="rec-2", releases=[release]
                ),
            }
        ),
        "fetch_release": lambda release_id: _release_payload(
            [("rec-1", 1), ("rec-2", 2)]
        ),
        "fetch_cover_art": lambda url: TINY_JPEG,
    }
    kwargs.update(overrides)
    return tag_album(folder, "Bonobo", **kwargs)


# ===========================================================================
# The folder
# ===========================================================================


class TestAlbumTracks:
    def test_it_finds_every_audio_file_including_nested_ones(self, tmp_path):
        _write_track(tmp_path / "b.flac", title="B")
        _write_track(tmp_path / "Disc 2" / "a.flac", title="A")
        (tmp_path / "cover.jpg").write_bytes(TINY_JPEG)
        (tmp_path / "notes.txt").write_text("hello")

        assert [path.name for path in album_tracks(tmp_path)] == ["a.flac", "b.flac"]

    def test_a_folder_with_no_audio_is_empty(self, tmp_path):
        (tmp_path / "cover.jpg").write_bytes(TINY_JPEG)

        assert album_tracks(tmp_path) == []


# ===========================================================================
# One track
# ===========================================================================


class TestLookupTrack:
    def test_a_match_carries_the_whole_candidate(self, tmp_path):
        path = _write_track(tmp_path / "track.flac", title="Kerala (Official Video)")
        candidate = _candidate("Kerala", recording_id="rec-2", releases=[_release()])

        lookup = lookup_track(path, "Bonobo", search=_searcher({"Kerala": candidate}))

        assert lookup.matched
        assert lookup.candidate.recording_id == "rec-2"
        assert lookup.match == Match(title="Kerala", artist="Bonobo")

    def test_it_keeps_every_candidate_that_cleared_the_bar(self, tmp_path):
        """MusicBrainz duplicates recordings; the folder may need a later one."""
        path = _write_track(tmp_path / "track.flac", title="Kerala")
        duplicate = _candidate(
            "Kerala", recording_id="rec-dup", releases=[_release("bootleg")]
        )
        canonical = _candidate(
            "Kerala", recording_id="rec-2", releases=[_release("rel-1")]
        )
        wrong_title = _candidate(
            "Keralaa", recording_id="rec-x", releases=[_release("rel-1")]
        )

        lookup = lookup_track(
            path,
            "Bonobo",
            search=_searcher({"Kerala": [duplicate, wrong_title, canonical]}),
        )

        assert [one.recording_id for one in lookup.candidates] == ["rec-dup", "rec-2"]
        # `candidate` is still the first: what a per-track fix would pick.
        assert lookup.candidate is duplicate

    def test_a_miss_says_no_match_and_writes_nothing(self, tmp_path):
        path = _write_track(tmp_path / "track.flac", title="Kerala")
        before = path.read_bytes()

        lookup = lookup_track(path, "Bonobo", search=_searcher({"Kerala": None}))

        assert not lookup.matched and lookup.note == NOTE_NO_MATCH
        assert path.read_bytes() == before

    def test_a_non_flac_never_takes_part(self, tmp_path):
        path = tmp_path / "track.mp3"
        path.write_bytes(b"not really an mp3")

        lookup = lookup_track(path, "Bonobo", search=_searcher({}))

        assert lookup.note == NOTE_NOT_FLAC

    def test_musicbrainz_being_down_is_told_apart_from_a_miss(self, tmp_path):
        path = _write_track(tmp_path / "track.flac", title="Kerala")

        def search(*args):
            raise musicbrainzngs.NetworkError("down")

        lookup = lookup_track(path, "Bonobo", search=search)

        assert lookup.note == NOTE_UNAVAILABLE


class TestApplyNumbers:
    def test_it_writes_the_two_numbers(self, tmp_path):
        path = _write_track(tmp_path / "track.flac", title="Kerala")

        assert apply_numbers(path, 3, 1) is True

        audio = FLAC(path)
        assert audio["TRACKNUMBER"] == ["3"] and audio["DISCNUMBER"] == ["1"]

    def test_writing_the_same_numbers_again_leaves_the_bytes_alone(self, tmp_path):
        path = _write_track(tmp_path / "track.flac", title="Kerala")
        apply_numbers(path, 3, 1)
        before = path.read_bytes()

        assert apply_numbers(path, 3, 1) is False
        assert path.read_bytes() == before

    def test_a_missing_number_is_not_written(self, tmp_path):
        path = _write_track(tmp_path / "track.flac", title="Kerala")

        apply_numbers(path, 3, None)

        assert "DISCNUMBER" not in FLAC(path)


# ===========================================================================
# The release
# ===========================================================================


class TestChooseRelease:
    def _lookup(self, releases):
        return album_tagger.TrackLookup(
            path=Path("x.flac"),
            candidates=(_candidate("x", recording_id="rec", releases=releases),),
        )

    def test_the_release_common_to_every_track_wins(self):
        shared = _release("shared", track_count=2)
        chosen = choose_release(
            [
                self._lookup([_release("only-mine"), shared]),
                self._lookup([shared, _release("only-theirs")]),
            ],
            2,
        )

        assert chosen is not None and chosen.id == "shared"

    def test_no_release_in_common_means_no_release(self):
        assert (
            choose_release(
                [self._lookup([_release("a")]), self._lookup([_release("b")])], 2
            )
            is None
        )

    def test_the_release_whose_track_count_matches_the_folder_wins(self):
        # The compilation is a compilation by its release group, not merely by
        # being long: a folder of twelve tracks whose hits disc happened to
        # have twelve on it must lose the same way.
        compilation = _release(
            "compilation", track_count=90, secondary=("Compilation",)
        )
        album = _release("album", track_count=12)
        chosen = choose_release(
            [self._lookup([compilation, album]), self._lookup([compilation, album])], 12
        )

        assert chosen.id == "album"

    def test_musicbrainz_order_is_the_tie_break(self):
        first = _release("first", track_count=None)
        second = _release("second", track_count=None)
        chosen = choose_release(
            [self._lookup([first, second]), self._lookup([second, first])], 2
        )

        assert chosen.id == "first"

    def test_every_clearing_candidate_contributes_its_releases(self):
        """The best-scoring entity is often a duplicate on one odd release."""
        shared = _release("shared", track_count=2)
        duplicate = _candidate(
            "x", recording_id="rec-dup", releases=[_release("bootleg")]
        )
        canonical = _candidate("x", recording_id="rec-1", releases=[shared])
        lopsided = album_tagger.TrackLookup(
            path=Path("a.flac"), candidates=(duplicate, canonical)
        )

        chosen = choose_release([lopsided, self._lookup([shared])], 2)

        assert chosen is not None and chosen.id == "shared"

    def test_the_ordered_union_is_de_duplicated(self):
        """One release reached through two candidates is still one release."""
        shared = _release("shared", track_count=None)
        other = _release("other", track_count=None)
        both = album_tagger.TrackLookup(
            path=Path("a.flac"),
            candidates=(
                _candidate("x", recording_id="rec-a", releases=[shared, other]),
                _candidate("x", recording_id="rec-b", releases=[shared]),
            ),
        )

        chosen = choose_release([both, self._lookup([other, shared])], 2)

        # "shared" is first in the first candidate's own order and is not
        # re-considered when the second candidate names it again.
        assert chosen is not None and chosen.id == "shared"

    def test_nothing_matched_means_no_release(self):
        assert choose_release([], 3) is None

    def test_a_pseudo_release_common_to_every_track_does_not_win(self):
        """The intersection's favourite failure: a placeholder edition that
        carries every recording of the album and is therefore on every track's
        page.  ``None`` sends the folder to the release-first fallback."""
        pseudo = _release("pseudo", track_count=2, status="Pseudo-Release")

        assert choose_release([self._lookup([pseudo]), self._lookup([pseudo])], 2) is None

    def test_a_compilation_common_to_every_track_does_not_win(self):
        hits = _release("hits", track_count=2, secondary=("Compilation",))

        assert choose_release([self._lookup([hits]), self._lookup([hits])], 2) is None

    def test_a_release_credited_to_another_artist_does_not_win(self):
        """A tribute band's re-recording is credited on the release itself."""
        tribute = _release("tribute", track_count=2, artist_phrase="Tribute Band")
        real = _release("real", track_count=2)
        lookups = [
            self._lookup([tribute, real]),
            self._lookup([tribute, real]),
        ]

        chosen = choose_release(lookups, 2, "Bonobo")

        assert chosen is not None and chosen.id == "real"

    def test_the_only_common_release_being_implausible_means_no_release(self):
        tribute = _release("tribute", track_count=2, artist_phrase="Tribute Band")

        assert (
            choose_release([self._lookup([tribute]), self._lookup([tribute])], 2, "Bonobo")
            is None
        )


class TestTrackNumbers:
    """The reduction the album pass numbers from, over a fetched tracklist."""

    def _numbers(self, payload: dict) -> dict:
        return track_numbers(release_tracks("rel-1", fetch=lambda _: payload))

    def test_it_maps_recording_ids_to_track_and_disc(self):
        numbers = self._numbers(
            _release_payload([("rec-1", 1), ("rec-2", 2)], disc=2)
        )

        assert numbers == {"rec-1": (1, 2), "rec-2": (2, 2)}

    def test_musicbrainz_being_down_fails_the_step(self):
        def fetch(release_id):
            raise musicbrainzngs.NetworkError("down")

        with pytest.raises(TagStepFailed) as caught:
            release_tracks("rel-1", fetch=fetch)

        assert caught.value.note == NOTE_UNAVAILABLE

    def test_a_recording_on_two_media_keeps_the_first(self):
        """A reprise on the bonus disc must not renumber the album track."""
        payload = {
            "release": {
                "id": "rel-1",
                "medium-list": [
                    {
                        "position": "1",
                        "track-list": [
                            {"position": "4", "recording": {"id": "rec-1"}}
                        ],
                    },
                    {
                        "position": "2",
                        "track-list": [
                            {"position": "9", "recording": {"id": "rec-1"}}
                        ],
                    },
                ],
            }
        }

        assert self._numbers(payload) == {"rec-1": (4, 1)}

    def test_a_malformed_response_is_survived(self):
        assert self._numbers({}) == {}


class TestReleaseTracks:
    """The one fetch the fallback verifies *and* numbers from."""

    def test_it_reads_title_length_and_credit_off_each_row(self):
        rows = release_tracks(
            "rel-1",
            fetch=lambda _: _tracklist_payload(
                [("rec-1", 1, "Migration", LENGTH_MS)],
                credit="Bonobo feat. Andreya Triana",
                names=("Bonobo", "Andreya Triana"),
            ),
        )

        assert rows == [
            ReleaseTrack(
                recording_id="rec-1",
                title="Migration",
                length_ms=LENGTH_MS,
                artist_credit="Bonobo feat. Andreya Triana",
                artist_names=("Bonobo", "Andreya Triana"),
                track_number=1,
                disc_number=1,
            )
        ]

    def test_a_track_level_credit_overrides_the_recordings(self):
        payload = _tracklist_payload([("rec-1", 1, "Migration", LENGTH_MS)])
        track = payload["release"]["medium-list"][0]["track-list"][0]
        track["artist-credit"] = [{"artist": {"id": "va", "name": "Someone Else"}}]

        (row,) = release_tracks("rel-1", fetch=lambda _: payload)

        assert row.artist_names == ("Someone Else",)


class TestRankReleaseCandidates:
    """The free half of the fallback: which releases are worth a request."""

    def _lookup(self, path_name: str, releases) -> TrackLookup:
        return TrackLookup(
            path=Path(path_name),
            candidates=(
                _candidate("Migration", recording_id="rec-1", releases=releases),
            ),
            cleaned_title="Migration",
            duration=DURATION,
        )

    def test_a_tribute_a_bootleg_and_a_compilation_are_all_dropped(self):
        good = _ranked_release("rel-good")
        lookup = self._lookup(
            "a.flac",
            [
                good,
                _ranked_release("rel-tribute", artist_phrase="Tribute Band"),
                _ranked_release("rel-bootleg", status="Bootleg"),
                _ranked_release("rel-pseudo", status="Pseudo-Release"),
                _ranked_release("rel-hits", secondary=("Compilation",)),
            ],
        )

        assert rank_release_candidates([lookup], "Bonobo", 2) == [good]

    def test_a_release_with_no_status_at_all_is_kept(self):
        """MusicBrainz omits the field on plenty of ordinary releases, and a
        silent release is unknown rather than a bootleg."""
        ref = _ranked_release("rel-1", status="")

        assert rank_release_candidates([self._lookup("a.flac", [ref])], "Bonobo", 2) == [
            ref
        ]

    @pytest.mark.parametrize("status", ["Bootleg", "Pseudo-Release"])
    def test_a_status_that_is_not_official_is_still_dropped(self, status):
        ref = _ranked_release("rel-1", status=status)

        assert rank_release_candidates([self._lookup("a.flac", [ref])], "Bonobo", 2) == []

    def test_an_absent_credit_phrase_counts_as_the_folders_artist(self):
        """MusicBrainz omits the release credit when it equals the recording's."""
        ref = _ranked_release("rel-1", artist_phrase="")

        assert rank_release_candidates([self._lookup("a.flac", [ref])], "Bonobo", 2) == [
            ref
        ]

    def test_a_secondary_type_that_is_not_compilation_is_kept(self):
        ref = _ranked_release("rel-1", secondary=("Soundtrack",))

        assert rank_release_candidates([self._lookup("a.flac", [ref])], "Bonobo", 2) == [
            ref
        ]

    def test_the_folders_track_count_outranks_the_vote(self):
        """The proven false positive: a hits disc every track names wins on
        votes, and would take an album only one track named."""
        album = _ranked_release("rel-album", track_count=2)
        hits = _ranked_release("rel-hits", track_count=40)
        lookups = [
            self._lookup("a.flac", [album, hits]),
            self._lookup("b.flac", [hits]),
        ]

        ranked = rank_release_candidates(lookups, "Bonobo", 2)

        assert [ref.id for ref in ranked] == ["rel-album", "rel-hits"]

    def test_votes_break_the_tie_between_equally_sized_releases(self):
        popular = _ranked_release("rel-popular")
        lonely = _ranked_release("rel-lonely")
        lookups = [
            self._lookup("a.flac", [lonely, popular]),
            self._lookup("b.flac", [popular]),
        ]

        assert [ref.id for ref in rank_release_candidates(lookups, "Bonobo", 2)] == [
            "rel-popular",
            "rel-lonely",
        ]

    def test_nothing_matched_ranks_nothing(self):
        assert rank_release_candidates([], "Bonobo", 2) == []


class TestMatchTracklist:
    """The paid half: does this release's tracklist really hold the folder?"""

    def _lookup(self, name: str, title: str, duration: float = DURATION) -> TrackLookup:
        return TrackLookup(
            path=Path(name),
            candidates=(_candidate(title, recording_id="rec-x"),),
            cleaned_title=title,
            duration=duration,
        )

    def _rows(self, **kwargs) -> list:
        return release_tracks(
            "rel-1",
            fetch=lambda _: _tracklist_payload(
                [("rec-1", 1, "Migration", LENGTH_MS), ("rec-2", 2, "Kerala", LENGTH_MS)],
                **kwargs,
            ),
        )

    def test_every_track_finds_its_row(self):
        rows = self._rows()

        assignment = match_tracklist(
            rows,
            [self._lookup("a.flac", "Migration"), self._lookup("b.flac", "Kerala")],
            "Bonobo",
        )

        assert assignment is not None
        assert assignment[Path("a.flac")].track_number == 1
        assert assignment[Path("b.flac")].track_number == 2

    def test_one_track_the_release_does_not_hold_gives_up(self):
        assignment = match_tracklist(
            self._rows(),
            [self._lookup("a.flac", "Migration"), self._lookup("b.flac", "Bambro Koyo")],
            "Bonobo",
        )

        assert assignment is None

    def test_a_length_outside_the_window_is_not_that_row(self):
        assignment = match_tracklist(
            self._rows(),
            [self._lookup("a.flac", "Migration", duration=DURATION + 30)],
            "Bonobo",
        )

        assert assignment is None

    def test_a_featuring_credit_is_accepted_through_the_named_artist(self):
        rows = self._rows(
            credit="Bonobo feat. Andreya Triana",
            names=("Bonobo", "Andreya Triana"),
        )

        assignment = match_tracklist(rows, [self._lookup("a.flac", "Migration")], "Bonobo")

        assert assignment is not None
        assert assignment[Path("a.flac")].artist_credit == "Bonobo feat. Andreya Triana"

    def test_another_artists_tracklist_is_refused(self):
        rows = self._rows(credit="Tribute Band", names=("Tribute Band",))

        assert match_tracklist(rows, [self._lookup("a.flac", "Migration")], "Bonobo") is None

    def test_a_repeated_recording_does_not_number_two_files_the_same(self):
        rows = release_tracks(
            "rel-1",
            fetch=lambda _: _tracklist_payload(
                [("rec-1", 1, "Migration", LENGTH_MS), ("rec-1", 7, "Migration", LENGTH_MS)]
            ),
        )

        assignment = match_tracklist(
            rows,
            [self._lookup("a.flac", "Migration"), self._lookup("b.flac", "Migration")],
            "Bonobo",
        )

        assert assignment is not None
        assert {row.track_number for row in assignment.values()} == {1, 7}

    def _pinned_lookup(
        self, name: str, title: str, *, recording_id: str, duration: float = DURATION
    ) -> TrackLookup:
        """A lookup whose own search result names one recording by id."""
        return TrackLookup(
            path=Path(name),
            candidates=(_candidate(title, recording_id=recording_id),),
            cleaned_title=title,
            duration=duration,
        )

    def _two_discs(self) -> list:
        """The same track on disc 1 (#3) and again on disc 2 (#7).

        Identical titles and lengths within the window, so the title-and-length
        bar cannot tell the two rows apart: only the recording id can.
        """
        first = _tracklist_payload([("rec-a", 3, "Kerala", LENGTH_MS)], disc=1)
        second = _tracklist_payload([("rec-b", 7, "Kerala", LENGTH_MS)], disc=2)
        first["release"]["medium-list"].extend(second["release"]["medium-list"])
        return release_tracks("rel-1", fetch=lambda _: first)

    @pytest.mark.parametrize("swapped", [False, True])
    def test_a_recording_id_pins_its_file_to_the_right_row(self, swapped):
        """Both rows clear the bar for both files; the search says which is
        which, and the answer must not depend on the folder's order."""
        rows = self._two_discs()
        pinned = self._pinned_lookup("b.flac", "Kerala", recording_id="rec-b")
        other = self._pinned_lookup("a.flac", "Kerala", recording_id="rec-a")
        lookups = [pinned, other] if swapped else [other, pinned]

        assignment = match_tracklist(rows, lookups, "Bonobo")

        assert assignment is not None
        assert assignment[Path("b.flac")].track_number == 7
        assert assignment[Path("b.flac")].disc_number == 2
        assert assignment[Path("a.flac")].track_number == 3

    def test_two_files_naming_the_same_recording_still_pair_off(self):
        """A contested claim narrows nothing: both files name ``rec-b``, and
        refusing to let one take the other row would give up a release that
        pairs off perfectly."""
        rows = self._two_discs()
        lookups = [
            self._pinned_lookup("a.flac", "Kerala", recording_id="rec-b"),
            self._pinned_lookup("b.flac", "Kerala", recording_id="rec-b"),
        ]

        assignment = match_tracklist(rows, lookups, "Bonobo")

        assert assignment is not None
        assert {row.track_number for row in assignment.values()} == {3, 7}

    def _two_intros(self) -> list:
        """Two rows of the same title 4s apart -- an album with a reprise."""
        return release_tracks(
            "rel-1",
            fetch=lambda _: _tracklist_payload(
                [("rec-1", 1, "Intro", 200_000), ("rec-2", 7, "Intro", 204_000)]
            ),
        )

    @pytest.mark.parametrize("order", [("a.flac", "b.flac"), ("b.flac", "a.flac")])
    def test_a_track_that_fits_one_row_only_gets_it_whichever_order(self, order):
        """`a` clears both rows, `b` only the first.  Taking each track's first
        free row in folder order gives `b` a row `a` already has; pairing them
        off gives every file a row of its own either way round."""
        durations = {"a.flac": 202.0, "b.flac": 198.0}
        lookups = [
            self._lookup(name, "Intro", duration=durations[name]) for name in order
        ]

        assignment = match_tracklist(self._two_intros(), lookups, "Bonobo")

        assert assignment is not None
        assert assignment[Path("a.flac")].track_number == 7
        assert assignment[Path("b.flac")].track_number == 1

    def test_two_files_that_fit_the_same_single_row_is_no_match(self):
        """No pairing exists at all: the release is not this folder."""
        lookups = [
            self._lookup("a.flac", "Intro", duration=198.0),
            self._lookup("b.flac", "Intro", duration=197.0),
        ]

        assert match_tracklist(self._two_intros(), lookups, "Bonobo") is None


# ===========================================================================
# Cover art
# ===========================================================================


class TestCoverArt:
    def test_the_release_front_is_tried_first(self):
        asked: list[str] = []

        def fetch(url):
            asked.append(url)
            return TINY_JPEG

        assert fetch_cover(_release(), fetch=fetch) == TINY_JPEG
        assert asked == ["https://coverartarchive.org/release/rel-1/front-500"]

    def test_the_release_group_is_the_fallback(self):
        asked: list[str] = []

        def fetch(url):
            asked.append(url)
            return None if "/release/" in url else TINY_PNG

        assert fetch_cover(_release(), fetch=fetch) == TINY_PNG
        assert asked[1] == (
            "https://coverartarchive.org/release-group/group-of-rel-1/front-500"
        )

    def test_something_that_is_not_an_image_is_refused(self):
        assert fetch_cover(_release(), fetch=lambda url: b"<html>nope</html>") is None

    def test_a_cover_is_downscaled_before_it_is_written(self, tmp_path, monkeypatch):
        seen: list[bytes] = []

        def fake_downscale(data, *args, **kwargs):
            seen.append(data)
            return b"\xff\xd8\xffsmaller"

        monkeypatch.setattr(album_tagger, "downscale_cover", fake_downscale)

        written, note = write_cover(tmp_path, TINY_JPEG)

        assert seen == [TINY_JPEG]
        assert note is None
        assert written.read_bytes() == b"\xff\xd8\xffsmaller"

    def test_a_png_the_downscaler_handed_back_is_written_as_cover_png(
        self, tmp_path, monkeypatch
    ):
        """ffmpeg missing means the original bytes come back; a PNG saved as
        ``cover.jpg`` is a file whose name lies about it."""
        monkeypatch.setattr(album_tagger, "downscale_cover", lambda data, *a, **k: data)

        written, note = write_cover(tmp_path, TINY_PNG)

        assert note is None
        assert written.name == "cover.png"
        assert written.read_bytes() == TINY_PNG
        assert not (tmp_path / COVER_FILENAME).exists()

    def test_a_webp_the_library_cannot_serve_is_not_written(
        self, tmp_path, monkeypatch
    ):
        folder = tmp_path / "album"
        folder.mkdir()
        webp = b"RIFF\x00\x00\x00\x00WEBPVP8 rest"
        monkeypatch.setattr(album_tagger, "downscale_cover", lambda data, *a, **k: data)

        written, note = write_cover(folder, webp)

        assert written is None
        assert note == "cover not fetched: it is not a JPEG or a PNG"
        # Not even a temporary file left behind.
        assert list(folder.iterdir()) == []

    def test_the_note_reaches_the_caller_through_store_cover(
        self, tmp_path, monkeypatch
    ):
        gif = b"GIF89a" + b"rest"
        monkeypatch.setattr(album_tagger, "downscale_cover", lambda data, *a, **k: data)

        written, note = store_cover(tmp_path, _release(), fetch=lambda url: gif)

        assert written is None
        assert note == "cover not fetched: it is not a JPEG or a PNG"

    def test_an_existing_cover_is_never_overwritten(self, tmp_path):
        (tmp_path / COVER_FILENAME).write_bytes(b"mine")

        assert write_cover(tmp_path, TINY_JPEG) == (None, None)
        assert (tmp_path / COVER_FILENAME).read_bytes() == b"mine"

    def test_any_sidecar_name_counts_as_art_already_there(self, tmp_path):
        (tmp_path / "folder.jpg").write_bytes(TINY_JPEG)

        assert has_sidecar_cover(tmp_path) is True
        written, note = store_cover(tmp_path, _release(), fetch=lambda url: TINY_JPEG)
        assert written is None and note is None

    def test_a_differently_cased_name_counts_too(self, tmp_path):
        """The library matches cover names case-insensitively; so must this.

        macOS's filesystem is case-insensitive, so a real-FS check would pass
        here whatever the code did -- the listing is asserted directly.
        """
        (tmp_path / "Folder.JPG").write_bytes(TINY_JPEG)

        assert has_sidecar_cover(tmp_path) is True

    def test_a_folder_that_is_not_there_has_no_cover(self, tmp_path):
        assert has_sidecar_cover(tmp_path / "gone") is False

    def test_no_temporary_file_survives_a_write(self, tmp_path):
        folder = tmp_path / "album"
        folder.mkdir()

        write_cover(folder, TINY_JPEG)

        assert sorted(path.name for path in folder.iterdir()) == [COVER_FILENAME]

    def test_an_unreachable_archive_is_a_note_not_a_failure(self, tmp_path):
        def fetch(url):
            raise httpx.ConnectError("no route")

        written, note = store_cover(tmp_path, _release(), fetch=fetch)

        assert written is None
        assert note.startswith("cover not fetched")

    def test_a_release_with_no_art_is_a_note(self, tmp_path):
        written, note = store_cover(tmp_path, _release(), fetch=lambda url: None)

        assert written is None and note == (
            "cover not fetched: no front image for this release"
        )


# ===========================================================================
# The pass
# ===========================================================================


class TestFullMatch:
    async def test_numbers_and_a_cover_are_written(self, tmp_path):
        folder = _two_track_album(tmp_path)

        result = await _full_match_pass(folder)

        assert result.complete and result.detail is None
        assert result.matched == 2 and result.total == 2
        first = FLAC(folder / "01 Migration.flac")
        second = FLAC(folder / "02 Kerala.flac")
        assert first["TITLE"] == ["Migration"] and first["TRACKNUMBER"] == ["1"]
        assert first["DISCNUMBER"] == ["1"]
        assert second["TRACKNUMBER"] == ["2"]
        assert (folder / COVER_FILENAME).exists()
        assert result.cover_written

    async def test_a_duplicate_top_candidate_does_not_lose_the_album(self, tmp_path):
        """The real case: one track's best-scoring entity is a 1-release copy.

        Dark Side of the Moon fails this way -- "Time"'s top clearing candidate
        is a duplicate recording that sits on one obscure release, while the
        recording the album actually lists is further down the same results.
        """
        folder = _two_track_album(tmp_path)
        shared = _release("rel-1", track_count=2)
        duplicate = _candidate(
            "Kerala", recording_id="rec-dup", releases=[_release("bootleg")]
        )
        canonical = _candidate("Kerala", recording_id="rec-2", releases=[shared])

        result = await _full_match_pass(
            folder,
            search=_searcher(
                {
                    "Migration": _candidate(
                        "Migration", recording_id="rec-1", releases=[shared]
                    ),
                    "Kerala": [duplicate, canonical],
                }
            ),
        )

        assert result.complete and result.matched == 2
        assert FLAC(folder / "02 Kerala.flac")["TRACKNUMBER"] == ["2"]

    async def test_the_tags_come_from_the_recording_the_release_lists(self, tmp_path):
        """A number and the title beside it must be one recording's, not two."""
        folder = _two_track_album(tmp_path)
        shared = _release("rel-1", track_count=2)
        # Clears the bar on `artist_names`, and would write a different
        # ARTIST from the recording the release actually lists.
        duplicate = Candidate(
            title="Kerala",
            artist_credit="Bonobo feat. Someone Else",
            artist_names=("Bonobo",),
            length_ms=LENGTH_MS,
            recording_id="rec-dup",
            releases=(_release("bootleg"),),
        )
        canonical = _candidate("Kerala", recording_id="rec-2", releases=[shared])

        await _full_match_pass(
            folder,
            search=_searcher(
                {
                    "Migration": _candidate(
                        "Migration", recording_id="rec-1", releases=[shared]
                    ),
                    # The featuring credit clears the bar first and is not on
                    # the album.
                    "Kerala": [duplicate, canonical],
                }
            ),
        )

        track = FLAC(folder / "02 Kerala.flac")
        assert track["ARTIST"] == ["Bonobo"]
        assert track["TRACKNUMBER"] == ["2"]

    async def test_no_musicbrainz_id_is_ever_written(self, tmp_path):
        folder = _two_track_album(tmp_path)

        await _full_match_pass(folder)

        for track in album_tracks(folder):
            keys = {key.upper() for key in FLAC(track).keys()}
            assert not any("MUSICBRAINZ" in key for key in keys)
            assert not any(key.endswith("_ID") for key in keys)

    async def test_album_and_albumartist_are_left_to_the_folders(self, tmp_path):
        folder = _two_track_album(tmp_path)
        path = folder / "01 Migration.flac"
        audio = FLAC(path)
        audio["ALBUM"] = "Whatever The Folder Says"
        audio["ALBUMARTIST"] = "Bonobo"
        audio.save()

        await _full_match_pass(folder)

        assert FLAC(path)["ALBUM"] == ["Whatever The Folder Says"]

    async def test_an_existing_cover_is_kept(self, tmp_path):
        folder = _two_track_album(tmp_path)
        (folder / COVER_FILENAME).write_bytes(b"the one the user chose")

        result = await _full_match_pass(folder)

        assert (folder / COVER_FILENAME).read_bytes() == b"the one the user chose"
        assert result.detail is None and not result.cover_written

    async def test_a_cover_that_could_not_be_fetched_still_finishes(self, tmp_path):
        folder = _two_track_album(tmp_path)

        result = await _full_match_pass(folder, fetch_cover_art=lambda url: None)

        assert result.complete
        assert result.detail.startswith("cover not fetched")
        assert not (folder / COVER_FILENAME).exists()

    async def test_the_changed_paths_are_what_was_written(self, tmp_path):
        folder = _two_track_album(tmp_path)

        result = await _full_match_pass(folder)

        assert sorted(path.name for path in result.changed) == [
            "01 Migration.flac",
            "02 Kerala.flac",
            COVER_FILENAME,
        ]


class TestPartialMatch:
    async def test_only_the_matched_tracks_are_fixed_and_no_numbers_are_written(
        self, tmp_path
    ):
        folder = _two_track_album(tmp_path)
        release = _release()

        result = await tag_album(
            folder,
            "Bonobo",
            run=_run,
            search=_searcher(
                {
                    "Migration": _candidate(
                        "Migration", recording_id="rec-1", releases=[release]
                    ),
                    "Kerala": None,
                }
            ),
            fetch_release=lambda release_id: _release_payload([("rec-1", 1)]),
            fetch_cover_art=lambda url: TINY_JPEG,
        )

        assert result.detail == partial_note(1, 2)
        assert not result.complete
        first = FLAC(folder / "01 Migration.flac")
        assert first["TITLE"] == ["Migration"]
        assert "TRACKNUMBER" not in first
        assert not (folder / COVER_FILENAME).exists()

    async def test_a_non_flac_counts_towards_the_total_but_is_never_fixed(
        self, tmp_path
    ):
        folder = _two_track_album(tmp_path)
        (folder / "03 Bonus.mp3").write_bytes(b"not a flac")
        release = _release()

        result = await tag_album(
            folder,
            "Bonobo",
            run=_run,
            search=_searcher(
                {
                    "Migration": _candidate(
                        "Migration", recording_id="rec-1", releases=[release]
                    ),
                    "Kerala": _candidate(
                        "Kerala", recording_id="rec-2", releases=[release]
                    ),
                }
            ),
            fetch_release=lambda release_id: _release_payload([("rec-1", 1)]),
            fetch_cover_art=lambda url: TINY_JPEG,
        )

        assert result.total == 3 and result.matched == 2
        assert result.detail == partial_note(2, 3)
        assert not (folder / COVER_FILENAME).exists()

    async def test_everything_matched_but_no_shared_release(self, tmp_path):
        folder = _two_track_album(tmp_path)

        result = await tag_album(
            folder,
            "Bonobo",
            run=_run,
            search=_searcher(
                {
                    "Migration": _candidate(
                        "Migration", recording_id="rec-1", releases=[_release("a")]
                    ),
                    "Kerala": _candidate(
                        "Kerala", recording_id="rec-2", releases=[_release("b")]
                    ),
                }
            ),
        )

        assert result.matched == 2 and not result.complete
        assert result.detail == NOTE_NO_RELEASE
        assert "TRACKNUMBER" not in FLAC(folder / "01 Migration.flac")

    async def test_a_release_missing_one_of_the_recordings_writes_no_numbers(
        self, tmp_path
    ):
        folder = _two_track_album(tmp_path)

        result = await _full_match_pass(
            folder, fetch_release=lambda release_id: _release_payload([("rec-1", 1)])
        )

        assert not result.complete and result.detail == NOTE_NO_RELEASE
        assert "TRACKNUMBER" not in FLAC(folder / "01 Migration.flac")

    async def test_an_empty_folder_says_so(self, tmp_path):
        folder = tmp_path / "Bonobo" / "Empty"
        folder.mkdir(parents=True)

        result = await tag_album(folder, "Bonobo", run=_run, search=_searcher({}))

        assert result.total == 0 and result.detail == album_tagger.NOTE_NO_TRACKS


class TestReleaseFallback:
    """Every track matched, no release common to all: fetch and verify."""

    def _split_search(self, first, second):
        """Two tracks whose searches name different releases, so the
        intersection :func:`choose_release` needs is empty."""
        return _searcher(
            {
                "Migration": _candidate(
                    "Migration", recording_id="rec-1", releases=[first]
                ),
                "Kerala": _candidate(
                    "Kerala", recording_id="rec-2", releases=[second]
                ),
            }
        )

    def _album_tracklist(self, **kwargs):
        return _tracklist_payload(
            [("rec-1", 1, "Migration", LENGTH_MS), ("rec-2", 2, "Kerala", LENGTH_MS)],
            **kwargs,
        )

    async def test_it_rescues_an_album_the_intersection_could_not_resolve(
        self, tmp_path
    ):
        folder = _two_track_album(tmp_path)
        fetched: list[str] = []

        def fetch_release(release_id):
            fetched.append(release_id)
            return self._album_tracklist()

        result = await tag_album(
            folder,
            "Bonobo",
            run=_run,
            search=self._split_search(
                _ranked_release("rel-a"), _ranked_release("rel-b")
            ),
            fetch_release=fetch_release,
            fetch_cover_art=lambda url: TINY_JPEG,
        )

        assert result.complete is True and result.detail is None
        assert fetched == ["rel-a"]
        first = FLAC(folder / "01 Migration.flac")
        assert first["TRACKNUMBER"] == ["1"] and first["DISCNUMBER"] == ["1"]
        assert FLAC(folder / "02 Kerala.flac")["TRACKNUMBER"] == ["2"]

    async def test_the_tags_come_from_the_row_that_cleared_the_bar(self, tmp_path):
        """Title and credit are the release's, not the search candidate's."""
        folder = _two_track_album(tmp_path)

        result = await tag_album(
            folder,
            "Bonobo",
            run=_run,
            search=self._split_search(
                _ranked_release("rel-a"), _ranked_release("rel-b")
            ),
            fetch_release=lambda _: self._album_tracklist(
                credit="Bonobo feat. Andreya Triana",
                names=("Bonobo", "Andreya Triana"),
            ),
            fetch_cover_art=lambda url: None,
        )

        assert result.complete is True
        first = FLAC(folder / "01 Migration.flac")
        assert first["ARTIST"] == ["Bonobo feat. Andreya Triana"]
        assert first["TITLE"] == ["Migration"]

    async def test_a_compilation_a_bootleg_and_a_tribute_are_never_fetched(
        self, tmp_path
    ):
        folder = _two_track_album(tmp_path)
        fetched: list[str] = []

        def fetch_release(release_id):
            fetched.append(release_id)
            return self._album_tracklist()

        # Different lists per track, so the intersection is empty and the
        # fallback is what has to throw these out.
        result = await tag_album(
            folder,
            "Bonobo",
            run=_run,
            search=_searcher(
                {
                    "Migration": _candidate(
                        "Migration",
                        recording_id="rec-1",
                        releases=[
                            _ranked_release("rel-hits", secondary=("Compilation",))
                        ],
                    ),
                    "Kerala": _candidate(
                        "Kerala",
                        recording_id="rec-2",
                        releases=[
                            _ranked_release("rel-boot", status="Bootleg"),
                            _ranked_release("rel-trib", artist_phrase="Tribute Band"),
                        ],
                    ),
                }
            ),
            fetch_release=fetch_release,
            fetch_cover_art=lambda url: None,
        )

        assert fetched == []
        assert result.complete is False and result.detail == NOTE_NO_RELEASE

    async def test_the_album_beats_the_collection_more_tracks_voted_for(
        self, tmp_path
    ):
        """Votes-first would pick the 40-track collection two of the three
        tracks name; track count first picks the album, which is the folder."""
        folder = _two_track_album(tmp_path)
        _write_track(folder / "03 Bambro.flac", title="Bambro Koyo Ganda")
        album = _ranked_release("rel-album", track_count=3)
        collection = _ranked_release("rel-collection", track_count=40)
        fetched: list[str] = []

        def fetch_release(release_id):
            fetched.append(release_id)
            return _tracklist_payload(
                [
                    ("rec-1", 1, "Migration", LENGTH_MS),
                    ("rec-2", 2, "Kerala", LENGTH_MS),
                    ("rec-3", 3, "Bambro Koyo Ganda", LENGTH_MS),
                ]
            )

        result = await tag_album(
            folder,
            "Bonobo",
            run=_run,
            search=_searcher(
                {
                    # No release is on all three, and the collection has twice
                    # the votes of the album.
                    "Migration": _candidate(
                        "Migration", recording_id="rec-1", releases=[collection]
                    ),
                    "Kerala": _candidate(
                        "Kerala", recording_id="rec-2", releases=[collection]
                    ),
                    "Bambro Koyo Ganda": _candidate(
                        "Bambro Koyo Ganda", recording_id="rec-3", releases=[album]
                    ),
                }
            ),
            fetch_release=fetch_release,
            fetch_cover_art=lambda url: None,
        )

        assert result.complete is True
        assert fetched == ["rel-album"]

    async def test_a_mixed_folder_gives_up_after_three_fetches(self, tmp_path):
        """One track from another album: no release holds both, and the pass
        stops spending requests rather than numbering from a near miss."""
        folder = _two_track_album(tmp_path)
        fetched: list[str] = []

        def fetch_release(release_id):
            fetched.append(release_id)
            # Every candidate release holds "Migration" and nothing else.
            return _tracklist_payload([("rec-1", 1, "Migration", LENGTH_MS)])

        result = await tag_album(
            folder,
            "Bonobo",
            run=_run,
            search=_searcher(
                {
                    "Migration": _candidate(
                        "Migration",
                        recording_id="rec-1",
                        releases=[
                            _ranked_release(f"rel-{index}") for index in range(4)
                        ],
                    ),
                    # Nothing in common, so choose_release spends nothing and
                    # the fallback's own cap is the whole budget.
                    "Kerala": _candidate(
                        "Kerala",
                        recording_id="rec-2",
                        releases=[_ranked_release("rel-9")],
                    ),
                }
            ),
            fetch_release=fetch_release,
            fetch_cover_art=lambda url: None,
        )

        assert len(fetched) == MAX_RELEASE_FETCHES
        assert result.complete is False and result.detail == NOTE_NO_RELEASE
        assert "TRACKNUMBER" not in FLAC(folder / "01 Migration.flac")
        assert not (folder / COVER_FILENAME).exists()

    async def test_a_tracklist_that_cannot_be_read_moves_on_to_the_next(
        self, tmp_path
    ):
        """The fallback is a rescue attempt for a folder already headed for
        "no common release": one unreadable tracklist must not throw away the
        tags every track was about to get."""
        folder = _two_track_album(tmp_path)
        fetched: list[str] = []

        def fetch_release(release_id):
            fetched.append(release_id)
            if release_id == "rel-a":
                raise musicbrainzngs.NetworkError("down")
            return self._album_tracklist()

        result = await tag_album(
            folder,
            "Bonobo",
            run=_run,
            search=self._split_search(
                _ranked_release("rel-a"), _ranked_release("rel-b")
            ),
            fetch_release=fetch_release,
            fetch_cover_art=lambda url: None,
        )

        assert fetched == ["rel-a", "rel-b"]
        assert result.complete is True
        assert FLAC(folder / "02 Kerala.flac")["TRACKNUMBER"] == ["2"]

    async def test_every_tracklist_failing_still_writes_the_tags(self, tmp_path):
        folder = _two_track_album(tmp_path)

        def fetch_release(release_id):
            raise musicbrainzngs.NetworkError("down")

        result = await tag_album(
            folder,
            "Bonobo",
            run=_run,
            search=self._split_search(
                _ranked_release("rel-a"), _ranked_release("rel-b")
            ),
            fetch_release=fetch_release,
            fetch_cover_art=lambda url: None,
        )

        assert result.complete is False and result.detail == NOTE_NO_RELEASE
        first = FLAC(folder / "01 Migration.flac")
        assert first["TITLE"] == ["Migration"] and "TRACKNUMBER" not in first

    async def test_a_release_the_search_ids_missed_is_matched_by_its_tracklist(
        self, tmp_path
    ):
        """The intersection picked the right release, but the search gave one
        track a duplicate recording id the release does not list.  Its
        tracklist holds the file all the same, and it is already fetched."""
        folder = _two_track_album(tmp_path)
        shared = _release("rel-1", track_count=2)
        fetched: list[str] = []

        def fetch_release(release_id):
            fetched.append(release_id)
            return _tracklist_payload(
                [
                    ("rec-1", 1, "Migration", LENGTH_MS),
                    ("rec-other", 2, "Kerala", LENGTH_MS),
                ]
            )

        result = await tag_album(
            folder,
            "Bonobo",
            run=_run,
            search=_searcher(
                {
                    "Migration": _candidate(
                        "Migration", recording_id="rec-1", releases=[shared]
                    ),
                    "Kerala": _candidate(
                        "Kerala", recording_id="rec-2", releases=[shared]
                    ),
                }
            ),
            fetch_release=fetch_release,
            fetch_cover_art=lambda url: None,
        )

        assert fetched == ["rel-1"]
        assert result.complete is True
        second = FLAC(folder / "02 Kerala.flac")
        assert second["TRACKNUMBER"] == ["2"] and second["TITLE"] == ["Kerala"]

    async def test_a_folder_the_intersection_resolved_costs_no_extra_fetch(
        self, tmp_path
    ):
        folder = _two_track_album(tmp_path)
        fetched: list[str] = []

        async def pass_with_counter():
            return await _full_match_pass(
                folder,
                fetch_release=lambda release_id: fetched.append(release_id)
                or _release_payload([("rec-1", 1), ("rec-2", 2)]),
            )

        result = await pass_with_counter()

        assert result.complete is True
        assert fetched == ["rel-1"]

    async def test_a_partial_folder_never_reaches_the_fallback(self, tmp_path):
        folder = _two_track_album(tmp_path)
        fetched: list[str] = []

        result = await tag_album(
            folder,
            "Bonobo",
            run=_run,
            search=_searcher(
                {
                    "Migration": _candidate(
                        "Migration", recording_id="rec-1", releases=[_ranked_release("rel-a")]
                    ),
                    "Kerala": None,
                }
            ),
            fetch_release=lambda release_id: fetched.append(release_id) or {},
            fetch_cover_art=lambda url: None,
        )

        assert fetched == []
        assert result.detail == partial_note(1, 2)


class TestProgressAndCancel:
    async def test_progress_counts_every_track(self, tmp_path):
        folder = _two_track_album(tmp_path)
        seen: list[tuple[int, int]] = []

        await _full_match_pass(folder, on_progress=lambda *pair: seen.append(pair))

        assert seen == [(0, 2), (1, 2), (2, 2)]

    async def test_a_cancel_before_the_first_lookup_writes_nothing(self, tmp_path):
        folder = _two_track_album(tmp_path)
        before = (folder / "01 Migration.flac").read_bytes()

        result = await _full_match_pass(folder, should_cancel=lambda: True)

        assert result.cancelled and result.changed == []
        assert (folder / "01 Migration.flac").read_bytes() == before

    async def test_a_cancel_during_the_writes_leaves_the_rest_untouched(self, tmp_path):
        folder = _two_track_album(tmp_path)
        second = folder / "02 Kerala.flac"
        before = second.read_bytes()
        writes = 0
        stop = False

        async def run(step):
            nonlocal writes, stop
            result = step()
            if isinstance(result, bool):  # a write_track step
                writes += 1
                if writes == 1:
                    stop = True
            return result

        result = await _full_match_pass(
            folder, run=run, should_cancel=lambda: stop
        )

        assert result.cancelled
        assert FLAC(folder / "01 Migration.flac")["TITLE"] == ["Migration"]
        assert second.read_bytes() == before
        assert not (folder / COVER_FILENAME).exists()


class TestFailures:
    async def test_musicbrainz_being_down_fails_the_pass(self, tmp_path):
        folder = _two_track_album(tmp_path)

        def search(*args):
            raise musicbrainzngs.NetworkError("down")

        with pytest.raises(TagStepFailed) as caught:
            await tag_album(folder, "Bonobo", run=_run, search=search)

        assert caught.value.note == NOTE_UNAVAILABLE

    async def test_a_file_that_cannot_be_written_fails_the_pass(
        self, tmp_path, monkeypatch
    ):
        folder = _two_track_album(tmp_path)

        def refuse(*args, **kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(album_tagger, "apply_fix", refuse)

        with pytest.raises(TagStepFailed) as caught:
            await _full_match_pass(folder)

        assert caught.value.note == "tags not fixed: file could not be written"


class TestSingles:
    async def test_a_single_is_a_track_lookup_with_no_album_and_no_cover(
        self, tmp_path
    ):
        """A loose Single is tagged one track at a time, never as a folder: the
        pass is only ever pointed at an album folder, and the artist folder is
        not one."""
        single = _write_track(
            tmp_path / "Bonobo" / "Kerala.flac", title="Kerala (Official Video)"
        )

        lookup = lookup_track(
            single,
            "Bonobo",
            search=_searcher({"Kerala": _candidate("Kerala", recording_id="rec-2")}),
        )
        album_tagger.write_track(single, lookup.match)

        audio = FLAC(single)
        assert audio["TITLE"] == ["Kerala"] and audio["ARTIST"] == ["Bonobo"]
        assert "ALBUM" not in audio and "TRACKNUMBER" not in audio
        assert not (single.parent / COVER_FILENAME).exists()


class TestResultShape:
    def test_the_partial_note_is_the_tickets_wording(self):
        assert partial_note(9, 12) == "partial: 9 of 12"

    def test_a_fresh_result_says_nothing(self):
        assert AlbumTagResult().detail is None


class TestFetchCoverBytes:
    """The one function that really speaks HTTP, against an in-process
    transport rather than the archive."""

    def _client_over(self, handler):
        def factory(**kwargs):
            return REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

        return factory

    def test_it_follows_the_archives_redirect(self, monkeypatch):
        def handler(request):
            if request.url.path.endswith("front-500"):
                return httpx.Response(
                    307, headers={"Location": "https://archive.org/the-image.jpg"}
                )
            return httpx.Response(200, content=TINY_JPEG)

        monkeypatch.setattr(album_tagger.httpx, "Client", self._client_over(handler))

        assert (
            album_tagger.fetch_cover_bytes(
                "https://coverartarchive.org/release/rel-1/front-500"
            )
            == TINY_JPEG
        )

    def test_a_release_with_no_front_image_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(
            album_tagger.httpx,
            "Client",
            self._client_over(lambda request: httpx.Response(404)),
        )

        assert album_tagger.fetch_cover_bytes("https://coverartarchive.org/x") is None

    def test_a_server_error_raises_for_the_caller_to_note(self, monkeypatch):
        monkeypatch.setattr(
            album_tagger.httpx,
            "Client",
            self._client_over(lambda request: httpx.Response(503)),
        )

        with pytest.raises(httpx.HTTPError):
            album_tagger.fetch_cover_bytes("https://coverartarchive.org/x")

    def test_something_far_too_big_is_refused(self, monkeypatch):
        oversized = b"\xff\xd8\xff" + bytes(album_tagger.MAX_COVER_BYTES)
        monkeypatch.setattr(
            album_tagger.httpx,
            "Client",
            self._client_over(lambda request: httpx.Response(200, content=oversized)),
        )

        assert album_tagger.fetch_cover_bytes("https://coverartarchive.org/x") is None

    def test_a_declared_length_over_the_cap_is_not_read(self, monkeypatch):
        def sized(request):
            response = httpx.Response(200, content=b"\xff\xd8\xff")
            response.headers["Content-Length"] = str(album_tagger.MAX_COVER_BYTES + 1)
            return response

        monkeypatch.setattr(album_tagger.httpx, "Client", self._client_over(sized))

        assert album_tagger.fetch_cover_bytes("https://coverartarchive.org/x") is None

    def test_a_chunked_body_is_abandoned_once_it_passes_the_cap(self, monkeypatch):
        """No Content-Length to go on: the read itself has to give up."""
        chunk = b"\xff\xd8\xff" + bytes(1024 * 1024)
        sent = {"chunks": 0}

        class Endless(httpx.SyncByteStream):
            def __iter__(self):
                while True:
                    sent["chunks"] += 1
                    yield chunk

        def handler(request):
            return httpx.Response(200, stream=Endless())

        monkeypatch.setattr(album_tagger.httpx, "Client", self._client_over(handler))

        assert album_tagger.fetch_cover_bytes("https://coverartarchive.org/x") is None
        # Stopped at the cap rather than reading an unbounded body.
        assert sent["chunks"] <= album_tagger.MAX_COVER_BYTES // len(chunk) + 2

    def test_it_identifies_itself(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["ua"] = request.headers.get("user-agent")
            return httpx.Response(200, content=TINY_JPEG)

        monkeypatch.setattr(album_tagger.httpx, "Client", self._client_over(handler))
        album_tagger.fetch_cover_bytes("https://coverartarchive.org/x")

        assert seen["ua"].startswith("music-for-arr/")
