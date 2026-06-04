"""mm_common.state_store — persist Supervisor state across HTTP requests (§6).

HITL ``ask_user`` pauses one request and resumes in a *later* request, so the
Supervisor's state (per-agent messages + interrupt_state, the pending ask->child
map, and the completed-stage cache) must outlive a single Runtime invocation.

``StateStore`` is the abstract contract; :class:`S3StateStore` writes one JSON blob
per session to ``s3://DOC_BUCKET/<S3_PREFIX>/<session_id>/supervisor_state.json``
(reusing the existing doc bucket). :class:`MemoryStateStore` is an in-process
fallback for unit tests / single-process runs.

The blob is a plain JSON dict produced by :meth:`Supervisor.serialize` and consumed
by :meth:`Supervisor.restore` — this module is agnostic to its shape.
"""
from __future__ import annotations

import json
from typing import Optional, Protocol

from . import config


class StateStore(Protocol):
    def save(self, session_id: str, data: dict) -> None: ...
    def load(self, session_id: str) -> Optional[dict]: ...


class MemoryStateStore:
    """In-process dict-backed store (tests / single-process)."""

    def __init__(self) -> None:
        self._d: dict[str, dict] = {}

    def save(self, session_id: str, data: dict) -> None:
        self._d[session_id] = json.loads(json.dumps(data, default=str))

    def load(self, session_id: str) -> Optional[dict]:
        return self._d.get(session_id)


class S3StateStore:
    """Persist supervisor state as a JSON object in the doc bucket.

    Key: ``<S3_PREFIX>/<session_id>/supervisor_state.json``. ``boto3`` is imported
    lazily so AWS-free unit tests can use :class:`MemoryStateStore` instead.
    """

    FILENAME = "supervisor_state.json"

    def __init__(self, bucket: str | None = None, region: str | None = None):
        self.bucket = bucket or config.DOC_BUCKET
        self.region = region or config.REGION
        self._client = None

    def _s3(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("s3", region_name=self.region)
        return self._client

    def _key(self, session_id: str) -> str:
        return f"{config.S3_PREFIX}/{session_id}/{self.FILENAME}"

    def save(self, session_id: str, data: dict) -> None:
        if not self.bucket:
            raise RuntimeError("S3StateStore requires DOC_BUCKET to be configured")
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._s3().put_object(
            Bucket=self.bucket, Key=self._key(session_id), Body=body,
            ContentType="application/json",
        )

    def load(self, session_id: str) -> Optional[dict]:
        if not self.bucket:
            return None
        try:
            obj = self._s3().get_object(Bucket=self.bucket, Key=self._key(session_id))
            return json.loads(obj["Body"].read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - missing key / first turn -> no prior state
            return None
