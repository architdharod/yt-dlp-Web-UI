FROM python:3.12-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

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
