"""effect_journal reads and writes (section 7.4).

The unique index on effect_key, written with majority write concern, is what
makes "at most one intent per effect" a database guarantee.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pymongo import ASCENDING, WriteConcern
from pymongo.errors import DuplicateKeyError

from agentjit.common.db import col


def _col():
    return col("effect_journal").with_options(write_concern=WriteConcern("majority"))


def ensure_indexes() -> None:
    c = _col()
    c.create_index([("effect_key", ASCENDING)], unique=True)
    c.create_index([("exec_id", ASCENDING)])
    c.create_index([("tool", ASCENDING), ("resource_id", ASCENDING), ("exec_id", ASCENDING)])


def insert_intent(effect_key: str, exec_id: str, tool: str, effect_class: str,
                  resource_id: Optional[str], args: dict[str, Any], pc: Optional[int]) -> bool:
    try:
        _col().insert_one({
            "effect_key": effect_key, "exec_id": exec_id, "tool": tool, "effect_class": effect_class,
            "resource_id": resource_id, "args": args, "pc": pc, "state": "intent",
            "result": None, "ts": datetime.now(timezone.utc)})
        return True
    except DuplicateKeyError:
        return False


def complete(effect_key: str, result: Any) -> None:
    _col().update_one({"effect_key": effect_key},
                      {"$set": {"state": "completed", "result": result, "completed_at": datetime.now(timezone.utc)}})


def mark_uncertain(effect_key: str) -> None:
    _col().update_one({"effect_key": effect_key, "state": "intent"}, {"$set": {"state": "uncertain"}})


def drop(effect_key: str) -> None:
    _col().delete_one({"effect_key": effect_key})


def find(effect_key: str) -> Optional[dict]:
    return _col().find_one({"effect_key": effect_key}, {"_id": 0})


def find_resource(exec_id: str, tool: str, resource_id: str) -> Optional[dict]:
    return _col().find_one({"exec_id": exec_id, "tool": tool, "resource_id": resource_id,
                            "state": "completed"}, {"_id": 0})


def unfinished(exec_id: str) -> list[dict]:
    return list(_col().find({"exec_id": exec_id, "state": {"$in": ["intent", "uncertain"]}}, {"_id": 0}))


def committed(exec_id: str) -> list[dict]:
    return list(_col().find({"exec_id": exec_id, "state": "completed"}, {"_id": 0}).sort("ts", ASCENDING))
