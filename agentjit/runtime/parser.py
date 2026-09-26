"""Task parser: section 7.1 stages 1-4 (envelope, family match, extraction, grounding).

Owner: Saurav (T2.1 in his P2 track).
The governing rule: any parsing failure routes to the interpreter. A task is
never rejected because parsing was unsure.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agentjit.common import config, embed, llm
from agentjit.common.db import col
from agentjit.common.models import ArgDecision, FamilyMatch, TaskGuardResult
from agentjit.common.skillcode import eval_guard
from agentjit.compileplane.families import match_family

# Hand-written pattern extractors (T2.1). Two independent methods per argument
# so the agreement check has a pair from day one; compiled extractors (8.8)
# would replace "pattern" later without changing this contract.
EXTRACTORS: dict[str, dict[str, re.Pattern]] = {
    "order_id": {
        "pattern": re.compile(r"\border\s*(?:#|no\.?|number)?\s*([A-Z]\d{4})\b", re.I),
        "shape": re.compile(r"\b([A-Z]\d{4})\b"),
    },
}


@dataclass
class ParseResult:
    envelope_id: str
    family: Optional[FamilyMatch]
    servable: bool
    args: dict[str, ArgDecision] = field(default_factory=dict)
    task_guards: TaskGuardResult = field(default_factory=lambda: TaskGuardResult(passed=0, failed=0))
    eligible: bool = False
    reason: str = ""
    cost_usd: float = 0.0
    simulated: bool = True

    def arg_values(self) -> dict[str, Any]:
        return {k: v.value for k, v in self.args.items()}


def store_envelope(envelope: dict) -> None:
    col("task_envelopes").update_one({"envelope_id": envelope["envelope_id"]}, {"$setOnInsert": envelope}, upsert=True)


def _grounded(value: Any, envelope: dict) -> bool:
    if value is None:
        return False
    if str(value) in envelope["raw_text"]:
        return True
    return any(str(value) == str(v) for v in (envelope.get("structured") or {}).values())


def _pattern(name: str, method: str, text: str) -> Optional[str]:
    rx = EXTRACTORS.get(name, {}).get(method)
    if rx is None:
        return None
    found = {m.group(1).upper() for m in rx.finditer(text)}
    return found.pop() if len(found) == 1 else None  # ambiguity is a failure, not a guess


def _llm_extract(name: str, spec: dict, envelope: dict) -> tuple[Optional[Any], float]:
    if not llm.available():
        return None, 0.0
    schema = {"type": "object", "properties": {name: {"type": spec.get("type", "string")}},
              "required": [name], "additionalProperties": False}
    resp, usage = llm.create(
        config.EXTRACTION_MODEL, max_tokens=256,
        messages=[{"role": "user", "content": f"Extract `{name}` from this request. Use null if absent.\n\n{envelope['raw_text']}"}],
        output_config={"format": {"type": "json_schema", "schema": schema}})
    try:
        return json.loads(llm.text_of(resp)).get(name), usage.cost_usd
    except (ValueError, AttributeError):
        return None, usage.cost_usd


def extract(name: str, spec: dict, envelope: dict, need_agreement: bool) -> tuple[Optional[ArgDecision], float, str]:
    """The extraction ladder. Returns (decision or None, cost, failure reason)."""
    cost = 0.0
    structured = (envelope.get("structured") or {}).get(name)
    candidates: list[tuple[str, Any]] = []
    if structured is not None:
        candidates.append(("structured", structured))
    for method in ("pattern", "shape"):
        v = _pattern(name, method, envelope["raw_text"])
        if v is not None:
            candidates.append((method, v))
    if len(candidates) < (2 if need_agreement else 1):
        v, c = _llm_extract(name, spec, envelope)
        cost += c
        if v is not None:
            candidates.append(("llm_schema", v))
        elif not llm.available() and len(candidates) < 1:
            cost += 0.0
    if not candidates:
        return None, cost, f"could not extract {name}"
    method, value = candidates[0]
    if not _grounded(value, envelope):
        return None, cost, f"{name}={value!r} is not grounded in the request"
    agreement = None
    if need_agreement:
        if len(candidates) < 2:
            return None, cost, f"{name}: only one extraction method succeeded"
        agreement = str(candidates[0][1]) == str(candidates[1][1])
        if not agreement:
            return None, cost, f"{name}: methods disagree ({candidates[0]} vs {candidates[1]})"
    label = "pattern" if method == "shape" else method
    return ArgDecision(value=value, method=label, grounded=True, agreement=agreement), cost, ""


def parse(envelope: dict, resolve_skill: Callable[[str], Optional[dict]]) -> tuple[ParseResult, Optional[dict]]:
    """resolve_skill(family_id) picks the candidate skill for the matched family."""
    store_envelope(envelope)
    vec = embed.embed_one(envelope["raw_text"])
    cost = embed.embedding_cost([envelope["raw_text"]])
    fam, servable = match_family(envelope["raw_text"], envelope.get("tenant", "acme"), vec)
    res = ParseResult(envelope["envelope_id"], fam, servable, cost_usd=cost, simulated=not llm.available())
    if fam is None or fam.similarity < config.FAMILY_MIN_SIMILARITY:
        res.reason = "no family"
        return res, None
    if fam.margin < config.FAMILY_MIN_MARGIN:
        res.reason = "ambiguous family"
        return res, None
    skill = resolve_skill(fam.id) if servable else None
    if skill is None:
        res.reason = "family has no servable skill"
        return res, None

    irreversible = any(s.get("effect") == "irreversible" for s in skill["steps"])
    for name, spec in skill.get("input_signature", {}).items():
        decision, c, why = extract(name, spec, envelope, need_agreement=irreversible)
        res.cost_usd += c
        if decision is None:
            res.reason = why
            return res, skill
        res.args[name] = decision

    passed = failed = 0
    for g in skill.get("task_guards", []):
        try:
            ok = eval_guard(g["expr"], res.arg_values()) is True
        except Exception:
            ok = False
        passed, failed = passed + ok, failed + (not ok)
    res.task_guards = TaskGuardResult(passed=passed, failed=failed)
    if failed:
        res.reason = "task-level guard failed"
        return res, skill
    res.eligible = True
    return res, skill
