# -- Stage 1: Build virtual environment --
FROM python:3.13.13-slim-bookworm@sha256:eabbb62836ee44c18d350821e9f78488bcf65134bf763ae9989d63e611fa04d9 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:3b7b60a81d3c57ef471703e5c83fd4aaa33abcd403596fb22ab07db85ae91347 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev --no-editable --compile-bytecode

COPY . .
RUN uv sync --frozen --no-dev --no-editable --compile-bytecode


# -- Stage 2: Production runtime --
FROM python:3.13.13-slim-bookworm@sha256:eabbb62836ee44c18d350821e9f78488bcf65134bf763ae9989d63e611fa04d9

RUN useradd -m -s /bin/bash pwuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/patchright

RUN patchright install-deps chromium && \
    patchright install chromium && \
    chmod -R 755 /opt/patchright && \
    rm -rf /var/lib/apt/lists/*

USER pwuser

ENTRYPOINT ["python", "-m", "linkedin_mcp_server"]
CMD []
