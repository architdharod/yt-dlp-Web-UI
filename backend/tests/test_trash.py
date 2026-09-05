"""Tests for the trash: delete, list, restore and empty.

Every acceptance criterion of the "Delete, Trash tab, Restore, Empty trash"
phase has a test here: an album that comes back with its ``cover.jpg`` intact,
a restore that refuses a collision without moving anything, a trash that is
invisible to both the library scan and the duplicate check, the ``.ndignore``
marker, the in-flight guard on both delete and restore, the empty-folder
cleanup after a track delete, and one ``library_changed`` per action.

The tree is the move tests' tree, built with :func:`tests.test_library.write_flac`
so the tag assertions are made against FLACs mutagen really wrote.
"""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.flac import FLAC

from app import library
from app.models import Job, JobKind, JobStatus, SSEEvent
from app.queue_manager import QueueManager
from tests.conftest import TINY_JPEG
from tests.test_library import write_flac


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def build_tree(root: Path) -> None:
    """One artist with two albums, a Single, and a stray file at the root."""
    write_flac(
        root / "Bonobo" / "Black Sands" / "Kiara.flac",
        title="Kiara",
        artist="Bonobo",
        albumartist="Bonobo",
        album="Black Sands",
        sourceid="youtube:abc123",
    )
    write_flac(
        root / "Bonobo" / "Black Sands" / "Kong.flac",
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
    write_flac(root / "Stray.flac", title="Stray", artist="Nobody")


@pytest.fixture()
def root(isolated_paths):
    download_dir, _ = isolated_paths
    build_tree(download_dir)
    library.invalidate()
    yield download_dir
    library.invalidate()


@pytest.fixture()
def client_and_queue(root):
    """A TestClient over the real app, its queue manager, and its events."""
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


def trash_dir(root: Path) -> Path:
    return root / ".trash"


def entry_dir(root: Path, entry_id: str) -> Path:
    return trash_dir(root) / entry_id


def changed_paths(events: list[SSEEvent]) -> list[list[str]]:
    return [
        event.data["paths"] for event in events if event.event == "library_changed"
    ]


def rel_paths(root: Path) -> set[str]:
    """Every file in the library proper, with the trash left out of it."""
    return {
        rel
        for dirpath, _dirs, files in os.walk(root)
        for name in files
        if not (rel := Path(dirpath, name).relative_to(root).as_posix()).startswith(
            ".trash/"
        )
    }


def tags_of(path: Path) -> dict[str, list[str]]:
    return {key.upper(): value for key, value in FLAC(path).items()}


def in_flight_job(**overrides) -> Job:
    """A running download aiming at ``Bonobo/Fragments`` unless told otherwise."""
    fields = {
        "id": "job-1",
        "url": "https://youtube.com/watch?v=1",
        "status": JobStatus.DOWNLOADING,
        "kind": JobKind.DOWNLOAD,
        "target_dir": "Bonobo/Fragments",
        "target_guessed": False,
    }
    fields.update(overrides)
    return Job(**fields)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_deleting_a_track_moves_it_into_a_trash_entry(client, root):
    response = client.post(
        "/library/delete", json={"path": "Bonobo/Black Sands/Kiara.flac"}
    )

    assert response.status_code == 200
    entry = response.json()["entry"]
    assert entry["kind"] == "track"
    assert entry["path"] == "Bonobo/Black Sands/Kiara.flac"
    assert entry["paths"] == ["Bonobo/Black Sands/Kiara.flac"]
    assert entry["track_count"] == 1
    assert entry["deleted_at"].endswith("Z")

    assert not (root / "Bonobo" / "Black Sands" / "Kiara.flac").exists()
    trashed = entry_dir(root, entry["id"]) / "Bonobo" / "Black Sands" / "Kiara.flac"
    assert trashed.is_file()
    # Renamed, not copied: the tags travelled untouched.
    assert tags_of(trashed)["SOURCEID"] == ["youtube:abc123"]


def test_deleting_several_tracks_makes_one_entry(client, root):
    response = client.post(
        "/library/delete",
        json={
            "paths": [
                "Bonobo/Black Sands/Kiara.flac",
                "Bonobo/Black Sands/Kong.flac",
            ]
        },
    )

    assert response.status_code == 200
    entry = response.json()["entry"]
    assert entry["kind"] == "tracks"
    assert entry["path"] == "Bonobo/Black Sands"
    assert entry["track_count"] == 2
    assert len(entry["paths"]) == 2
    # The album folder went with them: nothing but a cover was left in it.
    assert response.json()["removed"] == ["Bonobo/Black Sands"]
    assert not (root / "Bonobo" / "Black Sands").exists()


def test_deleting_tracks_from_two_folders_is_refused(client, root):
    response = client.post(
        "/library/delete",
        json={
            "paths": [
                "Bonobo/Black Sands/Kiara.flac",
                "Bonobo/Migration/Kerala.flac",
            ]
        },
    )

    assert response.status_code == 400
    assert (root / "Bonobo" / "Black Sands" / "Kiara.flac").is_file()


def test_deleting_an_album_carries_the_cover_with_it(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})

    assert response.status_code == 200
    entry = response.json()["entry"]
    assert entry["kind"] == "album"
    assert entry["path"] == "Bonobo/Black Sands"
    assert entry["track_count"] == 2

    moved = entry_dir(root, entry["id"]) / "Bonobo" / "Black Sands"
    assert (moved / "cover.jpg").read_bytes() == TINY_JPEG
    assert (moved / "Kiara.flac").is_file()
    assert not (root / "Bonobo" / "Black Sands").exists()
    # The artist still has other music, so it stays.
    assert (root / "Bonobo").is_dir()


def test_deleting_the_last_album_cleans_up_the_artist(client, root):
    client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    client.post("/library/delete", json={"path": "Bonobo/Flashlight.flac"})
    response = client.post("/library/delete", json={"path": "Bonobo/Migration"})

    assert response.status_code == 200
    assert response.json()["removed"] == ["Bonobo"]
    assert not (root / "Bonobo").exists()


def test_deleting_an_artist_takes_the_whole_folder(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo"})

    assert response.status_code == 200
    entry = response.json()["entry"]
    assert entry["kind"] == "artist"
    assert entry["track_count"] == 4
    assert not (root / "Bonobo").exists()
    assert (entry_dir(root, entry["id"]) / "Bonobo" / "Migration" / "Kerala.flac").is_file()


def test_deleting_a_root_level_file_works(client, root):
    response = client.post("/library/delete", json={"path": "Stray.flac"})

    assert response.status_code == 200
    assert response.json()["entry"]["kind"] == "track"
    assert not (root / "Stray.flac").exists()


def test_two_deletes_get_two_entries(client, root):
    first = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    second = client.post("/library/delete", json={"path": "Bonobo/Migration"})

    ids = {first.json()["entry"]["id"], second.json()["entry"]["id"]}
    assert len(ids) == 2
    assert len(list(trash_dir(root).glob("*/"))) == 2


def test_the_first_delete_writes_an_ndignore(client, root):
    client.post("/library/delete", json={"path": "Bonobo/Flashlight.flac"})

    assert (trash_dir(root) / ".ndignore").is_file()


def test_the_delete_writes_a_manifest(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    entry = response.json()["entry"]

    manifest = json.loads(
        (entry_dir(root, entry["id"]) / "entry.json").read_text("utf-8")
    )
    assert manifest["kind"] == "album"
    assert manifest["paths"] == ["Bonobo/Black Sands"]
    assert manifest["id"] == entry["id"]


def test_a_missing_path_is_404(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo/Nothing"})
    assert response.status_code == 404


def test_a_path_deeper_than_an_album_is_refused(client, root):
    (root / "Bonobo" / "Black Sands" / "Disc 1").mkdir()
    response = client.post(
        "/library/delete", json={"path": "Bonobo/Black Sands/Disc 1"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "path",
    ["../outside", "/etc/passwd", "Bonobo/../../escape", "Bonobo\\Black Sands"],
)
def test_paths_that_escape_the_library_are_refused(client, root, path):
    response = client.post("/library/delete", json={"path": path})
    assert response.status_code in (400, 404)


def test_a_symlink_out_of_the_library_cannot_be_trashed(client, root, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.flac").write_bytes(b"x")
    (root / "Bonobo" / "link.flac").symlink_to(outside / "secret.flac")

    response = client.post("/library/delete", json={"path": "Bonobo/link.flac"})

    assert response.status_code == 400
    assert (outside / "secret.flac").is_file()


def test_a_symlink_inside_the_library_is_trashed_as_a_link(client, root):
    (root / "Bonobo" / "link.flac").symlink_to(root / "Bonobo" / "Flashlight.flac")

    response = client.post("/library/delete", json={"path": "Bonobo/link.flac"})

    assert response.status_code == 200
    entry_id = response.json()["entry"]["id"]
    trashed = entry_dir(root, entry_id) / "Bonobo" / "link.flac"
    assert trashed.is_symlink()
    # The file it pointed at is still in the library: the link moved, not it.
    assert (root / "Bonobo" / "Flashlight.flac").is_file()


@pytest.mark.parametrize("path", [".trash", ".trash/whatever", ".tmp/job/x.flac"])
def test_reserved_folders_cannot_be_deleted(client, root, path):
    (root / ".trash").mkdir(exist_ok=True)
    (root / ".tmp" / "job").mkdir(parents=True, exist_ok=True)
    (root / ".tmp" / "job" / "x.flac").write_bytes(b"x")

    response = client.post("/library/delete", json={"path": path})
    assert response.status_code == 400


def test_giving_neither_path_nor_paths_is_refused(client, root):
    neither = client.post("/library/delete", json={})
    assert neither.status_code == 400
    assert neither.json()["detail"] == "give either 'path' or 'paths'"

    both = client.post(
        "/library/delete", json={"path": "Bonobo", "paths": ["Bonobo/Flashlight.flac"]}
    )
    assert both.status_code == 400
    assert both.json()["detail"] == "give 'path' or 'paths', not both"


def test_a_delete_whose_rollback_fails_keeps_the_tracks_in_the_trash(
    client, root, monkeypatch
):
    """The one failure mode that used to unlink audio: a rollback that fails.

    The third rename into the trash fails, and putting the first one back
    fails too -- so that track is in the entry and nowhere else.  The entry
    has to survive, the response has to say so, and nothing may be unlinked.
    """
    write_flac(
        root / "Bonobo" / "Black Sands" / "Animals.flac",
        title="Animals",
        artist="Bonobo",
        album="Black Sands",
    )
    real_rename = os.rename
    breaking = True

    def fake_rename(source, target):
        target_str = str(target)
        into_trash = ".trash" in target_str
        if not breaking:
            return real_rename(source, target)
        if into_trash and target_str.endswith("Animals.flac"):
            raise OSError(28, "No space left on device")
        if not into_trash and target_str.endswith("Kiara.flac"):
            raise OSError(13, "Permission denied")
        return real_rename(source, target)

    monkeypatch.setattr("app.library_ops.os.rename", fake_rename)

    response = client.post(
        "/library/delete",
        json={
            "paths": [
                "Bonobo/Black Sands/Kiara.flac",
                "Bonobo/Black Sands/Kong.flac",
                "Bonobo/Black Sands/Animals.flac",
            ]
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "The delete failed partway: 1 track(s) could not be put back and are "
        "in the Trash tab; restore them from there"
    )

    # The filesystem is well again; the entry is what has to hold up now.
    breaking = False

    # Nothing was unlinked: two tracks are back where they were, and the third
    # is in the trash entry that was kept for it.
    assert (root / "Bonobo" / "Black Sands" / "Kong.flac").is_file()
    assert (root / "Bonobo" / "Black Sands" / "Animals.flac").is_file()
    assert not (root / "Bonobo" / "Black Sands" / "Kiara.flac").exists()

    body = client.get("/library/trash").json()
    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    assert entry["paths"] == ["Bonobo/Black Sands/Kiara.flac"]
    assert entry["track_count"] == 1

    restored = client.post("/library/trash/restore", json={"id": entry["id"]})

    assert restored.status_code == 200
    assert (root / "Bonobo" / "Black Sands" / "Kiara.flac").is_file()
    assert tags_of(root / "Bonobo" / "Black Sands" / "Kiara.flac")["TITLE"] == ["Kiara"]


# ---------------------------------------------------------------------------
# The in-flight guard
# ---------------------------------------------------------------------------


def test_deleting_a_folder_a_download_targets_is_409(client_and_queue, root):
    client, manager, _events = client_and_queue
    manager._jobs["job-1"] = in_flight_job(target_dir="Bonobo/Black Sands")

    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})

    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"] == ["Bonobo/Black Sands"]
    assert (root / "Bonobo" / "Black Sands" / "Kiara.flac").is_file()


def test_deleting_a_track_whose_folder_a_download_targets_is_409(client_and_queue, root):
    client, manager, _events = client_and_queue
    manager._jobs["job-1"] = in_flight_job(target_dir="Bonobo/Black Sands")

    response = client.post(
        "/library/delete", json={"path": "Bonobo/Black Sands/Kiara.flac"}
    )

    assert response.status_code == 409


def test_a_download_elsewhere_does_not_block_a_delete(client_and_queue, root):
    client, manager, _events = client_and_queue
    manager._jobs["job-1"] = in_flight_job(target_dir="Someone Else/An Album")

    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})

    assert response.status_code == 200


def test_restoring_onto_a_folder_a_download_targets_is_409(client_and_queue, root):
    client, manager, _events = client_and_queue
    deleted = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    entry_id = deleted.json()["entry"]["id"]

    manager._jobs["job-1"] = in_flight_job(target_dir="Bonobo/Black Sands")
    response = client.post("/library/trash/restore", json={"id": entry_id})

    assert response.status_code == 409
    assert not (root / "Bonobo" / "Black Sands").exists()


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_the_trash_lists_entries_newest_first(client, root):
    first = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    second = client.post("/library/delete", json={"path": "Bonobo/Migration"})

    body = client.get("/library/trash").json()

    assert [entry["id"] for entry in body["entries"]] == [
        second.json()["entry"]["id"],
        first.json()["entry"]["id"],
    ]
    assert body["track_count"] == 3


def test_an_empty_trash_lists_nothing(client, root):
    body = client.get("/library/trash").json()
    assert body == {"entries": [], "track_count": 0}


def test_the_ndignore_is_not_an_entry(client, root):
    client.post("/library/delete", json={"path": "Bonobo/Flashlight.flac"})
    (trash_dir(root) / "stray.txt").write_text("hello")

    body = client.get("/library/trash").json()

    assert len(body["entries"]) == 1


def test_an_entry_without_a_manifest_is_still_listed(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    entry_id = response.json()["entry"]["id"]
    (entry_dir(root, entry_id) / "entry.json").unlink()

    body = client.get("/library/trash").json()

    entry = body["entries"][0]
    # Reconstructed from the files themselves: every file is its own path, so
    # a restore puts them back one by one rather than as a folder.
    assert entry["kind"] == "tracks"
    assert entry["path"] == "Bonobo/Black Sands"
    assert set(entry["paths"]) == {
        "Bonobo/Black Sands/Kiara.flac",
        "Bonobo/Black Sands/Kong.flac",
        "Bonobo/Black Sands/cover.jpg",
    }
    assert entry["track_count"] == 2


def test_a_corrupt_manifest_falls_back_to_the_files(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    entry_id = response.json()["entry"]["id"]
    (entry_dir(root, entry_id) / "entry.json").write_text("{ not json")

    body = client.get("/library/trash").json()

    assert body["entries"][0]["track_count"] == 2
    assert body["entries"][0]["kind"] == "tracks"


def test_a_manifest_with_an_escaping_path_is_ignored(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    entry_id = response.json()["entry"]["id"]
    (entry_dir(root, entry_id) / "entry.json").write_text(
        json.dumps({"kind": "album", "path": "../../etc", "paths": ["../../etc"]})
    )

    entry = client.get("/library/trash").json()["entries"][0]

    assert entry["path"] == "Bonobo/Black Sands"
    assert all(".." not in one for one in entry["paths"])


def test_a_root_level_multi_track_delete_keeps_its_manifest(client, root):
    """The library root is a real ``path`` -- the empty string -- not a reject.

    Every field comes back off the manifest, ``deleted_at`` included, which is
    how the listing tells a stamped entry from one rebuilt off the disk.
    """
    write_flac(root / "Loose.flac", title="Loose", artist="Nobody")

    response = client.post(
        "/library/delete", json={"paths": ["Stray.flac", "Loose.flac"]}
    )
    entry_id = response.json()["entry"]["id"]
    assert response.json()["entry"]["path"] == ""

    manifest = entry_dir(root, entry_id) / "entry.json"
    payload = json.loads(manifest.read_text())
    payload["deleted_at"] = "2020-01-01T00:00:00Z"
    manifest.write_text(json.dumps(payload))

    entry = client.get("/library/trash").json()["entries"][0]

    assert entry["kind"] == "tracks"
    assert entry["path"] == ""
    assert sorted(entry["paths"]) == ["Loose.flac", "Stray.flac"]
    assert entry["deleted_at"] == "2020-01-01T00:00:00Z"


def test_a_symlinked_entry_is_neither_listed_nor_restored(client, root, tmp_path):
    outside = tmp_path / "elsewhere"
    write_flac(outside / "Artist" / "Album" / "Secret.flac", title="Secret")
    trash_dir(root).mkdir(exist_ok=True)
    (trash_dir(root) / "sneaky").symlink_to(outside, target_is_directory=True)

    assert client.get("/library/trash").json()["entries"] == []

    response = client.post("/library/trash/restore", json={"id": "sneaky"})

    assert response.status_code == 404
    assert (outside / "Artist" / "Album" / "Secret.flac").is_file()
    assert not (root / "Artist").exists()


def test_a_hand_made_entry_lists_and_restores(client, root):
    made = trash_dir(root) / "by-hand"
    write_flac(made / "Hands" / "Album" / "Song.flac", title="Song", artist="Hands")

    entry = client.get("/library/trash").json()["entries"][0]
    assert entry["id"] == "by-hand"
    assert entry["track_count"] == 1

    response = client.post("/library/trash/restore", json={"id": "by-hand"})

    assert response.status_code == 200
    assert (root / "Hands" / "Album" / "Song.flac").is_file()
    assert not made.exists()


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def test_restoring_an_album_brings_back_the_identical_folder(client, root):
    before = rel_paths(root)
    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    entry_id = response.json()["entry"]["id"]

    restored = client.post("/library/trash/restore", json={"id": entry_id})

    assert restored.status_code == 200
    assert restored.json()["restored"] == [
        {"source": f"{entry_id}/Bonobo/Black Sands", "target": "Bonobo/Black Sands"}
    ]
    assert rel_paths(root) == before
    assert (root / "Bonobo" / "Black Sands" / "cover.jpg").read_bytes() == TINY_JPEG
    # The entry is gone, manifest and all.
    assert not entry_dir(root, entry_id).exists()


def test_restoring_a_track_recreates_the_album_folder(client, root):
    client.post(
        "/library/delete",
        json={
            "paths": [
                "Bonobo/Black Sands/Kiara.flac",
                "Bonobo/Black Sands/Kong.flac",
            ]
        },
    )
    entry_id = client.get("/library/trash").json()["entries"][0]["id"]
    assert not (root / "Bonobo" / "Black Sands").exists()

    response = client.post("/library/trash/restore", json={"id": entry_id})

    assert response.status_code == 200
    assert (root / "Bonobo" / "Black Sands" / "Kiara.flac").is_file()
    assert (root / "Bonobo" / "Black Sands" / "Kong.flac").is_file()


def test_restoring_an_artist_brings_the_whole_tree_back(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo"})
    entry_id = response.json()["entry"]["id"]

    assert client.post("/library/trash/restore", json={"id": entry_id}).status_code == 200
    assert (root / "Bonobo" / "Migration" / "Kerala.flac").is_file()
    assert (root / "Bonobo" / "Flashlight.flac").is_file()


def test_restoring_onto_an_occupied_path_is_409_and_moves_nothing(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    entry_id = response.json()["entry"]["id"]
    # Something else takes the name while the album is in the trash.
    write_flac(
        root / "Bonobo" / "Black Sands" / "Something.flac", title="Something"
    )

    restored = client.post("/library/trash/restore", json={"id": entry_id})

    assert restored.status_code == 409
    assert restored.json()["detail"]["conflicts"] == ["Bonobo/Black Sands"]
    assert (entry_dir(root, entry_id) / "Bonobo" / "Black Sands" / "Kiara.flac").is_file()
    assert not (root / "Bonobo" / "Black Sands" / "Kiara.flac").exists()


def test_a_track_restore_reports_every_collision_and_moves_nothing(client, root):
    client.post(
        "/library/delete",
        json={
            "paths": [
                "Bonobo/Black Sands/Kiara.flac",
                "Bonobo/Black Sands/Kong.flac",
            ]
        },
    )
    entry_id = client.get("/library/trash").json()["entries"][0]["id"]
    write_flac(root / "Bonobo" / "Black Sands" / "Kiara.flac", title="Impostor")
    write_flac(root / "Bonobo" / "Black Sands" / "Kong.flac", title="Impostor")

    response = client.post("/library/trash/restore", json={"id": entry_id})

    assert response.status_code == 409
    assert sorted(response.json()["detail"]["conflicts"]) == [
        "Bonobo/Black Sands/Kiara.flac",
        "Bonobo/Black Sands/Kong.flac",
    ]
    assert tags_of(root / "Bonobo" / "Black Sands" / "Kiara.flac")["TITLE"] == ["Impostor"]


def test_restoring_to_another_artist_rewrites_the_tags(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    entry_id = response.json()["entry"]["id"]

    restored = client.post(
        "/library/trash/restore",
        json={"id": entry_id, "artist": "Bonobo Remixes", "album": "Black Sands Remixed"},
    )

    assert restored.status_code == 200
    moved = root / "Bonobo Remixes" / "Black Sands Remixed" / "Kiara.flac"
    assert moved.is_file()
    tags = tags_of(moved)
    assert tags["ALBUMARTIST"] == ["Bonobo Remixes"]
    assert tags["ARTIST"] == ["Bonobo Remixes"]
    assert tags["ALBUM"] == ["Black Sands Remixed"]
    assert tags["SOURCEID"] == ["youtube:abc123"]


def test_restoring_a_track_as_a_single_clears_the_album(client, root):
    client.post("/library/delete", json={"path": "Bonobo/Black Sands/Kiara.flac"})
    entry_id = client.get("/library/trash").json()["entries"][0]["id"]

    response = client.post(
        "/library/trash/restore", json={"id": entry_id, "artist": "Bonobo", "album": ""}
    )

    assert response.status_code == 200
    moved = root / "Bonobo" / "Kiara.flac"
    assert moved.is_file()
    assert "ALBUM" not in tags_of(moved)
    assert tags_of(moved)["ALBUMARTIST"] == ["Bonobo"]


def test_restoring_an_album_to_a_new_artist_keeps_its_name(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    entry_id = response.json()["entry"]["id"]

    client.post(
        "/library/trash/restore", json={"id": entry_id, "artist": "Ninja Tune"}
    )

    assert (root / "Ninja Tune" / "Black Sands" / "Kiara.flac").is_file()
    assert tags_of(root / "Ninja Tune" / "Black Sands" / "Kiara.flac")["ALBUM"] == [
        "Black Sands"
    ]


def test_restoring_a_root_level_track_under_an_artist_rewrites_the_artist(client, root):
    """A stray track sat under no artist folder, so ARTIST follows the restore.

    Exactly what moving the same track does: there is no folder its ARTIST
    could have disagreed with, so the new artist is written unconditionally
    rather than left as whatever the file came in with.
    """
    client.post("/library/delete", json={"path": "Stray.flac"})
    entry_id = client.get("/library/trash").json()["entries"][0]["id"]

    response = client.post(
        "/library/trash/restore", json={"id": entry_id, "artist": "Zed"}
    )

    assert response.status_code == 200
    moved = root / "Zed" / "Stray.flac"
    assert moved.is_file()
    tags = tags_of(moved)
    assert tags["ALBUMARTIST"] == ["Zed"]
    assert tags["ARTIST"] == ["Zed"]


def test_restoring_an_album_into_an_existing_one_merges_it(client, root):
    """Restore-elsewhere merges exactly as ``POST /library/move`` does.

    The dialog behind it opens to resolve a 409; refusing it with a second one
    because the album folder exists would leave the user nowhere to go.
    """
    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    entry_id = response.json()["entry"]["id"]
    write_flac(
        root / "Ninja Tune" / "Black Sands" / "Recurring.flac",
        title="Recurring",
        artist="Bonobo",
        album="Black Sands",
    )
    (root / "Ninja Tune" / "Black Sands" / "cover.jpg").write_bytes(b"already here")

    restored = client.post(
        "/library/trash/restore", json={"id": entry_id, "artist": "Ninja Tune"}
    )

    assert restored.status_code == 200
    destination = root / "Ninja Tune" / "Black Sands"
    assert sorted(one.name for one in destination.iterdir()) == [
        "Kiara.flac",
        "Kong.flac",
        "Recurring.flac",
        "cover.jpg",
    ]
    tags = tags_of(destination / "Kiara.flac")
    assert tags["ALBUMARTIST"] == ["Ninja Tune"]
    assert tags["ARTIST"] == ["Ninja Tune"]
    assert tags["ALBUM"] == ["Black Sands"]
    # The album that was already there kept its own cover, and the duplicate
    # went with the entry -- the mover leaves a duplicate sidecar behind too.
    assert (destination / "cover.jpg").read_bytes() == b"already here"
    assert not entry_dir(root, entry_id).exists()


def test_merging_a_restored_album_onto_a_taken_name_is_409(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    entry_id = response.json()["entry"]["id"]
    write_flac(root / "Ninja Tune" / "Black Sands" / "Kiara.flac", title="Impostor")

    restored = client.post(
        "/library/trash/restore", json={"id": entry_id, "artist": "Ninja Tune"}
    )

    assert restored.status_code == 409
    assert restored.json()["detail"]["conflicts"] == ["Ninja Tune/Black Sands/Kiara.flac"]
    # Nothing moved: the entry still holds both tracks and the album that was
    # in the way is untouched.
    trashed = entry_dir(root, entry_id) / "Bonobo" / "Black Sands"
    assert (trashed / "Kiara.flac").is_file()
    assert (trashed / "Kong.flac").is_file()
    assert tags_of(root / "Ninja Tune" / "Black Sands" / "Kiara.flac")["TITLE"] == [
        "Impostor"
    ]
    assert not (root / "Ninja Tune" / "Black Sands" / "Kong.flac").exists()


def test_restoring_an_artist_under_a_new_name_leaves_the_albums_alone(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo"})
    entry_id = response.json()["entry"]["id"]

    client.post("/library/trash/restore", json={"id": entry_id, "artist": "Simon Green"})

    moved = root / "Simon Green" / "Black Sands" / "Kiara.flac"
    assert moved.is_file()
    tags = tags_of(moved)
    assert tags["ALBUMARTIST"] == ["Simon Green"]
    assert tags["ALBUM"] == ["Black Sands"]


def test_restoring_an_artist_onto_a_recreated_folder_merges_it(client, root):
    """An artist entry merges too, and for the same reason an album's does.

    Lidarr recreating the artist folder while the delete sits in the trash is
    the ordinary case, and refusing the restore with a 409 the dialog cannot
    answer would strand the user's music in the Trash tab.
    """
    write_flac(
        root / "Bonobo" / "Migration" / "Disc 2" / "Break Apart.flac",
        title="Break Apart",
        artist="Bonobo",
        album="Migration",
    )
    response = client.post("/library/delete", json={"path": "Bonobo"})
    entry_id = response.json()["entry"]["id"]
    # Something puts the artist back while the entry is in the trash.
    write_flac(
        root / "Bonobo" / "Fragments" / "Rosewood.flac",
        title="Rosewood",
        artist="Bonobo",
        album="Fragments",
    )
    (root / "Bonobo" / "Black Sands").mkdir(parents=True)
    (root / "Bonobo" / "Black Sands" / "cover.jpg").write_bytes(b"already here")

    restored = client.post(
        "/library/trash/restore", json={"id": entry_id, "artist": "Bonobo"}
    )

    assert restored.status_code == 200
    assert rel_paths(root) == {
        "Bonobo/Black Sands/Kiara.flac",
        "Bonobo/Black Sands/Kong.flac",
        "Bonobo/Black Sands/cover.jpg",
        "Bonobo/Migration/Kerala.flac",
        "Bonobo/Migration/Disc 2/Break Apart.flac",
        "Bonobo/Fragments/Rosewood.flac",
        "Bonobo/Flashlight.flac",
        "Stray.flac",
    }
    # The destination's own cover stayed; the entry's duplicate went with it.
    assert (root / "Bonobo" / "Black Sands" / "cover.jpg").read_bytes() == b"already here"
    assert not entry_dir(root, entry_id).exists()


def test_merging_a_restored_artist_onto_a_taken_track_is_409(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo"})
    entry_id = response.json()["entry"]["id"]
    write_flac(root / "Simon Green" / "Black Sands" / "Kiara.flac", title="Impostor")

    restored = client.post(
        "/library/trash/restore", json={"id": entry_id, "artist": "Simon Green"}
    )

    assert restored.status_code == 409
    assert restored.json()["detail"]["conflicts"] == ["Simon Green/Black Sands/Kiara.flac"]
    # Nothing moved at all: the entry is whole and the impostor is untouched.
    trashed = entry_dir(root, entry_id) / "Bonobo"
    assert (trashed / "Black Sands" / "Kong.flac").is_file()
    assert (trashed / "Migration" / "Kerala.flac").is_file()
    assert (trashed / "Flashlight.flac").is_file()
    assert tags_of(root / "Simon Green" / "Black Sands" / "Kiara.flac")["TITLE"] == [
        "Impostor"
    ]
    assert not (root / "Simon Green" / "Black Sands" / "Kong.flac").exists()


def test_merging_a_restored_artist_retags_without_touching_the_albums(client, root):
    """A cross-artist artist merge writes the artist pair and nothing else."""
    write_flac(
        root / "Bonobo" / "Black Sands" / "Stay the Same.flac",
        title="Stay the Same",
        artist="Andreya Triana",
        albumartist="Bonobo",
        album="Black Sands",
    )
    response = client.post("/library/delete", json={"path": "Bonobo"})
    entry_id = response.json()["entry"]["id"]
    write_flac(
        root / "Ninja Tune" / "Fragments" / "Rosewood.flac",
        title="Rosewood",
        artist="Ninja Tune",
        album="Fragments",
    )

    restored = client.post(
        "/library/trash/restore", json={"id": entry_id, "artist": "Ninja Tune"}
    )

    assert restored.status_code == 200
    moved = tags_of(root / "Ninja Tune" / "Black Sands" / "Kiara.flac")
    assert moved["ALBUMARTIST"] == ["Ninja Tune"]
    assert moved["ARTIST"] == ["Ninja Tune"]
    # Each album kept its own name: an artist rename is none of ALBUM's business.
    assert moved["ALBUM"] == ["Black Sands"]
    assert tags_of(root / "Ninja Tune" / "Migration" / "Kerala.flac")["ALBUM"] == [
        "Migration"
    ]
    # The guest credit disagreed with the old artist folder, so it survives.
    guest = tags_of(root / "Ninja Tune" / "Black Sands" / "Stay the Same.flac")
    assert guest["ARTIST"] == ["Andreya Triana"]
    assert guest["ALBUMARTIST"] == ["Ninja Tune"]


def test_an_entry_mapping_two_files_onto_one_path_is_refused(client, root):
    """A hand-made entry whose two folders share a filename is refused.

    Both targets are free, so the collision check cannot see it -- and the
    second rename would silently destroy the first track.
    """
    made = entry_dir(root, "20260101T000000000000Z")
    write_flac(made / "A" / "X" / "Kiara.flac", title="First")
    write_flac(made / "B" / "Kiara.flac", title="Second")

    response = client.post(
        "/library/trash/restore", json={"id": made.name, "artist": "Dest"}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "this trash entry maps two files onto one path"
    )
    assert (made / "A" / "X" / "Kiara.flac").is_file()
    assert (made / "B" / "Kiara.flac").is_file()
    assert not (root / "Dest").exists()


def test_restoring_to_a_dot_folder_is_refused(client, root):
    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    entry_id = response.json()["entry"]["id"]

    refused = client.post(
        "/library/trash/restore", json={"id": entry_id, "artist": ".trash"}
    )

    assert refused.status_code == 400
    assert (entry_dir(root, entry_id) / "Bonobo" / "Black Sands").is_dir()


@pytest.mark.parametrize("entry_id", ["../escape", "a/b", "."])
def test_a_bad_entry_id_is_refused(client, root, entry_id):
    response = client.post("/library/trash/restore", json={"id": entry_id})
    assert response.status_code in (400, 404)


def test_restoring_an_unknown_entry_is_404(client, root):
    response = client.post("/library/trash/restore", json={"id": "20260101T000000000000Z"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Empty
# ---------------------------------------------------------------------------


def test_emptying_the_trash_removes_everything_but_the_ndignore(client, root):
    client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    client.post("/library/delete", json={"path": "Bonobo/Flashlight.flac"})

    response = client.post("/library/trash/empty")

    assert response.status_code == 200
    assert response.json() == {"removed": 2, "track_count": 3}
    assert trash_dir(root).is_dir()
    assert list(trash_dir(root).iterdir()) == [trash_dir(root) / ".ndignore"]


def test_emptying_the_trash_counts_only_the_entries_it_listed(client, root):
    """The number in the response is the number the confirm dialog showed.

    A folder the listing cannot make an entry of -- an empty one left behind by
    a delete that failed -- is still swept away, but it was never an entry and
    counting it would report more than the user was asked about.
    """
    client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    phantom = trash_dir(root) / "20200101T000000000000Z"
    phantom.mkdir()

    assert len(client.get("/library/trash").json()["entries"]) == 1

    response = client.post("/library/trash/empty")

    assert response.json() == {"removed": 1, "track_count": 2}
    assert not phantom.exists()


def test_emptying_an_untouched_trash_reports_nothing(client, root):
    response = client.post("/library/trash/empty")
    assert response.json() == {"removed": 0, "track_count": 0}


# ---------------------------------------------------------------------------
# The trash is invisible
# ---------------------------------------------------------------------------


def test_the_trash_never_appears_in_the_library(client, root):
    client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    library.invalidate()

    body = client.get("/library").json()

    names = {artist["name"] for artist in body["artists"]}
    assert ".trash" not in names
    for artist in body["artists"]:
        for album in artist["albums"]:
            assert not album["path"].startswith(".trash")


def test_a_trashed_track_is_not_a_duplicate(client, root):
    from app.downloader import DownloadError, _already_in_library

    target = root / "Bonobo" / "Black Sands" / "Kiara.flac"
    with pytest.raises(DownloadError):
        _already_in_library(target, str(root))

    client.post("/library/delete", json={"path": "Bonobo/Black Sands/Kiara.flac"})

    # The very same path is free again: the copy in the trash does not count.
    _already_in_library(target, str(root))


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def test_every_action_emits_library_changed(client_and_queue, root):
    client, _manager, events = client_and_queue

    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})
    entry_id = response.json()["entry"]["id"]
    assert changed_paths(events) == [["Bonobo"]]

    events.clear()
    client.post("/library/trash/restore", json={"id": entry_id})
    # The album folder is what came back, so that is what changed.
    assert changed_paths(events) == [["Bonobo/Black Sands"]]

    events.clear()
    client.post("/library/delete", json={"path": "Bonobo"})
    # The artist folder is gone, so the library root is what changed.
    assert changed_paths(events) == [[""]]

    events.clear()
    client.post("/library/trash/empty")
    assert changed_paths(events) == [[""]]


def test_a_track_delete_names_the_surviving_folder(client_and_queue, root):
    client, _manager, events = client_and_queue

    client.post("/library/delete", json={"path": "Bonobo/Migration/Kerala.flac"})

    # The album folder went with the track, so the artist is what is left.
    assert changed_paths(events) == [["Bonobo"]]


def test_a_restore_names_the_folder_the_files_landed_in(client_and_queue, root):
    """Every restore reports where the files actually went, not its parent."""
    client, _manager, events = client_and_queue

    entry_id = client.post(
        "/library/delete", json={"path": "Bonobo"}
    ).json()["entry"]["id"]
    events.clear()
    client.post("/library/trash/restore", json={"id": entry_id})
    assert changed_paths(events) == [["Bonobo"]]

    entry_id = client.post(
        "/library/delete", json={"path": "Bonobo/Migration/Kerala.flac"}
    ).json()["entry"]["id"]
    events.clear()
    client.post("/library/trash/restore", json={"id": entry_id})
    assert changed_paths(events) == [["Bonobo/Migration"]]

    entry_id = client.post(
        "/library/delete", json={"path": "Stray.flac"}
    ).json()["entry"]["id"]
    events.clear()
    client.post("/library/trash/restore", json={"id": entry_id})
    # Back at the library root, which has no name of its own.
    assert changed_paths(events) == [[""]]


def test_a_merging_restore_names_the_album_folders(client_and_queue, root):
    client, _manager, events = client_and_queue

    entry_id = client.post(
        "/library/delete", json={"path": "Bonobo/Black Sands"}
    ).json()["entry"]["id"]
    write_flac(
        root / "Ninja Tune" / "Black Sands" / "Recurring.flac",
        title="Recurring",
        album="Black Sands",
    )
    events.clear()

    client.post("/library/trash/restore", json={"id": entry_id, "artist": "Ninja Tune"})

    assert changed_paths(events) == [["Ninja Tune/Black Sands"]]


# ---------------------------------------------------------------------------
# A write that broke its all-or-nothing promise
# ---------------------------------------------------------------------------


def _strand_one_file(monkeypatch):
    """Make ``rename_files`` move its first pair and then strand the rest."""
    from app.library_ops import PartialRenameError

    def fake_rename_files(pairs):
        source, target = pairs[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, target)
        raise PartialRenameError([source], "1 file(s) are somewhere else now")

    monkeypatch.setattr("app.trash.rename_files", fake_rename_files)


def _count_invalidations(monkeypatch):
    import app.main as main_module

    calls: list[int] = []
    real = main_module.library_invalidate

    def counting() -> None:
        calls.append(1)
        real()

    monkeypatch.setattr(main_module, "library_invalidate", counting)
    return calls


def test_a_delete_that_stranded_files_still_refreshes_the_tree(
    client_and_queue, root, monkeypatch
):
    """A 500 that says files moved has to be a 500 the open tabs believe.

    The rollback failed, so the tree really did change; leaving the scan cache
    and the tabs on the old picture would show the user the tracks still in
    the album the error just told them they are no longer in.
    """
    client, _manager, events = client_and_queue
    invalidations = _count_invalidations(monkeypatch)
    _strand_one_file(monkeypatch)
    events.clear()

    response = client.post("/library/delete", json={"path": "Bonobo/Black Sands"})

    assert response.status_code == 500
    # The delete writes its own sentence: the stranded tracks are in the trash.
    assert response.json()["detail"] == (
        "The delete failed partway: 1 track(s) could not be put back and are "
        "in the Trash tab; restore them from there"
    )
    assert changed_paths(events) == [[""]]
    assert invalidations


def test_a_restore_that_stranded_files_still_refreshes_the_tree(
    client_and_queue, root, monkeypatch
):
    client, _manager, events = client_and_queue
    entry_id = client.post(
        "/library/delete", json={"path": "Bonobo/Black Sands"}
    ).json()["entry"]["id"]
    invalidations = _count_invalidations(monkeypatch)
    _strand_one_file(monkeypatch)
    events.clear()

    response = client.post("/library/trash/restore", json={"id": entry_id})

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "The restore failed partway: 1 file(s) are somewhere else now"
    )
    assert changed_paths(events) == [[""]]
    assert invalidations
