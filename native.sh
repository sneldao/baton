#!/usr/bin/env bash
# Opt-in demo: run the real Vibe TUI with approvals routed through baton.
# Launches the installed vibe CLI with this repo's native_cli.py shim as
# its app server; approvals go to a baton client (e.g. the Telegram
# bridge on BATON_WS/BATON_HTTP pointed at BATON_NATIVE_PORT, default 8767).
set -euo pipefail

if [ -z "${BATON_TOKEN:-}" ]; then
  echo "BATON_TOKEN is required (e.g. export BATON_TOKEN=\$(openssl rand -hex 32))" >&2
  exit 1
fi

launcher=$(command -v vibe) || {
  echo "vibe is not installed on PATH" >&2
  exit 1
}
command -v vibe-app-server >/dev/null || {
  echo "vibe-app-server is not on PATH" >&2
  exit 1
}
uv_bin=$(command -v uv) || {
  echo "uv is not on PATH" >&2
  exit 1
}

# The vibe wrapper is a wheel script whose shebang is its Python interpreter.
vibe_python=${BATON_VIBE_PYTHON:-$(sed -n '1s/^#!//p' "$launcher")}
if [ ! -x "$vibe_python" ]; then
  echo "could not resolve the vibe interpreter from $launcher" >&2
  echo "set BATON_VIBE_PYTHON to the Python that has mistral-vibe installed" >&2
  exit 1
fi

root=$(cd "$(dirname "$0")" && pwd)

exec "$vibe_python" -c 'import sys; from vibe.cli.launcher import exec_rust_cli; uv, root, *args = sys.argv[1:]; exec_rust_cli(args, app_server_cmd=[uv, "run", "--directory", root, "--no-sync", "python", "-u", root + "/native_cli.py"])' "$uv_bin" "$root" "$@"
