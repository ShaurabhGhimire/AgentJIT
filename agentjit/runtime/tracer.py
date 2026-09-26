"""Trace recording with provenance capture (section 8.1).

Provenance is found by value matching: each argument value is looked up among
the task inputs and the results of earlier steps in the same trace, most
recent step first. That is what lets the generalizer tell parameters
(traceable) from holes (untraceable).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from agentjit.common.models import Trace, TraceStep


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _flatten(value: Any, path: str, out: list[tuple[str, Any]]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(v, f"{path}.{k}", out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _flatten(v, f"{path}[{i}]", out)
    else:
        out.append((path, value))


def _matchable(v: Any) -> bool:
    if isinstance(v, bool) or v is None:
        return False
    if isinstance(v, (int, float)):
        return v not in (0, 1)
    return isinstance(v, str) and len(v) >= 3


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return False


class Tracer:
    """inputs: parsed args for compiled runs; {**structured, "text": raw_text} for interpreted runs."""

    def __init__(self, inputs: dict[str, Any], family: str, mode: str, exec_id: str,
                 envelope_id: Optional[str] = None, trace_id: Optional[str] = None):
        self.inputs = inputs
        self.family = family
        self.mode = mode
        self.exec_id = exec_id
        self.envelope_id = envelope_id
        self.trace_id = trace_id or f"tr_{exec_id}"
        self.steps: list[TraceStep] = []
        self.extra_cost = 0.0  # model cost not attached to a single step

    def provenance(self, args: dict[str, Any]) -> dict[str, str]:
        prov: dict[str, str] = {}
        for name, value in args.items():
            if not _matchable(value):
                continue
            src = self._find(value)
            if src:
                prov[name] = src
        return prov

    def _find(self, value: Any) -> Optional[str]:
        for step in reversed(self.steps):
            flat: list[tuple[str, Any]] = []
            _flatten(step.result, f"step{step.i}.result", flat)
            for path, v in flat:
                if _same(v, value):
                    return path
        flat = []
        _flatten({k: v for k, v in self.inputs.items() if k != "text"}, "input", flat)
        for path, v in flat:
            if _same(v, value):
                return path
        # Interpreted runs: a value lifted out of the request text is grounded in it.
        text = self.inputs.get("text")
        if isinstance(value, str) and len(value) >= 4 and isinstance(text, str) and value in text:
            return "input.text"
        return None

    def record(self, tool: str, args: dict[str, Any], result: Any, effect_class: str,
               llm_span: Optional[str] = None, cost: float = 0.0, latency: int = 0) -> TraceStep:
        pure = effect_class == "pure"
        step = TraceStep(
            i=len(self.steps) + 1, tool=None if pure else tool, op=tool if pure else None,
            args=args, result=result, result_digest=digest(result), provenance=self.provenance(args),
            effect_class=effect_class, llm_span=llm_span, cost=cost, latency=latency)
        self.steps.append(step)
        return step

    @property
    def signature(self) -> str:
        return ">".join(s.tool for s in self.steps if s.tool)

    def trace(self, verified_success: bool, deopt_event_id: Optional[str] = None) -> Trace:
        return Trace(
            trace_id=self.trace_id, exec_id=self.exec_id, envelope_id=self.envelope_id,
            family=self.family, mode=self.mode, signature=self.signature, steps=self.steps,
            verified_success=verified_success,
            cost_usd=round(sum(s.cost for s in self.steps) + self.extra_cost, 6),
            deopt_event_id=deopt_event_id, ts=datetime.now(timezone.utc))
