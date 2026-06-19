#!/usr/bin/env bash
# =============================================================================
# run_portal.sh — start ONLY the Portal backend locally (foreground).
# Requires orchestrator first (scripts/run_orchestrator.sh).
# Usage:  bash scripts/run_portal.sh
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MM_ROOT="${REPO_ROOT}/mathmodeler"
cd "${MM_ROOT}"

# Load local dev env vars (DDB table name, region, etc.)
if [ -f .local.env ]; then
  source .local.env
fi

PORTAL_PORT="${PORTAL_PORT:-8090}"
ORCH_PORT="${ORCH_PORT:-8080}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-west-2}"
export PORTAL_ADMIN_USER="${PORTAL_ADMIN_USER:-admin}"
export PORTAL_ADMIN_PASSWORD="${PORTAL_ADMIN_PASSWORD:-demo123}"
export MM_LOCAL_ORCHESTRATOR_URL="${MM_LOCAL_ORCHESTRATOR_URL:-http://127.0.0.1:${ORCH_PORT}}"
export PYTHONPATH="common:portal/backend:agents/orchestrator:."

PROC_TAG="MM_PROC_TAG=mathmodeler-portal"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║  MathModeler Portal (FastAPI + SSE proxy)                ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# Kill previous instance by tag
OLD_PID=$(pgrep -f "${PROC_TAG}" 2>/dev/null || true)
if [ -n "$OLD_PID" ]; then
  echo "  ⏹  Killing previous portal (PID: ${OLD_PID})…"
  kill $OLD_PID 2>/dev/null && sleep 1 || true
else
  echo "  ✓  No existing portal process found"
fi

# Kill anything on the port
PORT_PID=$(lsof -ti :${PORTAL_PORT} 2>/dev/null || true)
if [ -n "$PORT_PID" ]; then
  echo "  ⏹  Killing process on port ${PORTAL_PORT} (PID: ${PORT_PID})…"
  kill $PORT_PID 2>/dev/null && sleep 1 || true
fi

echo ""
echo "  🚀 Starting Portal"
echo "     URL:           http://127.0.0.1:${PORTAL_PORT}"
echo "     Orchestrator:  ${MM_LOCAL_ORCHESTRATOR_URL}"
echo "     Login:         ${PORTAL_ADMIN_USER} / ${PORTAL_ADMIN_PASSWORD}"
echo ""

exec env "${PROC_TAG}" uv run \
  --with fastapi --with "uvicorn[standard]" --with strands-agents \
  --with boto3 --with numpy --with bedrock-agentcore --with pydantic \
  python -m uvicorn scripts.local_portal:app --host 127.0.0.1 --port "${PORTAL_PORT}"
