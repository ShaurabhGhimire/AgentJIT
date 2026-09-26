from typing import Any, Callable, Optional

# Tool registry: name -> effect_class + resource_id extractor
# effect_class values: read, pure, idempotent, compensable, irreversible
# resource_id: callable(args) -> str, or None for reads/pure
# fn: pure ops only; the gateway runs it in-process with no journal entry.
#     Pure ops are exposed to the interpreter as tools so computed values
#     (e.g. the refund amount) get a trace step and a provenance edge.
# description / params: what the interpreter's model sees.

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "get_order": {
        "effect_class": "read",
        "resource_id": None,
        "description": "Look up an order. Returns charge_id, total, discount, currency, status, customer_email, customer_name, ticket_id, days_since_delivery, reason.",
        "params": {"order_id": {"type": "string"}},
    },
    "get_shipments": {
        "effect_class": "read",
        "resource_id": None,
        "description": "List the shipments (parcels) of an order.",
        "params": {"order_id": {"type": "string"}},
    },
    "payments.list_refunds": {
        # reconciliation read for uncertain effects, and the pending-refund path
        "effect_class": "read",
        "resource_id": None,
        "description": "List refunds already issued against a charge, with their status.",
        "params": {"charge_id": {"type": "string"}},
    },
    "compute_amount": {
        "effect_class": "pure",
        "resource_id": None,
        "fn": lambda total, discount=0.0: {"amount": round(total - discount, 2)},
        "description": "Compute the refund amount for an order: total minus discount. Always use this rather than doing the arithmetic yourself.",
        "params": {"total": {"type": "number"}, "discount": {"type": "number"}},
        "required": ["total"],
    },
    "payments.refund": {
        "effect_class": "irreversible",
        "resource_id": lambda args: args["charge_id"],
        "description": "Refund an amount against a charge. Irreversible. Status is 'succeeded' or 'pending'.",
        "params": {"charge_id": {"type": "string"}, "amount": {"type": "number"}},
    },
    "tickets.update": {
        "effect_class": "idempotent",
        "resource_id": lambda args: args["ticket_id"],
        "description": "Set a support ticket's status: resolved, awaiting_refund, escalated, or closed.",
        "params": {"ticket_id": {"type": "string"},
                   "status": {"type": "string", "enum": ["resolved", "awaiting_refund", "escalated", "closed"]}},
    },
    "email.send": {
        "effect_class": "irreversible",
        "resource_id": lambda args: args["to"],
        "description": "Send an email to a customer. Irreversible.",
        "params": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
    },
}


def get_effect_class(tool: str) -> str:
    entry = TOOL_REGISTRY.get(tool)
    if entry is None:
        raise ValueError(f"Unregistered tool: {tool!r}")
    return entry["effect_class"]


def get_resource_id(tool: str, args: dict[str, Any]) -> Optional[str]:
    entry = TOOL_REGISTRY.get(tool)
    if entry is None:
        raise ValueError(f"Unregistered tool: {tool!r}")
    extractor: Optional[Callable] = entry["resource_id"]
    if extractor is None:
        return None
    return str(extractor(args))


def get_pure_fn(tool: str) -> Callable[..., dict[str, Any]]:
    entry = TOOL_REGISTRY.get(tool)
    if entry is None or entry["effect_class"] != "pure":
        raise ValueError(f"Not a registered pure op: {tool!r}")
    return entry["fn"]


def is_registered(tool: str) -> bool:
    return tool in TOOL_REGISTRY


def api_name(tool: str) -> str:
    """Claude tool names allow [a-zA-Z0-9_-] only, so dots become double underscores."""
    return tool.replace(".", "__")


def from_api_name(name: str) -> str:
    return name.replace("__", ".")


def api_tool_defs() -> list[dict[str, Any]]:
    defs = []
    for name, entry in TOOL_REGISTRY.items():
        params = dict(entry["params"])
        required = entry.get("required", list(params))
        if entry["effect_class"] in ("irreversible", "compensable"):
            params["declare_distinct"] = {
                "type": "boolean",
                "description": "Set true only if this is intentionally a second, distinct action on the same target."}
        defs.append({
            "name": api_name(name),
            "description": f"[{entry['effect_class']}] {entry['description']}",
            "input_schema": {
                "type": "object",
                "properties": params,
                "required": required,
                "additionalProperties": False,
            },
        })
    return defs
