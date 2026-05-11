# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS base

# uv: fast Python package installer. Pinned by digest in a real prod build.
COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev

COPY app/ ./app/
# alembic.ini + alembic/ ship in the image so the `migrate` compose service
# can run `alembic upgrade head` against the same DB the Hub will use.
# Operational scripts (manage_customer.py) are likewise inside the image so
# `docker compose exec hub uv run python -m scripts.manage_customer ...`
# works in any environment.
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY scripts/ ./scripts/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
