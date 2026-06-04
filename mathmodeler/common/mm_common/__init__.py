"""mm_common — MathModeler shared library.

Modules (per docs/tech-design.md §2):
  config    env/constants
  llm       Strands BedrockModel factory (build_agent) + generate() wrapper
  prompts   verbatim reference prompt templates + per-agent SYSTEM prompts
  embedding Bedrock Nova MME scorer (replaces mGTE)
  hmml      MethodScorer + MethodRetriever (faithful parent/child weighting)
  s3_io     document-bus put/get
  memory    AgentCore Memory client wrapper
  invoke    InvokeAgentRuntime (sub-agent calls + SSE parsing)
  dag       Kahn topological sort + linear fallback
  schemas   pydantic request/response/artifact schemas
  server    FastAPI skeleton (/ping + /invocations + SSE)
  tools     @tool factories for agents-as-tools
"""

__version__ = "0.1.0"
