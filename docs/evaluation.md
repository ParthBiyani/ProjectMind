# Evaluation methodology

Nothing in this repository is allowed to claim an improvement without a number
attached. This document says how those numbers are produced and what they are
worth.

## The set

40 queries in [`eval/queries.yaml`](../eval/queries.yaml), each labelled by hand
with the bundle it should receive. 51 episodic records across 14 projects in
[`eval/fixtures/corpus.yaml`](../eval/fixtures/corpus.yaml) provide the answers.

| Property | Count | Why |
|---|---|---|
| Queries | 40 | Small enough to label carefully, large enough that one query does not move a metric by more than 2.5 points. |
| Expect nothing | 10 | Without these, a system that always injects scores perfectly on everything else. |
| Cross-project cases | 15 | The gap that per-repository tooling cannot close. Caps the achievable hit rate at 0.375. |
| Queries with near-misses | 7 | Plausible-looking wrong answers, labelled `forbidden`. A set without them cannot punish a greedy retriever. |
| Task types covered | 6 | implement, debug, refactor, architect, explore, trivial. |

Every query carries a `note` recording the judgement behind its labels. The
labels are opinions, and opinions that cannot be inspected cannot be corrected.

## Deliberate traps

Three queries exist only to catch specific failure modes:

- **q007** ("convert this to an f-string") is a mechanical edit that a task-type
  classifier will happily call a refactor. It must inject nothing.
- **q009** ("bump the version in pyproject.toml") names a manifest file, which
  the fingerprinter cares about. Touching a manifest must not itself be a
  trigger.
- **q036** ("how should I deal with missing lab values") sits one step away from
  two class-imbalance records in closely related datasets. Both are labelled
  forbidden. The question is about imputation.

## Metrics

Defined in [`scoring.py`](../src/projectmind/evaluation/scoring.py). Two are
worth spelling out because reasonable people define them differently.

**`false_injection_rate`** is computed over *records*, not queries: every served
episodic record that is not labelled relevant for its query counts once. A
single query that serves five wrong records is five failures, which is what it
costs the reader. This is the metric that matters most, because default
injection means a bad gate degrades every task silently and the developer never
learns why the output went sideways.

**`cross_project_hit_rate`** uses all 40 queries as its denominator, matching
the PRD's "30% of queries". Only 15 queries have a cross-project answer at all,
so the ceiling is 0.375 and the target of 0.30 means recovering 80% of what is
theoretically available. `cross_project_recall`, over the 15 cases, is the
number to read when diagnosing the ranker.

## Baselines

Both bounds are committed so later phases can be read against something.

| System | gate_precision | precision@5 | cross_project | false_injection |
|---|---|---|---|---|
| [null](../eval/baselines/phase-0-null.json) (serves nothing, ever) | 0.250 | 0.000 | 0.000 | 0.000 |
| [oracle](../eval/baselines/phase-0-oracle.json) (serves the labels) | 1.000 | 1.000 | 0.375 | 0.000 |

The null floor is the interesting one. It scores 0.250 on gate precision purely
by refusing to answer, and a perfect 0.000 on false injection — because a system
that never injects never injects wrongly. Any real system has to beat 0.250
while holding false injection under 0.10, and those two pull in opposite
directions. That tension is the whole engineering problem, and the floor makes
it visible from day one.

Latency is zeroed in committed baselines. It is machine-dependent and would
churn the diff on every run; it is still measured and reported at runtime.

## Running it

```bash
projectmind eval run --system null
projectmind eval run --system live --save eval/baselines/phase-4.json
projectmind eval compare eval/baselines/phase-0-null.json eval/baselines/phase-4.json
```

## What this set does not measure

- **Real-world distribution.** These are 40 queries someone wrote down, not 40
  queries sampled from a week of work. They over-represent interesting cases.
- **Profile quality.** Profile statements are scored only as "served or not".
  Whether a statement is *true* is what the monthly review is for.
- **Downstream effect.** Retrieval precision is a proxy. Whether the agent's
  output actually improved is measured separately, through the bundle log and
  the clarification-turn count.
