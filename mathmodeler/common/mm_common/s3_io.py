"""mm_common.s3_io — S3 document-bus helpers (tech-design §2.5).

Replaces the reference implementation 's local ``output_dir/work_dir`` with an S3 document
bus. All keys follow ``{S3_PREFIX}/{session_id}/{rel}`` (§4).
"""
from __future__ import annotations

import json

from . import config

_client = None


def _get_client():
    global _client
    if _client is None:
        import boto3

        _client = boto3.client("s3", region_name=config.REGION)
    return _client


def _key(session_id: str, rel: str) -> str:
    return f"{config.S3_PREFIX}/{session_id}/{rel}"


def put_json(session_id: str, rel: str, obj) -> str:
    key = _key(session_id, rel)
    _get_client().put_object(
        Bucket=config.DOC_BUCKET,
        Key=key,
        Body=json.dumps(obj, ensure_ascii=False).encode(),
        ContentType="application/json",
    )
    return f"s3://{config.DOC_BUCKET}/{key}"


def get_json(session_id: str, rel: str):
    key = _key(session_id, rel)
    resp = _get_client().get_object(Bucket=config.DOC_BUCKET, Key=key)
    return json.loads(resp["Body"].read())


def put_text(session_id: str, rel: str, text: str, content_type: str = "text/markdown") -> str:
    key = _key(session_id, rel)
    _get_client().put_object(
        Bucket=config.DOC_BUCKET,
        Key=key,
        Body=text.encode(),
        ContentType=content_type,
    )
    return f"s3://{config.DOC_BUCKET}/{key}"


def get_text(session_id: str, rel: str) -> str:
    key = _key(session_id, rel)
    resp = _get_client().get_object(Bucket=config.DOC_BUCKET, Key=key)
    return resp["Body"].read().decode()


def put_bytes(session_id: str, rel: str, data: bytes, content_type: str) -> str:
    key = _key(session_id, rel)
    _get_client().put_object(
        Bucket=config.DOC_BUCKET, Key=key, Body=data, ContentType=content_type
    )
    return f"s3://{config.DOC_BUCKET}/{key}"


def get_bytes(session_id: str, rel: str) -> bytes:
    key = _key(session_id, rel)
    resp = _get_client().get_object(Bucket=config.DOC_BUCKET, Key=key)
    return resp["Body"].read()


def exists(session_id: str, rel: str) -> bool:
    from botocore.exceptions import ClientError

    key = _key(session_id, rel)
    try:
        _get_client().head_object(Bucket=config.DOC_BUCKET, Key=key)
        return True
    except ClientError:
        return False


def presign(session_id: str, rel: str, expires: int = 3600) -> str:
    """Generate a presigned GET URL (for report download)."""
    key = _key(session_id, rel)
    return _get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": config.DOC_BUCKET, "Key": key},
        ExpiresIn=expires,
    )
