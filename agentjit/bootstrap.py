"""One call to get a prefix ready: collections, indexes, a fresh world, family centroids."""
from __future__ import annotations

from typing import Optional

from agentjit.common import db
from agentjit.compileplane import families
from agentjit.runtime import journal
from agentjit.runtime.mockapi import world


def bootstrap(prefix: Optional[str] = None, fresh: bool = True, policy: Optional[dict] = None,
              knobs: Optional[dict] = None) -> None:
    import scripts.seed_atlas as seed_atlas

    if prefix:
        db.use_prefix(prefix)
    if fresh:
        db.drop_prefix(db.config.DB_PREFIX)
    seed_atlas.seed()
    journal.ensure_indexes()
    world.reset(policy, knobs)
    families.build_centroids()
