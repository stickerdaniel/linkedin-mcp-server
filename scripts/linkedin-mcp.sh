#!/usr/bin/env zsh
# Repo-local launcher for the LinkedIn MCP server.
set -euo pipefail

repo_dir="${0:A:h:h}"
cd "$repo_dir"

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.linkedin-mcp/patchright-browsers}"

args=(-m linkedin_mcp_server)

case "${1:-}" in
  ""|start)
    [[ $# -gt 0 ]] && shift
    ;;
  login)
    shift
    args+=(--login "$@")
    ;;
  login-serve)
    shift
    args+=(--login-serve "$@")
    ;;
  status)
    shift
    args+=(--status "$@")
    ;;
  http)
    shift
    args+=(--transport streamable-http --host 127.0.0.1 --port 8000 --path /mcp "$@")
    ;;
  help|-h|--help)
    cat <<'EOF'
Usage:
  ./start
  ./start login
  ./start login-serve
  ./start status
  ./start http
  ./start --no-headless
EOF
    exit 0
    ;;
  *)
    args+=("$@")
    ;;
esac

command=(uv run "${args[@]}")

if [[ "${LINKEDIN_MCP_DRY_RUN:-0}" == "1" ]]; then
  for arg in "${command[@]}"; do
    print -r -- "$arg"
  done
  exit 0
fi

exec "${command[@]}"
