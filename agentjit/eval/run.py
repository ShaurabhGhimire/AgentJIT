"""Ablation harness (section 14): arms A-E over one seeded stream and drift schedule.

Each arm runs on its own collection prefix with a fresh world, the same seed,
the same task order and the same drift points. Every metric is reported with
a 95% interval; at this scale only large effects are real (section 14.5).

    python -m agentjit.eval.run --n 330 --arms A,B,C,D,E
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import time
from datetime import datetime, timezone
from typing import Any

from agentjit import stream
from agentjit.bootstrap import bootstrap
from agentjit.common import config
from agentjit.common.stats import cp_interval
from agentjit.engine import ARMS, handle_task

WARMUP_END = 125  # tasks before this are warm-up and excluded from steady-state cost


def mean_ci(xs: list[float]) -> dict:
    if not xs:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    m = statistics.fmean(xs)
    if len(xs) < 2:
        return {"mean": m, "lo": m, "hi": m, "n": 1}
    half = 1.96 * statistics.stdev(xs) / math.sqrt(len(xs))
    return {"mean": m, "lo": m - half, "hi": m + half, "n": len(xs)}


def rate_ci(k: int, n: int) -> dict:
    lo, hi = cp_interval(k, n) if n else (None, None)
    return {"rate": (k / n) if n else None, "k": k, "n": n, "lo": lo, "hi": hi}


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def summarize(arm: str, out: list[dict]) -> dict[str, Any]:
    recs = [r for r in out if "route" in r]
    events = [r for r in out if "event" in r]
    steady = [r for r in recs if r["index"] >= WARMUP_END]
    compiled = [r for r in recs if r["route"] == "compiled"]
    clean = [r for r in compiled if not r["deopt"]]
    deopted = [r for r in compiled if r["deopt"]]
    window_drift = next((e["index"] for e in events if "window" in e["event"]), None)
    after_policy = [r for r in recs if window_drift is not None and r["index"] >= window_drift]
    demoted_at = next((r["index"] for r in after_policy if (r.get("gate") or {}).get("status") in ("recompiling", "rejected")), None)
    recompiled_at = next((r["index"] for r in after_policy if (r.get("scheduler") or {}).get("compiled")), None)
    return {
        "arm": arm, "label": ARMS[arm].label, "tasks": len(recs), "simulated": any(r["simulated"] for r in recs),
        "cost_per_task_all": mean_ci([r["cost_usd"] for r in recs]),
        "cost_per_task_steady": mean_ci([r["cost_usd"] for r in steady]),
        "parse_cost_share": (sum(r["parse_cost"] for r in recs) / max(1e-12, sum(r["cost_usd"] for r in recs))),
        "shadow_cost_share": (sum(r["shadow_cost"] for r in recs) / max(1e-12, sum(r["cost_usd"] for r in recs))),
        "latency_p50_ms": pct([r["latency_ms"] for r in recs], 0.5),
        "latency_p95_ms": pct([r["latency_ms"] for r in recs], 0.95),
        "verified_success": rate_ci(sum(r["verified"] for r in recs), len(recs)),
        "silent_error": rate_ci(sum(r["silent_error"] for r in compiled), len(compiled)),
        "silent_errors_after_policy_drift": sum(r["silent_error"] for r in after_policy),
        "duplicate_effects": sum(r["duplicate_effects"] for r in recs),
        "tasks_with_duplicates": sum(r["duplicate_effects"] > 0 for r in recs),
        "compiled_share": rate_ci(len(compiled), len(recs)),
        "compiled_share_steady": rate_ci(sum(r["route"] == "compiled" for r in steady), len(steady)),
        "deopts": {"safe": sum(r["deopt_zone"] == "safe" for r in recs), "osr": sum(r["deopt_zone"] == "osr" for r in recs),
                   "restart": sum(r["deopt_zone"] == "restart" for r in recs)},
        "deopt_recovery_cost": (statistics.fmean([r["cost_usd"] for r in deopted]) - statistics.fmean([r["cost_usd"] for r in clean]))
        if deopted and clean else None,
        "policy_drift": {"at": window_drift, "demoted_after_tasks": (demoted_at - window_drift) if demoted_at is not None else None,
                         "recompiled_after_tasks": (recompiled_at - window_drift) if recompiled_at is not None else None},
        "events": events,
    }


def run_arm(arm: str, n: int, seed: int, prefix_base: str, out_dir: pathlib.Path) -> dict:
    prefix = f"{prefix_base}_{arm}"
    # each arm compiles its own skills; keep them apart from the committed skills/
    config.SKILLS_DIR = str((out_dir / f"skills_{arm}").resolve())
    bootstrap(prefix)
    t0 = time.time()
    out = stream.run(n, seed, lambda e, rng: handle_task(e, rng, ARMS[arm]))
    summary = summarize(arm, out)
    summary["wall_s"] = round(time.time() - t0, 1)
    summary["prefix"] = prefix
    return summary


def fmt(x: Any, money: bool = False) -> str:
    if x is None:
        return "–"
    return f"${x:.4f}" if money else (f"{x:.3f}" if isinstance(x, float) else str(x))


def markdown(results: list[dict], n: int, seed: int) -> str:
    sim = any(r["simulated"] for r in results)
    lines = [
        f"# AgentJIT ablation — {n} tasks, seed {seed}",
        "",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}. "
        + ("**Offline run: the interpreter, holes and codegen are deterministic stand-ins and costs are nominal "
           "(config.SIM_COST_*). Shapes and counts are real; dollar amounts are not.**" if sim else "Live Claude run."),
        "",
        "Drift schedule: " + "; ".join(f"task {e['index']}: {e['event']}" for e in results[0]["events"]),
        "",
        "| Arm | Cost/task (steady, 95% CI) | Verified success (95% CI) | Silent errors (compiled runs) | Duplicate effects | Compiled share (steady) | Deopts safe / OSR / restart | Policy drift: demoted after |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        c, v, s, cs = r["cost_per_task_steady"], r["verified_success"], r["silent_error"], r["compiled_share_steady"]
        d = r["deopts"]
        lines.append(
            f"| {r['arm']} — {r['label']} | {fmt(c['mean'], True)} [{fmt(c['lo'], True)}, {fmt(c['hi'], True)}] "
            f"| {v['k']}/{v['n']} = {fmt(v['rate'])} [{fmt(v['lo'])}, {fmt(v['hi'])}] "
            f"| {s['k']}/{s['n']} [{fmt(s['lo'])}, {fmt(s['hi'])}] | {r['duplicate_effects']} "
            f"| {fmt(cs['rate'])} | {d['safe']} / {d['osr']} / {d['restart']} "
            f"| {fmt(r['policy_drift']['demoted_after_tasks'])} tasks |")
    lines += ["", "Success criteria (section 14.5):", ""]
    by = {r["arm"]: r for r in results}
    if "A" in by and "E" in by:
        a, e = by["A"]["cost_per_task_steady"], by["E"]["cost_per_task_steady"]
        lines.append(f"1. E steady cost below A, net of parse and shadow: "
                     f"{'yes' if e['hi'] is not None and a['lo'] is not None and e['hi'] < a['lo'] else 'not shown'} "
                     f"(E {fmt(e['mean'], True)} vs A {fmt(a['mean'], True)})")
        lines.append(f"2. E verified success not below A: E {fmt(by['E']['verified_success']['rate'])} vs A {fmt(by['A']['verified_success']['rate'])}")
    if "C" in by and "E" in by:
        lines.append(f"3. Duplicates on the pending drift: C {by['C']['duplicate_effects']}, E {by['E']['duplicate_effects']}")
    if "D" in by and "E" in by:
        lines.append(f"4. Silent errors after the policy drift: D {by['D']['silent_errors_after_policy_drift']}, "
                     f"E {by['E']['silent_errors_after_policy_drift']} (E demoted after "
                     f"{fmt(by['E']['policy_drift']['demoted_after_tasks'])} tasks)")
    lines.append("5. Megamorphic families: see `megamorphic_blocklist`; the refund family stays compilable here.")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=330)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--arms", default="A,B,C,D,E")
    ap.add_argument("--prefix", default="eval")
    ap.add_argument("--out", default="eval_results")
    args = ap.parse_args()
    results = []
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for arm in args.arms.split(","):
        print(f"arm {arm}: {ARMS[arm].label} ...", flush=True)
        results.append(run_arm(arm, args.n, args.seed, args.prefix, out))
        r = results[-1]
        print(f"  success {r['verified_success']['rate']:.3f}  dups {r['duplicate_effects']}  "
              f"compiled {r['compiled_share']['rate']:.2f}  {r['wall_s']}s", flush=True)
    (out / "results.json").write_text(json.dumps(results, indent=2, default=str))
    (out / "results.md").write_text(markdown(results, args.n, args.seed))
    print((out / "results.md").read_text())


if __name__ == "__main__":
    main()
