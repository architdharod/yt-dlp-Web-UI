"""Install a pinned deno release into the image, at build time.

Run from ``backend/Dockerfile`` as ``python3 scripts/install_deno.py <dest>``,
with the version and the per-architecture checksums passed in the environment
so that they stay visible in the Dockerfile itself.

This exists as a script rather than an inline ``RUN`` because the work is
awkward in shell: the base image has neither ``curl`` nor ``unzip``, and
installing them only to remove them again would cost an apt layer.  Python is
already there, and it can download, checksum, and unzip on its own.
"""

import hashlib
import os
import shutil
import stat
import sys
import tempfile
import time
import urllib.request
import zipfile

# docker's TARGETARCH values mapped to the deno release's target triples.
_TARGETS = {
    "amd64": "x86_64-unknown-linux-gnu",
    "arm64": "aarch64-unknown-linux-gnu",
}

# What `uname -m` reports, for a plain `docker build` that sets no TARGETARCH.
_MACHINES = {
    "x86_64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}

_URL = "https://github.com/denoland/deno/releases/download/v{version}/deno-{target}.zip"

# The GitHub release CDN drops connections often enough that a one-shot fetch
# makes the image build flaky, and a build that fails here has to be restarted
# from the top.  Retry a couple of times, backing off in between.
_ATTEMPTS = 3
_BACKOFF_SECONDS = 3
_TIMEOUT_SECONDS = 60


def _arch() -> str:
    arch = os.environ.get("TARGETARCH") or ""
    if not arch:
        machine = os.uname().machine
        arch = _MACHINES.get(machine, "")
        if not arch:
            raise SystemExit(f"install_deno: unsupported machine {machine!r}")
    if arch not in _TARGETS:
        raise SystemExit(f"install_deno: unsupported TARGETARCH {arch!r}")
    return arch


def _download(url: str, dest: str) -> None:
    """Fetch ``url`` to ``dest``, retrying a few times before giving up.

    Streamed rather than ``urlretrieve``d so the whole ~40MB archive is never
    held in memory, and so a read that dies mid-transfer raises where it can
    be retried.  The sha256 check in :func:`main` is what actually vouches for
    the bytes; this only has to get them all here.
    """
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response:
                with open(dest, "wb") as out:
                    shutil.copyfileobj(response, out, 1024 * 1024)
            return
        except OSError as exc:
            # URLError and the http.client errors are all OSErrors.
            print(
                f"install_deno: attempt {attempt}/{_ATTEMPTS} to fetch {url} "
                f"failed: {exc}",
                flush=True,
            )
            if attempt == _ATTEMPTS:
                raise SystemExit(
                    f"install_deno: could not download {url} after "
                    f"{_ATTEMPTS} attempts: {exc}"
                ) from exc
            time.sleep(_BACKOFF_SECONDS * attempt)


def main(dest: str) -> None:
    version = os.environ["DENO_VERSION"]
    arch = _arch()
    expected = os.environ[f"DENO_SHA256_{arch}"]
    url = _URL.format(version=version, target=_TARGETS[arch])

    print(f"install_deno: fetching {url}", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "deno.zip")
        _download(url, archive)

        digest = hashlib.sha256()
        with open(archive, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise SystemExit(
                f"install_deno: checksum mismatch for {url}: "
                f"expected {expected}, got {actual}"
            )

        with zipfile.ZipFile(archive) as archive_file:
            with archive_file.open("deno") as src, open(dest, "wb") as out:
                while chunk := src.read(1024 * 1024):
                    out.write(chunk)

    # World-readable and world-executable: the container runs as an arbitrary
    # PUID:PGID that is not known at build time.
    os.chmod(dest, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    print(f"install_deno: installed deno {version} ({arch}) to {dest}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/usr/local/bin/deno")
