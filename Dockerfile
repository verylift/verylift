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

RUN groupadd --system --gid 999 nonroot \
 && useradd --system --gid 999 --uid 999 --create-home nonroot

COPY --from=builder --chown=nonroot:nonroot /opt/venv /opt/venv
COPY --from=builder --chown=nonroot:nonroot /app /app

# Ensure STATIC_ROOT/MEDIA_ROOT exist and are writable by nonroot so their
# named volumes inherit nonroot ownership (collectstatic and user uploads).
RUN mkdir -p /app/staticfiles /app/media \
 && chown nonroot:nonroot /app/staticfiles /app/media

ENV PATH="/opt/venv/bin:$PATH"

USER nonroot
WORKDIR /app

CMD ["gunicorn", "root.wsgi:application", "-c", "gunicorn.conf.py"]
