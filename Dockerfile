FROM mcr.microsoft.com/playwright/python:v1.57.0-noble@sha256:3de745b23fc4b33fccbcb3f592ee52dd5c80ce79f19f839c825ce23364e403c1

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b /uv /uvx /bin/

# Set working directory and fix ownership
WORKDIR /app
RUN chown pwuser:pwuser /app

# Copy project files and set ownership
COPY --chown=pwuser:pwuser . /app

# Switch to non-root user
USER pwuser

# Sync dependencies and install project
RUN uv sync --frozen

# Set entrypoint and default arguments
ENTRYPOINT ["uv", "run", "-m", "linkedin_mcp_server"]
CMD []
