"""Dispatcher: section 7.1 stage 5 routing policy.

Owner: Saurav (T2.2 in his P2 track).
Chooses compiled vs interpreted, exploration, shadow flag and version. Writes
one DispatchDecision per task, every time. Any uncertainty routes to the
interpreter; a task is never rejected.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from agentjit.common import config
from agentjit.common.db import col
from agentjit.common.models import DispatchDecision
from agentjit.compileplane import gate
from agentjit.runtime.parser import ParseResult, parse


@dataclass
class Plan:
    route: str  # compiled | interpreted
    skill: Optional[dict]
    parse: ParseResult
    explore: bool
    shadow: bool
    decision: DispatchDecision


def candidate_skill(family_id: str, rng: random.Random) -> Optional[dict]:
    """Active or probation version; while a recompile is in probation, part of the traffic stays on the parent."""
    fam = col("task_families").find_one({"family_id": family_id}) or {}
    if col("megamorphic_blocklist").find_one({"family": family_id}):
        return None
    active, probation = fam.get("active_skill"), fam.get("probation_skill")
    pick = probation if probation and (not active or rng.random() >= config.PINNED_PARENT_SHARE) else active
    return col("skills").find_one({"_id": pick}) if pick else None


def dispatch(envelope: dict, rng: random.Random, compile_enabled: bool = True,
             shadow_enabled: bool = True) -> Plan:
    p, skill = parse(envelope, lambda fid: candidate_skill(fid, rng) if compile_enabled else None)
    route, explore, shadow = "interpreted", False, False
    if compile_enabled and p.eligible and skill is not None:
        if rng.random() < config.EXPLORATION_RATE:
            explore = True
        else:
            route = "compiled"
            shadow = shadow_enabled and gate.should_shadow(skill, rng)
    decision = DispatchDecision(
        envelope_id=envelope["envelope_id"], family=p.family,
        skill=skill["_id"] if route == "compiled" else None, args=p.args, task_guards=p.task_guards,
        route=route, explore=explore, shadow=shadow)
    col("dispatch_decisions").insert_one({**decision.model_dump(mode="json"), "reason": p.reason,
                                          "candidate": skill["_id"] if skill else None,
                                          "parse_cost": p.cost_usd, "ts": datetime.now(timezone.utc)})
    return Plan(route, skill if route == "compiled" else None, p, explore, shadow, decision)
