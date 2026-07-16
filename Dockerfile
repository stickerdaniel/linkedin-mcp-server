# -- Stage 1: Build virtual environment --
FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f AS builder

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:b46b03ddfcfbf8f547af7e9eaefdf8a39c8cebcba7c98858d3162bd28cf536f6 /uv /uvx /bin/

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
    playwright install-deps firefox && \
    patchright install chromium && \
    chmod -R 755 /opt/patchright && \
    rm -rf /var/lib/apt/lists/*

ENV HOME=/home/pwuser

# Camoufox resolves its browser cache from HOME, but its GeoIP downloader writes
# into the root-owned virtualenv package directory. Use the project's guarded
# fetch so the cache is published only after all assets pass validation, then
# hand only the user-cache tree to the non-root process.
RUN python -c "import asyncio; from linkedin_mcp_server.bootstrap import _run_camoufox_fetch; asyncio.run(_run_camoufox_fetch())" && \
    chown -R pwuser:pwuser /home/pwuser/.cache

USER pwuser

RUN python -c "from linkedin_mcp_server.bootstrap import camoufox_ready; raise SystemExit(0 if camoufox_ready() else 1)"

ENTRYPOINT ["python", "-m", "linkedin_mcp_server"]
CMD []
