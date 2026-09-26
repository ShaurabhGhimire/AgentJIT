"""Task families: centroids and match_family() for the parser (T1.2).

Centroids are the normalized mean embedding of a family's example envelopes.
match_family ranks *all* of a tenant's families so the top-two margin is
meaningful, then the caller routes to the interpreter unless the winner has a
servable (active or probation) skill.
"""
from __future__ import annotations

from typing import Optional

from pymongo.errors import OperationFailure

from agentjit.common import embed
from agentjit.common.db import col
from agentjit.common.models import FamilyMatch

# Example requests for families we never compile. They exist so an off-topic
# request lands near its own centroid instead of scoring "unknown" against refunds.
REFUND_EXAMPLES = [
    "Hi, my order A1042 arrived damaged. Can I get a refund please?",
    "My order B2210 never arrived, I want my money back.",
    "Order C3301 was the wrong item, please refund me.",
    "Requesting a refund for order number D4410; it arrived damaged.",
    "Hello support, order E5521 never arrived. Please refund me.",
    "Order #F6632 was the wrong item, I'd like my money back.",
]

OTHER_FAMILIES = {
    "address_change": [
        "Please update the shipping address on my account to 12 Oak Street.",
        "I moved, can you change my delivery address for future orders?",
        "Wrong address on my profile, please correct the street and zip code.",
    ],
    "billing_dispute": [
        "I was charged twice this month and the invoice total looks wrong.",
        "My subscription price went up without notice, I dispute this charge.",
        "The contract says a lower rate, please adjust my bill and credit the difference.",
    ],
    "order_status": [
        "Where is my package? The tracking has not updated in days.",
        "When will my order ship? I placed it last week.",
        "Can you tell me the delivery date for my recent purchase?",
    ],
}


def upsert_family(family_id: str, texts: list[str], tenant: str = "acme") -> dict:
    vecs = embed.embed(texts)
    doc = {"family_id": family_id, "tenant": tenant, "centroid": embed.centroid(vecs),
           "examples": len(texts)}
    col("task_families").update_one({"family_id": family_id},
                                    {"$set": doc, "$setOnInsert": {"has_servable_skill": False,
                                                                   "active_skill": None, "probation_skill": None}},
                                    upsert=True)
    return doc


def build_centroids(tenant: str = "acme") -> list[str]:
    """Refund centroid from stored envelopes that led to refund_request traces, plus the others."""
    env_ids = [t["envelope_id"] for t in col("traces").find({"family": "refund_request", "envelope_id": {"$ne": None}},
                                                            {"envelope_id": 1})]
    texts = [e["raw_text"] for e in col("task_envelopes").find({"envelope_id": {"$in": env_ids}}, {"raw_text": 1})]
    texts = texts + REFUND_EXAMPLES
    upsert_family("refund_request", texts, tenant)
    for fid, examples in OTHER_FAMILIES.items():
        upsert_family(fid, examples, tenant)
    return ["refund_request", *OTHER_FAMILIES]


def set_servable(family_id: str) -> None:
    """Recompute has_servable_skill from the family's skill pointers."""
    fam = col("task_families").find_one({"family_id": family_id}) or {}
    servable = bool(fam.get("active_skill") or fam.get("probation_skill"))
    col("task_families").update_one({"family_id": family_id}, {"$set": {"has_servable_skill": servable}})


def match_family(text: str, tenant: str = "acme", vector: Optional[list[float]] = None) -> tuple[Optional[FamilyMatch], bool]:
    """Returns (best match with margin, whether it has a servable skill)."""
    vec = vector or embed.embed_one(text)
    ranked: list[tuple[str, float, bool]] = []
    try:
        rows = col("task_families").aggregate([
            {"$vectorSearch": {"index": "family_centroid", "path": "centroid", "queryVector": vec,
                               "numCandidates": 50, "limit": 5, "filter": {"tenant": tenant}}},
            {"$project": {"family_id": 1, "has_servable_skill": 1, "score": {"$meta": "vectorSearchScore"}}},
        ])
        # Atlas cosine scores are (1 + cos) / 2; map back to cosine
        ranked = [(r["family_id"], 2 * r["score"] - 1, bool(r.get("has_servable_skill"))) for r in rows]
    except OperationFailure:
        ranked = sorted(((f["family_id"], embed.cosine(vec, f["centroid"]), bool(f.get("has_servable_skill")))
                         for f in col("task_families").find({"tenant": tenant})), key=lambda r: -r[1])
    if not ranked:
        return None, False
    best = ranked[0]
    margin = best[1] - (ranked[1][1] if len(ranked) > 1 else 0.0)
    return FamilyMatch(id=best[0], similarity=round(best[1], 4), margin=round(margin, 4)), best[2]
