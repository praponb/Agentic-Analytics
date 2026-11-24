#!/usr/bin/env bash
# Deploy the sales dashboard to the Ubuntu server and (re)start it.
#
#   agentic_analytics_demo/deploy/deploy.sh            # default host prapon@192.168.1.49
#   DEPLOY_HOST=user@host deploy/deploy.sh
#
# Copies the project (no local data, venv or secrets) to ~/agentic-dashboard on the server,
# sends the tunnel credentials file with mode 600, then builds and starts the containers.
# The image build runs `dbt build`, so a failing dbt test stops the deploy.
set -euo pipefail

HOST="${DEPLOY_HOST:-prapon@192.168.1.49}"
REMOTE_DIR="${DEPLOY_DIR:-agentic-dashboard}"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$PROJECT/deploy/cloudflared/config.yml"

TUNNEL_ID="$(awk '/^tunnel:/ {print $2}' "$CONFIG")"
CREDS="$PROJECT/deploy/cloudflared/$TUNNEL_ID.json"
[ -f "$CREDS" ] || CREDS="$HOME/.cloudflared/$TUNNEL_ID.json"
[ -f "$CREDS" ] || { echo "Tunnel credentials $TUNNEL_ID.json not found (deploy/cloudflared/ or ~/.cloudflared/)." >&2; exit 1; }

echo "==> Copying project to $HOST:~/$REMOTE_DIR"
rsync -az --delete \
  --exclude .venv/ --exclude '__pycache__/' --exclude .pytest_cache/ --exclude target/ \
  --exclude logs/ --exclude dbt_packages/ --exclude data_lake/ --exclude 'warehouse.duckdb*' \
  --exclude .env --exclude docker/.env --exclude .mcp.json --exclude .user.yml \
  --exclude 'deploy/.env' --exclude 'deploy/cloudflared/*.json' \
  "$PROJECT/" "$HOST:$REMOTE_DIR/"

echo "==> Sending tunnel credentials (mode 600)"
ssh "$HOST" "umask 077 && cat > $REMOTE_DIR/deploy/cloudflared/$TUNNEL_ID.json && chmod 600 $REMOTE_DIR/deploy/cloudflared/$TUNNEL_ID.json" < "$CREDS"

echo "==> Building and starting containers (runs dbt build; first run takes a few minutes)"
ssh "$HOST" "cd $REMOTE_DIR/deploy \
  && printf 'HOST_UID=%s\nHOST_GID=%s\n' \"\$(id -u)\" \"\$(id -g)\" > .env \
  && docker compose up -d --build --remove-orphans \
  && for i in \$(seq 1 30); do \
       [ \"\$(docker inspect -f '{{.State.Health.Status}}' agentic-dashboard-dashboard-1 2>/dev/null)\" = healthy ] && break; sleep 2; \
     done \
  && docker compose ps --format '{{.Service}}: {{.State}} {{.Status}}' \
  && echo -n 'health: ' && curl -s http://127.0.0.1:8502/_stcore/health && echo"
