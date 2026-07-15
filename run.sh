#!/usr/bin/env bash
# run.sh — NixOS wrapper for linkedin-mcp-server.
#
# Two things are missing on a stock NixOS host for this project to run:
#   1. `greenlet` (a Patchright/Playwright dependency) ships a prebuilt .so
#      that dynamically links libstdc++.so.6 from the standard FHS path,
#      which doesn't exist on NixOS.
#   2. Patchright's downloaded Chromium (chrome-headless-shell / Chrome for
#      Testing) AND Camoufox's downloaded Firefox are unpatched foreign
#      binaries and need ~30 shared libraries (glib, nss, gtk3, X11, mesa's
#      libgbm, ...) that also don't live on the standard FHS path.
#   3. `--import-from-browser` decrypts a Linux browser's cookies via the
#      `secret-tool` CLI (from libsecret), which isn't installed by default.
#
# This script resolves all of that from nixpkgs on every run (so it never
# goes stale after a `nix-collect-garbage` or channel bump) and then execs
# the real server. Usage is identical to the README's `uv run -m
# linkedin_mcp_server`, just prefixed with `./run.sh`:
#
#   ./run.sh --login
#   ./run.sh --import-from-browser brave
#   ./run.sh --status
#   ./run.sh                       # start the MCP server (stdio)
#
# Defaults to the Camoufox (Firefox) engine -- override per-invocation with
# `--browser patchright` (last `--browser` wins) or `BROWSER_ENGINE=patchright`.
#
# The `playwright` pip dependency is pinned to ==1.59.0 (see pyproject.toml),
# NOT the newest version camoufox's own `<1.61` constraint would allow:
# playwright==1.60.0's Firefox glue crashes the whole Node driver process on
# an uncaught in-page JS error with no `.location` (a real upstream bug, not
# ours). 1.59.0 does not reproduce this; do not bump past it without
# re-running `pytest -m camoufox_smoke` and re-verifying an authenticated
# /feed/ navigation first (see docs/camoufox-engine.md).
#
# For an MCP client config (Claude Desktop etc.), point "command" at this
# script's absolute path with no args, and pass server flags via "args".

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

LIB_PKGS=(
  glib.out nss nspr dbus.lib dbus-glib at-spi2-core cups.lib gtk3 pango cairo
  gdk-pixbuf alsa-lib libxkbcommon libgbm expat libdrm systemd
  libx11 libxcb libxcomposite libxcursor libxdamage libxext libxfixes
  libxi libxrandr libxrender libxtst libxshmfence freetype fontconfig.lib
  stdenv.cc.cc.lib
)

# Resolving ~20 nixpkgs attrs on every single invocation (including every
# MCP tool call's server startup) is a real latency tax. Cache the result,
# but re-validate every cached directory still exists on disk before
# trusting it -- a `nix-collect-garbage` between runs can delete an
# unrooted (`--no-link`) store path out from under a naively time-based
# cache, which would silently reintroduce the exact "missing shared
# library" crash this script exists to prevent.
cache_dir="$(pwd)/.run-cache"
cache_file="$cache_dir/ld_path"
pkgs_key=$(printf '%s\n' "${LIB_PKGS[@]}" | sha256sum | cut -d' ' -f1)

ld_path=""
cache_valid=0
if [ -f "$cache_file" ]; then
  cached_key=$(sed -n '1p' "$cache_file")
  cached_path=$(sed -n '2p' "$cache_file")
  if [ "$cached_key" = "$pkgs_key" ] && [ -n "$cached_path" ]; then
    cache_valid=1
    IFS=':' read -ra cached_dirs <<< "$cached_path"
    for dir in "${cached_dirs[@]}"; do
      [ -d "$dir" ] || { cache_valid=0; break; }
    done
  fi
fi

if [ "$cache_valid" -eq 1 ]; then
  ld_path="$cached_path"
else
  failed_pkgs=()
  for pkg in "${LIB_PKGS[@]}"; do
    resolved=""
    if ! resolved=$(nix build --no-link --print-out-paths "nixpkgs#${pkg}" 2>/dev/null); then
      failed_pkgs+=("$pkg")
      continue
    fi
    while IFS= read -r out; do
      [ -d "$out/lib" ] || continue
      ld_path="${ld_path:+$ld_path:}$out/lib"
    done <<< "$resolved"
  done

  if [ "${#failed_pkgs[@]}" -gt 0 ]; then
    echo "run.sh: failed to resolve these nixpkgs attrs: ${failed_pkgs[*]}" >&2
    echo "run.sh: refusing to launch with a partial LD_LIBRARY_PATH -- that" >&2
    echo "  risks a missing-shared-library crash. Check for a nixpkgs rename" >&2
    echo "  or typo in LIB_PKGS (run.sh) and fix it before retrying." >&2
    exit 1
  fi

  mkdir -p "$cache_dir"
  printf '%s\n%s\n' "$pkgs_key" "$ld_path" > "$cache_file"
fi

exec nix shell nixpkgs#libsecret --command env \
  LD_LIBRARY_PATH="$ld_path" \
  BROWSER_ENGINE="${BROWSER_ENGINE:-camoufox}" \
  uv run -m linkedin_mcp_server "$@"
