# syntax=docker/dockerfile:1.7
# One image for the api, slackbot and sim services (uv's documented multi-stage pattern).

FROM ghcr.io/astral-sh/uv:0.8.0-python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# Dependencies first, so code changes don't invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN groupadd --system app && useradd --system --gid app --home-dir /app app \
    && mkdir -p /state && chown app:app /state
COPY --from=builder --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    STATE_DIR=/state
WORKDIR /app
USER app
EXPOSE 8000
CMD ["kaiku-api"]
