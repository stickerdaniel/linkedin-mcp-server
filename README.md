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

The server automatically handles login, navigation, and data extraction.

## Installation Methods

Choose your preferred installation method:

[![Install with Claude Desktop](https://img.shields.io/badge/Claude_Desktop-One_Click_Install-blue?style=for-the-badge&logo=anthropic)](https://claude.ai/install-mcp?name=linkedin&config=eyJjb21tYW5kIjoiZG9ja2VyIiwiYXJncyI6WyJydW4iLCItaSIsIi0tcm0iLCItZSIsIkxJTktFRElOX0VNQUlMIiwiLWUiLCJMSU5LRURJTl9QQVNTV09SRCIsIm1jcC9saW5rZWRpbiJdfQ%3D%3D)
[![Docker Hub](https://img.shields.io/badge/Docker_Hub-stickerdaniel/linkedin--mcp--server-2496ED?style=for-the-badge&logo=docker)](https://hub.docker.com/r/stickerdaniel/linkedin-mcp-server)
[![Development](https://img.shields.io/badge/Contributors-Local_Setup-green?style=for-the-badge&logo=github)](#%EF%B8%8F-local-setup-develop--contribute)

---

## 🐳 Docker Setup (Recommended)

**Zero setup required** - no Chrome installation, no ChromeDriver management, no dependencies.

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

## 🛠️ Local Setup (Develop & Contribute)

**For contributors** who want to modify and debug the code.

**Prerequisites:**
- Python 3.12 or higher
- Chrome browser installed
- ChromeDriver (see setup below)

**ChromeDriver Setup:**
1. **Check Chrome version**: Chrome → menu (⋮) → Help → About Google Chrome
2. **Download matching ChromeDriver**: [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/)
3. **Make accessible**:
   - Place in PATH (`/usr/local/bin` on macOS/Linux)
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

⚠️ **Important:** Use responsibly and in accordance with [LinkedIn's Terms of Service](https://www.linkedin.com/legal/user-agreement). Web scraping may violate LinkedIn's terms. This tool is for personal use only.

**Acknowledgements:** Built with [LinkedIn Scraper](https://github.com/joeyism/linkedin_scraper) and [Model Context Protocol](https://modelcontextprotocol.io/).
