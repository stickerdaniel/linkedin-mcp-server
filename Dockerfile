# -- Stage 1: Build virtual environment --
FROM python:3.14-slim-bookworm@sha256:55e465cb7e50cd1d7217fcb5386aa87d0356ca2cd790872142ef68d9ef6812b4 AS builder

# Install uv package manager (pinned by sha256)
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:90bbb3c16635e9627f49eec6539f956d70746c409209041800a0280b93152823 /uv /uvx /bin/

# Install git (needed for git-based dependencies in pyproject.toml)
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Copy dependency files first to maximize layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies (ignoring the app project itself for now)
RUN uv sync --frozen --no-install-project

# Copy the actual project source and finish the sync installation
COPY . .
RUN uv sync --frozen


# -- Stage 2: Production runtime --
FROM python:3.14-slim-bookworm@sha256:55e465cb7e50cd1d7217fcb5386aa87d0356ca2cd790872142ef68d9ef6812b4

# Create non-root user
RUN useradd -m -s /bin/bash pwuser

WORKDIR /app

# Copy the built virtual environment from the builder stage
COPY --from=builder /app/.venv /app/.venv

# Prepend the virtual environment binaries to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Set browser install location for Patchright
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/patchright

# Install OS-level dependencies and Chromium as root
RUN patchright install-deps chromium && \
    patchright install chromium && \
    chmod -R 755 /opt/patchright && \
    rm -rf /var/lib/apt/lists/*

# Copy the runtime application codebase.
# Being copied by root makes the code read-only to pwuser, significantly reducing attack surface.
COPY . /app/

# Create a writable directory for browser caching or dynamic data if needed
RUN mkdir -p /app/data && chown pwuser:pwuser /app/data

# Switch to the non-privileged user for runtime bounds
USER pwuser

# Execute Python directly from the virtual environment (uv is not shipped in this container stage)
ENTRYPOINT ["python", "-m", "linkedin_mcp_server"]
CMD []
