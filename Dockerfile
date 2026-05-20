# -- Stage 1: Build virtual environment --
FROM python:3.13.13-slim-bookworm@sha256:85cea6e0fd8234bb1ae0615828656e3e80c5876bd6f953f40480fb48566de6a6 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev --no-editable --compile-bytecode

COPY . .
RUN uv sync --frozen --no-dev --no-editable --compile-bytecode


# -- Stage 2: Production runtime --
FROM python:3.13.13-slim-bookworm@sha256:85cea6e0fd8234bb1ae0615828656e3e80c5876bd6f953f40480fb48566de6a6

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
