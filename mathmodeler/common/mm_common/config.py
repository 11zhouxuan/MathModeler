"""mm_common.config — central env/constants (tech-design §2.1).

Values are resolved from environment variables with the faithful reference
defaults. Because some unit tests mutate the environment (``monkeypatch.setenv``)
*after* import, the public names are recomputed by :func:`reload`, and callers
that need always-fresh values can use the ``get_*`` helpers.
"""
from __future__ import annotations

import os

# --- region / models -------------------------------------------------------
# Claude Opus must be invoked via a cross-region inference profile (the bare
# on-demand foundation-model id is NOT supported for ConverseStream), so the
# default uses the ``us.`` inference-profile id.
_DEFAULT_MODEL_ID = "us.anthropic.claude-opus-4-8"


# Nova Multimodal Embeddings is only available in us-east-1 -> cross-region call.
_DEFAULT_EMBED_MODEL_ID = "amazon.nova-2-multimodal-embeddings-v1:0"


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


# Module-level constants (snapshot at import). Use reload() in tests after setenv.
REGION: str
MODEL_ID: str
DOC_BUCKET: str | None
MEMORY_ID: str | None
S3_PREFIX: str
EMBED_REGION: str
EMBED_MODEL_ID: str
EMBED_DIM: int
ANALYST_ARN: str | None
MODELER_ARN: str | None
SOLVER_ARN: str | None
REPORTER_ARN: str | None
HMML_TOP_K: int
HMML_PARENT_WEIGHT: float
HMML_CHILD_WEIGHT: float
ACTOR_CRITIC_ROUNDS: int
SOLVER_MAX_RETRIES: int
CI_SESSION_TIMEOUT_SECONDS: int
CI_HEARTBEAT_SECONDS: int
INPROCESS: bool

ORCHESTRATION: str



def reload() -> None:
    """(Re)resolve all module-level constants from the current environment."""
    g = globals()
    g["REGION"] = os.getenv("AWS_REGION", "us-west-2")
    g["MODEL_ID"] = os.getenv("MODEL_ID", _DEFAULT_MODEL_ID)
    g["DOC_BUCKET"] = os.getenv("DOC_BUCKET")
    g["MEMORY_ID"] = os.getenv("MEMORY_ID")
    g["S3_PREFIX"] = os.getenv("S3_PREFIX", "mathmodeler")

    # Embedding (Nova MME, cross-region us-east-1)
    g["EMBED_REGION"] = os.getenv("EMBED_REGION", "us-east-1")
    g["EMBED_MODEL_ID"] = os.getenv("EMBED_MODEL_ID", _DEFAULT_EMBED_MODEL_ID)
    g["EMBED_DIM"] = _int("EMBED_DIM", 1024)

    # Sub-agent runtime ARNs (CDK injects into the Orchestrator container)
    g["ANALYST_ARN"] = os.getenv("ANALYST_ARN")
    g["MODELER_ARN"] = os.getenv("MODELER_ARN")
    g["SOLVER_ARN"] = os.getenv("SOLVER_ARN")
    g["REPORTER_ARN"] = os.getenv("REPORTER_ARN")

    # Faithful reference defaults
    g["HMML_TOP_K"] = _int("HMML_TOP_K", 6)
    g["HMML_PARENT_WEIGHT"] = _float("HMML_PARENT_WEIGHT", 0.5)
    g["HMML_CHILD_WEIGHT"] = _float("HMML_CHILD_WEIGHT", 0.5)
    g["ACTOR_CRITIC_ROUNDS"] = _int("ACTOR_CRITIC_ROUNDS", 1)
    g["SOLVER_MAX_RETRIES"] = _int("SOLVER_MAX_RETRIES", 3)
    # Code Interpreter session lifetime + idle keep-alive heartbeat.
    # AgentCore CI allows up to 8h (28800s) max session lifetime, but reclaims a
    # session idle >~15min; a periodic lightweight keep-alive keeps it warm.
    g["CI_SESSION_TIMEOUT_SECONDS"] = _int("CI_SESSION_TIMEOUT_SECONDS", 28800)
    g["CI_HEARTBEAT_SECONDS"] = _int("CI_HEARTBEAT_SECONDS", 300)
    # Max time a single execute_code call may run before being declared timed-out.
    # Prevents long-running scripts from blocking the SSE stream indefinitely.
    g["CI_EXECUTE_TIMEOUT_SECONDS"] = _int("CI_EXECUTE_TIMEOUT_SECONDS", 300)


    # Single-Runtime agents-as-tools mode (§5/§8): when true, the Orchestrator's
    # invoke_* tools call the sub-agents IN-PROCESS (mm_common.runners) instead
    # of crossing the network via InvokeAgentRuntime. Defaults to in-process for
    # the merged single-Runtime image; set MM_INPROCESS=0 to use the legacy
    # 5-Runtime cross-network mode.
    g["INPROCESS"] = os.getenv("MM_INPROCESS", "1") not in ("0", "false", "False", "")
    # Orchestration strategy: "supervisor" = LLM free-routing agents-as-tools
    # (primary), "pipeline" = deterministic four-stage fallback.
    g["ORCHESTRATION"] = os.getenv("MM_ORCHESTRATION", "supervisor")


reload()

