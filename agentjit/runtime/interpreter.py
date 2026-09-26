"""The LLM interpreter: default executor, deopt resume target, shadow oracle (section 7.3).

Two implementations behind one entry point, run_interpreter():
- ClaudeAgent: a manual tool-use loop on Claude. Manual rather than the SDK
  tool runner because every call must pass through our gateway and tracer,
  and a resume must start from a rendered deopt frame.
- ScriptedAgent: a deterministic stand-in that follows the same policy, used
  when no credentials resolve (LLM_MODE=offline). Its costs are nominal and
  every metric it produces is marked simulated.

Both receive the current policy document in context. Neither ever sees
envelope.truth.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from agentjit.common import config, llm
from agentjit.common.models import DeoptFrame
from agentjit.common.tools import api_tool_defs, from_api_name
from agentjit.runtime.gateway import Gateway, UnknownTool
from agentjit.runtime.mockapi import world

ORDER_ID_RE = re.compile(r"\b([A-Z]\d{4})\b")


@dataclass
class InterpResult:
    finished: bool
    cost_usd: float
    latency_ms: int
    turns: int
    simulated: bool
    note: str = ""


def system_prompt(policy: dict) -> str:
    return f"""You are a customer-support agent that handles refund requests for an online store.
Every tool call you make is executed for real, except that repeating a call that already
completed in this task returns its recorded result instead of running again.

Current policy (authoritative; it can change between tasks):
- Refund window: {policy['refund_window_days']} days since delivery. Older orders are denied.
- Refundable currencies: {', '.join(policy['refundable_currencies'])}. Other currencies are escalated to finance.
- Orders shipped in more than {policy['max_auto_shipments']} parcel(s) are escalated for per-parcel review.

How to handle a request:
1. Look up the order and its shipments.
2. If the order is outside the refund window: set the ticket to closed and email the customer a denial.
3. Else if it must be escalated (currency or parcels): set the ticket to escalated and email the
   customer that the request is under review.
4. Otherwise compute the amount with compute_amount (pass total and discount), then call payments.refund.
   - If the refund status is succeeded: set the ticket to resolved and email a confirmation.
   - If it is pending: check payments.list_refunds, set the ticket to awaiting_refund, and email
     the customer that the refund is pending.
Send exactly one email per task, and mention the order ID in it. Never refund the same charge twice.
When the task is complete, reply with a one-line summary and no tool calls."""


def render_task(envelope: dict) -> str:
    fields = "\n".join(f"- {k}: {v}" for k, v in (envelope.get("structured") or {}).items())
    return f"New support request:\n\n{envelope['raw_text']}\n\nStructured fields:\n{fields or '- (none)'}"


def render_frame(frame: DeoptFrame) -> str:
    done = "\n".join(
        f"- step {e.step}: {e.tool} [{e.effect_class}] -> {json.dumps(e.result, default=str)}"
        for e in frame.committed_effects) or "- nothing with side effects yet"
    return f"""You are taking over a task midway. Compiled automation started it and stopped because
one of its assumptions did not hold.

Assumed: {frame.failed_guard}
Observed instead: {json.dumps(frame.observed, default=str)}
Known values: {json.dumps(frame.locals, default=str)}

Already done (these effects are committed; do not repeat them unless you mean a new, distinct action):
{done}

Remaining goal: {frame.remaining_goal}
Continue from here and finish the task under the current policy."""


# ----------------------------------------------------------------------------
class ClaudeAgent:
    def run(self, envelope: dict, gw: Gateway, frame: Optional[DeoptFrame]) -> InterpResult:
        policy = world.get_policy()
        user = render_task(envelope)
        if frame is not None:
            user += "\n\n" + render_frame(frame)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        usage = llm.Usage()
        for turn in range(1, config.INTERPRETER_MAX_TURNS + 1):
            resp, u = llm.create(config.INTERPRETER_MODEL, max_tokens=16000, system=system_prompt(policy),
                                 tools=api_tool_defs(), messages=messages,
                                 output_config={"effort": "low"})
            usage.add(u)
            if gw.tracer is not None:
                gw.tracer.extra_cost += u.cost_usd
            if resp.stop_reason != "tool_use":
                return InterpResult(True, usage.cost_usd, usage.latency_ms, turn, False, llm.text_of(resp)[:300])
            messages.append({"role": "assistant", "content": resp.content})
            span = llm.text_of(resp)[:200] or None
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                args = dict(block.input)
                distinct = bool(args.pop("declare_distinct", False))
                try:
                    out = gw.call(from_api_name(block.name), args, llm_span=span, declared_distinct=distinct)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(out, default=str)})
                except UnknownTool as e:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"unknown tool {e}", "is_error": True})
            messages.append({"role": "user", "content": results})
        return InterpResult(False, usage.cost_usd, usage.latency_ms, config.INTERPRETER_MAX_TURNS, False,
                            "turn limit reached")


# ----------------------------------------------------------------------------
class ScriptedAgent:
    """Deterministic stand-in for the LLM. Follows the same written policy.

    On resume it re-derives everything from the world under the same exec_id,
    re-issuing earlier calls the way a model that distrusts its context would;
    the gateway fences the repeats.
    """

    def run(self, envelope: dict, gw: Gateway, frame: Optional[DeoptFrame]) -> InterpResult:
        self.turns = 0
        self.gw = gw
        policy = world.get_policy()
        structured = envelope.get("structured") or {}
        m = ORDER_ID_RE.search(envelope["raw_text"])
        order = self._call("get_order", order_id=m.group(1)) if m else {"error": "no order id in request"}
        if "error" in order:
            if structured.get("ticket_id"):
                self._call("tickets.update", ticket_id=structured["ticket_id"], status="escalated")
            if structured.get("customer_email"):
                self._call("email.send", to=structured["customer_email"], subject="We received your request",
                           body="Thanks for reaching out. A member of our team will follow up shortly.")
            return self._done("escalated: could not identify an order")
        shipments = self._call("get_shipments", order_id=order["order_id"])
        name, oid, to = order["customer_name"], order["order_id"], order["customer_email"]

        if order["days_since_delivery"] > policy["refund_window_days"]:
            self._call("tickets.update", ticket_id=order["ticket_id"], status="closed")
            self._call("email.send", to=to, subject=f"Regarding your refund request for order {oid}",
                       body=f"Hi {name}, order {oid} was delivered {order['days_since_delivery']} days ago, "
                            f"outside our {policy['refund_window_days']}-day refund window, so we can't refund it.")
            return self._done("denied: outside window")
        if order["currency"] not in policy["refundable_currencies"] or len(shipments) > policy["max_auto_shipments"]:
            self._call("tickets.update", ticket_id=order["ticket_id"], status="escalated")
            self._call("email.send", to=to, subject="Your refund request is being reviewed",
                       body=f"Hi {name}, we've received your refund request for order {oid}. "
                            f"Our team will review it and follow up within 2 business days.")
            return self._done("escalated")

        amount = self._call("compute_amount", total=order["total"], discount=order.get("discount", 0.0))["amount"]
        refund = self._call("payments.refund", charge_id=order["charge_id"], amount=amount)
        if refund.get("status") != "succeeded":
            self._call("payments.list_refunds", charge_id=order["charge_id"])
            self._call("tickets.update", ticket_id=order["ticket_id"], status="awaiting_refund")
            self._call("email.send", to=to, subject="Your refund is being processed",
                       body=f"Hi {name}, your refund of ${amount:.2f} for order {oid} is pending with our "
                            f"payment provider. You'll get it as soon as it clears.")
            return self._done("refund pending")
        self._call("tickets.update", ticket_id=order["ticket_id"], status="resolved")
        self._call("email.send", to=to, subject="Your refund has been processed",
                   body=f"Hi {name}, your refund of ${amount:.2f} for order {oid} has been processed. "
                        f"You should see it in 3-5 business days.")
        return self._done("refunded")

    def _call(self, tool: str, **args: Any) -> Any:
        self.turns += 1
        return self.gw.call(tool, args, llm_span=f"scripted: {tool}",
                            cost=config.SIM_COST_INTERPRETER_TURN,
                            latency=config.SIM_LATENCY_INTERPRETER_TURN_MS)

    def _done(self, note: str) -> InterpResult:
        self.turns += 1  # the final no-tool turn
        if self.gw.tracer is not None:
            self.gw.tracer.extra_cost += config.SIM_COST_INTERPRETER_TURN
        cost = self.turns * config.SIM_COST_INTERPRETER_TURN
        return InterpResult(True, cost, self.turns * config.SIM_LATENCY_INTERPRETER_TURN_MS, self.turns, True, note)


def run_interpreter(envelope: dict, gw: Gateway, frame: Optional[DeoptFrame] = None) -> InterpResult:
    agent = ClaudeAgent() if llm.available() else ScriptedAgent()
    return agent.run(envelope, gw, frame)
