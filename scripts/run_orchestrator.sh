#!/usr/bin/env bash
# =============================================================================
# run_orchestrator.sh — start ONLY the Orchestrator Runtime locally (foreground).
# Usage:  bash scripts/run_orchestrator.sh
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MM_ROOT="${REPO_ROOT}/mathmodeler"
cd "${MM_ROOT}"

# Load local dev env vars (DDB table name, region, etc.)
if [ -f .local.env ]; then
  source .local.env
fi

ORCH_PORT="${ORCH_PORT:-8080}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-west-2}"
export MM_ORCHESTRATION="${MM_ORCHESTRATION:-supervisor}"
export PYTHONPATH="common:agents/orchestrator"

PROC_TAG="MM_PROC_TAG=mathmodeler-orchestrator"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║  MathModeler Orchestrator (Strands Supervisor)           ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# Kill previous instance by tag
OLD_PID=$(pgrep -f "${PROC_TAG}" 2>/dev/null || true)
if [ -n "$OLD_PID" ]; then
  echo "  ⏹  Killing previous orchestrator (PID: ${OLD_PID})…"
  kill $OLD_PID 2>/dev/null && sleep 1 || true
else
  echo "  ✓  No existing orchestrator process found"
fi

# Kill anything on the port
PORT_PID=$(lsof -ti :${ORCH_PORT} 2>/dev/null || true)
if [ -n "$PORT_PID" ]; then
  echo "  ⏹  Killing process on port ${ORCH_PORT} (PID: ${PORT_PID})…"
  kill $PORT_PID 2>/dev/null && sleep 1 || true
fi

echo ""
echo "  🚀 Starting Orchestrator"
echo "     URL:            http://127.0.0.1:${ORCH_PORT}"
echo "     Orchestration:  ${MM_ORCHESTRATION}"
echo "     Region:         ${AWS_DEFAULT_REGION}"
echo ""

exec env "${PROC_TAG}" uv run \
  --with fastapi --with "uvicorn[standard]" --with strands-agents \
  --with boto3 --with numpy --with bedrock-agentcore --with pydantic \
  python -m uvicorn app:app --host 127.0.0.1 --port "${ORCH_PORT}"
