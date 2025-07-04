#!/bin/bash
# Local test script for Smithery configuration

echo "🔍 Testing LinkedIn MCP Server (Smithery mode)..."
echo "================================================"

# Set environment variables
export PORT=8000
export LAZY_INIT=true
export NON_INTERACTIVE=true
export HEADLESS=true
export DEBUG=false

# Start the server in background
echo "Starting server on port $PORT..."
uv run python smithery_main.py &
SERVER_PID=$!

# Wait for server to start
echo "Waiting for server to start..."
sleep 5

# Test the server
echo -e "\n📡 Testing server endpoints..."

# Test 1: Initialize without credentials
echo -e "\n1. Testing initialize endpoint (no credentials)..."
curl -X POST "http://localhost:${PORT}/mcp" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
      "protocolVersion": "0.1.0",
      "capabilities": {},
      "clientInfo": {
        "name": "test-client",
        "version": "1.0.0"
      }
    },
    "id": 0
  }' -w "\nHTTP Status: %{http_code}\n" | jq . || echo "Failed to parse JSON"

# Test 2: List tools without credentials
echo -e "\n2. Testing tools/list endpoint (no credentials)..."
curl -X POST "http://localhost:${PORT}/mcp" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/list",
    "params": {},
    "id": 1
  }' -w "\nHTTP Status: %{http_code}\n" | jq . || echo "Failed to parse JSON"

# Test 3: Test with query parameters (simulating Smithery)
echo -e "\n3. Testing with query parameters (Smithery style)..."
curl -X POST "http://localhost:${PORT}/mcp?linkedin_email=test@example.com&linkedin_password=testpass" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/list",
    "params": {},
    "id": 2
  }' -w "\nHTTP Status: %{http_code}\n" | jq . || echo "Failed to parse JSON"

# Clean up
echo -e "\n🛑 Stopping server..."
kill $SERVER_PID 2>/dev/null
wait $SERVER_PID 2>/dev/null

echo -e "\n✅ Test complete!"
