---
name: verbatim-recall
description: Use when a compacted session loses detail. Recover verbatim turns by id.
---

# verbatim-recall

The compaction summary in your context is a **pointer index, not a replacement** for the
turns it replaced. When the user refers to something you can no longer see verbatim, or
when you are about to redo work you may already have done, the verbatim text is usually
still in the store and readable by id.

## What fires

A `pre_llm_call` hook indexes the compacted region of this session and injects 1–3 lines:

* `[verbatim-recall] N turns ... ids A-B ...` — once per region growth: the region exists
  and spans those ids. It is **not** a match.
* `[verbatim-recall] ids X-Y, summarized away, matched: t1, t2 ... -> session_search(...)` —
  a **lead**. Read that span before answering.
* `[verbatim-recall] LOW CONFIDENCE — only 2 of 4 query words appear here (...) so this may
  be an unrelated turn` — **check, do not trust**. Cheap to dismiss, and silence would have
  been worse: a pointer missed is lost, a wrong one costs one look.

## What to do with a pointer

1. Read the span: `session_search(session_id=..., around_message_id=<the id>, window=20)`.
2. Quote from the returned bytes, not from the summary.
3. If the span does not answer the question, say so and move on — do not "repair" the
   pointer by guessing.

## What never to conclude

* A pointer is not proof you already did the work; read it.
* No pointer does **not** mean it never happened — the matcher is lexical, so an unusual
  phrasing yields nothing. Try again with words you would have used at the time ("probook",
  "kraken"), or reach for the raw store.
* Never rebuild a fact from a summary when the verbatim id is available.

## Debugging it (offline, no gateway needed)

```bash
cd <hermes home>/plugins/verbatim-recall
python __init__.py sessions                       # which regions exist, sizes, id ranges
python __init__.py index   <session_id>           # kept terms, rare/boilerplate cut-offs
python __init__.py query   <session_id> "text"    # what the model would have been shown
python __init__.py explain <session_id> "text"    # per-word df, tier, score, coverage, picked id
```

`explain` is the important one: `df=0 absent` proves a word is not in the region (that is
how you choose a valid negative control), `common` means it was down-weighted, and
`picked 0` on a query that should have matched means the coverage bar is too high for this
region's vocabulary.

## Two traps

* **Editing `__init__.py` is not deploying it.** The gateway imports the module at start;
  `plugins enable` on an already-enabled plugin is a no-op. Restart before claiming it is live.
* **A negative control dies as soon as a test writes it into the archive.** `milk` and
  `haiku` were clean negatives until a probe run put them in the session. Use nonsense
  (`xylophone quasar zeppelin`) or verify absence with `explain`.

Full mechanism, knobs and the four-level proof protocol: the repository README (`README.md`)
and its agent adoption protocol (`AGENTS.md`).
