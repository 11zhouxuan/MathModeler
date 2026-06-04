# MathModeler Portal v2 — Implementation Plan

**Companion to:** `2026-06-04-portal-v2-aisdk-hitl-design.md` (approved design)
**Approach:** four phases, each independently testable; existing suite stays green throughout; AWS deploy only at the end with user approval.

> Note: the `writing-plans` skill is not installed in this environment, so this plan is authored directly from the approved spec. It is deliberately concrete (files to touch, interfaces, tests) so implementation can proceed phase-by-phase.

---

## Conventions / ground rules

- Keep the **single-commit** git history (amend) unless told otherwise.
- Keep the **plan-C security baseline** (ALB default-403 + host-header allow + P1 gate) untouched.
- ARM64 everything; deploy on the Graviton host via SSM; never block a single `invoke` waiting on the user.
- Tests must not call real AWS — mock `mm_common.invoke`, the Strands session manager, and sub-agent `invoke_*`.
- Run the full suite after every phase: `cd mathmodeler && uv run --with pytest --with boto3 --with pydantic --with fastapi --with httpx --with numpy --with moto python -m pytest tests -q`.

---

## Phase 1 — Backend HITL / session (Orchestrator)

**Goal:** Orchestrator can suspend on `ask_user`, emit `need_input`, and resume from a restored session.

**Files**
- `mathmodeler/agents/orchestrator/app.py` — rework `stream_pipeline`/`_run` so the run is driven by a Strands `Agent` with `AgentCoreMemorySessionManager`; support `payload={resume:true, answer}` to continue.
- `mathmodeler/common/mm_common/tools.py` — add `ask_user(question, stage)` tool (raises/returns a sentinel that makes the current run end and surfaces `need_input`).
- `mathmodeler/common/mm_common/llm.py` — helper to build the supervisor `Agent` with the session manager (memory_id/session_id/actor_id), region us-west-2.
- `mathmodeler/agents/orchestrator/requirements.txt` — add `strands-agents`, `bedrock-agentcore` (session-manager integration).
- New SSE event types emitted by Orchestrator: `subagent` (start/delta/done per sub-agent) and `need_input`; keep `stage`, `final`, `error`.

**Interfaces**
- `build_supervisor(session_id, actor_id) -> Agent` (session manager attached).
- `stream_pipeline(body)` yields `data: <json>` for `stage`, `subagent`, `need_input`, `final`, `error`.
- Resume: `body = {session_id, actor_id, resume:true, answer}` → restore session → continue.

**Tests** (`tests/agents/test_orchestrator.py`, `tests/agents/test_subagents.py`)
- `ask_user` → pipeline yields a `need_input` event and stops (no `final`).
- resume path: given a restored session + answer, pipeline continues and reaches `final` (session manager + `invoke_agent` mocked).
- `subagent` events emitted around each `invoke_*` (start/done with method/success/attempts).

**Exit:** unit tests pass; no real AWS.

---

## Phase 2 — Portal `/api/chat` (AI SDK v6 stream adapter)

**Goal:** portal exposes `POST /api/chat` that proxies the Orchestrator and emits an AI SDK v6 UI Message Stream.

**Files**
- `mathmodeler/portal/backend/server.py` — add `/api/chat` (P1-gated): parse `{messages, session_id, actor_id, mode}`; derive start vs resume from the latest user message + whether a question is pending; call `invoke.stream_agent`; translate internal SSE → AI SDK v6 stream frames:
  - report text → `text` parts
  - `stage` → `data-stage`
  - `subagent` → `data-subagent` (stable `id` per agent for reconcile)
  - `need_input` → `data-ask`
  - `final` → `data-final` + finish; `error` → error.
  - Emit the exact AI SDK v6 UI-message-stream wire framing (SSE `data:` lines with the v6 part envelope) compatible with `DefaultChatTransport`.
- Keep `/healthz`, `/api/login`, static serving. Remove `/api/solve` (migrate tests).

**Tests** (`tests/integration/test_portal_backend.py`)
- mock `invoke.stream_agent` to emit canned `stage`/`subagent`/`need_input`/`final`; assert the response is valid AI SDK v6 framing containing the expected `data-stage`/`data-subagent`/`data-ask`/`data-final` parts and text.
- start vs resume request mapping.
- P1 auth: 401 without/with wrong token; 200 with token.

**Exit:** portal tests pass against `/api/chat`; full suite green.

---

## Phase 3 — Frontend (Vite + React + TS + AI SDK v6 + AI Elements), Layout B

**Goal:** replace the static `index.html` with a built SPA.

**Files / structure**
- New `mathmodeler/portal/frontend/` Vite app: `package.json` (`ai@6`, `@ai-sdk/react@3`, `ai-elements`, react/vite/tailwind/typescript), `index.html`, `src/` with:
  - `main.tsx`, `App.tsx` (P1 login gate → console).
  - Left **Chat**: AI Elements `Conversation`/`Message`/`PromptInput`/`Response`; `useChat` + `DefaultChatTransport({ api:'/api/chat', headers: Bearer })`; answer-mode when a `data-ask` part is present.
  - Right **Pipeline panel**: read `messages` parts; `data-stage` → four-stage timeline; `data-subagent` → AI Elements `Task`/`Tool` grouped by stage; report quick area.
  - Typed `ChatMessage = UIMessage<Meta, { stage; subagent; ask; final }>`.
- Build output `dist/` consumed by the portal image.

**Dockerfile** (`mathmodeler/portal/backend/Dockerfile`)
- Add a Node build stage: `npm ci && npm run build` in `portal/frontend` → copy `dist/` into the python image's `/app/static`. Final stage stays ARM64 `python:3.12-slim` + uvicorn.

**Tests**
- Minimal: a render/smoke test or a transport-wiring unit (as effort allows). Not blocking.

**Exit:** `npm run build` produces `dist/`; local `uv` portal serves it; manual chat smoke locally.

---

## Phase 4 — Integration + deploy

- Local: run portal via `uv` + a mocked/stubbed Orchestrator (or against deployed runtimes) to smoke the HITL round-trip.
- Update `docs/architecture.*` + READMEs (portal now chat + HITL; mention AI SDK v6 / AI Elements; sub-agent streams).
- Commit (amend to single commit) + push.
- **Deploy (user-approved):** Graviton host re-clone + `cdk deploy -c adminUser=admin -c adminPassword=...`. Orchestrator image rebuilds with strands/agentcore deps; portal image rebuilds with built frontend.
- **Verify:** Host→200 / raw-IP→403 (anti-scan intact); login; submit a problem; observe four-stage + sub-agent streams; trigger a clarifying question, answer it, confirm resume; final report renders (KaTeX/Markdown).

---

## Risks / watch-items

- **Strands session-manager resume semantics:** confirm that re-invoking with the same `session_id` restores enough state for mid-pipeline continuation (vs only chat history). If the deterministic per-subtask loop state isn't captured by chat history alone, persist a small `progress` doc in S3 keyed by `session_id` and reconcile on resume. (Build Phase 1 to tolerate both.)
- **AI SDK v6 is beta:** pin exact versions matching agent-craft (`ai@6.0.x`, `@ai-sdk/react@3.0.x`, `ai-elements@1.9.x`); the v6 UI-message-stream wire format must match what `DefaultChatTransport` expects — validate with a tiny end-to-end stream test in Phase 2.
- **AgentCore single-invoke time limits:** since we never block on the user, each invoke is bounded by one (possibly multi-stage) run segment; long Solver runs still apply — keep ALB idle timeout (900s) and confirm Runtime limits.
- **Frontend build in the portal image:** adds Node to the build; keep it a multi-stage build so the runtime image stays slim/ARM64.
