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
    patchright install chromium --no-shell && \
    apt-get update && apt-get install -y --no-install-recommends tini && \
    chmod -R 755 /opt/patchright && \
    rm -rf /var/lib/apt/lists/*

COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/linkedin-mcp-entrypoint

# A full headed browser on a virtual display. No window reaches the host, and
# HEADLESS stays overridable for anyone who deliberately wants Chromium's real
# headless mode. DISPLAY is fixed inside one container, where there is only one
# X server to collide with.
ENV DISPLAY=:99
ENV HEADLESS=false

USER pwuser

# -g sends TERM to the whole process group: Python gets to run FastMCP's
# graceful shutdown, and Xvfb leaves with it. The entrypoint supervises both in
# that group, so either one dying terminates the other instead of leaving a live
# server with no display. A handled SIGTERM can still report 143, so -e maps
# that expected `docker stop` path to success. Without an init, Chromium
# subprocesses orphaned onto PID 1 would also accumulate as zombies.
ENTRYPOINT ["tini", "-g", "-e", "143", "--", "linkedin-mcp-entrypoint", "python", "-m", "linkedin_mcp_server"]
CMD []
