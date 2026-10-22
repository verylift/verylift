# Build stage: uv is only used here, never in the final image.
FROM python:3.12-slim AS builder

# Hash-pinned (PyPI's linux x86_64/aarch64 wheel hashes) because the base
# image's pip can trust compromised CAs via its vendored certifi. Keep the
# version in step with [tool.uv] required-version in pyproject.toml.
RUN echo 'uv==0.13.5' \
      '--hash=sha256:fa8d26e08e34c29c0c4e820cf3181757086d547240b4f4d31ff1d8bd810ca946' \
      '--hash=sha256:2386c2dc9b59fc1d39b1c500e4319d71d3268232eb3b38e29cd6775d6269e8d4' \
      '--hash=sha256:91e58e304b63b6c91bac5561459ab0c3ea24f17bc01ea9359d69fed72142978f' \
    | pip install --require-hashes --no-cache-dir -r /dev/stdin

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# The ln gives certifi's CA bundle a stable name across Python upgrades (see
# the final stage's SSL_CERT_FILE); COPY --from=builder carries the symlink.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project \
 && ln -s "$(/opt/venv/bin/python -m certifi)" /opt/venv/cacert.pem

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

# Every outbound TLS client (requests, httpx, sentry-sdk, urllib, SMTP) trusts
# one CA store: certifi's, in /opt/venv. It tracks Mozilla's root removals,
# which Debian's bundle lags (Debian's still carries roots browsers distrust),
# and an outdated certifi fails the weekly pip-audit run. SSL_CERT_DIR names an
# empty path so OpenSSL stops also reading Debian's /etc/ssl/certs. This is the
# image's own store, not the host's, whatever OS a deployment runs on.
ENV SSL_CERT_FILE=/opt/venv/cacert.pem \
    SSL_CERT_DIR=/nonexistent

USER nonroot
WORKDIR /app

CMD ["gunicorn", "root.wsgi:application", "-c", "gunicorn.conf.py"]
