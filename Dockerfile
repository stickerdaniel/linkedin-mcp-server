# -- Stage 1: Build virtual environment --
FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f AS builder

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:606e70c71c852d03f611b1e56a195d08648507018a7057fab82c4974c4eae105 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev --no-editable --compile-bytecode

COPY . .
RUN uv sync --frozen --no-dev --no-editable --compile-bytecode


# -- Stage 2: Production runtime --
FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f

RUN useradd -m -s /bin/bash pwuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/patchright

RUN patchright install-deps chromium && \
    patchright install chromium && \
    apt-get update && apt-get install -y --no-install-recommends tini && \
    chmod -R 755 /opt/patchright && \
    rm -rf /var/lib/apt/lists/*

USER pwuser

# tini reaps the chromium subprocesses Patchright orphans onto PID 1; without an
# init the container leaks zombie chrome-headless processes over its lifetime.
ENTRYPOINT ["tini", "--", "python", "-m", "linkedin_mcp_server"]
CMD []
