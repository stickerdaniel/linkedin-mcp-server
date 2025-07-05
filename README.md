# LinkedIn MCP Server

A Model Context Protocol (MCP) server that enables interaction with LinkedIn through Claude and other AI assistants. This server allows you to scrape LinkedIn profiles, companies, jobs, and perform job searches.

## Installation Methods

[![Docker](https://img.shields.io/badge/Docker-Universal_MCP-008fe2?style=for-the-badge&logo=docker&logoColor=008fe2)](#-docker-setup-recommended---universal)
[![Install DXT Extension](https://img.shields.io/badge/Claude_Desktop_Extension-d97757?style=for-the-badge&logo=anthropic)](#-claude-desktop-dxt-extension)
[![Development](https://img.shields.io/badge/Development-Local_Setup-ffd343?style=for-the-badge&logo=python&logoColor=ffd343)](#-local-setup-develop--contribute)

https://github.com/user-attachments/assets/eb84419a-6eaf-47bd-ac52-37bc59c83680

## Usage Examples

```
Research the background of this candidate https://www.linkedin.com/in/stickerdaniel/
```
```
Get this company profile for partnership discussions https://www.linkedin.com/company/inframs/
```
```
Suggest improvements for my CV to target this job posting https://www.linkedin.com/jobs/view/4252026496
```

## Features & Tool Status

**Working Tools:**
> [!TIP]
> - **Profile Scraping** (`get_person_profile`): Get detailed information from LinkedIn profiles including work history, education, skills, and connections
> - **Company Analysis** (`get_company_profile`): Extract company information with comprehensive details
> - **Job Details** (`get_job_details`): Retrieve specific job posting details using direct LinkedIn job URLs
> - **Session Management** (`close_session`): Properly close browser session and clean up resources

**Known Issues: (should be fixed after this [PR](https://github.com/joeyism/linkedin_scraper/pull/252) is merged)**
> [!WARNING]
> - **Job Search** (`search_jobs`): Compatibility issues with LinkedIn's search interface
> - **Recommended Jobs** (`get_recommended_jobs`): Selenium method compatibility issues
> - **Company Profiles** (`get_company_profile`): Some companies can't be accessed / may return empty results (need further investigation)

## 🛡️ Error Handling & Non-Interactive Mode

**NEW**: Enhanced error handling for Docker and CI/CD environments!

The server now provides detailed error information when login fails:
- **Specific error types**: `credentials_not_found`, `invalid_credentials`, `captcha_required`, `two_factor_auth_required`, `rate_limit`
- **Non-interactive mode**: Use `--no-setup` to skip all prompts (perfect for Docker)
- **Structured responses**: Each error includes type, message, and resolution steps

For detailed error handling documentation, see [ERROR_HANDLING.md](ERROR_HANDLING.md)

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
        "stickerdaniel/linkedin-mcp-server",
        "--no-setup"
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
<summary><b>🔧 Configuration</b></summary>

**Transport Modes:**
- **Default (stdio)**: Standard communication for local MCP servers
- **Streamable HTTP**: For a web-based MCP server

**CLI Options:**
- `--no-setup` - Skip interactive prompts (required for Docker/non-interactive environments)
- `--debug` - Enable detailed logging
- `--no-lazy-init` - Login to LinkedIn immediately instead of waiting for the first tool call
- `--transport {stdio,streamable-http}` - Set transport mode
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)

**HTTP Mode Example (for web-based MCP clients):**
```bash
docker run -i --rm \
  -e LINKEDIN_EMAIL="your.email@example.com" \
  -e LINKEDIN_PASSWORD="your_password" \
  -p 8080:8080 \
  stickerdaniel/linkedin-mcp-server \
  --no-setup --transport streamable-http --host 0.0.0.0 --port 8080 --path /mcp
```
**Test with mcp inspector:**
1. Install and run mcp inspector ```bunx @modelcontextprotocol/inspector```
2. Click pre-filled token url to open the inspector in your browser
3. Select `Streamable HTTP` as `Transport Type`
4. Set `URL` to `http://localhost:8080/mcp`
5. Connect
6. Test tools

</details>

<details>
<summary><b>❗ Troubleshooting</b></summary>

**Docker issues:**
- Make sure [Docker](https://www.docker.com/get-started/) is installed
- Check if Docker is running: `docker ps`

**Login issues:**
- Ensure your LinkedIn credentials are set and correct
- LinkedIn may require a login confirmation in the LinkedIn mobile app
- You might get a captcha challenge if you logged in a lot of times in a short period of time, then try again later or follow the [local setup instructions](#-local-setup-develop--contribute) to run the server manually in --no-headless mode where you can debug the login process (solve captcha manually)
</details>

## 📦 Claude Desktop (DXT Extension)

**Prerequisites:** [Claude Desktop](https://claude.ai/download) and [Docker](https://www.docker.com/get-started/) installed

**One-click installation** for Claude Desktop users:
1. Download the [DXT extension](https://github.com/stickerdaniel/linkedin-mcp-server/releases/latest/download/linkedin-mcp-server.dxt)
2. Double-click to install into Claude Desktop
3. Configure your LinkedIn credentials when prompted
4. Start using LinkedIn tools immediately

<details>
<summary><b>❗ Troubleshooting</b></summary>

**Docker issues:**
- Make sure [Docker](https://www.docker.com/get-started/) is installed
- Check if Docker is running: `docker ps`

**Login issues:**
- Ensure your LinkedIn credentials are set and correct
- LinkedIn may require a login confirmation in the LinkedIn mobile app
- You might get a captcha challenge if you logged in a lot of times in a short period of time, then try again later or follow the [local setup instructions](#-local-setup-develop--contribute) to run the server manually in --no-headless mode where you can debug the login process (solve captcha manually)
</details>

## 🐍 Local Setup (Develop & Contribute)

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
- `--no-setup` - Skip credential prompts (make sure to set `LINKEDIN_EMAIL` and `LINKEDIN_PASSWORD` in env or or run the server once manualy, then it will be stored in your OS keychain and you can run the server without credentials)
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
<summary><b>❗ Troubleshooting</b></summary>

**Login/Scraping issues:**
- Use `--no-headless` to see browser actions (captcha challenge, LinkedIn mobile app 2fa, ...)
- Add `--no-lazy-init` to attempt to login to LinkedIn immediately instead of waiting for the first tool call
- Add `--debug` to see more detailed logging

**ChromeDriver issues:**
- Ensure Chrome and ChromeDriver versions match
- Check ChromeDriver is in PATH or set `CHROMEDRIVER_PATH` in your env

**Python issues:**
- Check Python version: `uv python --version` (should be 3.12+)
- Reinstall dependencies: `uv sync --reinstall`

</details>

Feel free to open an [issue](https://github.com/stickerdaniel/linkedin-mcp-server/issues) or [PR](https://github.com/stickerdaniel/linkedin-mcp-server/pulls)!

---

## License

MIT License

## Acknowledgements
Built with [LinkedIn Scraper](https://github.com/joeyism/linkedin_scraper) by [@joeyism](https://github.com/joeyism) and [Model Context Protocol](https://modelcontextprotocol.io/).

⚠️ Use in accordance with [LinkedIn's Terms of Service](https://www.linkedin.com/legal/user-agreement). Web scraping may violate LinkedIn's terms. This tool is for personal use only.
