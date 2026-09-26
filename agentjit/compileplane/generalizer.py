"""Generalizer: anti-unification of same-signature traces into a template (section 8.3).

For every (step, argument) position across the source traces:
- all values agree                      -> constant
- values differ, one provenance source
  resolves to the observed value in
  every trace                           -> parameter bound to that source
- values differ, no source explains them -> typed LLM hole

Provenance recorded by the tracer is only a candidate; the generalizer checks
each candidate against every trace before trusting it (it is value-matched,
so trivial values such as a 0 discount carry no provenance of their own).
"""
from __future__ import annotations

import re
from typing import Any, Optional

from agentjit.common.tools import get_effect_class

# Binding names from the ABI guard-namespace table; pure ops bind their single result field.
BIND_NAMES = {
    "get_order": "order",
    "get_shipments": "shipments",
    "payments.list_refunds": "refunds",
    "compute_amount": "amount",
    "payments.refund": "refund",
    "tickets.update": "ticket",
}

_PATH_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


class GeneralizeError(Exception):
    pass


def step_name(step: dict) -> str:
    return step.get("tool") or step.get("op")


def resolve(path: str, trace: dict) -> Any:
    """Value at a provenance path like step1.result.charge_id or step2.result[0].id."""
    m = re.match(r"step(\d+)\.result(.*)", path)
    if not m:
        raise KeyError(path)
    value: Any = trace["steps"][int(m.group(1)) - 1]["result"]
    for key, idx in _PATH_TOKEN.findall(m.group(2)):
        value = value[key] if key else value[int(idx)]
    return value


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def _type_of(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    return "string"


def generalize(traces: list[dict]) -> dict:
    if len(traces) < 2:
        raise GeneralizeError("need at least two traces to generalize")
    shape = [step_name(s) for s in traces[0]["steps"]]
    for t in traces[1:]:
        if [step_name(s) for s in t["steps"]] != shape:
            raise GeneralizeError(f"trace {t['trace_id']} has a different step sequence")

    steps: list[dict] = []
    inputs: dict[str, dict] = {}
    holes: list[dict] = []
    for idx, name in enumerate(shape):
        pc = idx + 1
        eff = get_effect_class(name)
        arg_names = sorted({k for t in traces for k in t["steps"][idx]["args"]})
        args: dict[str, dict] = {}
        for arg in arg_names:
            values = [t["steps"][idx]["args"].get(arg) for t in traces]
            if all(_equal(v, values[0]) for v in values):
                args[arg] = {"kind": "const", "value": values[0]}
                continue
            spec = _param_spec(traces, idx, arg, values)
            if spec is not None:
                args[arg] = spec
                if spec["source"] == "input":
                    inputs.setdefault(spec["input"], {"type": _type_of(values[0])})
                continue
            hole = f"{BIND_NAMES.get(name) or name.split('.')[0]}_{arg}"
            args[arg] = {"kind": "hole", "hole": hole}
            holes.append({"name": hole, "pc": pc, "arg": arg, "tool": name,
                          "schema": {"type": "string", "maxLength": max(1200, max(len(str(v)) for v in values) * 2)},
                          })
        bind = BIND_NAMES.get(name)
        unwrap = None
        if eff == "pure":
            results = [t["steps"][idx]["result"] for t in traces]
            if all(isinstance(r, dict) and len(r) == 1 for r in results):
                unwrap = next(iter(results[0]))
                bind = unwrap
        steps.append({"pc": pc, "name": name, "effect": eff, "args": args, "bind": bind, "unwrap": unwrap})

    for h in holes:
        h["checks"] = _infer_checks(traces, h, inputs, steps)
        h["inputs"] = sorted(inputs) + [s["bind"] for s in steps[: h["pc"] - 1]
                                        if s["unwrap"] is not None]
        h["examples"] = [{"text": str(t["steps"][h["pc"] - 1]["args"][h["arg"]]),
                          "inputs": {name: _hole_input(t, name, inputs, steps) for name in h["inputs"]}}
                         for t in traces[:3]]
    return {
        "family": traces[0]["family"],
        "signature": traces[0]["signature"],
        "trace_ids": [t["trace_id"] for t in traces],
        "n": len(traces),
        "inputs": inputs,
        "steps": steps,
        "holes": holes,
    }


def _param_spec(traces: list[dict], idx: int, arg: str, values: list[Any]) -> Optional[dict]:
    candidates: dict[str, int] = {}
    for t in traces:
        src = t["steps"][idx].get("provenance", {}).get(arg)
        if src:
            candidates[src] = candidates.get(src, 0) + 1
    # Task inputs: every trace must have grounded this position in the request.
    if all((t["steps"][idx].get("provenance", {}).get(arg) or "").startswith("input.") for t in traces):
        return {"kind": "param", "source": "input", "input": arg}
    for src, _ in sorted(candidates.items(), key=lambda kv: -kv[1]):
        if src.startswith("input."):
            continue
        try:
            if all(_equal(resolve(src, t), v) for t, v in zip(traces, values)):
                return {"kind": "param", "source": "step", "path": src}
        except (KeyError, IndexError, TypeError):
            continue
    return None


def _infer_checks(traces: list[dict], hole: dict, inputs: dict, steps: list[dict]) -> list[str]:
    """A hole must mention a parameter if every observed output mentioned it."""
    checks = []
    idx = hole["pc"] - 1
    for name in sorted(inputs):
        spec = steps[idx]["args"]
        ok = True
        for t in traces:
            value = _input_value(t, name, steps)
            if value is None or str(value) not in str(t["steps"][idx]["args"].get(hole["arg"], "")):
                ok = False
                break
        if ok:
            checks.append(f"mentions({name})")
    return checks


def _hole_input(trace: dict, name: str, inputs: dict, steps: list[dict]) -> Any:
    if name in inputs:
        return _input_value(trace, name, steps)
    for s, rec in zip(steps, trace["steps"]):
        if s["bind"] == name and s["unwrap"] is not None:
            return rec["result"][s["unwrap"]]
    return None


def _input_value(trace: dict, name: str, steps: list[dict]) -> Any:
    for s, step in zip(steps, trace["steps"]):
        spec = s["args"].get(name)
        if spec and spec["kind"] == "param" and spec["source"] == "input":
            return step["args"].get(name)
    return None
