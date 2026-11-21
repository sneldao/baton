#!/usr/bin/env bash
# Run the baton <-> Telegram bridge.
#
#   BATON_TOKEN=<token> ./telegram.sh                    # local baton on :8765
#   BATON_TOKEN=<token> BATON_WS=ws://localhost:8766/ws ./telegram.sh
#
# TELEGRAM_BOT_TOKEN comes from .env (gitignored) or the environment.
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

: "${TELEGRAM_BOT_TOKEN:?set TELEGRAM_BOT_TOKEN in .env — talk to @BotFather}"
: "${BATON_TOKEN:?set BATON_TOKEN to the token server.py printed}"

exec uv run python -u tools/telegram_bridge.py "$@"
