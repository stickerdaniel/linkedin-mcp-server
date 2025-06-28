---
name: Bug Report
about: Create a report to help us improve the LinkedIn MCP server
title: '[BUG] '
labels: ['bug']
assignees: ''

---

## Bug Description
**Describe the bug**
A clear and concise description of what the bug is.

**Expected behavior**
A clear and concise description of what you expected to happen.

**Actual behavior**
What actually happened instead.

## MCP Configuration & Client Info
**MCP Client Used**
- [ ] Claude Desktop
- [ ] Other MCP client (specify): ___________

**Claude Desktop Configuration**
Please share your MCP configuration from Claude Desktop settings (remove sensitive info):
```json
{
  "mcpServers": {
    "linkedin-scraper": {
      // Your configuration here
    }
  }
}
```

**Transport Mode**
- [ ] stdio
- [ ] sse

## Environment Details
**Operating System**
- [ ] macOS
- [ ] Windows
- [ ] Linux

**Python Version**
- Python version: ___________

**Package Manager used**
- [ ] UV (recommended)
- [ ] pip
- [ ] Other: ___________

**ChromeDriver Info**
- ChromeDriver location: ___________
- Installation method:
  - [ ] Auto-detected
  - [ ] Manual path specified
  - [ ] Environment variable

## Tool & LinkedIn Context
**Tool Used**
- [ ] get_person_profile
- [ ] get_company_profile
- [ ] get_job_details
- [ ] search_jobs
- [ ] get_recommended_jobs
- [ ] close_session

**LinkedIn Context** (if applicable)
- Account type: [ ] Free [ ] Premium [ ] Sales Navigator
- Two-factor authentication enabled: [ ] Yes [ ] No
- Corporate/VPN network: [ ] Yes [ ] No

## Error Details
**Error Messages**
```
Paste any error messages here
```

**Console Output/Logs**
```
Paste relevant console output or logs here
```

## Steps to Reproduce
1. Go to '...'
2. Send message '....'
3. Scroll down to '....'
4. See error

## Screenshots/Videos
If applicable, add screenshots or videos to help explain your problem.

## Additional Context
- Issue also occurs in `--no-headless` mode: [ ] Yes [ ] No

Add any other context about the problem here.
