"""Profiler: hotness and stability of whole-task signatures per family (section 8.2).

Runs the section 10.4 aggregation and $merges it into hot_segments. Only
interpreted, verified traces count. Provenance consistency, the other half of
the signature definition, is verified position by position in the generalizer:
value-matched provenance is absent for trivial values (0, 1, booleans), so it
cannot be part of an exact grouping key.

v1 compiles whole-task traces per family; no sub-task segment mining.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from agentjit.common import config
from agentjit.common.db import col


def refresh(window_days: Optional[int] = None) -> list[dict]:
    start = datetime.now(timezone.utc) - timedelta(days=window_days or config.PROFILER_WINDOW_DAYS)
    col("traces").aggregate([
        {"$match": {"mode": "interpreted", "verified_success": True, "deopt_zone": {"$ne": "osr"},
                    "ts": {"$gte": start}}},
        {"$group": {"_id": {"family": "$family", "signature": "$signature"}, "count": {"$sum": 1}}},
        {"$group": {"_id": "$_id.family", "total": {"$sum": "$count"},
                    "sigs": {"$push": {"signature": "$_id.signature", "count": "$count"}}}},
        {"$unwind": "$sigs"},
        {"$project": {"_id": 0, "family": "$_id", "signature": "$sigs.signature", "count": "$sigs.count",
                      "stability": {"$divide": ["$sigs.count", "$total"]}, "window_start": {"$literal": start}}},
        {"$merge": {"into": col("hot_segments").name, "on": ["family", "signature"],
                    "whenMatched": "replace", "whenNotMatched": "insert"}},
    ])
    return list(col("hot_segments").find({}, {"_id": 0}).sort("count", -1))


def hot_segments(family: Optional[str] = None) -> list[dict]:
    """Segments over both thresholds: the compile trigger."""
    q: dict = {"count": {"$gte": config.HOTNESS_THRESHOLD}, "stability": {"$gte": config.STABILITY_THRESHOLD}}
    if family:
        q["family"] = family
    return list(col("hot_segments").find(q, {"_id": 0}).sort("count", -1))


def source_traces(family: str, signature: str, since: Optional[datetime] = None) -> list[dict]:
    q: dict = {"family": family, "signature": signature, "mode": "interpreted", "verified_success": True,
               "deopt_zone": {"$ne": "osr"}}
    if since:
        q["ts"] = {"$gte": since}
    return list(col("traces").find(q, {"_id": 0}).sort("ts", 1))


def divergent_traces(family: str, signature: str, since: Optional[datetime] = None) -> list[dict]:
    """Verified interpreted traces of the same family that took another path."""
    q: dict = {"family": family, "signature": {"$ne": signature}, "mode": "interpreted", "verified_success": True,
               "deopt_zone": {"$ne": "osr"}}
    if since:
        q["ts"] = {"$gte": since}
    return list(col("traces").find(q, {"_id": 0}))
