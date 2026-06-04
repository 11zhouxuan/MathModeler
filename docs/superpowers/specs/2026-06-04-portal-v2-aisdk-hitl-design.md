# MathModeler Portal v2 — Chat UI (AI SDK v6 + AI Elements) with Human-in-the-Loop

**Date:** 2026-06-04
**Status:** Approved (design); pending implementation plan
**Supersedes the portal frontend/contract of:** plan C (ALB + Fargate, single-shot `/api/solve`)

## 1. Goal

Turn the MathModeler portal into a **chat experience with true human-in-the-loop (HITL)**:

- User submits an open-ended math-modeling problem in a chat box.
- The Orchestrator runs the four-stage pipeline (Analysis → Modeling → Solving → Reporting). **At any stage** it may pause and ask the user a clarifying question.
- The run **suspends**, the question is shown in the chat, the user replies, and the run **resumes from where it left off**.
- When finished, the solution report renders in the chat / report panel. The user can then submit a **new** task.
- Everything streams in real time.

Non-goals: multi-user accounts/roles (P1 password gate stays), Cognito/OIDC, custom domain/HTTPS (host-header + HTTP stays).

## 2. Architecture (keeps plan C security baseline)

```
Browser (Vite + React + TS, AI SDK v6 + AI Elements)
  │  useChat → DefaultChatTransport → POST /api/chat   (P1 login gate)
  ▼  HTTP, Host = ALB DNS
ALB (HTTP:80)  — default action 403; only Host == ALB *.elb DNS is forwarded (anti-scan)
  ▼
Fargate — FastAPI portal
  • P1 password gate (bearer token)
  • POST /api/chat: adapt internal Orchestrator SSE → AI SDK v6 UI Message Stream
  • serves built frontend dist/
  ▼  boto3 InvokeAgentRuntime (same session_id across turns)
Orchestrator Runtime — Strands Agent + AgentCoreMemorySessionManager
  • conversation/session state persists & auto-restores across invocations (memory_id+session_id+actor_id)
  • new ask_user tool: when info is insufficient, end the current invoke and emit need_input
  ▼ invoke_* (sub-agents)            ▼ S3 doc bus     ▼ Code Interpreter    ▼ Bedrock (claude-opus-4)
Analyst → Modeler → Solver → Reporter
```

The browser still goes **through the portal proxy** (no Cognito). The ALB host-header gating + P1 gate from the current deployment are unchanged.

## 3. HITL suspend / resume (core mechanism)

**Persistence layer:** the Orchestrator becomes a Strands `Agent` configured with `AgentCoreMemorySessionManager`
(`bedrock_agentcore.memory.integrations.strands`), using the existing AgentCore Memory resource
(`memory_id`) plus a per-conversation `session_id` and `actor_id`. Strands persists conversation
messages to Memory and **auto-restores them on the next invocation with the same `session_id`** — this is
the resume substrate (no bespoke state store needed for conversation history; per-task artifacts continue
to live in the S3 document bus keyed by `session_id`).

**ask_user tool:** a new tool exposed to the Orchestrator agent. When any stage determines it lacks
information, the agent calls `ask_user(question, stage)`. The current `invoke_agent_runtime` call then
**ends** (it does NOT block waiting for the user) after emitting an SSE event
`{type: "need_input", question, stage}`. The pending question is part of the agent's persisted state.

**Resume turn:** the user types an answer in the chat. The portal issues another
`InvokeAgentRuntime` with the **same `session_id`** and `payload = {resume: true, answer}`. Strands
restores the prior conversation; the agent receives the answer as the tool result / next user message and
**continues from the suspension point** through the remaining stages.

**Pipeline shape:** the current deterministic `_run()` loop is refactored so that (a) stages can trigger
`ask_user`, and (b) progress is recoverable from the persisted session + S3 artifacts across multiple
invocations. The four-stage ordering and sub-agent calls (`invoke_*`) are preserved.

**Terminal events:** `{type: "final", report_key, report_url}` ends the task; `{type: "error", message}`
surfaces failures. After `final`, the user may submit a new task (new or reused `session_id`).

## 4. Wire protocol (portal ↔ frontend): AI SDK v6 UI Message Stream

The portal exposes **`POST /api/chat`** as the `DefaultChatTransport` target. It invokes the Orchestrator
and **adapts** the internal SSE (`{type: stage|need_input|final|error}`) into the AI SDK v6 UI Message
Stream parts:

| Internal SSE | AI SDK v6 stream part | Frontend consumption |
| --- | --- | --- |
| report text / increments | `text` parts | `Response` component (Markdown + KaTeX) in chat |
| `{type:stage, stage, status, ...}` | custom `data-stage` part | right-hand **pipeline panel** (four-stage timeline) |
| `{type:subagent, agent, status, text?}` | custom `data-subagent` part | per-sub-agent stream card (see §4a) |
| `{type:need_input, question, stage}` | custom `data-ask` part | chat shows the question; composer switches to "answer mode" |
| `{type:final, report_url}` | `data-final` part + finish | render report; mark task complete |
| `{type:error, message}` | error | error bubble |

Request body carries `{messages, session_id, actor_id, mode: "start"|"resume"}` (or equivalent derived from
the AI SDK message list). The portal maps the latest user message to either a new problem (`start`) or an
answer to a pending question (`resume`).

### 4a. Sub-agent stream display

AI SDK v6 **does** support showing sub-agent activity (not present in agent-craft). We model each of the
four sub-agents (Analyst / Modeler / Solver / Reporter) as a **typed custom data part** on the
supervisor's single conversation turn:

- **Typed message:** `UIMessage<Metadata, { stage: {...}; subagent: { agent: string; status: 'running'|'done'|'error'; text?: string; method?: string; success?: boolean; attempts?: number }; ask: {...}; final: {...} }>`.
- **Server (portal `/api/chat`):** build the response with `createUIMessageStream` and, as the Orchestrator
  SSE arrives, `writer.write({ type: 'data-subagent', id: <agent>, data: {...} })` — using a **stable `id`
  per sub-agent** so repeated writes (status/text deltas) *reconcile into the same part* (AI SDK merges
  data parts by `id`) instead of appending duplicates. Stage/ask/final parts written the same way.
- **Client:** read `message.parts`, filter `part.type === 'data-subagent'`, and render each with an
  **AI Elements `Task`** (collapsible "Analyst → done", "Solver → 2 attempts, success") and/or **`Tool`**
  component; live `text` deltas stream inside. These render in the right-hand pipeline panel grouped under
  their stage; the four-stage `data-stage` timeline remains the top-level progress.

So the Orchestrator emits, per sub-agent invocation, `subagent` SSE events (start/delta/done) in addition
to the coarse `stage` events; the portal forwards them as `data-subagent` parts; the UI shows each
sub-agent's own streamed progress distinctly.


Auth: `/api/chat` requires the P1 bearer token (same gate as before). `/healthz` stays public for the ALB.
The old `/api/solve` is removed (or kept only behind a flag for transitional tests) — tests are rewritten
against `/api/chat`.

## 5. Frontend (Vite + React + TypeScript + AI Elements)

- **Tooling:** Vite + React + TS; Tailwind; **AI SDK v6** (`ai`, `@ai-sdk/react`) + **AI Elements**
  (`Conversation`, `Message`, `PromptInput`, `Response`, etc. from elements.ai-sdk.dev). Build output is a
  **static `dist/`** served by the FastAPI portal (Dockerfile gains a Node build stage; final image still
  ARM64 uvicorn serving static + `/api/*`).
- **Layout B (approved):**
  - **Left — Chat** (AI Elements): the conversation, including HITL questions and user answers; the final
    report renders here via `Response` (Markdown/KaTeX). `useChat` + `DefaultChatTransport` → `/api/chat`.
  - **Right — Pipeline panel** (custom): the four-stage timeline (Analysis/Modeling/Solving/Reporting)
    driven by `data-stage` parts, plus a quick link/area for the report.
- **P1 login gate**: unchanged behavior (first-screen password gate; bearer token attached to `/api/chat`).
- **Answer mode:** when a `data-ask` part arrives, the composer indicates the agent is asking and the next
  user message is sent as a `resume` answer for the active `session_id`.

## 6. Testing

- **Portal (`/api/chat`)** — unit/integration with a mocked Orchestrator SSE (no AWS): assert the AI SDK v6
  stream framing for `text`, `data-stage`, `data-ask`, `data-final`; assert the `start` vs `resume` request
  mapping; assert P1 auth (401 without/with wrong token). Host-gating is infra, out of unit scope.
- **Orchestrator** — unit tests for the `ask_user` tool (emits `need_input` and suspends) and the
  resume path (restored session continues), with Memory/session-manager and sub-agent `invoke_*` mocked.
- **Frontend** — minimal smoke (transport wiring / a render test) as effort allows.
- Existing suite must stay green; portal tests migrate from `/api/solve` to `/api/chat`.

## 7. Deployment

Single Graviton EC2, one `cdk deploy`:
- Frontend built (Node stage) into the portal image; portal image stays ARM64 uvicorn.
- Orchestrator image gains `strands-agents` + `bedrock-agentcore` (session manager) deps; Memory
  read/write IAM already present on the runtime role.
- ALB host-header gating + P1 gate unchanged; output remains `http://<albDns>`.

## 8. Phasing (for the implementation plan)

1. **Backend HITL/session:** Orchestrator → Strands Agent + AgentCoreMemorySessionManager; add `ask_user`;
   refactor pipeline for suspend/resume; emit `need_input`. Unit tests (mocked).
2. **Portal protocol:** add `/api/chat` AI SDK v6 stream adapter (start/resume); keep P1; rewrite portal
   tests; remove `/api/solve`.
3. **Frontend:** Vite+React+AI Elements layout B (left chat / right pipeline), `useChat` transport,
   `data-stage`/`data-ask`/report rendering, P1 gate; build into portal image.
4. **Integration + deploy:** local `uv` + `vite` smoke; redeploy on Graviton; verify HITL round-trip and
   anti-scan (Host → 200, raw IP → 403).

## Open assumptions (made explicit)

- AI SDK **v6** (currently beta) is acceptable, matching agent-craft (`ai@6`, `@ai-sdk/react@3`).
- `ask_user` triggers are LLM-driven (the agent decides when info is insufficient); no fixed rule set.
- One in-flight task per session at a time; a new task may reuse or start a new `session_id`.
