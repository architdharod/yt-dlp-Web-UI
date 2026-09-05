"""The optional YTDLP_COOKIES_FILE cookie jar.

Covers the things that can go wrong with it: the jar silently not reaching
yt-dlp, a misconfigured path failing late instead of at boot, and -- the
reason the jar is shared rather than a `cookiefile` per YoutubeDL -- yt-dlp
writing the jar back over the user's own file when it closes.
"""

import os
import threading

import pytest
import yt_dlp.cookies
from fastapi.testclient import TestClient

from app import downloader
from app.downloader import (
    COOKIES_ENV_VAR,
    base_opts,
    cookies_file,
    load_cookie_jar,
    validate_cookies_file,
    ytdl,
)
from app.main import app

COOKIE_TEXT = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t2000000000\tSID\tsecret\n"
)


@pytest.fixture(autouse=True)
def reset_cookie_jar(monkeypatch):
    """Each test starts from an unset variable and no loaded jar."""
    monkeypatch.delenv(COOKIES_ENV_VAR, raising=False)
    monkeypatch.setattr(downloader, "_cookie_jar", None)
    yield
    monkeypatch.setattr(downloader, "_cookie_jar", None)


@pytest.fixture
def cookies_path(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(COOKIE_TEXT)
    return path


# --- base_opts ------------------------------------------------------------


def test_base_opts_never_sets_cookiefile_when_unset():
    assert "cookiefile" not in base_opts()


def test_base_opts_never_sets_cookiefile_when_cookies_are_configured(
    monkeypatch, cookies_path
):
    """`cookiefile` is what makes yt-dlp write the jar back; it stays unset."""
    monkeypatch.setenv(COOKIES_ENV_VAR, str(cookies_path))
    load_cookie_jar()
    assert "cookiefile" not in base_opts()


def test_base_opts_keeps_the_shared_options():
    opts = base_opts()
    assert opts["noplaylist"] is True
    assert opts["allowed_extractors"] == downloader.ALLOWED_EXTRACTORS


def test_base_opts_returns_a_fresh_dict_each_call():
    first = base_opts()
    first["mutated"] = True
    assert "mutated" not in base_opts()


def test_empty_string_counts_as_unset(monkeypatch):
    # docker compose substitutes an unset variable with "".
    monkeypatch.setenv(COOKIES_ENV_VAR, "")
    assert cookies_file() is None
    load_cookie_jar()
    assert downloader._cookie_jar is None


# --- the shared jar -------------------------------------------------------


def test_load_reads_the_configured_file(monkeypatch, cookies_path):
    monkeypatch.setenv(COOKIES_ENV_VAR, str(cookies_path))
    load_cookie_jar()
    jar = downloader._cookie_jar
    assert isinstance(jar, yt_dlp.cookies.YoutubeDLCookieJar)
    assert [cookie.name for cookie in jar] == ["SID"]


def test_ytdl_attaches_the_jar(monkeypatch, cookies_path):
    monkeypatch.setenv(COOKIES_ENV_VAR, str(cookies_path))
    load_cookie_jar()
    jar = downloader._cookie_jar
    with ytdl(base_opts()) as ydl:
        assert ydl.cookiejar is jar


def test_every_ytdl_gets_the_same_jar(monkeypatch, cookies_path):
    """One jar, so a cookie YouTube rotates in is there for the next job."""
    monkeypatch.setenv(COOKIES_ENV_VAR, str(cookies_path))
    load_cookie_jar()
    with ytdl(base_opts()) as first, ytdl(base_opts()) as second:
        assert first.cookiejar is second.cookiejar


def test_ytdl_leaves_the_default_jar_alone_when_unconfigured():
    with ytdl(base_opts()) as ydl:
        assert isinstance(ydl.cookiejar, yt_dlp.cookies.YoutubeDLCookieJar)
        assert len(ydl.cookiejar) == 0


def test_closing_writes_nothing_back_to_the_configured_file(
    monkeypatch, cookies_path
):
    """The README tells people to bind-mount the file read-only.

    yt-dlp dumps its jar back into ``cookiefile`` on close; with the jar
    attached and ``cookiefile`` unset, ``save_cookies`` short-circuits and the
    source file is untouched.
    """
    monkeypatch.setenv(COOKIES_ENV_VAR, str(cookies_path))
    load_cookie_jar()
    before = cookies_path.read_bytes()

    with ytdl(base_opts()) as ydl:
        ydl.cookiejar.set_cookie(
            yt_dlp.cookies.http.cookiejar.Cookie(
                0, "ROTATED", "new", None, False,
                ".youtube.com", True, True, "/", True,
                True, 2000000000, False, None, None, {},
            )
        )

    assert cookies_path.read_bytes() == before


def test_a_read_only_cookies_file_is_usable(monkeypatch, cookies_path):
    cookies_path.chmod(0o444)
    monkeypatch.setenv(COOKIES_ENV_VAR, str(cookies_path))
    validate_cookies_file()
    with ytdl(base_opts()) as ydl:
        assert len(ydl.cookiejar) == 1


def test_concurrent_ytdl_contexts_share_one_jar_safely(monkeypatch, cookies_path):
    """What the per-process `cookiefile` copy got wrong, under threads.

    Several downloads open and close a YoutubeDL at once on the one jar: no
    errors, and the configured file is byte-identical afterwards.
    """
    monkeypatch.setenv(COOKIES_ENV_VAR, str(cookies_path))
    load_cookie_jar()
    before = cookies_path.read_bytes()
    jar = downloader._cookie_jar

    start = threading.Barrier(8)
    errors: list[BaseException] = []
    seen: list[object] = []

    def worker() -> None:
        try:
            start.wait(timeout=10)
            for _ in range(10):
                with ytdl(base_opts()) as ydl:
                    seen.append(ydl.cookiejar)
                    ydl.cookiejar.get_cookie_header("https://www.youtube.com/")
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(seen) == 80
    assert all(item is jar for item in seen)
    assert cookies_path.read_bytes() == before


def test_the_cookie_contents_are_never_logged(monkeypatch, cookies_path, caplog):
    monkeypatch.setenv(COOKIES_ENV_VAR, str(cookies_path))
    with caplog.at_level("DEBUG"):
        validate_cookies_file()
        with ytdl(base_opts()):
            pass
    assert "secret" not in caplog.text


# --- startup validation ---------------------------------------------------


def test_validate_passes_when_unset():
    validate_cookies_file()  # does not raise


def test_validate_loads_the_jar(monkeypatch, cookies_path):
    monkeypatch.setenv(COOKIES_ENV_VAR, str(cookies_path))
    validate_cookies_file()
    assert downloader._cookie_jar is not None


def test_validate_rejects_a_missing_file(monkeypatch, tmp_path):
    missing = tmp_path / "nope.txt"
    monkeypatch.setenv(COOKIES_ENV_VAR, str(missing))
    with pytest.raises(RuntimeError) as excinfo:
        validate_cookies_file()
    message = str(excinfo.value)
    assert COOKIES_ENV_VAR in message
    assert str(missing) in message
    assert "does not exist" in message


def test_validate_rejects_a_malformed_file(monkeypatch, tmp_path, cookies_path):
    """A JSON export named cookies.txt is the classic mistake.

    A good file is loaded first, so the `is None` assertion below is about
    the failed load clearing the jar and not about the fixture never having
    set one.
    """
    monkeypatch.setenv(COOKIES_ENV_VAR, str(cookies_path))
    validate_cookies_file()
    assert downloader._cookie_jar is not None

    path = tmp_path / "bad.txt"
    path.write_text('{"cookies": [{"name": "SID", "value": "secret"}]}\n')
    monkeypatch.setenv(COOKIES_ENV_VAR, str(path))
    with pytest.raises(RuntimeError) as excinfo:
        validate_cookies_file()
    message = str(excinfo.value)
    assert COOKIES_ENV_VAR in message
    assert "Netscape" in message
    assert downloader._cookie_jar is None


def test_validate_rejects_a_binary_file(monkeypatch, tmp_path):
    """A cookies.sqlite or a UTF-16 export: bytes that are not even text.

    The decode blows up as a UnicodeDecodeError rather than a LoadError, and
    must still come out as the same friendly message.
    """
    path = tmp_path / "cookies.txt"
    path.write_bytes(b"\x00\x01\xff\xfe\x80\x81SQLite format 3\x00\xff")
    monkeypatch.setenv(COOKIES_ENV_VAR, str(path))
    with pytest.raises(RuntimeError) as excinfo:
        validate_cookies_file()
    message = str(excinfo.value)
    assert COOKIES_ENV_VAR in message
    assert "Netscape" in message
    assert downloader._cookie_jar is None


def test_validate_rejects_a_file_that_is_not_cookies_at_all(monkeypatch, tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text("just some text\n")
    monkeypatch.setenv(COOKIES_ENV_VAR, str(path))
    with pytest.raises(RuntimeError) as excinfo:
        validate_cookies_file()
    assert "Netscape" in str(excinfo.value)


def test_validate_rejects_a_directory(monkeypatch, tmp_path):
    """The /dev/null default bind-mounted at a missing host path.

    Docker creates a *directory* when the host path does not exist, so this is
    what a mistyped YTDLP_COOKIES_HOST_PATH actually looks like.
    """
    monkeypatch.setenv(COOKIES_ENV_VAR, str(tmp_path))
    with pytest.raises(RuntimeError) as excinfo:
        validate_cookies_file()
    message = str(excinfo.value)
    assert "not a regular file" in message
    assert "YTDLP_COOKIES_HOST_PATH" in message
    assert COOKIES_ENV_VAR in message


def test_validate_rejects_a_fifo(monkeypatch, tmp_path):
    """Any non-regular file, the way /dev/null is when compose mounts it.

    A FIFO stands in for the device node: it is creatable in a tmp_path and
    fails the same S_ISREG check.
    """
    fifo = tmp_path / "youtube.txt"
    os.mkfifo(fifo)
    monkeypatch.setenv(COOKIES_ENV_VAR, str(fifo))
    with pytest.raises(RuntimeError) as excinfo:
        validate_cookies_file()
    message = str(excinfo.value)
    assert "not a regular file" in message
    assert "YTDLP_COOKIES_HOST_PATH" in message


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_validate_rejects_an_unreadable_file(monkeypatch, cookies_path):
    cookies_path.chmod(0)
    monkeypatch.setenv(COOKIES_ENV_VAR, str(cookies_path))
    with pytest.raises(RuntimeError) as excinfo:
        validate_cookies_file()
    assert "not readable" in str(excinfo.value)


def test_startup_fails_when_the_cookies_file_is_missing(monkeypatch, tmp_path):
    """The app must refuse to boot, the way it does for a bad DATA_PATH."""
    monkeypatch.setenv(COOKIES_ENV_VAR, str(tmp_path / "gone.txt"))
    with pytest.raises(RuntimeError) as excinfo:
        with TestClient(app):
            pass
    assert COOKIES_ENV_VAR in str(excinfo.value)


def test_startup_loads_the_jar(monkeypatch, cookies_path):
    monkeypatch.setenv(COOKIES_ENV_VAR, str(cookies_path))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert downloader._cookie_jar is not None
