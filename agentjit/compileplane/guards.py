"""Guard inference, Daikon-lite (section 8.5). The LLM never writes a guard.

Observed values are collected at every binding the template creates, across
the source traces, and checked against a small template library:

- set membership   low-cardinality string fields     order['currency'] in ['USD']
- length           constant list lengths             len(shipments) == 1
- numeric range    only where evidence marks a       order['days_since_delivery'] <= 29
                   boundary: a divergent trace of the
                   same family lies outside the range
- ordering         amount vs order money fields      0 < amount <= order['total']
- post / residual  single-valued status fields of    refund['status'] == 'succeeded'
                   a write step's result

Every guard carries its support (the number of traces it held on). Bounds are
the observed extremes, never rounded outward: the asymmetry rule (section 4.4)
prefers an unnecessary deopt to unvalidated execution.

Divergence stumps: where a divergent trace shares the prefix and then branches,
a one-feature decision stump finds the predicate separating the branches.
"""
from __future__ import annotations

from typing import Any, Optional

from agentjit.common import config
from agentjit.compileplane.generalizer import step_name


def _bindings(trace: dict, template: dict) -> dict[str, tuple[int, Any]]:
    """binding name -> (pc that produced it, value) for one trace."""
    out = {}
    for s, rec in zip(template["steps"], trace["steps"]):
        if not s["bind"]:
            continue
        value = rec["result"]
        if s["unwrap"] is not None:
            value = value[s["unwrap"]]
        out[s["bind"]] = (s["pc"], value)
    return out


def _features(bindings: dict[str, tuple[int, Any]]) -> dict[str, tuple[int, Any]]:
    """Flatten bindings into guard-expression -> (pc, value)."""
    feats: dict[str, tuple[int, Any]] = {}
    for name, (pc, v) in bindings.items():
        if isinstance(v, dict):
            for k, fv in v.items():
                if isinstance(fv, (str, int, float)) and not isinstance(fv, bool):
                    feats[f"{name}[{k!r}]"] = (pc, fv)
        elif isinstance(v, list):
            feats[f"len({name})"] = (pc, len(v))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            feats[name] = (pc, v)
    return feats


def _prefix_features(trace: dict, template: dict) -> dict[str, Any]:
    """Features of a divergent trace over the steps it shares with the template."""
    shared = []
    for s, rec in zip(template["steps"], trace["steps"]):
        if step_name(rec) != s["name"]:
            break
        shared.append(s)
    sub = {**template, "steps": shared}
    return {k: v for k, (_, v) in _features(_bindings(trace, sub)).items()}


def infer(template: dict, traces: list[dict], divergent: Optional[list[dict]] = None) -> list[dict]:
    divergent = divergent or []
    n = len(traces)
    per_trace = [_features(_bindings(t, template)) for t in traces]
    div_feats = [_prefix_features(t, template) for t in divergent]
    write_pcs = {s["pc"] for s in template["steps"] if s["effect"] not in ("read", "pure")}
    guards: list[dict] = []
    if n < config.GUARD_MIN_SUPPORT:
        return guards

    for feat in sorted(per_trace[0]):
        if not all(feat in f for f in per_trace):
            continue
        pc = per_trace[0][feat][0]
        values = [f[feat][1] for f in per_trace]
        is_post = pc in write_pcs

        if is_post:
            # residual: single-valued status fields of a side effect's result
            if feat.endswith("['status']") and len(set(values)) == 1:
                guards.append({"expr": f"{feat} == {values[0]!r}", "support": n, "pc": pc, "post": True})
            continue

        if all(isinstance(v, str) for v in values):
            distinct = sorted(set(values))
            if len(distinct) <= max(1, int(n * config.GUARD_MAX_DISTINCT_FRACTION)):
                guards.append({"expr": f"{feat} in {distinct!r}", "support": n, "pc": pc})
            continue

        if feat.startswith("len("):
            if len(set(values)) == 1:
                guards.append({"expr": f"{feat} == {values[0]}", "support": n, "pc": pc})
            continue

        # numeric range, only with boundary evidence from divergent traces
        lo, hi = min(values), max(values)
        outside = [d[feat] for d in div_feats if feat in d and isinstance(d[feat], (int, float))]
        if any(v > hi for v in outside):
            guards.append({"expr": f"{feat} <= {hi}", "support": n, "pc": pc})
        if any(v < lo for v in outside):
            guards.append({"expr": f"{feat} >= {lo}", "support": n, "pc": pc})

    guards += _ordering(template, per_trace, n)
    return guards


def _ordering(template: dict, per_trace: list[dict], n: int) -> list[dict]:
    """0 < amount <= order['total'] style relations between a computed scalar and an input field."""
    out = []
    scalars = [s["bind"] for s in template["steps"] if s["unwrap"] is not None]
    for sc in scalars:
        if not all(sc in f for f in per_trace):
            continue
        pc = per_trace[0][sc][0]
        for feat in sorted(per_trace[0]):
            if feat == sc or not feat.endswith("['total']"):
                continue
            if all(0 < f[sc][1] <= f[feat][1] for f in per_trace if feat in f):
                out.append({"expr": f"0 < {sc} <= {feat}", "support": n, "pc": max(pc, per_trace[0][feat][0])})
    return out


def stump(template: dict, traces: list[dict], divergent: list[dict]) -> Optional[dict]:
    """Best single-feature predicate that is true on the template's traces and false on the divergent ones."""
    pos = [_prefix_features(t, template) for t in traces]
    neg = [_prefix_features(t, template) for t in divergent]
    if not neg:
        return None
    best = None
    for feat in sorted(pos[0]):
        pv = [p[feat] for p in pos if feat in p]
        nv = [q[feat] for q in neg if feat in q]
        if not pv or not nv:
            continue
        if all(isinstance(v, str) for v in pv + nv):
            allowed = sorted(set(pv))
            if len(allowed) > max(1, int(len(pv) * config.GUARD_MAX_DISTINCT_FRACTION)):
                continue  # identifier-like: separates by memorizing, not by a rule
            pred = f"{feat} in {allowed!r}"
            correct = len(pv) + sum(1 for v in nv if v not in allowed)
        elif all(isinstance(v, (int, float)) for v in pv + nv):
            hi = max(pv)
            pred = f"{feat} <= {hi}"
            correct = len(pv) + sum(1 for v in nv if v > hi)
        else:
            continue
        score = correct / (len(pv) + len(nv))
        if best is None or score > best["accuracy"]:
            best = {"expr": pred, "accuracy": round(score, 3), "support": len(pv) + len(nv)}
    return best
