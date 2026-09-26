"""Load fixture traces and envelopes into the traces and task_envelopes collections."""
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from agentjit.common.db import col


def load_fixtures() -> None:
    traces_dir = pathlib.Path("fixtures/traces")
    envelopes_dir = pathlib.Path("fixtures/envelopes")
    if not traces_dir.exists():
        print(f"fixtures/traces/ not found; run from the repo root", file=sys.stderr)
        sys.exit(1)

    # Stamp ts at load time, one minute apart, so the fixtures always fall
    # inside the profiler's sliding window.
    now = datetime.now(timezone.utc)
    trace_paths = sorted(traces_dir.glob("*.json"))

    traces_col = col("traces")
    for k, path in enumerate(trace_paths):
        with open(path) as f:
            doc = json.load(f)
        doc["ts"] = now - timedelta(minutes=len(trace_paths) - k)
        traces_col.replace_one({"trace_id": doc["trace_id"]}, doc, upsert=True)
        print(f"  loaded {doc['trace_id']} ({doc['family']}, {doc['mode']})")
    print(f"\n{len(trace_paths)} fixture traces -> '{traces_col.name}'")

    envelopes_col = col("task_envelopes")
    envelope_paths = sorted(envelopes_dir.glob("*.json"))
    for k, path in enumerate(envelope_paths):
        with open(path) as f:
            doc = json.load(f)
        doc["ts"] = now - timedelta(minutes=len(envelope_paths) - k)
        envelopes_col.replace_one({"envelope_id": doc["envelope_id"]}, doc, upsert=True)
    print(f"{len(envelope_paths)} fixture envelopes -> '{envelopes_col.name}'")


if __name__ == "__main__":
    load_fixtures()
