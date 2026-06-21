"""mm_common.invoke — InvokeAgentRuntime helpers + SSE parsing (tech-design §2.7).

``invoke_agent`` aggregates the SSE stream and returns the final JSON event
(Orchestrator -> sub-agents). ``stream_agent`` yields each SSE ``data:`` payload
(portal Lambda -> Orchestrator).
"""
from __future__ import annotations

import json

from . import config

_client = None


def _get_client():
    global _client
    if _client is None:
        import boto3

        _client = boto3.client("bedrock-agentcore", region_name=config.REGION)
    return _client


def _iter_data_lines(response):
    """Yield decoded ``data:`` payloads from an SSE response stream."""
    for line in response["response"].iter_lines():
        if not line:
            continue
        if isinstance(line, bytes):
            if line.startswith(b"data: "):
                yield line[6:].decode()
        else:  # already a str
            if line.startswith("data: "):
                yield line[6:]


def invoke_agent(agent_arn: str, payload: dict, session_id: str) -> dict:
    """Synchronously invoke a sub-agent; return the final aggregated JSON event."""
    resp = _get_client().invoke_agent_runtime(
        agentRuntimeArn=agent_arn,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode(),
        contentType="application/json",
        accept="text/event-stream",
    )
    chunks = list(_iter_data_lines(resp))
    return json.loads(chunks[-1]) if chunks else {}


def stream_agent(agent_arn: str, payload: dict, session_id: str):
    """Generator: pass each SSE event payload through (portal -> Orchestrator)."""
    resp = _get_client().invoke_agent_runtime(
        agentRuntimeArn=agent_arn,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode(),
        contentType="application/json",
        accept="text/event-stream",
    )
    yield from _iter_data_lines(resp)


def runtime_command(agent_arn: str, session_id: str, command: str, timeout: int = 30) -> tuple[str, str, int]:
    """Execute a shell command in the runtime session via InvokeAgentRuntimeCommand.

    Returns (stdout, stderr, exit_code). Raises RuntimeError if the session
    is unavailable (e.g. timed out / not yet started).
    """
    resp = _get_client().invoke_agent_runtime_command(
        agentRuntimeArn=agent_arn,
        runtimeSessionId=session_id,
        qualifier="DEFAULT",
        contentType="application/json",
        accept="application/vnd.amazon.eventstream",
        body={"command": command, "timeout": timeout},
    )
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    exit_code = -1
    for event in resp.get("stream", []):
        chunk = event.get("chunk", {})
        delta = chunk.get("contentDelta", {})
        if delta.get("stdout"):
            stdout_parts.append(delta["stdout"])
        if delta.get("stderr"):
            stderr_parts.append(delta["stderr"])
        stop = chunk.get("contentStop", {})
        if "exitCode" in stop:
            exit_code = stop["exitCode"]
    return "".join(stdout_parts), "".join(stderr_parts), exit_code
