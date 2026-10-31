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

## Phase 4 results, including what was missed (historical)

| Metric | Target | Phase 2 | Phase 4 | |
|---|---|---|---|---|
| `precision_at_5` | >= 0.70 | 0.000 | **0.750** | met |
| `cross_project_hit_rate` | >= 0.30 | 0.000 | **0.300** | met |
| `gate_precision` | >= 0.80 | 1.000 | **1.000** | met |
| `false_injection_rate` | <= 0.10 | 0.000 | **0.174** | **missed** |
| `ndcg@5` / `mrr` | - | 0.000 | 0.617 / 0.750 | |
| `forbidden_rate` | - | 0.000 | **0.000** | |

Three of four. The fourth is worth being precise about rather than quietly
restating the target.

**What false injection is measuring here.** Of the 23 episodic records served
across the whole set, four are not in the label list for their query. None of
them are `forbidden` records — the deliberately-planted near-misses are never
served, which is the stricter test. The four are records that are genuinely on
topic and that a reader would not call wrong; they are simply not the ones the
label lists. With 22 episodic queries and roughly one served record each,
reaching 0.10 means allowing two unlabelled records across the entire set, which
is close to demanding a perfect ranker.

**Why it is not simply tuned lower.** The cut thresholds were swept jointly
against the cross-project boost and the fingerprint floor. Every configuration
that pushed false injection under 0.15 also pushed `precision_at_5` below 0.70
or the cross-project rate below 0.30; the three move against each other. The
configuration shipped is the best point found that holds two targets outright.
The sweep is reproducible: `RELATIVE_FLOOR`, `MINIMUM_SCORE`,
`FINGERPRINT_FLOOR` and `cross_project_boost` are the four constants, and their
docstrings record what each alternative cost.

**A caveat that cuts the other way.** An earlier configuration reached 0.136,
before an absolute cosine floor was added ahead of score normalisation. Without
that floor, a query about "kubernetes ingress certificate rotation" against a
corpus containing nothing of the sort still served a record with a final score
of 0.945 — normalising to the best hit guarantees something always scores 1.0,
however bad the field. That is false injection in the plainest possible sense,
and this eval set has no query that exercises it, so the metric cannot price it.
The floor costs four points of measured false injection and removes a class of
the real thing. It stays.

**Tuning disclosure.** Those four constants were fitted on this 40-query set.
The set is therefore no longer a clean held-out measure *of them*, though it
remains one for everything else. The honest fix is a second labelled set, which
is noted in the roadmap rather than pretended away.

Latency is zeroed in committed baselines. It is machine-dependent and would
churn the diff on every run; it is still measured and reported at runtime.

## Correction to the Phase 4 figures (v0.2.0)

The Phase 4 table above records `false_injection_rate` 0.174, `precision@5` 0.750
and `cross_project_hit_rate` 0.300. **Those figures cannot be reproduced and should
not be quoted.** The cause was found in v0.2.0: the service computed a moment for
each request but never handed it to the retriever, so every run scored recency
against the wall clock. The evaluation therefore drifted on its own as time passed
(`precision@5` was 0.80 on the day it was fitted, 0.57 three months later and 0.0 a
year on).

The set now pins `as_of: 2026-07-15` and the clock reaches ranking, so a run is
reproducible to the digit. Measured with the hashing embedder:

| Metric | Target | v0.2.0 (pinned) | |
|---|---|---|---|
| `gate_precision` | >= 0.80 | **1.000** | met |
| `precision_at_k` | >= 0.70 | **0.742** | met |
| `cross_project_hit_rate` | >= 0.30 | **0.325** | met |
| `false_injection_rate` | <= 0.10 | **0.208** | **missed** |
| forbidden sources served | 0 | 0 | met |
| mean / max tokens | <= 2300 | 405 / 667 | met |
| p95 latency | | 13.6 ms | |

The ratchet in `tests/test_retrieval.py` is set to 0.21 so it cannot get worse.

### Current (v0.3.0, pinned clock)

After the v0.3 retrieval fixes (the last exploratory branches of the history gate were
removed, so a prompt that names nothing never searches history records; a named project is served at lower coverage; "explain the ..." is trivial
only before a code noun), the same pinned run measures:

| Metric | Target | v0.2.0 | **v0.3.0** | |
|---|---|---|---|---|
| `gate_precision` | >= 0.80 | 1.000 | **1.000** | met |
| `precision_at_k` | >= 0.70 | 0.742 | **0.750** | met |
| `cross_project_hit_rate` | >= 0.30 | 0.325 | **0.325** | met |
| `false_injection_rate` | <= 0.10 | 0.208 | **0.174** | **missed** |
| `ndcg@5` / `mrr` | | | 0.645 / 0.795 | |
| forbidden sources served | 0 | 0 | **0** | met |
| mean / max tokens | <= 2300 | 405 / 667 | 387 / 674 | met |
| p95 latency | | 13.6 ms | 12.6 ms | |

The coincidence with the irreproducible Phase 4 figure of 0.174 is only that: this one is
pinned and reproducible to the digit. False injection is counted per record, and the
remaining 4 unlabelled records out of 23 served all come from queries that expect a record
and received one correct record and one extra.

How it got from 0.323 to 0.208 (the first run of the combined front door scored
0.375): every unlabelled item came from the history-record path, none from the
new passage path, and nearly all from vague prompts that name nothing ("what
should I name this module?"), where the old exploratory rule searched anyway.
A threshold sweep could not fix that (the best setting that kept the other
targets was 0.296), so the rule was removed: a prompt that names nothing no longer
searches history records and is left to the passage path, which serves only what
covers the question. That gave 0.240; moving `RELATIVE_FLOOR` from 0.78 to 0.85,
which a sweep showed kept real margins on the other metrics, gave 0.208. The set
is 40 queries, so this is a tuned result on a small set, and a lower setting
(0.45 on the minimum score) was declined because it left the cross-project rate
exactly on its limit.

## Measured on real material (v0.2.0, semantic results v0.3.0)

The synthetic set cannot tell you whether retrieval works on what *you* have.
`projectmind eval real` scores the front door against questions about your own
files, commits, sessions and competitions. A query names what a correct answer must
cite, as substrings of the citation (so labels survive re-ingestion), or says that
the topic is absent and the right answer is silence. The query file names real
projects, so it lives outside version control (`~/.projectmind/eval/`); the format
is in [`eval/real.example.yaml`](../eval/real.example.yaml).

Measured on 2026-10-30 against a knowledge base of 9,000 documents and 27,400
passages (git history, notes and logs, notebooks, PDFs, Word and PowerPoint files,
Claude Code sessions and allow-listed browser pages), with 42 queries: 20
answerable in the sources' own words, 12 reworded to avoid them, and 10 about
topics that appear nowhere in the material.

| Metric | Result |
|---|---|
| Hit rate, sources' own words | **0.900** (18 of 20) |
| Hit rate, reworded questions | **0.667** (8 of 12) |
| Hit rate, all answerable | **0.812** (26 of 32) |
| Silence on absent topics | **1.000** (10 of 10) |
| False injection rate | **0.000** |
| Forbidden sources served | 0 |
| Served precision (served items that are expected sources) | 0.707 |
| Mean tokens per bundle | 535 |
| p95 latency (in process, hashing embedder) | 80 ms |

With semantic search (v0.3.0, measured 2026-10-31 on a copy of the same knowledge base
with all 27,403 passages embedded):

| | Hit rate | Silence | Served precision | p95 |
|---|---|---|---|---|
| Lexical only | 0.812 | 1.000 | 0.707 | 81 ms |
| **Semantic, default bar (0.78)** | **0.844** | **1.000** | 0.714 | 93 ms |
| Explicit search, words only | 28 of 32 found | | | |
| **Explicit search, words and meaning** | **30 of 32 found** | | | |

The threshold sweep that chose 0.78 is in [semantic.md](semantic.md#why-the-automatic-bar-is-high).

How to read these honestly:

- **Both misses served plausible sources that the labels did not list.** One asked
  what to do when a score plateaus and was answered from other competitions' notes
  on exactly that, not from the playbook file the label named; the other asked about
  bar blocks and was answered from the design documents that describe them, not
  the commit. They are counted as misses because the labels are not edited after the
  fact. Served precision (0.707) is the same effect measured across all answers.
- **The labels were written by the author of the system**, after looking at the
  index. A set labelled before the retriever existed would be harder. Treat 0.90 as
  an upper-ish figure and watch the replay (`projectmind history replay`) of real
  questions as the better long-run signal.
- **Silence has a cost.** Precision-first retrieval answers nothing when the question
  uses words the notes never do ("rate limits" for notes that say "429"). The ten
  silent queries cannot measure that; the interaction history can (`history list
  --empty`, then label what should have answered).
- **The reworded half is the honest one.** The first 20 questions were written
  in the projects' own words and score 0.90. The 12 paraphrases score 0.67, and the
  four misses are a true synonym gap ("heart cycle" for "Cardiac Cycle"), an
  abbreviation ("PF" for profit factor), a project referred to by description
  rather than name, and a topic described in different words than its notes use.
  Lexical retrieval cannot close that. Optional semantic search (see
  [semantic.md](semantic.md)) closes part of it: it lifts the hit rate from 0.812 to
  0.844 with silence held at 1.000, because the safe threshold admits only one of the
  four. The explicit search, which may show labelled neighbours, finds 30 of 32
  answerable questions against 28 by words alone; the two it misses are the
  label-strictness cases above.
- **What moved these numbers.** Two changes lifted the reworded set from 5 of 12
  to 8 of 12: a question that names a project now needs only 35% coverage
  instead of 60% (naming a project is strong evidence), and short "explain the
  ..." prompts are no longer classed as trivial unless they name a code noun. Silence
  and false injection did not move.
- **Forty-two queries is small.** One query moves the hit rate by 2 to 5 points.

## Running it

```bash
projectmind eval run --system live                      # the synthetic set, pinned clock
projectmind eval run --system live --save eval/baselines/phase-4.json
projectmind eval compare eval/baselines/phase-0-null.json eval/baselines/phase-4.json
projectmind eval real                                   # your own questions, your own data
projectmind eval real --min-hit 0.8 --max-false-injection 0.1   # exit non-zero on regression
projectmind eval real --semantic                        # the same, with meaning matches (needs `semantic index`)
```

## What this set does not measure

- **Real-world distribution.** These are 40 queries someone wrote down, not 40
  queries sampled from a week of work. They over-represent interesting cases.
- **Profile quality.** Profile statements are scored only as "served or not".
  Whether a statement is *true* is what the monthly review is for.
- **Downstream effect.** Retrieval precision is a proxy. Whether the agent's
  output actually improved is measured separately, through the bundle log and
  the clarification-turn count.
