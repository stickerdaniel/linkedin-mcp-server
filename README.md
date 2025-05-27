# LinkedIn MCP Server

[![smithery badge](https://smithery.ai/badge/@stickerdaniel/linkedin-mcp-server)](https://smithery.ai/server/@stickerdaniel/linkedin-mcp-server)

A Model Context Protocol (MCP) server that enables interaction with LinkedIn through Claude and other AI assistants. This server allows you to scrape LinkedIn profiles, companies, jobs, and perform job searches.


https://github.com/user-attachments/assets/eb84419a-6eaf-47bd-ac52-37bc59c83680


## 📋 Features & Tool Status

### ✅ **Working Tools**
- **Profile Scraping** (`get_person_profile`): Get detailed information from LinkedIn profiles including work history, education, skills, and connections
- **Company Analysis** (`get_company_profile`): Extract company information with comprehensive details (employees optional)
- **Job Details** (`get_job_details`): Retrieve specific job posting details using direct LinkedIn job URLs
- **Session Management** (`close_session`): Properly close browser sessions and clean up resources

### ⚠️ **Tools with Known Issues**
- **Job Search** (`search_jobs`): Currently experiencing ChromeDriver compatibility issues with LinkedIn's search interface
- **Recommended Jobs** (`get_recommended_jobs`): Has Selenium method compatibility issues due to outdated scraping methods
- **Company Profiles**: Some companies may have restricted access or may return empty results (need further investigation)

## 🔧 Installation

### Prerequisites

- Python 3.12 or higher
- Chrome browser installed
- ChromeDriver matching your Chrome version (we'll help you set this up)
- A LinkedIn account

### Quick Start (Recommended)

```bash
# 1. Clone the repository
git clone https://github.com/stickerdaniel/linkedin-mcp-server
cd linkedin-mcp-server

# 2. Install UV if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Install the project and all dependencies
uv sync

# 4. Run the server for initial setup (this will prompt you for credentials)
uv run main.py --no-lazy-init --no-headless
```

That's it! UV will:
- Automatically create a virtual environment
- Install all dependencies from `pyproject.toml`
- Handle the [LinkedIn scraper](https://github.com/joeyism/linkedin_scraper) git dependency

#### For Development

If you want to contribute or modify the code:

```bash
# Install with development dependencies
uv sync --group dev

# Install pre-commit hooks
uv run pre-commit install
```

### ChromeDriver Setup

ChromeDriver is required for Selenium to interact with Chrome. You need to install the version that matches your Chrome browser.

1. **Check your Chrome version**:
   - Open Chrome and go to the menu (three dots) > Help > About Google Chrome
   - Note the version number (e.g., 123.0.6312.87)

2. **Download matching ChromeDriver**:
   - Go to [ChromeDriver Downloads](https://chromedriver.chromium.org/downloads) / [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) (Chrome-Version 115+)
   - Download the version that matches your Chrome version
   - Extract the downloaded file

3. **Make ChromeDriver accessible**:
   - **Option 1**: Place it in a directory that's in your PATH (e.g., `/usr/local/bin` on macOS/Linux)
   - **Option 2**: Set the CHROMEDRIVER environment variable to the path where you placed it:
     ```bash
     export CHROMEDRIVER=/path/to/chromedriver  # macOS/Linux
     # OR
     set CHROMEDRIVER=C:\path\to\chromedriver.exe  # Windows
     ```
   - **Option 3**: The server will attempt to auto-detect or prompt you for the path when run

## 🚀 Running the Server

### Quick Start

After installation, simply run:

```bash
# Start the server (first time setup)
uv run main.py --no-lazy-init --no-headless
```

### Running Options

```bash
# Normal operation (lazy initialization)
uv run main.py

# Debug mode with visible browser
uv run main.py --no-headless --debug

# Skip setup prompts (for automation)
uv run main.py --no-setup
```

### Configuration for Claude Desktop

1. **The server will automatically**:
   - Display the configuration needed for Claude Desktop
   - Copy it to your clipboard for easy pasting

2. **Add to Claude Desktop**:
   - Open Claude Desktop and go to Settings > Developer > Edit Config
   - Paste the configuration provided by the server

   Example Claude Desktop configuration:
   ```json
   {
     "mcpServers": {
       "linkedin-scraper": {
         "command": "uv",
         "args": ["--directory", "/path/to/linkedin-mcp-server", "run", "main.py", "--no-setup"],
         "env": {
           "LINKEDIN_EMAIL": "your.email@example.com",
           "LINKEDIN_PASSWORD": "your_password"
         }
       }
     }
   }
   ```

### Credential Management

- **Lazy initialization (default behavior)**:
  - The server uses lazy initialization, meaning it will only create the Chrome driver and log in when a tool is actually used
  - You can set environment variables for non-interactive use:
    ```bash
    export LINKEDIN_EMAIL=your.email@example.com
    export LINKEDIN_PASSWORD=your_password
    ```
  - Alternatively, you can run the server once manually and you'll be prompted for credentials, which will then be stored securely in your system's keychain (macOS Keychain, Windows Credential Locker, etc.)

## ⚙️ Configuration System

### Configuration Hierarchy

Configuration values are loaded with the following precedence (highest to lowest):

1. **Command-line arguments**:
   ```bash
   uv run main.py --no-headless --debug
   ```

2. **Environment variables**:
   ```bash
   export LINKEDIN_EMAIL=your.email@example.com
   export LINKEDIN_PASSWORD=your_password
   export CHROMEDRIVER=/path/to/chromedriver
   ```
   *Note: Environment variables always override credentials stored in the system keychain*

3. **System keychain**: Securely stored credentials from previous sessions

4. **Default values**: Built-in fallback values

### Command-line Options

| Option | Description |
|--------|-------------|
| `--no-headless` | Run Chrome with a visible browser window |
| `--debug` | Enable debug mode with additional logging |
| `--no-setup` | Skip configuration setup prompts |
| `--no-lazy-init` | Initialize Chrome driver immediately (instead of on first use) |

### Credential Storage

Your LinkedIn credentials are stored securely using your system's native keychain/credential manager:

- **macOS**: macOS Keychain
- **Windows**: Windows Credential Locker
- **Linux**: Native keyring (varies by distribution)

Credentials are managed as follows:

1. First, the application checks for credentials in environment variables
2. Next, it checks the system keychain for stored credentials
3. If no credentials are found, you'll be prompted to enter them (in interactive mode)
4. Entered credentials are securely stored in your system keychain for future use

### Clearing Stored Credentials

If you need to change your stored credentials, run the application with the `--no-lazy-init` flag and when prompted about login failure, select "Yes" to try with different credentials.

### ChromeDriver Configuration

The ChromeDriver path is found in this order:
1. From the `CHROMEDRIVER` environment variable
2. Auto-detected from common locations
3. Manually specified when prompted (if auto-detection fails)

Once specified, the ChromeDriver path is used for the current session but not stored persistently.

## 🔄 Using with Claude Desktop

1. **After adding the configuration** to Claude Desktop, restart the application
2. **Start a conversation** with Claude
3. **You'll see tools available** in the tools menu (settings icon)
4. **You can now ask Claude** to retrieve LinkedIn profiles, companies, and job details

### ✅ **Recommended Usage Examples**
- "Can you tell me about Daniel's work experience? His LinkedIn profile is https://www.linkedin.com/in/stickerdaniel/"
- "Get details about this job posting: https://www.linkedin.com/jobs/view/1234567890"
- "Tell me about Electric Mind as a company based on their LinkedIn page"
- "Get the profile information for this LinkedIn user: [profile URL]"

### ⚠️ **Usage Examples with Known Issues**
- ~~"Search for machine learning engineer jobs on LinkedIn"~~ (currently not working due to browser automation issues)
- ~~"Show me recommended jobs from LinkedIn"~~ (has compatibility issues)

## 🔐 Security and Privacy

- Your LinkedIn credentials are securely stored in your system's native keychain/credential manager with user-only permissions
- Credentials are never exposed to Claude or any other AI and are only used for the LinkedIn login to scrape data
- The server runs on your local machine, not in the cloud
- All LinkedIn scraping happens through your account - be aware that profile visits are visible to other users

## ⚠️ Troubleshooting

### Tool-Specific Issues

**Job Search (`search_jobs`) Not Working:**
- This tool currently has ChromeDriver compatibility issues
- Use direct job URLs with `get_job_details` instead
- LinkedIn's search interface has anti-automation measures

**Recommended Jobs (`get_recommended_jobs`) Errors:**
- Contains outdated Selenium methods (`find_elements_by_class_name`)
- LinkedIn has updated their DOM structure
- Use manual job discovery and direct URLs as workaround

**Empty Job Descriptions:**
- Job content may be dynamically loaded
- Some job postings have protected content
- Try accessing the job URL directly in a browser first

**Company Profile Access Issues:**
- Some companies restrict automated access
- Corporate LinkedIn pages may have different structures
- Try with well-known company URLs first

### ChromeDriver Issues

If you encounter ChromeDriver errors:
1. Ensure your Chrome browser is updated
2. Download the matching ChromeDriver version
3. Set the CHROMEDRIVER path correctly
4. Try running with administrator/sudo privileges if permission issues occur

### Authentication Issues

If login fails:
1. Verify your LinkedIn credentials
2. Check if your account has two-factor authentication enabled
3. Try logging in manually to LinkedIn first, then run the server
4. Check your LinkedIn mobile app for a login request after running the server
5. Try to run the server with `--no-headless` to see where the login fails
6. Try to run the server with `--debug` to see more detailed logs

### Connection Issues

If Claude cannot connect to the server:
1. Ensure the server is running when you start it manually
2. Verify the configuration in Claude Desktop is correct
3. Restart Claude Desktop

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgements

- Based on the [LinkedIn Scraper](https://github.com/joeyism/linkedin_scraper) by joeyism
- Uses the Model Context Protocol (MCP) for integration with AI assistants

---

**Note**: This tool is for personal use only. Use responsibly and in accordance with LinkedIn's terms of service. Web scraping may violate LinkedIn's terms of service.
