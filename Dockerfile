# Production image for the Signal Flask app, targeting AWS Lightsail
# Container Service (linux/amd64 -- build with `docker build --platform
# linux/amd64`, since Lightsail doesn't run arm64).
#
# Multi-stage: build-essential/gcc (needed to compile some packages' C
# extensions) never ends up in the final image, only the compiled result.
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# CPU-only torch, installed BEFORE the rest of requirements-docker.txt so
# pip resolves sentence-transformers' torch dependency against this build
# rather than pulling the default GPU/CUDA wheel. Functionally identical
# for this app: Lightsail containers have no GPU, so the CUDA build's
# ~5.3GB of nvidia-*/triton packages (verified via `du -sh site-packages/*`
# on the unoptimized image) were 100% dead weight, never exercised by any
# code path -- same inference results, same API, CPU execution either way.
COPY requirements-docker.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements-docker.txt

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY . .

# Never bake the local dev database, uploaded documents, logs, or secrets
# into the image -- see .dockerignore. The app creates an empty SQLite DB
# at these paths on first run (storage/database.py::init_db()) if none
# exists; on Lightsail's ephemeral containers this means a fresh, empty
# database on every restart unless the app is pointed at an external DB
# (this deployment keeps the app's current SQLite-by-default architecture
# unchanged, per the task's explicit "do not change the database design").
#
# config.settings.ensure_data_dirs() normally creates these, but it's only
# ever called from main.py's CLI paths (`main.py serve`) -- gunicorn
# imports web.app:create_app() directly, bypassing main.py entirely, so
# these are created here instead rather than patching create_app() itself
# (keeps this a deployment-layer concern, not an application code change).
RUN mkdir -p /app/data/raw /app/data/normalized /app/data/documents /app/data/charts /app/logs

# Lightsail Container Service (like most container platforms) injects the
# listen port via $PORT -- default here only matters for `docker run`
# without -e PORT set, e.g. local testing.
ENV PORT=8080
EXPOSE 8080

# gunicorn, not the Flask dev server -- create_app() is a factory
# (web/app.py), so gunicorn needs the factory call, not a bare module
# attribute: "web.app:create_app()".
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 2 --timeout 120 'web.app:create_app()'"]
