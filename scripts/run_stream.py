"""Generate a reproducible task stream with drift events at fixed offsets.

Owner: Saurav (T3.1 in his P3 track). Headless, so the eval harness can drive it.

    python scripts/run_stream.py --n 330 --arm E --seed 42 [--fresh] [--no-drift]
"""
import argparse
import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from agentjit import stream
from agentjit.bootstrap import bootstrap
from agentjit.engine import ARMS, handle_task


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=330)
    ap.add_argument("--arm", default="E", choices=sorted(ARMS))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--fresh", action="store_true", help="drop this prefix and rebuild the world first")
    ap.add_argument("--no-drift", action="store_true")
    args = ap.parse_args()
    if args.fresh:
        bootstrap()

    def show(i: int, r: dict) -> None:
        mark = "ok " if r["verified"] else "FAIL"
        print(f"{i:4d} {mark} {r['route']:11s} {r['skill'] or '-':22s} deopt={r['deopt_zone'] or '-':7s} "
              f"${r['cost_usd']:.4f}{'  ' + r['verify_reason'] if not r['verified'] else ''}", flush=True)

    out = stream.run(args.n, args.seed, lambda e, rng: handle_task(e, rng, ARMS[args.arm]),
                     [] if args.no_drift else None, on_task=show)
    recs = [r for r in out if "route" in r]
    print(collections.Counter(r["route"] for r in recs), "failures:", sum(not r["verified"] for r in recs),
          "duplicates:", sum(r["duplicate_effects"] for r in recs))


if __name__ == "__main__":
    main()
