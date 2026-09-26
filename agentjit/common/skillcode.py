"""Loading generated skill code: a restricted namespace, no imports.

Shared by the compile plane (replay) and the runtime (executor), so both run
exactly the same loading rules.
"""
from __future__ import annotations

import builtins
from typing import Callable

_ALLOWED = {"len", "round", "min", "max", "abs", "str", "int", "float", "dict", "list", "sum",
            "sorted", "any", "all", "enumerate", "range", "isinstance", "True", "False", "None"}


class SkillLoadError(Exception):
    pass


def load_run(source: str, name: str = "<skill>") -> Callable:
    if "import " in source or "__" in source.replace('"""', "").replace("'''", ""):
        raise SkillLoadError("generated code may not import or touch dunder names")
    safe_builtins = {k: getattr(builtins, k) for k in _ALLOWED if hasattr(builtins, k)}
    ns: dict = {"__builtins__": safe_builtins}
    try:
        exec(compile(source, name, "exec"), ns)
    except SyntaxError as e:
        raise SkillLoadError(f"syntax error: {e}") from e
    run = ns.get("run")
    if not callable(run):
        raise SkillLoadError("skill has no run(ctx, args)")
    return run


GUARD_GLOBALS = {"__builtins__": {}, "len": len}


def eval_guard(expr: str, bindings: dict):
    """The ABI sandbox: subscript access only, len() the only builtin."""
    return eval(expr, GUARD_GLOBALS, dict(bindings))
