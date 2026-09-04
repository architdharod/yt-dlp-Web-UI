"""Tests for the cover downscaler.

ffmpeg is not installed on developer machines or in CI (see the fake in
``conftest``), so ``subprocess.run`` is replaced here with a recorder that
answers the way ffmpeg would.  What matters is the argv the helper builds and
that every failure path hands the original bytes back rather than raising.
"""

import subprocess
from unittest.mock import patch

from app.cover_art import DEFAULT_MAX_PIXELS, downscale_cover

from tests.conftest import TINY_JPEG

SMALLER = b"\xff\xd8\xff\xe0scaled\xff\xd9"


class FakeRun:
    """Stands in for ``subprocess.run``: records argv, replays a canned result."""

    def __init__(self, stdout: bytes = SMALLER, returncode: int = 0, error=None) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.command: list[str] | None = None
        self.stdin: bytes | None = None
        self.timeout: float | None = None

    def __call__(self, command, input=None, capture_output=False, timeout=None, check=False):
        self.command = list(command)
        self.stdin = input
        self.timeout = timeout
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(
            args=command, returncode=self.returncode, stdout=self.stdout, stderr=b"boom"
        )


def run_with(fake: FakeRun, data: bytes = TINY_JPEG, **kwargs) -> bytes:
    with patch("app.cover_art.subprocess.run", new=fake):
        return downscale_cover(data, **kwargs)


class TestDownscaleCover:
    def test_returns_the_scaled_bytes(self):
        fake = FakeRun()
        assert run_with(fake) == SMALLER
        assert fake.stdin == TINY_JPEG

    def test_command_caps_the_width_without_upscaling(self):
        fake = FakeRun()
        run_with(fake)
        command = fake.command
        assert command[0] == "ffmpeg"
        # The whole filter, quotes included, is one argv element: there is no
        # shell here to strip them, and ffmpeg needs them to read the comma.
        assert f"scale='min({DEFAULT_MAX_PIXELS},iw)':-2" in command
        assert command[-1] == "pipe:1"
        assert "pipe:0" in command
        assert fake.timeout is not None

    def test_max_px_is_honoured(self):
        fake = FakeRun()
        run_with(fake, max_px=500)
        assert "scale='min(500,iw)':-2" in fake.command

    def test_empty_input_never_starts_ffmpeg(self):
        fake = FakeRun()
        assert run_with(fake, data=b"") == b""
        assert fake.command is None

    def test_missing_ffmpeg_keeps_the_original(self):
        fake = FakeRun(error=FileNotFoundError("ffmpeg"))
        assert run_with(fake) == TINY_JPEG

    def test_non_zero_exit_keeps_the_original(self):
        assert run_with(FakeRun(returncode=1)) == TINY_JPEG

    def test_timeout_keeps_the_original(self):
        fake = FakeRun(error=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=20))
        assert run_with(fake) == TINY_JPEG

    def test_empty_output_keeps_the_original(self):
        assert run_with(FakeRun(stdout=b"")) == TINY_JPEG

    def test_spawn_failure_keeps_the_original(self):
        assert run_with(FakeRun(error=OSError("no fork for you"))) == TINY_JPEG
