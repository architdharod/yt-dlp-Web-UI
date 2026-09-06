"""Shared pytest fixtures for the backend test suite."""

import os
import subprocess
import threading
import time
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


# ---------------------------------------------------------------------------
# A stand-in for ffmpeg
# ---------------------------------------------------------------------------
# ffmpeg is a real external binary the pipeline shells out to, and it is not
# installed on developer machines or in CI.  Every test therefore runs against
# the fake below rather than the real thing; the fake records the exact argv it
# was handed, so the tests can still assert on how ffmpeg is invoked.


def minimal_flac_bytes(total_samples: int = 0) -> bytes:
    """Return the bytes of a valid, empty FLAC file.

    *total_samples* is written into STREAMINFO, so a caller that needs the file
    to *claim* a duration (the tagger's match bar compares one) can ask for
    ``44100 * seconds``.  The default 0 means "unknown", which is what every
    test that only needs a parseable file wants.

    The pipeline's last stage is mutagen, which parses whatever ffmpeg produced,
    so the fake ffmpeg cannot just write ``b"FLAC"`` -- a tag write against that
    raises.  This is the smallest thing mutagen accepts: the ``fLaC`` marker and
    a single STREAMINFO block (marked last, 34 bytes) describing a 44.1 kHz
    stereo 16-bit stream of zero samples, with no audio frames after it.

    Built here rather than checked in as a binary so what it contains is
    readable, and so it needs no ffmpeg (or any other tool) to regenerate.
    """
    value = 0
    bit_count = 0

    def push(number: int, width: int) -> None:
        nonlocal value, bit_count
        value = (value << width) | number
        bit_count += width

    push(4096, 16)  # minimum block size
    push(4096, 16)  # maximum block size
    push(0, 24)  # minimum frame size, 0 = unknown
    push(0, 24)  # maximum frame size, 0 = unknown
    push(44100, 20)  # sample rate
    push(2 - 1, 3)  # channels, stored as channels - 1
    push(16 - 1, 5)  # bits per sample, stored as bits - 1
    push(total_samples, 36)  # total samples, 0 = unknown
    assert bit_count == 144

    streaminfo = value.to_bytes(bit_count // 8, "big") + bytes(16)  # + MD5 of nothing
    return b"fLaC" + bytes([0x80, 0x00, 0x00, len(streaminfo)]) + streaminfo


# Smallest byte strings that carry each format's magic number.  The pipeline
# sniffs those bytes to pick a picture's MIME type and never decodes the image,
# so nothing more than the header has to be real.
TINY_JPEG = b"\xff\xd8\xff\xe0" + bytes(16) + b"\xff\xd9"
TINY_PNG = b"\x89PNG\r\n\x1a\n" + bytes(16)
TINY_WEBP = b"RIFF" + bytes(4) + b"WEBPVP8 " + bytes(16)

_FFMPEG_OUTPUT_BYTES = {
    ".flac": None,  # filled in per call: minimal_flac_bytes()
    ".jpg": TINY_JPEG,
    ".jpeg": TINY_JPEG,
    ".png": TINY_PNG,
}


class FakePipe:
    """One end of a fake process's stdout/stderr, so closing it is observable."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeFfmpegProcess:
    """One fake ffmpeg run: the subset of ``subprocess.Popen`` we actually use."""

    def __init__(
        self,
        command: list[str],
        gate: "threading.Event | None",
        returncode: int,
        stderr: bytes,
        ignore_terminate: bool = False,
    ) -> None:
        self.command = command
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.ignore_terminate = ignore_terminate
        # Popen opens both as pipes; the downloader has to close them on every
        # path, including the one where it never gets to communicate().
        self.stdout = FakePipe()
        self.stderr = FakePipe()
        self._gate = gate
        self._planned_returncode = returncode
        self._stderr = stderr
        self._stopped = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        """Close the pipes and reap, like ``Popen.__exit__``."""
        self.stdout.close()
        self.stderr.close()
        self.wait()

    def communicate(self, timeout=None):
        """Block until released, signalled or *timeout* expires; then write the
        output file.

        The timeout is honoured rather than ignored so a test can make the
        process ignore SIGTERM and still watch the worker escalate to SIGKILL.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        if self._gate is not None:
            while not self._gate.is_set() and not self._stopped.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(cmd=self.command, timeout=timeout)
                time.sleep(0.005)
        if self._stopped.is_set():
            # Signalled mid-encode: no output file, the shell's SIGTERM code.
            self.returncode = -15
            return b"", b""
        if self._planned_returncode == 0:
            self._write_output()
        self.returncode = self._planned_returncode
        return b"", self._stderr

    def _write_output(self) -> None:
        """Create the file real ffmpeg would have written (always the last argv)."""
        target = Path(self.command[-1])
        suffix = target.suffix.lower()
        if suffix not in _FFMPEG_OUTPUT_BYTES:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_FFMPEG_OUTPUT_BYTES[suffix] or minimal_flac_bytes())

    def terminate(self) -> None:
        self.terminated = True
        if not self.ignore_terminate:
            self._stopped.set()

    def kill(self) -> None:
        self.killed = True
        self._stopped.set()

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = -15
        return self.returncode


class FakeFfmpeg:
    """Replacement for ``subprocess.Popen`` inside the downloader.

    Attributes are the knobs a test turns before triggering a run:

    * ``returncode`` / ``stderr`` -- make the next runs fail like ffmpeg would.
    * ``gate`` -- an unset ``threading.Event`` makes every run block until it is
      set or the process is signalled, which is what a cancel-during-converting
      test needs to be able to cancel at all.
    * ``error`` -- an exception raised instead of spawning (``FileNotFoundError``
      is ffmpeg missing from the image).
    * ``ignore_terminate`` -- the run keeps going through a SIGTERM, like an
      ffmpeg that is wedged; only ``kill`` stops it.
    """

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.processes: list[FakeFfmpegProcess] = []
        self.started = threading.Event()
        self.returncode = 0
        self.stderr = b""
        self.gate: threading.Event | None = None
        self.error: Exception | None = None
        self.ignore_terminate = False

    def __call__(self, command, **kwargs):
        if self.error is not None:
            raise self.error
        self.commands.append(list(command))
        self.kwargs = kwargs
        process = FakeFfmpegProcess(
            list(command),
            self.gate,
            self.returncode,
            self.stderr,
            self.ignore_terminate,
        )
        self.processes.append(process)
        self.started.set()
        return process

    def command_for(self, output_suffix: str) -> list[str]:
        """Return the recorded run whose output file ends in *output_suffix*."""
        for command in self.commands:
            if command[-1].lower().endswith(output_suffix):
                return command
        raise AssertionError(
            f"No ffmpeg run wrote a {output_suffix} file; got {self.commands}"
        )


@pytest.fixture(autouse=True)
def fake_ffmpeg():
    """Replace the downloader's ffmpeg with :class:`FakeFfmpeg` for every test."""
    fake = FakeFfmpeg()
    with patch("app.downloader.subprocess.Popen", new=fake):
        yield fake


# ---------------------------------------------------------------------------
# The network, closed
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_musicbrainz_or_cover_art():
    """Never let a test reach MusicBrainz or the Cover Art Archive.

    Both are reached from inside the queue: a job the API queues starts before
    the request returns, so a route test that only looked at the response body
    would otherwise fire a real lookup at a public service.  The stand-ins sit
    at the true boundary -- the ``musicbrainzngs`` module and ``httpx.Client``
    -- rather than at our own wrappers, because the wrappers' defaults are
    bound at import and a test cannot replace them after the fact.

    Tests that mean to exercise a lookup hand their own stub in (every function
    in ``app.tagger`` and ``app.album_tagger`` takes one), or patch these same
    attributes over this fixture.
    """
    import musicbrainzngs

    import app.album_tagger as album_tagger_module

    def refuse(*args, **kwargs):
        raise AssertionError(
            "a test tried to reach MusicBrainz or the Cover Art Archive; "
            "pass a stub search/fetch instead"
        )

    with (
        patch.object(musicbrainzngs, "search_recordings", side_effect=refuse),
        patch.object(musicbrainzngs, "get_release_by_id", side_effect=refuse),
        patch.object(album_tagger_module.httpx, "Client", side_effect=refuse),
    ):
        yield


@pytest.fixture(autouse=True)
def no_bandcamp_page_fetch():
    """Never let a test reach a Bandcamp seller's page.

    A probe of a Bandcamp artist or label URL reads the page once for the
    display name, and a route test that only looked at the response body would
    otherwise fire a real request at Bandcamp.  Returning None is the "page
    could not be read" case, in which the probe keeps the subdomain -- which is
    what every test expected before the page was read at all.

    Tests that are *about* the name patch the same attribute over this fixture.
    """
    import app.probe as probe_module

    with patch.object(probe_module, "resolve_bandcamp_artist_name", return_value=None):
        yield


# ---------------------------------------------------------------------------
# A stand-in for requests.Session
# ---------------------------------------------------------------------------
#
# Shared by every test of something that reads a public page through
# :mod:`app.fetch` -- Spotify's artist name and Bandcamp's display name so far.
# The bounded read is one piece of code now, so its stand-in is one too.


class FakeRaw:
    """The urllib3 response body, read the way :func:`app.fetch.get_text` reads it.

    ``read1`` hands back at most *amt* bytes and an empty ``bytes`` at the end,
    which is what the loop breaks on.
    """

    def __init__(self, body: bytes):
        self._body = body
        self._offset = 0
        self.reads = 0

    def read1(self, amt=-1, decode_content=True):
        self.reads += 1
        size = len(self._body) - self._offset if amt is None or amt < 0 else amt
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class FakeResponse:
    """One canned answer.

    *content_type* is the ``Content-Type`` header verbatim, so a test can leave
    the charset undeclared -- which is the case these sites actually serve -- or
    declare one.  ``encoding`` is deliberately absent: :func:`app.fetch.get_text` reads
    the header rather than ``requests``' ISO-8859-1 guess.
    """

    def __init__(self, status_code=200, body=b"", content_type=None):
        self.status_code = status_code
        self.raw = FakeRaw(body)
        self.headers = {} if content_type is None else {"Content-Type": content_type}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


class FakeSession:
    """``requests.Session`` with canned answers, keyed by URL.

    A value that is an ``Exception`` is raised, which is how a timeout is
    written; anything else is the response.  Every call is recorded so the
    tests can assert on the request itself -- the timeout, the redirect policy
    and the User-Agent are as much of the contract as the body is.

    ``closed`` records :meth:`close`, because whether a caller closes a session
    it opened itself (and leaves alone one it was handed) is part of what the
    callers promise.
    """

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls: list[dict] = []
        self.closed = False

    def close(self):
        self.closed = True

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        answer = self.responses.get(url)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return FakeResponse(status_code=404)
        return answer
