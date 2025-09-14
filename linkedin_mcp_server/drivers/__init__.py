# src/linkedin_mcp_server/drivers/__init__.py
"""
Driver management package for LinkedIn scraping.

This package provides Chrome WebDriver management and automation capabilities
for LinkedIn scraping. It implements a singleton pattern for driver instances
to ensure session persistence across multiple tool calls while handling
authentication, session management, and proper resource cleanup.

Key Components:
- Chrome WebDriver initialization and configuration
- LinkedIn authentication and session management
- Singleton pattern for driver reuse across tools
- Automatic driver cleanup and resource management
- Cross-platform Chrome driver detection and setup
"""
