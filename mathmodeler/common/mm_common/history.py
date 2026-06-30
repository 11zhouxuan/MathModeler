"""mm_common.history — Load session history from AgentCore Memory and map to AI SDK v6 format.

Replaces DDB-based chat history. Loads the supervisor's Strands conversation
from AgentCore Memory, parses tool calls (run_subagent, update_task, ask_user,
thinking), and reconstructs AI SDK v6 UIMessage parts for the frontend.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from . import config

logger = logging.getLogger("mm.history")

_STAGE_OF = {
    "analyst": "analysis",
    "modeler": "modeling",
    "solver": "solving",
    "reporter": "report",
}


def load_session_history(session_id: str) -> list[dict]:
    """Load session history from AgentCore Memory and map to AI SDK format.

    Loads supervisor conversation + sub-agent tool call details from Memory.
    Returns a list of UIMessages: [{id, role, parts}].
    """
    raw_messages = _load_from_memory(session_id)
    if not raw_messages:
        return []
    pending = _load_pending_interrupts(session_id)
    subagent_histories = _load_subagent_histories(session_id, raw_messages)
    return _map_to_ai_sdk(raw_messages, session_id, pending, subagent_histories)


def _load_subagent_histories(session_id: str, supervisor_messages: list[dict]) -> dict[str, list[dict]]:
    """Load sub-agent conversation histories from AgentCore Memory.

    Scans supervisor messages to find run_subagent calls, then loads each
    sub-agent's history keyed by "{name}_{task_id}".

    Returns {agent_key: [messages]} where messages are Strands format.
    """
    # Find all run_subagent calls to know which sub-agent sessions to load
    agent_keys: set[str] = set()
    for m in supervisor_messages:
        for blk in m.get("content", []):
            tu = blk.get("toolUse")
            if tu and tu.get("name") == "run_subagent":
                inp = tu.get("input", {})
                name = inp.get("name", "")
                task_id = inp.get("task_id", "")
                if name and task_id:
                    agent_keys.add(f"{name}_{task_id}")
                elif name:
                    agent_keys.add(name)

    if not agent_keys:
        return {}

    result: dict[str, list[dict]] = {}
    for key in agent_keys:
        msgs = _load_agent_from_memory(session_id, key)
        if msgs:
            result[key] = msgs
            logger.info("[history] loaded sub-agent %s: %d messages", key, len(msgs))

    return result


def _load_agent_from_memory(session_id: str, agent_key: str) -> list[dict]:
    """Load a specific agent's messages from AgentCore Memory."""
    if not config.MEMORY_ID:
        return []
    try:
        from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
        from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
        from strands import Agent
        from .llm import make_model

        memory_config = AgentCoreMemoryConfig(
            memory_id=config.MEMORY_ID,
            session_id=f"{session_id}_{agent_key}",
            actor_id="system",
        )
        sm = AgentCoreMemorySessionManager(
            agentcore_memory_config=memory_config,
            region_name=config.REGION,
        )
        agent = Agent(model=make_model(), system_prompt="", callback_handler=None)
        sm.initialize(agent)
        return list(agent.messages or [])
    except Exception as e:
        logger.warning("[history] failed to load agent %s: %s", agent_key, e)
        return []


def _load_pending_interrupts(session_id: str) -> dict[str, str]:
    """Load pending interrupt mapping from S3 StateStore.

    Returns {toolUseId: full_interrupt_key} so we can map ask_user tool calls
    to the correct Strands interrupt IDs needed for resume.
    """
    from .state_store import S3StateStore
    try:
        store = S3StateStore()
        state = store.load(session_id)
        if not state or "pending" not in state:
            return {}
        # pending: {full_interrupt_key: {agent, question, ...}}
        # Build reverse map: toolUseId -> full_interrupt_key
        mapping = {}
        for key in state["pending"]:
            # key format: "v1:tool_call:{toolUseId}:{cycle_id}"
            parts = key.split(":")
            if len(parts) >= 3:
                tool_use_id = parts[2]
                mapping[tool_use_id] = key
        return mapping
    except Exception:
        return {}


def _load_from_memory(session_id: str) -> list[dict]:
    """Load raw Strands messages from AgentCore Memory for the supervisor session."""
    if not config.MEMORY_ID:
        return []
    try:
        from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
        from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
        from strands import Agent
        from .llm import make_model

        memory_config = AgentCoreMemoryConfig(
            memory_id=config.MEMORY_ID,
            session_id=f"{session_id}_supervisor",
            actor_id="system",
        )
        sm = AgentCoreMemorySessionManager(
            agentcore_memory_config=memory_config,
            region_name=config.REGION,
        )
        agent = Agent(model=make_model(), system_prompt="", callback_handler=None)
        sm.initialize(agent)
        return list(agent.messages or [])
    except Exception as e:
        logger.warning("[history] failed to load from memory session=%s: %s", session_id, e)
        return []


def _map_to_ai_sdk(messages: list[dict], session_id: str,
                    pending: dict[str, str] | None = None,
                    subagent_histories: dict[str, list[dict]] | None = None) -> list[dict]:
    """Map Strands messages to AI SDK v6 UIMessage format.

    Strands format: [{role, content: [text/toolUse/toolResult]}]
    AI SDK format: [{id, role, parts: [text/data-stage/data-task/data-ask/tool-*]}]

    pending: {toolUseId: full_interrupt_key} for correct interruptId mapping.
    subagent_histories: {agent_key: [messages]} for sub-agent tool call details.
    """
    result: list[dict] = []
    _pending = pending or {}
    _subagent_histories = subagent_histories or {}

    # Collect all messages into user/assistant groups
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "user")
        content = msg.get("content", [])

        if role == "user":
            parts = _map_user_message(content, i == 0, session_id)
            if parts:
                result.append({
                    "id": _gen_id(),
                    "role": "user",
                    "parts": parts,
                })

        elif role == "assistant":
            # An assistant turn may span multiple consecutive assistant messages
            # (Strands splits on tool boundaries). Collect all until next user msg.
            assistant_blocks: list[dict] = list(content)
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "assistant":
                assistant_blocks.extend(messages[j].get("content", []))
                j += 1
            # Also grab the toolResult messages (role=user with only toolResults)
            tool_results: dict[str, Any] = {}
            while j < len(messages):
                next_msg = messages[j]
                if next_msg.get("role") != "user":
                    break
                next_content = next_msg.get("content", [])
                has_tool_result = any("toolResult" in blk for blk in next_content)
                has_text = any("text" in blk for blk in next_content)
                if has_tool_result and not has_text:
                    for blk in next_content:
                        tr = blk.get("toolResult")
                        if tr:
                            tool_results[tr["toolUseId"]] = tr
                    j += 1
                else:
                    break

            parts = _map_assistant_turn(assistant_blocks, tool_results, _pending, _subagent_histories)
            if parts:
                result.append({
                    "id": _gen_id(),
                    "role": "assistant",
                    "parts": parts,
                })
            i = j
            continue

        i += 1

    return result


def _map_user_message(content: list[dict], is_first: bool, session_id: str) -> list[dict]:
    """Map a user message's content blocks to AI SDK parts."""
    parts = []
    if is_first:
        parts.append({"type": "data-session", "id": session_id, "data": {"session_id": session_id}})

    for blk in content:
        if "text" in blk:
            text = blk["text"]
            # Extract user problem from the wrapper format
            if "<user_problem>" in text:
                start = text.find("<user_problem>") + len("<user_problem>")
                end = text.find("</user_problem>")
                if end > start:
                    text = text[start:end].strip()
            elif text.startswith("session_id="):
                # Skip the session_id line, extract problem
                lines = text.split("\n", 2)
                text = lines[-1].strip() if len(lines) > 1 else text
            if text:
                parts.append({"type": "text", "text": text})
        elif "toolResult" in blk:
            # User providing tool result (e.g. ask_user answer) — skip
            pass

    return parts


def _map_assistant_turn(blocks: list[dict], tool_results: dict[str, Any],
                        pending: dict[str, str] | None = None,
                        subagent_histories: dict[str, list[dict]] | None = None) -> list[dict]:
    """Map an assistant turn's content blocks + tool results to AI SDK parts."""
    parts = []
    _pending = pending or {}
    _subagent_histories = subagent_histories or {}

    for blk in blocks:
        if "text" in blk:
            text = blk["text"]
            if text.strip():
                parts.append({"type": "text", "text": text})

        elif "toolUse" in blk:
            tu = blk["toolUse"]
            tool_id = tu["toolUseId"]
            name = tu["name"]
            tool_input = tu.get("input", {})
            tr = tool_results.get(tool_id)
            result_text = _extract_result_text(tr) if tr else ""

            if name == "thinking":
                # Skip thinking tool (internal reasoning)
                pass

            elif name == "update_task":
                tasks = tool_input.get("tasks", [])
                if tasks:
                    parts.append({
                        "type": "data-task",
                        "id": _gen_id(),
                        "data": {"tasks": tasks},
                    })

            elif name == "ask_user":
                question = tool_input.get("question", tool_input.get("prompt", ""))
                agent = tool_input.get("agent", "supervisor")
                # Use the full Strands interrupt key (from StateStore pending map)
                # so the frontend can correctly resume with interruptResponses.
                interrupt_id = _pending.get(tool_id, tool_id)
                parts.append({
                    "type": "data-ask",
                    "id": interrupt_id,
                    "data": {
                        "interruptId": interrupt_id,
                        "question": question,
                        "agent": agent,
                    },
                })

            elif name == "run_subagent":
                agent_name = tool_input.get("name", "subagent")
                task_id = tool_input.get("task_id", "")
                stage = _STAGE_OF.get(agent_name, agent_name)
                agent_key = f"{agent_name}_{task_id}" if task_id else agent_name
                # Stage start
                parts.append({
                    "type": "data-stage",
                    "id": _gen_id(),
                    "data": {"stage": stage, "status": "start", "agent": agent_name},
                })
                # Build agent parts from sub-agent history (tool calls + text)
                agent_parts = _build_subagent_parts(_subagent_histories.get(agent_key, []))
                if not agent_parts and result_text:
                    agent_parts = [{"type": "text", "text": result_text, "state": "done"}]
                if agent_parts:
                    parts.append({
                        "type": "data-agent",
                        "id": _gen_id(),
                        "data": {
                            "agent": agent_name,
                            "name": agent_name,
                            "stage": stage,
                            "parts": agent_parts,
                        },
                    })
                # Stage done
                parts.append({
                    "type": "data-stage",
                    "id": _gen_id(),
                    "data": {"stage": stage, "status": "done", "agent": agent_name},
                })

            else:
                # Other tools (shell, write_file, etc.)
                part = {
                    "type": f"tool-{name}",
                    "toolCallId": tool_id,
                    "state": "output-available" if tr else "input-available",
                    "input": tool_input,
                }
                if result_text:
                    part["output"] = result_text
                parts.append(part)

    return parts


def _build_subagent_parts(messages: list[dict]) -> list[dict]:
    """Build UI parts from a sub-agent's Strands conversation history.

    Maps the sub-agent's tool calls (execute_code, write_file, etc.) into
    tool-* parts matching the runtime SSE format.
    """
    if not messages:
        return []
    parts = []
    # Collect tool uses and their results
    tool_results_map: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "user":
            for blk in m.get("content", []):
                tr = blk.get("toolResult")
                if tr:
                    texts = [c.get("text", "") for c in tr.get("content", []) if c.get("text")]
                    tool_results_map[tr["toolUseId"]] = "\n".join(texts)

    for m in messages:
        if m.get("role") != "assistant":
            continue
        for blk in m.get("content", []):
            if "text" in blk and blk["text"].strip():
                parts.append({"type": "text", "text": blk["text"], "state": "done"})
            elif "toolUse" in blk:
                tu = blk["toolUse"]
                tname = tu.get("name", "tool")
                tid = tu.get("toolUseId", "")
                tinput = tu.get("input", {})
                output = tool_results_map.get(tid, "")
                # Extract description for title
                title = ""
                if isinstance(tinput, dict) and "description" in tinput:
                    title = str(tinput["description"])
                part: dict = {
                    "type": f"tool-{tname}",
                    "toolCallId": tid,
                    "state": "output-available" if output else "input-available",
                    "input": tinput,
                }
                if title:
                    part["title"] = title
                if output:
                    part["output"] = output
                parts.append(part)
    return parts


def _extract_result_text(tr: dict) -> str:
    """Extract text from a toolResult."""
    content = tr.get("content", [])
    texts = []
    for blk in content:
        if isinstance(blk, dict) and blk.get("text"):
            texts.append(blk["text"])
    return "\n".join(texts)


def _gen_id() -> str:
    return uuid.uuid4().hex[:16]
