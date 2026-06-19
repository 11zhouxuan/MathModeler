# Single-Runtime agents-as-tools image (§4/§8): the Orchestrator supervisor and
# all four sub-agents (Analyst/Modeler/Solver/Reporter) run in ONE process.
# Therefore this image must carry the union of all agents' dependencies:
#   * HMML library (Modeler retrieval)
#   * bedrock-agentcore Code Interpreter (Solver)
#   * numpy (embedding/scoring)
#   * texlive + xelatex + Chinese fonts (Reporter LaTeX compilation)
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

# Install LaTeX (xelatex) for report compilation + Chinese font support
RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-latex-base \
    texlive-latex-extra \
    texlive-fonts-recommended \
    texlive-lang-chinese \
    texlive-science \
    texlive-xetex \
    latexmk \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create workspace directory (symlinked to /mnt/workspace/jobs in Runtime)
RUN mkdir -p /app/jobs

COPY common /app/common
COPY agents/orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e /app/common
COPY agents/orchestrator/app.py .
COPY HMML /app/HMML

# Default workspace root (overridden by MM_WORKSPACE_ROOT in Runtime)
ENV MM_WORKSPACE_ROOT=/app/jobs

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
