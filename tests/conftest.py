import random
import uuid

import pytest

from agentjit.common import config, db


@pytest.fixture(autouse=True)
def isolated_db():
    """Each test gets its own collection prefix and runs offline."""
    old_prefix, old_mode = config.DB_PREFIX, config.LLM_MODE
    prefix = f"test_{uuid.uuid4().hex[:8]}"
    db.use_prefix(prefix)
    config.LLM_MODE = "offline"
    from agentjit.runtime import journal
    from agentjit.runtime.mockapi import world
    world.reset()
    journal.ensure_indexes()
    yield prefix
    db.drop_prefix(prefix)
    db.use_prefix(old_prefix)
    config.LLM_MODE = old_mode


@pytest.fixture
def rng():
    return random.Random(7)


def make_envelope(order: dict, eid: str | None = None) -> dict:
    """Store an envelope for an order the way the task stream does."""
    from agentjit.common.db import col
    eid = eid or f"env_{uuid.uuid4().hex[:8]}"
    env = {"envelope_id": eid, "tenant": "acme", "source": "webhook",
           "raw_text": f"Hi, my order {order['order_id']} arrived damaged. Can I get a refund please?",
           "structured": {"ticket_id": order["ticket_id"], "customer_email": order["customer_email"]},
           "truth": {"order_id": order["order_id"]}}
    col("task_envelopes").insert_one(dict(env))
    return env
