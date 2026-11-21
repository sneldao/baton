#!/usr/bin/env bash
# Run baton against any vibe-acp.
#
#   ./run.sh                          # sibling ../mistral-vibe clone, or vibe-acp on PATH
#   ./run.sh /path/to/vibe/fork       # a repo dir runnable via `uv run vibe-acp`
#   ./run.sh "some vibe-acp --flags"  # an explicit command
#
# MISTRAL_API_KEY must be exported (or set in ~/.vibe/.env).
set -euo pipefail
cd "$(dirname "$0")"

if [[ $# -ge 1 ]]; then
  if [[ -d "$1" ]]; then
    export VIBE_ACP_CMD="uv run --directory $(cd "$1" && pwd) vibe-acp"
  else
    export VIBE_ACP_CMD="$1"
  fi
  shift
fi

exec uv run python server.py "$@"
