# src/linkedin_mcp_server/tools/__init__.py
"""
LinkedIn scraping tools package.

This package contains the MCP tool implementations for LinkedIn data extraction.
Each tool module provides specific functionality for different LinkedIn entities
while sharing common error handling and driver management patterns.

Available Tools:
- Person tools: LinkedIn profile scraping and analysis
- Company tools: Company profile and information extraction
- Job tools: Job posting details and search functionality
- Messaging tools: Inbox, conversations, search, and sending messages

Architecture:
- FastMCP integration for MCP-compliant tool registration
- Depends()-based dependency injection for browser/extractor setup
- ToolError-based error handling through centralized raise_tool_error()
- Singleton driver pattern for session persistence
- Structured data return format for consistent MCP responses
"""
