"""Live demo dashboard (section 16.1 item 10, T3.2).

    uvicorn agentjit.dashboard.app:app --port 8000

Reads the current DB prefix. The page polls /api/state; the controls flip the
drift knobs and stream tasks through the real engine in a background thread.
"""
from __future__ import annotations

import pathlib
import random
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from agentjit import stream
from agentjit.common import config, llm
from agentjit.common.db import col
from agentjit.engine import ARMS, handle_task
from agentjit.runtime.mockapi import world

app = FastAPI(title="AgentJIT dashboard")
STATIC = pathlib.Path(__file__).parent / "static"
_lock = threading.Lock()
_runner: dict[str, Any] = {"running": False, "remaining": 0, "arm": "E"}


def _task_rows() -> list[dict]:
    rows = list(col("executions").find({}, {"_id": 0, "route": 1, "deopt_zone": 1, "cost_usd": 1, "verified": 1,
                                           "silent_error": 1, "diverged": 1, "skill": 1, "duplicate_effects": 1,
                                           "fenced": 1, "latency_ms": 1, "ts": 1}).sort("ts", 1))
    for i, r in enumerate(rows):
        r["i"] = i
        r["ts"] = r["ts"].isoformat() if r.get("ts") else None
    return rows


def _skill_view(skill: dict | None) -> dict | None:
    if not skill:
        return None
    code = ""
    branches = []
    if skill.get("shape") == "polymorphic":
        for bid in skill.get("branches", []):
            b = col("skills").find_one({"_id": bid}) or {}
            branches.append({"id": bid, "entry_guards": b.get("entry_guards", []), "code": _code(b.get("code_ref"))})
    else:
        code = _code(skill.get("code_ref"))
    return {
        "id": skill["_id"], "status": skill["status"], "shape": skill.get("shape"), "parent": skill.get("parent"),
        "entry_guards": skill.get("entry_guards", []),
        "residual": [{"pc": s["pc"], "expr": e} for s in skill.get("steps", []) for e in s.get("post", [])],
        "steps": [{"pc": s["pc"], "name": s.get("tool") or s.get("op"), "effect": s.get("effect")} for s in skill.get("steps", [])],
        "ponr": (skill.get("hoist") or {}).get("point_of_no_return"),
        "shadow": {k: v for k, v in (skill.get("shadow") or {}).items() if k != "window"},
        "code": code, "branches": branches, "codegen": (skill.get("codegen") or {}).get("method"),
    }


def _code(ref: str | None) -> str:
    if not ref:
        return ""
    path = pathlib.Path(ref)
    if not path.is_absolute():
        path = pathlib.Path(config.SKILLS_DIR).parent / ref
    return path.read_text() if path.exists() else f"# {ref} not found"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state() -> dict:
    fam = col("task_families").find_one({"family_id": "refund_request"}) or {}
    active = col("skills").find_one({"_id": fam.get("active_skill")}) if fam.get("active_skill") else None
    probation = col("skills").find_one({"_id": fam.get("probation_skill")}) if fam.get("probation_skill") else None
    deopt = col("deopt_events").find_one({}, {"_id": 0}, sort=[("ts", -1)])
    if deopt:
        deopt["ts"] = deopt["ts"].isoformat()
    skills = [{"id": s["_id"], "status": s["status"], "shape": s.get("shape"), "parent": s.get("parent"),
               "runs": (s.get("shadow") or {}).get("runs", 0), "divergences": (s.get("shadow") or {}).get("divergences", 0),
               "upper": (s.get("shadow") or {}).get("upper_bound_95"),
               "reason": s.get("demoted_reason") or s.get("rejected_reason")}
              for s in col("skills").find({"status": {"$ne": "branch"}}).sort("created_at", 1)]
    events = list(col("demo_events").find({}, {"_id": 0}).sort("index", 1))
    return {
        "tasks": _task_rows(), "events": events, "skills": skills,
        "active": _skill_view(active), "probation": _skill_view(probation), "latest_deopt": deopt,
        "knobs": world.get_knobs(), "policy": world.get_policy(), "runner": dict(_runner),
        "mode": "live Claude" if llm.available() else "offline (simulated costs)",
        "thresholds": {"promotion": config.SHADOW_PROMOTION_THRESHOLD, "active": config.SHADOW_ACTIVE_THRESHOLD},
        "fenced_total": sum(d.get("fenced_repeats", 0) for d in col("deopt_events").find({}, {"fenced_repeats": 1})),
    }


def _log_event(label: str) -> None:
    col("demo_events").insert_one({"index": col("executions").count_documents({}), "label": label,
                                   "ts": datetime.now(timezone.utc)})


def _run(n: int, arm: str) -> None:
    try:
        start = col("executions").count_documents({})
        rng = random.Random(1000 + start)
        route_rng = random.Random(2000 + start)
        for k in range(n):
            env = stream.make_envelope(rng, start + k)
            handle_task(env, route_rng, ARMS[arm])
            _runner["remaining"] = n - k - 1
    finally:
        _runner["running"] = False
        _lock.release()


@app.post("/api/run")
def run(body: dict) -> dict:
    n = max(1, min(int(body.get("n", 10)), 500))
    arm = body.get("arm", "E")
    if arm not in ARMS:
        raise HTTPException(400, "unknown arm")
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "a run is already in progress")
    _runner.update(running=True, remaining=n, arm=arm)
    threading.Thread(target=_run, args=(n, arm), daemon=True).start()
    return {"started": n}


@app.post("/api/knobs")
def knobs(body: dict) -> dict:
    allowed = {"currency_mix", "split_shipment_rate", "refund_returns_pending"}
    changes = {k: v for k, v in body.items() if k in allowed}
    world.set_knobs(**changes)
    for k, v in changes.items():
        _log_event(f"{k} = {v}")
    return world.get_knobs()


@app.post("/api/policy")
def policy(body: dict) -> dict:
    if "refund_window_days" in body:
        world.set_policy(refund_window_days=int(body["refund_window_days"]))
        _log_event(f"refund window = {int(body['refund_window_days'])} days")
    return world.get_policy()


# ---------------------------------------------------------------------------
# Side-by-side comparison: plain LLM agent (arm A) vs AgentJIT (arm E)
# ---------------------------------------------------------------------------
import collections
import copy

from agentjit.common.db import get_db, prefix_scope

LANES = {"base": ("demo_base", "A"), "jit": (config.DB_PREFIX, "E")}
_cmp_runner: dict[str, Any] = {"base": 0, "jit": 0}
_feed: collections.deque = collections.deque(maxlen=40)


def _watch_feed() -> None:
    prefix = LANES["jit"][0] + "_"
    names = [prefix + n for n in ("skills", "deopt_events", "shadow_runs", "effect_journal", "executions")]
    try:
        with get_db().watch([{"$match": {"ns.coll": {"$in": names}}}]) as cs:
            for ch in cs:
                _feed.appendleft({"ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                                  "op": ch["operationType"], "coll": ch["ns"]["coll"][len(prefix):]})
    except Exception as e:  # change streams unavailable: panel just stays empty
        _feed.appendleft({"ts": "", "op": "unavailable", "coll": str(e)[:80]})


threading.Thread(target=_watch_feed, daemon=True).start()


@app.get("/compare")
def compare_page() -> FileResponse:
    return FileResponse(STATIC / "compare.html")


def _lane_state(prefix: str) -> dict:
    with prefix_scope(prefix):
        rows = list(col("executions").find({}, {"_id": 0, "route": 1, "deopt_zone": 1, "cost_usd": 1, "verified": 1,
                                                "silent_error": 1, "duplicate_effects": 1, "fenced": 1,
                                                "latency_ms": 1, "diverged": 1}).sort("ts", 1))
        return {"tasks": rows, "knobs": world.get_knobs(), "policy": world.get_policy()}


@app.get("/api/compare/state")
def compare_state() -> dict:
    out = {k: _lane_state(p) for k, (p, _) in LANES.items()}
    with prefix_scope(LANES["jit"][0]):
        fam = col("task_families").find_one({"family_id": "refund_request"}) or {}
        last = col("dispatch_decisions").find_one({"family": {"$ne": None}}, {"_id": 0, "family": 1}, sort=[("ts", -1)])
        out["atlas"] = {
            "vector_routed": col("dispatch_decisions").count_documents({"family": {"$ne": None}}),
            "last_match": (last or {}).get("family"),
            "effects": col("effect_journal").estimated_document_count(),
            "fenced": sum(d.get("fenced_repeats", 0) for d in col("deopt_events").find({}, {"fenced_repeats": 1})),
            "skills": [{"id": s["_id"], "status": s["status"], "reason": s.get("demoted_reason") or s.get("rejected_reason")}
                       for s in col("skills").find({"status": {"$ne": "branch"}}, {"status": 1, "demoted_reason": 1,
                                                                                    "rejected_reason": 1}).sort("created_at", 1)],
            "active": fam.get("active_skill"),
            "hot_segments": list(col("hot_segments").find({}, {"_id": 0, "signature": 0}).limit(3)),
            "metrics": col("metrics").count_documents({}),
            "blocklist": col("megamorphic_blocklist").count_documents({}),
        }
    out["feed"] = list(_feed)[:15]
    out["running"] = dict(_cmp_runner)
    out["mode"] = "live Claude via OpenRouter" if llm.available() else "offline (simulated LLM costs)"
    return out


def _lane_run(lane: str, n: int, start: int) -> None:
    prefix, arm = LANES[lane]
    try:
        with prefix_scope(prefix):
            rng, route_rng = random.Random(1000 + start), random.Random(2000 + start)
            for k in range(n):
                env = stream.make_envelope(rng, start + k)
                handle_task(copy.deepcopy(env), route_rng, ARMS[arm])
                _cmp_runner[lane] = n - k - 1
    finally:
        _cmp_runner[lane] = 0


@app.post("/api/compare/run")
def compare_run(body: dict) -> dict:
    if any(_cmp_runner.values()):
        raise HTTPException(409, "a run is already in progress")
    n = max(1, min(int(body.get("n", 10)), 200))
    with prefix_scope(LANES["jit"][0]):
        start = col("executions").count_documents({})
    for lane in LANES:
        _cmp_runner[lane] = n
        threading.Thread(target=_lane_run, args=(lane, n, start), daemon=True).start()
    return {"started": n}


@app.post("/api/compare/drift")
def compare_drift(body: dict) -> dict:
    allowed = {"currency_mix", "split_shipment_rate", "refund_returns_pending"}
    knobs = {k: v for k, v in body.items() if k in allowed}
    for prefix, _ in LANES.values():
        with prefix_scope(prefix):
            if knobs:
                world.set_knobs(**knobs)
            if "refund_window_days" in body:
                world.set_policy(refund_window_days=int(body["refund_window_days"]))
    return {"ok": True}
