from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class TaskEnvelope(BaseModel):
    envelope_id: str
    tenant: str
    source: str  # webhook, email, api, etc.
    raw_text: str
    structured: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=datetime.utcnow)


class TraceStep(BaseModel):
    i: int  # program counter; matches skill step pc
    tool: Optional[str] = None
    op: Optional[str] = None  # for pure steps with no external call
    args: dict[str, Any] = Field(default_factory=dict)
    result: Optional[Any] = None  # dict for most tools; list for get_shipments
    result_digest: str
    provenance: dict[str, str] = Field(default_factory=dict)
    effect_class: Literal["read", "pure", "idempotent", "compensable", "irreversible"]
    llm_span: Optional[str] = None
    cost: float = 0.0
    latency: int = 0  # milliseconds


class Trace(BaseModel):
    trace_id: str
    family: str
    mode: Literal["interpreted", "compiled"]
    signature: str
    steps: list[TraceStep]
    verified_success: bool
    cost_usd: float


class Guard(BaseModel):
    expr: str
    support: int
    kind: Literal["task", "state", "residual"]


class Hole(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    schema_: dict[str, Any] = Field(alias="schema")
    checks: list[str] = Field(default_factory=list)


class SkillStep(BaseModel):
    pc: int
    tool: Optional[str] = None
    op: Optional[str] = None
    effect: Literal["read", "pure", "idempotent", "compensable", "irreversible"]
    post: list[str] = Field(default_factory=list)
    hole: Optional[str] = None


class ShadowStats(BaseModel):
    runs: int = 0
    divergences: int = 0
    upper_bound_95: float = 1.0


class InputArg(BaseModel):
    type: str
    extractor: Optional[str] = None
    values: Optional[list[str]] = None
    source: Optional[str] = None


class TaskGuard(BaseModel):
    expr: str
    support: int


class Skill(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias="_id")
    family: str
    status: Literal["candidate", "probation", "active", "recompiling", "megamorphic", "rejected"]
    shape: Literal["monomorphic", "polymorphic"] = "monomorphic"
    parent: Optional[str] = None
    input_signature: dict[str, InputArg] = Field(default_factory=dict)
    task_guards: list[TaskGuard] = Field(default_factory=list)
    entry_guards: list[Guard] = Field(default_factory=list)
    steps: list[SkillStep]
    holes: list[Hole] = Field(default_factory=list)
    shadow: ShadowStats = Field(default_factory=ShadowStats)
    code_ref: str
    embedding: Optional[list[float]] = None


class EffectRecord(BaseModel):
    effect_key: str
    exec_id: str
    tool: str
    effect_class: Literal["read", "pure", "idempotent", "compensable", "irreversible"]
    resource_id: Optional[str] = None
    state: Literal["intent", "completed", "uncertain"]
    result: Optional[dict[str, Any]] = None


class CommittedEffect(BaseModel):
    step: int
    tool: str
    effect_key: str
    effect_class: Literal["irreversible", "compensable", "idempotent"]
    result: Optional[dict[str, Any]] = None


class DeoptFrame(BaseModel):
    skill: str
    pc: int
    failed_guard: str
    observed: Any
    locals: dict[str, Any]
    committed_effects: list[CommittedEffect]
    remaining_goal: str
    original_task: str  # envelope_id


class FamilyMatch(BaseModel):
    id: str
    similarity: float
    margin: float


class ArgDecision(BaseModel):
    value: Any
    method: Literal["structured", "pattern", "llm_schema"]
    grounded: bool
    agreement: Optional[bool] = None


class TaskGuardResult(BaseModel):
    passed: int
    failed: int


class DispatchDecision(BaseModel):
    envelope_id: str
    family: FamilyMatch
    skill: Optional[str] = None
    args: dict[str, ArgDecision] = Field(default_factory=dict)
    task_guards: TaskGuardResult
    route: Literal["compiled", "interpreted"]
    explore: bool = False
    shadow: bool = False


class ExecResult(BaseModel):
    exec_id: str
    ok: bool
    mode: Literal["interpreted", "compiled"]
    cost_usd: float
    latency_ms: int
    deopted: bool = False
    effects: list[EffectRecord] = Field(default_factory=list)


class VerifierResult(BaseModel):
    ok: bool
    reason: str
