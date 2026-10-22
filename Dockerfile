# Build stage: uv is only used here, never in the final image.
FROM python:3.12-slim AS builder

# Hash-pinned because the base image's pip trusts its own vendored certifi on
# top of the system store, and so still trusted the CAs compromised in October
# 2026 (eMudhra) until pip 26.2.2: the hashes keep a tampered download out
# whatever pip's TLS trusts. Hashes are PyPI's for the linux x86_64/aarch64
# wheels. Keep the version in step with [tool.uv] required-version in
# pyproject.toml.
RUN printf '%s\n' 'uv==0.13.5 \' \
      '--hash=sha256:fa8d26e08e34c29c0c4e820cf3181757086d547240b4f4d31ff1d8bd810ca946 \' \
      '--hash=sha256:2386c2dc9b59fc1d39b1c500e4319d71d3268232eb3b38e29cd6775d6269e8d4 \' \
      '--hash=sha256:91e58e304b63b6c91bac5561459ab0c3ea24f17bc01ea9359d69fed72142978f' \
      > /tmp/uv-requirements.txt \
 && pip install --require-hashes --no-cache-dir -r /tmp/uv-requirements.txt \
 && rm /tmp/uv-requirements.txt

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

USER nonroot
WORKDIR /app

CMD ["gunicorn", "root.wsgi:application", "-c", "gunicorn.conf.py"]
