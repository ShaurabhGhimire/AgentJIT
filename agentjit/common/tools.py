from typing import Any, Callable, Optional

# Tool registry: name -> effect_class + resource_id extractor
# effect_class values: read, pure, idempotent, compensable, irreversible
# resource_id: callable(args) -> str, or None for reads/pure
# fn: pure ops only; the gateway runs it in-process with no journal entry.
#     Pure ops are exposed to the interpreter as tools so computed values
#     (e.g. the refund amount) get a trace step and a provenance edge.

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "get_order": {
        "effect_class": "read",
        "resource_id": None,
    },
    "get_shipments": {
        "effect_class": "read",
        "resource_id": None,
    },
    "payments.list_refunds": {
        # reconciliation read for uncertain effects, and the pending-refund path
        "effect_class": "read",
        "resource_id": None,
    },
    "compute_amount": {
        "effect_class": "pure",
        "resource_id": None,
        "fn": lambda total, discount=0.0: {"amount": round(total - discount, 2)},
    },
    "payments.refund": {
        "effect_class": "irreversible",
        "resource_id": lambda args: args["charge_id"],
    },
    "tickets.update": {
        "effect_class": "idempotent",
        "resource_id": lambda args: args["ticket_id"],
    },
    "email.send": {
        "effect_class": "irreversible",
        "resource_id": lambda args: args["to"],
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
