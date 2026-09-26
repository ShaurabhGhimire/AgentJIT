from typing import Any, Callable, Optional

# Tool registry: name -> effect_class + resource_id extractor
# effect_class values: read, pure, idempotent, compensable, irreversible
# resource_id: callable(args) -> str, or None for reads/pure

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "get_order": {
        "effect_class": "read",
        "resource_id": None,
    },
    "get_shipments": {
        "effect_class": "read",
        "resource_id": None,
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


def is_registered(tool: str) -> bool:
    return tool in TOOL_REGISTRY
