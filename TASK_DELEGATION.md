# AgentJIT — Task Delegation

**Team:** Susan and Saurav
**Source of truth:** [`AgentJIT_Project_Architecture.md`](./AgentJIT_Project_Architecture.md) — section numbers below (§7.4, §8.5, …) point into it.
**Scope:** the MVP in §16.1, built in the order of §16.3, demoed as §16.5.

### Assumptions (change these if wrong)

- Two people, one repo, working in parallel on separate branches.
- Phases are sized for a **~36-hour hackathon**. Hour markers are indicative — the *phase order and the sync gates* are what matter, not the clock.
- Python + FastAPI + MongoDB Atlas, per §16.2.
- One family only: `refund_request` (§11). A second family is stretch.

### The one rule that makes parallel work possible

> **Phase 0 is done together, before either of us writes any feature code.** It produces the shared contracts and two fixture artifacts. After Phase 0, neither of us is blocked on the other for anything until Gate A.

---

## 1. Ownership map

The architecture's four roles (§16.4) collapse into two, along the cleanest seam in the system:

| | **Saurav — Runtime & World** | **Susan — Compiler & Evidence** |
|---|---|---|
| Owns | The world, and everything on the hot path through it | Everything that learns from traces and judges what it learned |
| Planes | Runtime plane (§7) + mock environment | Compile plane (§8) + data/eval (§10, §14) |
| Mental model | "What happens when a task arrives" | "What the system learns overnight, and whether to trust it" |

### Directory ownership

Touch files outside your column only via a heads-up in chat.

```
agentjit/
  common/            SHARED — frozen after Phase 0 (see §5 change rule)
    models.py          pydantic schemas: Envelope, Trace, Step, Skill, Guard,
                       Hole, DeoptFrame, EffectRecord, DispatchDecision
    tools.py           tool registry: name -> effect_class -> resource_id extractor
    config.py          thresholds, sampling rates, model names, env
    db.py              Mongo client + typed collection accessors
  runtime/           SAURAV
    mockapi/           orders, shipments, payments, tickets, email outbox (+ drift knobs)
    verifier.py        hard pass/fail on end state
    gateway.py         effect classes, write-ahead, fencing, reconciliation
    journal.py         effect_journal reads/writes
    interpreter.py     LLM agent loop; also the deopt resume target
    tracer.py          trace recording with provenance capture
    executor.py        compiled skill executor + guard evaluation + holes
    deopt.py           frame materialization and handoff
    parser.py          §7.1 stages 1-4 (envelope, family match, extraction, grounding)
    dispatcher.py      §7.1 stage 5 routing policy
  compileplane/      SUSAN   (not "compile/" — shadows a builtin)
    profiler.py        signature counts, hotness, stability, hot_segments view
    generalizer.py     anti-unification -> template
    codegen.py         LLM drafts skill code
    replay.py          replay-equivalence harness
    guards.py          Daikon-lite invariant inference
    hoist.py           point-of-no-return analysis + guard hoisting
    gate.py            promotion gate, probation, Clopper-Pearson, rollback
    shadow.py          shadow sampler + effect-diff + adjudication
    watchers.py        change-stream consumers
  skills/            GENERATED — Susan's codegen writes, Saurav's executor reads.
                     Committed output. Never hand-edit after Phase 0.
  dashboard/         SAURAV (moved here to balance the columns)
  eval/              SUSAN — ablation arms A-E, drift schedule, metrics rollup
  fixtures/          SHARED, written in Phase 0
  scripts/
    seed_atlas.py      collections + indexes          SUSAN
    seed_demo.py       pre-warmed demo state          SAURAV, P4
    run_stream.py      task stream generator          SAURAV
  docs/
    skill_abi.md       THE hard contract (see 2.3)  SHARED, Phase 0
```

### Git workflow

- Branches: `saurav/<topic>`, `susan/<topic>`. Never commit to `main` directly.
- Merge to `main` at least twice a day, and always before a sync gate.
- `common/`, `docs/skill_abi.md`, `fixtures/` → **PR + the other person's ack**, always.
- Everything else in your own column → merge freely.
- Commit generated skills in `skills/` so the other person can run them without rerunning codegen.

---

## 2. Phase 0 — Shared foundation (hours 0–2, do it in the same room)

Nothing else starts until these five items exist. They are what un-block both directions.

### 2.1 Repo skeleton — 20 min, pair

- [ ] Create the tree above, with `__init__.py` files and empty modules
- [ ] `pyproject.toml` / `requirements.txt`: `pymongo`, `fastapi`, `uvicorn`, `pydantic`, `anthropic` (or chosen SDK), `scipy` (Clopper–Pearson), `pytest`
- [ ] `.env.example` with `MONGODB_URI`, `ANTHROPIC_API_KEY`, `DB_NAME`
- [ ] `make dev` / `make run` targets so both of us run the same commands
- [ ] One Atlas cluster, one DB, **two collection prefixes**: `dev_saurav_*` and `dev_susan_*`, selected by `config.DB_PREFIX`, so our test data never collide

### 2.2 `common/models.py` — 40 min, pair

Types both halves pass across the seam. Write them as pydantic models with the exact field names from §8.1, §10.3, and §7.5.

- [ ] `TaskEnvelope` (§7.1 stage 1)
- [ ] `TraceStep` — `i`, `tool`, `args`, `result_digest`, `provenance`, `effect_class`, `llm_span`, `cost`, `latency`
- [ ] `Trace` — `trace_id`, `family`, `mode`, `signature`, `steps[]`, `verified_success`, `cost_usd`
- [ ] `Guard` — `expr`, `support`, `kind` ∈ {task, state, residual}
- [ ] `Hole` — `name`, `schema`, `checks[]`
- [ ] `SkillStep` — `pc`, `tool` | `op`, `effect`, `post[]`, `hole`
- [ ] `Skill` — exactly the §10.3 document shape
- [ ] `EffectRecord` — `effect_key`, `exec_id`, `tool`, `effect_class`, `resource_id`, `state` ∈ {intent, completed, uncertain}, `result`
- [ ] `DeoptFrame` — exactly the §7.5 schema
- [ ] `DispatchDecision` — exactly the §7.1 schema
- [ ] `ExecResult` — `exec_id`, `ok`, `mode`, `cost_usd`, `latency_ms`, `deopted`, `effects[]`

### 2.3 `docs/skill_abi.md` + `common/tools.py` — 40 min, pair

**This, not `models.py`, is the contract that breaks integrations.** Susan's codegen emits skill code; Saurav's executor runs it. Write down and agree:

- [ ] **Entry point.** How does the executor invoke a generated skill? Agreed: `def run(ctx: SkillContext, args: dict) -> dict`
- [ ] **`pc` numbering.** 1-indexed, one `pc` per `steps[]` entry, and the generated code must `ctx.step(pc)` before each call. The deopt frame names `pc` — codegen and executor must number identically or every frame is wrong.
- [ ] **Guard namespace.** A guard is a string expression (`order.currency in ['USD']`, `len(shipments) == 1`). Pin down: who binds `order`, `shipments`, `amount`? Agreed: the executor keeps a dict of named step results; codegen declares the binding name per step; guards evaluate against that dict in a restricted eval.
- [ ] **Hole invocation.** `ctx.hole("email_body", inputs={...}) -> validated value`; executor owns the model call, schema validation, and the `checks[]`.
- [ ] **Tool calls.** Generated code never imports the mock API. It calls `ctx.call(tool_name, **args)` and the gateway does the rest.
- [ ] **`common/tools.py`** — the registry table, needed by *both* (Saurav's gateway classifies with it; Susan's hoisting computes the point of no return from it):

  | tool | effect_class | resource_id extractor |
  |---|---|---|
  | `get_order` | read | — |
  | `get_shipments` | read | — |
  | `payments.refund` | irreversible | `args.charge_id` |
  | `tickets.update` | idempotent | `args.ticket_id` |
  | `email.send` | irreversible | `args.to` |

- [ ] **Verifier signature.** Saurav owns the implementation; Susan's shadow adjudication calls it. Agreed: `verify(envelope_id) -> VerifierResult(ok: bool, reason: str)`

### 2.4 `fixtures/traces/` — 30 min, **owner Susan**, Saurav reviews

Hand-written traces in the exact §8.1 schema. This is what lets Susan build the entire compile plane before the runtime produces a single real trace.

- [ ] ~20 traces of the modal refund signature `get_order>get_shipments>refund>ticket_update>email_send`, with varied order IDs, charge IDs, amounts, emails
- [ ] Every step carries real `provenance` (`step1.result.charge_id`, `input.order_id`) — the generalizer is worthless without it
- [ ] **3–4 deliberately divergent traces**, or guard inference has nothing to separate:
  - one EUR order → gives set-membership `currency in ['USD']` something to exclude
  - one two-shipment order that takes a different path → feeds the divergence stump (§8.5)
  - one where the refund returns `pending` → the residual-guard case
  - one 45-day-old order → gives the range invariant a boundary
- [ ] A `make fixtures` target that loads them into `traces`

### 2.5 `fixtures/skills/refund_standard@v0` — 30 min, **owner Saurav**, Susan reviews

The mirror artifact. Hand-written skill code + its skill JSON document, conforming to the ABI. Saurav's executor, guard evaluation, hoisting boundary, holes, and deopt frames are all untestable until a skill exists — and Susan's codegen won't emit one until Phase 2.

- [ ] `fixtures/skills/refund_standard_v0.py` — the §11 happy path, hand-written against the ABI
- [ ] `fixtures/skills/refund_standard_v0.json` — the §10.3 document, with the four entry guards and the `email_body` hole
- [ ] It runs under the executor and produces the right tool sequence. This doubles as the ABI's executable spec.

**Phase 0 exit check:** fixtures load, and `refund_standard@v0` executes against the mock API stub. Now split.

---

## 3. Saurav's track — Runtime & World

### P1 · Mock world and the correctness backbone (hours 2–10)

Build order §16.3 items 1–3. Everything downstream depends on hard pass/fail, so the verifier comes early.

**T1.1 Mock business API** (`runtime/mockapi/`)
- [ ] Mongo-backed collections: `orders`, `shipments`, `charges`, `tickets`, `outbox`
- [ ] FastAPI endpoints: `get_order`, `get_shipments`, `payments.refund`, `tickets.update`, `email.send`
- [ ] Seed generator: N orders with realistic spread (currency, shipment count, age, total)
- [ ] **Drift knobs** as config documents, flippable at runtime — these *are* the demo (§11 phase 4):
  - [ ] `currency_mix` → inject EUR orders
  - [ ] `split_shipment_rate` → orders with 2 parcels
  - [ ] `refund_returns_pending` → provider returns `pending` not `succeeded`
  - [ ] `refund_window_days` → 30 → 14, the invisible drift
- [ ] Every write endpoint accepts an `idempotency_key`

**T1.2 Hard verifier** (`runtime/verifier.py`) — signature agreed in 2.3
- [ ] Given an envelope, compute expected end state: refund exists with right amount, ticket resolved, exactly one outbox message
- [ ] Return `VerifierResult(ok, reason)` — the reason string feeds shadow adjudication and the eval table
- [ ] **Counts duplicate effects** — this is success criterion §14.5.3 and the arm C money shot

**T1.3 Tool gateway + effect journal** (`runtime/gateway.py`, `journal.py`) — §7.4, the correctness backbone
- [ ] Effect-class lookup from `common/tools.py`; refuse to call an unregistered tool
- [ ] `effect_key = hash(exec_id, pc, tool, normalized_args)`
- [ ] Write-ahead protocol: intent record → external call (passing `effect_key` as idempotency key) → completion record
- [ ] Unique index on `effect_key`, majority write concern
- [ ] **Argument normalization** before hashing (`49` ≡ `49.00`, email casing, whitespace)
- [ ] **Fencing:** a call matching a completed journal entry returns the journaled result, does not execute
- [ ] **Resource fences:** a second irreversible call to the same tool + same `resource_id` within one exec is blocked unless explicitly declared distinct — and the declaration is logged
- [ ] **Uncertain state:** intent with no completion → mark uncertain, force a reconciliation read before anything else proceeds
- [ ] `mode="shadow"` — stub all writes, record intended calls, return plausible results. Susan's shadow tester calls this.

**T1.4 LLM interpreter + trace recorder** (`runtime/interpreter.py`, `tracer.py`) — §7.3, §8.1
- [ ] Minimal agent loop: tools described from the registry, all calls through the gateway
- [ ] Emits a §8.1-shaped `Trace` per run: signature, steps, cost, latency
- [ ] **Provenance capture** — match each argument value against values seen earlier in the same trace; tag `input.x` or `stepN.result.y`. Susan's generalizer is dead without this; get it right, not approximately right.
- [ ] `llm_span` summaries per reasoning step
- [ ] Generate 50–100 real traces and hand them to Susan — she replaces her fixtures with these

### P2 · Compiled execution and deopt (hours 10–18)

Build order §16.3 items 4, 7. This is the contribution (§5), so it gets the most care.

**T2.1 Task parser** (`runtime/parser.py`) — §7.1 stages 1–4
- [ ] Stage 1 envelope: persist to `task_envelopes` before any decision
- [ ] Stage 2 family match: embed the text, `$vectorSearch` against `task_families.centroid` filtered by tenant + `has_active_skill` (Susan owns the index and the centroids)
  - [ ] below-threshold similarity → interpreter
  - [ ] top-two margin too small → interpreter
- [ ] Stage 3 extraction ladder, stop at first success: structured field → learned pattern → schema-constrained small model
- [ ] **Grounding check:** every extracted value must literally appear in the envelope or derive from something that does
- [ ] **Agreement check:** for any skill with an irreversible step, two independent extraction methods must agree; disagreement → interpreter
- [ ] Stage 4: evaluate task-level guards
- [ ] Track parsing cost as its own line item — §15 lists "parsing cost eats savings" as a named risk and §14.3 requires it separately

**T2.2 Dispatcher** (`runtime/dispatcher.py`) — §7.1 stage 5
- [ ] Route compiled vs interpreted
- [ ] Exploration: fixed small fraction of eligible tasks go interpreted anyway (without fresh traces the profiler can never see drift)
- [ ] Shadow flag from `gate.should_shadow(skill)` — Susan's function
- [ ] Version pinning while a recompile is in probation
- [ ] Write one `DispatchDecision` per task, §7.1 schema, every time
- [ ] **Any uncertainty routes to the interpreter. A task is never rejected.** (§4.2)

**T2.3 Compiled executor** (`runtime/executor.py`) — §7.2, implements the ABI
- [ ] Load skill code from `skills/`, hot-reload on change stream
- [ ] `SkillContext`: `ctx.call`, `ctx.hole`, `ctx.step(pc)`, named-result bindings
- [ ] Guard evaluator: restricted eval over the binding dict; log which guard failed and the observed value
- [ ] Run read prefix → evaluate hoisted state guards → proceed only if all pass
- [ ] Typed holes: bounded model call, schema validation, `checks[]` (`mentions(order_id)`)
- [ ] Residual postconditions evaluated immediately after their step
- [ ] Every tool call through the gateway; the executor never touches the mock API directly
- [ ] Emits a `Trace` with `mode: "compiled"` just like the interpreter

**T2.4 On-stack deoptimization** (`runtime/deopt.py`) — §7.5, **the contribution**
- [ ] On guard failure: snapshot locals, read committed effects for this exec from the journal
- [ ] Materialize the §7.5 frame: `pc`, `failed_guard`, `observed`, `locals`, `committed_effects[]`, `remaining_goal`, `original_task`
- [ ] Persist to `deopt_events` (Susan's recompile scheduler watches this stream)
- [ ] Render the frame into interpreter context: what was done, what was assumed, what was observed instead, what remains
- [ ] Interpreter resumes **from pc**, not from scratch; all its calls go through the gateway and get fenced
- [ ] Link the continuation trace back to the deopt event — it is exactly the evidence Susan's recompile needs
- [ ] **The money shot — build for this:** with `refund_returns_pending` on, a compiled run deopts at pc=4, the resuming LLM re-issues the refund, the gateway fences it, and the verifier reports **zero duplicate refunds**
- [ ] The cheap path: an EUR order deopts in the safe zone with no committed effects

### P3 · Stream, dashboard, drift (hours 18–28)

**T3.1 Task stream** (`scripts/run_stream.py`)
- [ ] Generate a reproducible task stream from a fixed seed
- [ ] Inject the four §11 drift events at fixed offsets, same order and seed for every arm (§14.4)
- [ ] Runs headless so Susan's eval harness can drive it

**T3.2 Dashboard** (`dashboard/`) — §16.1 item 10
- [ ] Cost per task over time — the curve that drops. This is the 0:50–1:15 demo beat.
- [ ] Compiled share rising
- [ ] Deopt count, split safe-zone vs on-stack
- [ ] **Duplicate side effects** — reads 0 for arm E, non-zero for arm C
- [ ] Current divergence bound per skill, with its probation/active state
- [ ] Live-updating during the demo (poll, or a change-stream socket)

### P4 · Demo seed and buffer (hours 28–32)

**T4.1 Pre-warmed demo state** (`scripts/seed_demo.py`) — §16.3 item 10. This is a build task, not rehearsal, and it seeds the same collections the task stream writes.
- [ ] Loads Atlas with an already-**active** `refund_standard@v3`, its guards and support counts
- [ ] Accumulated interpreted traces so the cost curve has history to show
- [ ] A filled shadow counter so the bound is already below threshold
- [ ] Drift knobs off, ready to flip live
- [ ] One command, idempotent, under 30 seconds. **We never warm up live on stage.**

**T4.2 Buffer**
- [ ] Slack for whatever ran over. Then join the joint end-to-end phase (§6).

---

## 4. Susan's track — Compiler & Evidence

### P1 · Atlas, profiler, generalizer (hours 2–10)

You can do all of P1 against `fixtures/traces/` without waiting for Saurav's runtime.

**T1.1 Atlas schema and indexes** (`scripts/seed_atlas.py`) — §10.2, §10.4
- [ ] Create all 12 collections from the §10.2 table
- [ ] `task_families` vector index on `centroid`, filters `tenant` + `has_active_skill`
- [ ] `skills` vector index `skill_embedding` on `embedding`, filters `status` + `family`
- [ ] Unique index `effect_journal.effect_key` — hand to Saurav in P1, he needs it immediately
- [ ] Unique index `hot_segments {family, signature}` (required by the `$merge` `on` clause)
- [ ] `{family, signature, ts}` on `traces`; `{skill, ts}` on `executions`, `deopt_events`, `shadow_runs`
- [ ] `{tool, resource_id, exec_id}` on `effect_journal` for resource fences
- [ ] `metrics` as a **time series** collection, `timeField: ts`, `metaField: meta`, minutes granularity
- [ ] TTL on `megamorphic_blocklist.expiresAt`, `expireAfterSeconds: 0`
- [ ] Idempotent — safe to re-run

**T1.2 Family centroids and embeddings**
- [ ] Embed the fixture envelopes, cluster, compute the `refund_request` centroid
- [ ] Populate `task_families`, maintain `has_active_skill`
- [ ] Hand Saurav a `match_family(text) -> (family_id, similarity, margin)` helper for his parser

**T1.3 Profiler** (`compileplane/profiler.py`) — §8.2
- [ ] Signature = tool sequence **+ the provenance pattern of each argument**, not concrete values
- [ ] The §10.4 aggregation with `$merge` into `hot_segments`
- [ ] Hotness (count over a sliding window) and stability (modal signature's share of recent family traces)
- [ ] Compile trigger: both above threshold, thresholds in `common/config.py`
- [ ] Only `mode: "interpreted"` + `verified_success: true` traces feed it
- [ ] **v1 compiles whole-task traces per family. Do not attempt sub-task segment mining** — §15 names it as the risk that eats the whole project.

**T1.4 Generalizer** (`compileplane/generalizer.py`) — §8.3
- [ ] Anti-unification (Plotkin LGG) across traces sharing a signature
- [ ] Values agreeing across all traces → constant
- [ ] Values differing **with traceable provenance** → parameter `$o`, `$c`, `$a`
- [ ] Values differing **with no traceable source** → typed hole `?body`
- [ ] Output a `Template` with parameters, holes, and the data-flow graph
- [ ] Reproduces the §8.3 worked example on the fixture traces — that table is the spec

### P2 · Codegen, replay, guards (hours 10–18)

**T2.1 Code generator** (`compileplane/codegen.py`) — §8.4
- [ ] Prompt an LLM with the template + the ABI from `docs/skill_abi.md` + the v0 fixture skill as a worked example
- [ ] Emit Python conforming to the ABI: `pc` numbering, `ctx.call`, `ctx.hole`, named bindings
- [ ] Write to `skills/<skill_id>/v<n>.py` and the skill doc to `skills`
- [ ] **The draft is never trusted.** It goes straight to replay.

**T2.2 Replay-equivalence harness** (`compileplane/replay.py`) — §8.4, the thing that makes codegen safe
- [ ] Run the generated program against every source trace with tools mocked to return the recorded responses
- [ ] Assert an **identical sequence of tool calls with identical arguments**, holes excluded
- [ ] Any mismatch rejects the draft; feed the diff back for up to N retries, then give up and record the reason
- [ ] **Held-out replay** (§8.7 step 2): same family and signature, traces it was *not* compiled from, all guards passing

**T2.3 Guard inference** (`compileplane/guards.py`) — §8.5. Guards are inferred statistically; the LLM never writes one.
- [ ] Instrument program points: skill entry, before each step, after each step
- [ ] Template library: equality/constant, set membership, numeric range, length, variable ordering, simple linear relation, non-null
- [ ] **Support thresholds** — `currency ∈ {USD}` from 200 traces is meaningful; from 3 it is noise. Distinct observed values must be small relative to sample count.
- [ ] Store `support` on every guard (the demo shows these counts on screen at 0:20–0:50)
- [ ] **Divergence stumps:** where traces share a prefix then branch, fit a decision stump over inferred features; the separating predicate becomes a branch guard
- [ ] **The asymmetry rule (§4.4):** when in doubt keep the *tighter* invariant. Over-tight = an unnecessary deopt, which costs money but is safe. Over-loose = unvalidated execution, which is silent and unsafe.
- [ ] Reproduces the four §10.3 entry guards from the fixture traces

**T2.4 Guard hoisting** (`compileplane/hoist.py`) — §8.6, "the compiler pass that most directly reduces risk"
- [ ] Compute the **point of no return**: first compensable or irreversible step, using `common/tools.py`
- [ ] Move every guard whose inputs are available before that point in front of it
- [ ] Classify the remainder as residual (depends on a side effect's result) — these are the only ones needing full OSR
- [ ] Emit the §8.6 diagram's split as an artifact the executor and the dashboard can both read
- [ ] Target output: all four refund entry guards land before the refund step; only `refund.status == 'succeeded'` stays behind it

### P3 · Shadow testing and the promotion gate (hours 18–28)

**T3.1 Shadow tester** (`compileplane/shadow.py`) — §7.6
- [ ] Sample compiled executions; re-run them through Saurav's interpreter with `gateway(mode="shadow")`
- [ ] Replay the compiled run's reads from a snapshot where possible — otherwise §15's "shadow read non-repeatability" produces false divergences
- [ ] Compare the two sets of *intended* effects after normalization; compare hole outputs loosely or exclude them
- [ ] **Adjudication:** where the hard verifier can decide, it decides. Otherwise count the divergence conservatively against the skill and flag it. The interpreter is not ground truth (§15).
- [ ] Write `shadow_runs` documents with the effect diff

**T3.2 Confidence bound and promotion gate** (`compileplane/gate.py`) — §7.6, §8.7
- [ ] One-sided 95% Clopper–Pearson upper bound on divergence rate per skill
- [ ] Calibrate against the §7.6 table: 100 runs / 0 divergences ≈ 3.0%; 300/0 ≈ 1.0%; 180/1 ≈ 2.6%
- [ ] Lifecycle (§9): Candidate → Probation → Active | Rejected; Active → Recompiling → Probation | Megamorphic
- [ ] **Probation:** shadow every run until the bound drops below threshold
- [ ] **Active:** sampling rate decays but **never reaches zero** — residual sampling is the only defense against invisible drift
- [ ] **Activation is a multi-document transaction:** skill status + family active pointer + audit record, atomically
- [ ] **Rollback:** every version keeps a `parent`; demotion flips the pointer back in one transaction
- [ ] Rejection stores the reason and keeps the version for audit
- [ ] `should_shadow(skill) -> bool` exported for Saurav's dispatcher

**T3.3 Change-stream watchers** (`compileplane/watchers.py`) — §10.4
- [ ] `skills` (status → active/demoted) → runtime workers hot-load or unload code
- [ ] `deopt_events` (inserts) → cluster continuations, trigger a recompile when evidence accumulates
- [ ] `shadow_runs` (inserts) → update the bound, demote on breach
- [ ] Resumable after restart (store resume tokens) — a dropped stream mid-demo is a silent failure

**T3.4 Recompilation** (§9)
- [ ] Triggers: deopt rate over a window, a continuation cluster large enough to support a branch, or the divergence bound rising above the active threshold
- [ ] Merge deopt continuations into a new version → polymorphic branch selected by a divergence-derived guard
- [ ] Branch count above k → mark family **megamorphic**, insert into the TTL blocklist, stay interpreted
- [ ] New version enters probation, does not go straight to active

### P4 · Evaluation (hours 28–32)

**T4.1 Ablation harness** (`eval/`) — §14.2
- [ ] Arm A: interpreted only
- [ ] Arm B: plan cache, no guards
- [ ] Arm C: AgentJIT with **restart**-on-deopt (this is the arm that produces duplicate refunds)
- [ ] Arm D: AgentJIT with no shadow testing (misses the policy drift)
- [ ] Arm E: full AgentJIT
- [ ] Same seed, same drift schedule, same task order for every arm

**T4.2 Metrics** — §14.3
- [ ] Cost per task **including parsing and shadow cost** — excluding them is the easiest way to accidentally lie
- [ ] Latency p50 / p95
- [ ] Verified success rate
- [ ] **Silent error rate:** passes every guard, fails the verifier
- [ ] Duplicate side effects per task
- [ ] Deopt recovery cost vs clean compiled runs
- [ ] Compiled share; time-to-recompile after each drift event
- [ ] Write to the `metrics` time series collection for Saurav's dashboard
- [ ] **Report confidence intervals, not point estimates.** At hackathon scale only large effects are real (§14.5).

---

## 5. Handoff checkpoints

We are **not** testing as we go. Each of us builds our column straight through; verification happens once, jointly, in §6. What these checkpoints are for is **exchanging artifacts** and merging to `main` so neither of us drifts onto a stale contract.

| # | When | Handoff | Direction |
|---|---|---|---|
| **H0** | end of Phase 0 | `common/`, `docs/skill_abi.md`, fixture traces, fixture skill `@v0` | both ways |
| **H1** | ~hour 10 | Unique `effect_journal.effect_key` index + `match_family()` helper | Susan → Saurav |
| **H2** | ~hour 10 | 50–100 real interpreted traces with provenance | Saurav → Susan |
| **H3** | ~hour 18 | First generated skill in `skills/` (code + doc + hoisted guards) | Susan → Saurav |
| **H4** | ~hour 18 | `should_shadow(skill)` + `verify(envelope_id)` wired both ways | both ways |
| **H5** | ~hour 28 | Metrics written to the `metrics` time series collection | Susan → Saurav (dashboard) |
| **H6** | ~hour 32 | Both columns feature-complete → start §6 | both ways |

**At H2, glance at the trace shape.** Susan opens three of Saurav's real traces and confirms the `provenance` strings and `effect_class` values match the fixture shape *before* she deletes her fixtures. If his tracer emits `step_1.result.charge_id` where the fixtures say `step1.result.charge_id`, her generalizer keeps passing on fixtures and then quietly turns every parameter into a hole, and we don't find out until §6. Five-minute glance, not a test pass.

**The other thing worth eyeballing early, before H3.** The skill ABI is where the two columns physically meet: Susan's codegen emits the code, Saurav's executor runs it. If `pc` numbering or guard bindings disagree there, nothing in §6 will work and it will be hour 32 when we find out. So the moment codegen emits its first file, Saurav loads it into the executor once and we look at it together. That is a five-minute glance, not a test pass.

### Contract change rule

`common/models.py`, `common/tools.py`, and `docs/skill_abi.md` are frozen after Phase 0. To change one: say so in chat, open a PR, get an ack, merge. A silent change to the skill ABI at hour 20 costs an afternoon.

---

## 6. End-to-end testing — joint, after both tracks are done (hours 32–35)

Both columns complete first. Then we sit down together and run the whole system once, top to bottom. Work down this list in order; each item assumes the ones above it pass.

**Budget honestly.** Two hours is thin for a first full integration of a system neither half has exercised against the other. The compression is deliberate — we'd rather spend the time building — but if T6.3 or T6.5 goes red we are cutting from §8, not debugging into the demo slot. If either of us finishes a column early, pull this phase forward rather than starting something new.

**T6.1 Wire-up**
- [ ] Merge both branches to `main`; point both of us at one shared collection prefix
- [ ] `scripts/seed_atlas.py` against a clean DB; confirm every index and the time series collection exist
- [ ] Boot the mock API, the runtime, and Susan's watchers together

**T6.2 Cold path — the system behaves like a normal agent**
- [ ] Run the task stream with no skills present: everything routes interpreted, verifier passes, traces land with provenance
- [ ] Every failure path falls back to the interpreter rather than erroring — parse miss, ambiguous family, ungrounded argument, unknown tool

**T6.3 Warm path — the system compiles itself**
- [ ] Profiler marks the refund signature hot and stable
- [ ] Generalizer → codegen → replay passes on source traces and on held-out traces
- [ ] Guard inference yields the four §10.3 entry guards with support counts; hoisting puts all four before the refund
- [ ] Skill enters probation, serves live traffic, and the executor runs it end-to-end with the verifier passing

**T6.4 Correctness backbone**
- [ ] Same call issued twice → one external call, two identical results (fencing)
- [ ] Kill the process between intent and completion → effect marked uncertain, reconciliation read forced before anything else proceeds
- [ ] Resource fence blocks a second irreversible call to the same target within one execution

**T6.5 The four drift events (§11 phase 4)** — these double as the demo dry-run
- [ ] `currency_mix` → cheap safe-zone deopt, zero committed effects
- [ ] `split_shipment_rate` → cheap deopt; enough continuations accumulate to trigger a polymorphic recompile
- [ ] `refund_returns_pending` → on-stack deopt at pc=4; frame written; resuming LLM's repeat refund is fenced; **verifier reports zero duplicate refunds**
- [ ] `refund_window_days` 30 → 14 → no guard fires; shadow run diverges; bound breaches; skill demoted and recompiled

**T6.6 Ablation run**
- [ ] Arms A, C, E over the full seeded stream with the same drift schedule
- [ ] Arm C shows duplicate refunds where arm E shows zero (§14.5.3)
- [ ] Arm E cost per task below arm A after warm-up, **net of parsing and shadow cost**
- [ ] Arm E verified success not below arm A
- [ ] Dashboard reads correctly live throughout

**T6.7 Fix list**
- [ ] Triage whatever broke, split the fixes by column, re-run T6.3 → T6.6 once

---

## 7. Demo (both, hours 35–36)

§16.5 is the deliverable. Each beat needs both halves working, so it is owned jointly.

**T7.1 Stage state** — `scripts/seed_demo.py` was built in Saurav's P4
- [ ] Run it against a clean DB and confirm the dashboard opens onto a warm, active skill

**T7.2 The four beats**
- [ ] 0:20–0:50 — show the compiled skill as readable code, guards with support counts
- [ ] 0:50–1:15 — stream tasks; cost-per-task drops, compiled share rises
- [ ] 1:15–1:40 — **flip `currency_mix`** → cheap safe-zone deopt, nothing written
- [ ] 1:40–2:10 — **flip `refund_returns_pending`** → on-stack deopt; show the frame; show the fenced repeat; show arm C's duplicate refund side by side
- [ ] 2:10–2:40 — **flip `refund_window_days` 30 → 14** → no guard fires; shadow catches it; bound breaches; skill demoted and recompiled
- [ ] Have a recorded fallback video of each beat

**T7.3 Judge prep** — rehearse the §16.6 answers, especially "isn't this just caching?" and "where is the recursion?"

---

## 8. If we fall behind — cut in this order

Cut from the bottom. Never cut anything above the line.

| Cut | Cost of cutting |
|---|---|
| Second domain / family | None — it was always stretch |
| Compiled argument extractors (§8.8) | Parsing stays more expensive; say so honestly |
| Megamorphic detection + TTL | Lose the §12.4 "knows when not to compile" answer |
| Polymorphic recompilation | Lose demo beat 2's payoff; the cheap deopt still shows |
| Arms B and D | Lose two ablation columns; keep A, C, E |
| Dashboard polish | Read numbers off the terminal |
| — **do not cut below this line** — | |
| Gateway + journal + fencing | The correctness claim dies |
| On-stack deopt frame + resume | **This is the contribution.** Without it the project is a cache. |
| Shadow testing + Clopper–Pearson bound | The "measured silent error" claim dies |
| Hard verifier | Nothing is provable |

