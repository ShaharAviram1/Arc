# syntax=docker/dockerfile:1

# ---- builder: resolve and install dependencies into /app/.venv -------------
FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so the layer is reused when only source changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# ---- runtime --------------------------------------------------------------
FROM python:3.14-slim AS runtime

# ffmpeg/ffprobe are used by the worker's transcode jobs (M7); the api image
# is the same image, so it carries them too.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 arc

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

WORKDIR /app
COPY --from=builder --chown=arc:arc /app /app

RUN mkdir -p /data/downloads /data/renditions /data/fonts && chown -R arc:arc /data

USER arc
EXPOSE 8000

# Compose overrides this for the worker service.
CMD ["uvicorn", "arc.main:app", "--host", "0.0.0.0", "--port", "8000"]
