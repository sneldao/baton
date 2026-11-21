#!/usr/bin/env bash
# Public HTTPS tunnel for the phone client.
#
# Phone browsers only allow getUserMedia (the mic) in a secure context —
# http://<lan-ip>:8765 is NOT one, so voice features silently fail over
# plain LAN HTTP. A cloudflared quick tunnel gives a free HTTPS origin:
#
#   ./run.sh &           # start baton first
#   ./tunnel.sh          # prints https://<random>.trycloudflare.com
#
# Then open https://<random>.trycloudflare.com/?token=<BATON_TOKEN> on the
# phone (token is printed by server.py at startup, or set BATON_TOKEN).
exec cloudflared tunnel --url "http://localhost:${PORT:-8765}"
