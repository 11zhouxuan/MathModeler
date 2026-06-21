"""mm_common.tools — per-agent @tool factories (tech-design §2.10, route ② core).

Each agent is a   ``Agent(system_prompt, tools)``. The reusable ``@tool``s
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

import subprocess

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

from . import config, dag, events, invoke, memory, workspace, s3_io



# ---------------------------------------------------------------------------
# Analyst
# ---------------------------------------------------------------------------
def analyst_tools(session_id: str) -> list:
    @tool
    def describe_data(description: str) -> str:
        """List and summarise data files in the session workspace data/ folder.

        Args:
            description: ≤10字中文动作摘要（展示给用户）。

        MUST be called at the start of analysis to check if any data files exist.
        Returns file listing with summaries, or 'No data files found' if empty.
        """
        try:
            data_dir = workspace.file_path(session_id, "data")
            if not data_dir.exists():
                return "No data files found in workspace."
            files = list(data_dir.iterdir())
            if not files:
                return "No data files found in workspace."
            summaries = []
            for f in sorted(files):
                if f.is_file():
                    size = f.stat().st_size
                    # Read first 2000 chars for text files
                    preview = ""
                    try:
                        text = f.read_text(encoding="utf-8")[:2000]
                        preview = f"\n  Preview: {text[:500]}..."
                    except Exception:
                        preview = " (binary file)"
                    summaries.append(f"  - {f.name} ({size} bytes){preview}")
            if not summaries:
                return "No data files found in workspace."
            return "Data files in workspace:\n" + "\n".join(summaries)
        except Exception as e:  # noqa: BLE001
            return f'{{"ok": false, "error": "{e}"}}'

    @tool
    def build_dag(description: str, tasks: list, graph: dict) -> dict:
        """Build a subtask DAG from task definitions and dependency graph.

        Args:
            description: ≤10字中文动作摘要（展示给用户）。
            tasks: List of [{id, title, description}] defining each subtask.
            graph: Adjacency list {tid: [dep_ids]} defining dependencies.

        Saves both task_descriptions.json and dag.json to the workspace.
        Returns {"dag": ..., "order": [...], "tasknum": N}.
        """
        # Save task descriptions
        workspace.write_json(session_id, "analysis/task_descriptions.json", tasks)

        # Compute topological order; fallback to linear on error
        tasknum = len(tasks)
        try:
            order = dag.compute_dag_order(graph)
        except Exception:
            graph = dag.fallback_linear_dag(tasknum)
            order = dag.compute_dag_order(graph)

        # Save DAG
        workspace.write_json(session_id, "analysis/dag.json", graph)
        return {"dag": graph, "order": order, "tasknum": tasknum}

    return [describe_data, build_dag]


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
            return f'{{"ok": false, "error": "{e}"}}'

    @tool
    def critique_modeling(description: str, task_description: str, task_analysis: str,
                          modeling_formulas: str) -> str:
        """Self-evaluation tool: ask an independent Mathematical Modeling Critic
        to critique the CURRENT modeling formulas (accuracy/rigor, innovation,
        applicability). Returns a plain-text critique highlighting weaknesses
        only — use it to drive an actor->critic refinement loop before saving.

        Args:
            description: ≤10字中文动作摘要（展示给用户）。
            task_description: The subtask description.
            task_analysis: The task analysis text.
            modeling_formulas: The current modeling formulas to critique.
        """
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

    return [retrieve_hmml_methods, critique_modeling]



# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------
# Maximum stdout chars returned to the LLM (~5K tokens). Prevents base64 or
# other massive print() output from blowing up context. The limit is enforced
# ONLY on the tool result seen by the model; internal tools (export_sandbox_file)
# bypass this because they call ci.execute() directly.
_MAX_STDOUT_CHARS = 20_000

def solver_tools(session_id: str, ci) -> list:
    @tool
    async def execute_code(description: str, code: str):
        """Execute Python in the AgentCore Code Interpreter sandbox.

        Args:
            description: <=10 char Chinese action summary (shown to user).
            code: The Python code to execute.

        Returns {ok, stdout, stderr, artifacts}.
        Streams stdout in real-time by tailing a log file in the sandbox.

        IMPORTANT:
        - Execution time is LIMITED to ~5 minutes. If your script takes longer,
          it will be terminated with a TIMEOUT error.
        - NEVER print binary/base64 content to stdout. Use plt.savefig() to create
          figures, then call export_sandbox_file to transfer them to workspace.
        - DO print progress messages (e.g. print("Step 1/3 done...", flush=True))
          so the user can see computation progress in real time.
        """
        import asyncio
        import threading
        import time as _time
        timeout = config.CI_EXECUTE_TIMEOUT_SECONDS

        # Wrap user code: tee stdout to _run.log so we can tail it.
        # NOTE: All paths use relative (CWD) names because the AgentCore CI SDK
        # rejects absolute /tmp/ paths in write_files (path traversal filter).
        wrapper_code = "import sys, io\nclass _Tee(io.TextIOBase):\n    def __init__(self, orig, log):\n        self._orig = orig\n        self._log = log\n    def write(self, s):\n        self._orig.write(s)\n        self._log.write(s)\n        self._log.flush()\n        return len(s)\n    def flush(self):\n        self._orig.flush()\n        self._log.flush()\n_log_f = open('_run.log', 'w')\nsys.stdout = _Tee(sys.stdout, _log_f)\ntry:\n    exec(open('_user_code.py').read())\nfinally:\n    sys.stdout = sys.stdout._orig\n    _log_f.close()\n    open('_run_done', 'w').write('1')\n"

        # Upload user code to sandbox (relative path — /tmp/ is blocked by SDK)
        try:
            ci.write_files([{"path": "_user_code.py", "text": code}])
        except Exception as e:  # noqa: BLE001
            yield {"ok": False, "stdout": "", "stderr": f"Failed to upload code: {e}", "artifacts": []}
            return

        # Start execution in a background thread
        exec_result = {}

        def _run():
            try:
                exec_result["value"] = ci.execute(wrapper_code)
            except Exception as e:  # noqa: BLE001
                exec_result["error"] = str(e)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        # Poll _run.log every 2 seconds
        offset = 0
        deadline = _time.time() + timeout
        poll_interval = 2.0

        while thread.is_alive() and _time.time() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                tail_result = ci.invoke(
                    "executeCommand",
                    {"command": f"tail -c +{offset + 1} _run.log 2>/dev/null"}
                )
                new_text = ""
                stream = tail_result.get("stream") if isinstance(tail_result, dict) else None
                if stream:
                    for ev in stream:
                        res = ev.get("result", ev) if isinstance(ev, dict) else {}
                        for blk in res.get("content", []) or []:
                            if isinstance(blk, dict) and blk.get("type") == "text":
                                new_text += blk.get("text", "")
                if new_text:
                    offset += len(new_text.encode("utf-8"))
                    yield {"stdout_chunk": new_text}
                else:
                    # Heartbeat: keep the SSE connection alive during long CI executions
                    yield {"heartbeat": True}
            except Exception:  # noqa: BLE001
                yield {"heartbeat": True}

        # Check timeout
        if thread.is_alive():
            yield {"stdout_chunk": "[TIMEOUT] execution timed out"}
            yield {
                "ok": False,
                "stdout": "",
                "stderr": (
                    f"EXECUTION_TIMEOUT: Script exceeded {timeout}s limit. "
                    "Your code is too slow. Optimize or simplify, then retry."
                ),
                "artifacts": [],
            }
            return

        # Thread finished; get result
        thread.join(timeout=5)

        if "error" in exec_result:
            yield {"ok": False, "stdout": "", "stderr": exec_result["error"], "artifacts": []}
            return

        result = exec_result.get("value", {"ok": False, "stdout": "", "stderr": "No result", "artifacts": []})

        # Truncate large stdout: keep head + tail, omit middle (never error)
        stdout = result.get("stdout", "") or ""
        if len(stdout) > _MAX_STDOUT_CHARS:
            head = stdout[:3000]
            tail = stdout[-3000:]
            omitted = len(stdout) - 6000
            result["stdout"] = head + "\n... [" + str(omitted) + " chars omitted] ...\n" + tail

        yield result

    @tool
    def write_sandbox_file(description: str, path: str, content: str) -> dict:
        """Write a file (e.g. a full Python script) into the sandbox filesystem.

        Args:
            description: ≤10字中文动作摘要（展示给用户）。
            path: File path in the sandbox.
            content: File content.

        Prefer writing your COMPLETE self-contained script with this tool (e.g.
        path="solution.py"), then run it via
        execute_code("exec(open('solution.py').read())"). This keeps each
        execute_code payload small and avoids long, fragmented interactions.
        Returns {ok, stderr}.
        """
        try:
            return ci.write_files([{"path": path, "text": content}])
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "stderr": str(e)}

    @tool
    def read_sandbox_file(description: str, path: str) -> dict:
        """Read a file back from the sandbox filesystem.

        Args:
            description: ≤10字中文动作摘要（展示给用户）。
            path: File path in the sandbox.

        Returns {ok, stdout, stderr}.
        """
        try:
            read_code = (
                f"import sys, os\n"
                f"path = {path!r}\n"
                f"if not os.path.exists(path):\n"
                f"    print('FILE_NOT_FOUND: ' + path, file=sys.stderr)\n"
                f"else:\n"
                f"    print(open(path).read())\n"
            )
            result = ci.execute(read_code)
            return {"ok": result.get("ok", False), "stdout": result.get("stdout", ""), "stderr": result.get("stderr", "")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "stdout": "", "stderr": str(e)}

    @tool
    def export_sandbox_file(description: str, sandbox_path: str, workspace_path: str) -> dict:
        """Export a file (including binary like PNG/PDF) from the sandbox to the workspace.

        Use this to save plots/figures generated in the sandbox (e.g. by matplotlib
        plt.savefig) directly to the workspace WITHOUT base64 encoding. This is the
        preferred way to export images and other binary artifacts.

        Args:
            description: ≤10字中文动作摘要（展示给用户）。
            sandbox_path: Path of the file inside the sandbox (e.g. "plot.png").
            workspace_path: Destination path in the workspace (e.g. "solving/figures/T1_trajectory.png").
        """
        import base64 as _b64
        try:
            # Use executeCode to base64-encode the file inside the sandbox and
            # capture it via stdout. This bypasses the readFiles API which returns
            # empty for binary files (SDK limitation confirmed by test).
            # The base64 content stays internal to this tool — never exposed to LLM.
            _export_code = (
                f"import base64, os, sys\n"
                f"path = {sandbox_path!r}\n"
                f"if not os.path.exists(path):\n"
                f"    print('__FILE_NOT_FOUND__', file=sys.stderr)\n"
                f"else:\n"
                f"    with open(path, 'rb') as f:\n"
                f"        print(base64.b64encode(f.read()).decode())\n"
            )
            result = ci.execute(_export_code)
            stderr = result.get("stderr", "")
            if "__FILE_NOT_FOUND__" in stderr:
                return {"ok": False, "error": f"File not found in sandbox: {sandbox_path}"}
            raw = (result.get("stdout", "") or "").strip()
            if not raw:
                return {"ok": False, "error": f"Empty content from sandbox: {sandbox_path}"}
            # Guard: reject files > 5MB (base64 is ~1.33x original size)
            if len(raw) > 7_000_000:  # ~5MB binary
                size_kb = len(raw) // 1333
                return {
                    "ok": False,
                    "error": (
                        f"File too large to export in one piece ({size_kb}KB, max ~5MB). "
                        "Split the file in the sandbox first using execute_code, e.g.:\n"
                        "  import os; data=open('big.pdf','rb').read()\n"
                        "  chunk=4*1024*1024  # 4MB chunks\n"
                        "  for i in range(0,len(data),chunk):\n"
                        "      open(f'part_{i//chunk}.bin','wb').write(data[i:i+chunk])\n"
                        "Then export each part_N.bin separately with export_sandbox_file "
                        "and reassemble on the workspace side, or reduce the file size "
                        "(e.g. lower DPI for plots)."
                    ),
                }
            from . import workspace as _ws
            from .runners import _current_session_id
            sid = _current_session_id
            if not sid:
                return {"ok": False, "error": "No active session context"}
            out_path = _ws.session_path(sid) / workspace_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            data = _b64.b64decode(raw)
            out_path.write_bytes(data)
            return {"ok": True, "path": workspace_path, "size": len(data)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return [execute_code, write_sandbox_file, read_sandbox_file, export_sandbox_file]



# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------
def reporter_tools(session_id: str) -> list:
    @tool
    def list_artifacts(description: str, task_id: str) -> list:
        """List chart/figure files generated by the solver for a subtask.

        Args:
            description: ≤10字中文动作摘要（展示给用户）。
            task_id: The subtask ID whose artifacts to list.
        """
        figures_dir = workspace.file_path(session_id, "solving/figures")
        if not figures_dir.exists():
            return []
        # Return files that match the task_id prefix
        results = []
        for f in sorted(figures_dir.iterdir()):
            if f.is_file() and (task_id in f.name or f.name.startswith(task_id)):
                results.append({
                    "name": f.name,
                    "path": f"solving/figures/{f.name}",
                    "size": f.stat().st_size,
                })
        # If no task-specific files, return all figures
        if not results:
            for f in sorted(figures_dir.iterdir()):
                if f.is_file():
                    results.append({
                        "name": f.name,
                        "path": f"solving/figures/{f.name}",
                        "size": f.stat().st_size,
                    })
        return results

    @tool
    def compile_report(description: str) -> dict:
        """Compile report.tex to PDF using xelatex.

        Args:
            description: ≤10字中文动作摘要（展示给用户）。

        Runs xelatex twice (for cross-references). If successful, uploads the
        PDF to S3 and returns {ok, pdf_path, s3_url}. If compilation fails,
        returns {ok: false, stderr: ...}.
        """
        report_dir = workspace.file_path(session_id, "report")
        tex_file = report_dir / "report.tex"

        if not tex_file.exists():
            return {"ok": False, "stderr": "report.tex not found. Write report/report.tex first."}

        # Copy solver figures to report/figures/ for LaTeX \includegraphics
        solver_figs = workspace.file_path(session_id, "solving/figures")
        report_figs = report_dir / "figures"
        report_figs.mkdir(parents=True, exist_ok=True)
        if solver_figs.exists():
            import shutil
            for fig in solver_figs.iterdir():
                if fig.is_file():
                    shutil.copy2(fig, report_figs / fig.name)

        # Run xelatex twice
        stderr_output = ""
        for _pass in range(2):
            result = subprocess.run(
                ["xelatex", "-interaction=nonstopmode", "-halt-on-error", "report.tex"],
                cwd=str(report_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                stderr_output = result.stdout[-3000:] if result.stdout else result.stderr[-3000:]
                # Don't fail on first pass (cross-ref warnings are expected)
                if _pass == 1:
                    return {"ok": False, "stderr": f"xelatex failed (pass {_pass+1}):\n{stderr_output}"}

        pdf_path = report_dir / "report.pdf"
        if not pdf_path.exists():
            return {"ok": False, "stderr": "PDF not generated after xelatex runs."}

        # Upload final PDF to S3
        s3_url = ""
        try:
            pdf_bytes = pdf_path.read_bytes()
            s3_url = s3_io.put_bytes(session_id, "report/report.pdf", pdf_bytes, "application/pdf")
        except Exception as e:  # noqa: BLE001
            s3_url = f"(S3 upload failed: {e})"

        return {
            "ok": True,
            "pdf_path": str(pdf_path),
            "s3_url": s3_url,
            "size_bytes": pdf_path.stat().st_size,
        }

    return [list_artifacts, compile_report]



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
    def invoke_reporter(problem: str, order: list, language: str = "中文") -> dict:
        """Invoke the Reporter sub-agent to assemble the FINAL report once all
        subtasks in ``order`` have been modelled and solved. ``language`` is
        the user-confirmed language for the report (中文/English/mixed).
        Returns {ok, pdf_path, s3_url}."""
        return _dispatch(
            "reporter", config.REPORTER_ARN,
            {"session_id": session_id, "problem": problem, "order": order,
             "language": language},
            session_id,
        )

    @tool
    def get_task_descriptions() -> list:
        """Read the Analyst's subtask decomposition [{id,title,description}] so
        you can pass each subtask's description to invoke_modeler."""
        try:
            return workspace.read_json(session_id, "analysis/task_descriptions.json") or []
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

