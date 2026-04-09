For an installation guide, refer to the [README](https://github.com/stickerdaniel/linkedin-mcp-server/blob/main/README.md).

## 📦 Update MCP Bundle Installation
**For Claude Desktop users:**

```
${DOWNLOAD_BTN}
```
**👆 [Click here to download](https://github.com/stickerdaniel/linkedin-mcp-server/releases/download/v${VERSION}/linkedin-mcp-server-v${VERSION}.mcpb)**

Then click the downloaded file to install in Claude Desktop.

> **Note:** MCP Bundles do not auto-update. You need to download and install the latest `.mcpb` file for each new release.

## 🐳 Update Docker Installation
**For users with Docker-based MCP client configurations:**
```bash
docker pull stickerdaniel/linkedin-mcp-server:latest
```
The `latest` tag will always point to the most recent release.
To pull this specific version, run:
```bash
docker pull stickerdaniel/linkedin-mcp-server:${VERSION}
```
