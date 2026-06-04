"""mm_common.tools — per-agent @tool factories (tech-design §2.10, route ② core).

Each agent is a Strands ``Agent(system_prompt, tools)``. The reusable ``@tool``s
are defined here as factory functions that close over ``session_id`` /
``task_id`` and are then handed to ``build_agent``. The original deterministic
flow (actor-critic / debug-retry / four-stage ordering) lives in each agent's
system prompt; the tools are deterministic Python returning structured results
(including ``{ok:false,error}``) so the LLM can self-correct.

``strands.tool`` is imported defensively: when ``strands-agents`` is not
installed (AWS-free unit tests), a no-op decorator is used so the factory
functions can still be imported and their wiring (count / names / args)
asserted.
"""
from __future__ import annotations

try:  # pragma: no cover - exercised indirectly
    from strands import tool
except Exception:  # strands-agents not installed (unit-test environment)
    def tool(func=None, **_kwargs):
        """Fallback no-op decorator preserving the wrapped function."""
        if func is None:
            def _wrap(f):
                return f
            return _wrap
        return func

from . import config, dag, events, invoke, memory, s3_io



# ---------------------------------------------------------------------------
# Analyst
# ---------------------------------------------------------------------------
def analyst_tools(session_id: str) -> list:
    @tool
    def describe_data(s3_key: str) -> str:
        """Read and summarise an uploaded data file (returns a plain-text summary)."""
        try:
            # s3_key is an absolute key under the bucket; read raw text best-effort
            rel = s3_key.split(f"{session_id}/", 1)[-1]
            text = s3_io.get_text(session_id, rel)
            return text[:4000]
        except Exception as e:  # noqa: BLE001
            return f"{{\"ok\": false, \"error\": \"{e}\"}}"

    @tool
    def save_analysis(markdown: str) -> str:
        """Save the finalised problem analysis to analysis/problem_analysis.md; return S3 key."""
        return s3_io.put_text(session_id, "analysis/problem_analysis.md", markdown)

    @tool
    def save_task_descriptions(tasks: list) -> str:
        """Save subtask decomposition [{id,title,description}] to analysis/task_descriptions.json."""
        return s3_io.put_json(session_id, "analysis/task_descriptions.json", tasks)

    @tool
    def build_dag(graph: dict, tasknum: int) -> dict:
        """Build a DAG from adjacency list {tid:[deps]}; Kahn topo-sort -> order.

        Falls back to linear dependencies on parse/cycle error. Returns
        {"dag":..., "order":[...]}.
        """
        try:
            order = dag.compute_dag_order(graph)
        except Exception:
            graph = dag.fallback_linear_dag(tasknum)
            order = dag.compute_dag_order(graph)
        s3_io.put_json(session_id, "analysis/dag.json", graph)
        return {"dag": graph, "order": order}

    return [describe_data, save_analysis, save_task_descriptions, build_dag]


# ---------------------------------------------------------------------------
# Modeler
# ---------------------------------------------------------------------------
def modeler_tools(session_id: str, retriever) -> list:
    @tool
    def retrieve_hmml_methods(description: str, top_k: int = config.HMML_TOP_K) -> str:
        """Semantic-retrieve top_k modeling methods from the full HMML library
        (Nova MME embedding + parent/child-weighted tree recursion)."""
        try:
            return retriever.retrieve_methods(description, top_k=top_k, method="embedding")
        except Exception as e:  # noqa: BLE001
            return f"{{\"ok\": false, \"error\": \"{e}\"}}"

    @tool
    def get_analysis() -> str:
        """Read the problem analysis at analysis/problem_analysis.md."""
        return s3_io.get_text(session_id, "analysis/problem_analysis.md")

    @tool
    def critique_modeling(task_description: str, task_analysis: str,
                          modeling_formulas: str) -> str:
        """Self-evaluation tool: ask an independent Mathematical Modeling Critic
        to critique the CURRENT modeling formulas (accuracy/rigor, innovation,
        applicability). Returns a plain-text critique highlighting weaknesses
        only — use it to drive an actor->critic refinement loop before saving."""
        from .llm import LLM
        from .prompts import FORMULAS_CRITIC_SYSTEM

        prompt = (
            f"# Task Description:\n{task_description}\n\n"
            f"# Task Analysis:\n{task_analysis}\n\n"
            f"# Current Task Modeling Formulas:\n{modeling_formulas}\n\n"
            "---\n\nCritique the modeling formulas above. Highlight weaknesses, "
            "gaps and limitations only; do not provide suggestions or rewrites."
        )
        try:
            return LLM(temperature=0.0).generate(prompt, system=FORMULAS_CRITIC_SYSTEM)
        except Exception as e:  # noqa: BLE001
            return f"(critic unavailable: {e})"

    @tool
    def save_modeling(task_id: str, payload: dict) -> str:
        """Save a subtask's modeling result to modeling/<task_id>.json.

        ``payload`` may include ``critic_rounds`` (a list of {round, critique})
        recording the actor->critic refinement trace."""
        return s3_io.put_json(session_id, f"modeling/{task_id}.json", payload)

    return [retrieve_hmml_methods, get_analysis, critique_modeling, save_modeling]



# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------
def solver_tools(session_id: str, ci) -> list:
    @tool
    def execute_code(code: str) -> dict:
        """Execute Python in the AgentCore Code Interpreter sandbox.

        Returns {ok, stdout, stderr, artifacts}.
        """
        try:
            return ci.execute(code)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "stdout": "", "stderr": str(e), "artifacts": []}

    @tool
    def get_modeling(task_id: str) -> dict:
        """Read the modeling result for a subtask from modeling/<task_id>.json."""
        return s3_io.get_json(session_id, f"modeling/{task_id}.json")

    @tool
    def read_dependent_artifacts(task_id: str) -> str:
        """Read prerequisite subtasks' artifact paths/results (dependent_file_prompt)."""
        try:
            return s3_io.get_text(session_id, f"solving/{task_id}.deps.txt")
        except Exception:
            return ""

    @tool
    def save_code(task_id: str, code: str) -> str:
        """Save the generated code to solving/<task_id>.py."""
        return s3_io.put_text(session_id, f"solving/{task_id}.py", code, content_type="text/x-python")

    @tool
    def save_result(task_id: str, result: dict) -> str:
        """Save the solving result to solving/<task_id>.json."""
        return s3_io.put_json(session_id, f"solving/{task_id}.json", result)

    return [execute_code, get_modeling, read_dependent_artifacts, save_code, save_result]


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------
def reporter_tools(session_id: str) -> list:
    @tool
    def get_analysis() -> str:
        """Read the problem analysis at analysis/problem_analysis.md."""
        return s3_io.get_text(session_id, "analysis/problem_analysis.md")

    @tool
    def get_modeling(task_id: str) -> dict:
        """Read the modeling result for a subtask."""
        return s3_io.get_json(session_id, f"modeling/{task_id}.json")

    @tool
    def get_solving(task_id: str) -> dict:
        """Read the solving result for a subtask."""
        return s3_io.get_json(session_id, f"solving/{task_id}.json")

    @tool
    def list_artifacts(task_id: str) -> list:
        """Read the list of chart/artifact references for a subtask (best-effort)."""
        try:
            res = s3_io.get_json(session_id, f"solving/{task_id}.json")
            return res.get("artifacts", [])
        except Exception:
            return []

    @tool
    def save_report(markdown: str) -> dict:
        """Save the final report to report/report.md; return {report_key, report_url}."""
        key = s3_io.put_text(session_id, "report/report.md", markdown)
        url = s3_io.presign(session_id, "report/report.md")
        return {"report_key": key, "report_url": url}

    return [get_analysis, get_modeling, get_solving, list_artifacts, save_report]


# ---------------------------------------------------------------------------
# Orchestrator (supervisor; sub-agents exposed as invoke_* tools)
# ---------------------------------------------------------------------------
# Map each invoke_* tool to the four-stage SSE ``stage`` label.
_STAGE_OF = {
    "analyst": "analysis",
    "modeler": "modeling",
    "solver": "solving",
    "reporter": "report",
}


def _dispatch(agent: str, arn, payload: dict, session_id: str) -> dict:
    """Run a sub-agent either IN-PROCESS (agents-as-tools, single Runtime) or
    across the network (legacy 5-Runtime mode), selected by ``config.INPROCESS``.

    Either way a ``{type:"stage", stage, status}`` pair is published to the
    in-process event bus so the Orchestrator's SSE stream reflects progress even
    when an LLM supervisor (not the deterministic pipeline) drives the calls.
    """
    stage = _STAGE_OF[agent]
    task_id = payload.get("task_id")
    start_ev = {"type": "stage", "stage": stage, "status": "start", "agent": agent}
    if task_id is not None:
        start_ev["task_id"] = str(task_id)
    events.emit(start_ev)

    if config.INPROCESS:
        # Lazy import to avoid a circular import (runners imports tools).
        from . import runners

        runner = getattr(runners, f"run_{agent}")
        result = runner(payload)
    else:
        result = invoke.invoke_agent(arn, payload, session_id)

    done_ev = {"type": "stage", "stage": stage, "status": "done", "agent": agent}
    if task_id is not None:
        done_ev["task_id"] = str(task_id)
    if isinstance(result, dict):
        if "order" in result:
            done_ev["order"] = result["order"]
        if "task_modeling_method" in result:
            done_ev["method"] = result.get("task_modeling_method", "")
        if "success" in result:
            done_ev["success"] = bool(result.get("success", False))
        if "attempts" in result:
            done_ev["attempts"] = int(result.get("attempts", 0))
        if result.get("report_url"):
            done_ev["report_url"] = result["report_url"]
    events.emit(done_ev)
    return result


def orchestrator_tools(session_id: str, actor_id: str) -> list:
    @tool
    def invoke_analyst(problem: str) -> dict:
        """Invoke the Analyst sub-agent to analyse the problem and decompose it
        into a dependency DAG. Returns {ok, order, tasknum, *_key} (the
        topological ``order`` of subtask ids drives the rest of the pipeline)."""
        return _dispatch(
            "analyst", config.ANALYST_ARN,
            {"session_id": session_id, "problem": problem, "with_code": True,
             "actor_id": actor_id},
            session_id,
        )

    @tool
    def invoke_modeler(task_id: str, problem: str, task_description: str) -> dict:
        """Invoke the Modeler sub-agent for ONE subtask (HMML retrieval +
        actor->critic modeling). Call this BEFORE invoke_solver for the same
        task_id. Returns {ok, modeling_key, task_modeling_method, ...}."""
        return _dispatch(
            "modeler", config.MODELER_ARN,
            {"session_id": session_id, "task_id": task_id, "problem": problem,
             "task_description": task_description, "with_code": True},
            session_id,
        )

    @tool
    def invoke_solver(task_id: str, problem: str, modeling_key: str,
                      dependent_file_prompt: str = "") -> dict:
        """Invoke the Solver sub-agent for ONE subtask (code generation + sandbox
        execution + self-repair retry). Call this AFTER invoke_modeler for the
        same task_id, passing its ``modeling_key``. Returns {ok, success,
        attempts, code_key, result_key, artifacts}."""
        return _dispatch(
            "solver", config.SOLVER_ARN,
            {"session_id": session_id, "task_id": task_id, "problem": problem,
             "modeling_key": modeling_key, "dependent_file_prompt": dependent_file_prompt,
             "max_retries": config.SOLVER_MAX_RETRIES},
            session_id,
        )

    @tool
    def invoke_reporter(problem: str, order: list) -> dict:
        """Invoke the Reporter sub-agent to assemble the FINAL report once all
        subtasks in ``order`` have been modelled and solved. Returns
        {ok, report_key, report_url}."""
        return _dispatch(
            "reporter", config.REPORTER_ARN,
            {"session_id": session_id, "problem": problem, "order": order},
            session_id,
        )

    @tool
    def get_task_descriptions() -> list:
        """Read the Analyst's subtask decomposition [{id,title,description}] so
        you can pass each subtask's description to invoke_modeler."""
        try:
            return s3_io.get_json(session_id, "analysis/task_descriptions.json") or []
        except Exception:  # noqa: BLE001
            return []

    @tool
    def save_memory_event(text: str) -> None:
        """Record a progress note as a short-term Memory event."""
        return memory.save_event(session_id, "orchestrator", "assistant", text)

    @tool
    def retrieve_preferences(query: str) -> list:
        """Retrieve long-term user preferences (semantic) for this actor."""
        return memory.retrieve(actor_id=actor_id, query=query,
                               namespace="preferences", top_k=5)

    return [invoke_analyst, invoke_modeler, invoke_solver, invoke_reporter,
            get_task_descriptions, save_memory_event, retrieve_preferences]

