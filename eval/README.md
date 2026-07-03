# Evaluation

The harness is built before the pipeline, because a retrieval system without a
score is a system whose regressions are invisible.

## What is measured

| Metric | Definition | Target |
|---|---|---|
| `precision_at_5` | Of the episodic records served, the share labelled relevant. Averaged over queries that expect episodic context. | >= 0.70 |
| `cross_project_hit_rate` | Share of cross-project cases where at least one served record came from a different project than the caller. | >= 0.30 |
| `gate_precision` | Share of queries where the decision to inject or not matched the label. | >= 0.80 |
| `false_injection_rate` | Of every episodic record served across the whole set, the share not labelled relevant for its query. | <= 0.10 |
| `ndcg_at_5`, `mrr` | Ranking quality, reported for diagnosis rather than as a gate. | - |
| `p95_latency_ms` | Wall time of the front door. | - |

`false_injection_rate` is the metric that matters most and the one that is
easiest to ignore, because a bad gate degrades every task silently. It is
computed over records rather than over queries, so one query that serves five
wrong records counts five times.

## The query set

`queries.yaml` holds 40 hand-written queries. The mix is deliberate:

- **cross-project** cases, where the right answer lives in a different
  repository. These are the reason the system exists.
- **trivial** cases, where the right answer is nothing at all. Without these,
  gate precision cannot be measured, and a system that always injects scores
  well.
- **debug** cases, which should surface past failures and not past decisions.
- **near-miss** cases, where a plausible-looking record is labelled `forbidden`
  because serving it here would be wrong.

Every query carries a `note` explaining the judgement, so the labels can be
argued with later instead of taken on faith.

## Running it

```bash
projectmind eval run --system null      # record the floor
projectmind eval run --system live      # score the current front door
projectmind eval compare eval/baselines/phase-0.json eval/baselines/phase-4.json
```

Results land in `eval/baselines/` and are committed, so every phase boundary
has a number attached to it in the history.
