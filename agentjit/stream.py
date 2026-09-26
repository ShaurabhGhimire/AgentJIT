"""Reproducible task streams with drift events at fixed offsets (section 14.4).

Same seed + same schedule => the same orders, the same request texts and the
same drift points for every arm.
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agentjit.common.db import col
from agentjit.runtime.mockapi import world

REASON_TEXT = {"damaged": "arrived damaged", "not_received": "never arrived", "wrong_item": "was the wrong item"}
TEMPLATES = [
    "Hi, my order {oid} {why}. Can I get a refund please?",
    "Order #{oid} {why}, I'd like my money back.",
    "Requesting a refund for order number {oid}; it {why}.",
    "Hello support, order {oid} {why}. Please refund me.",
]

# (task index, knob/policy changes, label). Warm-up compiles at ~20 tasks and
# probation needs ~100 clean shadow runs to activate at a 3% bound, so drift starts at 150.
DEFAULT_SCHEDULE: list[tuple[int, dict, str]] = [
    (150, {"knobs": {"currency_mix": 0.4}}, "currency_mix on (EUR orders)"),
    (170, {"knobs": {"currency_mix": 0.0}}, "currency_mix off"),
    (180, {"knobs": {"refund_returns_pending": True}}, "provider returns pending"),
    (195, {"knobs": {"refund_returns_pending": False}}, "provider back to succeeded"),
    (205, {"policy": {"refund_window_days": 14}}, "refund window 30 -> 14 (invisible drift)"),
]


def apply(change: dict) -> None:
    if change.get("knobs"):
        world.set_knobs(**change["knobs"])
    if change.get("policy"):
        world.set_policy(**change["policy"])


def make_envelope(rng: random.Random, index: int) -> dict:
    order = world.new_order(rng)
    text = rng.choice(TEMPLATES).format(oid=order["order_id"], why=REASON_TEXT[order["reason"]])
    env = {"envelope_id": f"env_{index:05d}_{uuid.uuid4().hex[:6]}", "tenant": "acme", "source": "webhook",
           "raw_text": text,
           "structured": {"ticket_id": order["ticket_id"], "customer_email": order["customer_email"]},
           "truth": {"order_id": order["order_id"]}}
    col("task_envelopes").insert_one(dict(env))
    return env


def run(n: int, seed: int, handle: Callable[[dict, random.Random], dict],
        schedule: Optional[list[tuple[int, dict, str]]] = None,
        on_task: Optional[Callable[[int, dict], None]] = None, start: int = 0,
        until: Optional[Callable[[], bool]] = None) -> list[dict]:
    """Two RNGs: one for the world (orders, texts), one for routing, so arms see identical worlds."""
    world_rng = random.Random(seed)
    route_rng = random.Random(seed + 1)
    events = {i: (c, label) for i, c, label in (schedule if schedule is not None else DEFAULT_SCHEDULE)}
    out = []
    for i in range(start, start + n):
        if i in events:
            apply(events[i][0])
            out.append({"event": events[i][1], "index": i})
        env = make_envelope(world_rng, i)
        rec = handle(env, route_rng)
        rec["index"] = i
        out.append(rec)
        if on_task:
            on_task(i, rec)
        if until is not None and until():
            break
    return out
