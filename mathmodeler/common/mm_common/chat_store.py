"""mm_common.chat_store — DynamoDB-backed UI chat history persistence.

Stores chat sessions and messages in DynamoDB for cross-browser, cross-device
access. Follows agent-craft's per-message storage pattern to avoid the 400KB
item size limit.

Table schema (single-table design):
  Session metadata:
    PK = "SESSION", SK = session_id
    Attributes: title, problem, created_at, updated_at

  Individual messages:
    PK = "MSG#{session_id}", SK = "{index:06d}"
    Attributes: role, parts (JSON), msg_id

Environment:
  CHAT_HISTORY_TABLE — DynamoDB table name (default: "MathModeler-ChatHistory")
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

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


def save_session_meta(session_id: str, problem: str = "", title: str | None = None) -> None:
    """Save only session metadata (title for sidebar). No message items."""
    if not session_id:
        return
    now = int(time.time())
    derived_title = title or (problem[:24] if problem else "新会话")
    try:
        _ddb().update_item(
            Key={"PK": "SESSION", "SK": session_id},
            UpdateExpression="SET updated_at = :ts"
                            ", #t = if_not_exists(#t, :title)"
                            ", problem = if_not_exists(problem, :prob)"
                            ", created_at = if_not_exists(created_at, :ts)",
            ExpressionAttributeNames={"#t": "title"},
            ExpressionAttributeValues={
                ":ts": now,
                ":title": derived_title,
                ":prob": (problem or "")[:500],
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[chat_store] save_session_meta failed for {session_id}: {e}")


def list_sessions(limit: int = 50) -> list[dict[str, Any]]:
    """List all sessions ordered by updated_at descending (most recent first)."""
    try:
        resp = _ddb().query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": "SESSION"},
            ProjectionExpression="SK, title, updated_at",
            Limit=limit,
        )
        items = resp.get("Items", [])
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




def delete_session(session_id: str) -> None:
    """Delete a session and all its messages."""
    if not session_id:
        return
    try:
        table = _ddb()
        # Delete session metadata
        table.delete_item(Key={"PK": "SESSION", "SK": session_id})
        # Delete all message items
        resp = table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": f"MSG#{session_id}"},
            ProjectionExpression="PK, SK",
        )
        with table.batch_writer() as batch:
            for item in resp.get("Items", []):
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
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
