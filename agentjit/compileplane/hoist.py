"""Guard hoisting against the point of no return (section 8.6).

The point of no return is the first compensable or irreversible step. Every
guard whose inputs are bound before it becomes an entry (state) guard checked
in the safe zone, where a deopt costs nothing to undo. Guards over a side
effect's result stay behind it as residual postconditions; they are the only
places the full on-stack deopt protocol is needed.
"""
from __future__ import annotations

from typing import Optional


def point_of_no_return(template: dict) -> Optional[int]:
    for s in template["steps"]:
        if s["effect"] in ("compensable", "irreversible"):
            return s["pc"]
    return None


def hoist(template: dict, guards: list[dict]) -> dict:
    ponr = point_of_no_return(template)
    inputs = set(template["inputs"])
    task, entry, residual = [], [], {}
    for g in guards:
        if g.get("post"):
            residual.setdefault(g["pc"], []).append(g["expr"])
        elif g.get("inputs_only") or (g["pc"] == 0 and inputs):
            task.append({"expr": g["expr"], "support": g["support"], "kind": "task"})
        elif ponr is None or g["pc"] < ponr:
            entry.append({"expr": g["expr"], "support": g["support"], "kind": "state"})
        else:
            # depends on a value only available after the point of no return
            residual.setdefault(g["pc"], []).append(g["expr"])
    return {
        "point_of_no_return": ponr,
        "task_guards": task,
        "entry_guards": entry,
        "residual": residual,
        "safe_zone": [s["pc"] for s in template["steps"] if ponr is None or s["pc"] < ponr],
        "osr_zone": [s["pc"] for s in template["steps"] if ponr is not None and s["pc"] >= ponr],
    }
