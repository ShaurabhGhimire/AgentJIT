# AgentJIT

**A tracing JIT compiler for LLM agents, with safe deoptimization across real-world side effects.**

Built for the MongoDB *Harness Engineering & Model Wrangling* hackathon (problem statement: Recursive Harnessing).

Production agents pay full LLM cost and latency for the thousandth identical task. AgentJIT watches an agent work, finds the task paths that are frequent and stable, and compiles them into ordinary Python guarded by invariants inferred from the agent's own traces. When the world changes and a guard fails, control returns to the LLM mid-task with a record of what already happened, so nothing is refunded, emailed or booked twice.

Agents get cheaper and faster the longer they run, and every compiled behavior is readable, versioned, tested before promotion and reversible.

The full design is in [`AgentJIT_Project_Architecture.md`](AgentJIT_Project_Architecture.md).

---

## How it works

A JavaScript engine interprets code, profiles which paths run hot, compiles them with guards, and deoptimizes back to the interpreter when a guard fails. AgentJIT applies the same discipline to agents.

1. **Interpret.** Every new task runs through the LLM agent loop. Each run is recorded as a trace with provenance for every value.
2. **Profile.** An aggregation pipeline groups verified traces by signature and finds paths that are hot and stable.
3. **Compile.** The hot path is generalized, turned into a Python skill (free-text parts stay as small typed LLM "holes"), replay-checked against its source traces, and given guards inferred from those traces.
4. **Probation and shadow testing.** A new skill serves traffic while an LLM shadow re-runs its tasks and compares. It is promoted only when a one-sided 95% bound on the divergence rate is below the threshold.
5. **Deoptimize.** When a guard fails:
   - *before any side effect*: the task is handed to the LLM cheaply (safe-zone deopt);
   - *after a side effect*: the task resumes in the LLM from a frame holding the committed effects (on-stack deopt), and the effect journal fences any repeat.
6. **Demote and recompile.** Shadow divergences or a rising deopt rate demote the skill atomically, and fresh traces feed a new version, possibly polymorphic with one guarded branch per path.

---

## MongoDB Atlas in the system

Each Atlas feature does a job in the design:

| Atlas feature | Job in AgentJIT | Where |
|---|---|---|
| Document model | Whole traces, skills, deopt frames and dispatch records stored as single nested documents | throughout |
| `$vectorSearch` with filters | Routes each incoming task to its task family, pre-filtered by tenant | `compileplane/families.py` |
| Aggregation with `$group` and `$merge` | Profiler: signature counts and stability, materialized into `hot_segments` | `compileplane/profiler.py` |
| Unique index on `effect_key` | Database-enforced at-most-once side effects; blocks duplicate refunds after a deopt | `runtime/journal.py` |
| Multi-document transactions | Atomic promotion and rollback: skill status, family pointer and audit record together | `compileplane/gate.py` |
| Change streams | Hot-swap skill versions, trigger recompiles from deopt events, log shadow divergences | `compileplane/watchers.py`, dashboard feed |
| Time-series collection | Per-task cost, latency, deopt and divergence metrics | `engine.py`, `metrics` collection |
| TTL index | Cooldown for megamorphic task families that are too unstable to compile | `megamorphic_blocklist` |

`scripts/seed_atlas.py` creates every collection, index and vector search index. Against a plain `mongod` without Atlas Search, vector indexes are skipped and family matching falls back to cosine similarity in Python.

---

## Quick start

Requires Python 3.11+ and a MongoDB Atlas cluster (or the `mongodb-atlas-local` Docker image).

```bash
python -m venv .venv && source .venv/bin/activate
make install
```

Create a `.env` in the repo root. It is gitignored; never commit real credentials.

```bash
MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/
DB_NAME=agentjit
DB_PREFIX=dev_yourname          # keeps each person's collections separate

# LLM: either the Anthropic API directly...
ANTHROPIC_API_KEY=sk-ant-...
# ...or OpenRouter through its Anthropic-compatible endpoint
# ANTHROPIC_BASE_URL=https://openrouter.ai/api
# ANTHROPIC_AUTH_TOKEN=sk-or-...

# Optional: Voyage AI embeddings (otherwise a local hashing embedding is used)
# VOYAGE_API_KEY=...

# Optional: run without any LLM calls, with simulated costs
# LLM_MODE=offline
```

No local MongoDB? `make mongo` starts a single-node Atlas Local container on port 27017 with transactions, change streams and vector search.

Then create the collections and indexes:

```bash
make seed
```

---

## Running the demo

The demo dashboard runs the same task stream through two lanes side by side: a plain LLM agent (arm A) and full AgentJIT (arm E).

**1. Pre-warm both lanes.** Run these at the same time in two terminals. Each runs 125 warm-up tasks; on Atlas this takes about 10 minutes, so do it well before presenting.

```bash
DB_PREFIX=demo      python scripts/seed_demo.py demo
DB_PREFIX=demo_base python scripts/seed_baseline.py demo_base 125
```

The first ends with `"active_skill": "refund_standard@v1"`.

**2. Start the dashboard.**

```bash
DB_PREFIX=demo LLM_MODE=offline uvicorn agentjit.dashboard.app:app --host 127.0.0.1 --port 8010
```

Leave out `LLM_MODE=offline` to use real Claude calls. That is slower (several seconds per LLM task) and costs roughly $0.06 per interpreted task.

**3. Open** http://127.0.0.1:8010/compare

| Control | What it does |
|---|---|
| Stream 10 / 30 | Sends the same new refund requests into both lanes |
| Drift 1: EUR orders | 30% of orders arrive in euros. The compiled skill's currency guard fails before any action, so it deopts cheaply to the LLM. |
| Drift 2: provider returns "pending" | The payment API changes behavior after the refund is issued. On-stack deopt resumes in the LLM, and the unique index fences the repeat refund. |
| Drift 3: refund window 30→14 days | A policy change no guard can see. Shadow testing catches the divergence and the skill is demoted. |
| Reset drift | Turns all drift off |

Task squares are colored by outcome: red for LLM, green for compiled, yellow for safe deopt, blue for on-stack deopt with fenced repeats, white for a caught shadow divergence. The Atlas panel shows each feature's live counters and a live change-stream feed.

Each run adds tasks to both lanes. Re-run step 1 for a clean slate.

The original single-lane dashboard is still available at `/`, served by `make demo`.

---

## Evaluation

The ablation harness runs arms A–E over one seeded stream and drift schedule, each on its own collection prefix, and reports every metric with a 95% interval.

| Arm | Configuration |
|---|---|
| A | Interpreted only (plain LLM agent) |
| B | Plan cache, no guards |
| C | AgentJIT, restart on deopt instead of resuming |
| D | AgentJIT, no shadow testing |
| E | Full AgentJIT |

```bash
make eval                                   # python -m agentjit.eval.run --n 600 --out eval_results
python scripts/run_stream.py --n 330 --arm E --seed 42 --fresh
make test
```

---

## Repository layout

```
agentjit/
  engine.py            per-task pipeline: dispatch, execute or interpret, deopt, shadow, verify, record
  stream.py            seeded task stream and drift schedule
  bootstrap.py         one call to make a prefix ready (collections, indexes, world, centroids)
  common/              config, Atlas access, LLM client, embeddings, models, stats
  runtime/             gateway, effect journal, interpreter, compiled executor, deopt, verifier, mock world
  compileplane/        profiler, generalizer, codegen, guards, hoisting, shadow gate, scheduler, watchers
  dashboard/           FastAPI app; static/index.html (single lane) and static/compare.html (side by side)
  eval/                ablation harness
scripts/               seed_atlas, seed_demo, seed_baseline, run_stream, load_fixtures
skills/                generated skill code, versioned (refund_standard/)
fixtures/              fixture envelopes, traces and skills
docs/skill_abi.md      contract between codegen and the compiled executor
tests/                 ABI, backbone and end-to-end tests
```
