# Use slim Python base instead of full Playwright image (saves ~300-400 MB)
# Only Chromium is installed, not Firefox/WebKit
FROM python:3.14-slim-bookworm@sha256:adb6bdfbcc7c744c3b1a05976136555e2d82b7df01ac3efe71737d7f95ef0f2d

# Install uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:59240a65d6b57e6c507429b45f01b8f2c7c0bbeee0fb697c41a39c6a8e3a4cfb /uv /uvx /bin/

# Create non-root user first (matching original pwuser from Playwright image)
RUN useradd -m -s /bin/bash pwuser

# Set working directory and ownership
WORKDIR /app
RUN chown pwuser:pwuser /app

# Copy project files with correct ownership
COPY --chown=pwuser:pwuser . /app

# Set Playwright browser install location
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
# Install dependencies and Playwright with ONLY Chromium (not Firefox/WebKit)
# --with-deps installs required system dependencies (fonts, libraries) via apt (needs root)
RUN uv sync --frozen && \
    uv run playwright install --with-deps chromium && \
    chmod -R 755 /opt/playwright

# Fix ownership of app directory (venv created by uv)
RUN chown -R pwuser:pwuser /app

# Switch to non-root user
USER pwuser

# Set entrypoint and default arguments
ENTRYPOINT ["uv", "run", "-m", "linkedin_mcp_server"]
CMD []
