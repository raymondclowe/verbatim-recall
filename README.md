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

Since `0.3.0` it also looks **outside the current session**: if another session on the same
agent holds the answer, you get a louder, explicitly low-confidence pointer naming the
*other* session's id (see [Cross-session recall](#cross-session-recall-on-by-default)).

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

## Cross-session recall (on by default)

A compaction summary is per-session, but a real agent runs many sessions: one per Telegram
topic, one per API client, one per scheduled job. A fact learned in November's chat is
usually the answer to December's question, and the current session's own region knows
nothing about it. So `0.3.0` also queries **every other session on the same agent** and
emits a pointer when one of them matches the current turn — whether or not the local
region matched too:

```
[verbatim-recall] LOW CONFIDENCE — ANOTHER SESSION may hold this: 20261109_093808_ca1f280e
"Salvage RAM and hard drive from old HP ProBook", 09 Nov, ids 18902-18907,
matched 3 of 3 query words (probook, rebuilt, ubuntu)
— opens: "# Plan: wipe probook → Ubuntu Server 24.04, hostname PR1 …"
-> session_search(session_id='20261109_093808_ca1f280e', around_message_id=18902, window=20)
```

Four properties make that pointer trustworthy enough to inject:

| Property | Why |
|---|---|
| **The pointer carries the owner's session id**, not yours | the reader needs the foreign id as its handle, or the span is unreachable |
| **Always `LOW CONFIDENCE`, and it needs 3 agreeing words** (not 2) | a foreign turn was written days apart, in different words, by a different prompt; equal evidence would be over-trust |
| **Scored on the pool's own term statistics, gated relative to the pool's best** | a global IDF over the whole store would rank the loudest session, not the most relevant one |
| **It shows the opening of the span** (`— opens: "…"`) | one line lets the model judge relevance without spending a read |

Cost: one scoped FTS query plus a re-score, measured at **5–40 ms per turn** on a 20k-message
store, **0 bytes injected on a miss**, and a cooldown (`cross_session.cooldown`, 6 turns per
foreign session) so the same old session cannot be re-advertised every turn.

### Who is in scope

Cross-session recall widens the trust boundary: content from *other* conversations enters the
prompt. Only sessions whose `source` is in `cross_session.sources` are eligible
(default: `telegram, api_server, cli, desktop, mobile, a2a` — automation sources like `cron`
and `oneshot` are excluded, because scheduled jobs are noisy and rarely worth quoting).

On top of that, **identity scoping is on by default** (`cross_session.same_user_only: true`): a
foreign row is eligible only when its `user_id` equals the asking session's, or was never
recorded, **or** its source is a trusted peer (`cross_session.trusted_sources`, default `a2a` —
agent-to-agent rows belong to the same operator's other machines, each with its own
`user_id`, so identity scoping would otherwise hide the one source you explicitly wanted),
**or** its principal is one of `cross_session.trusted_principals` (default `api` — the local
API surface is this operator by definition; without it, a Telegram session never sees the
agent's own API sessions, which is the whole point of the feature).

Set `cross_session.same_user_only: false` if your store is single-operator and you want
unrestricted recall. Leave it at `true` on any deployment where two people talk to the same
agent: it is the only thing stopping one person's chat being quoted into another person's
prompt.

---

## Install

Single file, stdlib only, Python 3.9+. Pick the row that matches your host.

### Hermes Agent (native plugin)

```bash
mkdir -p "$HERMES_HOME/plugins/verbatim-recall"      # Windows: %LOCALAPPDATA%\hermes\plugins\...
cd "$HERMES_HOME/plugins/verbatim-recall"
BASE=https://raw.githubusercontent.com/raymondclowe/verbatim-recall/main
curl -fsSL "$BASE/verbatim_recall.py" -o __init__.py   # loader imports __init__.py, not the repo name
curl -fsSL "$BASE/plugin.yaml"        -o plugin.yaml    # manifest: name/version/description
hermes plugins enable verbatim-recall
# the module is imported at gateway start — restart the gateway, or `hermes plugins enable`
# has already told you it is a no-op on an already-enabled plugin
```

`register(ctx)` wires `pre_llm_call` → `recall` and `post_compaction` → cache drop. The
default source is the `db_path` setting (unset → `<hermes home>/state.db`), reading
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
python verbatim_recall.py cross   <session_id> "probook rebuilt ubuntu"   # foreign sessions
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

Cross-session recall (`cross_session.enabled: true`, the default) needs two more things, and simply
switches itself off when they are absent:

1. an **FTS5 index** over the message bodies with `rowid` = message id (Hermes' `state.db`
   ships `messages_fts` for free) — it is only a *candidate generator*; the scoring is done
   in Python on the returned pool, so a small or crude index still works;
2. a **`sessions` table** with `id`, `source`, and optionally `user_id`, `chat_id`, `title`,
   `last_activity_at` — used for scope, for the date, for the human title in the pointer, and
   as the reader's handle. On stores without it, pass your own candidate callable.

If you bring your own source callable, cross-session recall is off unless you also pass
`cross_db=<path to the store>` — the plugin will not guess a database it was not given.

---

## Tuning

Every knob is a **YAML item** in this repo's `plugin.yaml` (`config_schema:`), and its value
is written to `config.yaml` under `plugins.entries.verbatim-recall.settings`. A dotted key
becomes a nested mapping, and nothing reads the environment — so the settings file is the
single source of truth:

```yaml
plugins:
  entries:
    verbatim-recall:
      settings:
        min_score: 1.8
        cross_session:
          cooldown: 12
          sources: [telegram, a2a]
```

Check the effective values with `hermes plugins settings verbatim-recall` (or the settings
UI, which renders the same schema with labels and descriptions). Every key below is optional:
omit it and the built-in default applies, so an empty settings block behaves exactly like
the tuned defaults. Imported outside Hermes (tests, CLI), `configure({...})` accepts the same
flat keys.

| Setting | Default | Meaning |
|---|---|---|
| `db_path` | `<hermes home>/state.db` | Hermes state DB path |
| `top_k` | `2` | strict pointers per turn (capped 4) |
| `min_score` | `1.6` | absolute floor for a strict hit |
| `coverage` | `0.45` | share of the query's IDF mass the turn must carry |
| `min_terms` | `2` | agreeing terms needed (single term must be near-unique) |
| `loose_score` | `0.9` | floor for the low-confidence tier |
| `loose_coverage` | `0.20` | coverage floor for the low-confidence tier |
| `loose_top_k` | `1` | max low-confidence pointers per turn (capped 2) |
| `common_weight` | `0.3` | weight of non-rare terms in the loose tier |
| `rare_df` | `0` = `max(8, 5% of turns)` | df at or below which a term counts as rare |
| `boiler_df` | `0` = `max(12, 10% of turns)` | terms above this df are dropped as boilerplate |
| `span` | `24` | min id distance between two pointers (no near-duplicates) |
| `preview` | `90` | chars of the opening ask quoted in a pointer |
| `window` | `20` | suggested scroll radius handed to the reader |
| `announce` | `true` | emit the ids-range line once per region growth |
| `quiet` | `false` | `true` = index only, never inject |
| `cross_session.enabled` | `true` | also point at other sessions on the same agent |
| `cross_session.sources` | `[telegram, api_server, cli, desktop, mobile, a2a]` | session sources eligible for cross-session recall |
| `cross_session.same_user_only` | `true` | only recall sessions belonging to the same principal |
| `cross_session.trusted_principals` | `[api]` | extra principals treated as this operator (the local API surface) |
| `cross_session.trusted_sources` | `[a2a]` | sources exempt from the identity check (peers have their own ids) |
| `cross_session.top_k` | `2` | foreign pointers per turn |
| `cross_session.min_terms` | `3` | agreeing words required — one more than the local tier |
| `cross_session.share` | `0.55` | matched IDF mass as a share of the pool's best turn |
| `cross_session.pool` | `400` | candidate turns pulled from the FTS index before re-scoring |
| `cross_session.max_terms` | `12` | query words used for the foreign fingerprint match |
| `cross_session.cooldown` | `6` | turns before the same foreign session may be pointed at again |

**Too many weak pointers?** Raise `coverage` to 0.55, then `min_score`.
Widen `boiler_df` if your region is small. **Missing things?** Lower
`loose_score`/`loose_coverage` first — they only affect the labelled
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
5. **The crossing points** — ask a session about work done in another session and confirm the
   pointer names the *other* session's id, and that `session_search(session_id='<that id>',
   around_message_id=<lo>)` returns the turn the pointer promised. Then confirm a second
   session belonging to a different `user_id` is **not** quoted (`cross_hits` in
   `tests/test_cross.py` covers both, with a synthetic stranger session as the control).

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
* cross-session recall is a **lexical prefilter over an FTS index**, not a semantic search: it
  finds sessions that share your words, which is not the same as sessions that answer your
  question. It also cannot tell that two sessions are about the same *project* when the words
  differ, and a foreign pointer costs ~200 extra tokens whenever it fires alongside a local one.
* the cheapstem is a 6-character prefix, not a stemmer: `session`/`sessions` match (intended),
  `server`/`serves` also match (harmless in practice, wrong in principle).
* a foreign pointer is a **dangling pointer risk**: if the owning session is pruned or deleted,
  the id no longer resolves. The label names the owner so a failed read is diagnosable.
* identity scoping compares `user_id` strings; a store that stamps a shared placeholder
  (`api` for every API client, for instance) cannot separate two humans by itself — set
  `cross_session.same_user_only: false` deliberately, or fix the stamps upstream.
* the archive's coherence is the framework's job; if rows are missing, pointers are missing.
* no reranking of the matched span's content: the top-1 id is chosen by the gate, not by a
  second model pass.

## License

MIT. See `LICENSE`.
