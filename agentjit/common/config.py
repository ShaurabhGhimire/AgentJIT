import os

from dotenv import load_dotenv

load_dotenv()

MONGODB_URI: str = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017/?directConnection=true")
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
DB_NAME: str = os.getenv("DB_NAME", "agentjit")

# dev_saurav or dev_susan - keeps each person's test data in separate collections
DB_PREFIX: str = os.getenv("DB_PREFIX", "dev_susan")

# Profiler thresholds
HOTNESS_THRESHOLD: int = int(os.getenv("HOTNESS_THRESHOLD", "20"))
STABILITY_THRESHOLD: float = float(os.getenv("STABILITY_THRESHOLD", "0.80"))
PROFILER_WINDOW_DAYS: int = int(os.getenv("PROFILER_WINDOW_DAYS", "7"))

# Shadow testing
SHADOW_PROMOTION_THRESHOLD: float = float(os.getenv("SHADOW_PROMOTION_THRESHOLD", "0.03"))
SHADOW_ACTIVE_THRESHOLD: float = float(os.getenv("SHADOW_ACTIVE_THRESHOLD", "0.05"))
# Activation: cumulative 95% upper bound < SHADOW_PROMOTION_THRESHOLD.
# Demotion: any verifier-confirmed divergence demotes at once; otherwise demote
# when the 95% *lower* bound over the last SHADOW_WINDOW_RUNS shadow runs exceeds
# SHADOW_ACTIVE_THRESHOLD. (A cumulative bound over hundreds of clean runs
# barely moves after a drift; an upper bound over a small window always breaches.)
SHADOW_WINDOW_RUNS: int = int(os.getenv("SHADOW_WINDOW_RUNS", "30"))
# Active skills decay toward this shadow rate and never below it.
SHADOW_ACTIVE_MIN_RATE: float = float(os.getenv("SHADOW_ACTIVE_MIN_RATE", "0.10"))
# Active shadow rate = max(MIN_RATE, 1 / (1 + active_runs * SHADOW_DECAY))
SHADOW_DECAY: float = float(os.getenv("SHADOW_DECAY", "0.05"))
# Probation gives up after this many shadow runs without reaching the threshold.
PROBATION_BUDGET_RUNS: int = int(os.getenv("PROBATION_BUDGET_RUNS", "400"))

# Dispatch
EXPLORATION_RATE: float = float(os.getenv("EXPLORATION_RATE", "0.05"))
FAMILY_MIN_SIMILARITY: float = float(os.getenv("FAMILY_MIN_SIMILARITY", "0.45"))
FAMILY_MIN_MARGIN: float = float(os.getenv("FAMILY_MIN_MARGIN", "0.05"))
# Share of traffic kept on the parent version while a recompile is in probation.
PINNED_PARENT_SHARE: float = float(os.getenv("PINNED_PARENT_SHARE", "0.5"))

# Guard inference
GUARD_MIN_SUPPORT: int = int(os.getenv("GUARD_MIN_SUPPORT", "10"))
# Set-membership guards need distinct values <= this fraction of samples.
GUARD_MAX_DISTINCT_FRACTION: float = float(os.getenv("GUARD_MAX_DISTINCT_FRACTION", "0.2"))
# Held-out share of source traces for promotion-gate replay (section 8.7 step 2).
HELD_OUT_FRACTION: float = float(os.getenv("HELD_OUT_FRACTION", "0.2"))
CODEGEN_MAX_ATTEMPTS: int = int(os.getenv("CODEGEN_MAX_ATTEMPTS", "3"))

# Lifecycle
POLYMORPHIC_K: int = int(os.getenv("POLYMORPHIC_K", "3"))
MEGAMORPHIC_COOLDOWN_S: int = int(os.getenv("MEGAMORPHIC_COOLDOWN_S", "3600"))
RECOMPILE_MIN_CONTINUATIONS: int = int(os.getenv("RECOMPILE_MIN_CONTINUATIONS", "5"))
RECOMPILE_DEOPT_RATE: float = float(os.getenv("RECOMPILE_DEOPT_RATE", "0.3"))
RECOMPILE_WINDOW_RUNS: int = int(os.getenv("RECOMPILE_WINDOW_RUNS", "20"))

# Models. "auto" uses Claude when credentials resolve, else the offline stand-ins
# (scripted interpreter, template codegen/holes). "offline" forces the stand-ins.
LLM_MODE: str = os.getenv("LLM_MODE", "auto")
INTERPRETER_MODEL: str = os.getenv("INTERPRETER_MODEL", "claude-opus-5")
CODEGEN_MODEL: str = os.getenv("CODEGEN_MODEL", "claude-opus-5")
EXTRACTION_MODEL: str = os.getenv("EXTRACTION_MODEL", "claude-haiku-4-5")
HOLE_MODEL: str = os.getenv("HOLE_MODEL", "claude-haiku-4-5")
INTERPRETER_MAX_TURNS: int = int(os.getenv("INTERPRETER_MAX_TURNS", "16"))

# Embeddings: "voyage" (MongoDB's Voyage AI) when VOYAGE_API_KEY is set, else a
# deterministic local hashing embedding so everything runs without keys.
VOYAGE_API_KEY: str = os.getenv("VOYAGE_API_KEY", "")
EMBEDDING_PROVIDER: str = os.getenv("EMBEDDING_PROVIDER", "voyage" if VOYAGE_API_KEY else "local")
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "voyage-3.5")
EMBEDDING_DIMENSIONS: int = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))

# USD per million tokens (input, output)
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
}
VOYAGE_PRICE_PER_MTOK: float = 0.06

# Offline stand-ins have no token bill. These nominal costs keep cost curves
# meaningful offline; every metric row they produce is marked simulated=True.
SIM_COST_INTERPRETER_TURN: float = float(os.getenv("SIM_COST_INTERPRETER_TURN", "0.008"))
SIM_COST_HOLE: float = float(os.getenv("SIM_COST_HOLE", "0.0015"))
SIM_COST_EXTRACTION: float = float(os.getenv("SIM_COST_EXTRACTION", "0.0005"))
SIM_COST_EMBEDDING: float = float(os.getenv("SIM_COST_EMBEDDING", "0.00002"))
SIM_LATENCY_INTERPRETER_TURN_MS: int = int(os.getenv("SIM_LATENCY_INTERPRETER_TURN_MS", "900"))
SIM_LATENCY_HOLE_MS: int = int(os.getenv("SIM_LATENCY_HOLE_MS", "350"))

SKILLS_DIR: str = os.getenv("SKILLS_DIR", "skills")
