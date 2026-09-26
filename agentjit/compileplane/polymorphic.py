"""Polymorphic recompilation from deopt continuations (section 9, inline-cache states).

When an active skill keeps deopting in its safe zone, the interpreter's
continuations for those tasks are whole-task traces of another path. Once
enough of them share a signature, that path is compiled as a branch. The new
polymorphic version tries branches in order; a branch whose entry guards fail
before any effect hands over to the next one, and only when every branch
rejects the task does it deopt to the interpreter.

Safety rule: a new branch is accepted only if its entry guards reject every
source trace of the existing branches. Otherwise it could capture tasks the
old path owns (for example, escalate an order that should be refunded).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from agentjit.common import config, embed
from agentjit.common.db import col
from agentjit.common.skillcode import eval_guard
from agentjit.compileplane import codegen, gate, guards, hoist, replay, scheduler_hooks
from agentjit.compileplane.generalizer import GeneralizeError, generalize


def _branches(skill: dict) -> list[str]:
    return skill.get("branches") or [skill["_id"]]


def _source_traces_of(branch: dict) -> list[dict]:
    ids = branch.get("template", {}).get("trace_ids", [])
    return list(col("traces").find({"trace_id": {"$in": ids}}, {"_id": 0}))


def _rejects_all(entry: list[str], template: dict, traces: list[dict]) -> bool:
    for t in traces:
        b = {}
        for s, rec in zip(template["steps"], t["steps"]):
            if (rec.get("tool") or rec.get("op")) != s["name"] or not s["bind"]:
                break
            b[s["bind"]] = rec["result"][s["unwrap"]] if s["unwrap"] else rec["result"]
        try:
            if all(eval_guard(g, b) is True for g in entry):
                return False
        except Exception:
            continue  # a guard that cannot evaluate on this path rejects it
    return True


def maybe_add_branch(family: str, active_id: str) -> Optional[dict]:
    fam = col("task_families").find_one({"family_id": family}) or {}
    if fam.get("probation_skill"):
        return {"polymorphic": False, "reason": f"{fam['probation_skill']} is still in probation"}
    active = col("skills").find_one({"_id": active_id})
    events = list(col("deopt_events").find({"skill": {"$in": _branches(active) + [active_id]}, "zone": "safe"},
                                           {"continuation_trace": 1}))
    cont_ids = [e["continuation_trace"] for e in events if e.get("continuation_trace")]
    conts = list(col("traces").find({"trace_id": {"$in": cont_ids}, "verified_success": True}, {"_id": 0}))
    by_sig: dict[str, list[dict]] = {}
    for t in conts:
        by_sig.setdefault(t["signature"], []).append(t)
    need = max(config.RECOMPILE_MIN_CONTINUATIONS, config.GUARD_MIN_SUPPORT)
    ready = [(sig, ts) for sig, ts in by_sig.items() if len(ts) >= need]
    if not ready:
        return {"polymorphic": False, "reason": f"waiting for {need} continuations of one path",
                "have": {s: len(t) for s, t in by_sig.items()}}
    sig, traces = max(ready, key=lambda kv: len(kv[1]))

    existing = [col("skills").find_one({"_id": b}) for b in _branches(active)]
    if any(b["template"]["signature"] == sig for b in existing if b.get("template")):
        return {"polymorphic": False, "reason": "path already compiled"}
    if len(existing) + 1 > config.POLYMORPHIC_K:
        scheduler_hooks.megamorphic(family, active_id, f"more than {config.POLYMORPHIC_K} branches needed")
        return {"polymorphic": False, "megamorphic": True}

    try:
        template = generalize(traces)
    except GeneralizeError as e:
        return {"polymorphic": False, "reason": str(e)}
    others = [t for b in existing for t in _source_traces_of(b)]
    inferred = guards.infer(template, traces, others)
    split = hoist.hoist(template, inferred)
    entry = [g["expr"] for g in split["entry_guards"]]
    st = guards.stump(template, traces, others)
    if not _rejects_all(entry, template, others) and st and st["accuracy"] == 1.0:
        split["entry_guards"].append({"expr": st["expr"], "support": st["support"], "kind": "state", "stump": True})
        entry.append(st["expr"])
    if not entry or not _rejects_all(entry, template, others):
        return {"polymorphic": False, "reason": "no guard separates the new path from the existing ones"}

    name = active["name"]
    version = (col("skills").find_one({"name": name}, sort=[("version", -1)])["version"]) + 1
    branch_id = f"{name}@v{version}.b{len(existing) + 1}"
    source, method, cost = None, None, 0.0
    feedback = ""
    for _ in range(config.CODEGEN_MAX_ATTEMPTS):
        draft, c, method = codegen.draft(template, branch_id, feedback)
        cost += c
        bad = [r for r in replay.replay_all(draft, traces, template) if not r.ok]
        if not bad:
            source = draft
            break
        feedback = "\n".join(f"{r.trace_id}: {r.diff}" for r in bad[:5])
    if source is None:
        return {"polymorphic": False, "reason": f"branch replay failed: {feedback}"}
    code_ref = codegen.write(name, f"{version}_b{len(existing) + 1}", source)
    rel = str(code_ref).replace(str(config.SKILLS_DIR).rsplit("/", 1)[0] + "/", "")
    branch = {
        "_id": branch_id, "name": name, "version": version, "family": family, "status": "branch",
        "shape": "monomorphic", "parent": active_id, "input_signature": active["input_signature"],
        "task_guards": [], "entry_guards": split["entry_guards"],
        "steps": [{"pc": s["pc"], **({"op": s["name"]} if s["effect"] == "pure" else {"tool": s["name"]}),
                   "effect": s["effect"], "post": split["residual"].get(s["pc"], [])} for s in template["steps"]],
        "holes": template["holes"], "hoist": {k: v for k, v in split.items() if k != "residual"},
        "code_ref": rel, "template": template, "codegen": {"method": method, "cost_usd": cost},
        "created_at": datetime.now(timezone.utc)}
    col("skills").insert_one(branch)
    poly_id = f"{name}@v{version}"
    poly = {
        "_id": poly_id, "name": name, "version": version, "family": family, "status": "candidate",
        "shape": "polymorphic", "parent": active_id, "branches": _branches(active) + [branch_id],
        "input_signature": active["input_signature"], "task_guards": active.get("task_guards", []),
        "entry_guards": [], "steps": active["steps"], "holes": [], "code_ref": "",
        "shadow": {"runs": 0, "divergences": 0, "upper_bound_95": 1.0, "window": [], "active_runs": 0},
        "embedding": embed.embed_one(f"{family} polymorphic"), "created_at": datetime.now(timezone.utc)}
    col("skills").insert_one(poly)
    gate.enter_probation(poly_id)
    return {"polymorphic": True, "skill": poly_id, "new_branch": branch_id, "branch_guards": entry}
