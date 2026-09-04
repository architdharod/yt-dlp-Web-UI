"""Tests for the library scanner, the cover endpoint, and the scan cache.

The tree every test builds is the one the phase's acceptance criteria name: a
normal artist with two albums, an album with a too-deep ``Disc 1`` folder, a
loose Single at depth 2, a root-level file, a real folder that happens to be
called ``Unknown Artist``, ``.trash`` and ``.tmp`` full of audio that must stay
invisible, a non-audio file, and a corrupt FLAC.

Test audio is built from :func:`tests.conftest.minimal_flac_bytes` -- the same
valid, empty FLAC the fake ffmpeg writes -- with mutagen writing real tags and
pictures onto it.  Nothing here needs ffmpeg or a checked-in binary.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.flac import FLAC, Picture

from app import library
from app.library import LibraryPathError, validate_library_path
from app.queue_manager import QueueManager
from tests.conftest import TINY_JPEG, TINY_PNG, minimal_flac_bytes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_flac(path: Path, picture: bytes | None = None, **tags: str) -> Path:
    """Write a valid, tagged FLAC at *path*, optionally with a front cover."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(minimal_flac_bytes())
    audio = FLAC(path)
    for key, value in tags.items():
        audio[key.upper()] = value
    if picture is not None:
        block = Picture()
        block.type = 3  # front cover
        block.mime = "image/jpeg" if picture.startswith(b"\xff\xd8") else "image/png"
        block.desc = "Cover"
        block.data = picture
        audio.add_picture(block)
    audio.save()
    return path


def build_tree(root: Path) -> None:
    """Create the reference library tree described in the module docstring."""
    # A normal artist with two albums.
    write_flac(
        root / "Bonobo" / "Black Sands" / "Kiara.flac",
        title="Kiara",
        artist="Bonobo",
        albumartist="Bonobo",
        album="Black Sands",
        tracknumber="3/12",
    )
    write_flac(
        root / "Bonobo" / "Black Sands" / "Kong.flac",
        title="Kong",
        artist="Bonobo",
        album="Black Sands",
        tracknumber="1",
    )
    write_flac(
        root / "Bonobo" / "Migration" / "Break Apart.flac",
        picture=TINY_JPEG,
        title="Break Apart",
        artist="Bonobo",
        album="Migration",
        tracknumber="4",
    )
    # A folder deeper than depth 2 flattens into its album.
    write_flac(
        root / "Bonobo" / "Migration" / "Disc 1" / "Outer.flac",
        title="Outer",
        artist="Bonobo",
        album="Migration",
        tracknumber="2",
        discnumber="1",
    )
    # A loose Single at depth 2.
    write_flac(root / "Bonobo" / "Flashlight.flac", title="Flashlight", artist="Bonobo")
    # A root-level file: the synthetic bucket.
    write_flac(root / "Stray.flac", title="Stray")
    # A real folder named like the synthetic bucket, to prove they stay apart.
    write_flac(root / "Unknown Artist" / "Odd.flac", title="Odd")
    # Must never be scanned.
    write_flac(root / ".trash" / "20260101T000000Z" / "Gone.flac", title="Gone")
    write_flac(root / ".tmp" / "job-1.flac", title="Scratch")
    write_flac(root / "Bonobo" / ".hidden.flac", title="Hidden")
    (root / "Bonobo" / "Black Sands" / "notes.txt").write_text("not audio")
    # Random bytes: mutagen cannot read it, but it is still a Track on disk.
    (root / "Bonobo" / "Black Sands" / "corrupt.flac").write_bytes(os.urandom(512))


@pytest.fixture()
def library_root(isolated_paths):
    """The reference tree, with the module's caches reset around each test."""
    download_dir, _ = isolated_paths
    build_tree(download_dir)
    library.invalidate()
    library.reset_tag_read_count()
    yield download_dir
    library.invalidate()
    library.reset_tag_read_count()


@pytest.fixture()
def client(library_root):
    """TestClient over the real app, with the reference tree on disk.

    The module-level ``queue_manager`` is swapped for a fresh one, as
    ``tests/test_routes.py`` does: starting the app attaches a JobStore to
    whichever manager is installed and closes it again on shutdown, and the
    library tests have no business leaving the singleton holding a closed
    store for whatever runs next.
    """
    import app.main as main_module

    original = main_module.queue_manager
    main_module.queue_manager = QueueManager(
        max_concurrent=1, timeout=10, on_event=main_module._on_queue_event
    )
    try:
        with TestClient(main_module.app) as test_client:
            yield test_client
    finally:
        main_module.queue_manager = original


def scan(root: Path) -> dict:
    return library.scan_library(root)


def artist(payload: dict, name: str, synthetic: bool = False) -> dict:
    for entry in payload["artists"]:
        if entry["name"] == name and entry["synthetic"] is synthetic:
            return entry
    raise AssertionError(f"no artist {name!r} (synthetic={synthetic}) in {payload}")


def album(artist_payload: dict, name: str) -> dict:
    for entry in artist_payload["albums"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"no album {name!r} in {artist_payload['name']}")


# ===========================================================================
# Scanner: shape of the tree
# ===========================================================================


def test_scan_groups_artists_albums_and_tracks(library_root):
    payload = scan(library_root)

    bonobo = artist(payload, "Bonobo")
    assert bonobo["path"] == "Bonobo"
    assert bonobo["synthetic"] is False
    assert [entry["name"] for entry in bonobo["albums"]] == ["Black Sands", "Migration"]

    black_sands = album(bonobo, "Black Sands")
    assert black_sands["path"] == "Bonobo/Black Sands"
    assert [track["title"] for track in black_sands["tracks"]] == [
        "Kong",
        "Kiara",
        "corrupt",
    ]


def test_loose_track_at_depth_two_is_a_single(library_root):
    bonobo = artist(scan(library_root), "Bonobo")

    assert [track["title"] for track in bonobo["singles"]] == ["Flashlight"]
    assert bonobo["singles"][0]["path"] == "Bonobo/Flashlight.flac"


def test_root_file_lands_in_the_synthetic_artist(library_root):
    payload = scan(library_root)

    synthetic = artist(payload, "Unknown Artist", synthetic=True)
    assert synthetic["path"] == ""
    assert synthetic["albums"] == []
    assert [track["path"] for track in synthetic["singles"]] == ["Stray.flac"]

    # A real folder of the same name is a separate, non-synthetic entry.
    real = artist(payload, "Unknown Artist", synthetic=False)
    assert real["path"] == "Unknown Artist"
    assert real["album_count"] == 0
    assert [track["path"] for track in real["singles"]] == ["Unknown Artist/Odd.flac"]


def test_synthetic_artist_is_absent_without_root_files(isolated_paths):
    download_dir, _ = isolated_paths
    write_flac(download_dir / "Bonobo" / "Migration" / "Kerala.flac", title="Kerala")
    library.invalidate()

    payload = scan(download_dir)

    assert [entry["name"] for entry in payload["artists"]] == ["Bonobo"]
    assert all(not entry["synthetic"] for entry in payload["artists"])


def test_deep_folder_flattens_into_its_album(library_root):
    migration = album(artist(scan(library_root), "Bonobo"), "Migration")

    paths = [track["path"] for track in migration["tracks"]]
    assert "Bonobo/Migration/Disc 1/Outer.flac" in paths
    assert migration["track_count"] == 2


def test_hidden_dirs_files_and_non_audio_are_ignored(library_root):
    payload = scan(library_root)

    every_path = [
        track["path"]
        for entry in payload["artists"]
        for group in (entry["singles"], *[a["tracks"] for a in entry["albums"]])
        for track in group
    ]
    # Positive control: the filter is discriminating, not just returning an
    # empty list that would satisfy every assertion below.
    assert "Bonobo/Black Sands/Kiara.flac" in every_path
    assert len(every_path) == 8
    assert payload["track_count"] == 8
    assert not any(path.startswith(".trash") for path in every_path)
    assert not any(path.startswith(".tmp") for path in every_path)
    assert not any(".hidden" in path for path in every_path)
    assert not any(path.endswith(".txt") for path in every_path)


def test_symlinked_directory_is_not_descended(isolated_paths, tmp_path):
    download_dir, _ = isolated_paths
    outside = tmp_path / "outside"
    write_flac(outside / "Secret.flac", title="Secret")
    (download_dir / "Escape").symlink_to(outside, target_is_directory=True)
    write_flac(download_dir / "Bonobo" / "Kerala.flac", title="Kerala")
    library.invalidate()

    payload = scan(download_dir)

    assert [entry["name"] for entry in payload["artists"]] == ["Bonobo"]


def test_an_audio_file_symlinked_outside_the_root_is_not_listed(
    client, library_root, tmp_path
):
    """Following it would publish an outside file's tags, size and mtime."""
    write_flac(
        tmp_path / "outside" / "Secret.flac",
        picture=TINY_PNG,
        title="Secret",
        artist="Nobody",
    )
    folder = library_root / "Bonobo" / "Black Sands"
    os.symlink(tmp_path / "outside" / "Secret.flac", folder / "Secret.flac")
    library.invalidate()

    payload = client.get("/library").json()
    black_sands = album(artist(payload, "Bonobo"), "Black Sands")
    cover = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})

    assert "Secret" not in [track["title"] for track in black_sands["tracks"]]
    assert "Bonobo/Black Sands/Secret.flac" not in [
        track["path"] for track in black_sands["tracks"]
    ]
    assert black_sands["track_count"] == 3
    # And its embedded art is not reachable through the cover endpoint either.
    assert black_sands["has_cover"] is False
    assert cover.headers["content-type"] == "image/svg+xml"
    assert TINY_PNG not in cover.content


def test_a_sidecar_symlinked_outside_the_root_is_not_served(
    client, library_root, tmp_path
):
    """cover.jpg pointing anywhere on the box would make this a file reader."""
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(TINY_JPEG)
    folder = library_root / "Bonobo" / "Black Sands"
    os.symlink(outside, folder / "cover.jpg")
    library.invalidate()

    payload = client.get("/library").json()
    cover = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})

    assert album(artist(payload, "Bonobo"), "Black Sands")["has_cover"] is False
    assert cover.headers["content-type"] == "image/svg+xml"
    assert TINY_JPEG not in cover.content


def test_a_symlink_to_a_file_inside_the_library_is_still_a_track(client, library_root):
    """The check is resolve-and-compare, not a blanket skip of every symlink."""
    source = library_root / "Bonobo" / "Migration" / "Break Apart.flac"
    linked = library_root / "Bonobo" / "Linked"
    linked.mkdir()
    os.symlink(source, linked / "Copy.flac")
    library.invalidate()

    payload = client.get("/library").json()
    album_payload = album(artist(payload, "Bonobo"), "Linked")
    cover = client.get("/library/cover", params={"path": "Bonobo/Linked"})

    assert album_payload["track_count"] == 1
    assert album_payload["tracks"][0]["path"] == "Bonobo/Linked/Copy.flac"
    assert album_payload["has_cover"] is True
    assert cover.content == TINY_JPEG


def test_counts_cover_albums_and_singles(library_root):
    payload = scan(library_root)
    bonobo = artist(payload, "Bonobo")

    assert bonobo["album_count"] == 2
    # 3 in Black Sands (corrupt included) + 2 in Migration + 1 single
    assert bonobo["track_count"] == 6
    assert payload["artist_count"] == len(payload["artists"]) == 3
    assert payload["album_count"] == 2
    assert payload["track_count"] == 8
    assert payload["scanned_at"].endswith("+00:00")


# ===========================================================================
# Scanner: per-track fields
# ===========================================================================


def test_track_fields_come_from_the_tags(library_root):
    black_sands = album(artist(scan(library_root), "Bonobo"), "Black Sands")
    kiara = next(track for track in black_sands["tracks"] if track["title"] == "Kiara")

    assert kiara["name"] == "Kiara.flac"
    assert kiara["path"] == "Bonobo/Black Sands/Kiara.flac"
    assert kiara["artist"] == "Bonobo"
    assert kiara["album_artist"] == "Bonobo"
    assert kiara["album"] == "Black Sands"
    assert kiara["track_number"] == 3  # parsed out of "3/12"
    assert kiara["disc_number"] is None
    assert kiara["format"] == "flac"
    assert kiara["sample_rate"] == 44100
    assert kiara["size"] > 0
    assert kiara["mtime"].endswith("+00:00")
    assert kiara["has_embedded_art"] is False
    assert kiara["error"] is None
    assert kiara["tags"]["tracknumber"] == ["3/12"]
    assert kiara["tags"]["albumartist"] == ["Bonobo"]


def test_untagged_track_falls_back_to_its_filename(isolated_paths):
    download_dir, _ = isolated_paths
    path = download_dir / "Bonobo" / "Migration" / "No Tags.flac"
    path.parent.mkdir(parents=True)
    path.write_bytes(minimal_flac_bytes())
    library.invalidate()

    migration = album(artist(scan(download_dir), "Bonobo"), "Migration")

    assert migration["tracks"][0]["title"] == "No Tags"
    assert migration["tracks"][0]["tags"] == {}


def test_corrupt_track_is_listed_with_an_error(library_root, caplog):
    with caplog.at_level("WARNING"):
        black_sands = album(artist(scan(library_root), "Bonobo"), "Black Sands")
    corrupt = next(track for track in black_sands["tracks"] if track["name"] == "corrupt.flac")

    assert corrupt["title"] == "corrupt"
    assert corrupt["error"]
    assert corrupt["duration"] is None
    assert corrupt["artist"] is None
    assert corrupt["tags"] == {}
    assert str(library_root) not in corrupt["error"]
    assert any("Could not read tags" in record.message for record in caplog.records)


def test_embedded_art_is_reported(library_root):
    migration = album(artist(scan(library_root), "Bonobo"), "Migration")
    break_apart = next(track for track in migration["tracks"] if track["title"] == "Break Apart")

    assert break_apart["has_embedded_art"] is True
    assert "metadata_block_picture" not in break_apart["tags"]


# ===========================================================================
# Scanner: sorting
# ===========================================================================


def test_artists_sort_case_insensitively_with_the_synthetic_bucket_last(isolated_paths):
    download_dir, _ = isolated_paths
    for name in ("zebra", "Aphex Twin", "bonobo"):
        write_flac(download_dir / name / "Album" / "t.flac", title="t")
    write_flac(download_dir / "Stray.flac", title="Stray")
    library.invalidate()

    payload = scan(download_dir)

    assert [entry["name"] for entry in payload["artists"]] == [
        "Aphex Twin",
        "bonobo",
        "zebra",
        "Unknown Artist",
    ]
    assert payload["artists"][-1]["synthetic"] is True


def test_tracks_sort_by_disc_then_number_then_title(isolated_paths):
    download_dir, _ = isolated_paths
    folder = download_dir / "A" / "Album"
    write_flac(folder / "d2.flac", title="Second disc", discnumber="2", tracknumber="1")
    write_flac(folder / "n2.flac", title="Numbered two", tracknumber="2")
    write_flac(folder / "n1.flac", title="Numbered one", tracknumber="1")
    write_flac(folder / "zz.flac", title="zed unnumbered")
    write_flac(folder / "aa.flac", title="Aaa unnumbered")
    library.invalidate()

    tracks = album(artist(scan(download_dir), "A"), "Album")["tracks"]

    assert [track["title"] for track in tracks] == [
        "Numbered one",
        "Numbered two",
        "Aaa unnumbered",
        "zed unnumbered",
        "Second disc",
    ]


def test_albums_sort_case_insensitively(isolated_paths):
    download_dir, _ = isolated_paths
    for name in ("zeta", "Alpha", "beta"):
        write_flac(download_dir / "A" / name / "t.flac", title="t")
    library.invalidate()

    names = [entry["name"] for entry in artist(scan(download_dir), "A")["albums"]]

    assert names == ["Alpha", "beta", "zeta"]


# ===========================================================================
# Scanner: cover metadata on the payload
# ===========================================================================


def test_has_cover_and_cover_album_path(library_root):
    bonobo = artist(scan(library_root), "Bonobo")

    # No track in Black Sands carries art and the folder has no cover file.
    assert album(bonobo, "Black Sands")["has_cover"] is False
    # Migration's "Break Apart" does, even though it is not the first track.
    assert album(bonobo, "Migration")["has_cover"] is True
    assert bonobo["cover_album_path"] == "Bonobo/Migration"


def test_sidecar_cover_sets_has_cover_and_cover_album_path(library_root):
    (library_root / "Bonobo" / "Black Sands" / "cover.jpg").write_bytes(TINY_JPEG)
    library.invalidate()

    bonobo = artist(scan(library_root), "Bonobo")

    assert album(bonobo, "Black Sands")["has_cover"] is True
    # Black Sands now sorts first among the albums that have art.
    assert bonobo["cover_album_path"] == "Bonobo/Black Sands"


def test_cover_version_covers_the_folder_and_everything_in_it(library_root):
    """Every input the cover chain reads moves the stamp: the folder itself, a
    sidecar image, a track directly inside, and a track in a nested folder.

    Each leg is made the newest thing in turn and the stamp asserted to follow
    it, so nothing here depends on the order the fixture happened to write files
    in or on the filesystem's mtime granularity.
    """
    folder = library_root / "Bonobo" / "Black Sands"
    sidecar = folder / "cover.jpg"
    sidecar.write_bytes(TINY_JPEG)
    nested = write_flac(folder / "Disc 2" / "Deep.flac", title="Deep")
    track = folder / "Kiara.flac"

    def version() -> int:
        library.invalidate()
        return album(artist(scan(library_root), "Bonobo"), "Black Sands")["cover_version"]

    newest = version()
    for target in (folder, sidecar, track, nested):
        newest += 2_000_000_000
        os.utime(target, ns=(newest, newest))
        assert version() == newest


# ===========================================================================
# The scan cache
# ===========================================================================


def test_second_scan_reads_no_tags(library_root):
    scan(library_root)
    first = library.tag_read_count()
    assert first > 0

    library.reset_tag_read_count()
    scan(library_root)

    assert library.tag_read_count() == 0


def test_only_a_changed_file_is_re_read(library_root):
    scan(library_root)
    library.reset_tag_read_count()

    changed = library_root / "Bonobo" / "Black Sands" / "Kiara.flac"
    stat = changed.stat()
    os.utime(changed, ns=(stat.st_mtime_ns + 2_000_000_000, stat.st_mtime_ns + 2_000_000_000))
    scan(library_root)

    assert library.tag_read_count() == 1


def test_cache_entries_for_deleted_files_are_evicted(library_root):
    scan(library_root)
    before = len(library._state.entries)

    (library_root / "Bonobo" / "Flashlight.flac").unlink()
    scan(library_root)

    assert len(library._state.entries) == before - 1


def test_invalidate_forces_a_full_re_read(library_root):
    scan(library_root)
    library.reset_tag_read_count()
    library.invalidate()

    scan(library_root)

    assert library.tag_read_count() > 0


def test_a_few_hundred_files_scan_without_blowing_up(isolated_paths, tmp_path):
    download_dir, _ = isolated_paths
    # One tagged FLAC, copied 360 times: mutagen rewrites the whole file on
    # every save, and doing that per track makes this fixture slower than the
    # scan it is meant to exercise.
    template = write_flac(tmp_path / "template.flac", title="Track", tracknumber="1")
    payload_bytes = template.read_bytes()
    for artist_index in range(6):
        for album_index in range(5):
            folder = download_dir / f"Artist {artist_index}" / f"Album {album_index}"
            folder.mkdir(parents=True, exist_ok=True)
            for track_index in range(12):
                (folder / f"Track {track_index:02d}.flac").write_bytes(payload_bytes)
    library.invalidate()

    payload = scan(download_dir)

    assert payload["track_count"] == 360
    assert payload["album_count"] == 30
    library.reset_tag_read_count()
    scan(download_dir)
    assert library.tag_read_count() == 0


# ===========================================================================
# Path validation
# ===========================================================================


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "..",
        "../etc/passwd",
        "Bonobo/../../etc",
        "/etc/passwd",
        "Bonobo//Black Sands",
        "Bonobo\\Black Sands",
        "Bonobo/\x00",
        ".",
        "./Bonobo",
    ],
)
def test_validate_library_path_rejects(bad, library_root):
    with pytest.raises(LibraryPathError):
        validate_library_path(bad, library_root)


def test_validate_library_path_rejects_an_escaping_symlink(library_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (library_root / "Escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(LibraryPathError):
        validate_library_path("Escape", library_root)


def test_validate_library_path_accepts_a_real_album(library_root):
    resolved = validate_library_path("Bonobo/Black Sands", library_root)

    assert resolved == (library_root / "Bonobo" / "Black Sands").resolve()


# ===========================================================================
# GET /library
# ===========================================================================


def test_get_library_returns_the_tree(client, library_root):
    response = client.get("/library")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["artist_count"] == 3
    assert {entry["name"] for entry in payload["artists"]} == {"Bonobo", "Unknown Artist"}
    bonobo = artist(payload, "Bonobo")
    assert bonobo["albums"][0]["tracks"][0]["title"] == "Kong"


def test_get_library_never_leaks_absolute_paths(client, library_root):
    response = client.get("/library")
    body = response.text

    # A 500 or an empty tree would satisfy the assertion below for the wrong
    # reason, so pin that the response really is the whole library.
    assert response.status_code == 200
    assert response.json()["track_count"] == 8
    assert "Kiara" in body
    assert len(body) > 500
    assert str(library_root) not in body


def test_get_library_on_an_empty_root(client, isolated_paths):
    download_dir, _ = isolated_paths
    for child in download_dir.iterdir():
        if child.is_dir():
            for sub in sorted(child.rglob("*"), reverse=True):
                sub.unlink() if sub.is_file() else sub.rmdir()
            child.rmdir()
        else:
            child.unlink()
    library.invalidate()

    payload = client.get("/library").json()

    assert payload["artists"] == []
    assert payload["artist_count"] == 0


# ===========================================================================
# GET /library/cover
# ===========================================================================


def test_cover_serves_the_embedded_picture(client, library_root):
    response = client.get("/library/cover", params={"path": "Bonobo/Migration"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == TINY_JPEG
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"]


def test_cover_falls_back_to_a_sidecar_file(client, library_root):
    (library_root / "Bonobo" / "Black Sands" / "Cover.JPG").write_bytes(TINY_JPEG)

    response = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == TINY_JPEG


def test_cover_falls_back_to_a_png_sidecar(client, library_root):
    (library_root / "Bonobo" / "Black Sands" / "cover.png").write_bytes(TINY_PNG)

    response = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == TINY_PNG


def test_cover_falls_back_to_a_placeholder(client, library_root):
    response = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.content.startswith(b"<svg")
    assert b"BS" in response.content


def test_placeholder_is_deterministic(client, library_root):
    first = client.get("/library/cover", params={"path": "Bonobo/Black Sands"}).content
    library.invalidate()
    second = client.get("/library/cover", params={"path": "Bonobo/Black Sands"}).content

    assert first == second


def test_cover_for_the_synthetic_bucket_is_a_placeholder(client, library_root):
    response = client.get("/library/cover", params={"path": ""})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"


def test_cover_for_an_artist_path_is_a_placeholder(client, library_root):
    response = client.get("/library/cover", params={"path": "Bonobo"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"


def test_cover_with_a_version_is_immutable(client, library_root):
    response = client.get(
        "/library/cover", params={"path": "Bonobo/Migration", "v": "123456"}
    )

    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize(
    "bad",
    ["../x", "/etc/passwd", "Bonobo\\Black Sands", "Bonobo/../../etc", ".."],
)
def test_cover_rejects_unsafe_paths(client, library_root, bad):
    response = client.get("/library/cover", params={"path": bad})

    assert response.status_code == 400
    assert str(library_root) not in response.text


def test_cover_rejects_a_symlink_escaping_the_root(client, library_root, tmp_path):
    outside = tmp_path / "outside"
    write_flac(outside / "Secret.flac", picture=TINY_JPEG, title="Secret")
    (library_root / "Escape").symlink_to(outside, target_is_directory=True)

    response = client.get("/library/cover", params={"path": "Escape"})

    assert response.status_code == 400


def test_cover_404s_for_a_missing_album(client, library_root):
    response = client.get("/library/cover", params={"path": "Bonobo/Nope"})

    assert response.status_code == 404
    assert str(library_root) not in response.text


def test_cover_404s_when_the_path_names_a_file(client, library_root):
    """A track is a well-formed path inside the root, but it is not an album."""
    response = client.get(
        "/library/cover", params={"path": "Bonobo/Black Sands/Kiara.flac"}
    )

    assert response.status_code == 404
    assert str(library_root) not in response.text


def test_cover_cache_hit_does_not_re_run_the_fallback_chain(
    client, library_root, monkeypatch
):
    """A hit still walks the folder to stat it -- that walk *is* the version
    stamp, and without it an overwritten cover would never be noticed.  What it
    must not do is resolve the fallback chain again or parse a single tag.
    """
    first = client.get("/library/cover", params={"path": "Bonobo/Migration"})
    assert first.status_code == 200

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("a cache hit re-ran the fallback chain")

    monkeypatch.setattr(library, "_album_cover_bytes", explode)
    library.reset_tag_read_count()
    second = client.get("/library/cover", params={"path": "Bonobo/Migration"})

    assert second.status_code == 200
    assert library.tag_read_count() == 0
    assert second.content == first.content
    assert second.headers["etag"] == first.headers["etag"]


def test_a_sidecar_only_cover_miss_opens_no_audio_files(
    client, library_root, monkeypatch
):
    """``has_embedded_art`` is already in the tag cache, and it tests exactly the
    four picture sources ``_embedded_picture`` does -- so an album whose only art
    is a sidecar must not reopen every track to rediscover it has none.
    """
    folder = library_root / "Bonobo" / "Black Sands"  # no embedded art anywhere
    (folder / "cover.jpg").write_bytes(TINY_JPEG)
    library.invalidate()
    assert client.get("/library").status_code == 200  # fills the tag cache

    opened: list[str] = []
    real_file = library.mutagen.File

    def counting(path, *args, **kwargs):
        opened.append(str(path))
        return real_file(path, *args, **kwargs)

    monkeypatch.setattr(library.mutagen, "File", counting)
    response = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})

    assert response.status_code == 200
    assert response.content == TINY_JPEG
    assert opened == []


def test_cover_cache_files_land_under_data_path(client, isolated_paths, library_root):
    _, data_dir = isolated_paths

    client.get("/library/cover", params={"path": "Bonobo/Migration"})

    cached = list((data_dir / "covers").iterdir())
    assert len(cached) == 1
    assert cached[0].name.endswith(".jpg")


def test_writing_a_new_cover_changes_what_the_endpoint_returns(
    client, library_root, isolated_paths
):
    _, data_dir = isolated_paths
    folder = library_root / "Bonobo" / "Black Sands"
    before = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})
    assert before.headers["content-type"] == "image/svg+xml"

    (folder / "cover.png").write_bytes(TINY_PNG)
    # Writing into the folder bumps its mtime, which is the cache key; nudge it
    # forward explicitly so the test does not depend on filesystem granularity.
    stat = folder.stat()
    os.utime(folder, ns=(stat.st_mtime_ns + 2_000_000_000, stat.st_mtime_ns + 2_000_000_000))

    after = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})

    assert after.headers["content-type"] == "image/png"
    assert after.content == TINY_PNG
    assert after.headers["etag"] != before.headers["etag"]
    # The superseded entry for this album is gone rather than accumulating.
    assert len(list((data_dir / "covers").iterdir())) == 1


def test_overwriting_a_sidecar_in_place_changes_the_cover_and_its_version(
    client, library_root
):
    """The folder mtime alone would miss this: same filename, new bytes."""
    folder = library_root / "Bonobo" / "Black Sands"
    (folder / "cover.png").write_bytes(TINY_PNG)
    library.invalidate()
    before = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})
    assert before.content == TINY_PNG
    before_version = album(
        artist(client.get("/library").json(), "Bonobo"), "Black Sands"
    )["cover_version"]

    folder_mtime = folder.stat().st_mtime_ns
    replacement = TINY_PNG + b"\x00" * 32
    (folder / "cover.png").write_bytes(replacement)
    cover_stat = (folder / "cover.png").stat()
    os.utime(
        folder / "cover.png",
        ns=(cover_stat.st_mtime_ns + 2_000_000_000, cover_stat.st_mtime_ns + 2_000_000_000),
    )
    # Force the folder mtime back to what it was: only the file changed.
    os.utime(folder, ns=(folder_mtime, folder_mtime))
    library.invalidate()

    after = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})
    after_version = album(
        artist(client.get("/library").json(), "Bonobo"), "Black Sands"
    )["cover_version"]

    assert after.content == replacement
    assert after.headers["etag"] != before.headers["etag"]
    assert after_version != before_version


def test_replacing_embedded_art_changes_the_cover_and_its_version(client, library_root):
    """Re-tagging a track leaves the album folder's own mtime untouched."""
    folder = library_root / "Bonobo" / "Migration"
    track = folder / "Break Apart.flac"
    before = client.get("/library/cover", params={"path": "Bonobo/Migration"})
    assert before.content == TINY_JPEG
    before_version = album(
        artist(client.get("/library").json(), "Bonobo"), "Migration"
    )["cover_version"]

    folder_mtime = folder.stat().st_mtime_ns
    audio = FLAC(track)
    audio.clear_pictures()
    block = Picture()
    block.type = 3
    block.mime = "image/png"
    block.desc = "Cover"
    block.data = TINY_PNG
    audio.add_picture(block)
    audio.save()
    stat = track.stat()
    os.utime(track, ns=(stat.st_mtime_ns + 2_000_000_000, stat.st_mtime_ns + 2_000_000_000))
    os.utime(folder, ns=(folder_mtime, folder_mtime))
    library.invalidate()

    after = client.get("/library/cover", params={"path": "Bonobo/Migration"})
    after_version = album(
        artist(client.get("/library").json(), "Bonobo"), "Migration"
    )["cover_version"]

    assert after.content == TINY_PNG
    assert after.headers["content-type"] == "image/png"
    assert after.headers["etag"] != before.headers["etag"]
    assert after_version != before_version


# ===========================================================================
# Conditional requests and content-type sniffing
# ===========================================================================


def test_cover_honours_if_none_match(client, library_root):
    first = client.get("/library/cover", params={"path": "Bonobo/Migration"})
    etag = first.headers["etag"]

    second = client.get(
        "/library/cover",
        params={"path": "Bonobo/Migration"},
        headers={"If-None-Match": etag},
    )

    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag
    assert second.headers["cache-control"] == "no-cache"


def test_a_stale_if_none_match_gets_the_new_cover_not_a_304(client, library_root):
    """The art changed under the same URL, so the old validator must not match."""
    folder = library_root / "Bonobo" / "Black Sands"
    sidecar = folder / "cover.png"
    sidecar.write_bytes(TINY_PNG)
    library.invalidate()
    first = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})
    assert first.content == TINY_PNG
    stale = first.headers["etag"]

    replacement = TINY_PNG + b"\x00" * 32
    sidecar.write_bytes(replacement)
    stat = sidecar.stat()
    os.utime(sidecar, ns=(stat.st_mtime_ns + 2_000_000_000,) * 2)
    library.invalidate()

    second = client.get(
        "/library/cover",
        params={"path": "Bonobo/Black Sands"},
        headers={"If-None-Match": stale},
    )

    assert second.status_code == 200
    assert second.content == replacement
    assert second.headers["etag"] != stale


def test_cover_honours_a_weak_if_none_match(client, library_root):
    etag = client.get("/library/cover", params={"path": "Bonobo/Migration"}).headers["etag"]

    response = client.get(
        "/library/cover",
        params={"path": "Bonobo/Migration"},
        headers={"If-None-Match": f'W/{etag}, "something-else"'},
    )

    assert response.status_code == 304
    assert response.content == b""


def test_cover_ignores_a_non_matching_if_none_match(client, library_root):
    response = client.get(
        "/library/cover",
        params={"path": "Bonobo/Migration"},
        headers={"If-None-Match": '"not-the-one"'},
    )

    assert response.status_code == 200
    assert response.content == TINY_JPEG


def test_cover_responses_carry_nosniff(client, library_root):
    served = client.get("/library/cover", params={"path": "Bonobo/Migration"})
    not_modified = client.get(
        "/library/cover",
        params={"path": "Bonobo/Migration"},
        headers={"If-None-Match": served.headers["etag"]},
    )

    assert served.headers["x-content-type-options"] == "nosniff"
    assert not_modified.headers["x-content-type-options"] == "nosniff"


def test_a_lying_picture_mime_is_never_echoed(client, library_root):
    """A PICTURE block claiming text/html must not make us serve text/html."""
    folder = library_root / "Bonobo" / "Black Sands"
    track = folder / "Kiara.flac"
    audio = FLAC(track)
    block = Picture()
    block.type = 3
    block.mime = "text/html"
    block.desc = "Cover"
    block.data = b"<html><script>alert(1)</script></html>"
    audio.add_picture(block)
    audio.save()
    library.invalidate()

    response = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})

    assert response.status_code == 200
    assert "text/html" not in response.headers["content-type"]
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.content.startswith(b"<svg")


def test_sidecar_content_type_comes_from_the_bytes_not_the_suffix(client, library_root):
    gif = b"GIF89a" + bytes(16)
    (library_root / "Bonobo" / "Black Sands" / "cover.jpg").write_bytes(gif)
    library.invalidate()

    response = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/gif"
    assert response.content == gif


def test_an_unrecognised_sidecar_falls_through_to_the_placeholder(client, library_root):
    (library_root / "Bonobo" / "Black Sands" / "cover.jpg").write_bytes(
        b"<html><script>alert(1)</script></html>"
    )
    library.invalidate()

    response = client.get("/library/cover", params={"path": "Bonobo/Black Sands"})

    assert response.headers["content-type"] == "image/svg+xml"


def test_artist_cover_album_prefers_one_that_has_tracks(isolated_paths):
    download_dir, _ = isolated_paths
    write_flac(download_dir / "A" / "Beta" / "t.flac", picture=TINY_JPEG, title="t")
    # An "Alpha" album with art but no music: it sorts first, but a tile drawn
    # from it would point at an album the browser shows as empty.
    (download_dir / "A" / "Alpha").mkdir(parents=True)
    (download_dir / "A" / "Alpha" / "cover.jpg").write_bytes(TINY_JPEG)
    library.invalidate()

    a = artist(scan(download_dir), "A")

    assert album(a, "Alpha")["has_cover"] is True
    assert a["cover_album_path"] == "A/Beta"


def test_artist_cover_album_falls_back_to_a_track_less_album(isolated_paths):
    download_dir, _ = isolated_paths
    write_flac(download_dir / "A" / "Beta" / "t.flac", title="t")
    (download_dir / "A" / "Alpha").mkdir(parents=True)
    (download_dir / "A" / "Alpha" / "cover.jpg").write_bytes(TINY_JPEG)
    library.invalidate()

    a = artist(scan(download_dir), "A")

    assert a["cover_album_path"] == "A/Alpha"
