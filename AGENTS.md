# AGENTS.md — adoption protocol for an AI agent

You have been given the URL of this repository and told something like *"do this"*.
Follow the protocol below. It is written so that partial adoption is a valid outcome:
use whatever fits the host you are on, skip the rest, and say exactly what you skipped
and why. **Never report success you did not observe.**

Read `README.md` once for the mechanism, then work the steps in order.

---

## 0. Rules that override your instincts

1. **Read-only until step 5.** Do not modify the host's config, plugins, databases or
   transcripts before you know where its compacted turns live.
2. **No new transcript store.** This tool consumes an archive that already exists. Do not
   build a parallel memory system around it, do not "improve" the archive's schema, and do
   not add writes. If the host discards verbatim turns at compaction, that is a finding to
   report, not a refactor to perform.
3. **Fails open, always.** Any integration you write must return "nothing" rather than
   raise, block, or slow the model call. Wrap the call; never let it sit in the critical
   path unguarded.
4. **Editing a file is not deploying it.** Plugin/hook modules are imported once at process
   start. Until you have seen the new code execute inside the *running* process (or
   restarted it deliberately, with permission), the change is not live. Say so.
5. **Restarting a gateway/service/host is a shared-infrastructure action.** Ask first unless
   you were explicitly told to restart it.
6. **Never invent evidence.** "It works" is proved by a named id whose bytes you re-read,
   not by an import that returned no error.

---

## 1. Inventory the host (read-only)

Establish, with commands you actually ran:

* **What agent framework is running here?** Look for `hermes` (`hermes plugins list`,
  `$HERMES_HOME` / `%LOCALAPPDATA%\hermes`), a LangGraph checkpointer, mem0, memgpt,
  CrewAI, AutoGen, LlamaIndex, a bespoke loop, or plain scripts.
* **Where is the conversation state?** Find candidate stores: `*.db`, `*.sqlite*`,
  `state.db`, `checkpoints/`, `transcripts/*.jsonl`, `logs/*.jsonl`. Inspect schema with
  read-only queries (`sqlite3 file.db ".schema"`).
* **Does compaction keep the verbatim rows?** The single decisive question. Look for a
  compaction/summary flag column (`compacted`, `is_summary`, `archived`) or an append-only
  transcript file whose entries are never deleted. Grep for the summariser's code path.
* **How does the model prompt get assembled?** Find the hook point: a plugin hook
  (`pre_llm_call`), a callback, a middleware, or one function that builds `messages`.

If any of the last three cannot be established, **stop and report the inventory**. Partial
adoption (CLI-only, step 4b) is a legitimate end state.

## 2. Decide the integration mode

| Host state | Mode |
|---|---|
| Hermes Agent, or any host with a pre-model-call hook | **A — native hook** (best) |
| Prompt assembled in one known function | **B — wrap that function** |
| Framework you cannot hook, or uncertain access | **C — CLI only**: the agent shells out to `query` when it suspects lost context |
| Verbatim turns are discarded at compaction | **D — unsupported**: report the retention gap and the one-line schema change that would enable it |

Prefer the least invasive mode that can actually fire. Do not rewrite the host's memory
system to reach mode A.

## 3. Install the module

Single file, stdlib only, Python 3.9+.

```bash
git clone https://github.com/raymondclowe/verbatim-recall /tmp/verbatim-recall
# or, no git:
curl -fsSL https://raw.githubusercontent.com/raymondclowe/verbatim-recall/main/verbatim_recall.py -o verbatim_recall.py
```

* **Hermes**: copy it to `$HERMES_HOME/plugins/verbatim-recall/__init__.py` (Windows:
  `%LOCALAPPDATA%\hermes\plugins\verbatim-recall\__init__.py`) and run
  `hermes plugins enable verbatim-recall`. Record the pre-change config backup path.
* **Otherwise**: place it on `sys.path` (or vendor it next to the host's code) and import it.
* Verify the file you installed is the file you fetched (`sha256sum`), then run
  `python -m unittest discover -s tests -v` in the clone. **Do not proceed on a red suite.**

## 4. Wire it, then bound it

```python
import verbatim_recall as vr

def build_prompt(session_id, user_text):
    block = vr.recall(session_id=session_id, user_message=user_text, source=SOURCE,
                      reader_hint=READER)          # see below
    ...                                        # append block["context"] as a system/context line
```

* **`SOURCE`** — a callable `(session_id) -> rows` with `id`/`role`/`content`, **compacted
  rows only**. Use `vr.hermes_source()` (default), `vr.sqlite_source(path, table, where)`,
  `vr.jsonl_source(path)`, or write six lines of your own. Verify it returns >0 rows for a
  session that has actually been compacted before claiming it is wired.
* **`READER`** — the hint that tells the model how to read a span back, e.g.
  `"read_messages(session='{session_id}', around={message_id})"` or Hermes's
  `session_search(session_id='{session_id}', around_message_id={message_id}, window={window})`.
  **If the agent cannot fetch a span by id, the pointer is useless** — this is the one
  part of the integration that must not be skipped.
* Bound the cost: cap the block's length (it is 1–3 lines by construction), and keep the
  call inside a `try/except` that returns `None`.
* Drop the index on compaction: register `on_post_compaction` alongside `recall`, or call
  `vr._cache.clear()` from your own compaction callback.

## 4b. Cross-session scope — decide it on purpose

`cross_session.enabled` defaults to `true`: the same `recall()` call also points at **other
sessions on the host**, not just the compacted region of the current one. For an agent that has
been working with this operator for weeks that is usually the difference between "I remember
our conversation" and "I remember everything we have done". It also widens the trust boundary —
content from another chat can now enter this prompt — so decide it deliberately. Every value
below lives in `config.yaml` under `plugins.entries.verbatim-recall.settings` (schema in
`plugin.yaml`); nothing reads the environment.

* **It needs a second index.** Foreign candidates come from an FTS5 table over message
  bodies whose `rowid` is the message id (Hermes ships `messages_fts`), joined to a
  `sessions` table with `id`, `source`, and ideally `user_id`/`title`/`last_activity_at`.
  That index is only a **candidate generator**: bm25 narrows to `cross_session.pool`
  (400) rows and the pool's own term statistics decide the winner. No FTS5 → the stage
  disappears silently, which is the intended degradation.
* **A custom `SOURCE` disables it unless you say where the store is.** Pass
  `cross_db=<path>`; the plugin will not guess a database it was not given.
* **Identity scoping is the safety catch.** `cross_session.same_user_only: true` (default)
  quotes a foreign span only if it belongs to the same `user_id` as the calling session, or
  carries no principal. On a store shared with anyone else, leave it on.
* **…and it over-filters when the host stamps a placeholder principal.** Hermes'
  `api_server` sessions carry the literal `user_id='api'`, so a Telegram caller cannot see
  them. If your host does the same and you are the only operator, the honest fix is to name
  that principal in `cross_session.trusted_principals` (default `[api]`, so the API surface
  is already reachable) **written host-locally with that reason**, never by deleting the
  clause. Only drop to `same_user_only: false` when the store is genuinely single-operator.
* **Peers are exempt on purpose.** `cross_session.trusted_sources` (default `[a2a]`) names
  sources whose `user_id` is a transport detail rather than a person — the same operator's
  other machine. Add sources here only when that is true of them.
* **Excluded sources:** `cron` and `oneshot`, which produce machine chatter that never
  answers a question. Override `cross_session.sources` when your host names channels
  differently.
* **The pointer carries the owner's session id, not the caller's.** If the agent passes its
  own id to the reader it re-reads the wrong turns and concludes the pointer lies. State
  this in the host's own instructions/skill, not only in your report.

## 5. Verify — five levels, each with the command that proved it

1. **Fires.** Call `recall()` with a real session id and a query about it; capture the block.
   `python verbatim_recall.py query <session_id> "<real question>"` is enough at this level.
2. **The pointer is true.** Read the named id from the store and compare bytes against what
   the summary said. Print both. A resolving id with different content means the index and
   the archive disagree — stop and investigate, do not ship.
3. **Relevance and silence.** A few labelled queries: does top-1 land on the turn that really
   answers it? Do nonsense words (`xylophone quasar zeppelin`) inject nothing? Use `explain`
   to confirm a control word has `df=0`. Never use a real word as a negative control — your
   own test text enters the archive and stops being negative.
4. **Live.** Confirm the *running* process emits the block (a real turn, or a deliberate
   restart you were permitted to do). State plainly which of the two it was.
5. **Written where the next agent looks.** Put the adopted mode, store path, knob values and
   the two facts that cost time here (the pointer carries the *owner's* session id; `compacted`
   is per live session, not global) into the host's own instructions or skill, then re-read the
   rendered file. Verification that lives only in a chat message dies at the next compaction —
   that is exactly how the same investigation gets bought twice.

## 6. Tune, then hand back

Tune against the host's own region, in this order: `coverage` (0.45 → 0.55 if
pointers feel weak) → `min_score` → the loose tier knobs last, because they only
affect labelled output. Write the chosen values into `config.yaml` under
`plugins.entries.verbatim-recall.settings` with a comment saying why, so the next agent does
not re-derive them.

## 7. Report template

Answer in this shape; keep it short and specific:

```
Adopted: <mode A/B/C/D> — <one line>
Store: <path> (<rows> compacted rows, ids X-Y, session <id>)
Hook: <where it now runs>
Reader: <the tool call a pointer hands the model>
Proved: 1) fires: <output snippet>  2) bytes: id N == <sha/size>  3) top-1 + silence: <n>/<n>, controls silent  4) live: yes/no — <how>  5) written: <file> sha <12 hex>
Skipped: <what and why>
Knobs changed: <name=value, why>
Risk left: <e.g. gateway restart pending, or loose tier untested in normal use>
```

---

## Anti-patterns (each one has already happened)

* Indexing summaries or process notices — they quote everything and match everything.
* Scoring on common words — `output`, `error`, `exit`, `file` carry no signal.
* Rendering a pointer without the ids **and** the reader call — the model cannot act on it.
* Reporting "enabled" for an edited-but-not-reloaded module.
* Silencing the low-confidence tier to look tidy — a missed pointer is permanent, a wrong
  one costs a single dismissal.
* Using `milk` / `haiku` / any ordinary word as a test negative after a test run wrote it
  into the archive.
* Building a second memory store "so pointers are reliable" — that is a different project.
