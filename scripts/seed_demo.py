"""Pre-warm Atlas with demo state: active skill, cost-curve history, filled shadow counter.

Owner: Saurav (T4.1 in his P4 track).
One command, idempotent, under 30 seconds. We never warm up live on stage.

The warm-up runs the real engine end to end (interpreted traces -> compile ->
probation with every run shadowed -> activation), using the offline stand-ins
so it is fast, free and deterministic. Drift knobs are left off, ready to flip.

    DB_PREFIX=demo python scripts/seed_demo.py
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from agentjit import stream
from agentjit.bootstrap import bootstrap
from agentjit.common import config
from agentjit.common.db import col
from agentjit.engine import ARMS, handle_task

MAX_TASKS = 200


def seed_demo(prefix: str = "demo", seed: int = 7) -> dict:
    t0 = time.time()
    mode = config.LLM_MODE
    config.LLM_MODE = "offline"
    try:
        bootstrap(prefix)
        def active() -> bool:
            return bool((col("task_families").find_one({"family_id": "refund_request"}) or {}).get("active_skill"))

        out = stream.run(MAX_TASKS, seed, lambda e, rng: handle_task(e, rng, ARMS["E"]), schedule=[], until=active)
        n = len(out)
    finally:
        config.LLM_MODE = mode
    fam = col("task_families").find_one({"family_id": "refund_request"}) or {}
    skill = col("skills").find_one({"_id": fam.get("active_skill")}) or {}
    col("demo_events").delete_many({})
    return {"prefix": prefix, "tasks": n, "active_skill": skill.get("_id"),
            "bound": (skill.get("shadow") or {}).get("upper_bound_95"),
            "guards": [g["expr"] for g in skill.get("entry_guards", [])], "seconds": round(time.time() - t0, 1)}


if __name__ == "__main__":
    import json
    print(json.dumps(seed_demo(sys.argv[1] if len(sys.argv) > 1 else config.DB_PREFIX), indent=2))
