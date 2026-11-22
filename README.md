# verbatim-recall

**Your agent's compaction summary is a tombstone. This turns it back into a pointer.**

When a long session gets compressed, most agent frameworks keep a prose summary in
context and drop the verbatim turns. The detail you actually need — the exact command,
the file path, the error text, the user's own words — survives only as a paraphrase.

But the verbatim turns are almost always *still on disk*, marked as compacted, with
stable ids. Nothing is lost except the handle to them. `verbatim-recall` manufactures
that handle: before each model call it injects one or two lines naming the exact id
span of the compacted region that overlaps the incoming message, so the agent can
re-read it byte-for-byte instead of guessing from the summary.

```
[verbatim-recall] 1164 turns of this session are summarized away but kept verbatim in
the store (ids 19235-21591) — any span is readable with
session_search(session_id='…', around_message_id=19235, window=20).
[verbatim-recall] ids 20482-20487, summarized away, matched: probook, rebuilt, ubuntu
— in the turn answering: "did you help before with the probook rebuild to ubuntu?"
-> session_search(session_id='…', around_message_id=20480, window=20)
```

Cost: ~5 ms per turn, **0 bytes on a miss**, ~120–500 tokens when it fires.

---

## Why not a second memory store

It adds one. It reads the store you already have:

* no embeddings, no model calls, **no writes**
* no second copy of the transcript, so nothing to drift out of sync
* ephemeral index: rebuilt in memory when the compacted region changes, dropped on compaction
* every path fails open — a broken source, an empty region, a weird message returns `None`

The only thing it asks of your host is: *do compacted turns survive somewhere readable
by id?* If yes, they become reclaimable.

---

## The gate (why it doesn't fire on everything)

A naive matcher over a session archive matches everything, because everything shares
vocabulary. Four rules do the work:

| Rule | Reason |
|---|---|
| **Derived text is not indexed** | summaries and process notices quote anything, so they match anything |
| **Boilerplate is not evidence** | a term carried by >10% of the region (`output`, `error`, `exit_code`) is dropped |
| **Only rare terms score in the strict tier** | one common word fakes a hit |
| **Coverage decides, not score** | the matched turn must carry ≥45% of the *query's* IDF mass |

And then the rule that matters most in practice:

> **A pointer that is missed is lost forever; a pointer that is wrong costs one dismissal.**
> So when the strict tier finds nothing, a **loose tier** surfaces partial overlap anyway,
> explicitly labelled:

```
[verbatim-recall] LOW CONFIDENCE — only 2 of 4 query words appear here (context, model),
so this may be an unrelated turn: ids 20482-20487 — in the turn answering: "…"
-> session_search(…)
```

Treat that as a hint to check, not an answer. It still needs two agreeing words (or one
word rare enough to be an identifier), and only one low-confidence pointer is allowed
per turn, so it cannot flood the window.

---

## Install

Single file, stdlib only, Python 3.9+. Pick the row that matches your host.

### Hermes Agent (native plugin)

```bash
mkdir -p "$HERMES_HOME/plugins/verbatim-recall"      # Windows: %LOCALAPPDATA%\hermes\plugins\...
curl -fsSL https://raw.githubusercontent.com/raymondclowe/verbatim-recall/main/verbatim_recall.py \
  -o "$HERMES_HOME/plugins/verbatim-recall/__init__.py"
hermes plugins enable verbatim-recall
# the module is imported at gateway start — restart the gateway, or `hermes plugins enable`
# has already told you it is a no-op on an already-enabled plugin
```

`register(ctx)` wires `pre_llm_call` → `recall` and `post_compaction` → cache drop. The
default source is `$VERBATIM_DB` or `<hermes home>/state.db`, reading
`messages(id, role, content, session_id)` where `compacted=1`.

### Any other framework

Drop `verbatim_recall.py` anywhere importable and call it where you assemble the prompt:

```python
import verbatim_recall as vr

# choose a source: a callable(session_id) -> rows with id / role / content
src = vr.jsonl_source("transcripts.jsonl")          # or vr.sqlite_source("sessions.db")
# src = lambda sid: my_store.get_compacted(sid)     # or your own

block = vr.recall(session_id=sid, user_message=user_text, source=src)
if block:
    messages.append({"role": "system", "content": block["context"]})
```

The model must know how to read a span back, so pass the tool it should call:

```python
vr.recall(..., reader_hint="read_messages(session='{session_id}', around={message_id})")
```

For LangGraph, LangChain, OpenAI Agents, CrewAI, AutoGen, mem0 or a hand-rolled loop, the
hook point is the same: **just before the model call, after compaction has run.**

### CLI (no integration at all)

```bash
python verbatim_recall.py sessions                      # which regions exist
python verbatim_recall.py index   <session_id>          # what the index kept
python verbatim_recall.py query   <session_id> "did the probook rebuild work"
python verbatim_recall.py explain <session_id> "op3 local model context"
```

`explain` prints per-word document frequency (`df=0 absent` = the word is not in the
region at all), the tier, the score, the coverage and the picked id — this is the tool
for debugging a miss *and* for choosing negative controls that are actually negative.

---

## Requirements on your store

`recall()` needs rows that carry:

1. an integer **id**, stable and ordered
2. the **role**
3. the **content**
4. a marker for *compacted* (summarized away) vs live tail — feed only the compacted part,
   or the index will point at things already in the window

If your framework **discards** verbatim turns at compaction instead of marking them,
there is nothing to reclaim: fix retention first (keep rows, set a `compacted` flag, or
append to a JSONL archive). This tool is deliberately not a transcript writer.

---

## Tuning

Everything is an environment variable; the defaults were tuned on a real 1,164-turn
compacted region and are meant to be a starting point, not a law.

| Var | Default | Meaning |
|---|---|---|
| `VERBATIM_DB` | `<hermes home>/state.db` | Hermes state DB path |
| `VERBATIM_TOP_K` | `2` | strict pointers per turn (capped 4) |
| `VERBATIM_MIN_SCORE` | `1.6` | absolute floor for a strict hit |
| `VERBATIM_COVERAGE` | `0.45` | share of the query's IDF mass the turn must carry |
| `VERBATIM_MIN_TERMS` | `2` | agreeing terms needed (single term must be near-unique) |
| `VERBATIM_LOOSE_SCORE` | `0.9` | floor for the low-confidence tier |
| `VERBATIM_LOOSE_COVERAGE` | `0.20` | coverage floor for the low-confidence tier |
| `VERBATIM_LOOSE_TOP_K` | `1` | max low-confidence pointers per turn (capped 2) |
| `VERBATIM_COMMON_WEIGHT` | `0.3` | weight of non-rare terms in the loose tier |
| `VERBATIM_RARE_DF` | `max(8, 5% of turns)` | df at or below which a term counts as rare |
| `VERBATIM_BOILER_DF` | `max(12, 10% of turns)` | terms above this df are dropped as boilerplate |
| `VERBATIM_SPAN` | `24` | min id distance between two pointers (no near-duplicates) |
| `VERBATIM_PREVIEW` | `90` | chars of the opening ask quoted in a pointer |
| `VERBATIM_WINDOW` | `20` | suggested scroll radius handed to the reader |
| `VERBATIM_ANNOUNCE` | `1` | emit the ids-range line once per region growth |
| `VERBATIM_QUIET` | `0` | `1` = index only, never inject |

**Too many weak pointers?** Raise `VERBATIM_COVERAGE` to 0.55, then `VERBATIM_MIN_SCORE`.
Widen `VERBATIM_BOILER_DF` if your region is small. **Missing things?** Lower
`VERBATIM_LOOSE_SCORE`/`VERBATIM_LOOSE_COVERAGE` first — they only affect the labelled
tier, so the trustworthy pointers stay untouched.

---

## Prove it works (do not accept "it imports")

1. **It fires** — call `recall()` with a real session id and query; assert the block is non-empty.
2. **The pointer resolves to the same bytes** — read the named id from the store and diff it
   against what the summary claimed. This is the only step that proves the pointer is *true*.
3. **Relevance** — a labelled query set sizes the top-1 hit, with negative controls made of
   words that are genuinely absent (`explain` shows `df=0`). Nonsense words only: any real
   word you use in a test enters the archive and stops being a control.
4. **Silence is the default** — non-matching queries inject 0 bytes.

```bash
python -m unittest discover -s tests -v      # the gate itself, no DB needed
```

Then, for a live host: confirm the *running* process has the new code. Editing the file is
not deploying it — plugin modules are imported once at start.

---

## Limits, honestly

* lexical (token + IDF) matching — no synonyms, no paraphrase, no cross-language recall.
  A query phrased in words absent from the region yields nothing; that is what the loose
  tier and, ultimately, a better query are for.
* English-centric stopword list; other languages mostly work by falling through to rare terms.
* one session at a time — no cross-session search (feed a session id, get that region).
* the archive's coherence is the framework's job; if rows are missing, pointers are missing.
* no reranking of the matched span's content: the top-1 id is chosen by the gate, not by a
  second model pass.

## License

MIT. See `LICENSE`.
