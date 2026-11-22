# Cross-session recall — measured design notes

How to make any session surface verbatim turns from another session on the same agent, without a second store. All
numbers below were measured on this host's `state.db` with read-only probes (`file:state.db?mode=ro`).

## What Hermes already gives you: a live FTS5 index

- `messages_fts` (FTS5) and `messages_fts_trigram` are kept in sync by six triggers on `messages`
  (`messages_fts_insert/delete/update` + trigram equivalents) — no lag, no backfill needed.
- `messages_fts_src` is a **VIEW**, not a table: `select id, case when role='tool' then substr(content,1,8192) else
  content end, tool_name, tool_calls from messages`. Consequences:
  - it has **no `rowid`** — `select max(rowid) from messages_fts_src` fails with `no such column: rowid`; join
    `messages.id = messages_fts.rowid` instead.
  - tool output is truncated at 8 KB in the index, so FTS hits inside long tool output are partial by design.
- Cost with role + scope filters on ~20 K messages: candidate count ~4.5 ms, `order by bm25(...) limit 5` ~14 ms for a
  3-term OR query, ~370 candidate rows. That is cheap enough to run inside a per-turn hook; a hand-rolled lexical
  index over the same rows costs seconds.
- `sqlite3` shipped with CPython normally has FTS5, but probe `sqlite_master` for `messages`, `sessions`,
  `messages_fts` and fall back to single-session behaviour when any is missing — never assume.

## Candidate query (scoped, role-filtered)

```sql
select m.id, m.session_id, m.role, coalesce(m.content,''),
       s.title, coalesce(s.display_name,''), coalesce(s.last_activity_at, s.started_at)
  from messages_fts f
  join messages m  on m.id = f.rowid
  join sessions s  on s.id = m.session_id
 where f.messages_fts match :fts_query
   and m.role in ('user','assistant')
   and m.session_id <> :current_session
   and s.source in ('telegram','api_server','cli','desktop','mobile','a2a')
   and coalesce(m.content,'') not like '[CONTEXT COMPACTION%'
 order by bm25(messages_fts)
 limit :pool_cap
```

Sanitise the MATCH string before it reaches SQLite: keep `[A-Za-z0-9_]{3,}` tokens, drop `AND/OR/NOT/NEAR` and the
module's boiler terms, quote each term with doubled inner quotes, join with ` OR `, cap at ~12 terms. User text is never
safe to splice into FTS syntax, and a syntax error inside a hook must fail open (return nothing), not raise.

## Session scoping facts

| source | sessions |
|---|---|
| cron | 2,445 |
| a2a | 107 |
| telegram | 53 |
| api_server | 44 |
| cli | 19 |
| oneshot | 17 |
| desktop / mobile | 1 each |

- 118 sessions survive the scope filter; the rest is automation chatter and tracebacks, not decisions.
- `thread_id` is `NULL`/empty on all 53 telegram sessions here — "different DM topic" is *not* a distinct `thread_id` on
  this host. Scope on `source` + `user_id`/`chat_id` and treat the session id itself as the topic split.
- `compacted=1` exists in exactly one session (the live one). Cross-session sources must use ordinary rows.

## Gate shape as built (0.3.0), and why

1. **current-session strict** — bare pointer, treated as a real lead.
2. **current-session loose** — `LOW CONFIDENCE — only N of M query words appear here`.
3. **cross-session, runs whether or not 1 and 2 fired** — a foreign pointer costs ~200 tokens and can be the only thing
   that answers the question, so it is not gated behind local silence.
   - FTS5 bm25 is only a **candidate generator** (`CROSS_POOL` 400 rows). Scoring happens in Python over the pool's own
     term statistics, so the gate does not need the index to be authoritative and does not fire df queries per turn.
   - Agreement is tested on a **6-character cheapstem key** (`_key(t) = t.lower()[:6]`) on both sides, and the FTS query
     uses `key*` prefix wildcards to widen the pool. Cheapstem collisions (`server`/`serves`) are harmless, an added
     dictionary is not worth it.
   - Threshold: at least `CROSS_MIN_TERMS` 3 agreeing keys **and** `CROSS_SHARE` 0.55 of the best-scoring candidate in
     the pool. A coverage fraction of the *query* was tried and masked every hit, because the pool's vocabulary is much
     richer than one region's.
   - Collapse per owning session, keep the best row, expand the span, cap 2 sessions per turn, `CROSS_COOLDOWN` 6 turns
     per foreign session so a long conversation is not re-pointed at the same old session every turn.
   - Label it loudly, with the owner's title and date, so the agent knows the evidence is second-hand. The label doubles
     as the instruction, because the id in it is not the caller's:

```
[verbatim-recall] LOW CONFIDENCE — ANOTHER SESSION may hold this: 20261109_093808_ca1f280e
"Salvage RAM and hard drive from old HP ProBook", 09 Nov, ids 18902-18907,
matched 3 of 3 query words (probook, rebuilt, ubuntu) — opens: "# Plan: wipe probook → Ubuntu Server 24.04…"
-> session_search(session_id='20261109_093808_ca1f280e', around_message_id=18902, window=20)
```

- Never call a foreign row "summarized away": that wording is only true for the caller's compacted region.
- Fail open everywhere: `mode=ro`, `timeout=0.4`, any missing table/FTS/`user_id` lookup returns nothing rather than
  raising inside a hook.

## Identity scoping

A foreign hit must belong to the same principal as the asking session, or to a row with no recorded principal, or to a
`CROSS_TRUSTED` source (`a2a`: each peer is the same operator's other machine with its own id). Otherwise one person's
chat on a shared store becomes quotable into another's prompt.

The escape hatches are the weak points, and they are structural, not fixable in the query: `api_server` stamps the
literal `api` for every API client (and NULL for others), so a Telegram operator cannot reach their own API sessions
while another human's API client would be admitted if it stamped `api` too. Treat it as *a* boundary, describe it as
one, and do not claim shared-store multi-user isolation.

## Decided

- **On by default** when the FTS index and `sessions` table exist; silently off when they do not.
- **`a2a` is a recall source** (scope excludes only `cron`/`oneshot`) and is exempt from the identity filter.
- **A foreign pointer accompanies a local hit** rather than waiting for silence.
- **Rejected:** embeddings; a plugin-side global lexical index; "just let the agent call `session_search`"; scoping by
  `chat_id`/`thread_id` alone.

## Verified

`session_search(session_id='<other session>', around_message_id=<id>, window=1)` returns that session's own turns and the
correct `session_meta` (title, date, model). Reading a foreign span already works; only *finding* it and *naming its
owner* were missing.

## Alternatives rejected

- **Global lexical index in the plugin** — seconds per turn, or a background store that drifts from `state.db`.
- **Embeddings (a LAN host serves `nomic-embed-text`)** — better on synonyms, but needs re-embedding every new message,
  a vector store, an HTTP hop in every turn's critical path, and one more host that must be up. Legitimate opt-in
  reranker later, not the primary path.
- **Just let the agent call `session_search`** — it is cross-session already, but it depends on the agent realising it
  should search, which is exactly the failure being fixed. The hook is the always-called probe.
- **`compacted=1` across sessions** — finds nothing (see above).

## Still open

- Whether cooldown should be per-session-family rather than per-session-id.
- Whether `api_server` should stamp the operator id instead of the literal `api`; until it does, same-user
  scoping cannot reach API sessions from Telegram, and the honest workaround is
  `cross_session.same_user_only: false` on a single-operator store, documented rather than silent.
- Content preview in a foreign pointer: decided yes — it already prints `— opens: "..."`.
