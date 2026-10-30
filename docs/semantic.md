# Semantic search

Lexical retrieval is exact and never invents a connection, but it cannot see that
"heart cycle" and "Cardiac Cycle" are the same thing, or that "PF" is profit factor.
Semantic search covers that gap. It is optional and off by default, because it costs
a model download, background CPU, and a decision about how much to trust a near
match.

## Use

```bash
pip install 'projectmind[semantic]'     # fastembed + numpy; the model is ~130 MB, fetched once
projectmind semantic enable
projectmind semantic index              # embeds what is already indexed; safe to interrupt
projectmind semantic status
projectmind daemon stop                 # then start it again (projectmind serve, or log in again);
                                        # the daemon holds the vectors in memory
```

After that, `sync` embeds new passages in the background at low priority, and the
daemon reloads its matrix when it finishes.

## How it works

- Every passage (heading and text, first 1,200 characters) is embedded once with
  `BAAI/bge-small-en-v1.5` and stored as float16 in `chunk_vectors`, keyed by chunk
  and model. Deleting a document deletes its vectors. A different model starts with
  an empty set.
- The daemon loads the vectors into one matrix (about 21 MB for 27,000 passages). A
  search is one matrix product, a few milliseconds, plus embedding the question.
- The hook asks the daemon. The in-process fallback stays lexical on purpose: loading
  a model on every prompt would cost more than the answer is worth.

## Two ways it is used, with different bars

**Automatic injection stays strict.** A meaning match is added after the lexical
answers only if all of these hold:

1. cosine similarity at least `PROJECTMIND_SEMANTIC_THRESHOLD` (default 0.78);
2. within `PROJECTMIND_SEMANTIC_MARGIN` (0.05) of the best match;
3. it shares at least one distinctive word with the question
   (`PROJECTMIND_SEMANTIC_NEEDS_WORD`), a word found in few passages;
4. the usual rules: not already served, one or two passages per document, no
   near-duplicates.

**Explicit search is generous and says so.** `search_sources` (MCP) and
`projectmind sources search` show meaning matches down to similarity 0.60, each
labelled `by meaning, similarity 0.74` or `by words`. Someone asked, so a close
neighbour is welcome, and the label tells the reader not to treat it as verified.

## Why the automatic bar is high

A similarity score cannot tell a paraphrase from a neighbour in the same field. On
the author's material, questions about topics that appear nowhere (Cassandra
compaction, Snowflake migration) scored 0.67 to 0.78, while correct reworded
answers scored 0.72 to 0.93. The ranges overlap, so any threshold that admits the
low correct ones also admits wrong ones. At 0.72 the false injection rate was 20%
to 30%; at 0.78 it was 0%.

| Setting | Hit rate | Silence on absent topics |
|---|---|---|
| Lexical only | 0.812 | 1.000 |
| Semantic, threshold 0.72 | 0.938 | 0.700 to 0.800 |
| Semantic, threshold 0.76 | 0.875 | 0.900 |
| **Semantic, threshold 0.78 (default)** | **0.844** | **1.000** |

The default buys a small, safe gain. The 0.78 threshold sits 0.001 above the highest
absent-topic query in a set of ten, so it is calibrated on little data; keep an eye
on `history list` and relabel (`history label`) rather than trusting the figure.

## What it does not do

- It does not rerank or replace lexical results; it adds to them.
- It does not make a wrong answer right: a near match found by meaning is a passage
  to read, not a fact.
- It does not run in the hook's fallback path, only when the daemon is up.
