"""Lifecycle side effects shared by the scheduler and the polymorphic compiler."""
from datetime import datetime, timedelta, timezone

from agentjit.common import config
from agentjit.common.db import col
from agentjit.compileplane import gate


def megamorphic(family: str, skill_id: str, reason: str) -> None:
    """Too many paths: stop compiling this family until the cooldown (TTL) expires."""
    col("megamorphic_blocklist").update_one(
        {"family": family},
        {"$set": {"family": family, "reason": reason,
                  "expiresAt": datetime.now(timezone.utc) + timedelta(seconds=config.MEGAMORPHIC_COOLDOWN_S)}},
        upsert=True)
    skill = col("skills").find_one({"_id": skill_id})
    if skill and skill["status"] in ("active", "probation"):
        gate.demote(skill_id, f"megamorphic: {reason}")
    col("skills").update_one({"_id": skill_id}, {"$set": {"status": "megamorphic"}})
