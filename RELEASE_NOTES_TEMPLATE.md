## Install or update

**uvx:** clients configured with `mcp-server-linkedin@latest` pick up v${VERSION} on their next start.

**Claude Desktop:** download [linkedin-mcp-server-v${VERSION}.mcpb](https://github.com/stickerdaniel/linkedin-mcp-server/releases/download/v${VERSION}/linkedin-mcp-server-v${VERSION}.mcpb) and open it. Bundles do not update themselves, so install each release this way.

**Docker:** `latest` always points to the newest release. To pin this one:

```bash
docker pull stickerdaniel/linkedin-mcp-server:${VERSION}
```

New here? The [README](https://github.com/stickerdaniel/linkedin-mcp-server#readme) covers setup.
