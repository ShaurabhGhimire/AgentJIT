"""On-stack deoptimization: frame materialization and handoff (section 7.5).

Owner: Saurav (T2.4 in his P2 track). This is the contribution.

On a guard failure the executor's state becomes a DeoptFrame: pc, the failed
guard and what was observed, locals, every effect this execution already
committed (read from the journal, not from executor memory), and the goal that
remains. The frame is persisted to deopt_events, rendered into the
interpreter's context, and the interpreter resumes under the *same exec_id*,
so every repeat of a committed effect hits the journal and is fenced.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from agentjit.common.db import col
from agentjit.common.models import CommittedEffect, DeoptFrame
from agentjit.runtime import journal
from agentjit.runtime.executor import DeoptSignal, SkillContext
from agentjit.runtime.gateway import Gateway
from agentjit.runtime.interpreter import InterpResult, run_interpreter
from agentjit.runtime.tracer import Tracer


def materialize(skill: dict, ctx: SkillContext, signal: DeoptSignal, exec_id: str, envelope_id: str) -> DeoptFrame:
    committed = [
        CommittedEffect(step=e.get("pc") or 0, tool=e["tool"], effect_key=e["effect_key"],
                        effect_class=e["effect_class"], result=e["result"] if isinstance(e["result"], dict) else {"result": e["result"]})
        for e in journal.committed(exec_id)
    ]
    done_pcs = {e.step for e in committed}
    remaining = [s for s in skill["steps"] if s["pc"] > signal.pc or (s["pc"] == signal.pc and s["pc"] not in done_pcs
                                                                     and signal.kind != "post")]
    goal = ", ".join(f"{s.get('tool') or s.get('op')} (pc {s['pc']})" for s in remaining
                     if s.get("effect") != "read") or "confirm the task is complete"
    return DeoptFrame(skill=skill["_id"], pc=signal.pc, failed_guard=signal.guard, observed=signal.observed,
                      locals=ctx.locals(), committed_effects=committed, remaining_goal=goal,
                      original_task=envelope_id)


def handoff(skill: dict, ctx: SkillContext, signal: DeoptSignal, exec_id: str, envelope: dict,
            gw: Gateway) -> tuple[DeoptFrame, str, InterpResult, Tracer]:
    """Persist the frame and resume the interpreter from it. Returns (frame, event_id, result, continuation tracer)."""
    frame = materialize(skill, ctx, signal, exec_id, envelope["envelope_id"])
    zone = "osr" if frame.committed_effects else "safe"
    event_id = f"dx_{uuid.uuid4().hex[:10]}"
    col("deopt_events").insert_one({
        "event_id": event_id, "exec_id": exec_id, "skill": skill["_id"], "family": skill["family"],
        "pc": frame.pc, "failed_guard": frame.failed_guard, "observed": frame.observed, "kind": signal.kind,
        "zone": zone, "frame": frame.model_dump(mode="json"), "continuation_trace": None,
        "ts": datetime.now(timezone.utc)})

    inputs = {**(envelope.get("structured") or {}), "text": envelope["raw_text"]}
    tracer = Tracer(inputs, skill["family"], "interpreted", exec_id, envelope["envelope_id"],
                    trace_id=f"tr_{exec_id}_cont")
    resume_gw = Gateway(exec_id, tracer=tracer)  # same exec_id: committed effects are fenced
    result = run_interpreter(envelope, resume_gw, frame)
    gw.stats.fenced += resume_gw.stats.fenced
    gw.stats.resource_blocked += resume_gw.stats.resource_blocked
    col("deopt_events").update_one({"event_id": event_id}, {"$set": {
        "continuation_trace": tracer.trace_id, "fenced_repeats": resume_gw.stats.fenced + resume_gw.stats.resource_blocked,
        "resumed_ok": result.finished}})
    return frame, event_id, result, tracer
