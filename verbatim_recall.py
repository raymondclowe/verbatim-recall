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

__version__ = "0.2.0"

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


# ---------------------------------------------------------------- render / hooks

def recall(
    session_id: str = "",
    user_message: str = "",
    source=None,
    window: int = None,
    announce: bool = None,
    reader_hint: str = None,
    **kwargs,
):
    """Return ``{"context": "..."}`` with pointers, or None when there is nothing to say.

    ``reader_hint`` overrides the sentence telling the agent how to read a span back
    (default: ``session_search(session_id=..., around_message_id=..., window=...)``).
    """
    if QUIET or not session_id or not (user_message or "").strip():
        return None
    window = WINDOW if window is None else window
    announce = ANNOUNCE if announce is None else announce
    try:
        idx = index_for(session_id, source)
        if not idx:
            return None

        def reader(mid: int) -> str:
            if reader_hint:
                return reader_hint.format(session_id=session_id, message_id=mid, window=window)
            return (f"session_search(session_id='{session_id}', "
                    f"around_message_id={mid}, window={window})")

        lines = []
        if announce and _announced.get(session_id, 0) < idx["hi"]:
            _announced[session_id] = idx["hi"]
            lines.append(
                f"[verbatim-recall] {idx['n']} turns of this session are summarized away but "
                f"kept verbatim in the store (ids {idx['lo']}-{idx['hi']}) — any span is "
                f"readable with {reader(idx['lo'])}."
            )

        qwords = len(set(tokens(user_message)))
        for mid, _sc, terms, tier, _cov in match(idx, user_message):
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

        return {"context": "\n".join(lines)} if lines else None
    except Exception:
        return None


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
        print("usage: python -m verbatim_recall [sessions|index|query|explain] <session_id> [text]")
        return 2
    sid, rest = argv[2], " ".join(argv[3:])
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
