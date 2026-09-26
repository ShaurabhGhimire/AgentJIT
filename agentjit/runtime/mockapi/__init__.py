"""FastAPI surface over the mock world, for poking at it and flipping drift knobs.

The runtime's gateway calls the world in-process (agentjit.runtime.mockapi.world);
this HTTP app exposes the same tools plus the knobs for the demo.
"""
from typing import Any

from fastapi import FastAPI, HTTPException

from . import world

app = FastAPI(title="AgentJIT mock world")


@app.post("/tools/{name}")
def call_tool(name: str, args: dict[str, Any]) -> Any:
    fn = world.WORLD_TOOLS.get(name)
    if fn is None:
        raise HTTPException(404, f"unknown tool {name}")
    return fn(**args)


@app.get("/knobs")
def get_knobs() -> dict:
    return world.get_knobs()


@app.post("/knobs")
def set_knobs(changes: dict[str, Any]) -> dict:
    return world.set_knobs(**changes)


@app.get("/policy")
def get_policy() -> dict:
    return world.get_policy()


@app.post("/policy")
def set_policy(changes: dict[str, Any]) -> dict:
    return world.set_policy(**changes)
