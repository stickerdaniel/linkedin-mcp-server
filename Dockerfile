# -- Stage 1: Build virtual environment --
FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f AS builder

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:606e70c71c852d03f611b1e56a195d08648507018a7057fab82c4974c4eae105 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev --no-editable --compile-bytecode

# The project itself is installed separately, because installing it runs
# `setuptools.build_meta` and `uv sync` has no way to constrain that build. The
# distributions published from CI pin the backend against this same hashed file
# (#654); without this the image would resolve setuptools fresh from PyPI in the
# one job that holds the Docker Hub credentials. `--no-deps` keeps the runtime
# dependencies exactly as the frozen lock installed them above, so this adds the
# project and nothing else.
COPY . .
RUN uv pip install --python /app/.venv/bin/python --no-deps --compile-bytecode \
    --build-constraints build-constraints.txt .


# -- Stage 2: Production runtime --
FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f

# The official MCP Registry proves ownership of an image by pulling its config
# and comparing Config.Labels["io.modelcontextprotocol.server.name"] to the name
# in server.json. A LABEL instruction is what writes that; a manifest annotation
# is a different surface it never reads. It has to be on the runtime stage,
# because the builder's labels never ship.
LABEL io.modelcontextprotocol.server.name="io.github.stickerdaniel/linkedin-mcp-server"

RUN useradd -m -s /bin/bash pwuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/patchright

# One-shot CLI modes re-run the installer to record profile-local metadata. It
# exits without downloading when this exact revision is present, but still needs
# to acquire its cache lock as pwuser.
RUN patchright install-deps chromium && \
    patchright install chromium --no-shell && \
    apt-get update && apt-get install -y --no-install-recommends \
        tini openbox x11vnc novnc websockify && \
    chown -R pwuser:pwuser /opt/patchright && \
    chmod -R 755 /opt/patchright && \
    rm -rf /var/lib/apt/lists/*

# Docker seeds a fresh named volume from this directory. Bind mounts retain the
# host directory's ownership and are prepared by the documented login command.
RUN install -d -m 0700 -o pwuser -g pwuser /home/pwuser/.linkedin-mcp

COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/linkedin-mcp-entrypoint

# A full headed browser on a virtual display. No window reaches the host, and
# HEADLESS stays overridable for anyone who deliberately wants Chromium's real
# headless mode. DISPLAY is fixed inside one container, where there is only one
# X server to collide with.
ENV DISPLAY=:99
ENV HEADLESS=false

USER pwuser

# Tini signals the supervisor, which gives Python its graceful shutdown before
# stopping Xvfb. Keeping the display alive is required while an active login
# browser closes. A handled SIGTERM can still report 143, so -e maps that
# expected `docker stop` path to success. Without an init, Chromium subprocesses
# orphaned onto PID 1 would also accumulate as zombies.
ENTRYPOINT ["tini", "-e", "143", "--", "linkedin-mcp-entrypoint", "python", "-m", "linkedin_mcp_server"]
CMD []
