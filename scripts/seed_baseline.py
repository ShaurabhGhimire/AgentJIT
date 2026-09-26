"""Pre-warm the "without AgentJIT" lane of the comparison dashboard.

Runs the same seeded task stream as seed_demo.py, but through arm A (every task
interpreted by the LLM, nothing compiled), so both lanes share a history from
task 0 and the cumulative-cost chart shows the real break-even point.

    DB_PREFIX=demo_base python scripts/seed_baseline.py demo_base 125
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


def seed_baseline(prefix: str = "demo_base", n: int = 125, seed: int = 7) -> dict:
    t0 = time.time()
    mode = config.LLM_MODE
    config.LLM_MODE = "offline"
    try:
        bootstrap(prefix)
        stream.run(n, seed, lambda e, rng: handle_task(e, rng, ARMS["A"]), schedule=[])
    finally:
        config.LLM_MODE = mode
    col("demo_events").delete_many({})
    return {"prefix": prefix, "tasks": col("executions").count_documents({}), "seconds": round(time.time() - t0, 1)}


if __name__ == "__main__":
    import json
    prefix = sys.argv[1] if len(sys.argv) > 1 else "demo_base"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 125
    print(json.dumps(seed_baseline(prefix, n), indent=2))
