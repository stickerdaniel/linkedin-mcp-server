# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Environment Setup
```bash
# Install UV package manager first
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync
uv sync --group dev

# Install pre-commit hooks
uv run pre-commit install
```

### Development Workflow
```bash
# Start server in development mode (visible browser, immediate login)
uv run main.py --no-headless --no-lazy-init

# Start server command for MCP client configurations
uv run main.py --no-setup

# For debugging, show browser and login immediately
uv run main.py --no-headless --no-lazy-init --debug

# Run linting
uv run ruff check .
uv run ruff check --fix .

# Run formatting
uv run ruff format .

# Check dependencies
uv sync --reinstall
```

### Docker Development
```bash
# Build local Docker image
docker build -t linkedin-mcp-server .

# Run with environment variables
docker run -i --rm \
  -e LINKEDIN_EMAIL="your-email" \
  -e LINKEDIN_PASSWORD="your-password" \
  linkedin-mcp-server
```

## Publishing & Release Commands

### Docker Hub Publishing
```bash
# Build and tag for Docker Hub
docker build -t stickerdaniel/linkedin-mcp-server:latest .
docker build -t stickerdaniel/linkedin-mcp-server:v1.0.0 .

# Push to Docker Hub
docker push stickerdaniel/linkedin-mcp-server:latest
docker push stickerdaniel/linkedin-mcp-server:v1.0.0
```

### DXT Package Creation
```bash
# Package DXT extension (Desktop Extension for Claude Desktop installation)
bunx @anthropic-ai/dxt pack
```
# This creates linkedin-mcp-server.dxt file based on manifest.json. Specifications:
- https://github.com/anthropics/dxt/blob/main/README.md - DXT architecture overview, capabilities, and integration patterns
- https://github.com/anthropics/dxt/blob/main/MANIFEST.md - Complete extension manifest structure and field definitions
- https://github.com/anthropics/dxt/tree/main/examples - Reference implementations including a "Hello World" example


### GitHub Release
```bash
# Create GitHub release with DXT file
gh release create v1.0.0 linkedin-mcp-server.dxt \
  --title "📦 v1.0.0 - Claude Desktop DXT Extension" \
  --notes "Initial DXT extension release for Claude Desktop users.

## Claude Desktop DXT Extension
This release contains the `.dxt` extension file for Claude Desktop installation.

**Installation:**
1. Download the \`linkedin-mcp-server.dxt\` file
2. Double-click to open in Claude Desktop
3. Configure with your LinkedIn credentials

**Prerequisites:**
- Claude Desktop application
- Docker installed and running

For other MCP clients, refer to the [Docker setup guide](https://github.com/stickerdaniel/linkedin-mcp-server#-docker-setup-recommended---universal)."

# List releases
gh release list

# View specific release
gh release view v1.0.0
```

## Architecture Overview

This is a Model Context Protocol (MCP) server for LinkedIn integration with the following key architecture:

### Core Components
- **Entry Point**: `main.py` - Handles initialization, CLI args, and transport setup
- **MCP Server**: `linkedin_mcp_server/server.py` - FastMCP-based server implementation with tool registration
- **Driver Management**: `linkedin_mcp_server/drivers/chrome.py` - Selenium WebDriver session management with LinkedIn authentication
- **Configuration System**: `linkedin_mcp_server/config/` - Layered configuration with CLI args → env vars → defaults

### Tool Implementation
- **Person Tools**: `linkedin_mcp_server/tools/person.py` - Profile scraping (`get_person_profile`)
- **Company Tools**: `linkedin_mcp_server/tools/company.py` - Company analysis (`get_company_profile`)
- **Job Tools**: `linkedin_mcp_server/tools/job.py` - Job details and search (`get_job_details`, `search_jobs`, `get_recommended_jobs`)

### Configuration Layers (Priority Order)
1. Command line arguments (highest)
2. Environment variables (`LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD`, `CHROMEDRIVER_PATH`)
3. System keyring (secure credential storage)
4. Interactive prompts (development)
5. Auto-detection (ChromeDriver path)

### Key Design Patterns
- **Singleton Driver**: Global WebDriver instance reused across all tools for session persistence
- **Lazy Initialization**: Driver and login only created when first tool is called (unless `--no-lazy-init`)
- **Secure Credentials**: System keyring integration with fallback to environment variables
- **Resource Cleanup**: Automatic browser session cleanup on shutdown

### Distribution Methods
- **Docker Container**: Production deployment with pre-configured Chrome/ChromeDriver
- **Claude Desktop DXT**: One-click extension installation via `manifest.json`
- **Local Development**: UV-based Python environment with manual ChromeDriver setup

## Important Development Notes

### Credential Handling
- Credentials are NEVER logged or exposed in error messages
- Use system keyring for persistent storage in development
- Environment variables for production/CI
- Interactive prompts only in development mode

### Browser Automation
- ChromeDriver must match Chrome version exactly
- Auto-detection checks common paths: `/usr/local/bin/chromedriver`, `/usr/bin/chromedriver`, etc.
- Use `--no-headless` for debugging browser automation issues
- LinkedIn login happens automatically with retry logic and 2FA support

### Tool Development
- All tools follow FastMCP registration pattern in `server.py`
- Tools reuse the global driver instance for session consistency
- Return structured data, not raw HTML
- Handle LinkedIn rate limiting and session expiry gracefully

### Known Issues
- `search_jobs` and `get_recommended_jobs` have compatibility issues with LinkedIn's current interface
- Some company profiles may be restricted and return empty results
- ChromeDriver version mismatches cause common setup issues

### Code Quality Standards
- Use UV package manager (not pip/conda)
- Follow commit message format: `type(scope): subject` (see `.cursor/rules/commit-message-instructions.mdc`)
- Run `ruff check --fix .` before committing
- Keep Python 3.12+ compatibility
- All new dependencies must be added to `pyproject.toml` via `uv add`

### Testing LinkedIn Integration
- Test with personal LinkedIn account first
- Use `--no-headless --debug` to watch browser automation
- LinkedIn may require mobile app confirmation for new login locations
- Session persists across tool calls for performance

### CLI Tool Integration
Use `linkedin_mcp_server/cli.py` to generate Claude Desktop configuration automatically and copy to clipboard for easy setup.
