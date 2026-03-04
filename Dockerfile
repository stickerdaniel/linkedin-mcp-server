# Use slim Python base instead of full Playwright image (saves ~300-400 MB)
# Only Chromium is installed, not Firefox/WebKit
FROM python:3.14-slim-bookworm@sha256:5404df00cf00e6e7273375f415651837b4d192ac6859c44d3b740888ac798c99

# Install uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:88234bc9e09c2b2f6d176a3daf411419eb0370d450a08129257410de9cfafd2a /uv /uvx /bin/

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
