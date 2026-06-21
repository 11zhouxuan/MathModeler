# Portal for ALB + Fargate (tech-design §8.2, plan C). Build context = repo root
# (mathmodeler/) so we can COPY common and build the frontend SPA.
# Multi-stage: (1) Node builds the Next.js static export -> out/;
#              (2) ARM64 python runtime serves it + the FastAPI streaming API.

# --- stage 1: build the frontend (Next.js + AI SDK v6 + shadcn/ui) --------

FROM --platform=linux/arm64 node:20-slim AS frontend
WORKDIR /fe
COPY portal/frontend/package.json portal/frontend/package-lock.json* ./
RUN npm install
COPY portal/frontend/ ./
# Turbopack (Next.js 16 default bundler) skips PostCSS on ARM64 Docker.
# Pre-compile Tailwind v4 CSS via the CLI so the build receives plain CSS.
RUN npx @tailwindcss/cli@4 -i app/globals.css -o app/globals.compiled.css --minify \
    && mv app/globals.compiled.css app/globals.css
RUN npm run build   # -> /fe/out

# --- stage 2: python runtime (FastAPI on uvicorn) -------------------------
FROM --platform=linux/arm64 python:3.12-slim
WORKDIR /app

# Shared library (provides mm_common.invoke for the SSE proxy).
COPY common /opt/common
RUN pip install --no-cache-dir -e /opt/common

COPY portal/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Built SPA from stage 1 -> /app/static (served by FastAPI).
COPY --from=frontend /fe/out/ /app/static/

COPY portal/backend/server.py /app/server.py

ENV STATIC_DIR=/app/static
EXPOSE 8080

# Single worker keeps the in-process token store + streaming generator simple.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
# cache-bust: 1782017813
