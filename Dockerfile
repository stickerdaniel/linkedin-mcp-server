FROM mcr.microsoft.com/playwright/python:v1.57.0-noble

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory and fix ownership
WORKDIR /app
RUN chown pwuser:pwuser /app

# Copy project files and set ownership
COPY --chown=pwuser:pwuser . /app

# Switch to non-root user
USER pwuser

# Sync dependencies and install project (with cache for faster rebuilds)
RUN --mount=type=cache,target=/home/pwuser/.cache/uv,uid=1000,gid=1000 \
    uv sync --frozen

# Set entrypoint and default arguments
ENTRYPOINT ["uv", "run", "-m", "linkedin_mcp_server"]
CMD []
