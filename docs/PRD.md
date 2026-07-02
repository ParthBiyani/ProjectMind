# ProjectMind — PRD v2

**Status:** Pre-build, architecture locked
**Owner:** Parth
**Changes from v1:** Preference lifecycle decided (explicit supersession + periodic re-confirmation). Retrieval mode decided (default injection, gated on project structure + prompt). Both decisions propagate through schema, MCP surface, and roadmap.

---

## 1. Problem

Coding agents start every task with amnesia. They don't know you already solved this auth pattern in a different repo, in a different language, eight months ago. They don't know you tried Riverpod and abandoned it. They don't know you reach for Flutter for anything client-side and drop no-code tools within two days.

Per-repo context tools — Cursor's codebase index, `CLAUDE.md`, project rules — solve none of this and structurally can't, because they are scoped to the repo they live in. The gap is **cross-project** and **personal**, and it is the one gap no single agent vendor is positioned to close.

## 2. What ProjectMind is

A memory layer behind coding agents, supplying two distinct kinds of context.

| | **Profile memory** | **Episodic memory** |
|---|---|---|
| Content | How you work, what you reach for, what you've abandoned | What happened, on which project, when, why |
| Volume | 50–200 statements | Thousands of records |
| Update path | Reflection loop → human approval | Continuous ingestion |
| Injection | **Always**, every task | **Conditionally**, when the gate fires |
| Cost of a wrong record | High — poisons every future task | Low — poisons one task |
| Confidence bar | ≥ 0.85 to activate | ≥ 0.6 to serve |

Delivered as an **MCP server**. No chatbot in v1. No Flutter frontend in v1.

## 3. Non-goals (v1)

- Writing or editing code
- Teams / multi-user
- Slack, Notion, meeting transcription
- Neo4j or any graph DB — Postgres recursive CTEs until proven insufficient
- Eleven specialized agents — two extractors and one reflection agent
- Cloud hosting — local-first, single user

## 4. User

One: you. Everything else is premature.

---

## 5. Decision A — How a preference dies

Two mechanisms, both required. Neither alone is sufficient: supersession catches sharp reversals, re-confirmation catches slow drift.

### 5.1 Explicit supersession

Triggered by contradicting evidence, not by time.

When the reflection loop ingests episodic records that contradict an active profile statement, it does **not** create a second, competing statement. It opens a **supersession proposal**:

```
ACTIVE:    "Prefers Firebase for backend-as-a-service on mobile projects"
           confidence 0.9 · active since 2025-11 · 4 supporting refs

CONTRADICTED BY:
           ShopOS v2  → chose Supabase   (2026-02)
           CivicPulse → chose Supabase   (2026-03)
           Kairos     → chose Supabase   (2026-04)

PROPOSED:  "Prefers Supabase for backend-as-a-service; moved off Firebase
            over pricing at scale"

[Supersede]  [Keep both — they're context-dependent]  [Reject proposal]
```

Three outcomes, all meaningful:

- **Supersede** → old statement `status = superseded`, `superseded_by` set, retained in history but never served. Nothing is deleted; the timeline of how you changed is itself valuable.
- **Keep both** → the system was wrong that they conflict. Both stay active, each gains a `scope` qualifier so the gate can disambiguate ("Firebase for throwaway prototypes, Supabase for anything shipping").
- **Reject** → contradicting evidence was noise. Statement's `last_confirmed_at` refreshes, `contradiction_count` resets. **Log the rejection** — repeated false contradictions on the same statement means the extractor is misreading a class of artifact.

**Contradiction threshold:** a single contradicting record does not open a proposal. Three within 90 days does, or one record explicitly flagged `type = reversal` by the extractor. This exists to stop one experimental branch from overturning a settled preference.

### 5.2 Periodic re-confirmation

Catches the preferences that die quietly — no dramatic reversal, you just stopped doing it.

Every profile statement carries `last_confirmed_at`. A statement is due for re-confirmation when:

```
now - last_confirmed_at > ttl(category)
```

| Category | TTL | Rationale |
|---|---|---|
| `tool_preference` | 90 days | Fastest-moving; the ecosystem churns |
| `work_style` | 180 days | Stable but not permanent |
| `abandoned` | 365 days | You rarely un-abandon things |
| `constraint` | 180 days | Hardware, budget, deadline — real but changeable |

**Re-confirmation is passive first, active second.** If new supporting evidence arrives during the TTL window, `last_confirmed_at` refreshes silently and you are never asked. You only get asked about statements with *no* evidence either way.

Un-refreshed statements decay rather than die outright:

```
TTL exceeded         → confidence × 0.8, still served
2 × TTL exceeded     → confidence × 0.5, served only if nothing better ranks
3 × TTL exceeded     → status = dormant, not served, surfaced in next review
```

**Review batching is a hard requirement.** A tool that pings you weekly to confirm facts about yourself will be uninstalled within a month. All pending re-confirmations and supersession proposals collect into a **single monthly review** — one sitting, target under five minutes, presented as a ranked list with the highest-impact items first. Nothing interrupts a working session, ever.

---

## 6. Decision B — Default injection

Context is pushed by default, not pulled on the agent's initiative. The agent should not have to know that memory exists.

### 6.1 The mechanism, honestly stated

MCP is pull-based; a server cannot force content into an agent's context. So "default injection" is implemented as **one mandatory front-door tool** the agent is instructed to call once at the start of every task, plus a client-side hook where the client supports one.

```
get_context(prompt: str, project_path: str) -> ContextBundle
```

The agent calls this and nothing else. All gating, ranking, and budgeting happens server-side. This matters: leaving retrieval decisions to the agent means retrieval quality varies by which agent you're using, and you can't measure it.

Where the client supports a pre-prompt hook (Claude Code hooks, Cursor rules), the call is wired there and becomes invisible. Where it doesn't, a one-line instruction in the project rules file does the job. **Fallback behavior is explicit:** if the agent doesn't call `get_context`, it gets no memory — the system degrades to a normal agent, never to a wrong one.

### 6.2 The gate

Two signals decide what gets injected: **project structure** and **the prompt**.

```
                 ┌──────────────────┐
prompt ─────────►│                  │
                 │   RELEVANCE      │──► profile slice  (always)
project_path ───►│      GATE        │──► episodic slice (conditional)
   ↓             │                  │
 fingerprint     └──────────────────┘
```

**Project fingerprint** — computed on first call per project, cached, invalidated on manifest change. Cheap and deterministic; no LLM.

- Language and framework from manifests (`pubspec.yaml`, `package.json`, `requirements.txt`, `Cargo.toml`)
- Dependency set — the strongest cross-project similarity signal you have
- Directory shape and rough size
- Git remote → project identity
- Domain hints from README

The fingerprint is what makes cross-project retrieval work. A Flutter + Supabase + Riverpod fingerprint matches your other Flutter + Supabase projects **before any semantic search runs**, which is exactly the "I solved this before, elsewhere" case that per-repo tools structurally cannot serve.

**Prompt analysis** — a small classifier or a cheap LLM call:

- Task type: implement / debug / refactor / architect / explore
- Technical entities: named tools, libraries, patterns
- Novelty: does this touch territory the fingerprint says is new?

**Gating rules:**

| Condition | Injected |
|---|---|
| Always | Profile slice, filtered to statements matching the fingerprint |
| Prompt names an entity present in episodic memory | Matching decisions + failures, cross-project first |
| Task type is `architect` or `explore` | Prior decisions on similar fingerprints, weighted toward cross-project |
| Task type is `debug` | Failure records only — past bugs and what fixed them |
| Prompt is trivial (rename, format, one-liner) | Nothing. Skip the retrieval entirely. |
| No episodic record clears confidence ≥ 0.6 | Profile only. **Never pad to fill the budget.** |

### 6.3 Budget

Enforced server-side. Hard caps.

| Slice | Cap |
|---|---|
| Profile | 800 tokens (~15 statements, fingerprint-filtered) |
| Episodic | 1500 tokens (~5 records) |
| **Total** | **2300 tokens** |

Ranking when over budget: confidence × recency-decay × fingerprint-similarity, with an explicit **cross-project boost** so a mediocre same-repo result doesn't automatically outrank a strong cross-repo one. That boost is the product; without it you've rebuilt Cursor's index.

### 6.4 The escape hatch

`get_context(..., ignore_profile=true)`, plus a `/pm off` toggle for the session.

Non-negotiable and worth stating plainly: a system that always tells your agent "Parth prefers Flutter" makes you faster at what you already do and quietly worse at anything new. Deliberate exploration must be able to run clean.

---

## 7. Data model

**Project**
```
id, name, git_remote, fingerprint (jsonb), languages[], frameworks[],
dependencies[], first_seen, last_active
```
First-class entity. Cross-project retrieval is the point; project cannot be an afterthought field.

**Profile statement**
```
id, statement, category (tool_preference | work_style | abandoned | constraint),
scope (jsonb — nullable qualifier from a "keep both" resolution),
evidence_refs[], confidence,
status (proposed | active | superseded | dormant | rejected),
superseded_by, supersedes,
contradiction_count, contradiction_refs[],
created_at, last_confirmed_at, ttl_days, origin (manual | reflection)
```

**Episodic memory**
```
id, project_id, type (decision | failure | experiment | research | reversal),
what, why, rejected_alternatives[], outcome,
entities[] (tools, libraries, patterns — indexed, drives the prompt gate),
source_url, source_type, occurred_at,
confidence, is_inference (bool), superseded_by, embedding
```

**Review queue**
```
id, kind (supersession | reconfirmation), target_id, proposal,
evidence_refs[], created_at, resolved_at, resolution, batch_id
```

### Hard rules

1. **Every record carries provenance.** No unattributed claims. Memory the user can't click through and verify won't be trusted, and untrusted memory is worse than none.
2. **Inference is labeled as inference.** An extracted "why" is an LLM hypothesis about an artifact. `is_inference` exists so the agent can be told so.
3. **Supersession, not accumulation.** Never store both sides of a contradiction as active.
4. **Precision over recall.** If unsure, don't serve it. Empty is a valid response.
5. **Nothing interrupts a working session.** All human review is batched monthly.

---

## 8. Success metrics

These *are* the resume bullets. Instrument before building the pipeline.

| Metric | Target |
|---|---|
| Retrieval precision @5, hand-labeled 40-query eval set | ≥ 0.70 |
| Cross-project hit rate (relevant result from a different repo) | ≥ 30% of queries |
| Gate precision (injected-when-useful, no-injection-when-not) | ≥ 0.80 |
| **False injection rate** (irrelevant context pushed) | **≤ 10%** |
| Profile proposals accepted at monthly review | ≥ 60% |
| Monthly review time | < 5 min |
| Reduction in clarification turns, with vs. without | measurable, directional |
| Repos ingested / history covered | 8+ / 12+ months |

False injection rate is the metric that matters most and the one nobody measures. Default injection means a bad gate degrades every task silently — the developer never learns why the output went sideways. Track it from day one.

---

# Roadmap

Six phases. Each ends with something usable. Do not start a phase before the previous one is measured.

### Phase 0 — Eval harness *(first, always)*
40 hand-written queries against your own history, with the answers you'd want back. Must include: cross-project queries, trivial prompts that should return **nothing**, and debug-type prompts. The no-injection cases are what let you measure the gate.
**Exit:** eval set committed, scoring script runs, baseline recorded.

### Phase 1 — Profile, hand-seeded + always-on injection
Write ~50 preference statements yourself, tagged by category with TTLs set. Postgres. MCP server exposing `get_context` — profile slice only, fingerprint-filtered. Wire into Claude Code via hook. Use daily.
**Why first:** trivially buildable, immediately useful, and it teaches you what a good preference statement looks like before you try to infer them.
**Exit:** running in daily workflow; profile injection under 800 tokens.

### Phase 2 — Project fingerprinting + the gate
Manifest parsing, dependency extraction, fingerprint cache with invalidation. Prompt classifier. Gating rules from §6.2, including the trivial-prompt skip.
**Exit:** gate precision ≥ 0.80 on Phase 0's no-injection cases. Measurable before any episodic memory exists — which is the point of sequencing it here.

### Phase 3 — Cross-project episodic ingestion
GitHub API across all repos. Two extractors: decisions (PRs, commits) and failures (reverts, fix commits, issue closures). Entity extraction feeding the prompt gate. Run over 50 PRs and read every output yourself — expect to discard the prompt twice.
**Exit:** `search_memory` live, precision @5 scored.

### Phase 4 — Retrieval quality
BM25 + vector hybrid, merged. Fingerprint-similarity as an explicit ranking feature. Tune the cross-project boost. Cross-encoder re-rank only if numbers demand it.
**Exit:** precision ≥ 0.70, cross-project hit rate ≥ 30%, false injection ≤ 10%.

### Phase 5 — Reflection loop + monthly review *(the interesting engineering)*
LangGraph job: read recent episodic memory → detect contradictions against active profile → open supersession proposals; scan TTLs → passive refresh where evidence exists, queue the rest. Monthly batch review UI (a terminal prompt is fine — no Flutter). Log every rejection as extractor signal.
**Exit:** ≥ 60% acceptance, ≥ 10 statements originated by the loop, review under 5 min, at least one full supersession observed end to end.

### Phase 6 — Browser + AI chat capture *(last, not first)*
Noisiest sources, highest privacy risk, lowest per-item signal — but they feed the **profile** layer well, because "searched X, read three docs, never used it" is a real fact about how you work. Extract to profile, never to episodic.
**Exit:** measurable improvement on the Phase 0 eval. If none, cut the phase and say so.

---

## Stack

Python, FastAPI, Postgres + pgvector, LangGraph (Phase 5), MCP SDK, one LLM API. Local-first; no repo content leaves the machine except to the extraction model, with a per-repo exclusion list.

Resume stack line: **LangGraph, MCP, RAG, pgvector** — four, not six. MCP earns its place far more than FastAPI does.

## Target resume bullets

Build toward these; they define what to instrument.

> Built a cross-project engineering memory layer exposed via MCP, injecting decisions, failed approaches, and inferred work preferences into coding agents by default — gated on project fingerprint and prompt intent — across 8 repos and 14 months of history.

> Designed a human-in-the-loop reflection loop with explicit supersession and TTL-based re-confirmation for preference decay; measured retrieval precision at 0.7X on a hand-labeled 40-query eval set, false injection rate under 10%, and X% fewer redundant clarification turns per agent session.

The second bullet gets the interview. Almost nobody puts a number on retrieval quality, and fewer still measure the cost of retrieving wrongly.

## Remaining open questions

1. **Privacy boundary.** Local-first is assumed, but the extraction model sees repo content. Which model, and which repos are on the exclusion list? Decide before Phase 3.
2. **Cold start.** Phases 1–2 run on a hand-written profile. If the Phase 5 loop can't beat your hand-written statements on the eval, the loop isn't earning its complexity — be willing to find that out and say so.
3. **Contradiction detection cost.** Checking every new episodic record against every active profile statement is O(n×m) LLM calls. Batch it, or pre-filter on entity overlap before spending a call.
