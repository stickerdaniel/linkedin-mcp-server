# LinkedIn MCP Server

A Model Context Protocol (MCP) server that enables interaction with LinkedIn through Claude and other AI assistants. This server allows you to scrape LinkedIn profiles, companies, jobs, and perform job searches.


https://github.com/user-attachments/assets/eb84419a-6eaf-47bd-ac52-37bc59c83680


## Features & Tool Status

### Working Tools
- **Profile Scraping** (`get_person_profile`): Get detailed information from LinkedIn profiles including work history, education, skills, and connections
- **Company Analysis** (`get_company_profile`): Extract company information with comprehensive details
- **Job Details** (`get_job_details`): Retrieve specific job posting details using direct LinkedIn job URLs
- **Session Management** (`close_session`): Properly close browser sessions and clean up resources

### Tools with Known Issues
- **Job Search** (`search_jobs`): Currently experiencing ChromeDriver compatibility issues with LinkedIn's search interface
- **Recommended Jobs** (`get_recommended_jobs`): Has Selenium method compatibility issues due to outdated scraping methods
- **Company Profiles**: Some companies may have restricted access or may return empty results (need further investigation)

## 🎯 Usage Examples

```
Get Daniel's profile https://www.linkedin.com/in/stickerdaniel/
```
```
Analyze this company https://www.linkedin.com/company/docker/
```
```
Get details about this job posting https://www.linkedin.com/jobs/view/123456789
```

## Installation Methods

Choose your preferred installation method:

[![Docker Hub](https://img.shields.io/badge/Docker_Hub-Universal_MCP_Server-2496ED?style=for-the-badge&logo=docker)](https://hub.docker.com/r/stickerdaniel/linkedin-mcp-server)
[![Install DXT Extension](https://img.shields.io/badge/Claude_Desktop_Extension-purple?style=for-the-badge&logo=anthropic)](./linkedin-mcp-server-1.0.0.dxt)
[![Development](https://img.shields.io/badge/Development-Local_Setup-green?style=for-the-badge&logo=github)](#%EF%B8%8F-local-setup-develop--contribute)

---

## 🐳 Docker Setup (Recommended - Universal)

**Prerequisites:** Make sure you have [Docker](https://www.docker.com/get-started/) installed and running.

**Zero setup required** - just add the mcp server to your client config and replace email and password with your linkedin credentials.

### Installation

**Claude Desktop:**
```json
{
  "mcpServers": {
    "linkedin": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "LINKEDIN_EMAIL",
        "-e", "LINKEDIN_PASSWORD",
        "stickerdaniel/linkedin-mcp-server"
      ],
      "env": {
        "LINKEDIN_EMAIL": "your.email@example.com",
        "LINKEDIN_PASSWORD": "your_password"
      }
    }
  }
}
```

<details>
<summary><b>🐳 Manual Docker Usage</b></summary>

```bash
docker run -i --rm \
  -e LINKEDIN_EMAIL="your.email@example.com" \
  -e LINKEDIN_PASSWORD="your_password" \
  stickerdaniel/linkedin-mcp-server
```

</details>

<details>
<summary><b>🚨 Troubleshooting</b></summary>

**Container won't start:**
```bash
# Check Docker is running
docker ps

# Pull latest image
docker pull stickerdaniel/linkedin-mcp-server
```

**Login issues:**
- Verify credentials are correct
- Check for typos in email/password
- Check if you need to confirm the login in the mobile app

</details>

---

## 📦 Claude Desktop (DXT Extension)

**Prerequisites:** [Claude Desktop](https://claude.ai/desktop) installed

**One-click installation** for Claude Desktop users:
1. Download the [DXT extension](./linkedin-mcp-server-1.0.0.dxt)
2. Double-click to install into Claude Desktop
3. Configure your LinkedIn credentials when prompted
4. Start using LinkedIn tools immediately

The extension automatically handles Docker setup and credential management.

---

## 🛠️ Local Setup (Develop & Contribute)

**For contributors** who want to modify and debug the code.

**Prerequisites:** [Chrome browser](https://www.google.com/chrome/) and [Git](https://git-scm.com/downloads) installed

**ChromeDriver Setup:**
1. **Check Chrome version**: Chrome → menu (⋮) → Help → About Google Chrome
2. **Download matching ChromeDriver**: [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/)
3. **Make it accessible**:
   - Place ChromeDriver in PATH (`/usr/local/bin` on macOS/Linux)
   - Or set: `export CHROMEDRIVER_PATH=/path/to/chromedriver`
   - if no CHROMEDRIVER_PATH is set, the server will try to find it automatically by checking common locations

### Installation

```bash
# 1. Clone repository
git clone https://github.com/stickerdaniel/linkedin-mcp-server
cd linkedin-mcp-server

# 2. Install UV package manager
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python # install python if you don't have it

# 3. Install dependencies and dev dependencies
uv sync
uv sync --group dev

# 4. Install pre-commit hooks
uv run pre-commit install

# 5. Start the server once manually
# (you will be prompted to enter your LinkedIn credentials, and they are securely stored in your OS keychain)
uv run main.py --no-headless --no-lazy-init
```

<details>
<summary><b>🔧 Configuration</b></summary>

**CLI Options:**
- `--no-headless` - Show browser window (debugging)
- `--debug` - Enable detailed logging
- `--no-setup` - Skip credential prompts (make sure to set `LINKEDIN_EMAIL` and `LINKEDIN_PASSWORD` in env)
- `--no-lazy-init` - Login to LinkedIn immediately instead of waiting for the first tool call

**Claude Desktop:**
```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uv",
      "args": ["--directory", "/path/to/linkedin-mcp-server", "run", "main.py", "--no-setup"]
    }
  }
}
```

</details>

<details>
<summary><b>🚨 Troubleshooting</b></summary>

**Scraping issues:**
- Use `--no-headless` to see browser actions
- Add `--debug` to see more detailed logging

**ChromeDriver issues:**
- Ensure Chrome and ChromeDriver versions match
- Check ChromeDriver is in PATH or set `CHROMEDRIVER_PATH`

**Python issues:**
```bash
# Check Python version
python --version  # Should be 3.12+

# Reinstall dependencies
uv sync --reinstall
```

</details>

Feel free to open an [issue](https://github.com/stickerdaniel/linkedin-mcp-server/issues) or [PR](https://github.com/stickerdaniel/linkedin-mcp-server/pulls)!

## License

MIT License

## Acknowledgements
Built with [LinkedIn Scraper](https://github.com/joeyism/linkedin_scraper) by [@joeyism](https://github.com/joeyism) and [Model Context Protocol](https://modelcontextprotocol.io/).

⚠️ Use responsibly and in accordance with [LinkedIn's Terms of Service](https://www.linkedin.com/legal/user-agreement). Web scraping may violate LinkedIn's terms. This tool is for personal use only.
