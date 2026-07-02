# Roadmap

Six phases. Each one ends with something usable, and no phase starts before the
previous one is measured. Status is updated as each exit criterion is met.

| Phase | Deliverable | Exit criterion | Status |
|---|---|---|---|
| 0 | Eval harness | 40 queries committed, scorer runs, baseline recorded | not started |
| 1 | Profile memory + always-on injection | running in the daily workflow, profile slice under 800 tokens | not started |
| 2 | Project fingerprinting + the gate | gate precision ≥ 0.80 on the no-injection cases | not started |
| 3 | Cross-project episodic ingestion | `search_memory` live, precision@5 scored | not started |
| 4 | Retrieval quality | precision ≥ 0.70, cross-project hit rate ≥ 30%, false injection ≤ 10% | not started |
| 5 | Reflection loop + monthly review | ≥ 60% proposal acceptance, review under 5 minutes | not started |
| 6 | Browser and AI chat capture | measurable improvement on the Phase 0 eval, or cut | deferred |

## Why this order

Phase 0 is first because the no-injection cases are the only way to measure the
gate, and the gate is the component whose failures are silent.

Phase 1 is hand-seeded on purpose. Writing fifty preference statements by hand
teaches what a good statement looks like before the reflection loop tries to
infer them, and it gives Phase 5 a baseline it has to beat to justify itself.

Phase 2 lands before any episodic memory exists, which means gate precision can
be measured in isolation rather than confounded with retrieval quality.

Phase 6 is last because browser history and chat logs are the noisiest sources
with the highest privacy cost. They feed the profile layer, never the episodic
one, and the phase is cut if it does not move the eval.
