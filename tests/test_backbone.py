"""Correctness backbone: world, gateway, journal, fencing, interpreter, verifier (T6.4)."""
import pytest

from agentjit.runtime import journal
from agentjit.runtime.gateway import Gateway, SimulatedCrash, UnknownTool
from agentjit.runtime.interpreter import ScriptedAgent
from agentjit.runtime.mockapi import world
from agentjit.runtime.tracer import Tracer
from agentjit.runtime.verifier import verify
from tests.conftest import make_envelope


def test_interpreted_refund_verifies_with_provenance(rng):
    order = world.new_order(rng)
    env = make_envelope(order)
    tracer = Tracer({}, "refund_request", "interpreted", "ex1", env["envelope_id"])
    res = ScriptedAgent().run(env, Gateway("ex1", tracer=tracer), None)
    assert res.finished
    assert verify(env["envelope_id"]).ok
    t = tracer.trace(True)
    assert t.signature == "get_order>get_shipments>payments.refund>tickets.update>email.send"
    refund = t.steps[3]
    assert refund.tool == "payments.refund"
    assert refund.provenance == {"charge_id": "step1.result.charge_id", "amount": "step3.result.amount"}
    assert t.steps[1].provenance == {"order_id": "step1.result.order_id"}


def test_same_call_twice_executes_once(rng):
    order = world.new_order(rng)
    gw = Gateway("ex2")
    a = gw.call("payments.refund", {"charge_id": order["charge_id"], "amount": 10})
    b = gw.call("payments.refund", {"charge_id": order["charge_id"], "amount": 10.00})
    assert a == b
    assert len(world.list_refunds(order["charge_id"])) == 1
    assert gw.stats.fenced == 1


def test_resource_fence_blocks_second_irreversible_call(rng):
    order = world.new_order(rng)
    gw = Gateway("ex3")
    gw.call("payments.refund", {"charge_id": order["charge_id"], "amount": 10})
    blocked = gw.call("payments.refund", {"charge_id": order["charge_id"], "amount": 12})
    assert "fenced" in blocked
    assert len(world.list_refunds(order["charge_id"])) == 1
    gw.call("payments.refund", {"charge_id": order["charge_id"], "amount": 12}, declared_distinct=True)
    assert len(world.list_refunds(order["charge_id"])) == 2


def test_crash_between_call_and_completion_is_reconciled(rng):
    order = world.new_order(rng)
    gw = Gateway("ex4", crash_after="payments.refund")
    with pytest.raises(SimulatedCrash):
        gw.call("payments.refund", {"charge_id": order["charge_id"], "amount": 10})
    assert journal.unfinished("ex4")[0]["state"] == "intent"
    # process restarts; the next non-read call forces reconciliation first
    gw2 = Gateway("ex4")
    gw2.call("tickets.update", {"ticket_id": order["ticket_id"], "status": "resolved"})
    assert gw2.stats.reconciled == 1
    again = gw2.call("payments.refund", {"charge_id": order["charge_id"], "amount": 10})
    assert again["status"] == "succeeded"
    assert len(world.list_refunds(order["charge_id"])) == 1


def test_unknown_tool_refused():
    with pytest.raises(UnknownTool):
        Gateway("ex5").call("payments.wire_transfer", {})


def test_shadow_mode_stubs_writes(rng):
    order = world.new_order(rng)
    gw = Gateway("ex6", mode="shadow")
    gw.call("payments.refund", {"charge_id": order["charge_id"], "amount": 10})
    assert world.list_refunds(order["charge_id"]) == []
    assert gw.intended[0][0] == "payments.refund"


def test_verifier_policy_outcomes(rng):
    world.set_knobs(currency_mix=1.0)
    eur = world.new_order(rng)
    env = make_envelope(eur)
    ScriptedAgent().run(env, Gateway("ex7"), None)
    r = verify(env["envelope_id"])
    assert r.ok and r.expected == "escalate"

    world.set_knobs(currency_mix=0.0, refund_returns_pending=True)
    order = world.new_order(rng)
    env = make_envelope(order)
    ScriptedAgent().run(env, Gateway("ex8"), None)
    r = verify(env["envelope_id"])
    assert r.ok, r.reason

    world.set_knobs(refund_returns_pending=False)
    old = world.new_order(rng)
    world.age_order(old["order_id"], 20)
    world.set_policy(refund_window_days=14)
    env = make_envelope(old)
    ScriptedAgent().run(env, Gateway("ex9"), None)
    r = verify(env["envelope_id"])
    assert r.ok and r.expected == "deny"
