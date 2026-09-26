"""Load fixture traces from fixtures/traces/ into the traces collection."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from agentjit.common.db import col


def load_fixtures() -> None:
    traces_dir = pathlib.Path("fixtures/traces")
    if not traces_dir.exists():
        print(f"fixtures/traces/ not found; run from the repo root", file=sys.stderr)
        sys.exit(1)

    traces_col = col("traces")
    loaded = 0
    for path in sorted(traces_dir.glob("*.json")):
        with open(path) as f:
            doc = json.load(f)
        traces_col.replace_one({"trace_id": doc["trace_id"]}, doc, upsert=True)
        loaded += 1
        print(f"  loaded {doc['trace_id']} ({doc['family']}, {doc['mode']})")

    print(f"\n{loaded} fixture traces -> '{traces_col.name}'")


if __name__ == "__main__":
    load_fixtures()
