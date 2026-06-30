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
    """Load supervisor conversation from AgentCore Memory and map to AI SDK format.

    Returns a list of UIMessages: [{id, role, parts}].
    """
    raw_messages = _load_from_memory(session_id)
    if not raw_messages:
        return []
    return _map_to_ai_sdk(raw_messages, session_id)


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


def _map_to_ai_sdk(messages: list[dict], session_id: str) -> list[dict]:
    """Map Strands messages to AI SDK v6 UIMessage format.

    Strands format: [{role, content: [text/toolUse/toolResult]}]
    AI SDK format: [{id, role, parts: [text/data-stage/data-task/data-ask/tool-*]}]
    """
    result: list[dict] = []
    # First message from user contains the problem
    user_msg_idx = 0

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

            parts = _map_assistant_turn(assistant_blocks, tool_results)
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


def _map_assistant_turn(blocks: list[dict], tool_results: dict[str, Any]) -> list[dict]:
    """Map an assistant turn's content blocks + tool results to AI SDK parts."""
    parts = []

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
                question = tool_input.get("question", "")
                agent = tool_input.get("agent", "supervisor")
                parts.append({
                    "type": "data-ask",
                    "id": tool_id,
                    "data": {
                        "interruptId": tool_id,
                        "question": question,
                        "agent": agent,
                    },
                })

            elif name == "run_subagent":
                agent_name = tool_input.get("name", "subagent")
                stage = _STAGE_OF.get(agent_name, agent_name)
                # Stage start
                parts.append({
                    "type": "data-stage",
                    "id": _gen_id(),
                    "data": {"stage": stage, "status": "start", "agent": agent_name},
                })
                # Agent result as data-agent
                if result_text:
                    parts.append({
                        "type": "data-agent",
                        "id": _gen_id(),
                        "data": {
                            "agent": agent_name,
                            "name": agent_name,
                            "stage": stage,
                            "parts": [{"type": "text", "text": result_text, "state": "done"}],
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
