# Single-Runtime agents-as-tools image (§4/§8): the Orchestrator supervisor and
# all four sub-agents (Analyst/Modeler/Solver/Reporter) run in ONE process.
# Therefore this image must carry the union of all agents' dependencies:
#   * HMML library (Modeler retrieval)
#   * bedrock-agentcore Code Interpreter (Solver)
#   * numpy (embedding/scoring)
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY common /app/common
COPY agents/orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e /app/common
COPY agents/orchestrator/app.py .
COPY HMML /app/HMML
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
