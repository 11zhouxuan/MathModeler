"""mm_common.llm — Strands Agent factory (tech-design §2.2, route ② core).

Each agent = Strands ``Agent(model=BedrockModel(claude-opus-4), system_prompt,
tools)``. This module provides ``build_agent()`` (tool-using agent factory) plus
a no-tools ``LLM.generate()`` convenience wrapper (preserving the original
signature) for pure-text scenarios such as HMML LLM scoring.

The ``strands`` imports are deferred so that AWS-free unit tests can monkeypatch
``make_model`` / ``build_agent`` without installing ``strands-agents`` or
touching Bedrock.
"""
from __future__ import annotations

from . import config


def make_model(temperature: float = 0.0, max_tokens: int = 16384):
    """Construct a Strands ``BedrockModel`` for the configured Claude model.

    Newer Claude Opus models (e.g. opus-4-8) reject the ``temperature`` inference
    parameter ("`temperature` is deprecated for this model"), so it is omitted
    here; ``temperature`` is kept in the signature for backward compatibility but
    intentionally not forwarded.
    """
    from botocore.config import Config as BotoConfig
    from strands.models import BedrockModel

    # Increase read timeout to avoid ReadTimeoutError on long tool executions
    # (e.g. save_analysis writing large analysis markdown). Default is 60s.
    boto_config = BotoConfig(
        read_timeout=300,
        connect_timeout=10,
        retries={"max_attempts": 2, "mode": "adaptive"},
    )

    return BedrockModel(
        model_id=config.MODEL_ID,
        region_name=config.REGION,
        max_tokens=max_tokens,
        boto_client_config=boto_config,
        additional_request_fields={
            "thinking": {"type": "disabled"},
        },
    )



def make_session_manager(session_id: str, agent_name: str):
    """Create an AgentCoreMemorySessionManager for persisting agent chat history.

    Each agent gets its own memory session: "{session_id}_{agent_name}".
    Returns None if MEMORY_ID is not configured (local dev).
    """
    if not config.MEMORY_ID:
        return None
    try:
        from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
        from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig

        memory_config = AgentCoreMemoryConfig(
            memory_id=config.MEMORY_ID,
            session_id=f"{session_id}_{agent_name}",
            actor_id="system",
            async_mode=True,
        )
        return AgentCoreMemorySessionManager(
            agentcore_memory_config=memory_config,
            region_name=config.REGION,
        )
    except Exception:
        return None


def build_agent(system_prompt: str, tools: list, temperature: float = 0.0,
                max_tokens: int = 16384, *, streaming: bool = True,
                session_manager=None):
    """Construct a tool-using Strands Agent (agents-as-tools).

    ``tools`` is a list of ``@tool``-decorated functions.

    ``streaming=True`` (the default) sets ``callback_handler=None`` so the agent is
    driven via ``agent.stream_async()`` (the Supervisor path) without the default
    stdout PrintingCallbackHandler interfering. Set ``streaming=False`` to keep the
    legacy synchronous ``agent(task)`` behaviour with the default callback handler.
    """
    from strands import Agent

    kwargs: dict = {
        "model": make_model(temperature, max_tokens),
        "system_prompt": system_prompt,
        "tools": tools,
    }
    if streaming:
        kwargs["callback_handler"] = None
    if session_manager is not None:
        kwargs["session_manager"] = session_manager
    return Agent(**kwargs)



class LLM:
    """No-tools convenience wrapper preserving the original ``generate()`` API."""

    def __init__(self, temperature: float = 0.0, max_tokens: int = 8192):
        self.model = make_model(temperature, max_tokens)
        self._usage = {"input_tokens": 0, "output_tokens": 0}

    def generate(self, prompt: str, system: str | None = None) -> str:
        from strands import Agent

        agent = Agent(model=self.model, system_prompt=system or "")
        result = agent(prompt)
        self._accumulate_usage(result)
        return str(result)

    def _accumulate_usage(self, result) -> None:
        # Strands AgentResult exposes usage metrics; be tolerant of shape.
        try:
            usage = getattr(getattr(result, "metrics", None), "accumulated_usage", None)
            if usage:
                self._usage["input_tokens"] += int(usage.get("inputTokens", 0))
                self._usage["output_tokens"] += int(usage.get("outputTokens", 0))
        except Exception:
            pass

    def get_total_usage(self) -> dict:
        return dict(self._usage)

    def clear_usage(self) -> None:
        self._usage = {"input_tokens": 0, "output_tokens": 0}
