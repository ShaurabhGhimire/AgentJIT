"""The runtime loop for one task, and the ablation arms of section 14.2.

handle_task: parse + dispatch -> compiled executor or interpreter -> deopt
handoff (on-stack, or restart for arm C) -> shadow sample -> verify ->
trace, execution, metrics. Costs include parsing and shadow runs, always.
"""
from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agentjit.common import config, llm
from agentjit.common.db import col
from agentjit.compileplane import scheduler, shadow
from agentjit.runtime import deopt, executor
from agentjit.runtime.dispatcher import dispatch
from agentjit.runtime.gateway import Gateway
from agentjit.runtime.interpreter import run_interpreter
from agentjit.runtime.tracer import Tracer
from agentjit.runtime.verifier import verify


@dataclass(frozen=True)
class Arm:
    name: str
    compile: bool = True
    guards: bool = True
    deopt: str = "osr"  # osr | restart
    shadow: bool = True
    label: str = ""


ARMS = {
    "A": Arm("A", compile=False, shadow=False, label="interpreted only"),
    "B": Arm("B", guards=False, shadow=False, deopt="restart", label="plan cache, no guards"),
    "C": Arm("C", deopt="restart", label="AgentJIT, restart on deopt"),
    "D": Arm("D", shadow=False, label="AgentJIT, no shadow testing"),
    "E": Arm("E", label="full AgentJIT"),
}


def _family_of(plan) -> str:
    f = plan.parse.family
    if f is None or f.similarity < config.FAMILY_MIN_SIMILARITY or f.margin < config.FAMILY_MIN_MARGIN:
        return "unknown"
    return f.id


def _store_trace(trace, verified: bool) -> None:
    doc = trace.model_dump(mode="json")
    doc["verified_success"] = verified
    doc["ts"] = datetime.now(timezone.utc)
    col("traces").insert_one(doc)


def handle_task(envelope: dict, rng: random.Random, arm: Arm = ARMS["E"], auto_compile: bool = True) -> dict:
    wall0 = time.monotonic()
    plan = dispatch(envelope, rng, compile_enabled=arm.compile, shadow_enabled=arm.shadow)
    family = _family_of(plan)
    exec_id = f"ex_{uuid.uuid4().hex[:12]}"
    env_id = envelope["envelope_id"]
    rec: dict[str, Any] = {
        "exec_id": exec_id, "envelope_id": env_id, "arm": arm.name, "family": family,
        "route": plan.route, "skill": plan.skill["_id"] if plan.skill else None, "explore": plan.explore,
        "parse_cost": plan.parse.cost_usd, "exec_cost": 0.0, "shadow_cost": 0.0, "latency_ms": 0,
        "deopt": False, "deopt_zone": None, "deopt_guard": None, "fenced": 0, "diverged": None,
        "simulated": not llm.available(),
    }
    traces = []
    compiled_ok = False
    if plan.route == "interpreted":
        tracer = Tracer({**(envelope.get("structured") or {}), "text": envelope["raw_text"]}, family,
                        "interpreted", exec_id, env_id)
        gw = Gateway(exec_id, tracer=tracer)
        r = run_interpreter(envelope, gw)
        rec["exec_cost"] += r.cost_usd
        rec["latency_ms"] += r.latency_ms
        traces.append(tracer)
    else:
        skill = plan.skill
        tracer = Tracer(plan.parse.arg_values(), skill["family"], "compiled", exec_id, env_id)
        gw = Gateway(exec_id, tracer=tracer)
        out = executor.execute(skill, gw, plan.parse.arg_values(), guards_enabled=arm.guards)
        rec["exec_cost"] += tracer.extra_cost
        rec["latency_ms"] += sum(s.latency for s in tracer.steps) + out.ctx.hole_latency
        traces.append(tracer)
        if out.ok:
            compiled_ok = True
        else:
            d = out.deopt
            rec.update(deopt=True, deopt_guard=d.guard, deopt_pc=d.pc)
            if arm.deopt == "restart":
                # Arm C: start over under a new execution id, so the journal cannot fence.
                rec["deopt_zone"] = "restart"
                exec2 = f"{exec_id}_restart"
                t2 = Tracer({**(envelope.get("structured") or {}), "text": envelope["raw_text"]}, family,
                            "interpreted", exec2, env_id)
                r = run_interpreter(envelope, Gateway(exec2, tracer=t2))
                traces.append(t2)
            else:
                frame, event_id, r, cont = deopt.handoff(out.skill or skill, out.ctx, d, exec_id, envelope, gw)
                rec["deopt_zone"] = "osr" if frame.committed_effects else "safe"
                rec["deopt_event"] = event_id
                rec["committed_before_deopt"] = len(frame.committed_effects)
                cont.deopt_zone = rec["deopt_zone"]
                traces.append(cont)
            rec["exec_cost"] += r.cost_usd
            rec["latency_ms"] += r.latency_ms
        rec["fenced"] = gw.stats.fenced + gw.stats.resource_blocked
        if compiled_ok and plan.shadow:
            sh = shadow.run_shadow(envelope, skill, exec_id, gw.reads, tracer.steps)
            rec["shadow_cost"] = sh["cost_usd"]
            rec["diverged"] = sh["diverged"]
            rec["adjudication"] = sh["adjudication"]
            rec["gate"] = sh["gate"]

    v = verify(env_id)
    rec.update(verified=v.ok, verify_reason=v.reason, expected=v.expected, duplicate_effects=v.duplicate_effects,
               silent_error=compiled_ok and not v.ok)
    for t in traces:
        tr = t.trace(v.ok, deopt_event_id=rec.get("deopt_event") if t.trace_id.endswith("_cont") else None)
        if t.trace_id.endswith("_cont"):
            tr.deopt_zone = rec["deopt_zone"]
        _store_trace(tr, v.ok)
    rec["cost_usd"] = round(rec["parse_cost"] + rec["exec_cost"] + rec["shadow_cost"], 6)
    if not llm.available():
        rec["latency_ms"] += 40  # parse + dispatch overhead, nominal
    else:
        rec["latency_ms"] = int((time.monotonic() - wall0) * 1000)
    rec["ts"] = datetime.now(timezone.utc)
    col("executions").insert_one(dict(rec))
    col("metrics").insert_one({
        "ts": rec["ts"], "meta": {"arm": arm.name, "family": family, "skill": rec["skill"]},
        "route": rec["route"], "cost_usd": rec["cost_usd"], "parse_cost": rec["parse_cost"],
        "shadow_cost": rec["shadow_cost"], "latency_ms": rec["latency_ms"], "deopt": rec["deopt"],
        "deopt_zone": rec["deopt_zone"], "diverged": rec["diverged"], "verified": v.ok,
        "duplicate_effects": v.duplicate_effects, "silent_error": rec["silent_error"],
        "fenced": rec["fenced"], "simulated": rec["simulated"]})
    if auto_compile and arm.compile and family != "unknown":
        rec["scheduler"] = scheduler.after_task(family, rec)
    return rec
