"""Hard pass/fail verifier: checks expected end state for a given envelope.

Owner: Saurav (T1.2 in his P1 track).
Interface agreed in docs/skill_abi.md section 8.

The expected outcome is computed from the order and the *current* policy
document, read at verification time. That is what lets it adjudicate the
policy-drift divergence that no guard can see.
"""
from agentjit.common.db import col
from agentjit.common.models import VerifierResult
from agentjit.runtime.mockapi import world


def expected_outcome(order: dict, shipments: list[dict], policy: dict) -> str:
    if order["days_since_delivery"] > policy["refund_window_days"]:
        return "deny"
    if order["currency"] not in policy["refundable_currencies"]:
        return "escalate"
    if len(shipments) > policy["max_auto_shipments"]:
        return "escalate"
    return "refund"


def verify(envelope_id: str) -> VerifierResult:
    env = col("task_envelopes").find_one({"envelope_id": envelope_id})
    if env is None or not env.get("truth", {}).get("order_id"):
        return VerifierResult(ok=False, reason="no ground truth for envelope")
    order = world.get_order(env["truth"]["order_id"])
    if "error" in order:
        return VerifierResult(ok=False, reason=order["error"])
    shipments = world.get_shipments(order["order_id"])
    want = expected_outcome(order, shipments, world.get_policy())

    refunds = world.list_refunds(order["charge_id"])
    emails = list(col("world_outbox").find({"to": order["customer_email"]}))
    ticket = col("world_tickets").find_one({"ticket_id": order["ticket_id"]}) or {}
    dups = max(0, len(refunds) - 1) + max(0, len(emails) - 1)

    def result(ok: bool, reason: str) -> VerifierResult:
        return VerifierResult(ok=ok, reason=reason, expected=want, duplicate_effects=dups)

    if len(refunds) > 1:
        return result(False, f"duplicate refund: {len(refunds)} refunds on {order['charge_id']}")
    if len(emails) != 1:
        return result(False, f"expected exactly one email, found {len(emails)}")
    status = ticket.get("status")
    if want == "refund":
        if not refunds:
            return result(False, "refund expected, none issued")
        amount = round(order["total"] - order.get("discount", 0.0), 2)
        if abs(refunds[0]["amount"] - amount) > 0.005:
            return result(False, f"refund amount {refunds[0]['amount']} != {amount}")
        if refunds[0]["status"] == "pending":
            if status != "awaiting_refund":
                return result(False, f"refund pending but ticket is {status}")
        elif status != "resolved":
            return result(False, f"ticket is {status}, expected resolved")
        return result(True, "refunded")
    if refunds:
        return result(False, f"{want} expected under current policy, but a refund was issued")
    target = "closed" if want == "deny" else "escalated"
    if status != target:
        return result(False, f"ticket is {status}, expected {target}")
    return result(True, want)
