"""Tool gateway: the single chokepoint between agents and the world (section 7.4).

Every call, compiled or interpreted, passes through Gateway.call:
- effect class looked up in the registry; unregistered tools are refused
- pure ops run in-process; reads go to the world (or a snapshot in shadow mode)
- non-read calls follow the write-ahead protocol: intent -> external call with
  the effect_key as idempotency key -> completion
- a call matching a completed journal entry is fenced: the journaled result
  comes back and nothing executes
- a second irreversible call to the same tool + resource within one execution
  is blocked unless explicitly declared distinct (logged)
- an intent with no completion (a crash mid-call) is marked uncertain and
  reconciled against the world before anything else proceeds
- mode="shadow" stubs every write and records the intended call
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from agentjit.common.db import col
from agentjit.common.tools import TOOL_REGISTRY, get_effect_class, get_pure_fn, get_resource_id
from agentjit.runtime import journal
from agentjit.runtime.mockapi import world
from agentjit.runtime.tracer import Tracer

_EMAIL_KEYS = {"to", "customer_email"}


class UnknownTool(Exception):
    pass


class SimulatedCrash(Exception):
    """Raised by the crash hook between the external call and the completion record."""


def normalize(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in sorted(args):
        v = args[k]
        if isinstance(v, bool) or v is None:
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = round(float(v), 2)
        elif isinstance(v, str):
            v = " ".join(v.split())
            out[k] = v.lower() if k in _EMAIL_KEYS else v
        else:
            out[k] = v
    return out


def effect_key(exec_id: str, tool: str, norm_args: dict[str, Any]) -> str:
    # No pc: a resuming interpreter numbers its steps differently (skill_abi.md section 7).
    blob = json.dumps([exec_id, tool, norm_args], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def read_key(tool: str, norm_args: dict[str, Any]) -> str:
    return json.dumps([tool, norm_args], sort_keys=True, default=str)


def _shadow_stub(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool == "payments.refund":
        return {"refund_id": "ref_shadow", "charge_id": args.get("charge_id"), "amount": args.get("amount"),
                "status": "succeeded", "created_at": "shadow"}
    if tool == "tickets.update":
        return {"ticket_id": args.get("ticket_id"), "status": args.get("status"), "updated_at": "shadow"}
    if tool == "email.send":
        return {"message_id": "msg_shadow", "to": args.get("to"), "delivered": True}
    return {"ok": True, "shadow": True}


@dataclass
class GatewayStats:
    fenced: int = 0
    resource_blocked: int = 0
    reconciled: int = 0
    external_writes: int = 0


class Gateway:
    def __init__(self, exec_id: str, mode: str = "live", tracer: Optional[Tracer] = None,
                 read_snapshot: Optional[dict[str, Any]] = None, crash_after: Optional[str] = None):
        assert mode in ("live", "shadow")
        self.exec_id = exec_id
        self.mode = mode
        self.tracer = tracer
        self.read_snapshot = read_snapshot or {}
        self.crash_after = crash_after  # tool name: simulate a crash after its external call
        self.reads: dict[str, Any] = {}  # live reads, replayed to this run's shadow
        self.intended: list[tuple[str, dict[str, Any]]] = []  # shadow-mode writes
        self.stats = GatewayStats()
        self.pc: Optional[int] = None  # set by the compiled executor, stored on intents for audit

    # ------------------------------------------------------------------
    def call(self, tool: str, args: dict[str, Any], llm_span: Optional[str] = None,
             declared_distinct: bool = False, cost: float = 0.0, latency: int = 0) -> Any:
        if tool not in TOOL_REGISTRY:
            raise UnknownTool(tool)
        eff = get_effect_class(tool)
        norm = normalize(args)

        if eff == "pure":
            result = get_pure_fn(tool)(**args)
        elif eff == "read":
            result = self._read(tool, args, norm)
        elif self.mode == "shadow":
            self.intended.append((tool, norm))
            result = _shadow_stub(tool, args)
        else:
            self._reconcile_unfinished()
            result = self._write(tool, eff, args, norm, declared_distinct)

        if self.tracer is not None:
            self.tracer.record(tool, args, result, eff, llm_span=llm_span, cost=cost, latency=latency)
        return result

    # ------------------------------------------------------------------
    def _read(self, tool: str, args: dict[str, Any], norm: dict[str, Any]) -> Any:
        key = read_key(tool, norm)
        if self.mode == "shadow" and key in self.read_snapshot:
            return self.read_snapshot[key]
        result = world.WORLD_TOOLS[tool](**args)
        if self.mode == "live":
            self.reads[key] = result
        return result

    def _write(self, tool: str, eff: str, args: dict[str, Any], norm: dict[str, Any],
               declared_distinct: bool) -> Any:
        key = effect_key(self.exec_id, tool, norm)
        prior = journal.find(key)
        if prior and prior["state"] == "completed":
            self.stats.fenced += 1
            self._log("fenced", tool, key, args)
            return prior["result"]

        resource = get_resource_id(tool, args)
        if eff in ("irreversible", "compensable") and resource is not None:
            same_target = journal.find_resource(self.exec_id, tool, resource)
            if same_target and same_target["effect_key"] != key:
                if not declared_distinct:
                    self.stats.resource_blocked += 1
                    self._log("resource_fence", tool, key, args, prior_key=same_target["effect_key"])
                    blocked = dict(same_target["result"] or {})
                    blocked["fenced"] = f"{tool} already executed on {resource} in this execution"
                    return blocked
                self._log("declared_distinct", tool, key, args, prior_key=same_target["effect_key"])

        if not journal.insert_intent(key, self.exec_id, tool, eff, resource, norm, self.pc):
            # An intent with this key exists but never completed: reconcile first.
            self._reconcile(journal.find(key))
            done = journal.find(key)
            if done and done["state"] == "completed":
                self.stats.fenced += 1
                return done["result"]
            journal.insert_intent(key, self.exec_id, tool, eff, resource, norm, self.pc)

        result = world.WORLD_TOOLS[tool](**args, idempotency_key=key)
        self.stats.external_writes += 1
        if self.crash_after == tool:
            self.crash_after = None
            raise SimulatedCrash(f"crashed after external {tool} call, before completion record")
        journal.complete(key, result)
        return result

    def _reconcile_unfinished(self) -> None:
        for entry in journal.unfinished(self.exec_id):
            self._reconcile(entry)

    def _reconcile(self, entry: Optional[dict]) -> None:
        """Uncertain effect: ask the world whether it happened (the forced reconciliation read)."""
        if entry is None:
            return
        journal.mark_uncertain(entry["effect_key"])
        self.stats.reconciled += 1
        found = world.lookup_by_idempotency_key(entry["tool"], entry["effect_key"])
        if found is not None:
            journal.complete(entry["effect_key"], found)
            self._log("reconciled_completed", entry["tool"], entry["effect_key"], entry["args"])
        else:
            journal.drop(entry["effect_key"])  # never happened: safe to issue again
            self._log("reconciled_absent", entry["tool"], entry["effect_key"], entry["args"])

    def _log(self, event: str, tool: str, key: str, args: dict[str, Any], **extra: Any) -> None:
        col("gateway_events").insert_one({"event": event, "exec_id": self.exec_id, "tool": tool,
                                          "effect_key": key, "args": args, "ts": datetime.now(timezone.utc), **extra})
