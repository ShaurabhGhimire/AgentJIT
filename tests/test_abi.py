"""The Phase 0 exit check: the v0 fixture skill runs under the executor (section 2.5)."""
import json
import pathlib

from agentjit.common import config
from agentjit.runtime import executor
from agentjit.runtime.gateway import Gateway
from agentjit.runtime.mockapi import world
from agentjit.runtime.tracer import Tracer

ROOT = pathlib.Path(__file__).resolve().parents[1]


def v0() -> dict:
    return json.loads((ROOT / "fixtures/skills/refund_standard_v0.json").read_text())


def test_v0_runs_and_produces_the_modal_signature(rng, monkeypatch):
    monkeypatch.setattr(config, "SKILLS_DIR", str(ROOT / "skills"))
    order = world.new_order(rng)
    tracer = Tracer({"order_id": order["order_id"]}, "refund_request", "compiled", "ex_v0")
    out = executor.execute(v0(), Gateway("ex_v0", tracer=tracer), {"order_id": order["order_id"]})
    assert out.ok, out.deopt
    assert tracer.signature == "get_order>get_shipments>payments.refund>tickets.update>email.send"
    assert [s.i for s in tracer.steps] == [1, 2, 3, 4, 5, 6]


def test_v0_entry_guard_deopts_in_safe_zone(rng, monkeypatch):
    monkeypatch.setattr(config, "SKILLS_DIR", str(ROOT / "skills"))
    world.set_knobs(currency_mix=1.0)
    order = world.new_order(rng)
    out = executor.execute(v0(), Gateway("ex_v0b"), {"order_id": order["order_id"]})
    assert not out.ok and out.deopt.kind == "entry" and out.deopt.pc == 4
    assert out.deopt.observed == "EUR"
    assert world.list_refunds(order["charge_id"]) == []


def test_v0_residual_guard_fires_after_the_refund(rng, monkeypatch):
    monkeypatch.setattr(config, "SKILLS_DIR", str(ROOT / "skills"))
    world.set_knobs(refund_returns_pending=True)
    order = world.new_order(rng)
    out = executor.execute(v0(), Gateway("ex_v0c"), {"order_id": order["order_id"]})
    assert not out.ok and out.deopt.kind == "post" and out.deopt.pc == 4
    assert len(world.list_refunds(order["charge_id"])) == 1


def test_watcher_resumes_from_stored_token(rng):
    from agentjit.common.db import col
    from agentjit.compileplane import watchers
    col("shadow_runs").insert_one({"skill": "x", "diverged": False})
    import threading
    t = threading.Thread(target=watchers.watch, args=("shadows",), kwargs={"max_events": 1})
    t.start()
    import time
    time.sleep(0.5)
    col("shadow_runs").insert_one({"skill": "x", "diverged": True, "adjudication": "skill_wrong"})
    t.join(timeout=10)
    assert col("watcher_state").find_one({"_id": "shadows"})["resume_token"]
