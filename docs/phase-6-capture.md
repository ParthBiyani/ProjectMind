# Phase 6 — browser and AI chat capture

**Status: designed, deliberately unbuilt.**

This document exists so the decision not to build it is on the record, with the
conditions under which that changes.

## What it would do

Read browser history and AI chat logs, and extract from them into the
**profile** layer only — never the episodic one.

The signal is real and nothing else produces it. "Searched for Temporal, read
three docs pages, never used it" is a fact about how someone works that no
repository contains. Neither does "asked six questions about Rust ownership
across two months". Those are exactly the kind of statement the profile layer is
for, and the only source is the noisy one.

## Why it is last, and why it is still unbuilt

The PRD sequenced it last for three reasons, all of which held up:

1. **Lowest signal per item.** A commit is an act; a page visit is a maybe.
   Thousands of history entries might yield a handful of statements worth
   keeping, and the extractor has to be far more conservative than the git one
   — which already discards most of what it sees.
2. **Highest privacy cost.** Browser history is the most sensitive corpus on a
   developer's machine and the one with the least to do with work. Banking,
   health, everything. An exclusion list is not sufficient protection for a
   source where the default is "read everything".
3. **It was never the bottleneck.** After Phase 4 the limiting metric was
   precision, not coverage. Adding the noisiest available source to a system
   whose measured weakness is false injection would make the one number that
   already misses its target worse.

## The condition for building it

The PRD's exit criterion is the right one and it has not changed:

> Measurable improvement on the Phase 0 eval. If none, cut the phase and say so.

Concretely, before any of this is written:

- the eval set needs queries that only browser or chat history could answer,
  which the current 40 do not contain;
- `false_injection_rate` needs to be at or under its 0.10 target from the
  existing sources, so that a regression is attributable;
- extraction has to be demonstrated conservative enough that a hundred history
  entries produce at most one or two statements.

## How it would be built

Recorded so the design is not lost.

**Sources.** Browser history is SQLite on every major browser (`places.sqlite`,
`History`), read from a copy since the live file is locked. AI chat logs are
per-client: Claude Code keeps JSONL transcripts under its config directory.

**Extraction to profile only.** The unit is not a page but a *pattern across
pages*: repeated visits to one tool's documentation followed by no appearance
of that tool in any repository is an `abandoned` statement. Repeated visits
followed by a dependency appearing is a `tool_preference`. A single visit is
nothing.

**Privacy, which would have to come first.**

- An allow-list of domains, not a deny-list. The default is to read nothing.
- A time window, defaulting to working hours.
- A dry-run mode that prints every candidate statement and writes none, which
  would be the only supported mode until the extractor has been read end to end.
- Raw history never persisted. Only derived statements, each with a count and a
  date range rather than a list of URLs.

**Where it plugs in.** As another source behind `IngestionPipeline`, writing
proposals through `ProfileRepository` so that everything lands as `proposed`
and reaches the same monthly review as any other proposal. No new path to
active, and no new storage.

## If it is cut

Then this file becomes the record of why, and the roadmap says five phases
rather than six. That is a perfectly good outcome; a phase that cannot show it
helps is a phase that should not ship.
