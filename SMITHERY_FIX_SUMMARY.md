# Smithery Deployment Fix Summary

## Issues Identified and Fixed

### 1. **Tool Discovery Failure**
- **Problem**: Smithery couldn't scan tools from the server, getting "TypeError: fetch failed"
- **Root Cause**: Server wasn't properly handling HTTP requests for tool discovery
- **Fix**: Created proper ASGI app using FastMCP's `http_app()` method with middleware support

### 2. **Configuration Handling**
- **Problem**: Server expected environment variables, but Smithery passes config as query parameters
- **Root Cause**: Misunderstanding of how Smithery passes configuration
- **Fix**: Implemented Starlette middleware to extract query parameters and update environment

### 3. **Middleware Implementation**
- **Problem**: Initial attempt used incorrect FastMCP middleware API
- **Root Cause**: Used `@mcp.middleware()` decorator which doesn't exist
- **Fix**: Used proper Starlette middleware passed to `http_app()` method

### 4. **Server Startup**
- **Problem**: Server needed to start without credentials for tool discovery
- **Root Cause**: Lazy initialization wasn't properly configured
- **Fix**: Ensured all environment variables are set for lazy init before imports

## Key Changes Made

### 1. **smithery_main.py**
```python
# Proper ASGI app creation with middleware
def create_app():
    mcp = create_mcp_server()
    middleware = [Middleware(SmitheryConfigMiddleware)]
    app = mcp.http_app(path="/mcp", middleware=middleware, transport="streamable-http")
    return app

# Use uvicorn to run the ASGI app
uvicorn.run(app, host="0.0.0.0", port=port)
```

### 2. **Configuration Updates**
- Updated `loaders.py` to support `LAZY_INIT` and `NON_INTERACTIVE` env vars
- Made credentials optional in `smithery.yaml` for tool discovery

### 3. **Dockerfile.smithery**
- Added `CHROMEDRIVER_PATH` environment variable
- Set all required environment variables for Smithery mode

## How Smithery Integration Works

1. **Tool Discovery Phase**:
   - Smithery sends requests to `/mcp` without credentials
   - Server must respond with available tools list
   - No Chrome driver or authentication needed

2. **Tool Execution Phase**:
   - Smithery passes credentials as query parameters: `/mcp?linkedin_email=...&linkedin_password=...`
   - Middleware extracts these and updates environment
   - Chrome driver is initialized only when tools are actually called

3. **Configuration Flow**:
   ```
   Smithery UI → Query Parameters → Middleware → Environment Variables → Config Reset → Tool Execution
   ```

## Testing Commands

```bash
# Local testing
chmod +x test_local_smithery.sh
./test_local_smithery.sh

# Manual server start
PORT=8000 uv run python smithery_main.py

# Test tool discovery
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","params":{},"id":1}'
```

## Deployment Steps

1. **Commit and push changes**:
   ```bash
   git add -A
   git commit -m "Fix Smithery deployment - proper query param handling and lazy init"
   git push origin feat/smithery-http-transport
   ```

2. **Monitor Smithery deployment**:
   - Check Docker build succeeds
   - Verify "Tool scanning" passes
   - Test connection with credentials

## Key Principles

1. **Lazy Loading**: No resources initialized until needed
2. **Query Parameter Config**: Smithery passes config via URL params, not env vars
3. **ASGI Application**: Use FastMCP's `http_app()` for proper HTTP handling
4. **Middleware**: Use Starlette middleware for HTTP request processing
5. **Non-Interactive**: No prompts or user input in container environment

## Troubleshooting

If deployment still fails:
1. Check Smithery logs for specific errors
2. Ensure ChromeDriver is available at `/usr/bin/chromedriver` in container
3. Verify all Python dependencies are installed
4. Test locally with `test_local_smithery.sh` first
