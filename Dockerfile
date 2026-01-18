# Use slim Python base instead of full Playwright image (saves ~300-400 MB)
# Only Chromium is installed, not Firefox/WebKit
FROM python:3.12-slim-bookworm

# Install uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Create non-root user first (matching original pwuser from Playwright image)
RUN useradd -m -s /bin/bash pwuser

# Set working directory and ownership
WORKDIR /app
RUN chown pwuser:pwuser /app

# Copy project files with correct ownership
COPY --chown=pwuser:pwuser . /app

# Set paths for Playwright browsers and uv Python installs to shared locations
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
ENV UV_PYTHON_INSTALL_DIR=/opt/python

# Install dependencies and Playwright with ONLY Chromium (not Firefox/WebKit)
# --with-deps installs required system dependencies (fonts, libraries) via apt (needs root)
RUN uv sync --frozen && \
    uv run playwright install --with-deps chromium && \
    chmod -R 755 /opt/playwright /opt/python

# Fix ownership of app directory (venv created by uv)
RUN chown -R pwuser:pwuser /app

# Switch to non-root user
USER pwuser

# Set entrypoint and default arguments
ENTRYPOINT ["uv", "run", "-m", "linkedin_mcp_server"]
CMD []
