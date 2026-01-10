FROM python:3.13-alpine@sha256:e7e041128ffc3e3600509f508e44d34ab08ff432bdb62ec508d01dfc5ca459f7

# Install system dependencies including Chromium and ChromeDriver
RUN apk add --no-cache \
    git \
    curl \
    chromium \
    chromium-chromedriver

# Install uv from official image
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:816fdce3387ed2142e37d2e56e1b1b97ccc1ea87731ba199dc8a25c04e4997c5 /uv /uvx /bin/

# Set working directory
WORKDIR /app

# Copy project files
COPY . /app

# Sync dependencies and install project
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

# Create a non-root user
RUN adduser -D -u 1000 mcpuser && chown -R mcpuser:mcpuser /app
USER mcpuser

# Set entrypoint and default arguments
ENTRYPOINT ["uv", "run", "-m", "linkedin_mcp_server"]
CMD []
