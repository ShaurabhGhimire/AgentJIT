import os

from dotenv import load_dotenv

load_dotenv()

MONGODB_URI: str = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
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

# Dispatch
EXPLORATION_RATE: float = float(os.getenv("EXPLORATION_RATE", "0.05"))

# Models
INTERPRETER_MODEL: str = os.getenv("INTERPRETER_MODEL", "claude-opus-4-7")
CODEGEN_MODEL: str = os.getenv("CODEGEN_MODEL", "claude-opus-4-7")
EXTRACTION_MODEL: str = os.getenv("EXTRACTION_MODEL", "claude-haiku-4-5-20251001")
HOLE_MODEL: str = os.getenv("HOLE_MODEL", "claude-haiku-4-5-20251001")
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMENSIONS: int = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
