"""mm_common.s3_io — document-bus helpers (tech-design §2.5).

Replaces the reference implementation's ``output_dir/work_dir`` with an S3
document bus. All keys follow ``{S3_PREFIX}/{session_id}/{rel}`` (§4).

Local-dev fallback
------------------
When ``DOC_BUCKET`` is not configured (typical for local development), there is
no S3 bucket to write to and every ``put_*``/``get_*`` would otherwise fail with
a confusing ``ParamValidationError`` (``Bucket=None``). To make the full
four-stage pipeline runnable locally — so artifacts actually persist and the
Reporter can produce a report — we transparently fall back to a local directory
(``MM_LOCAL_ARTIFACT_DIR`` or ``./.local-artifacts``). The same ``{prefix}/{sid}/
{rel}`` layout is mirrored on disk, and ``presign`` returns a ``file://`` URL.

Every operation logs (INFO) which backend (s3 / local) handled it and the key,
so the backend logs show exactly where each artifact went.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import config

logger = logging.getLogger("mm.s3_io")

_client = None


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
def _use_local() -> bool:
    """True when there is no S3 bucket configured -> use the on-disk fallback."""
    return not config.DOC_BUCKET


def _local_root() -> Path:
    return Path(os.getenv("MM_LOCAL_ARTIFACT_DIR", ".local-artifacts")).resolve()


def _local_path(session_id: str, rel: str) -> Path:
    return _local_root() / config.S3_PREFIX / session_id / rel


def _get_client():
    global _client
    if _client is None:
        import boto3

        _client = boto3.client("s3", region_name=config.REGION)
    return _client


def _key(session_id: str, rel: str) -> str:
    return f"{config.S3_PREFIX}/{session_id}/{rel}"


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def put_json(session_id: str, rel: str, obj) -> str:
    key = _key(session_id, rel)
    body = json.dumps(obj, ensure_ascii=False)
    if _use_local():
        path = _local_path(session_id, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        logger.info("[s3_io] put_json LOCAL key=%s bytes=%d -> %s", key, len(body), path)
        return f"file://{path}"
    _get_client().put_object(
        Bucket=config.DOC_BUCKET,
        Key=key,
        Body=body.encode(),
        ContentType="application/json",
    )
    logger.info("[s3_io] put_json S3 s3://%s/%s bytes=%d", config.DOC_BUCKET, key, len(body))
    return f"s3://{config.DOC_BUCKET}/{key}"


def get_json(session_id: str, rel: str):
    key = _key(session_id, rel)
    if _use_local():
        path = _local_path(session_id, rel)
        if not path.exists():
            logger.info("[s3_io] get_json LOCAL MISS key=%s (%s)", key, path)
            return None
        logger.info("[s3_io] get_json LOCAL key=%s <- %s", key, path)
        return json.loads(path.read_text(encoding="utf-8"))
    resp = _get_client().get_object(Bucket=config.DOC_BUCKET, Key=key)
    logger.info("[s3_io] get_json S3 s3://%s/%s", config.DOC_BUCKET, key)
    return json.loads(resp["Body"].read())


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
def put_text(session_id: str, rel: str, text: str, content_type: str = "text/markdown") -> str:
    key = _key(session_id, rel)
    if _use_local():
        path = _local_path(session_id, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        logger.info("[s3_io] put_text LOCAL key=%s bytes=%d -> %s", key, len(text), path)
        return f"file://{path}"
    _get_client().put_object(
        Bucket=config.DOC_BUCKET,
        Key=key,
        Body=text.encode(),
        ContentType=content_type,
    )
    logger.info("[s3_io] put_text S3 s3://%s/%s bytes=%d", config.DOC_BUCKET, key, len(text))
    return f"s3://{config.DOC_BUCKET}/{key}"


def get_text(session_id: str, rel: str) -> str:
    key = _key(session_id, rel)
    if _use_local():
        path = _local_path(session_id, rel)
        if not path.exists():
            logger.info("[s3_io] get_text LOCAL MISS key=%s (%s)", key, path)
            return ""
        logger.info("[s3_io] get_text LOCAL key=%s <- %s", key, path)
        return path.read_text(encoding="utf-8")
    resp = _get_client().get_object(Bucket=config.DOC_BUCKET, Key=key)
    logger.info("[s3_io] get_text S3 s3://%s/%s", config.DOC_BUCKET, key)
    return resp["Body"].read().decode()


# ---------------------------------------------------------------------------
# Bytes
# ---------------------------------------------------------------------------
def put_bytes(session_id: str, rel: str, data: bytes, content_type: str) -> str:
    key = _key(session_id, rel)
    if _use_local():
        path = _local_path(session_id, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        logger.info("[s3_io] put_bytes LOCAL key=%s bytes=%d -> %s", key, len(data), path)
        return f"file://{path}"
    _get_client().put_object(
        Bucket=config.DOC_BUCKET, Key=key, Body=data, ContentType=content_type
    )
    logger.info("[s3_io] put_bytes S3 s3://%s/%s bytes=%d", config.DOC_BUCKET, key, len(data))
    return f"s3://{config.DOC_BUCKET}/{key}"


def get_bytes(session_id: str, rel: str) -> bytes:
    key = _key(session_id, rel)
    if _use_local():
        path = _local_path(session_id, rel)
        if not path.exists():
            logger.info("[s3_io] get_bytes LOCAL MISS key=%s (%s)", key, path)
            return b""
        return path.read_bytes()
    resp = _get_client().get_object(Bucket=config.DOC_BUCKET, Key=key)
    return resp["Body"].read()


def exists(session_id: str, rel: str) -> bool:
    key = _key(session_id, rel)
    if _use_local():
        return _local_path(session_id, rel).exists()
    from botocore.exceptions import ClientError

    try:
        _get_client().head_object(Bucket=config.DOC_BUCKET, Key=key)
        return True
    except ClientError:
        return False


def presign(session_id: str, rel: str, expires: int = 3600) -> str:
    """Generate a presigned GET URL (for report download).

    With the local fallback there is nothing to presign, so a ``file://`` URL to
    the on-disk artifact is returned instead.
    """
    key = _key(session_id, rel)
    if _use_local():
        path = _local_path(session_id, rel)
        logger.info("[s3_io] presign LOCAL key=%s -> file://%s", key, path)
        return f"file://{path}"
    url = _get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": config.DOC_BUCKET, "Key": key},
        ExpiresIn=expires,
    )
    logger.info("[s3_io] presign S3 s3://%s/%s", config.DOC_BUCKET, key)
    return url
