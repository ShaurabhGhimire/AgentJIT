"""Thin wrapper over the Anthropic SDK: availability, cost accounting, calls.

Everything that calls Claude goes through here so costs are counted in one
place and the offline stand-ins can take over when no credentials resolve.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from typing import Any

from . import config

_client = None


def available() -> bool:
    if config.LLM_MODE == "offline":
        return False
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return True
    # `ant auth login` stores a profile the SDK picks up without env vars
    return (pathlib.Path.home() / ".config" / "anthropic").exists()


def client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()
    return _client


@dataclass
class Usage:
    cost_usd: float = 0.0
    latency_ms: int = 0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.cost_usd += other.cost_usd
        self.latency_ms += other.latency_ms
        self.calls += other.calls


def cost_of(model: str, usage: Any) -> float:
    price_in, price_out = config.MODEL_PRICES.get(model, (5.0, 25.0))
    tokens_in = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * 1.25
    tokens_in += (getattr(usage, "cache_read_input_tokens", 0) or 0) * 0.1
    tokens_out = getattr(usage, "output_tokens", 0) or 0
    return (tokens_in * price_in + tokens_out * price_out) / 1e6


class Refused(RuntimeError):
    pass


def create(model: str, **kwargs):
    """messages.create with server-side refusal fallbacks on Opus 5.

    Returns (response, Usage). Raises Refused if the whole fallback chain declined.
    """
    import time

    t0 = time.monotonic()
    if model.startswith("claude-opus-5") or model.startswith("claude-fable"):
        resp = client().beta.messages.create(
            model=model,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **kwargs,
        )
    else:
        resp = client().messages.create(model=model, **kwargs)
    usage = Usage(cost_of(model, resp.usage), int((time.monotonic() - t0) * 1000), 1)
    if resp.stop_reason == "refusal":
        raise Refused(str(getattr(resp, "stop_details", None)))
    return resp, usage


def text_of(resp) -> str:
    return "".join(b.text for b in resp.content if b.type == "text")
