"""Compiled skill executor: loads skill code, evaluates guards, calls holes (section 7.2).

Owner: Saurav (T2.3 in his P2 track).
Interface: see docs/skill_abi.md for the SkillContext contract.

Order of events for a skill with a point of no return (PONR) at pc P:
  steps 1..P-1 run (reads, pure ops) -> ctx.step(P) evaluates every hoisted
  entry guard -> a failure raises DeoptSignal in the safe zone, with nothing
  committed. After a write step, its residual postconditions are evaluated at
  the next ctx.step (or when run returns); a failure there is an on-stack
  deopt with effects already committed.
"""
from __future__ import annotations

import ast
import pathlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agentjit.common import config, llm
from agentjit.common.skillcode import eval_guard, load_run
from agentjit.runtime.gateway import Gateway

_cache: dict[str, tuple[float, Callable]] = {}


class DeoptSignal(Exception):
    def __init__(self, pc: int, guard: str, observed: Any, kind: str):
        super().__init__(f"deopt at pc {pc}: {guard} (observed {observed!r})")
        self.pc, self.guard, self.observed, self.kind = pc, guard, observed, kind


def load_skill_code(code_ref: str) -> Callable:
    path = pathlib.Path(code_ref)
    if not path.is_absolute():
        path = pathlib.Path(config.SKILLS_DIR).parent / code_ref
    mtime = path.stat().st_mtime
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    run = load_run(path.read_text(), str(path))
    _cache[str(path)] = (mtime, run)
    return run


def invalidate(code_ref: Optional[str] = None) -> None:
    """Called by the skills change-stream watcher on activation/demotion."""
    if code_ref is None:
        _cache.clear()
    else:
        _cache.pop(str(pathlib.Path(config.SKILLS_DIR).parent / code_ref), None)


def observe(expr: str, bindings: dict) -> Any:
    """The values a guard looked at, for the deopt frame: {"order['currency']": "EUR"}."""
    out = {}
    try:
        tree = ast.parse(expr, mode="eval").body
    except SyntaxError:
        return None
    operands = [tree.left, *tree.comparators] if isinstance(tree, ast.Compare) else [tree]
    for node in operands:
        src = ast.unparse(node)
        try:
            ast.literal_eval(node)
            continue  # a literal the guard compares against, not an observation
        except ValueError:
            pass
        try:
            out[src] = eval_guard(src, bindings)
        except Exception as e:
            out[src] = f"<{type(e).__name__}>"
    if len(out) == 1:
        return next(iter(out.values()))
    return out


# ----------------------------------------------------------------------------
def run_hole(hole: dict, inputs: dict) -> tuple[str, float, int, bool]:
    """Returns (text, cost, latency_ms, simulated)."""
    if llm.available():
        examples = "\n\n".join(e["text"] for e in hole.get("examples", [])[:2])
        prompt = (f"Write the `{hole['arg']}` field for a `{hole['tool']}` call.\n"
                  f"Inputs: {inputs}\n"
                  f"Requirements: {', '.join(hole.get('checks', [])) or 'none'}; "
                  f"at most {hole['schema'].get('maxLength', 1200)} characters.\n"
                  f"Examples of past outputs:\n{examples}\n\nReply with only the text.")
        resp, usage = llm.create(config.HOLE_MODEL, max_tokens=600, messages=[{"role": "user", "content": prompt}])
        return llm.text_of(resp).strip(), usage.cost_usd, usage.latency_ms, False
    # Offline: re-instantiate an observed output with this task's inputs.
    ex = hole["examples"][0]
    text = re.sub(r"^Hi [A-Z][a-z]+,", "Hi,", ex["text"])
    for name, old in ex["inputs"].items():
        new = inputs.get(name)
        if old is None or new is None:
            continue
        if isinstance(old, (int, float)):
            text = text.replace(f"{old:.2f}", f"{new:.2f}")
        else:
            text = text.replace(str(old), str(new))
    return text, config.SIM_COST_HOLE, config.SIM_LATENCY_HOLE_MS, True


def check_hole(hole: dict, value: Any, inputs: dict) -> Optional[str]:
    schema = hole.get("schema", {})
    if not isinstance(value, str):
        return "hole output is not a string"
    if len(value) > schema.get("maxLength", 10_000) or not value.strip():
        return "hole output length out of bounds"
    for check in hole.get("checks", []):
        m = re.fullmatch(r"mentions\((\w+)\)", check)
        if m and str(inputs.get(m.group(1))) not in value:
            return f"hole output fails {check}"
    return None


# ----------------------------------------------------------------------------
class SkillContext:
    def __init__(self, skill: dict, gw: Gateway, args: dict, guards_enabled: bool = True):
        self.skill = skill
        self.gw = gw
        self.args = args
        self.guards_enabled = guards_enabled
        self.pc = 0
        self.bindings: dict[str, Any] = {}
        self.ponr = skill.get("hoist", {}).get("point_of_no_return")
        self.entry_checked = False
        self.posts = {s["pc"]: s.get("post", []) for s in skill["steps"]}
        self.holes = {h["name"]: h for h in skill.get("holes", [])}
        self.hole_cost = 0.0
        self.hole_latency = 0
        self.simulated = False

    def _check(self, exprs: list[str], pc: int, kind: str) -> None:
        if not self.guards_enabled:
            return
        for expr in exprs:
            try:
                ok = eval_guard(expr, self.bindings) is True
            except Exception:
                ok = False
            if not ok:
                raise DeoptSignal(pc, expr if kind == "entry" else f"post: {expr}", observe(expr, self.bindings), kind)

    def _check_entry(self) -> None:
        self.entry_checked = True
        self._check([g["expr"] for g in self.skill.get("entry_guards", [])], self.pc, "entry")

    def step(self, pc: int) -> None:
        if self.pc:
            self._check(self.posts.get(self.pc, []), self.pc, "post")
        self.pc = pc
        self.gw.pc = pc
        if self.ponr is not None and pc == self.ponr and not self.entry_checked:
            self._check_entry()

    def call(self, tool: str, **args: Any) -> Any:
        return self.gw.call(tool, args)

    def hole(self, name: str, inputs: dict) -> Any:
        hole = self.holes[name]
        text, cost, latency, simulated = run_hole(hole, inputs)
        self.hole_cost += cost
        self.hole_latency += latency
        self.simulated |= simulated
        if self.gw.tracer is not None:
            self.gw.tracer.extra_cost += cost
        problem = check_hole(hole, text, inputs)
        if problem:
            raise DeoptSignal(self.pc, f"hole: {name} {problem}", text[:200], "hole")
        return text

    def bind(self, name: str, value: Any) -> None:
        self.bindings[name] = value

    def finish(self) -> None:
        if self.pc:
            self._check(self.posts.get(self.pc, []), self.pc, "post")
        if not self.entry_checked:
            self._check_entry()

    def locals(self) -> dict:
        out = dict(self.args)
        for name, v in self.bindings.items():
            if isinstance(v, dict):
                for k, fv in v.items():
                    if isinstance(fv, (str, int, float, bool)):
                        out[f"{name}.{k}"] = fv
            elif isinstance(v, (str, int, float, bool)):
                out[name] = v
            elif isinstance(v, list):
                out[f"len({name})"] = len(v)
        return out


@dataclass
class CompiledOutcome:
    ok: bool
    ctx: SkillContext
    deopt: Optional[DeoptSignal] = None
    skill: Optional[dict] = None  # the branch that ran (polymorphic skills)
    branches_tried: int = 1


def execute(skill: dict, gw: Gateway, args: dict, guards_enabled: bool = True) -> CompiledOutcome:
    if skill.get("shape") == "polymorphic":
        return _execute_polymorphic(skill, gw, args, guards_enabled)
    out = _execute_one(skill, gw, args, guards_enabled)
    out.skill = skill
    return out


def _execute_polymorphic(skill: dict, gw: Gateway, args: dict, guards_enabled: bool) -> CompiledOutcome:
    """Inline-cache dispatch: try branches in order; a safe-zone entry-guard miss moves to the next."""
    from agentjit.common.db import col

    out = None
    for i, bid in enumerate(skill["branches"], 1):
        branch = col("skills").find_one({"_id": bid})
        if gw.tracer is not None:
            gw.tracer.steps = []  # discard the previous branch's reads; they had no effects
        out = _execute_one(branch, gw, args, guards_enabled)
        out.skill, out.branches_tried = branch, i
        if out.ok or out.deopt.kind != "entry":
            return out
    return out


def _execute_one(skill: dict, gw: Gateway, args: dict, guards_enabled: bool) -> CompiledOutcome:
    run = load_skill_code(skill["code_ref"])
    ctx = SkillContext(skill, gw, args, guards_enabled)
    try:
        run(ctx, args)
        ctx.finish()
        return CompiledOutcome(True, ctx)
    except DeoptSignal as d:
        return CompiledOutcome(False, ctx, deopt=d)
    except Exception as e:
        # Anything unexpected inside compiled code is a deopt too: route to the interpreter.
        return CompiledOutcome(False, ctx, deopt=DeoptSignal(ctx.pc or 1, f"exception: {type(e).__name__}",
                                                             str(e)[:200], "error"))
