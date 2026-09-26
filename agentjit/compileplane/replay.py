"""Replay-equivalence harness (section 8.4) and held-out replay (section 8.7 step 2).

The candidate program runs against each recorded trace with every tool mocked
to return the recorded response. It must issue the identical sequence of calls
with identical (normalized) arguments; hole arguments are excluded and holes
return the recorded value. Any mismatch rejects the draft.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from agentjit.common.skillcode import SkillLoadError, eval_guard, load_run
from agentjit.runtime.gateway import normalize


class Mismatch(Exception):
    pass


@dataclass
class ReplayResult:
    ok: bool
    trace_id: str
    diff: str = ""


class ReplayCtx:
    def __init__(self, trace: dict, template: dict):
        self.steps = trace["steps"]
        self.cursor = 0
        self.pc: Optional[int] = None
        self.bindings: dict[str, Any] = {}
        self.hole_args = {(h["pc"], h["arg"]) for h in template["holes"]}
        self.holes = {h["name"]: h for h in template["holes"]}

    def step(self, pc: int) -> None:
        if pc != self.cursor + 1:
            raise Mismatch(f"ctx.step({pc}) but the next recorded step is {self.cursor + 1}")
        self.pc = pc

    def call(self, tool: str, **args: Any) -> Any:
        if self.cursor >= len(self.steps):
            raise Mismatch(f"extra call {tool} after the recorded trace ended")
        rec = self.steps[self.cursor]
        name = rec.get("tool") or rec.get("op")
        if self.pc != rec["i"]:
            raise Mismatch(f"call {tool} at pc {self.pc}, recorded step is {rec['i']}")
        if tool != name:
            raise Mismatch(f"pc {rec['i']}: called {tool}, recorded {name}")
        got = {k: v for k, v in args.items() if (rec["i"], k) not in self.hole_args}
        want = {k: v for k, v in rec["args"].items() if (rec["i"], k) not in self.hole_args}
        if normalize(got) != normalize(want):
            raise Mismatch(f"pc {rec['i']} {tool}: args {normalize(got)} != recorded {normalize(want)}")
        self.cursor += 1
        return rec["result"]

    def hole(self, name: str, inputs: dict) -> Any:
        h = self.holes.get(name)
        if h is None:
            raise Mismatch(f"unknown hole {name}")
        return self.steps[h["pc"] - 1]["args"][h["arg"]]

    def bind(self, name: str, value: Any) -> None:
        self.bindings[name] = value


def replay_one(source: str, trace: dict, template: dict, args: dict,
               guards: Optional[list[str]] = None) -> ReplayResult:
    try:
        run = load_run(source)
        ctx = ReplayCtx(trace, template)
        run(ctx, args)
        if ctx.cursor != len(trace["steps"]):
            raise Mismatch(f"program stopped after {ctx.cursor} of {len(trace['steps'])} steps")
        for g in guards or []:
            if eval_guard(g, ctx.bindings) is not True:
                raise Mismatch(f"guard failed on held-out trace: {g}")
        return ReplayResult(True, trace["trace_id"])
    except (Mismatch, SkillLoadError) as e:
        return ReplayResult(False, trace["trace_id"], str(e))
    except Exception as e:  # anything the draft raises is a rejection, not a crash
        return ReplayResult(False, trace["trace_id"], f"{type(e).__name__}: {e}")


def args_for(trace: dict, template: dict) -> dict:
    """Recover the task inputs a trace was run with, from the positions bound to them."""
    out = {}
    for s, rec in zip(template["steps"], trace["steps"]):
        for arg, spec in s["args"].items():
            if spec["kind"] == "param" and spec["source"] == "input":
                out[spec["input"]] = rec["args"][arg]
    return out


def replay_all(source: str, traces: list[dict], template: dict,
               guards: Optional[list[str]] = None) -> list[ReplayResult]:
    return [replay_one(source, t, template, args_for(t, template), guards) for t in traces]
