# Use slim Python base instead of full Playwright image (saves ~300-400 MB)
# Only Chromium is installed, not Firefox/WebKit
FROM python:3.14-slim-bookworm@sha256:55e465cb7e50cd1d7217fcb5386aa87d0356ca2cd790872142ef68d9ef6812b4

# Install uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:3472e43b4e738cf911c99d41bb34331280efad54c73b1def654a6227bb59b2b4 /uv /uvx /bin/

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
