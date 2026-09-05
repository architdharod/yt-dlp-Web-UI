FROM python:3.12-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Deno, the JavaScript runtime yt-dlp needs to solve YouTube's player
# challenges.  Since 2026 yt-dlp's YouTube extractor runs its EJS scripts in an
# external JS runtime; without one it logs "No supported JavaScript runtime
# could be found", loses formats, and is much likelier to be answered with
# "Sign in to confirm you're not a bot".  deno is the only runtime yt-dlp
# enables by default, so it is the one the image ships.
#
# DENO_VERSION must track the version yt-dlp itself pins in its `pin-deno`
# extra; CI's `deno-pin-check` job reads that pin out of the installed yt-dlp
# metadata and fails if the two drift, so bump it here when that check
# complains -- the image itself still builds meanwhile.  The binary is fetched
# from the GitHub release and checked against the sha256 that release
# publishes; the two DENO_SHA256_* values have to be bumped with it.
# TARGETARCH picks the asset so the image builds on both amd64 (the homelab)
# and arm64 (Apple Silicon).
ARG TARGETARCH
ARG DENO_VERSION=2.9.5
ARG DENO_SHA256_amd64=8b010a3b1a4a0188a67cdb8a7a27348b2a501af78aec7fc74f2ace167368d530
ARG DENO_SHA256_arm64=6b7cae3a8fc4385a59dea3146fcb8bad7fea4230e0ad36a8c692afacbc254be0
COPY scripts/install_deno.py /tmp/install_deno.py
RUN python3 /tmp/install_deno.py /usr/local/bin/deno && \
    rm -f /tmp/install_deno.py && \
    deno --version

# deno insists on a writable cache directory.  The container runs as an
# arbitrary uid with no passwd entry, so the default (~/.cache/deno) would sit
# under a home nobody owns; HOME is /tmp below, and this keeps the two
# together explicitly rather than by luck.
ENV DENO_DIR=/tmp/deno

WORKDIR /app

# Pre-create the DATA_PATH mount point.  A fresh named volume inherits the
# mode of the image directory it is mounted over; without this the directory
# Docker creates is root:root 0755 and the container -- which runs as an
# arbitrary ${PUID}:${PGID} with no passwd entry -- cannot write queue.db.
# 0777 rather than a chown because PUID/PGID are not known at build time.
RUN mkdir -p /config && chmod 0777 /config

# The container runs as an arbitrary uid with no passwd entry, so HOME is
# unset; yt-dlp needs a writable home for its cache.
ENV HOME=/tmp

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--forwarded-allow-ips=*"]
