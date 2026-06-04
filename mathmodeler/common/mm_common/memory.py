"""mm_common.memory — AgentCore Memory client wrapper (tech-design §2.6).

Short-term: per-session subtask conclusions (replacing coordinator.memory /
code_memory). Long-term: cross-session user preferences (semantic retrieval).
The exact SDK call names (``create_event`` / ``list_events`` /
``retrieve_memory_records``) are pinned at deploy time.
"""
from __future__ import annotations

from typing import List

from . import config

_client = None


def _get_client():
    global _client
    if _client is None:
        import boto3

        _client = boto3.client("bedrock-agentcore", region_name=config.REGION)
    return _client


def save_event(session_id: str, actor_id: str, role: str, text: str) -> None:
    """Persist a short-term session event (subtask conclusion / progress note)."""
    _get_client().create_event(
        memoryId=config.MEMORY_ID,
        actorId=actor_id,
        sessionId=session_id,
        payload=[{"role": role, "content": text}],
    )


def list_events(session_id: str, actor_id: str, max_results: int = 50) -> List[dict]:
    resp = _get_client().list_events(
        memoryId=config.MEMORY_ID,
        actorId=actor_id,
        sessionId=session_id,
        maxResults=max_results,
    )
    return resp.get("events", [])


def retrieve(actor_id: str, query: str, namespace: str = "preferences",
             top_k: int = 5) -> List[str]:
    """Semantic long-term retrieval (cross-session user preferences)."""
    resp = _get_client().retrieve_memory_records(
        memoryId=config.MEMORY_ID,
        namespace=namespace,
        searchCriteria={"searchQuery": query, "topK": top_k},
    )
    return [r["content"]["text"] for r in resp.get("memoryRecordSummaries", [])]
