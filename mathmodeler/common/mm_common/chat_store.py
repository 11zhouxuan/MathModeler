"""mm_common.chat_store — DynamoDB-backed UI chat history persistence.

Stores chat sessions and messages in DynamoDB for cross-browser, cross-device
access. Inspired by agent-craft's ChatHistoryService but simplified for the
single-user MathModeler portal.

Table schema (single-table design):
  PK = "SESSION"
  SK = session_id
  Attributes: title, problem, created_at, updated_at, messages (JSON list)

Environment:
  CHAT_HISTORY_TABLE — DynamoDB table name (default: "MathModeler-ChatHistory")
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from . import config

logger = logging.getLogger("mm.chat_store")

_TABLE_NAME: str | None = None
_client = None


def _table_name() -> str:
    global _TABLE_NAME
    if _TABLE_NAME is None:
        import os
        _TABLE_NAME = os.environ.get("CHAT_HISTORY_TABLE", "MathModeler-ChatHistory")
    return _TABLE_NAME


def _ddb():
    """Lazy boto3 DynamoDB resource.Table (reuse across calls)."""
    global _client
    if _client is None:
        import boto3
        region = config.REGION
        dynamodb = boto3.resource("dynamodb", region_name=region)
        _client = dynamodb.Table(_table_name())
    return _client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_session(
    session_id: str,
    messages: list[dict[str, Any]],
    problem: str = "",
    title: str | None = None,
) -> None:
    """Upsert a chat session with its full message list.

    Called:
      1. By the portal backend after streaming completes (server-side save).
      2. By the frontend POST /api/sessions/:id/messages (client-side save).
    """
    if not session_id:
        return
    now = int(time.time())
    derived_title = title or (problem[:24] if problem else "新会话")

    try:
        _ddb().put_item(Item={
            "PK": "SESSION",
            "SK": session_id,
            "title": derived_title,
            "problem": (problem or "")[:500],
            "created_at": now,  # Will be overwritten on update; acceptable for simplicity
            "updated_at": now,
            "messages": json.dumps(messages, ensure_ascii=False, default=str),
        })
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[chat_store] save_session failed for {session_id}: {e}")


def update_session_timestamp(session_id: str, title: str | None = None) -> None:
    """Touch the updated_at timestamp (called during streaming for liveness)."""
    if not session_id:
        return
    now = int(time.time())
    try:
        expr = "SET updated_at = :ts"
        vals: dict = {":ts": now}
        if title:
            expr += ", title = :t"
            vals[":t"] = title
        _ddb().update_item(
            Key={"PK": "SESSION", "SK": session_id},
            UpdateExpression=expr,
            ExpressionAttributeValues=vals,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[chat_store] update_session_timestamp failed: {e}")


def list_sessions(limit: int = 50) -> list[dict[str, Any]]:
    """List all sessions ordered by updated_at descending (most recent first).

    Returns a list of {id, title, updatedAt} dicts for the sidebar.
    """
    try:
        resp = _ddb().query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": "SESSION"},
            ProjectionExpression="SK, title, updated_at",
            ScanIndexForward=False,  # DDB sorts by SK; we'll sort client-side
            Limit=limit,
        )
        items = resp.get("Items", [])
        # Sort by updated_at descending (SK is session_id, not sortable by time)
        items.sort(key=lambda x: int(x.get("updated_at", 0)), reverse=True)
        return [
            {
                "id": item["SK"],
                "title": item.get("title", "新会话"),
                "updatedAt": int(item.get("updated_at", 0)),
            }
            for item in items
        ]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[chat_store] list_sessions failed: {e}")
        return []


def load_messages(session_id: str) -> list[dict[str, Any]]:
    """Load the full messages array for a session."""
    if not session_id:
        return []
    try:
        resp = _ddb().get_item(
            Key={"PK": "SESSION", "SK": session_id},
            ProjectionExpression="messages",
        )
        item = resp.get("Item")
        if not item:
            return []
        raw = item.get("messages", "[]")
        if isinstance(raw, str):
            return json.loads(raw)
        return raw if isinstance(raw, list) else []
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[chat_store] load_messages failed for {session_id}: {e}")
        return []


def delete_session(session_id: str) -> None:
    """Delete a session from the table."""
    if not session_id:
        return
    try:
        _ddb().delete_item(Key={"PK": "SESSION", "SK": session_id})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[chat_store] delete_session failed for {session_id}: {e}")


def session_exists(session_id: str) -> bool:
    """Check if a session record exists (lightweight)."""
    if not session_id:
        return False
    try:
        resp = _ddb().get_item(
            Key={"PK": "SESSION", "SK": session_id},
            ProjectionExpression="SK",
        )
        return "Item" in resp
    except Exception:  # noqa: BLE001
        return False
