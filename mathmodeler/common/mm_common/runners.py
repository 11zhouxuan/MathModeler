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

import logging
import threading

from . import config, llm, s3_io, workspace


logger = logging.getLogger("mm.runners")

from .hmml import MethodRetriever
from .prompts import (
    ANALYST_SYSTEM,
    MODELER_SYSTEM,
    REPORTER_SYSTEM,
    SOLVER_SYSTEM,
)
from .tools import analyst_tools, modeler_tools, reporter_tools, solver_tools

# Strands built-in tools: file operations + shell (available to all sub-agents).
# We re-implement the core logic of strands' read_file/write_file/editor/shell
# (which are simple file I/O + subprocess) as our own @tool functions, adding a
# `description` first parameter for UI display. This avoids the issue of trying
# to programmatically call strands Tool objects (which are not plain callables).
#
# IMPORTANT: These tools resolve file paths relative to the SESSION WORKSPACE
# (jobs/mm-xxx/), not the process CWD, so files appear in the file browser panel.
import pathlib as _pathlib
import subprocess as _subprocess_mod

# Module-level session context: set before agent.stream_async() is called.
# Using a simple global (not threading.local) because Strands' @tool functions
# may execute in a different thread (ThreadPoolExecutor), making thread-local
# invisible. The orchestrator processes one session at a time, so this is safe.
_current_session_id: str | None = None


def set_session_context(session_id: str) -> None:
    """Set the current session_id for builtin tools to resolve paths against."""
    global _current_session_id
    _current_session_id = session_id


def _resolve_path(file_path: str) -> _pathlib.Path:
    """Resolve a relative file_path against the current session workspace.

    Guards against double-nesting: if the agent passes a path that already
    contains the session workspace prefix (e.g. "jobs/mm-xxx/modeling/T1.json"),
    strip the redundant prefix so we don't create
    ``jobs/mm-xxx/jobs/mm-xxx/modeling/T1.json``.
    """
    p = _pathlib.Path(file_path)
    if p.is_absolute():
        return p
    if not _current_session_id:
        return p
    # Strip redundant session-workspace prefix if present.
    # Patterns the LLM might hallucinate:
    #   "jobs/<session_id>/..."  or  "<session_id>/..."
    parts = p.parts
    session_base = workspace.session_path(_current_session_id)
    # Case 1: path starts with "jobs/<session_id>/..."
    if len(parts) >= 2 and parts[0] == "jobs" and parts[1] == _current_session_id:
        rel = _pathlib.Path(*parts[2:]) if len(parts) > 2 else _pathlib.Path(".")
        return session_base / rel
    # Case 2: path starts with "<session_id>/..."
    if len(parts) >= 1 and parts[0] == _current_session_id:
        rel = _pathlib.Path(*parts[1:]) if len(parts) > 1 else _pathlib.Path(".")
        return session_base / rel
    # Normal case: relative path within workspace
    return session_base / file_path


try:
    from strands import tool as _tool

    @_tool
    def read_file(description: str, file_path: str) -> str:
        """Read a file from the workspace.

        Args:
            description: ≤10字中文动作摘要（展示给用户）。
            file_path: Path to the file to read.
        """
        p = _resolve_path(file_path)
        if not p.exists():
            return f"Error: file not found: {file_path}"
        try:
            return p.read_text(encoding="utf-8")
        except Exception as e:
            return f"Error reading {file_path}: {e}"

    @_tool
    def write_file(description: str, file_path: str, content: str) -> str:
        """Write content to a file in the workspace (creates directories as needed).

        Args:
            description: ≤10字中文动作摘要（展示给用户）。
            file_path: Path to the file to write.
            content: The content to write.
        """
        p = _resolve_path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} chars to {file_path}"

    @_tool
    def editor(description: str, file_path: str, old_text: str, new_text: str) -> str:
        """Edit a file by replacing a specific text segment.

        Args:
            description: ≤10字中文动作摘要（展示给用户）。
            file_path: Path to the file to edit.
            old_text: The exact text to find and replace (must match exactly once).
            new_text: The replacement text.
        """
        p = _resolve_path(file_path)
        if not p.exists():
            return f"Error: file not found: {file_path}"
        text = p.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count == 0:
            return f"Error: old_text not found in {file_path}"
        if count > 1:
            return f"Error: old_text matches {count} locations in {file_path}. Please provide a more specific/unique snippet."
        text = text.replace(old_text, new_text, 1)
        p.write_text(text, encoding="utf-8")
        return f"Successfully edited {file_path}"

    @_tool
    def shell(description: str, command: str) -> str:
        """Execute a shell command and return its output.

        Args:
            description: ≤10字中文动作摘要（展示给用户）。
            command: The shell command to execute.
        """
        try:
            result = _subprocess_mod.run(
                command, shell=True, capture_output=True, text=True, timeout=60
            )
            output = result.stdout
            if result.returncode != 0:
                output += f"\n[stderr] {result.stderr}" if result.stderr else ""
                output += f"\n[exit code: {result.returncode}]"
            return output or "(no output)"
        except _subprocess_mod.TimeoutExpired:
            return "Error: command timed out (60s)"
        except Exception as e:
            return f"Error: {e}"

    BUILTIN_TOOLS = [read_file, write_file, editor, shell]
except Exception:  # strands not installed (unit-test environment)
    BUILTIN_TOOLS = []

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
        tasks = workspace.read_json(session_id, "analysis/task_descriptions.json") or []
    except Exception:  # noqa: BLE001
        tasks = []
    dag_graph = {}
    try:
        dag_graph = workspace.read_json(session_id, "analysis/dag.json") or {}
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
    # Merge deps from dag_graph into each task so the supervisor has ready-to-use
    # task items with deps for update_task calls.
    for t in tasks:
        tid = t.get("id")
        if tid and "deps" not in t:
            t["deps"] = dag_graph.get(tid, [])
    return {
        "order": order,
        "tasknum": len(order),
        "dag": dag_graph,
        "tasks": tasks,
    }


def build_analyst_agent(session_id: str):
    """Construct the Analyst Strands Agent (streaming-ready, for the Supervisor)."""
    logger.info("[runners] build_analyst_agent session=%s", session_id)
    return llm.build_agent(ANALYST_SYSTEM, analyst_tools(session_id) + BUILTIN_TOOLS)



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
        payload = workspace.read_json(session_id, f"modeling/{task_id}.json") or {}
    except Exception:  # noqa: BLE001
        payload = {}
    return {
        "modeling_key": f"modeling/{task_id}.json",
        "task_modeling_method": payload.get("task_modeling_method", ""),
        "retrieved_methods": payload.get("retrieved_methods", []),
        "task_analysis": payload.get("task_analysis", ""),
        "task_modeling_formulas": payload.get("task_modeling_formulas", ""),
        "critic_rounds": payload.get("critic_rounds", []),
    }


def build_modeler_agent(session_id: str):
    """Construct the Modeler Strands Agent (streaming-ready, for the Supervisor)."""
    logger.info("[runners] build_modeler_agent session=%s (loading HMML retriever…)", session_id)
    agent = llm.build_agent(_MODELER_SYSTEM, modeler_tools(session_id, get_retriever()) + BUILTIN_TOOLS)
    logger.info("[runners] build_modeler_agent ready session=%s", session_id)
    return agent



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
# Substrings that identify an AgentCore Code Interpreter session that was
# reclaimed/expired (idle >~15min or transient infra error). When seen we
# transparently re-provision a fresh session and retry the call ONCE.
_CI_DEAD_MARKERS = (
    "not active",
    "session is not active",
    "validationexception",
    "resourcenotfound",
    "sessionnotfound",
    "session not found",
    "expired",
)


def _is_session_dead(err: str) -> bool:
    e = (err or "").lower()
    return any(m in e for m in _CI_DEAD_MARKERS)


class CodeInterpreterClient:
    """Adapter exposing ``.execute(code) -> {ok, stdout, stderr, artifacts}``.

    Wraps the AgentCore Code Interpreter SDK. The concrete SDK class/method
    names are resolved defensively so unit tests can inject a fake with the
    same ``execute`` contract without importing ``bedrock_agentcore``.

    Resilience (added):
      * the session is started with the max ``session_timeout_seconds`` (8h);
      * a background daemon HEARTBEAT keeps the session warm (AgentCore reclaims
        sessions idle >~15min) — a tiny no-op ``executeCode`` every N seconds;
      * a ``threading.Lock`` serializes all ``invoke`` calls (heartbeat vs. the
        solver's tools) since one session must not be invoked concurrently;
      * every ``invoke`` SELF-HEALS once: if the session is reported dead, it
        ``stop()``+``start()`` a fresh session and retries the call a single
        time (note: a restart wipes the sandbox filesystem, hence the Solver is
        instructed to use single self-contained scripts).
    """

    def __init__(self, region: str | None = None):
        self.region = region or config.REGION
        self._ci = None
        self._lock = threading.RLock()
        self._hb_stop: threading.Event | None = None
        self._hb_thread: threading.Thread | None = None

    # ---------------------------------------------------------------- lifecycle
    def _new_session(self) -> None:
        from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

        self._ci = CodeInterpreter(region=self.region)
        # Pass the max lifetime if the SDK supports it; fall back gracefully.
        try:
            self._ci.start(session_timeout_seconds=config.CI_SESSION_TIMEOUT_SECONDS)
        except TypeError:
            self._ci.start()

    def start(self) -> None:
        self._new_session()
        self._start_heartbeat()

    def _raw_invoke(self, action: str, params: dict):
        """Single SDK invoke under the lock (no self-heal). Raises on failure."""
        if self._ci is None:
            raise RuntimeError("Code Interpreter session not started")
        with self._lock:
            return self._ci.invoke(action, params)

    def invoke(self, action: str, params: dict):
        """Invoke an SDK action, self-healing a dead session exactly once."""
        try:
            return self._raw_invoke(action, params)
        except Exception as e:  # noqa: BLE001
            if not _is_session_dead(str(e)):
                raise
            logger.warning("[runners] CI session dead on %s (%s) — restarting & retrying once",
                           action, e)
            with self._lock:
                try:
                    if self._ci is not None:
                        self._ci.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._new_session()
            return self._raw_invoke(action, params)

    # ------------------------------------------------------------------ heartbeat
    def _start_heartbeat(self) -> None:
        interval = max(30, int(config.CI_HEARTBEAT_SECONDS))
        self._hb_stop = threading.Event()

        def _beat():
            while self._hb_stop is not None and not self._hb_stop.wait(interval):
                try:
                    # Lightweight no-op keeps the sandbox session warm.
                    self.invoke("executeCode", {"language": "python", "code": "pass"})
                except Exception:  # noqa: BLE001 - heartbeat must never crash
                    logger.debug("[runners] CI heartbeat hiccup (ignored)")

        self._hb_thread = threading.Thread(target=_beat, name="ci-heartbeat", daemon=True)
        self._hb_thread.start()

    def _stop_heartbeat(self) -> None:
        if self._hb_stop is not None:
            self._hb_stop.set()
        self._hb_thread = None
        self._hb_stop = None

    # ------------------------------------------------------------------ file ops
    def write_files(self, files: list[dict]) -> dict:
        """Write files into the sandbox. ``files`` = [{"path","text"}, ...]."""
        result = self.invoke("writeFiles", {"content": files})
        _, stderr, _ = _parse_ci_result(result)
        return {"ok": stderr == "", "stderr": stderr}

    def read_files(self, paths: list[str]) -> dict:
        """Read files back from the sandbox; returns concatenated text output."""
        result = self.invoke("readFiles", {"paths": paths})
        stdout, stderr, _ = _parse_ci_result(result)
        return {"ok": stderr == "", "stdout": stdout, "stderr": stderr}

    # -------------------------------------------------------------------- execute
    def execute(self, code: str) -> dict:
        if self._ci is None:
            raise RuntimeError("Code Interpreter session not started")
        result = self.invoke("executeCode", {"language": "python", "code": code})
        stdout, stderr, artifacts = _parse_ci_result(result)
        return {
            "ok": stderr == "",
            "stdout": stdout,
            "stderr": stderr,
            "artifacts": artifacts,
        }

    def execute_streaming(self, code: str):
        """Execute code and yield stdout lines as they arrive from the EventStream.

        Yields: dict with {"stdout_chunk": str} for each text event.
        Final yield: full result dict {ok, stdout, stderr, artifacts}.
        """
        if self._ci is None:
            raise RuntimeError("Code Interpreter session not started")
        result = self.invoke("executeCode", {"language": "python", "code": code})
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
                        text = block.get("text", "")
                        stdout_parts.append(text)
                        if text.strip():
                            yield {"stdout_chunk": text}
        except Exception:  # noqa: BLE001
            stdout_parts.append(str(result))
        yield {
            "ok": stderr == "",
            "stdout": "".join(stdout_parts),
            "stderr": stderr,
            "artifacts": artifacts,
        }

    def stop(self) -> None:
        self._stop_heartbeat()
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
        res = workspace.read_json(session_id, f"solving/{task_id}.json") or {}
    except Exception:  # noqa: BLE001
        res = {}
    stdout = res.get("stdout", "")
    return {
        "success": bool(res.get("success", False)),
        "attempts": int(res.get("attempts", 0)),
        "code_key": f"solving/{task_id}.py",
        "result_key": f"solving/{task_id}.json",
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
        logger.info("[runners] build_solver_agent session=%s — starting Code Interpreter…",
                    session_id)
        try:
            ci.start()
            logger.info("[runners] Code Interpreter started session=%s", session_id)
        except Exception:
            logger.exception("[runners] Code Interpreter FAILED to start session=%s "
                             "(solver code execution will not work locally)", session_id)
            raise
    agent = llm.build_agent(SOLVER_SYSTEM, solver_tools(session_id, ci) + BUILTIN_TOOLS)
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
    pdf_path = workspace.file_path(session_id, "report/report.pdf")
    pdf_exists = pdf_path.exists()
    # Try to get S3 URL if PDF was uploaded
    s3_url = ""
    try:
        s3_url = s3_io.presign(session_id, "report/report.pdf")
    except Exception:  # noqa: BLE001
        pass
    return {
        "pdf_path": str(pdf_path) if pdf_exists else "",
        "s3_url": s3_url,
        "pdf_exists": pdf_exists,
    }


def build_reporter_agent(session_id: str):
    """Construct the Reporter Strands Agent (streaming-ready, for the Supervisor)."""
    return llm.build_agent(REPORTER_SYSTEM, reporter_tools(session_id) + BUILTIN_TOOLS)


def run_reporter(body: dict) -> dict:
    session_id = body["session_id"]
    problem = body["problem"]
    order = body.get("order", [])
    language = body.get("language", "中文")
    agent = llm.build_agent(REPORTER_SYSTEM, reporter_tools(session_id), streaming=False)

    task = (
        f"session_id={session_id} order={order} language={language}\n"
        "请逐个子任务读取分析/建模/求解产物，先给出论文提纲，然后分段撰写 LaTeX 论文"
        "（write_report_section 分段写入），最后调用 compile_report 编译为 PDF。\n\n"
        f"问题：{problem}"
    )
    try:
        agent(task)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), **_reporter_extract(session_id)}
    return {"ok": True, **_reporter_extract(session_id)}
