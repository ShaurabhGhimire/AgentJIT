"""End-to-end: the system compiles itself, and the drift events behave as designed (T6.2-T6.5)."""
import random

import pytest

from agentjit import stream
from agentjit.bootstrap import bootstrap
from agentjit.common import config
from agentjit.common.db import col
from agentjit.engine import ARMS, handle_task


@pytest.fixture
def fast(monkeypatch, tmp_path, isolated_db):
    monkeypatch.setattr(config, "HOTNESS_THRESHOLD", 12)
    monkeypatch.setattr(config, "GUARD_MIN_SUPPORT", 8)
    monkeypatch.setattr(config, "SHADOW_PROMOTION_THRESHOLD", 0.12)  # ~24 clean shadow runs
    monkeypatch.setattr(config, "EXPLORATION_RATE", 0.0)
    monkeypatch.setattr(config, "SKILLS_DIR", str(tmp_path / "skills"))
    bootstrap(fresh=False)


def run(arm, n, schedule, start=0):
    return [r for r in stream.run(n, 3, lambda e, rng: handle_task(e, rng, ARMS[arm]), schedule, start=start)
            if "route" in r]


def test_cold_path_is_a_normal_agent(fast):
    recs = run("A", 15, [])
    assert all(r["route"] == "interpreted" and r["verified"] for r in recs)
    t = col("traces").find_one({"mode": "interpreted"})
    assert any(s["provenance"] for s in t["steps"])


def test_warm_path_compiles_activates_and_runs(fast):
    recs = run("E", 60, [])
    skill = col("skills").find_one({"_id": "refund_standard@v1"})
    assert skill is not None and skill["status"] == "active"
    assert skill["hoist"]["point_of_no_return"] == 4
    exprs = [g["expr"] for g in skill["entry_guards"]]
    assert "len(shipments) == 1" in exprs and "order['currency'] in ['USD']" in exprs
    compiled = [r for r in recs if r["route"] == "compiled"]
    assert compiled and all(r["verified"] for r in compiled)


def test_pending_refund_on_stack_deopt_has_no_duplicates(fast):
    recs = run("E", 70, [(55, {"knobs": {"refund_returns_pending": True}}, "pending")])
    osr = [r for r in recs if r["deopt_zone"] == "osr"]
    assert osr, "expected on-stack deopts after the provider starts returning pending"
    assert all(r["verified"] and r["duplicate_effects"] == 0 for r in osr)
    assert all(r["fenced"] >= 1 for r in osr)
    ev = col("deopt_events").find_one({"zone": "osr"})
    assert ev["pc"] == 4 and ev["frame"]["committed_effects"][0]["tool"] == "payments.refund"


def test_restart_on_deopt_duplicates_refunds(fast):
    recs = run("C", 70, [(55, {"knobs": {"refund_returns_pending": True}}, "pending")])
    restarted = [r for r in recs if r["deopt_zone"] == "restart"]
    assert restarted and all(r["duplicate_effects"] >= 1 for r in restarted)


def test_currency_drift_deopts_in_safe_zone(fast):
    recs = run("E", 70, [(55, {"knobs": {"currency_mix": 1.0}}, "eur")])
    safe = [r for r in recs if r["deopt_zone"] == "safe"]
    assert safe and all(r["verified"] and r["expected"] == "escalate" for r in safe)
    assert all(ev["frame"]["committed_effects"] == [] for ev in col("deopt_events").find({"zone": "safe"}))


def test_policy_drift_is_caught_by_shadow_and_demotes(fast, monkeypatch):
    monkeypatch.setattr(config, "SHADOW_ACTIVE_MIN_RATE", 1.0)
    run("E", 55, [])
    col("world_config").update_one({"_id": "knobs"}, {"$set": {"max_order_age_days": 29}})
    stream.apply({"policy": {"refund_window_days": 3}})
    recs = run("E", 25, [], start=55)
    skill = col("skills").find_one({"_id": "refund_standard@v1"})
    assert skill["status"] == "recompiling"
    assert col("shadow_runs").find_one({"adjudication": "skill_wrong"}) is not None
