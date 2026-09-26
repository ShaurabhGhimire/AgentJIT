"""The mock business world: orders, shipments, charges, refunds, tickets, outbox.

Mongo-backed, so drift can be injected by editing documents. Every write
accepts an idempotency_key and deduplicates on it the way a real payments
provider would. The policy document is what the interpreter is told and what
the verifier judges against; it is deliberately not exposed as a tool.
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from agentjit.common.db import col

DEFAULT_POLICY = {
    "_id": "policy",
    "refund_window_days": 30,
    "refundable_currencies": ["USD"],
    "max_auto_shipments": 1,
}
DEFAULT_KNOBS = {
    "_id": "knobs",
    "currency_mix": 0.0,           # share of new orders paid in EUR
    "split_shipment_rate": 0.0,    # share of new orders shipped in 2 parcels
    "refund_returns_pending": False,  # provider returns pending, not succeeded
    "max_order_age_days": 29,      # new orders are delivered 1..N days ago
}

FIRST_NAMES = ["Ana", "Li", "Sam", "Omar", "Priya", "Kim", "Tom", "Lena", "Raj", "Maya",
               "Noah", "Zoe", "Ivan", "Chen", "Ada", "Leo", "Nina", "Hans", "Cara", "Yuki"]
REASONS = ["damaged", "not_received", "wrong_item"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def reset(policy: Optional[dict] = None, knobs: Optional[dict] = None) -> None:
    for name in ("world_orders", "world_shipments", "world_refunds", "world_tickets", "world_outbox"):
        col(name).delete_many({})
    col("world_config").replace_one({"_id": "policy"}, {**DEFAULT_POLICY, **(policy or {})}, upsert=True)
    col("world_config").replace_one({"_id": "knobs"}, {**DEFAULT_KNOBS, **(knobs or {})}, upsert=True)
    col("world_orders").create_index("order_id", unique=True)
    col("world_refunds").create_index("charge_id")
    col("world_refunds").create_index("idempotency_key")
    col("world_outbox").create_index("to")
    col("world_outbox").create_index("idempotency_key")


def get_policy() -> dict:
    return col("world_config").find_one({"_id": "policy"}, {"_id": 0}) or {k: v for k, v in DEFAULT_POLICY.items() if k != "_id"}


def get_knobs() -> dict:
    return col("world_config").find_one({"_id": "knobs"}, {"_id": 0}) or {k: v for k, v in DEFAULT_KNOBS.items() if k != "_id"}


def set_policy(**changes: Any) -> dict:
    col("world_config").update_one({"_id": "policy"}, {"$set": changes}, upsert=True)
    return get_policy()


def set_knobs(**changes: Any) -> dict:
    col("world_config").update_one({"_id": "knobs"}, {"$set": changes}, upsert=True)
    return get_knobs()


def new_order(rng: random.Random, knobs: Optional[dict] = None) -> dict:
    """Create an order with its charge, shipments and ticket, drawn per the knobs."""
    knobs = knobs or get_knobs()
    seq = col("world_config").find_one_and_update(
        {"_id": "seq"}, {"$inc": {"n": 1}}, upsert=True, return_document=True)["n"]
    order_id = f"{chr(ord('A') + seq % 26)}{1000 + seq:04d}"
    name = rng.choice(FIRST_NAMES)
    email = f"{name.lower()}.{seq}@example.com"
    total = round(rng.uniform(12, 400), 2)
    discount = round(rng.choice([0, 0, 0, 5, 10]) if total > 60 else 0.0, 2)
    currency = "EUR" if rng.random() < knobs.get("currency_mix", 0) else "USD"
    parcels = 2 if rng.random() < knobs.get("split_shipment_rate", 0) else 1
    # Refund requests skew recent: exponential with a 7-day mean, capped.
    age = min(int(knobs.get("max_order_age_days", 29)), 1 + int(rng.expovariate(1 / 7)))
    delivered_at = _now() - timedelta(days=age, hours=rng.randint(0, 6))
    order = {
        "order_id": order_id, "charge_id": f"ch_{seq}", "total": total, "discount": discount,
        "currency": currency, "status": "delivered", "customer_email": email,
        "customer_name": name, "ticket_id": f"TKT-{seq}", "delivered_at": delivered_at,
        "reason": rng.choice(REASONS),
    }
    col("world_orders").insert_one(dict(order))
    for p in range(parcels):
        col("world_shipments").insert_one({
            "shipment_id": f"SHP-{seq}{'ab'[p] if parcels > 1 else ''}", "order_id": order_id,
            "tracking": f"1Z{seq:010d}{p}", "status": "delivered", "delivered_at": delivered_at})
    col("world_tickets").insert_one({"ticket_id": order["ticket_id"], "status": "open", "history": []})
    return order


def age_order(order_id: str, days: int) -> None:
    """Move an order's delivery date back (used to build policy-drift scenarios)."""
    when = _now() - timedelta(days=days, hours=1)
    col("world_orders").update_one({"order_id": order_id}, {"$set": {"delivered_at": when}})
    col("world_shipments").update_many({"order_id": order_id}, {"$set": {"delivered_at": when}})


# ---- tools --------------------------------------------------------------

def get_order(order_id: str) -> dict:
    o = col("world_orders").find_one({"order_id": order_id}, {"_id": 0})
    if o is None:
        return {"error": "order_not_found", "order_id": order_id}
    delivered = _aware(o.pop("delivered_at"))
    o["days_since_delivery"] = (_now() - delivered).days
    return o


def get_shipments(order_id: str) -> list[dict]:
    rows = list(col("world_shipments").find({"order_id": order_id}, {"_id": 0, "order_id": 0}))
    for r in rows:
        r["delivered_at"] = _aware(r["delivered_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    return rows


def list_refunds(charge_id: str) -> list[dict]:
    return list(col("world_refunds").find({"charge_id": charge_id}, {"_id": 0, "idempotency_key": 0}))


def refund(charge_id: str, amount: float, idempotency_key: Optional[str] = None) -> dict:
    if idempotency_key:
        prior = col("world_refunds").find_one({"idempotency_key": idempotency_key}, {"_id": 0, "idempotency_key": 0})
        if prior:
            return prior
    if col("world_orders").find_one({"charge_id": charge_id}) is None:
        return {"error": "charge_not_found", "charge_id": charge_id}
    status = "pending" if get_knobs().get("refund_returns_pending") else "succeeded"
    doc = {"refund_id": f"ref_{uuid.uuid4().hex[:10]}", "charge_id": charge_id, "amount": round(float(amount), 2),
           "status": status, "created_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"), "idempotency_key": idempotency_key}
    col("world_refunds").insert_one(dict(doc))
    doc.pop("idempotency_key")
    return doc


def update_ticket(ticket_id: str, status: str, idempotency_key: Optional[str] = None) -> dict:
    res = col("world_tickets").find_one_and_update(
        {"ticket_id": ticket_id},
        {"$set": {"status": status}, "$push": {"history": {"status": status, "at": _now()}}},
        return_document=True)
    if res is None:
        return {"error": "ticket_not_found", "ticket_id": ticket_id}
    return {"ticket_id": ticket_id, "status": status, "updated_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ")}


def send_email(to: str, subject: str, body: str, idempotency_key: Optional[str] = None) -> dict:
    if idempotency_key:
        prior = col("world_outbox").find_one({"idempotency_key": idempotency_key})
        if prior:
            return {"message_id": prior["message_id"], "to": prior["to"], "delivered": True}
    mid = f"msg_{uuid.uuid4().hex[:10]}"
    col("world_outbox").insert_one({"message_id": mid, "to": to, "subject": subject, "body": body,
                                    "idempotency_key": idempotency_key, "at": _now()})
    return {"message_id": mid, "to": to, "delivered": True}


def lookup_by_idempotency_key(tool: str, key: str) -> Optional[dict]:
    """Reconciliation: did an effect with this key actually happen?"""
    if tool == "payments.refund":
        return col("world_refunds").find_one({"idempotency_key": key}, {"_id": 0, "idempotency_key": 0})
    if tool == "email.send":
        m = col("world_outbox").find_one({"idempotency_key": key})
        return {"message_id": m["message_id"], "to": m["to"], "delivered": True} if m else None
    return None  # idempotent tools are safe to simply re-run


WORLD_TOOLS = {
    "get_order": get_order,
    "get_shipments": get_shipments,
    "payments.list_refunds": list_refunds,
    "payments.refund": refund,
    "tickets.update": update_ticket,
    "email.send": send_email,
}
