"""mm_common.schemas — pydantic request/response/artifact schemas (tech-design §3).

These models define the contract between the Orchestrator (supervisor agent)
and the four sub-agents, plus the portal entry payload. Sub-agents validate
their responses against these in tests (``Model.model_validate(resp)``).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

# --- shared ---------------------------------------------------------------


class DataFile(BaseModel):
    name: str
    s3_key: str


# --- portal -> Orchestrator (entry) ---------------------------------------


class SolveRequest(BaseModel):
    session_id: str
    problem: str
    data_files: list[DataFile] = Field(default_factory=list)
    actor_id: str = "anonymous"


class SolveResponse(BaseModel):
    ok: bool = True
    report_key: str
    report_url: str
    order: list[str]


# --- Orchestrator -> Analyst ----------------------------------------------


class AnalystRequest(BaseModel):
    session_id: str
    problem: str
    with_code: bool = True
    actor_id: str = "anonymous"


class AnalystResponse(BaseModel):
    ok: bool = True
    problem_analysis_key: str
    task_descriptions_key: str
    dag_key: str
    order: list[str]
    tasknum: int


# --- Orchestrator -> Modeler ----------------------------------------------


class ModelerRequest(BaseModel):
    session_id: str
    task_id: str
    problem: str
    task_description: str
    problem_analysis_key: str | None = None
    with_code: bool = True


class ModelerResponse(BaseModel):
    ok: bool = True
    modeling_key: str
    task_modeling_method: str
    retrieved_methods: list[str]
    task_analysis: str = ""
    task_modeling_formulas: str = ""


# --- Orchestrator -> Solver -----------------------------------------------


class SolverRequest(BaseModel):
    session_id: str
    task_id: str
    problem: str
    modeling_key: str
    dependent_file_prompt: str = ""
    max_retries: int = 3


class SolverResponse(BaseModel):
    ok: bool = True
    success: bool
    attempts: int
    code_key: str
    result_key: str
    artifacts: list[str] = Field(default_factory=list)
    stdout_tail: str = ""
    error: str | None = None


# --- Orchestrator -> Reporter ---------------------------------------------


class ReporterRequest(BaseModel):
    session_id: str
    problem: str
    order: list[str]


class ReporterResponse(BaseModel):
    ok: bool = True
    report_key: str
    report_url: str


# --- artifacts written to S3 ----------------------------------------------


class TaskDescription(BaseModel):
    id: str
    title: str = ""
    description: str = ""


class ModelingArtifact(BaseModel):
    task_analysis: str = ""
    task_modeling_formulas: str = ""
    task_modeling_method: str = ""
    retrieved_methods: list[str] = Field(default_factory=list)


class SolvingResult(BaseModel):
    success: bool
    attempts: int
    stdout: str = ""
    stderr: str = ""
    return_values: dict | None = None
