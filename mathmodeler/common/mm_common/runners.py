"""mm_common.runners — process-local sub-agent runners (agents-as-tools §8.1).

These ``run_*`` functions contain the *exact* logic each sub-agent previously
had inside its own ``agents/<name>/app.py:handle(body)``: build the Strands
``Agent`` (system prompt + ``*_tools``), drive it with the task instruction, and
deterministically reconstruct the §3 response by reading the artifacts the agent
wrote to S3.

Both call sites share this single implementation (zero behaviour drift):
  * the per-sub-agent Runtime entrypoints (legacy 5-Runtime mode), and
  * the Orchestrator's in-process ``invoke_*`` tools (single-Runtime
    agents-as-tools mode), which call these directly instead of crossing the
    network via ``InvokeAgentRuntime``.

The Modeler retriever and Solver Code Interpreter are injected/created here so a
single merged image can run all four. ``build_agent`` is looked up via the
module attribute (``llm.build_agent``) so tests can monkeypatch it.
"""
from __future__ import annotations

from . import config, llm, s3_io
from .hmml import MethodRetriever
from .prompts import (
    ANALYST_SYSTEM,
    MODELER_SYSTEM,
    REPORTER_SYSTEM,
    SOLVER_SYSTEM,
)
from .tools import analyst_tools, modeler_tools, reporter_tools, solver_tools

# ---------------------------------------------------------------------------
# Lazily-constructed, process-global Modeler retriever (HMML.json loaded once).
# Kept module-level so a single Runtime reuses one retriever across subtasks.
# ---------------------------------------------------------------------------
_RETRIEVER: MethodRetriever | None = None


def get_retriever() -> MethodRetriever:
    global _RETRIEVER
    if _RETRIEVER is None:
        _RETRIEVER = MethodRetriever(rag=True)
    return _RETRIEVER


# ---------------------------------------------------------------------------
# Analyst
# ---------------------------------------------------------------------------
def _analyst_extract(session_id: str) -> dict:
    tasks = []
    try:
        tasks = s3_io.get_json(session_id, "analysis/task_descriptions.json") or []
    except Exception:  # noqa: BLE001
        tasks = []
    dag_graph = {}
    try:
        dag_graph = s3_io.get_json(session_id, "analysis/dag.json") or {}
    except Exception:  # noqa: BLE001
        dag_graph = {}
    order: list[str] = []
    if dag_graph:
        try:
            from . import dag as _dag

            order = _dag.compute_dag_order(dag_graph)
        except Exception:  # noqa: BLE001
            order = list(dag_graph.keys())
    if not order:
        order = [str(t.get("id")) for t in tasks if t.get("id") is not None]
    return {
        "problem_analysis_key": s3_io._key(session_id, "analysis/problem_analysis.md"),
        "task_descriptions_key": s3_io._key(session_id, "analysis/task_descriptions.json"),
        "dag_key": s3_io._key(session_id, "analysis/dag.json"),
        "order": order,
        "tasknum": len(order),
    }


def build_analyst_agent(session_id: str):
    """Construct the Analyst Strands Agent (streaming-ready, for the Supervisor)."""
    return llm.build_agent(ANALYST_SYSTEM, analyst_tools(session_id))


def run_analyst(body: dict) -> dict:
    session_id = body["session_id"]
    problem = body["problem"]
    agent = llm.build_agent(ANALYST_SYSTEM, analyst_tools(session_id), streaming=False)

    task = (
        f"session_id={session_id}\n"
        "请分析并分解以下数学建模问题，理解题意后拆分为有依赖关系的子任务，"
        "并调用 build_dag 构建依赖 DAG 得到拓扑执行顺序。\n\n"
        f"问题：\n{problem}"
    )
    try:
        agent(task)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), **_analyst_extract(session_id)}
    return {"ok": True, **_analyst_extract(session_id)}


# ---------------------------------------------------------------------------
# Modeler (HMML retrieval + actor->critic self-evaluation)
# ---------------------------------------------------------------------------
_MODELER_SYSTEM = MODELER_SYSTEM.format(ACTOR_CRITIC_ROUNDS=config.ACTOR_CRITIC_ROUNDS)


def _modeler_extract(session_id: str, task_id: str) -> dict:
    payload = {}
    try:
        payload = s3_io.get_json(session_id, f"modeling/{task_id}.json") or {}
    except Exception:  # noqa: BLE001
        payload = {}
    return {
        "modeling_key": s3_io._key(session_id, f"modeling/{task_id}.json"),
        "task_modeling_method": payload.get("task_modeling_method", ""),
        "retrieved_methods": payload.get("retrieved_methods", []),
        "task_analysis": payload.get("task_analysis", ""),
        "task_modeling_formulas": payload.get("task_modeling_formulas", ""),
        "critic_rounds": payload.get("critic_rounds", []),
    }


def build_modeler_agent(session_id: str):
    """Construct the Modeler Strands Agent (streaming-ready, for the Supervisor)."""
    return llm.build_agent(_MODELER_SYSTEM, modeler_tools(session_id, get_retriever()))


def run_modeler(body: dict) -> dict:
    session_id = body["session_id"]
    task_id = body["task_id"]
    problem = body["problem"]
    task_description = body.get("task_description", "")
    agent = llm.build_agent(_MODELER_SYSTEM, modeler_tools(session_id, get_retriever()),
                            streaming=False)

    task = (
        f"session_id={session_id} task_id={task_id}\n"
        "请先调用 retrieve_hmml_methods 检索候选建模方法并选定方法、推导初始公式；"
        "随后调用 critique_modeling 对公式自评估并据评审改进（actor->critic 自精炼），"
        "最后调用 save_modeling 保存结果（可在 payload.critic_rounds 记录每轮评审）。\n\n"
        f"问题：{problem}\n子任务：{task_description}"
    )
    try:
        agent(task)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), **_modeler_extract(session_id, task_id)}
    return {"ok": True, **_modeler_extract(session_id, task_id)}


# ---------------------------------------------------------------------------
# Solver (Code Interpreter sandbox + self-repair retry)
# ---------------------------------------------------------------------------
class CodeInterpreterClient:
    """Adapter exposing ``.execute(code) -> {ok, stdout, stderr, artifacts}``.

    Wraps the AgentCore Code Interpreter SDK. The concrete SDK class/method
    names are resolved defensively so unit tests can inject a fake with the
    same ``execute`` contract without importing ``bedrock_agentcore``.
    """

    def __init__(self, region: str | None = None):
        self.region = region or config.REGION
        self._ci = None

    def start(self) -> None:
        from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

        self._ci = CodeInterpreter(region=self.region)
        self._ci.start()

    def execute(self, code: str) -> dict:
        if self._ci is None:
            raise RuntimeError("Code Interpreter session not started")
        result = self._ci.invoke("executeCode", {"language": "python", "code": code})
        stdout, stderr, artifacts = _parse_ci_result(result)
        return {
            "ok": stderr == "",
            "stdout": stdout,
            "stderr": stderr,
            "artifacts": artifacts,
        }

    def stop(self) -> None:
        if self._ci is not None:
            try:
                self._ci.stop()
            except Exception:  # noqa: BLE001
                pass
            self._ci = None


def _parse_ci_result(result) -> tuple[str, str, list]:
    """Best-effort extraction of (stdout, stderr, artifacts) from an SDK result."""
    stdout_parts: list[str] = []
    stderr = ""
    artifacts: list = []
    try:
        events = result.get("stream") if isinstance(result, dict) else result
        for event in events or []:
            res = event.get("result", event) if isinstance(event, dict) else {}
            if res.get("isError"):
                stderr = str(res.get("content") or res)
            for block in res.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    stdout_parts.append(block.get("text", ""))
    except Exception:  # noqa: BLE001
        stdout_parts.append(str(result))
    return "".join(stdout_parts), stderr, artifacts


def _solver_extract(session_id: str, task_id: str) -> dict:
    res = {}
    try:
        res = s3_io.get_json(session_id, f"solving/{task_id}.json") or {}
    except Exception:  # noqa: BLE001
        res = {}
    stdout = res.get("stdout", "")
    return {
        "success": bool(res.get("success", False)),
        "attempts": int(res.get("attempts", 0)),
        "code_key": s3_io._key(session_id, f"solving/{task_id}.py"),
        "result_key": s3_io._key(session_id, f"solving/{task_id}.json"),
        "artifacts": res.get("artifacts", []),
        "stdout_tail": stdout[-2000:] if stdout else "",
        "error": res.get("error"),
    }


def build_solver_agent(session_id: str, ci=None):
    """Construct the Solver Strands Agent (streaming-ready, for the Supervisor).

    A live, started :class:`CodeInterpreterClient` is created if not supplied and
    attached as ``agent._ci`` so the caller (Supervisor wiring) can stop it on
    teardown. The sandbox session is kept open for the agent's lifetime.
    """
    if ci is None:
        ci = CodeInterpreterClient(region=config.REGION)
        ci.start()
    agent = llm.build_agent(SOLVER_SYSTEM, solver_tools(session_id, ci))
    agent._ci = ci  # type: ignore[attr-defined]
    return agent


def run_solver(body: dict) -> dict:
    session_id = body["session_id"]
    task_id = body["task_id"]
    problem = body["problem"]
    max_retries = body.get("max_retries", config.SOLVER_MAX_RETRIES)

    ci = CodeInterpreterClient(region=config.REGION)
    ci.start()
    try:
        agent = llm.build_agent(SOLVER_SYSTEM, solver_tools(session_id, ci), streaming=False)

        task = (
            f"session_id={session_id} task_id={task_id}\n"
            f"max_retries={max_retries}\n"
            "请读取该子任务的建模结果与依赖产物，生成 Python 代码，调用 execute_code 在沙盒运行；"
            "失败则根据 stderr 自我修复并重试（不超过 max_retries），"
            "最终调用 save_code 与 save_result 保存代码与结果。\n\n"
            f"问题：{problem}"
        )
        try:
            agent(task)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), **_solver_extract(session_id, task_id)}
    finally:
        ci.stop()
    return {"ok": True, **_solver_extract(session_id, task_id)}


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------
def _reporter_extract(session_id: str) -> dict:
    report_key = s3_io._key(session_id, "report/report.md")
    try:
        report_url = s3_io.presign(session_id, "report/report.md")
    except Exception:  # noqa: BLE001
        report_url = ""
    return {"report_key": report_key, "report_url": report_url}


def build_reporter_agent(session_id: str):
    """Construct the Reporter Strands Agent (streaming-ready, for the Supervisor)."""
    return llm.build_agent(REPORTER_SYSTEM, reporter_tools(session_id))


def run_reporter(body: dict) -> dict:
    session_id = body["session_id"]
    problem = body["problem"]
    order = body.get("order", [])
    agent = llm.build_agent(REPORTER_SYSTEM, reporter_tools(session_id), streaming=False)

    task = (
        f"session_id={session_id} order={order}\n"
        "请逐个子任务读取分析/建模/求解产物，组织为：标题、摘要、各子任务"
        "（方法/公式/结果/图表）、结论，输出可读的 Markdown（含 KaTeX 公式），"
        "最后调用 save_report 保存。\n\n"
        f"问题：{problem}"
    )
    try:
        agent(task)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), **_reporter_extract(session_id)}
    return {"ok": True, **_reporter_extract(session_id)}
