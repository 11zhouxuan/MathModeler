"""§10.1 config.py — env parsing & defaults."""
import mm_common.config as config


def test_defaults(monkeypatch):
    for k in [
        "AWS_REGION", "MODEL_ID", "DOC_BUCKET", "MEMORY_ID", "S3_PREFIX",
        "EMBED_REGION", "EMBED_MODEL_ID", "EMBED_DIM",
        "HMML_TOP_K", "HMML_PARENT_WEIGHT", "HMML_CHILD_WEIGHT",
        "ACTOR_CRITIC_ROUNDS", "SOLVER_MAX_RETRIES",
    ]:
        monkeypatch.delenv(k, raising=False)
    config.reload()

    assert config.REGION == "us-west-2"
    assert config.MODEL_ID == "us.anthropic.claude-opus-4-8"


    assert config.S3_PREFIX == "mathmodeler"
    assert config.EMBED_REGION == "us-east-1"
    assert config.EMBED_MODEL_ID == "amazon.nova-2-multimodal-embeddings-v1:0"
    assert config.EMBED_DIM == 1024
    assert config.HMML_TOP_K == 6
    assert config.HMML_PARENT_WEIGHT == 0.5
    assert config.HMML_CHILD_WEIGHT == 0.5
    assert config.ACTOR_CRITIC_ROUNDS == 1
    assert config.SOLVER_MAX_RETRIES == 3


def test_overrides(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("HMML_TOP_K", "10")
    monkeypatch.setenv("HMML_PARENT_WEIGHT", "0.3")
    monkeypatch.setenv("SOLVER_MAX_RETRIES", "5")
    monkeypatch.setenv("DOC_BUCKET", "my-bucket")
    config.reload()
    try:
        assert config.REGION == "eu-west-1"
        assert config.HMML_TOP_K == 10
        assert config.HMML_PARENT_WEIGHT == 0.3
        assert config.SOLVER_MAX_RETRIES == 5
        assert config.DOC_BUCKET == "my-bucket"
    finally:
        for k in ["AWS_REGION", "HMML_TOP_K", "HMML_PARENT_WEIGHT",
                  "SOLVER_MAX_RETRIES", "DOC_BUCKET"]:
            monkeypatch.delenv(k, raising=False)
        config.reload()
