"""Change-stream consumers (section 10.4 change streams, T3.3).

In a long-running deployment the compile plane reacts to the runtime through
three streams; engine.handle_task also calls the same handlers synchronously,
which keeps eval runs deterministic.

- skills (status changes)      -> invalidate the executor's code cache (hot swap)
- deopt_events (inserts)       -> scheduler: polymorphic recompile when evidence accumulates
- shadow_runs (inserts)        -> log gate transitions (the gate itself updates in-line)

Resume tokens are stored per stream in watcher_state, so a restarted watcher
continues where it stopped instead of silently missing events.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from pymongo.errors import PyMongoError

from agentjit.common.db import col
from agentjit.compileplane import scheduler
from agentjit.runtime import executor

log = logging.getLogger("agentjit.watchers")


def _on_skill_change(change: dict) -> None:
    doc = change.get("fullDocument") or {}
    executor.invalidate(doc.get("code_ref") or None)
    log.info("skill %s -> %s", doc.get("_id"), doc.get("status"))


def _on_deopt(change: dict) -> None:
    ev = change["fullDocument"]
    fam = col("task_families").find_one({"family_id": ev["family"]}) or {}
    if fam.get("active_skill"):
        result = scheduler.after_task(ev["family"], {"skill": fam["active_skill"], "deopt": True})
        if result:
            log.info("recompile check for %s: %s", ev["family"], result)


def _on_shadow(change: dict) -> None:
    doc = change["fullDocument"]
    if doc.get("diverged"):
        log.warning("shadow divergence on %s (%s)", doc["skill"], doc.get("adjudication"))


STREAMS: dict[str, tuple[str, list, Callable[[dict], None]]] = {
    "skills": ("skills", [{"$match": {"operationType": {"$in": ["update", "replace", "insert"]}}}], _on_skill_change),
    "deopts": ("deopt_events", [{"$match": {"operationType": "insert"}}], _on_deopt),
    "shadows": ("shadow_runs", [{"$match": {"operationType": "insert"}}], _on_shadow),
}


def watch(name: str, stop: Optional[threading.Event] = None, max_events: Optional[int] = None) -> int:
    collection, pipeline, handler = STREAMS[name]
    state = col("watcher_state").find_one({"_id": name}) or {}
    seen = 0
    kwargs = {"full_document": "updateLookup"}
    if state.get("resume_token"):
        kwargs["resume_after"] = state["resume_token"]
    try:
        ctx = col(collection).watch(pipeline, **kwargs)
    except PyMongoError as e:
        log.warning("change streams unavailable for '%s': %s", name, e)
        return 0
    with ctx as stream:
        while not (stop and stop.is_set()):
            change = stream.try_next()
            if change is None:
                if max_events is not None and seen >= max_events:
                    break
                continue
            try:
                handler(change)
            except PyMongoError:
                log.exception("handler failed for %s", name)
            col("watcher_state").update_one({"_id": name}, {"$set": {
                "resume_token": stream.resume_token, "at": datetime.now(timezone.utc)}}, upsert=True)
            seen += 1
            if max_events is not None and seen >= max_events:
                break
    return seen


def run_all(stop: Optional[threading.Event] = None) -> list[threading.Thread]:
    threads = [threading.Thread(target=watch, args=(n, stop), daemon=True, name=f"watch-{n}") for n in STREAMS]
    for t in threads:
        t.start()
    return threads


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    for t in run_all(stop):
        t.join()
