# Build stage: uv is only used here, never in the final image.
FROM python:3.12-slim AS builder

RUN pip install "uv>=0.11,<0.12" --no-cache-dir

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

# Final image: no uv, python invoked directly via the venv on PATH.
FROM python:3.12-slim

# The standard OCI label GHCR uses to link a pushed image to its source repo —
# without it, `docker push`/`kamal build push` still succeeds and the image is
# pullable, but it never shows up on the repo's own Packages tab.
LABEL org.opencontainers.image.source="https://github.com/verylift/verylift"

# python:3.12-slim's baked-in Debian packages lag behind Debian's own security
# repo between upstream image rebuilds (Trivy caught util-linux/CVE-2026-53615
# this way) -- pulling latest package versions at build time closes that gap
# without waiting on/pinning to a fresher upstream base image tag.
RUN apt-get update \
 && apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 999 nonroot \
 && useradd --system --gid 999 --uid 999 --create-home nonroot

COPY --from=builder --chown=nonroot:nonroot /opt/venv /opt/venv
COPY --from=builder --chown=nonroot:nonroot /app /app

# Ensure STATIC_ROOT/MEDIA_ROOT/data dirs exist and are writable by nonroot so
# their named volumes inherit nonroot ownership (collectstatic, user uploads,
# and the default SQLite database file for self-hosters not running Postgres).
RUN mkdir -p /app/staticfiles /app/media /app/data \
 && chown nonroot:nonroot /app/staticfiles /app/media /app/data

ENV PATH="/opt/venv/bin:$PATH"

# requests, httpx and sentry-sdk otherwise trust certifi's bundle in /opt/venv
# while urllib and SMTP use the base image's system CA bundle, so a CA distrust
# (e.g. the October 2026 eMudhra key compromise) only fully landed once both
# were updated. Pointing everything at the system bundle leaves one store, kept
# current by the `apt-get upgrade` above on every rebuild. This is the image's
# own bundle, not the host's: a container never reads the host's CA store, so
# every deployment trusts the same roots whatever OS it runs on.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

USER nonroot
WORKDIR /app

CMD ["gunicorn", "root.wsgi:application", "-c", "gunicorn.conf.py"]
