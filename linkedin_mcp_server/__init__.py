# src/linkedin_mcp_server/__init__.py
"""
LinkedIn MCP Server package.

A Model Context Protocol (MCP) server that provides LinkedIn integration capabilities
for AI assistants. This package enables secure LinkedIn profile, company, and job
data scraping through a standardized MCP interface.

Key Features:
- Secure LinkedIn authentication via session cookies
- LinkedIn profile, company, and job data scraping
- MCP-compliant server implementation using FastMCP
- Chrome WebDriver automation with session persistence
- Layered configuration system with secure credential storage
- Docker containerization for easy deployment
- Claude Desktop DXT extension support

Architecture:
- Clean separation between authentication, driver management, and MCP server
- Singleton pattern for WebDriver session management
- Comprehensive error handling and logging
- Cross-platform compatibility (macOS, Windows, Linux)
"""

__version__ = "1.0.0"
