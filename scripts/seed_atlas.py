"""Create all Atlas collections, indexes, and the time-series metrics collection.

Owner: Susan (T1.1 in her P1 track).
Run idempotently against a clean or existing DB. Vector search indexes need
Atlas (or the atlas-local image); against a plain mongod they are skipped and
the parser falls back to cosine similarity in Python.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import CollectionInvalid, OperationFailure

from agentjit.common import config
from agentjit.common.db import col, get_db

COLLECTIONS = ["task_envelopes", "task_families", "dispatch_decisions", "traces", "hot_segments",
               "skills", "executions", "effect_journal", "deopt_events", "shadow_runs",
               "metrics", "megamorphic_blocklist"]


def _vector_index(collection: str, name: str, path: str, filters: list[str]) -> str:
    definition = {"fields": [{"type": "vector", "path": path, "numDimensions": config.EMBEDDING_DIMENSIONS,
                              "similarity": "cosine"}] + [{"type": "filter", "path": f} for f in filters]}
    try:
        from pymongo.operations import SearchIndexModel
        existing = {i["name"] for i in col(collection).list_search_indexes()}
        if name in existing:
            col(collection).update_search_index(name, definition)
            return f"updated {name}"
        col(collection).create_search_index(SearchIndexModel(definition=definition, name=name, type="vectorSearch"))
        return f"created {name}"
    except OperationFailure as e:
        return f"skipped {name} (no Atlas Search here: {e.code})"


def seed() -> list[str]:
    db = get_db()
    log = []
    existing = set(db.list_collection_names())
    for name in COLLECTIONS:
        full = f"{config.DB_PREFIX}_{name}"
        if full in existing:
            continue
        try:
            if name == "metrics":
                db.create_collection(full, timeseries={"timeField": "ts", "metaField": "meta", "granularity": "minutes"})
            else:
                db.create_collection(full)
            log.append(f"created {full}")
        except CollectionInvalid:
            pass

    col("task_envelopes").create_index([("tenant", ASCENDING), ("ts", DESCENDING)])
    col("task_envelopes").create_index("envelope_id", unique=True)
    col("task_families").create_index("family_id", unique=True)
    col("dispatch_decisions").create_index([("family.id", ASCENDING), ("ts", DESCENDING)])
    col("traces").create_index([("family", ASCENDING), ("signature", ASCENDING), ("ts", DESCENDING)])
    col("traces").create_index("trace_id", unique=True)
    col("hot_segments").create_index([("family", ASCENDING), ("signature", ASCENDING)], unique=True)
    col("skills").create_index([("family", ASCENDING), ("status", ASCENDING)])
    for name in ("executions", "deopt_events", "shadow_runs"):
        col(name).create_index([("skill", ASCENDING), ("ts", DESCENDING)])
    col("executions").create_index("exec_id", unique=True)
    col("effect_journal").create_index("effect_key", unique=True)
    col("effect_journal").create_index("exec_id")
    col("effect_journal").create_index([("tool", ASCENDING), ("resource_id", ASCENDING), ("exec_id", ASCENDING)])
    col("megamorphic_blocklist").create_index("expiresAt", expireAfterSeconds=0)
    log.append("indexes ensured")

    log.append(_vector_index("task_families", "family_centroid", "centroid", ["tenant", "has_servable_skill"]))
    log.append(_vector_index("skills", "skill_embedding", "embedding", ["status", "family"]))
    return log


if __name__ == "__main__":
    for line in seed():
        print(" ", line)
