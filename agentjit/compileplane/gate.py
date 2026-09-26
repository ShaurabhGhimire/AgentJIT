"""Promotion gate, probation, Clopper-Pearson bound, rollback (sections 7.6, 8.7, 9).

Owner: Susan (T3.2 in her P3 track).

- Probation: every run is shadowed. Activation when the cumulative one-sided
  95% upper bound on divergence drops below SHADOW_PROMOTION_THRESHOLD.
- Active: shadow rate decays but never below SHADOW_ACTIVE_MIN_RATE.
- Demotion: any divergence the verifier adjudicates against the skill, or the
  95% lower bound over the last SHADOW_WINDOW_RUNS runs above
  SHADOW_ACTIVE_THRESHOLD.
- Activation, demotion and rejection are multi-document transactions: skill
  status + family pointer + audit record together.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Optional

from agentjit.common import config
from agentjit.common.db import col, get_client
from agentjit.common.stats import cp_lower, cp_upper


def should_shadow(skill: dict, rng: Optional[random.Random] = None) -> bool:
    if skill["status"] == "probation":
        return True
    active_runs = skill.get("shadow", {}).get("active_runs", 0)
    rate = max(config.SHADOW_ACTIVE_MIN_RATE, 1.0 / (1.0 + active_runs * config.SHADOW_DECAY))
    return (rng or random).random() < rate


def _txn(fn) -> None:
    with get_client().start_session() as s:
        s.with_transaction(lambda session: fn(session))


def _audit(session, skill_id: str, event: str, **extra) -> None:
    col("skill_audit").insert_one({"skill": skill_id, "event": event, "ts": datetime.now(timezone.utc), **extra},
                                  session=session)


def _set_servable(session, family: str) -> None:
    fam = col("task_families").find_one({"family_id": family}, session=session) or {}
    col("task_families").update_one({"family_id": family}, {"$set": {
        "has_servable_skill": bool(fam.get("active_skill") or fam.get("probation_skill"))}}, session=session)


def enter_probation(skill_id: str) -> None:
    skill = col("skills").find_one({"_id": skill_id})

    def fn(session):
        col("skills").update_one({"_id": skill_id}, {"$set": {"status": "probation"}}, session=session)
        col("task_families").update_one({"family_id": skill["family"]},
                                        {"$set": {"probation_skill": skill_id}}, upsert=True, session=session)
        _set_servable(session, skill["family"])
        _audit(session, skill_id, "probation")
    _txn(fn)


def activate(skill_id: str) -> None:
    skill = col("skills").find_one({"_id": skill_id})

    def fn(session):
        fam = col("task_families").find_one({"family_id": skill["family"]}, session=session) or {}
        previous = fam.get("active_skill")
        if previous and previous != skill_id:
            col("skills").update_one({"_id": previous}, {"$set": {"status": "retired"}}, session=session)
        col("skills").update_one({"_id": skill_id}, {"$set": {"status": "active",
                                                              "activated_at": datetime.now(timezone.utc)}}, session=session)
        col("task_families").update_one({"family_id": skill["family"]},
                                        {"$set": {"active_skill": skill_id, "probation_skill": None}}, session=session)
        _set_servable(session, skill["family"])
        _audit(session, skill_id, "activated", replaced=previous)
    _txn(fn)


def demote(skill_id: str, reason: str) -> None:
    """Active (or probation) -> recompiling; the family pointer rolls back to the parent if it is still sound."""
    skill = col("skills").find_one({"_id": skill_id})

    def fn(session):
        fam = col("task_families").find_one({"family_id": skill["family"]}, session=session) or {}
        update: dict = {}
        if fam.get("active_skill") == skill_id:
            parent = col("skills").find_one({"_id": skill.get("parent")}, session=session) if skill.get("parent") else None
            # Roll back only to a parent that was retired cleanly, never to one demoted for cause.
            update["active_skill"] = parent["_id"] if parent and parent["status"] == "retired" else None
            if update["active_skill"]:
                col("skills").update_one({"_id": parent["_id"]}, {"$set": {"status": "active"}}, session=session)
        if fam.get("probation_skill") == skill_id:
            update["probation_skill"] = None
        if update:
            col("task_families").update_one({"family_id": skill["family"]}, {"$set": update}, session=session)
        col("skills").update_one({"_id": skill_id}, {"$set": {"status": "recompiling", "demoted_reason": reason,
                                                              "demoted_at": datetime.now(timezone.utc)}}, session=session)
        _set_servable(session, skill["family"])
        _audit(session, skill_id, "demoted", reason=reason, rolled_back_to=update.get("active_skill"))
    _txn(fn)


def reject(skill_id: str, reason: str) -> None:
    skill = col("skills").find_one({"_id": skill_id})

    def fn(session):
        # demoted_at marks where fresh evidence starts: the next compile ignores older traces
        col("skills").update_one({"_id": skill_id}, {"$set": {"status": "rejected", "rejected_reason": reason,
                                                              "demoted_at": datetime.now(timezone.utc)}},
                                 session=session)
        col("task_families").update_one({"family_id": skill["family"], "probation_skill": skill_id},
                                        {"$set": {"probation_skill": None}}, session=session)
        _set_servable(session, skill["family"])
        _audit(session, skill_id, "rejected", reason=reason)
    _txn(fn)


def record_shadow(skill_id: str, diverged: bool, adjudication: str) -> dict:
    """Update the skill's divergence statistics and apply the lifecycle rules. Returns the new state."""
    skill = col("skills").find_one({"_id": skill_id})
    if skill is None or skill["status"] not in ("probation", "active"):
        return {"status": skill["status"] if skill else None}
    counted = diverged and adjudication != "interpreter_wrong"
    sh = skill.get("shadow", {})
    runs, divs = sh.get("runs", 0) + 1, sh.get("divergences", 0) + int(counted)
    window = (sh.get("window", []) + [int(counted)])[-config.SHADOW_WINDOW_RUNS:]
    active_runs = sh.get("active_runs", 0) + (skill["status"] == "active")
    upper = cp_upper(divs, runs)
    window_lower = cp_lower(sum(window), len(window))
    col("skills").update_one({"_id": skill_id}, {"$set": {"shadow": {
        "runs": runs, "divergences": divs, "upper_bound_95": round(upper, 4), "window": window,
        "window_lower_95": round(window_lower, 4), "active_runs": active_runs}}})

    if skill["status"] == "probation":
        if counted and adjudication == "skill_wrong":
            reject(skill_id, "verifier-confirmed divergence during probation")
            return {"status": "rejected", "upper": upper}
        if upper < config.SHADOW_PROMOTION_THRESHOLD:
            activate(skill_id)
            return {"status": "active", "upper": upper}
        if runs >= config.PROBATION_BUDGET_RUNS:
            reject(skill_id, f"bound {upper:.3f} still above threshold after {runs} shadow runs")
            return {"status": "rejected", "upper": upper}
        return {"status": "probation", "upper": upper}

    if counted and adjudication == "skill_wrong":
        demote(skill_id, "verifier-confirmed shadow divergence")
        return {"status": "recompiling", "upper": upper}
    if window_lower > config.SHADOW_ACTIVE_THRESHOLD:
        demote(skill_id, f"windowed divergence lower bound {window_lower:.3f} above threshold")
        return {"status": "recompiling", "upper": upper}
    return {"status": "active", "upper": upper}
