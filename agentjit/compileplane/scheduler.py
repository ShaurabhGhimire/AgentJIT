"""Recompile scheduler: decides when the compile plane runs (section 9).

- A family with no servable skill is compiled once enough verified,
  interpreted evidence has accumulated. After a demotion, only evidence from
  after the demotion counts, so a skill demoted for policy drift is rebuilt
  from traces that reflect the new policy.
- An active skill whose deopt rate over its last RECOMPILE_WINDOW_RUNS
  compiled runs exceeds RECOMPILE_DEOPT_RATE gets a polymorphic recompile:
  the continuation paths that caused the deopts become extra branches.
- More than POLYMORPHIC_K branches marks the family megamorphic: it is
  blocklisted with a TTL and stays interpreted.

Called synchronously after each task by the engine (deterministic runs), and
by the change-stream watchers in a long-running deployment.
"""
from __future__ import annotations


from typing import Optional

from agentjit.common import config
from agentjit.common.db import col
from agentjit.compileplane import compiler, polymorphic

CHECK_EVERY = 5


def _since(family: str) -> Optional[datetime]:
    last = col("skills").find_one({"family": family, "demoted_at": {"$exists": True}}, sort=[("demoted_at", -1)])
    return last["demoted_at"] if last else None


def after_task(family: str, rec: dict) -> Optional[dict]:
    if col("megamorphic_blocklist").find_one({"family": family}):
        return None
    fam = col("task_families").find_one({"family_id": family}) or {}
    if not (fam.get("active_skill") or fam.get("probation_skill")):
        since = _since(family)
        q = {"family": family, "mode": "interpreted", "verified_success": True, "deopt_zone": {"$ne": "osr"}}
        if since:
            q["ts"] = {"$gte": since}
        n = col("traces").count_documents(q)
        if n < config.HOTNESS_THRESHOLD or n % CHECK_EVERY:
            return None
        return compiler.compile_family(family, since=since)

    active = fam.get("active_skill")
    if active and rec.get("skill") == active and rec.get("deopt"):
        recent = list(col("executions").find({"skill": active, "route": "compiled"}, {"deopt": 1})
                      .sort("ts", -1).limit(config.RECOMPILE_WINDOW_RUNS))
        if len(recent) >= config.RECOMPILE_WINDOW_RUNS // 2:
            rate = sum(r["deopt"] for r in recent) / len(recent)
            if rate > config.RECOMPILE_DEOPT_RATE:
                return polymorphic.maybe_add_branch(family, active)
    return None
