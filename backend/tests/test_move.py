"""Tests for POST /library/move and the mover behind it.

Every acceptance criterion of the "Move and rename" phase has a test here:
tracks to a new artist and album with the three tags rewritten and ``SOURCEID``
surviving, an album merged into an existing one, a 409 that leaves nothing
half-moved, an artist rename that follows through into every FLAC, the path
validation the domain model demands, the empty-folder cleanup, the in-flight
guard, and the Singles form that clears ``ALBUM``.

The tree is built with :func:`tests.test_library.write_flac`, which writes real
tagged FLACs from :func:`tests.conftest.minimal_flac_bytes`, so the tag
assertions are made against files mutagen really wrote and really read back.
"""

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.flac import FLAC

from app import library, library_ops
from app.models import Job, JobKind, JobStatus, SSEEvent
from app.queue_manager import QueueManager
from tests.conftest import TINY_JPEG
from tests.test_library import write_flac


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def build_tree(root: Path) -> None:
    """A small library: one artist with two albums, a Single, and a stray file."""
    write_flac(
        root / "Bonobo" / "Black Sands" / "Kiara.flac",
        title="Kiara",
        artist="Bonobo",
        albumartist="Bonobo",
        album="Black Sands",
        sourceid="youtube:abc123",
        sourceurl="https://www.youtube.com/watch?v=abc123",
    )
    write_flac(
        root / "Bonobo" / "Black Sands" / "Kong.flac",
        picture=TINY_JPEG,
        title="Kong",
        artist="Bonobo",
        albumartist="Bonobo",
        album="Black Sands",
    )
    (root / "Bonobo" / "Black Sands" / "cover.jpg").write_bytes(TINY_JPEG)
    write_flac(
        root / "Bonobo" / "Migration" / "Kerala.flac",
        title="Kerala",
        artist="Bonobo",
        albumartist="Bonobo",
        album="Migration",
    )
    write_flac(
        root / "Bonobo" / "Flashlight.flac",
        title="Flashlight",
        artist="Bonobo",
        albumartist="Bonobo",
        album="Flashlight EP",
    )


@pytest.fixture()
def root(isolated_paths):
    """The reference tree on disk, with the scan caches reset around the test."""
    download_dir, _ = isolated_paths
    build_tree(download_dir)
    library.invalidate()
    yield download_dir
    library.invalidate()


@pytest.fixture()
def client_and_queue(root):
    """A TestClient over the real app, plus the queue manager it is wired to.

    The module-level singleton is swapped for a fresh manager, as the other
    route tests do, so this test's jobs and this test's SSE events belong to it
    alone.  ``events`` collects everything the manager emits, which is how the
    ``library_changed`` assertions are made without a live SSE client.
    """
    import app.main as main_module

    events: list[SSEEvent] = []

    def record(event: SSEEvent) -> None:
        events.append(event)
        main_module._on_queue_event(event)

    original = main_module.queue_manager
    manager = QueueManager(max_concurrent=1, timeout=10, on_event=record)
    main_module.queue_manager = manager
    try:
        with TestClient(main_module.app) as test_client:
            yield test_client, manager, events
    finally:
        main_module.queue_manager = original


@pytest.fixture()
def client(client_and_queue):
    return client_and_queue[0]


def tags_of(path: Path) -> dict[str, list[str]]:
    return {key.upper(): value for key, value in FLAC(path).items()}


def rel_paths(root: Path) -> set[str]:
    """Every file under *root*, relative and POSIX, for whole-tree assertions."""
    return {
        Path(dirpath, name).relative_to(root).as_posix()
        for dirpath, _dirs, files in os.walk(root)
        for name in files
    }


# ---------------------------------------------------------------------------
# Moving tracks
# ---------------------------------------------------------------------------


def test_moving_a_track_to_a_new_artist_and_album_creates_both(client, root):
    response = client.post(
        "/library/move",
        json={
            "paths": ["Bonobo/Black Sands/Kiara.flac"],
            "artist": "Bonobo Remixes",
            "album": "Black Sands Remixed",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["moved"] == [
        {
            "from": "Bonobo/Black Sands/Kiara.flac",
            "to": "Bonobo Remixes/Black Sands Remixed/Kiara.flac",
        }
    ]

    moved = root / "Bonobo Remixes" / "Black Sands Remixed" / "Kiara.flac"
    assert moved.is_file()
    assert not (root / "Bonobo" / "Black Sands" / "Kiara.flac").exists()

    tags = tags_of(moved)
    assert tags["ARTIST"] == ["Bonobo Remixes"]
    assert tags["ALBUMARTIST"] == ["Bonobo Remixes"]
    assert tags["ALBUM"] == ["Black Sands Remixed"]
    # Everything else survives: provenance is what dedup and the tag fixer key on.
    assert tags["SOURCEID"] == ["youtube:abc123"]
    assert tags["SOURCEURL"] == ["https://www.youtube.com/watch?v=abc123"]
    assert tags["TITLE"] == ["Kiara"]


def test_a_new_folder_name_is_sanitised(client, root):
    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Flashlight.flac"], "artist": "AC/DC", "album": "Back"},
    )

    assert response.status_code == 200
    created = {
        entry.name for entry in root.iterdir() if entry.is_dir()
    }
    assert "AC/DC" not in created
    assert any(name.startswith("AC") and "DC" in name for name in created)


def test_moving_a_track_with_no_album_makes_it_a_single_and_clears_album(client, root):
    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Black Sands/Kiara.flac"], "artist": "Bonobo", "album": ""},
    )

    assert response.status_code == 200
    moved = root / "Bonobo" / "Kiara.flac"
    assert moved.is_file()
    tags = tags_of(moved)
    assert "ALBUM" not in tags
    assert tags["ALBUMARTIST"] == ["Bonobo"]


def test_moving_a_single_into_an_album_sets_the_album_tag(client, root):
    response = client.post(
        "/library/move",
        json={
            "paths": ["Bonobo/Flashlight.flac"],
            "artist": "Bonobo",
            "album": "Black Sands",
        },
    )

    assert response.status_code == 200
    assert tags_of(root / "Bonobo" / "Black Sands" / "Flashlight.flac")["ALBUM"] == [
        "Black Sands"
    ]


def test_moving_several_tracks_at_once(client, root):
    response = client.post(
        "/library/move",
        json={
            "paths": [
                "Bonobo/Black Sands/Kiara.flac",
                "Bonobo/Black Sands/Kong.flac",
            ],
            "artist": "Bonobo",
            "album": "Migration",
        },
    )

    assert response.status_code == 200
    assert len(response.json()["moved"]) == 2
    assert (root / "Bonobo" / "Migration" / "Kiara.flac").is_file()
    assert (root / "Bonobo" / "Migration" / "Kong.flac").is_file()


def test_tracks_from_two_folders_are_refused(client, root):
    response = client.post(
        "/library/move",
        json={
            "paths": ["Bonobo/Black Sands/Kiara.flac", "Bonobo/Migration/Kerala.flac"],
            "artist": "Someone",
        },
    )

    assert response.status_code == 400


def test_a_track_move_that_collides_moves_nothing(client, root):
    write_flac(root / "Bonobo" / "Migration" / "Kiara.flac", title="Kiara (other)")
    before = rel_paths(root)

    response = client.post(
        "/library/move",
        json={
            "paths": [
                "Bonobo/Black Sands/Kiara.flac",
                "Bonobo/Black Sands/Kong.flac",
            ],
            "artist": "Bonobo",
            "album": "Migration",
        },
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["conflicts"] == ["Bonobo/Migration/Kiara.flac"]
    assert "message" in detail
    # Kong would have fitted; all-or-nothing means it stayed where it was.
    assert rel_paths(root) == before


def test_a_non_flac_track_is_moved_without_a_tag_rewrite(client, root):
    source = root / "Bonobo" / "Black Sands" / "Kiara.mp3"
    source.write_bytes(b"not really an mp3")

    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Black Sands/Kiara.mp3"], "artist": "Someone Else"},
    )

    assert response.status_code == 200
    moved = root / "Someone Else" / "Kiara.mp3"
    assert moved.read_bytes() == b"not really an mp3"


# ---------------------------------------------------------------------------
# Empty folder cleanup
# ---------------------------------------------------------------------------


def test_the_last_track_out_of_an_album_takes_the_folder_and_its_cover(client, root):
    response = client.post(
        "/library/move",
        json={
            "paths": [
                "Bonobo/Black Sands/Kiara.flac",
                "Bonobo/Black Sands/Kong.flac",
            ],
            "artist": "Bonobo",
            "album": "Migration",
        },
    )

    assert response.status_code == 200
    assert response.json()["removed"] == ["Bonobo/Black Sands"]
    assert not (root / "Bonobo" / "Black Sands").exists()
    # The cover.jpg left behind went with it.
    assert not (root / "Bonobo" / "Black Sands" / "cover.jpg").exists()
    # The artist still has audio, so the artist folder stays.
    assert (root / "Bonobo").is_dir()


def test_the_last_track_out_of_an_artist_takes_the_artist_folder_too(client, root):
    write_flac(root / "Solo" / "Only" / "One.flac", title="One")
    (root / "Solo" / "Only" / "cover.jpg").write_bytes(TINY_JPEG)

    response = client.post(
        "/library/move",
        json={"paths": ["Solo/Only/One.flac"], "artist": "Bonobo", "album": "Migration"},
    )

    assert response.status_code == 200
    assert set(response.json()["removed"]) == {"Solo/Only", "Solo"}
    assert not (root / "Solo").exists()


def test_a_source_folder_that_still_has_audio_is_kept(client, root):
    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Black Sands/Kiara.flac"], "artist": "Someone"},
    )

    assert response.status_code == 200
    assert response.json()["removed"] == []
    assert (root / "Bonobo" / "Black Sands" / "Kong.flac").is_file()


# ---------------------------------------------------------------------------
# Moving an album
# ---------------------------------------------------------------------------


def test_moving_an_album_to_another_artist(client, root):
    response = client.post(
        "/library/move",
        json={"path": "Bonobo/Black Sands", "artist": "Ninja Tune"},
    )

    assert response.status_code == 200
    assert (root / "Ninja Tune" / "Black Sands" / "Kiara.flac").is_file()
    assert (root / "Ninja Tune" / "Black Sands" / "cover.jpg").is_file()
    assert not (root / "Bonobo" / "Black Sands").exists()
    assert tags_of(root / "Ninja Tune" / "Black Sands" / "Kiara.flac")["ALBUMARTIST"] == [
        "Ninja Tune"
    ]


def test_moving_an_album_can_rename_it(client, root):
    response = client.post(
        "/library/move",
        json={"path": "Bonobo/Black Sands", "artist": "Bonobo", "album": "Black Sands (2010)"},
    )

    assert response.status_code == 200
    assert (root / "Bonobo" / "Black Sands (2010)" / "Kong.flac").is_file()
    assert tags_of(root / "Bonobo" / "Black Sands (2010)" / "Kong.flac")["ALBUM"] == [
        "Black Sands (2010)"
    ]


def test_moving_an_album_merges_disjoint_files(client, root):
    write_flac(
        root / "Ninja Tune" / "Black Sands" / "Eyesdown.flac",
        title="Eyesdown",
        album="Black Sands",
    )
    (root / "Ninja Tune" / "Black Sands" / "cover.jpg").write_bytes(TINY_JPEG)

    response = client.post(
        "/library/move",
        json={"path": "Bonobo/Black Sands", "artist": "Ninja Tune"},
    )

    assert response.status_code == 200
    merged = root / "Ninja Tune" / "Black Sands"
    assert {entry.name for entry in merged.iterdir()} == {
        "Eyesdown.flac",
        "Kiara.flac",
        "Kong.flac",
        "cover.jpg",
    }
    # The source folder, and the duplicate cover.jpg that stayed behind, are gone.
    assert not (root / "Bonobo" / "Black Sands").exists()
    assert tags_of(merged / "Kiara.flac")["ALBUMARTIST"] == ["Ninja Tune"]


def test_an_album_merge_that_collides_moves_nothing(client, root):
    write_flac(
        root / "Ninja Tune" / "Black Sands" / "Kong.flac",
        title="Kong (theirs)",
        album="Black Sands",
    )
    before = rel_paths(root)

    response = client.post(
        "/library/move",
        json={"path": "Bonobo/Black Sands", "artist": "Ninja Tune"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"] == ["Ninja Tune/Black Sands/Kong.flac"]
    assert rel_paths(root) == before
    assert tags_of(root / "Bonobo" / "Black Sands" / "Kiara.flac")["ALBUMARTIST"] == [
        "Bonobo"
    ]


def test_an_album_folder_nested_deeper_moves_as_a_whole(client, root):
    write_flac(
        root / "Bonobo" / "Migration" / "Disc 1" / "Outer.flac",
        title="Outer",
        album="Migration",
    )

    response = client.post(
        "/library/move",
        json={"path": "Bonobo/Migration", "artist": "Ninja Tune"},
    )

    assert response.status_code == 200
    assert (root / "Ninja Tune" / "Migration" / "Disc 1" / "Outer.flac").is_file()


# ---------------------------------------------------------------------------
# Renaming an artist
# ---------------------------------------------------------------------------


def test_renaming_an_artist_rewrites_albumartist_everywhere(client, root):
    response = client.post("/library/move", json={"path": "Bonobo", "artist": "Bonobo (UK)"})

    assert response.status_code == 200
    renamed = root / "Bonobo (UK)"
    assert renamed.is_dir()
    assert not (root / "Bonobo").exists()

    for track in ("Black Sands/Kiara.flac", "Black Sands/Kong.flac", "Migration/Kerala.flac"):
        assert tags_of(renamed / track)["ALBUMARTIST"] == ["Bonobo (UK)"]
        assert tags_of(renamed / track)["ARTIST"] == ["Bonobo (UK)"]
    # The album tag is none of a rename's business.
    assert tags_of(renamed / "Black Sands" / "Kiara.flac")["ALBUM"] == ["Black Sands"]


def test_renaming_an_artist_leaves_a_guest_artist_tag_alone(client, root):
    write_flac(
        root / "Bonobo" / "Black Sands" / "Guest.flac",
        title="Guest",
        artist="Andreya Triana",
        albumartist="Bonobo",
        album="Black Sands",
    )

    response = client.post("/library/move", json={"path": "Bonobo", "artist": "Bonobo (UK)"})

    assert response.status_code == 200
    tags = tags_of(root / "Bonobo (UK)" / "Black Sands" / "Guest.flac")
    assert tags["ALBUMARTIST"] == ["Bonobo (UK)"]
    assert tags["ARTIST"] == ["Andreya Triana"]


def test_renaming_an_artist_onto_an_existing_one_is_refused(client, root):
    write_flac(root / "Ninja Tune" / "Some Album" / "Track.flac", title="Track")
    before = rel_paths(root)

    response = client.post("/library/move", json={"path": "Bonobo", "artist": "Ninja Tune"})

    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"] == ["Ninja Tune"]
    assert rel_paths(root) == before


def test_a_case_only_artist_rename_works(client, root):
    response = client.post("/library/move", json={"path": "Bonobo", "artist": "BONOBO"})

    assert response.status_code == 200
    names = {entry.name for entry in root.iterdir() if entry.is_dir()}
    assert "BONOBO" in names
    assert "Bonobo" not in names
    assert tags_of(root / "BONOBO" / "Black Sands" / "Kiara.flac")["ALBUMARTIST"] == [
        "BONOBO"
    ]


def test_the_response_says_where_an_album_and_an_artist_now_live(client, root):
    """The dialog follows what it moved, so it has to be told where it went."""
    album = client.post(
        "/library/move",
        json={"path": "Bonobo/Black Sands", "artist": "Bonobo", "album": "Black Sands (2010)"},
    )
    assert album.status_code == 200
    assert album.json()["destination"] == "Bonobo/Black Sands (2010)"

    artist = client.post("/library/move", json={"path": "Bonobo", "artist": "Bonobo (UK)"})
    assert artist.status_code == 200
    assert artist.json()["destination"] == "Bonobo (UK)"


def test_a_track_move_reports_no_destination(client, root):
    """Tracks went *into* a folder; the folder itself did not move."""
    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Flashlight.flac"], "artist": "Someone Else"},
    )

    assert response.status_code == 200
    assert response.json()["destination"] is None


def test_a_loose_root_track_takes_the_new_artist_in_its_artist_tag(client, root):
    """A track loose in the root was under no artist folder, so ARTIST follows.

    The guard that keeps a guest artist's credit only applies when there was a
    folder to disagree with.  Filing a stray file under an artist is the user
    saying whose it is, and all three tags say so afterwards.
    """
    write_flac(root / "Loose.flac", title="Loose", artist="Some Guest")

    response = client.post(
        "/library/move", json={"paths": ["Loose.flac"], "artist": "New Artist"}
    )

    assert response.status_code == 200
    tags = tags_of(root / "New Artist" / "Loose.flac")
    assert tags["ARTIST"] == ["New Artist"]
    assert tags["ALBUMARTIST"] == ["New Artist"]
    assert "ALBUM" not in tags


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../etc",
        "Bonobo/../../etc",
        "/etc/passwd",
        "Bonobo\\Black Sands",
        "",
    ],
)
def test_a_malformed_source_path_is_refused(client, path):
    response = client.post("/library/move", json={"path": path, "artist": "Someone"})
    assert response.status_code in (400, 404, 422)


@pytest.mark.parametrize(
    "artist", ["..", ".", "", "   ", ".hidden", "...", ".config"]
)
def test_a_malformed_artist_name_is_refused(client, artist):
    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Flashlight.flac"], "artist": artist},
    )
    assert response.status_code in (400, 422)


def test_a_name_with_separators_becomes_one_folder(client, root):
    """"AC/DC" is a band, not a path: sanitising it is the domain model's rule."""
    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Flashlight.flac"], "artist": "Some/Artist/Deep"},
    )

    assert response.status_code == 200
    target = response.json()["moved"][0]["to"]
    assert target.count("/") == 1  # one folder, then the filename


def test_a_symlink_escaping_the_root_is_refused(client, root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Escape.flac").write_bytes(b"x")
    (root / "Escape").symlink_to(outside)

    response = client.post(
        "/library/move",
        json={"paths": ["Escape/Escape.flac"], "artist": "Someone"},
    )

    assert response.status_code == 400
    assert (outside / "Escape.flac").exists()


def test_an_unknown_source_is_a_404(client):
    response = client.post(
        "/library/move", json={"path": "Nobody/Nothing", "artist": "Someone"}
    )
    assert response.status_code == 404


def test_giving_both_path_and_paths_is_refused(client):
    response = client.post(
        "/library/move",
        json={"path": "Bonobo", "paths": ["Bonobo/Flashlight.flac"], "artist": "X"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "give 'path' or 'paths', not both"


def test_giving_neither_path_nor_paths_is_refused(client):
    """Its own message: "not both" is no help to a caller that gave neither."""
    response = client.post("/library/move", json={"artist": "X"})
    assert response.status_code == 400
    assert response.json()["detail"] == "give either 'path' or 'paths'"


def test_an_empty_paths_list_is_refused(client):
    response = client.post("/library/move", json={"paths": [], "artist": "X"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The in-flight guard
# ---------------------------------------------------------------------------


def _in_flight_job(**overrides) -> Job:
    """A running job that has already resolved where it is going.

    ``target_dir`` is what the guard reads -- not ``artist``/``album``, which
    are only what the user typed and are blank for most downloads.
    """
    defaults = {
        "id": "job-1",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "status": JobStatus.DOWNLOADING,
        "target_dir": "Bonobo/Fragments",
    }
    defaults.update(overrides)
    return Job(**defaults)


def test_in_flight_targets_are_the_folders_a_job_will_write_into():
    manager = QueueManager(max_concurrent=1, timeout=10)
    manager._jobs["job-1"] = _in_flight_job()
    manager._jobs["job-2"] = _in_flight_job(id="job-2", target_dir="Solo")
    manager._jobs["job-3"] = _in_flight_job(id="job-3", status=JobStatus.DONE)

    in_flight = manager.in_flight_library_targets()

    assert in_flight.targets == ["Bonobo", "Bonobo/Fragments", "Solo"]
    assert in_flight.unresolved == 0


def test_a_job_that_has_not_resolved_a_target_is_counted_as_unresolved():
    manager = QueueManager(max_concurrent=1, timeout=10)
    manager._jobs["job-1"] = _in_flight_job(target_dir=None)

    in_flight = manager.in_flight_library_targets()

    assert in_flight.targets == []
    assert in_flight.unresolved == 1


def test_a_guessed_target_is_counted_as_unresolved_rather_than_guarded():
    """"Unknown Artist" is the fallback, not a folder the download will use."""
    manager = QueueManager(max_concurrent=1, timeout=10)
    manager._jobs["job-1"] = _in_flight_job(
        target_dir="Unknown Artist", target_guessed=True, title="Some Track"
    )

    in_flight = manager.in_flight_library_targets()

    assert in_flight.targets == []
    assert in_flight.unresolved == 1
    assert in_flight.unresolved_jobs == ["Some Track"]


def test_a_tagging_job_is_not_an_unresolved_download():
    """Only downloads create folders, so only downloads can block a move.

    A tagging job has no ``target_dir`` at all -- it re-tags files that are
    already in the library -- and counting it as unresolved would refuse every
    move for as long as one was running.
    """
    manager = QueueManager(max_concurrent=1, timeout=10)
    manager._jobs["job-1"] = _in_flight_job(
        kind=JobKind.TAGGING, target_dir=None, path="Bonobo"
    )

    in_flight = manager.in_flight_library_targets()

    assert in_flight.targets == []
    assert in_flight.unresolved == 0


def test_a_download_being_tagged_still_guards_its_folder():
    """The tag fix writes into the file the download filed, so the folder is
    still off limits until the job is done -- moving it out from under the
    tagger would have it writing to a path that no longer exists."""
    manager = QueueManager(max_concurrent=1, timeout=10)
    manager._jobs["job-1"] = _in_flight_job(
        status=JobStatus.TAGGING, result_path="Bonobo/Fragments/Otomo.flac"
    )

    in_flight = manager.in_flight_library_targets()

    assert in_flight.targets == ["Bonobo", "Bonobo/Fragments"]
    assert in_flight.unresolved == 0


def test_renaming_an_artist_a_job_is_tagging_in_is_refused(client_and_queue, root):
    client, manager, _events = client_and_queue
    manager._jobs["job-1"] = _in_flight_job(status=JobStatus.TAGGING)
    before = rel_paths(root)

    response = client.post(
        "/library/move", json={"path": "Bonobo", "artist": "Bonobo (UK)"}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"] == ["Bonobo"]
    assert rel_paths(root) == before


def test_renaming_an_artist_a_job_is_downloading_into_is_refused(client_and_queue, root):
    client, manager, _events = client_and_queue
    manager._jobs["job-1"] = _in_flight_job()
    before = rel_paths(root)

    response = client.post("/library/move", json={"path": "Bonobo", "artist": "Bonobo (UK)"})

    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"] == ["Bonobo"]
    assert rel_paths(root) == before


def test_moving_into_a_folder_a_job_is_downloading_into_is_refused(client_and_queue, root):
    client, manager, _events = client_and_queue
    manager._jobs["job-1"] = _in_flight_job(target_dir="Bonobo/Black Sands")

    response = client.post(
        "/library/move",
        json={
            "paths": ["Bonobo/Migration/Kerala.flac"],
            "artist": "Bonobo",
            "album": "Black Sands",
        },
    )

    assert response.status_code == 409
    assert (root / "Bonobo" / "Migration" / "Kerala.flac").is_file()


def test_a_job_with_no_typed_artist_still_guards_where_it_landed(
    client_and_queue, root
):
    """The guard follows the resolved destination, not what the user typed.

    Most downloads carry no artist at all: guessing from ``job.artist`` said
    "Unknown Artist" while yt-dlp's own name put the file somewhere else
    entirely, and the folder it was really about to appear in was renamed out
    from under it.
    """
    client, manager, _events = client_and_queue
    write_flac(root / "Blender" / "Track.flac", title="Track")
    manager._jobs["job-1"] = _in_flight_job(
        artist=None, album=None, target_dir="Blender"
    )
    before = rel_paths(root)

    response = client.post(
        "/library/move", json={"path": "Blender", "artist": "Blender (band)"}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"] == ["Blender"]
    assert rel_paths(root) == before


def test_a_job_that_has_not_resolved_its_destination_blocks_a_move(
    client_and_queue, root
):
    client, manager, _events = client_and_queue
    manager._jobs["job-1"] = _in_flight_job(target_dir=None)
    before = rel_paths(root)

    response = client.post(
        "/library/move", json={"path": "Bonobo", "artist": "Bonobo (UK)"}
    )

    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "resolved" in detail["message"]
    # Actionable: the download being waited on is named, by title, by url, or
    # by id, rather than leaving the user to guess which queue entry to watch.
    assert url in detail["message"]
    assert detail["conflicts"] == [url]
    assert rel_paths(root) == before


def test_moving_a_track_out_of_a_folder_a_job_is_aiming_at_is_refused(
    client_and_queue, root
):
    """The source end is guarded too: the move may empty and remove it."""
    client, manager, _events = client_and_queue
    manager._jobs["job-1"] = _in_flight_job(target_dir="Bonobo/Migration")

    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Migration/Kerala.flac"], "artist": "Someone Else"},
    )

    assert response.status_code == 409
    assert (root / "Bonobo" / "Migration" / "Kerala.flac").is_file()


def test_an_artist_folder_a_job_is_aiming_at_survives_the_cleanup(
    client_and_queue, root
):
    """Moving an artist's last album away leaves the folder when a job wants it."""
    client, manager, _events = client_and_queue
    write_flac(root / "Lone" / "Only Album" / "Track.flac", title="Track")
    manager._jobs["job-1"] = _in_flight_job(target_dir="Lone")

    response = client.post(
        "/library/move", json={"path": "Lone/Only Album", "artist": "Bonobo"}
    )

    assert response.status_code == 200
    assert response.json()["removed"] == []
    assert (root / "Lone").is_dir()
    assert (root / "Bonobo" / "Only Album" / "Track.flac").is_file()


def test_cleanup_never_removes_an_audio_file_that_turned_up(root):
    """A download that files a track in during the cleanup keeps its folder.

    ``_remove_leftovers`` is what replaced ``shutil.rmtree`` here: the
    emptiness check and the removal cannot be one atomic step, so the removal
    itself has to refuse the moment it meets audio.
    """
    from app.library_ops import remove_leftovers

    folder = root / "Bonobo" / "Black Sands"

    assert remove_leftovers(folder) is False
    assert (folder / "Kiara.flac").is_file()
    assert (folder / "Kong.flac").is_file()


def test_a_finished_job_does_not_block_a_move(client_and_queue, root):
    client, manager, _events = client_and_queue
    manager._jobs["job-1"] = _in_flight_job(status=JobStatus.DONE)

    response = client.post("/library/move", json={"path": "Bonobo", "artist": "Bonobo (UK)"})

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# What a move tells the rest of the app
# ---------------------------------------------------------------------------


def test_a_move_emits_library_changed_with_both_ends(client_and_queue, root):
    client, _manager, events = client_and_queue

    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Migration/Kerala.flac"], "artist": "Bonobo", "album": "Black Sands"},
    )

    assert response.status_code == 200
    changed = [event for event in events if event.event == "library_changed"]
    assert len(changed) == 1
    assert set(changed[0].data["paths"]) == {"Bonobo/Migration", "Bonobo/Black Sands"}


def test_a_move_that_stranded_files_still_refreshes_the_tree(
    client_and_queue, root, monkeypatch
):
    """A 500 that says files moved has to be a 500 the open tabs believe.

    The rollback failed, so the tree really did change; leaving the scan cache
    and the tabs on the old picture would show the user the track still in the
    album the error has just told them it is no longer in.
    """
    client, _manager, events = client_and_queue
    import app.main as main_module

    invalidations: list[int] = []
    real_invalidate = main_module.library_invalidate

    def counting() -> None:
        invalidations.append(1)
        real_invalidate()

    monkeypatch.setattr(main_module, "library_invalidate", counting)

    def fake_rename_files(pairs):
        source, target = pairs[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, target)
        raise library_ops.PartialRenameError([source], "1 file(s) are somewhere else now")

    monkeypatch.setattr("app.mover.rename_files", fake_rename_files)
    events.clear()

    response = client.post(
        "/library/move",
        json={
            "paths": ["Bonobo/Migration/Kerala.flac"],
            "artist": "Bonobo",
            "album": "Black Sands",
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "The move failed partway: 1 file(s) are somewhere else now"
    )
    changed = [event for event in events if event.event == "library_changed"]
    assert [event.data["paths"] for event in changed] == [[""]]
    assert invalidations


def test_a_move_that_changed_nothing_emits_nothing(client_and_queue, root):
    client, _manager, events = client_and_queue

    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Black Sands/Kiara.flac"], "artist": "Bonobo", "album": "Black Sands"},
    )

    assert response.status_code == 200
    assert response.json()["moved"] == []
    assert [event for event in events if event.event == "library_changed"] == []


def test_the_library_scan_shows_the_move_immediately(client, root):
    before = client.get("/library").json()
    assert [artist["name"] for artist in before["artists"]] == ["Bonobo"]

    client.post("/library/move", json={"path": "Bonobo", "artist": "Bonobo (UK)"})

    after = client.get("/library").json()
    assert [artist["name"] for artist in after["artists"]] == ["Bonobo (UK)"]


# ---------------------------------------------------------------------------
# The cover cache prune
# ---------------------------------------------------------------------------


def test_a_scan_prunes_covers_for_albums_that_are_gone(client, root, isolated_paths):
    _download_dir, data_dir = isolated_paths
    covers = library.cover_cache_dir(Path(data_dir))

    assert client.get("/library/cover", params={"path": "Bonobo/Black Sands"}).status_code == 200
    assert client.get("/library/cover", params={"path": "Bonobo/Migration"}).status_code == 200
    assert len(list(covers.iterdir())) == 2

    client.post("/library/move", json={"path": "Bonobo/Black Sands", "artist": "Ninja Tune"})
    client.get("/library")

    remaining = list(covers.iterdir())
    assert len(remaining) == 1
    # The one left is Migration's: Black Sands lives at a new path now, whose
    # cache key is a different hash, so its old entry can never be hit again.
    assert remaining[0].name.startswith(library._cache_key("Bonobo/Migration"))


def test_the_prune_leaves_covers_for_albums_that_are_still_there(client, root, isolated_paths):
    _download_dir, data_dir = isolated_paths
    covers = library.cover_cache_dir(Path(data_dir))

    client.get("/library/cover", params={"path": "Bonobo/Black Sands"})
    library.invalidate()
    client.get("/library")

    assert len(list(covers.iterdir())) == 1


# ---------------------------------------------------------------------------
# Destinations that are not what they look like
# ---------------------------------------------------------------------------


def _case_insensitive(root: Path) -> bool:
    """Whether *root* is on a filesystem that cannot tell 'a' from 'A'."""
    probe = root / ".case-probe"
    probe.mkdir()
    try:
        return (root / ".CASE-PROBE").exists()
    finally:
        probe.rmdir()


def test_a_symlinked_destination_artist_is_refused(client, root, tmp_path):
    """The source is validated as a path; the destination is built from a name.

    An existing folder is adopted exactly as it is, and that folder may be a
    symlink pointing anywhere -- so the destination gets its own resolve check.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "Escape").symlink_to(outside)
    before = rel_paths(root)

    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Flashlight.flac"], "artist": "Escape"},
    )

    assert response.status_code == 400
    assert rel_paths(root) == before
    assert list(outside.iterdir()) == []


def test_a_symlinked_destination_artist_is_refused_for_an_album(client, root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "Escape").symlink_to(outside)
    before = rel_paths(root)

    response = client.post(
        "/library/move", json={"path": "Bonobo/Migration", "artist": "Escape"}
    )

    assert response.status_code == 400
    assert rel_paths(root) == before
    assert list(outside.iterdir()) == []


def test_a_symlinked_destination_album_is_refused(client, root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "Bonobo" / "Escape").symlink_to(outside)
    before = rel_paths(root)

    response = client.post(
        "/library/move",
        json={
            "paths": ["Bonobo/Flashlight.flac"],
            "artist": "Bonobo",
            "album": "Escape",
        },
    )

    assert response.status_code == 400
    assert rel_paths(root) == before
    assert list(outside.iterdir()) == []


def test_a_dangling_symlink_out_of_the_root_is_refused_as_a_destination(
    client, root, tmp_path
):
    (root / "Gone").symlink_to(tmp_path / "not-there")

    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Flashlight.flac"], "artist": "Gone"},
    )

    assert response.status_code in (400, 409)
    assert (root / "Bonobo" / "Flashlight.flac").is_file()


def test_a_symlinked_destination_inside_the_root_is_allowed(client, root):
    """A symlink is only a problem when it leaves the library."""
    (root / "Real").mkdir()
    (root / "Alias").symlink_to(root / "Real")

    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Flashlight.flac"], "artist": "Alias"},
    )

    assert response.status_code == 200
    assert (root / "Real" / "Flashlight.flac").is_file()


# ---------------------------------------------------------------------------
# A file where a folder should be
# ---------------------------------------------------------------------------


def test_a_file_where_the_artist_folder_should_go_refuses_a_track_move(client, root):
    (root / "Blocker").write_bytes(b"not a folder")
    before = rel_paths(root)

    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Flashlight.flac"], "artist": "Blocker"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"] == ["Blocker"]
    assert rel_paths(root) == before


def test_a_file_where_the_artist_folder_should_go_refuses_an_album_move(client, root):
    (root / "Blocker").write_bytes(b"not a folder")
    before = rel_paths(root)

    response = client.post(
        "/library/move", json={"path": "Bonobo/Migration", "artist": "Blocker"}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"] == ["Blocker"]
    assert rel_paths(root) == before


def test_a_file_where_the_album_folder_should_go_is_a_conflict(client, root):
    (root / "Bonobo" / "Blocker").write_bytes(b"not a folder")
    before = rel_paths(root)

    response = client.post(
        "/library/move",
        json={
            "paths": ["Bonobo/Flashlight.flac"],
            "artist": "Bonobo",
            "album": "Blocker",
        },
    )

    assert response.status_code == 409
    assert rel_paths(root) == before


def test_a_file_named_like_a_source_subfolder_refuses_the_merge(client, root):
    write_flac(root / "Bonobo" / "Migration" / "Disc 1" / "Deep.flac", title="Deep")
    (root / "Bonobo" / "Black Sands" / "Disc 1").write_bytes(b"not a folder")
    before = rel_paths(root)

    response = client.post(
        "/library/move",
        json={
            "path": "Bonobo/Migration",
            "artist": "Bonobo",
            "album": "Black Sands",
        },
    )

    assert response.status_code == 409
    assert "Bonobo/Black Sands/Disc 1" in response.json()["detail"]["conflicts"]
    assert rel_paths(root) == before


def test_a_directory_where_the_source_has_a_cover_refuses_the_merge(client, root):
    (root / "Bonobo" / "Migration" / "cover.jpg").write_bytes(TINY_JPEG)
    (root / "Bonobo" / "Black Sands" / "cover.jpg").unlink()
    (root / "Bonobo" / "Black Sands" / "cover.jpg").mkdir()
    before = rel_paths(root)

    response = client.post(
        "/library/move",
        json={
            "path": "Bonobo/Migration",
            "artist": "Bonobo",
            "album": "Black Sands",
        },
    )

    assert response.status_code == 409
    assert rel_paths(root) == before


def test_a_dangling_symlink_at_the_target_is_a_conflict(client, root):
    """It occupies the name as firmly as a file does, and ``exists()`` says no."""
    (root / "Bonobo" / "Black Sands" / "Flashlight.flac").symlink_to(
        root / "Bonobo" / "nowhere.flac"
    )

    response = client.post(
        "/library/move",
        json={
            "paths": ["Bonobo/Flashlight.flac"],
            "artist": "Bonobo",
            "album": "Black Sands",
        },
    )

    assert response.status_code == 409
    assert (root / "Bonobo" / "Flashlight.flac").is_file()


# ---------------------------------------------------------------------------
# Spelling, on a filesystem that may not care about it
# ---------------------------------------------------------------------------


def test_a_track_move_to_another_spelling_of_an_existing_artist_is_a_no_op(
    client, root
):
    """The folder on disk wins.

    Creating "bonobo" beside "Bonobo" either makes a second folder the user did
    not ask for or, on a case-insensitive filesystem, silently files into the
    existing one while the tags and the response claim the new spelling.
    """
    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Flashlight.flac"], "artist": "bonobo"},
    )

    assert response.status_code == 200
    assert response.json()["moved"] == []
    assert {entry.name for entry in root.iterdir() if entry.is_dir()} == {"Bonobo"}
    assert tags_of(root / "Bonobo" / "Flashlight.flac")["ALBUMARTIST"] == ["Bonobo"]


def test_an_album_move_to_another_spelling_of_an_existing_artist_stays_put(
    client, root
):
    response = client.post(
        "/library/move", json={"path": "Bonobo/Migration", "artist": "bonobo"}
    )

    assert response.status_code == 200
    assert response.json()["moved"] == []
    assert {entry.name for entry in root.iterdir() if entry.is_dir()} == {"Bonobo"}
    assert (root / "Bonobo" / "Migration" / "Kerala.flac").is_file()


def test_an_exact_folder_name_wins_over_a_case_insensitive_match(client, root):
    if _case_insensitive(root):
        pytest.skip("needs a case-sensitive filesystem to hold both spellings")
    (root / "bonobo").mkdir()

    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Flashlight.flac"], "artist": "bonobo"},
    )

    assert response.status_code == 200
    assert (root / "bonobo" / "Flashlight.flac").is_file()


@pytest.mark.parametrize(
    "name", [".trash", ".TRASH", ".tmp", ".hidden", "...", ".config"]
)
def test_a_reserved_album_name_is_refused(client, root, name):
    before = rel_paths(root)

    response = client.post(
        "/library/move",
        json={"paths": ["Bonobo/Flashlight.flac"], "artist": "Bonobo", "album": name},
    )

    assert response.status_code == 400
    assert rel_paths(root) == before


def test_more_paths_than_a_move_may_carry_are_refused(client):
    response = client.post(
        "/library/move",
        json={"paths": [f"Bonobo/{n}.flac" for n in range(1001)], "artist": "X"},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Tags that must not follow the folder
# ---------------------------------------------------------------------------


def test_moving_and_renaming_an_album_keeps_a_guest_artist(client, root):
    write_flac(
        root / "Bonobo" / "Migration" / "Guest.flac",
        title="Break Apart",
        artist="Rhye",
        albumartist="Bonobo",
        album="Migration",
    )

    response = client.post(
        "/library/move",
        json={
            "path": "Bonobo/Migration",
            "artist": "Ninja Tune",
            "album": "Migration (2017)",
        },
    )

    assert response.status_code == 200
    guest = tags_of(root / "Ninja Tune" / "Migration (2017)" / "Guest.flac")
    assert guest["ARTIST"] == ["Rhye"]
    assert guest["ALBUMARTIST"] == ["Ninja Tune"]
    assert guest["ALBUM"] == ["Migration (2017)"]
    # The track whose ARTIST did agree with the folder does follow it.
    assert tags_of(root / "Ninja Tune" / "Migration (2017)" / "Kerala.flac")[
        "ARTIST"
    ] == ["Ninja Tune"]


def test_renaming_an_artist_folder_with_no_audio_emits_library_changed(
    client_and_queue, root
):
    """``changed`` is what the rescan and the event are about, not ``moved``."""
    client, _manager, events = client_and_queue
    (root / "Empty Artist").mkdir()

    response = client.post(
        "/library/move", json={"path": "Empty Artist", "artist": "Renamed"}
    )

    assert response.status_code == 200
    assert response.json()["moved"] == []
    assert (root / "Renamed").is_dir()
    changed = [event for event in events if event.event == "library_changed"]
    assert len(changed) == 1
    assert set(changed[0].data["paths"]) == {"Empty Artist", "Renamed"}


# ---------------------------------------------------------------------------
# The reserved folders
# ---------------------------------------------------------------------------


def test_a_track_in_a_reserved_folder_cannot_be_moved_out(client, root):
    """``.tmp`` holds half-written downloads, and a *file* in it was movable.

    The reserved check only ran on the directory branch, so a path naming a
    file under ``.tmp`` or ``.trash`` walked straight into the track move --
    handing the user a partial download, or lifting a track back out of the
    trash without going through restore.
    """
    source = root / ".tmp" / "job-1" / "half.flac"
    write_flac(source, title="Half")
    before = rel_paths(root)

    response = client.post(
        "/library/move", json={"path": ".tmp/job-1/half.flac", "artist": "Bonobo"}
    )

    assert response.status_code == 400
    assert "not part of the library" in response.json()["detail"]
    assert rel_paths(root) == before


def test_tracks_listed_in_a_reserved_folder_are_refused(client, root):
    """The ``paths`` form is checked the same way, before anything is moved."""
    write_flac(root / ".trash" / "Bonobo" / "Black Sands" / "Kong.flac", title="Kong")
    before = rel_paths(root)

    response = client.post(
        "/library/move",
        json={
            "paths": [".trash/Bonobo/Black Sands/Kong.flac"],
            "artist": "Bonobo",
            "album": "Black Sands",
        },
    )

    assert response.status_code == 400
    assert "not part of the library" in response.json()["detail"]
    assert rel_paths(root) == before


def test_a_reserved_folder_spelled_in_another_case_is_still_reserved(client, root):
    """``.TRASH`` is the same folder as ``.trash`` where case does not count.

    Spelled that way the source passed every guard, and the cleanup that
    climbs out of an emptied folder then removed the trash root itself.
    """
    source = root / ".TRASH" / "x" / "a.flac"
    write_flac(source, title="A")

    response = client.post(
        "/library/move", json={"path": ".TRASH/x/a.flac", "artist": "Bonobo"}
    )

    assert response.status_code == 400
    assert source.is_file()
    assert (root / ".TRASH").is_dir()


@pytest.mark.parametrize(
    ("album", "message"),
    [
        # Caught by the leading-dot rule before the sanitiser ever sees it.
        (".\x7f", "must not start with a dot"),
        # Sanitises to ".", a reserved name, whose album fallback is NO_ALBUM
        # -- so the name comes back empty, which every caller reads as
        # "no album".
        ("\x7f.", "usable folder name"),
    ],
)
def test_an_album_name_that_sanitises_to_nothing_is_refused(
    client, root, album, message
):
    """A name the sanitiser empties is a 400, not a 200.

    An empty result is what every caller reads as "no album": the album quietly
    became a Single and the response said 200.
    """
    before = rel_paths(root)

    response = client.post(
        "/library/move",
        json={
            "paths": ["Bonobo/Black Sands/Kiara.flac"],
            "artist": "Bonobo",
            "album": album,
        },
    )

    assert response.status_code == 400
    assert message in response.json()["detail"]
    assert rel_paths(root) == before


def test_cleanup_removes_a_folder_holding_a_symlink_to_a_directory(client, root):
    """``os.walk`` lists a symlinked directory among the dirnames.

    ``rmdir`` on one raises ENOTDIR, which aborted the whole cleanup and left
    the emptied album standing.  Unlinking removes the link alone.
    """
    elsewhere = root / "Elsewhere"
    write_flac(elsewhere / "Kept.flac", title="Kept")
    link = root / "Bonobo" / "Migration" / "extras"
    try:
        link.symlink_to(elsewhere, target_is_directory=True)
    except OSError:  # pragma: no cover - platform without symlinks
        pytest.skip("this filesystem does not support symlinks")

    response = client.post(
        "/library/move",
        json={
            "paths": ["Bonobo/Migration/Kerala.flac"],
            "artist": "Bonobo",
            "album": "Black Sands",
        },
    )

    assert response.status_code == 200
    assert "Bonobo/Migration" in response.json()["removed"]
    assert not (root / "Bonobo" / "Migration").exists()
    # Only the link went; what it pointed at is a real part of the library.
    assert (elsewhere / "Kept.flac").is_file()


def test_the_prune_leaves_the_cache_alone_when_the_scan_found_no_artists(
    isolated_paths,
):
    """A zero-artist scan is an unmounted root far more often than an empty library."""
    _download_dir, data_dir = isolated_paths
    cache = library.cover_cache_dir(data_dir)
    cache.mkdir(parents=True, exist_ok=True)
    entry = cache / "deadbeef-1"
    entry.write_bytes(TINY_JPEG)

    assert library.prune_cover_cache({"artists": []}, data_dir) == 0
    assert entry.is_file()


def test_the_prune_reclaims_an_abandoned_temp_cover(isolated_paths):
    """A ``tmp-`` file from a killed process must not sit in the cache forever.

    A fresh one is still somebody's in-flight write and is left alone; an old
    one belongs to nobody, because the gap between ``mkstemp`` and
    ``os.replace`` is milliseconds.
    """
    _download_dir, data_dir = isolated_paths
    cache = library.cover_cache_dir(data_dir)
    cache.mkdir(parents=True, exist_ok=True)
    fresh = cache / "tmp-fresh.tmp"
    fresh.write_bytes(TINY_JPEG)
    stale = cache / "tmp-stale.tmp"
    stale.write_bytes(TINY_JPEG)
    old = time.time() - library.TEMP_CACHE_MAX_AGE_SECONDS - 60
    os.utime(stale, (old, old))

    payload = {"artists": [{"albums": [{"path": "Bonobo/Black Sands"}]}]}
    assert library.prune_cover_cache(payload, data_dir) == 1
    assert fresh.is_file()
    assert not stale.exists()


def test_a_failed_case_only_rename_leaves_a_visible_folder(root, monkeypatch):
    """The staging name must never be dot-prefixed.

    A case-only rename goes through a temporary name because the filesystem
    treats the one-step version as a no-op.  When the second rename fails the
    folder is left standing under that name -- and under a dot-prefixed one it
    was hidden from the scanner, from Navidrome and from Lidarr, so the user's
    album had silently vanished from every view they have of it.
    """
    if not _case_insensitive(root):
        pytest.skip("a case-only rename is a single rename here, with no staging name")

    real_rename = os.rename
    calls = []

    def failing_rename(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real_rename(source, target)

    monkeypatch.setattr(library_ops.os, "rename", failing_rename)

    source = root / "Bonobo"
    with pytest.raises(OSError):
        library_ops.rename_folder(source, root / "BONOBO", root)

    leftovers = [entry.name for entry in root.iterdir() if entry.is_dir()]
    staged = [name for name in leftovers if ".moving-" in name]
    assert staged, f"expected a staging folder, got {leftovers}"
    assert not any(name.startswith(".") for name in staged)
    # And it is a folder the scanner can see, which is the whole point.
    assert not library._is_hidden(staged[0])
