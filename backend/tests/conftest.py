"""Shared pytest fixtures for the backend test suite."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolated_paths(monkeypatch, tmp_path):
    """Point DOWNLOAD_PATH and DATA_PATH at per-test temporary directories.

    The app's startup check fails fast when DOWNLOAD_PATH does not exist or
    is not writable, so every test that spins up a ``TestClient`` needs a
    real directory.  Using ``tmp_path`` also keeps tests that actually write
    files out of each other's way.

    ``DATA_PATH`` gets the same treatment: startup opens ``queue.db`` inside it
    and refuses to boot if it is missing, and a per-test directory keeps every
    test's queue database to itself instead of sharing (or creating) ``/config``.

    Tests that need the variables gone (e.g. the QueueManager default-config
    tests, which use ``patch.dict(os.environ, {}, clear=True)``) still see them
    removed inside their own patch context.

    Returns ``(download_dir, data_dir)``.
    """
    download_dir = tmp_path / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DOWNLOAD_PATH", str(download_dir))

    data_dir = tmp_path / "config"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_PATH", str(data_dir))

    return download_dir, data_dir


@pytest.fixture(autouse=True)
def stub_download_audio(isolated_paths):
    """Never let a test reach the real yt-dlp.

    ``POST /download`` puts every job on the queue immediately, so a route test
    that only patches ``app.main.extract_metadata`` used to run the real
    downloader -- one of them genuinely fetched a few megabytes from YouTube.
    This replaces the queue's entry point with a fast stub that returns the
    path a real download would have produced.

    The stub returns instantly rather than blocking: a blocking default takes
    the suite from ~20 s to ~180 s.  A test that needs to observe a job while
    it is still in flight installs its own blocking ``side_effect`` on top --
    an explicit ``@patch("app.queue_manager.download_audio")`` simply layers
    over this one.
    """
    download_dir, _ = isolated_paths

    def fake_download(job, *args, **kwargs):
        target = Path(download_dir) / "Stub Artist" / "Stub Album" / "Stub Track.flac"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        return target

    with patch("app.queue_manager.download_audio", side_effect=fake_download):
        yield
