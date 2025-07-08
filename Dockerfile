FROM python:3.12-alpine

# Install system dependencies including Chromium and ChromeDriver
RUN apk add --no-cache \
    git \
    curl \
    chromium \
    chromium-chromedriver

# Install uv from official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

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
ENTRYPOINT ["uv", "run", "main.py"]
CMD []
