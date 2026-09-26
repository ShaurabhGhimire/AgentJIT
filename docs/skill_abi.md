# Skill ABI

This document is the hard contract between Susan's codegen (`compileplane/codegen.py`) and Saurav's executor (`runtime/executor.py`).
A silent change here at hour 20 costs an afternoon.
Any change requires a PR and the other person's ack.

---

## 1. Entry point

Every generated skill file must expose exactly one function:

```python
def run(ctx: SkillContext, args: dict) -> dict:
    ...
```

- `args` contains the typed, grounded arguments from the task parser, keyed by the names in `input_signature`.
- The return value is a dict with at least `{"ok": True}` on success.
- The function must not import the mock API or call any external system directly.
All external calls go through `ctx`.

---

## 2. pc numbering

- pc is **1-indexed**.
- There is one pc per entry in the skill's `steps[]` document, including pure (`op`) steps.
- The generated code must call `ctx.step(pc)` as the first line of each step, before any `ctx.call`, `ctx.hole`, or computation that reads step results.
- pc values must match the `pc` field in the skill document exactly.
The deopt frame names `pc`; if codegen and executor number differently, every frame is wrong.

Example:

```python
def run(ctx, args):
    ctx.step(1)
    order = ctx.call("get_order", order_id=args["order_id"])
    ctx.bind("order", order)

    ctx.step(2)
    shipments = ctx.call("get_shipments", order_id=order["order_id"])
    ctx.bind("shipments", shipments)

    ctx.step(3)
    amount = order["total"]
    ctx.bind("amount", amount)

    ctx.step(4)
    refund = ctx.call("payments.refund", charge_id=order["charge_id"], amount=amount)
    ctx.bind("refund", refund)

    ctx.step(5)
    ticket = ctx.call("tickets.update", ticket_id=order["ticket_id"], status="resolved")
    ctx.bind("ticket", ticket)

    ctx.step(6)
    body = ctx.hole("email_body", inputs={"order_id": args["order_id"], "amount": amount})
    ctx.call("email.send", to=order["customer_email"], subject="Your refund has been processed", body=body)

    return {"ok": True}
```

---

## 3. Guard namespace

Guards are string expressions evaluated by the executor in a restricted `eval` over a binding dict.
Codegen declares a binding name for each step result via `ctx.bind(name, value)`.

Standard binding names for `refund_standard`:

| Binding | Source |
|---|---|
| `order` | result of `get_order` (step 1) |
| `shipments` | result of `get_shipments` (step 2) |
| `amount` | computed refund amount (step 3) |
| `refund` | result of `payments.refund` (step 4) |
| `ticket` | result of `tickets.update` (step 5) |

Guard expressions reference these names:

```
len(shipments) == 1
order.currency in ['USD']
days_since_delivery <= 30
0 < amount <= order['total']
refund['status'] == 'succeeded'
```

The executor evaluates guards using Python's `eval` with `{"__builtins__": {}, "len": len}` plus the binding dict.
Codegen must use the same binding names the executor expects; they are the column `Binding` in the table above.

---

## 4. Hole invocation

A hole is a bounded LLM call inside the skill.
Invocation:

```python
value = ctx.hole(name, inputs={...})
```

- `name` must match a key in the skill document's `holes[]` array.
- `inputs` is a dict of values the LLM needs to generate the output.
The executor owns the model call, schema validation, and the `checks[]` evaluation.
- The return value is the validated, checked output.
- Codegen must not construct the hole prompt itself or call the model directly.

---

## 5. Tool calls

```python
result = ctx.call(tool_name, **kwargs)
```

- `tool_name` must be a key in `common/tools.py:TOOL_REGISTRY`.
- All kwargs become the `args` dict in the effect journal entry.
- The executor routes the call through the gateway, which does journaling, fencing, and idempotency.
- Generated code must never `import` the mock API or any external client directly.

---

## 6. Tool registry

Source of truth: `agentjit/common/tools.py`.

| Tool | Effect class | Resource id |
|---|---|---|
| `get_order` | read | - |
| `get_shipments` | read | - |
| `payments.refund` | irreversible | `args["charge_id"]` |
| `tickets.update` | idempotent | `args["ticket_id"]` |
| `email.send` | irreversible | `args["to"]` |

---

## 7. Verifier signature

Saurav owns the implementation in `runtime/verifier.py`.
Susan's shadow adjudication calls it.
Agreed interface:

```python
from agentjit.common.models import VerifierResult

def verify(envelope_id: str) -> VerifierResult:
    ...
```

`VerifierResult` is defined in `common/models.py`:

```python
class VerifierResult(BaseModel):
    ok: bool
    reason: str
```

---

## 8. What codegen must never do

- Import `agentjit.runtime.mockapi` or any API client directly.
- Call `eval` or `exec` itself.
- Use `ctx.bind` with a name not listed in the guard namespace table above (for this skill family).
- Skip a `ctx.step(pc)` call.
- Renumber pcs relative to the skill document.
