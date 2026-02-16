# Use slim Python base instead of full Playwright image (saves ~300-400 MB)
# Only Chromium is installed, not Firefox/WebKit
FROM python:3.14-slim-bookworm@sha256:f0540d0436a220db0a576ccfe75631ab072391e43a24b88972ef9833f699095f

# Install uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:7a88d4c4e6f44200575000638453a5a381db0ae31ad5c3a51b14f8687c9d93a3 /uv /uvx /bin/

# Create non-root user first (matching original pwuser from Playwright image)
RUN useradd -m -s /bin/bash pwuser

# Set working directory and ownership
WORKDIR /app
RUN chown pwuser:pwuser /app

# Copy project files with correct ownership
COPY --chown=pwuser:pwuser . /app

# Install git (needed for git-based dependencies in pyproject.toml)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Set browser install location (Patchright reads PLAYWRIGHT_BROWSERS_PATH internally)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/patchright
# Install dependencies, system libs for Chromium, and patched Chromium binary
RUN uv sync --frozen && \
    uv run patchright install-deps chromium && \
    uv run patchright install chromium && \
    chmod -R 755 /opt/patchright

# Fix ownership of app directory (venv created by uv)
RUN chown -R pwuser:pwuser /app

# Switch to non-root user
USER pwuser

# Set entrypoint and default arguments
ENTRYPOINT ["uv", "run", "-m", "linkedin_mcp_server"]
CMD []
