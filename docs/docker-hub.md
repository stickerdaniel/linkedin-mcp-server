# LinkedIn MCP Server

A Model Context Protocol (MCP) server that connects AI assistants to LinkedIn. Access profiles, companies, and job postings through a Docker container.

## Features
- **Profile Access**: Get detailed LinkedIn profile information
- **Company Profiles**: Extract comprehensive company data
- **Job Details**: Retrieve job posting information
- **Job Search**: Search for jobs with keywords and location filters

## Quick Start

### Option 1: Cookie Authentication (Simplest)

Pass your LinkedIn `li_at` cookie - session will be created and stored automatically.

> **Note:** If you encounter authentication challenges, use Option 2 instead.

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-e", "LINKEDIN_COOKIE", "stickerdaniel/linkedin-mcp-server"],
      "env": {
        "LINKEDIN_COOKIE": "your_li_at_cookie_value"
      }
    }
  }
}
```

### Option 2: Browser Login via uvx

Create a session using the [uvx setup](https://github.com/stickerdaniel/linkedin-mcp-server#-uvx-setup-recommended---universal), then mount it:

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "~/.linkedin-mcp:/home/pwuser/.linkedin-mcp",
        "stickerdaniel/linkedin-mcp-server:latest"
      ]
    }
  }
}
```

> **Note:** Docker containers don't have a display server. If you encounter authentication issues with cookie auth, use the [uvx setup](https://github.com/stickerdaniel/linkedin-mcp-server#-uvx-setup-recommended---universal) to create a session on your host machine.

## Repository
- **Source**: https://github.com/stickerdaniel/linkedin-mcp-server
- **Documentation**: Full setup and usage guide in README
- **License**: Apache 2.0
