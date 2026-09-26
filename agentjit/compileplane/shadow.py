"""Differential shadow testing (section 7.6).

Owner: Susan (T3.1 in her P3 track).

A sampled compiled execution is re-run through the interpreter with every
write stubbed. Reads the compiled run made are replayed from its snapshot, so
the shadow sees the same world state; anything the compiled run never read
(including the policy, which lives in the interpreter's context) is live.
The two sets of intended effects are compared after normalization, hole
content excluded. A divergence goes to the hard verifier for adjudication.
"""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

from agentjit.common.db import col
from agentjit.compileplane import gate
from agentjit.runtime.gateway import Gateway, normalize
from agentjit.runtime.interpreter import run_interpreter
from agentjit.runtime.tracer import Tracer
from agentjit.runtime.verifier import verify

# Which arguments identify an effect for comparison; the rest (email prose) is hole content.
EFFECT_IDENTITY = {
    "payments.refund": ("charge_id", "amount"),
    "tickets.update": ("ticket_id", "status"),
    "email.send": ("to",),
}


def effect_signature(tool: str, args: dict[str, Any]) -> tuple:
    keys = EFFECT_IDENTITY.get(tool)
    norm = normalize(args)
    if keys is None:
        return (tool, tuple(sorted(norm.items())))
    return (tool, tuple((k, norm.get(k)) for k in keys))


def compiled_effects(trace_steps: list) -> list[tuple]:
    out = []
    for s in trace_steps:
        tool = s.tool if hasattr(s, "tool") else s.get("tool")
        eff = s.effect_class if hasattr(s, "effect_class") else s.get("effect_class")
        args = s.args if hasattr(s, "args") else s.get("args")
        if tool and eff not in ("read", "pure"):
            out.append(effect_signature(tool, args))
    return out


def run_shadow(envelope: dict, skill: dict, exec_id: str, reads: dict, compiled_steps: list,
               adjudicate: bool = True) -> dict:
    shadow_exec = f"{exec_id}-shadow"
    tracer = Tracer({**(envelope.get("structured") or {}), "text": envelope["raw_text"]}, skill["family"],
                    "shadow", shadow_exec, envelope["envelope_id"])
    gw = Gateway(shadow_exec, mode="shadow", tracer=tracer, read_snapshot=reads)
    result = run_interpreter(envelope, gw)
    ours = Counter(compiled_effects(compiled_steps))
    theirs = Counter(effect_signature(t, a) for t, a in gw.intended)
    diverged = ours != theirs
    adjudication = "agree"
    verdict = None
    if diverged:
        if adjudicate:
            v = verify(envelope["envelope_id"])
            verdict = v.model_dump()
            adjudication = "interpreter_wrong" if v.ok else "skill_wrong"
        else:
            adjudication = "unadjudicated"
    doc = {
        "shadow_id": f"sh_{uuid.uuid4().hex[:10]}", "exec_id": exec_id, "skill": skill["_id"],
        "envelope_id": envelope["envelope_id"], "diverged": diverged,
        "effect_diff": {"compiled_only": [list(map(str, k)) for k in (ours - theirs)],
                        "interpreter_only": [list(map(str, k)) for k in (theirs - ours)]},
        "adjudication": adjudication, "verifier": verdict, "cost_usd": result.cost_usd,
        "simulated": result.simulated, "ts": datetime.now(timezone.utc)}
    col("shadow_runs").insert_one(dict(doc))
    col("traces").insert_one(tracer.trace(False).model_dump(mode="json") | {"ts": datetime.now(timezone.utc)})
    doc["gate"] = gate.record_shadow(skill["_id"], diverged, adjudication)
    return doc
