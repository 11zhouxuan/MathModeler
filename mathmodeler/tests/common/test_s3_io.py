"""Tests for mm_common.s3_io (S3 document bus) — §10.1, mocked with moto."""
from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from mm_common import config, s3_io

BUCKET = "mm-test-bucket"
SID = "mm-" + "a" * 32  # >=33 chars


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("DOC_BUCKET", BUCKET)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    config.reload()
    with mock_aws():
        client = boto3.client("s3", region_name="us-west-2")
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        # reset cached client so it picks up the mocked session
        s3_io._client = None
        yield client
    s3_io._client = None


def test_key_convention(s3):
    assert s3_io._key(SID, "analysis/x.md") == f"mathmodeler/{SID}/analysis/x.md"


def test_put_get_json_roundtrip(s3):
    uri = s3_io.put_json(SID, "analysis/task.json", {"a": 1, "中文": "ok"})
    assert uri == f"s3://{BUCKET}/mathmodeler/{SID}/analysis/task.json"
    assert s3_io.get_json(SID, "analysis/task.json") == {"a": 1, "中文": "ok"}


def test_put_get_text_roundtrip(s3):
    s3_io.put_text(SID, "report/report.md", "# Hello")
    assert s3_io.get_text(SID, "report/report.md") == "# Hello"


def test_put_get_bytes_roundtrip(s3):
    s3_io.put_bytes(SID, "img/x.png", b"\x89PNG", "image/png")
    assert s3_io.get_bytes(SID, "img/x.png") == b"\x89PNG"


def test_exists(s3):
    assert s3_io.exists(SID, "missing.txt") is False
    s3_io.put_text(SID, "there.txt", "x")
    assert s3_io.exists(SID, "there.txt") is True


def test_presign_returns_url(s3):
    s3_io.put_text(SID, "report/report.md", "x")
    url = s3_io.presign(SID, "report/report.md")
    assert url.startswith("https://") and BUCKET in url
