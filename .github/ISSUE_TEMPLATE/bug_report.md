---
name: Bug Report
about: Create a report to help us improve the LinkedIn MCP server
title: '[BUG] '
labels: ['bug']
assignees: ''

---

## Installation Method
- [ ] Docker (specify docker image version/tag): _._._
- [ ] Claude Desktop DXT extension (specify docker image version/tag): _._._
- [ ] Local Python setup

## When does the error occur?
- [ ] At startup
- [ ] During tool call (specify which tool):
  - [ ] get_person_profile
  - [ ] get_company_profile
  - [ ] get_job_details
  - [ ] search_jobs
  - [ ] get_recommended_jobs
  - [ ] close_session

## MCP Client Configuration

**Claude Desktop Config** (`/Users/[username]/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "linkedin": {
      // Your configuration here (remove sensitive credentials)
    }
  }
}
```

## MCP Client Logs
**Claude Desktop Logs** (`/Users/[username]/Library/Logs/Claude/mcp-server-LinkedIn MCP Server.log`):
```
Paste relevant log entries here
```

## Error Description
What went wrong and what did you expect to happen?

