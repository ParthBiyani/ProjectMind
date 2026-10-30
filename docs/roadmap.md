# Roadmap

Six phases and a hardening release. Each one ends with something usable, and no
phase starts before the previous one is measured. Status is updated as each exit
criterion is met.

| Phase | Deliverable | Exit criterion | Status |
|---|---|---|---|
| 0 | Eval harness | 40 queries committed, scorer runs, baseline recorded | **complete** |
| 1 | Profile memory + always-on injection | running in the daily workflow, profile slice under 800 tokens | **complete** |
| 2 | Project fingerprinting + the gate | gate precision ≥ 0.80 on the no-injection cases | **complete** |
| 3 | Cross-project episodic ingestion | `search_memory` live, precision@5 scored | **complete** |
| 4 | Retrieval quality | precision ≥ 0.70, cross-project hit rate ≥ 30%, false injection ≤ 10% | **3 of 4 met** (re-measured under a pinned clock in v0.2.0; see below) |
| 5 | Reflection loop + monthly review | ≥ 60% proposal acceptance, review under 5 minutes | **complete, unmeasured** |
| 6 | Browser and AI chat capture | measurable improvement on the Phase 0 eval, or cut | **built in v0.2.0**, differently from the design (below) |
| v0.2 | Real-world hardening and a grounded knowledge base | works on real material, measured | **complete**; see below |

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

## Recorded results

| Phase | Date | gate_precision | precision@5 | cross_project | false_injection |
|---|---|---|---|---|---|
| 0 (null floor) | 2026-07-04 | 0.250 | 0.000 | 0.000 | 0.000 |
| 0 (oracle ceiling) | 2026-07-04 | 1.000 | 1.000 | 0.375 | 0.000 |
| 2 (gate, no retriever) | 2026-07-12 | **1.000** | 0.000 | 0.000 | 0.000 |
| 4 (hybrid retrieval) | 2026-07-15 | 1.000 | **0.750** | **0.300** | 0.174 |

Phase 1 exit, measured on the seeded profile: the slice stays at 436–481 tokens
against the 800 cap across Flutter, ML and unknown-stack fingerprints, and 15
of 50 statements are selected. The Phase 0 eval is not re-run here because
Phase 1 serves no episodic memory; its numbers would be identical to the null
floor by construction. Retrieval scoring resumes at Phase 2, where the gate
gives it something to measure.

Phase 2 exit: gate precision **1.000** against a target of 0.80, with all ten
no-injection cases correctly served nothing and zero budget breaches. Task-type
accuracy is 0.850 and episodic-gate agreement 0.900; the six task-type
disagreements are all "implement versus architect" on prompts phrased *how
should I...*, which route to the same place. `precision@5` and the cross-project
rate are still zero because no retriever is attached yet — that is Phase 4, and
scoring it now would be scoring an empty index.

Methodology and the meaning of each metric: [evaluation.md](evaluation.md).

## Phase 4, stated plainly

Three of four targets met. `false_injection_rate` sits at 0.174 against a
target of 0.10 and is not being rounded down. The full analysis, including the
threshold sweep and the one trade that deliberately made the number worse, is
in [evaluation.md](evaluation.md#phase-4-results-including-what-was-missed).

Ingestion covers 11 local repositories and 190 commits, producing 82 episodic
records with no network call and no API token.

## Phase 5, stated plainly

The loop runs, the state machine is real, and a full supersession is exercised
end to end in the test suite: evidence accumulates, a proposal opens, a human
accepts it, the old statement stops being served and the timeline survives.

The entity prefilter answers the PRD's open question about O(n x m) cost with a
number. Over the real local corpus — 50 statements against 89 ingested records,
4450 possible pairs — it discards **99.1%** for free, leaving 42 pairs for the
structural rules. A model call per pair would have been unaffordable; at 42 it
is optional.

**The acceptance-rate target is not measured.** It needs a human working
through a queue of real proposals, and the honest position after two weeks is
that the corpus has not yet drifted enough to produce one outside a constructed
test. `projectmind review history` reports the rate as soon as there is one to
report. Claiming 60% now would be inventing it.

## Known gaps

- **A second eval set.** Four ranking constants were fitted on the existing 40
  queries, so that set no longer measures them independently.
- **Phase 6 is unbuilt.** Browser and chat capture remain designed and
  documented only; see [phase-6-capture.md](phase-6-capture.md).

## v0.2.0: what changed and why

v0.1.0 passed its own tests and failed on a real machine. Run against real
repositories it read one repository of fourteen (git's ownership check refused the
rest and the tool reported "not a git repository"), wrote bytes into the agent's
context that were not valid UTF-8, reported a healthy install that was not
installed, and answered questions about the user's own projects with nothing,
because retrieval was gated on a closed list of technology names. Its evaluation
also drifted with the wall clock, so its recorded numbers could not be reproduced
(see [evaluation.md](evaluation.md)).

v0.2.0 fixes those and makes the system answer from what its owner has actually
written down:

1. **Hardening.** Per-command ownership override; UTF-8 hook I/O in isolated mode;
   a clock that reaches ranking; `.env` read from ProjectMind's home only; `doctor`
   that runs the hook; profile add/edit/remove; a complete interaction history with
   `replay`.
2. **A cited knowledge base.** Notes, scripts, notebooks, logs, documents and Claude
   Code sessions become verbatim, citable passages; commits are searchable by what
   they say; a passage is served only when it covers one of the question's most specific
   term. Honest records: no invented reasons.
3. **Always current.** `sync`, a local daemon, a fast hook, a logon task.
4. **The browser.** Allow-listed history and an Edge/Chrome extension limited by its
   manifest to the same list.
5. **Proof.** A scorer for questions about your own material, and a demo that is
   also a regression test.

Phase 6 was designed as a source of *profile proposals* and deliberately left
unbuilt. What was built instead feeds the *knowledge base*: reading and session
turns become citable passages, which is the part that was measurable. Turning
repeated reading into profile proposals ("researched X six times, never adopted
it") remains unbuilt, for the reason the design gave: it needs a conservative
extractor and its own evaluation.

## Known gaps after v0.2.0

- **Vocabulary mismatch is silence.** Retrieval is lexical with stemming. A question
  in words the notes never use returns nothing. An opt-in embedding reranker is the
  likely fix, and needs its own calibration.
- **The history-record path still has a closed-vocabulary gate** and misses its
  false-injection target (0.323 against 0.10). Commits are now also reachable through
  the open-vocabulary path, which makes this less important in practice, not
  non-existent.
- **Optional notes from a language model** (`ANTHROPIC_API_KEY`) are described in the
  configuration but not implemented. Everything else works without a key.
- **The extension has not been loaded into a browser in CI.** Its logic and its
  manifest are tested; the end-to-end check is manual (`extension/README.md`).
- **Evaluation labels were written by the system's author** after seeing the index.
