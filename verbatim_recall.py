"""verbatim-recall — compaction summaries should be pointers, not tombstones.

Long agent sessions get compressed. The compressor keeps a prose summary in context
and the verbatim turns are dropped from the window — but in most implementations they
are *still on disk*, marked as compacted, with stable ids. Nothing is lost except the
handle to it. This module manufactures that handle.

It indexes the compacted region of the current session and, before each model call,
injects one or two lines naming the exact id span that the incoming message overlaps,
so the agent can re-read that span verbatim instead of trusting (or losing) the summary:

    [verbatim-recall] 1164 turns of this session are summarized away but kept verbatim
    in the store (ids 19235-21591) — any span is readable with
    session_search(session_id='…', around_message_id=<id>, window=20).
    [verbatim-recall] ids 20482-20487, summarized away, matched: probook, rebuilt, ubuntu
    — in the turn answering: "did you help before with the probook rebuild to ubuntu?"
    -> session_search(session_id='…', around_message_id=20482, window=20)

Design rules, each learned from a matcher that fired on everything:

  * derived text is not indexed — summaries and process notices quote anything, so
    they match anything
  * boilerplate is not evidence — a term carried by more than ~10% of the region
    ("output", "error", "exit_code") is dropped from the index
  * only rare terms count in the strict tier — a single common word fakes a hit
  * coverage decides — the matched turn must carry enough of the *query's* IDF mass
  * a miss is worse than an extra hit — when strict finds nothing, a second loose
    tier surfaces partial overlap explicitly labelled LOW CONFIDENCE

No new store, no embeddings, no model calls, no writes: the archive is whatever your
store already has, this is an in-memory index over it, and every path fails open.

Cross-session: the same store also holds every *other* session on the agent, so when a
query matches nothing in this session (or as well as what it finds here) the hook emits a
pointer into a foreign session — carrying that session's own id, because the reader needs
it as the handle. Foreign evidence is weaker than local evidence (the term statistics are
computed over a candidate pool, not the whole store), so a foreign pointer is always
labelled LOW CONFIDENCE and needs more agreement to fire. On a store with no FTS index
the whole stage disappears and behaviour is identical to a single-session install.

Sources ship for Hermes (`state.db`), generic SQLite and JSONL; pass your own callable
to use any other store. See README.md for host setup and AGENTS.md for machine adoption.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import threading
import time

__version__ = "0.3.0"

# ---------------------------------------------------------------- knobs

def _env_f(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


def _env_i(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return int(default)


QUIET = os.environ.get("VERBATIM_QUIET", "0") == "1"
TOP_K = _env_i("VERBATIM_TOP_K", 2)
MIN_SCORE = _env_f("VERBATIM_MIN_SCORE", 1.6)
COVERAGE = _env_f("VERBATIM_COVERAGE", 0.45)
MIN_TERMS = _env_i("VERBATIM_MIN_TERMS", 2)
LOOSE_SCORE = _env_f("VERBATIM_LOOSE_SCORE", 0.9)
LOOSE_COVERAGE = _env_f("VERBATIM_LOOSE_COVERAGE", 0.20)
LOOSE_TOP_K = _env_i("VERBATIM_LOOSE_TOP_K", 1)
COMMON_W = _env_f("VERBATIM_COMMON_WEIGHT", 0.3)
SPAN = _env_i("VERBATIM_SPAN", 24)
ANNOUNCE = os.environ.get("VERBATIM_ANNOUNCE", "1") == "1"
PREVIEW = _env_i("VERBATIM_PREVIEW", 90)      # chars of the opening ask shown in a pointer
WINDOW = _env_i("VERBATIM_WINDOW", 20)        # suggested scroll radius for the reader

# ---- cross-session (on by default; disappears silently when the store has no FTS)
CROSS = os.environ.get("VERBATIM_CROSS", "1") == "1"
CROSS_TOP_K = _env_i("VERBATIM_CROSS_TOP_K", 2)
CROSS_MIN_TERMS = _env_i("VERBATIM_CROSS_MIN_TERMS", 3)     # more agreement than the local loose tier
CROSS_SHARE = _env_f("VERBATIM_CROSS_SHARE", 0.55)          # must score within 55% of the pool's best
CROSS_POOL = _env_i("VERBATIM_CROSS_POOL", 400)             # bm25 candidates re-scored in python
CROSS_MAX_TERMS = _env_i("VERBATIM_CROSS_MAX_TERMS", 12)    # query terms sent to MATCH
CROSS_COOLDOWN = _env_i("VERBATIM_CROSS_COOLDOWN", 6)       # turns before the same foreign session repeats
CROSS_SOURCES = tuple(
    s.strip() for s in (os.environ.get("VERBATIM_CROSS_SOURCES", "") or "").split(",") if s.strip()
) or ("telegram", "api_server", "cli", "desktop", "mobile", "a2a")
# Identity scoping: another person's chat on a shared store must not be quotable.
CROSS_SAME_USER = os.environ.get("VERBATIM_CROSS_SAME_USER", "1") == "1"
# …but a2a rows are the same operator's other machines, each with its own user_id
# (ip:192.168.0.83), so a peer source has to stay eligible regardless of identity.
CROSS_TRUSTED = tuple(
    s.strip() for s in (os.environ.get("VERBATIM_CROSS_TRUSTED", "") or "").split(",") if s.strip()
) or ("a2a",)


_DERIVED = (
    "[CONTEXT COMPACTION",
    "[IMPORTANT: Background process",
    "[STILL IN PROGRESS",
)
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.\-/]{3,}", re.I)
_STOP = frozenset("""
this that these those there their them they then than with without within which while where when what
have has had been being will would should could must might also into from over under about above below
more most less least such same other others some any all each every only just very much many few own
here does done doing made make makes making like want wants need needs know knows think thinks sure
because before after again against between during through your yours you're it's don't doesn't didn't
isn't aren't wasn't weren't can't won't couldn't shouldn't wouldn't i've i'm i'll i'd we've we're they've
read reads reading write writes writing wrote written file files line lines text something anything
thing things going gone goes went back down out off too now yet still even ever never always
decide decided decision say says said talk talks talked discuss discussed remember mean means
check checks checked look looks looked apply applies applied put puts keep keeps kept let lets
get gets got give gives gave take takes took come comes came find finds found try tries tried
call calls called use uses used run runs running ran start starts started stop stops stopped
tell tells told ask asks asked answer answers answered reply replies replied move moves moved
sure right wrong good bad better best worse worst big small long short new old first last next
say seems seem seemed want wants wanted need needs needed able unable possible maybe perhaps
""".split())

_lock = threading.Lock()
_cache: dict = {}          # session_id -> (signature, index)
_announced: dict = {}      # session_id -> highest id already announced
_cross_turn: dict = {}     # session_id -> turn counter, for the foreign-pointer cooldown
_cross_seen: dict = {}     # (session_id, foreign_session_id) -> turn last pointed at
_fts_ok: dict = {}         # db path -> bool (does this store have messages_fts?)


# ---------------------------------------------------------------- sources
#
# A source is a callable ``source(session_id) -> iterable of rows`` where each row
# exposes ``id``, ``role`` and ``content`` (mapping or object). Only *compacted*
# (summarized-away) rows must be returned; the index never wants the live tail.


def _hermes_home() -> str:
    h = os.environ.get("HERMES_HOME")
    if h:
        return h
    la = os.environ.get("LOCALAPPDATA")
    if la and os.path.isdir(os.path.join(la, "hermes")):
        return os.path.join(la, "hermes")
    return os.path.expanduser("~/.hermes")


def hermes_source(db_path: str | None = None):
    """Default source: a Hermes ``state.db`` (``messages`` table, ``compacted=1``)."""
    path = db_path or os.environ.get("VERBATIM_DB") or os.path.join(_hermes_home(), "state.db")

    def source(session_id: str):
        con = sqlite3.connect(path, timeout=3)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(
                "select id, role, content from messages "
                "where session_id=? and compacted=1 order by id",
                (session_id,),
            ).fetchall()
        finally:
            con.close()

    source.path = path
    return source


def sqlite_source(path: str, table: str = "messages", where: str = "1=1"):
    """Any SQLite table with id/role/content columns and a session column."""

    def source(session_id: str):
        con = sqlite3.connect(path, timeout=3)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(
                f"select id, role, content from {table} "
                f"where session_id=? and ({where}) order by id",
                (session_id,),
            ).fetchall()
        finally:
            con.close()

    source.path = path
    return source


def jsonl_source(path: str, session_field: str = "session_id", compacted_field: str = "compacted"):
    """One JSON object per line: {id, role, content, session_id, compacted}."""

    def source(session_id: str):
        rows = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get(session_field) != session_id:
                    continue
                if compacted_field in rec and not rec.get(compacted_field):
                    continue
                rows.append({"id": rec.get("id"), "role": rec.get("role", ""),
                             "content": rec.get("content", "")})
        return rows

    source.path = path
    return source


def _field(row, name, default=""):
    if isinstance(row, dict):
        return row.get(name, default)
    try:
        return row[name]
    except Exception:
        return getattr(row, name, default)


# ---------------------------------------------------------------- tokenising

def tokens(text: str) -> list:
    out = []
    for m in _TOKEN.findall(text or ""):
        t = m.lower().strip(".-/")
        if len(t) >= 4 and t not in _STOP and not t.isdigit():
            out.append(t)
    return out


def _clean(text: str, limit: int = None) -> str:
    limit = PREVIEW if limit is None else limit
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return (t[: limit - 1] + "…") if len(t) > limit else t


def _derived(body: str) -> bool:
    head = (body or "")[:120].lstrip()
    return any(head.startswith(d) for d in _DERIVED)


# ---------------------------------------------------------------- index

def build_index(rows):
    """Index compacted rows: token -> ids, plus a preview per id and the id range."""
    inv: dict = {}
    preview: dict = {}
    n = 0
    last_ask = ""
    first = last = 0
    raw = 0
    for r in rows:
        raw += 1
        body = _field(r, "content") or ""
        mid = _field(r, "id")
        if mid is None:
            continue
        mid = int(mid)
        if first == 0:
            first = mid
        last = mid
        if _derived(body):
            continue
        n += 1
        if _field(r, "role") == "user" and body.strip():
            last_ask = _clean(body)
        preview[mid] = (_field(r, "role"), last_ask)
        for t in set(tokens(body)):
            inv.setdefault(t, set()).add(mid)

    if not n:
        return None

    # boilerplate: a term nearly every substantive message carries is not evidence
    # (measured on a real region: boilerplate sits above ~10% df, topical words at
    #  1-5% — so precision must come from query coverage, not a tight rarity cut)
    boiler_df = _env_i("VERBATIM_BOILER_DF", 0) or max(12, int(n * 0.10))
    rare_df = _env_i("VERBATIM_RARE_DF", 0) or max(8, int(n * 0.05))
    inv = {t: ids for t, ids in inv.items() if len(ids) <= boiler_df}
    idf = {t: math.log(1.0 + n / len(ids)) for t, ids in inv.items()}
    return {
        "inv": inv,
        "idf": idf,
        "rare": {t for t, ids in inv.items() if len(ids) <= rare_df},
        "preview": preview,
        "n": n,
        "lo": first,
        "hi": last,
        "raw": raw,
        "boiler_df": boiler_df,
        "rare_df": rare_df,
    }


def _signature(source, session_id: str):
    try:
        n = 0
        hi = 0
        for r in source(session_id):
            n += 1
            try:
                hi = max(hi, int(_field(r, "id") or 0))
            except Exception:
                pass
        return (n, hi)
    except Exception:
        return None


def index_for(session_id: str, source=None):
    """Cached index; rebuilt only when the compacted region's shape changed."""
    source = source or hermes_source()
    sig = _signature(source, session_id)
    if sig is None:
        return None
    with _lock:
        hit = _cache.get(session_id)
        if hit and hit[0] == sig:
            return hit[1]
    try:
        idx = build_index(source(session_id))
    except Exception:
        return None
    with _lock:
        _cache[session_id] = (sig, idx)
    return idx


def match(idx, user_message: str, top_k: int = None, loose_top_k: int = None):
    """Rank region messages against the query. Returns (id, score, terms, tier, coverage)."""
    top_k = TOP_K if top_k is None else top_k
    loose_top_k = LOOSE_TOP_K if loose_top_k is None else loose_top_k
    words = set(tokens(user_message))
    if not words or idx is None:
        return []
    live = [t for t in words if t in idx["idf"]]
    if not live:
        return []
    qmass = sum(idx["idf"][t] for t in live)
    if qmass <= 0:
        return []

    # ---- strict tier: rare terms only, high coverage
    need = max(MIN_SCORE, COVERAGE * qmass)
    scores: dict = {}
    why: dict = {}
    for t in words & idx["rare"]:
        w = idx["idf"][t]
        for mid in idx["inv"][t]:
            scores[mid] = scores.get(mid, 0.0) + w
            why.setdefault(mid, []).append(t)

    picked = []
    for mid, sc in sorted(scores.items(), key=lambda kv: -kv[1]):
        if sc < need:
            break
        terms = why[mid]
        # two agreeing terms, or one term so rare it is effectively an identifier
        if len(terms) < MIN_TERMS and len(idx["inv"][terms[0]]) > 2:
            continue
        if any(abs(mid - p[0]) < SPAN for p in picked):
            continue
        picked.append((mid, sc, terms, "strict", sc / qmass))
        if len(picked) >= min(top_k, 4):
            break
    if picked:
        return picked

    # ---- loose tier: partial overlap is still a lead, labelled low confidence
    need_loose = max(LOOSE_SCORE, LOOSE_COVERAGE * qmass)
    lsc: dict = {}
    lwhy: dict = {}
    for t in live:
        w = idx["idf"][t] * (1.0 if t in idx["rare"] else COMMON_W)
        for mid in idx["inv"][t]:
            lsc[mid] = lsc.get(mid, 0.0) + w
            lwhy.setdefault(mid, []).append(t)

    out = []
    for mid, sc in sorted(lsc.items(), key=lambda kv: -kv[1]):
        if sc < need_loose:
            break
        terms = lwhy[mid]
        rare_hit = [t for t in terms if t in idx["rare"]]
        # a single word is enough only when it is near-identifier rare
        if len(terms) < 2 and not (rare_hit and len(idx["inv"][rare_hit[0]]) <= 5):
            continue
        if any(abs(mid - p[0]) < SPAN for p in out):
            continue
        out.append((mid, sc, terms, "loose", sc / qmass))
        if len(out) >= max(1, min(loose_top_k, 2)):
            break
    return out


# ---------------------------------------------------------------- cross-session
#
# Every other session on the agent lives in the same store, so a question asked here
# can often be answered by a turn recorded there. FTS5 is already indexed by the host
# (measured: 5 ms-15 ms for a scoped bm25 query over ~20k messages), so it is used as a
# candidate generator only — the same idea as the local tier, one stage later: bm25
# narrows thousands of rows to a pool, the pool's own term statistics then decide.
#
# Two rules make foreign evidence trustworthy enough to emit:
#   * the pointer carries its OWNER session id — the reader needs it as the handle
#   * it is always labelled LOW CONFIDENCE and needs more agreement than a local hit,
#     because df is measured inside the pool, where rarity is not provable


def _connect_ro(path: str):
    """Read-only handle; never writes, short timeout, fails open."""
    p = os.path.abspath(path).replace("\\", "/")
    con = sqlite3.connect("file:" + p + "?mode=ro", uri=True, timeout=0.4)
    con.row_factory = sqlite3.Row
    return con


def fts_capable(db_path: str) -> bool:
    """True when this store has the three tables cross-session needs."""
    if not db_path or not os.path.exists(db_path):
        return False
    key = (db_path, os.path.getmtime(db_path))
    hit = _fts_ok.get(key)
    if hit is not None:
        return hit
    ok = False
    try:
        con = _connect_ro(db_path)
        try:
            names = {r[0] for r in con.execute(
                "select name from sqlite_master where type in ('table','view') "
                "and name in ('messages_fts','messages','sessions')")}
        finally:
            con.close()
        ok = names == {"messages_fts", "messages", "sessions"}
    except Exception:
        ok = False
    if len(_fts_ok) > 32:
        _fts_ok.clear()
    _fts_ok[key] = ok
    return ok


def _key(term: str) -> str:
    """A cheapstem: two words share a key when their first 6 chars agree.

    Foreign turns are written days apart by a different phrasing, so exact-token
    agreement under-counts ("rebuild" vs "rebuilt", "session" vs "sessions"). Six
    characters keeps real stems together and keeps different words apart — no
    dictionary, no dependency, and it is only ever used to decide agreement.
    """
    t = term.lower()
    return t[:6] if len(t) > 6 else t


def fts_query(user_message: str, max_terms: int = None) -> str:
    """Build a safe FTS5 OR-query from the query's substantive terms.

    Long terms become prefix wildcards on the cheapstem, so "rebuild" also finds
    "rebuilt" — the pool is only a candidate generator, the scorer decides.
    """
    max_terms = CROSS_MAX_TERMS if max_terms is None else max_terms
    terms = list(dict.fromkeys(tokens(user_message)))[:max(1, max_terms)]
    if len(terms) < CROSS_MIN_TERMS:
        return ""
    return " OR ".join('"%s"' % (_key(t) + "*" if len(t) > 6 else t).replace('"', "")
                       for t in terms)


def _cross_candidates(con, query: str, exclude_session_id: str, pool: int = None,
                      user_id: str = None):
    """One scoped bm25 pass over every other session on this agent."""
    pool = CROSS_POOL if pool is None else pool
    sql = (
        "select m.id as id, m.session_id as session_id, coalesce(m.content,'') as content,"
        " coalesce(s.title,'') as title, coalesce(s.display_name,'') as name,"
        " coalesce(m.timestamp, s.last_activity_at, s.started_at, 0) as ts,"
        " bm25(messages_fts) as bm from messages_fts f"
        " join messages m on m.id = f.rowid"
        " left join sessions s on s.id = m.session_id"
        " where f.messages_fts match ? and m.role in ('user','assistant')"
        " and m.session_id <> ?"
    )
    args = [query, exclude_session_id]
    if CROSS_SOURCES and CROSS_SOURCES != ("all",):
        sql += " and coalesce(s.source,'') in (%s)" % ",".join("?" * len(CROSS_SOURCES))
        args.extend(CROSS_SOURCES)
    if CROSS_SAME_USER and user_id:
        # same principal, or a row whose principal was never recorded (local CLI/API
        # use on this host), or a trusted peer source chatting agent-to-agent.
        sql += (" and (coalesce(s.user_id,'') = ? or coalesce(s.user_id,'') = ''"
                + (" or coalesce(s.source,'') in (%s)" % ",".join("?" * len(CROSS_TRUSTED))
                   if CROSS_TRUSTED else "")
                + ")")
        args.append(user_id)
        args.extend(CROSS_TRUSTED)
    sql += " order by bm25(messages_fts) limit ?"
    args.append(pool)
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def score_candidates(cands, user_message: str, min_terms: int = None, share: float = None):
    """Re-score a bm25 pool with the pool's own idf and keep only strong agreement.

    Two gates, both cheap and both deliberately blunt:
      * ``min_terms`` distinct query terms must appear in the turn — one common word
        ("drive", "update") is not evidence of anything;
      * the turn's matched idf must reach ``share`` of the best turn in the pool — the
        pool is already bm25-ordered, so this only removes tail noise, and it avoids
        dividing by the whole query's idf mass (which no single turn ever covers).
    """
    min_terms = CROSS_MIN_TERMS if min_terms is None else min_terms
    share = CROSS_SHARE if share is None else share
    qterms = list(dict.fromkeys(_key(t) for t in tokens(user_message)))
    if not cands or not qterms:
        return []
    toks = []
    for c in cands:
        body = c.get("content") or ""
        toks.append(set() if _derived(body) else set(_key(t) for t in tokens(body)))
    df: dict = {}
    for ts in toks:
        for t in set(qterms) & ts:
            df[t] = df.get(t, 0) + 1
    pool_n = max(1, len(cands))
    idf = {t: math.log(1.0 + pool_n / d) for t, d in df.items()}
    qmass = sum(idf.values()) or 1.0
    out = []
    for c, ts in zip(cands, toks):
        hit = [t for t in qterms if t in ts]
        if len(hit) < min_terms:
            continue
        sc = sum(idf[t] for t in hit)
        out.append(dict(c, terms=hit, score=sc, coverage=sc / qmass))
    if not out:
        return []
    out.sort(key=lambda c: -c["score"])   # stable: bm25 order survives ties
    floor = share * out[0]["score"]
    return [c for c in out if c["score"] >= floor]


def _session_range(con, owner: str):
    try:
        r = con.execute("select min(id), max(id) from messages where session_id=?",
                        (owner,)).fetchone()
        if r and r[0] is not None:
            return int(r[0]), int(r[1])
    except Exception:
        pass
    return 0, 0


def cross_hits(session_id: str, user_message: str, db_path: str = None, top_k: int = None):
    """Spans in *other* sessions that overlap this query, one pointer per session."""
    if not CROSS or not session_id:
        return []
    query = fts_query(user_message)
    if not query:
        return []
    db_path = db_path or getattr(hermes_source(), "path", None)
    if not fts_capable(db_path):
        return []
    top_k = CROSS_TOP_K if top_k is None else top_k
    con = _connect_ro(db_path)
    try:
        try:
            row = con.execute("select coalesce(user_id,'') as u from sessions where id = ?",
                              (session_id,)).fetchone()
            uid = (row["u"] if row else "") or ""
        except Exception:
            uid = ""
        ranked = score_candidates(
            _cross_candidates(con, query, session_id, user_id=uid), user_message)
        if not ranked:
            return []
        best: dict = {}
        for c in ranked:
            best.setdefault(c["session_id"], c)          # ranked, so first wins
        picks = list(best.values())[:max(1, top_k)]
        for p in picks:
            lo, hi = _session_range(con, p["session_id"])
            p["lo"] = max(lo, int(p["id"]) - 2)
            p["hi"] = min(hi, int(p["id"]) + 3)
        return picks
    finally:
        con.close()


def _when(ts) -> str:
    try:
        return time.strftime("%d %b", time.localtime(float(ts)))
    except Exception:
        return ""


def _cross_line(hit, reader, qwords: list, kmap: dict) -> str:
    title = _clean((hit.get("title") or hit.get("name") or "").strip(), 60)
    label = hit["session_id"] + (' "%s"' % title if title else "")
    when = _when(hit.get("ts"))
    if when:
        label += ", " + when
    shown = [kmap.get(k, k) for k in hit["terms"]]
    body = (
        "[verbatim-recall] LOW CONFIDENCE — ANOTHER SESSION may hold this: %s, "
        "ids %d-%d, matched %d of %d query words (%s)"
        % (label, hit["lo"], hit["hi"], len(hit["terms"]), max(1, len(qwords)),
           ", ".join(sorted(shown)[:5]))
    )
    opening = _clean(hit.get("content") or "")
    if opening:
        body += ' — opens: "%s"' % opening
    return body + " -> " + reader(hit["lo"], hit["session_id"])


# ---------------------------------------------------------------- render / hooks

def recall(
    session_id: str = "",
    user_message: str = "",
    source=None,
    window: int = None,
    announce: bool = None,
    reader_hint: str = None,
    cross: bool = None,
    cross_db: str = None,
    **kwargs,
):
    """Return ``{"context": "..."}`` with pointers, or None when there is nothing to say.

    ``reader_hint`` overrides the sentence telling the agent how to read a span back
    (default: ``session_search(session_id=..., around_message_id=..., window=...)``);
    it is formatted with ``session_id``/``owner_session_id`` (the session that owns the
    span — the hook's own session, or a foreign one for a cross-session pointer),
    ``message_id`` and ``window``.

    Cross-session pointers need a store to query: they run when the source carries a
    ``.path`` (the built-in sources do) or when ``cross_db`` names one. A caller passing
    its own callable therefore stays single-session unless it opts in with ``cross_db``.
    """
    if QUIET or not session_id or not (user_message or "").strip():
        return None
    window = WINDOW if window is None else window
    announce = ANNOUNCE if announce is None else announce
    cross = CROSS if cross is None else cross
    try:
        idx = index_for(session_id, source)
        if not idx and not cross:
            return None

        def reader(mid: int, owner: str = None) -> str:
            owner = owner or session_id
            if reader_hint:
                return reader_hint.format(session_id=owner, owner_session_id=owner,
                                          message_id=mid, window=window)
            return (f"session_search(session_id='{owner}', "
                    f"around_message_id={mid}, window={window})")

        lines = []
        if idx and announce and _announced.get(session_id, 0) < idx["hi"]:
            _announced[session_id] = idx["hi"]
            lines.append(
                f"[verbatim-recall] {idx['n']} turns of this session are summarized away but "
                f"kept verbatim in the store (ids {idx['lo']}-{idx['hi']}) — any span is "
                f"readable with {reader(idx['lo'])}."
            )

        qwords = len(set(tokens(user_message)))
        for mid, _sc, terms, tier, _cov in (match(idx, user_message) if idx else []):
            role, ask = idx["preview"].get(mid, ("", ""))
            start = max(idx["lo"], mid - 2)
            span = f"ids {start}-{min(idx['hi'], mid + 3)}"
            if tier == "loose":
                body = (
                    f"[verbatim-recall] LOW CONFIDENCE — only {len(terms)} of {qwords} query "
                    f"words appear here ({', '.join(sorted(terms)[:4])}), so this may be an "
                    f"unrelated turn: {span}"
                )
            else:
                body = (f"[verbatim-recall] {span}, summarized away, "
                        f"matched: {', '.join(sorted(terms)[:5])}")
            if ask:
                who = "user asked" if role == "user" else "in the turn answering"
                body += f' — {who}: "{ask}"'
            body += f" -> {reader(start)}"
            lines.append(body)

        if cross:
            lines.extend(_cross_lines(session_id, user_message, reader, qwords,
                                      cross_db or getattr(source, "path", None)))

        return {"context": "\n".join(lines)} if lines else None
    except Exception:
        return None


def _cross_lines(session_id: str, user_message: str, reader, qwords: int, db_path: str):
    """Foreign pointers, cooled down so one old session cannot nag on every turn."""
    try:
        if not db_path:
            return []
        hits = cross_hits(session_id, user_message, db_path=db_path)
        if not hits:
            return []
        words = list(dict.fromkeys(tokens(user_message)))
        kmap = {}
        for w in words:
            kmap.setdefault(_key(w), w)
        turn = _cross_turn.get(session_id, 0) + 1
        _cross_turn[session_id] = turn
        if len(_cross_turn) > 512:
            _cross_turn.clear()
            _cross_turn[session_id] = turn
        if len(_cross_seen) > 4096:
            _cross_seen.clear()
        out = []
        for hit in hits:
            key = (session_id, hit["session_id"])
            last = _cross_seen.get(key)
            if CROSS_COOLDOWN and last is not None and turn - last < CROSS_COOLDOWN:
                continue
            _cross_seen[key] = turn
            out.append(_cross_line(hit, reader, words, kmap))
        return out
    except Exception:
        return []


def on_post_compaction(session_id: str = "", old_session_id: str = "", **kwargs):
    """Drop the stale index so the next turn sees the enlarged region."""
    try:
        with _lock:
            for sid in {session_id, old_session_id}:
                if sid:
                    _cache.pop(sid, None)
    except Exception:
        pass
    return None


def register(ctx) -> None:
    """Hermes plugin entry point: ``ctx.register_hook(name, fn)``."""
    ctx.register_hook("pre_llm_call", recall)
    ctx.register_hook("post_compaction", on_post_compaction)


# ---------------------------------------------------------------- cli

def _main(argv) -> int:
    cmd = argv[1] if len(argv) > 1 else "sessions"
    src = hermes_source()
    if cmd == "sessions":
        path = getattr(src, "path", None)
        if not path or not os.path.exists(path):
            print(f"no store at {path} — set VERBATIM_DB")
            return 1
        con = sqlite3.connect(path, timeout=3)
        try:
            rows = con.execute(
                "select session_id, count(*) n, min(id) lo, max(id) hi from messages "
                "where compacted=1 group by session_id order by hi desc limit 10"
            ).fetchall()
        finally:
            con.close()
        for r in rows:
            print(f"{r[0]}  n={r[1]}  ids {r[2]}-{r[3]}")
        return 0
    if len(argv) < 3:
        print("usage: python -m verbatim_recall [sessions|index|query|explain|cross] <session_id> [text]")
        return 2
    sid, rest = argv[2], " ".join(argv[3:])
    if cmd == "cross":
        q = fts_query(rest)
        if not q:
            print("query too short for cross-session (needs at least %d terms)" % CROSS_MIN_TERMS)
            return 0
        t0 = time.time()
        hits = cross_hits(sid, rest, db_path=getattr(src, "path", None))
        dt = (time.time() - t0) * 1000.0
        print(f"match: {q}")
        print(f"{len(hits)} foreign span(s) in {dt:.1f} ms "
              f"(pool {CROSS_POOL}, top_k {CROSS_TOP_K}, "
              f"min_terms {CROSS_MIN_TERMS}, share {CROSS_SHARE:.2f})")
        for h in hits:
            print(f"  id={h['id']} span={h['lo']}-{h['hi']} sid={h['session_id']} "
                  f"score={h['score']:.2f} cov={h['coverage']:.2f} "
                  f"terms={','.join(sorted(h['terms']))} when={_when(h.get('ts'))} "
                  f"title={_clean(h.get('title') or '', 50)!r}")
        return 0
    if cmd == "index":
        idx = index_for(sid, src)
        if not idx:
            print("no compacted region")
            return 1
        print(f"{idx['raw']} compacted msgs, {idx['n']} indexed, ids {idx['lo']}-{idx['hi']}\n"
              f"{len(idx['inv'])} kept terms ({len(idx['rare'])} rare<={idx['rare_df']}), "
              f"boilerplate cut at df>{idx['boiler_df']}")
        return 0
    if cmd in ("query", "q"):
        out = recall(session_id=sid, user_message=rest, source=src)
        print(out["context"] if out else "(no match — zero tokens injected)")
        return 0
    if cmd in ("explain", "x"):
        idx = index_for(sid, src)
        if not idx:
            print("no compacted region")
            return 1
        print(f"n={idx['n']} rare_df<={idx['rare_df']} boiler_df>{idx['boiler_df']}")
        for t in sorted(set(tokens(rest))):
            df = len(idx["inv"].get(t, ()))
            tag = ("absent" if not df else
                   "rare" if t in idx["rare"] else "common")
            print(f"  {t:16s} df={df:5d} {tag:11s} idf={idx['idf'].get(t, 0.0):.2f}")
        hits = match(idx, rest)
        print(f"picked {len(hits)}")
        for mid, sc, terms, tier, cov in hits:
            role, ask = idx["preview"].get(mid, ("", ""))
            print(f"  id={mid} score={sc:.2f} cov={cov:.2f} tier={tier} "
                  f"terms={','.join(sorted(terms))} role={role} ask={ask[:60]!r}")
        return 0
    print("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
