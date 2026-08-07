FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY epub_parser.py .
COPY classify.py .
COPY book_renderer.py .
COPY build_library.py .
# Book content itself is NOT baked into the image — it's on the GCS bucket
# mounted at /mnt/site (see tts-pipeline-infra/environments/dev/main.tf), so
# /import can add new books at runtime and every instance sees them
# immediately. The bulk-load path (build_library.py) writes to the same
# bucket via `gcloud storage rsync`, not into this image.

# --timeout 900: /import writes one file per chapter onto the GCS FUSE
# mount, which is much slower than local disk — measured ~0.18s/chapter,
# so the biggest book here (3543 chapters) needs ~630s. 900s gives margin.
CMD exec gunicorn --bind :${PORT:-8080} --workers 2 --threads 4 --timeout 900 server:app
