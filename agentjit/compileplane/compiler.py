"""The compile pipeline: profile -> generalize -> draft -> replay -> guards -> hoist -> probation.

Only codegen uses an LLM, and its output is mechanically verified; every other
stage is deterministic. Code is drafted from a training split and must also
replay the held-out traces (section 8.7 step 2). Guards are inferred over all
source traces: the held-out check is about the code generalizing, and the
asymmetry rule already keeps guards tight.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from agentjit.common import config, embed
from agentjit.common.db import col
from agentjit.compileplane import codegen, gate, guards, hoist, profiler, replay
from agentjit.compileplane.generalizer import GeneralizeError, generalize

SKILL_NAMES = {"refund_request": "refund_standard"}


def skill_name(family: str) -> str:
    return SKILL_NAMES.get(family, f"{family}_standard")


def next_version(name: str) -> int:
    latest = col("skills").find_one({"name": name}, sort=[("version", -1)])
    return (latest["version"] + 1) if latest else 1


def compile_family(family: str, since: Optional[datetime] = None, force_signature: Optional[str] = None) -> dict:
    profiler.refresh()
    hot = profiler.hot_segments(family)
    if since is not None or force_signature:
        # After a demotion only post-change evidence counts; recount hotness over it.
        sig = force_signature or (hot[0]["signature"] if hot else None)
        cands = {}
        for t in col("traces").find({"family": family, "mode": "interpreted", "verified_success": True,
                                     "deopt_zone": {"$ne": "osr"}, **({"ts": {"$gte": since}} if since else {})},
                                    {"signature": 1}):
            cands[t["signature"]] = cands.get(t["signature"], 0) + 1
        total = sum(cands.values())
        if not cands:
            return {"compiled": False, "reason": "no traces since the change"}
        sig, count = max(cands.items(), key=lambda kv: kv[1]) if not force_signature else (sig, cands.get(sig, 0))
        if count < config.HOTNESS_THRESHOLD or count / total < config.STABILITY_THRESHOLD:
            return {"compiled": False, "reason": f"not hot/stable yet: {count}/{total}"}
    elif not hot:
        return {"compiled": False, "reason": "no hot, stable segment"}
    else:
        sig = hot[0]["signature"]

    traces = profiler.source_traces(family, sig, since)
    divergent = profiler.divergent_traces(family, sig, since)
    n_held = max(1, int(len(traces) * config.HELD_OUT_FRACTION))
    train, held = traces[:-n_held], traces[-n_held:]
    try:
        template = generalize(train)
    except GeneralizeError as e:
        return {"compiled": False, "reason": str(e)}

    name = skill_name(family)
    version = next_version(name)
    skill_id = f"{name}@v{version}"
    feedback, source, method, cost = "", None, None, 0.0
    for attempt in range(1, config.CODEGEN_MAX_ATTEMPTS + 1):
        draft, c, method = codegen.draft(template, skill_id, feedback)
        cost += c
        results = replay.replay_all(draft, train, template)
        bad = [r for r in results if not r.ok]
        if not bad:
            source = draft
            break
        feedback = "\n".join(f"{r.trace_id}: {r.diff}" for r in bad[:5])
    if source is None:
        _record_rejection(skill_id, name, version, family, f"replay failed after {attempt} drafts: {feedback}")
        return {"compiled": False, "reason": "replay failed", "detail": feedback}

    inferred = guards.infer(template, traces, divergent)
    split = hoist.hoist(template, inferred)
    entry_exprs = [g["expr"] for g in split["entry_guards"]]
    held_results = replay.replay_all(source, held, template, entry_exprs)
    if not all(r.ok for r in held_results):
        detail = "; ".join(r.diff for r in held_results if not r.ok)
        _record_rejection(skill_id, name, version, family, f"held-out replay failed: {detail}")
        return {"compiled": False, "reason": "held-out replay failed", "detail": detail}

    code_ref = codegen.write(name, version, source)
    fam = col("task_families").find_one({"family_id": family}) or {}
    parent = fam.get("active_skill") or _last_version(name)
    doc = {
        "_id": skill_id, "name": name, "version": version, "family": family, "status": "candidate",
        "shape": "monomorphic", "parent": parent,
        "input_signature": {k: {"type": v["type"], "extractor": f"pattern:{k}@v1"} for k, v in template["inputs"].items()},
        "task_guards": split["task_guards"], "entry_guards": split["entry_guards"],
        "steps": [{"pc": s["pc"], **({"op": s["name"]} if s["effect"] == "pure" else {"tool": s["name"]}),
                   "effect": s["effect"], "post": split["residual"].get(s["pc"], []),
                   **({"hole": h["name"]} if (h := next((h for h in template["holes"] if h["pc"] == s["pc"]), None)) else {})}
                  for s in template["steps"]],
        "holes": template["holes"], "hoist": {k: v for k, v in split.items() if k not in ("residual",)},
        "shadow": {"runs": 0, "divergences": 0, "upper_bound_95": 1.0, "window": [], "active_runs": 0},
        "code_ref": str(code_ref).replace(str(config.SKILLS_DIR.rsplit("/", 1)[0]) + "/", ""),
        "template": template, "codegen": {"method": method, "attempts": attempt, "cost_usd": cost},
        "source_traces": len(train), "held_out": len(held), "stump": guards.stump(template, traces, divergent),
        "embedding": embed.embed_one(f"{family} {template['signature']}"),
        "created_at": datetime.now(timezone.utc), "compiled_since": since,
    }
    col("skills").insert_one(doc)
    gate.enter_probation(skill_id)
    return {"compiled": True, "skill": skill_id, "guards": [g["expr"] for g in split["entry_guards"]],
            "residual": split["residual"], "method": method}


def _last_version(name: str) -> Optional[str]:
    last = col("skills").find_one({"name": name, "status": {"$in": ["active", "retired"]}}, sort=[("version", -1)])
    return last["_id"] if last else None


def _record_rejection(skill_id: str, name: str, version: int, family: str, reason: str) -> None:
    col("skills").insert_one({"_id": skill_id, "name": name, "version": version, "family": family,
                              "status": "rejected", "rejected_reason": reason, "steps": [], "code_ref": "",
                              "created_at": datetime.now(timezone.utc)})
