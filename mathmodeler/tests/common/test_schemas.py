"""§10.2 contract — pydantic schemas validate the §3 response shapes."""
import pytest
from pydantic import ValidationError

from mm_common import schemas


def test_analyst_response_ok():
    m = schemas.AnalystResponse.model_validate({
        "ok": True,
        "problem_analysis_key": "mathmodeler/s/analysis/problem_analysis.md",
        "task_descriptions_key": "mathmodeler/s/analysis/task_descriptions.json",
        "dag_key": "mathmodeler/s/analysis/dag.json",
        "order": ["1", "2"],
        "tasknum": 2,
    })
    assert m.order == ["1", "2"]
    assert m.tasknum == 2


def test_solver_response_failure_path():
    m = schemas.SolverResponse.model_validate({
        "success": False, "attempts": 3,
        "code_key": "k/code.py", "result_key": "k/result.json",
        "artifacts": [], "stdout_tail": "...", "error": "Traceback ...",
    })
    assert m.success is False and m.attempts == 3 and m.error


def test_modeler_response_requires_method():
    with pytest.raises(ValidationError):
        schemas.ModelerResponse.model_validate({
            "modeling_key": "k.json", "retrieved_methods": ["MIP"],
            # missing task_modeling_method
        })


def test_solve_request_defaults():
    r = schemas.SolveRequest.model_validate({"session_id": "mm-abc", "problem": "p"})
    assert r.actor_id == "anonymous" and r.data_files == []
