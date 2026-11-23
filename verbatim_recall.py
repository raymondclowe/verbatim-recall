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

__version__ = "0.4.2"

# ---------------------------------------------------------------- config
#
# Every knob is a YAML item under ``plugins.entries.verbatim-recall.settings`` in
# the Hermes ``config.yaml``; ``plugin.yaml`` declares the schema, so
# ``hermes plugins`` renders the fields and writes them back to the same place.
# There are no environment variables — one source of truth, visible and editable.
# On a host without a plugin context, call ``configure({...})`` before use.

DEFAULTS: dict = {
    "db_path": None,                    # Hermes state DB; default <hermes home>/state.db
    "quiet": False,                     # index only, never inject
    "announce": True,                   # the ids-range line, once per region growth
    "top_k": 2,                         # strict pointers per turn
    "min_score": 1.6,                   # absolute floor for a strict hit
    "coverage": 0.45,                   # share of the query's IDF mass a strict hit carries
    "min_terms": 2,                     # agreeing terms needed for a strict hit
    "loose_score": 0.9,                 # floor for the LOW CONFIDENCE tier
    "loose_coverage": 0.20,
    "loose_top_k": 1,
    "common_weight": 0.3,               # weight of non-rare terms in the loose tier
    "span": 24,                         # min id distance between two pointers
    "preview": 90,                      # chars of the opening ask quoted in a pointer
    "window": 20,                       # suggested scroll radius handed to the reader
    "boiler_df": 0,                     # 0 = derive; else a term in more than N rows is boilerplate
    "rare_df": 0,                       # 0 = derive; else a term in at most N rows is rare
    "cross_session.enabled": True,      # also point at other sessions on the same agent
    "cross_session.top_k": 2,           # foreign pointers per turn
    "cross_session.min_terms": 3,       # one more agreeing word than the local tier
    "cross_session.share": 0.55,        # matched IDF mass, as a share of the pool's best turn
    "cross_session.pool": 200,          # candidates re-scored in python (measured: 400 is slower AND less accurate)
    "cross_session.max_terms": 12,      # query terms sent to MATCH
    "cross_session.df_probe": 12,       # terms whose store-wide df is probed (default: all)
    "cross_session.df_cap": 5000,       # a term in more rows than this cannot anchor a MATCH
    "cross_session.cooldown": 6,        # turns before the same foreign session repeats
    "cross_session.sources": ["telegram", "api_server", "cli", "desktop", "mobile", "a2a"],
    # Identity scoping. On by default: a shared state.db must never quote another person's
    # chat, so only rows whose principal matches the caller, whose principal is listed in
    # ``trusted_principals`` (the operator's own API surface), whose principal was never
    # recorded, or whose source is in ``trusted_sources`` can be quoted. A single-operator
    # store sees everything that is his; set it false to drop even that check.
    "cross_session.same_user_only": True,
    # Principals that are the operator's own non-chat surfaces: ``api`` is what the local
    # api_server records, so turns typed into the API are quotable from Telegram.
    "cross_session.trusted_principals": ["api"],
    # An asking session whose principal was never recorded (NULL/empty user_id) used to be
    # treated as "the operator", which quoted alice and bob to an anonymous asker. Now an
    # unrecorded asker matches only rows that are also unrecorded, unless its source is
    # listed in ``unattributed_sources`` -- the surfaces that run on this host under this
    # operator and genuinely record no principal.
    "cross_session.unattributed_sources": ["cli", "api_server"],
    # a2a rows are the same operator's other machines, each with its own user_id (ip:…)
    "cross_session.trusted_sources": ["a2a"],

    # ---- measurement ------------------------------------------------------------
    # Whether the plugin works is not a matter of opinion: every turn writes one
    # line saying what it injected, and every time the agent reads a named span
    # back another line says so. ``report`` turns the log into the answer.
    "metrics.enabled": True,
    "metrics.path": "",                 # "" = <hermes home>/logs/verbatim-recall
    "metrics.retention_days": 30,       # day files older than this are deleted
    "metrics.store_terms": True,        # keep matched query words; never message bodies
    "metrics.shadow_knobs": True,       # record what the gates threw away (false-negative watch)
    "metrics.tag": "",                  # written as "src" on every event; "test" for suites
}

CONFIG: dict = dict(DEFAULTS)   # active settings, dotted keys as declared above


def _as_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
    return bool(default)


def _as_int(value, default) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _as_float(value, default) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_list(value, default) -> list:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        items = [str(part).strip() for part in value if str(part).strip()]
    else:
        items = []
    return items or list(default)


# ---- the module globals the rest of this file reads; all set by _apply()
QUIET = False
ANNOUNCE = True
TOP_K = 2
MIN_SCORE = 1.6
COVERAGE = 0.45
MIN_TERMS = 2
LOOSE_SCORE = 0.9
LOOSE_COVERAGE = 0.20
LOOSE_TOP_K = 1
COMMON_W = 0.3
SPAN = 24
PREVIEW = 90
WINDOW = 20
CROSS = True
CROSS_TOP_K = 2
CROSS_MIN_TERMS = 3
CROSS_SHARE = 0.55
CROSS_POOL = 200
CROSS_MAX_TERMS = 12
DF_PROBE_TERMS = 6
DF_CAP = 5000
CROSS_COOLDOWN = 6
CROSS_SOURCES = ("telegram", "api_server", "cli", "desktop", "mobile", "a2a")
CROSS_SAME_USER = True
CROSS_TRUSTED = ("a2a",)
CROSS_TRUSTED_PRINCIPALS = ("api",)
CROSS_UNATTRIBUTED = ("cli", "api_server")   # may ask with no recorded principal
METRICS = True
METRICS_DIR = ""
METRICS_RETENTION = 30
METRICS_TERMS = True
METRICS_SHADOW = True


def _apply() -> None:
    """Project ``CONFIG`` onto the module globals the index and matcher read."""
    global QUIET, ANNOUNCE, TOP_K, MIN_SCORE, COVERAGE, MIN_TERMS
    global LOOSE_SCORE, LOOSE_COVERAGE, LOOSE_TOP_K, COMMON_W, SPAN, PREVIEW, WINDOW
    global CROSS, CROSS_TOP_K, CROSS_MIN_TERMS, CROSS_SHARE, CROSS_POOL
    global CROSS_MAX_TERMS, CROSS_COOLDOWN, CROSS_SOURCES, CROSS_SAME_USER, CROSS_TRUSTED
    global DF_PROBE_TERMS, DF_CAP
    global CROSS_TRUSTED_PRINCIPALS, CROSS_UNATTRIBUTED
    global METRICS, METRICS_DIR, METRICS_RETENTION, METRICS_TERMS, METRICS_SHADOW
    global METRICS_TAG
    cfg = CONFIG
    QUIET = _as_bool(cfg.get("quiet"), DEFAULTS["quiet"])
    ANNOUNCE = _as_bool(cfg.get("announce"), DEFAULTS["announce"])
    TOP_K = _as_int(cfg.get("top_k"), DEFAULTS["top_k"])
    MIN_SCORE = _as_float(cfg.get("min_score"), DEFAULTS["min_score"])
    COVERAGE = _as_float(cfg.get("coverage"), DEFAULTS["coverage"])
    MIN_TERMS = _as_int(cfg.get("min_terms"), DEFAULTS["min_terms"])
    LOOSE_SCORE = _as_float(cfg.get("loose_score"), DEFAULTS["loose_score"])
    LOOSE_COVERAGE = _as_float(cfg.get("loose_coverage"), DEFAULTS["loose_coverage"])
    LOOSE_TOP_K = _as_int(cfg.get("loose_top_k"), DEFAULTS["loose_top_k"])
    COMMON_W = _as_float(cfg.get("common_weight"), DEFAULTS["common_weight"])
    SPAN = _as_int(cfg.get("span"), DEFAULTS["span"])
    PREVIEW = _as_int(cfg.get("preview"), DEFAULTS["preview"])
    WINDOW = _as_int(cfg.get("window"), DEFAULTS["window"])
    CROSS = _as_bool(cfg.get("cross_session.enabled"), DEFAULTS["cross_session.enabled"])
    CROSS_TOP_K = _as_int(cfg.get("cross_session.top_k"), DEFAULTS["cross_session.top_k"])
    CROSS_MIN_TERMS = _as_int(cfg.get("cross_session.min_terms"), DEFAULTS["cross_session.min_terms"])
    CROSS_SHARE = _as_float(cfg.get("cross_session.share"), DEFAULTS["cross_session.share"])
    CROSS_POOL = _as_int(cfg.get("cross_session.pool"), DEFAULTS["cross_session.pool"])
    CROSS_MAX_TERMS = _as_int(cfg.get("cross_session.max_terms"), DEFAULTS["cross_session.max_terms"])
    DF_PROBE_TERMS = _as_int(cfg.get("cross_session.df_probe"),
                             DEFAULTS["cross_session.df_probe"])
    DF_CAP = _as_int(cfg.get("cross_session.df_cap"), DEFAULTS["cross_session.df_cap"])
    CROSS_COOLDOWN = _as_int(cfg.get("cross_session.cooldown"), DEFAULTS["cross_session.cooldown"])
    CROSS_SOURCES = tuple(_as_list(cfg.get("cross_session.sources"), DEFAULTS["cross_session.sources"]))
    CROSS_SAME_USER = _as_bool(cfg.get("cross_session.same_user_only"),
                               DEFAULTS["cross_session.same_user_only"])
    CROSS_TRUSTED = tuple(_as_list(cfg.get("cross_session.trusted_sources"),
                                   DEFAULTS["cross_session.trusted_sources"]))
    CROSS_TRUSTED_PRINCIPALS = tuple(
        _as_list(cfg.get("cross_session.trusted_principals"),
                 DEFAULTS["cross_session.trusted_principals"]))
    CROSS_UNATTRIBUTED = tuple(
        _as_list(cfg.get("cross_session.unattributed_sources"),
                 DEFAULTS["cross_session.unattributed_sources"]))
    METRICS = _as_bool(cfg.get("metrics.enabled"), DEFAULTS["metrics.enabled"])
    METRICS_DIR = str(cfg.get("metrics.path") or "").strip()
    METRICS_RETENTION = _as_int(cfg.get("metrics.retention_days"),
                                DEFAULTS["metrics.retention_days"])
    METRICS_TERMS = _as_bool(cfg.get("metrics.store_terms"), DEFAULTS["metrics.store_terms"])
    METRICS_SHADOW = _as_bool(cfg.get("metrics.shadow_knobs"),
                              DEFAULTS["metrics.shadow_knobs"])
    METRICS_TAG = str(cfg.get("metrics.tag") or "").strip()


def configure(overrides=None) -> dict:
    """Adopt ``overrides`` (dotted settings keys) on top of the defaults.

    Unknown keys are reported on stderr and ignored, so a typo in config.yaml is
    visible instead of silently doing nothing. Returns the effective settings.
    """
    merged = dict(DEFAULTS)
    for key, value in dict(overrides or {}).items():
        name = str(key).strip()
        if name in DEFAULTS:
            merged[name] = value
        elif name:
            print(f"verbatim-recall: ignoring unknown setting {name!r}", file=sys.stderr)
    CONFIG.clear()
    CONFIG.update(merged)
    _apply()
    return dict(CONFIG)


def settings_from_context(ctx) -> dict:
    """Read every declared setting from a Hermes plugin context (never raises)."""
    getter = getattr(ctx, "get_config", None)
    if not callable(getter):
        return {}
    found = {}
    for key, default in DEFAULTS.items():
        try:
            found[key] = getter(key, default)
        except Exception:
            found[key] = default
    return found


_apply()


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
    path = db_path or CONFIG.get("db_path") or os.path.join(_hermes_home(), "state.db")

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


def _identifier_like(term: str) -> bool:
    """Does this term look like a handle rather than an English word?

    Digits, an internal separator, or a token long enough to be a hash/id
    fragment. Shape alone is not enough: the caller also requires the term to
    be carried by at most two turns, because being rare in this region is not
    the same claim as identifying a turn -- "capitulation" is 12 letters and
    occurs in 47 turns of a live 1,889-turn region, and it is not a handle.
    """
    return (any(ch.isdigit() for ch in term)
            or any(ch in "_.-/" for ch in term)
            or len(term) >= 16)


# ---------------------------------------------------------------- index

def build_index(rows):
    """Index compacted rows: token -> ids, plus a preview per id and the id range."""
    inv: dict = {}
    preview: dict = {}
    n = 0
    last_ask = ""
    first = last = 0
    raw = 0
    tools = 0
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
        role = _field(r, "role")
        if role == "tool":
            # A tool row is the agent's own output: grep dumps and file reads that echo
            # the words being searched for. On a live region they are 929 of 1,889 rows,
            # so a query matched them more readily than the answer -- the winner for one
            # real query was a grep dump. They cannot be *pointed at* as an answer, so
            # they are not indexed. Counted, so the drop is visible in `index`.
            tools += 1
            continue
        n += 1
        if role == "user" and body.strip():
            last_ask = _clean(body)
        preview[mid] = (role, last_ask)
        for t in set(tokens(body)):
            inv.setdefault(t, set()).add(mid)

    if not n:
        return None

    # boilerplate: a term nearly every substantive message carries is not evidence
    # (measured on a real region: boilerplate sits above ~10% df, topical words at
    #  1-5% — so precision must come from query coverage, not a tight rarity cut)
    boiler_df = _as_int(CONFIG.get("boiler_df"), 0) or max(12, int(n * 0.10))
    rare_df = _as_int(CONFIG.get("rare_df"), 0) or max(8, int(n * 0.05))
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
        "tools": tools,
        "boiler_df": boiler_df,
        "rare_df": rare_df,
    }


def _signature(source, session_id: str):
    """(row count, highest id) -- the cheap shape check, read from the STORE.

    It used to iterate ``source(session_id)`` and pull every row's full content just
    to count rows: 19.1 ms per turn on a 2,041-row region, spent to decide whether to
    rebuild an index that was usually unchanged. The source built from a database has
    a ``.path``, and two index-covered aggregates answer the same question in ~0.1 ms.
    A caller's own iterable (no path) still falls back to counting it.
    """
    path = getattr(source, "path", None)
    if path:
        try:
            con = _connect_ro(path)
            try:
                row = con.execute(
                    "select count(*) as n, coalesce(max(id), 0) as hi from messages "
                    "where session_id = ? and compacted = 1", (session_id,)).fetchone()
            finally:
                con.close()
            if row is not None:
                return (int(row["n"]), int(row["hi"]))
        except Exception:
            pass
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


def match(idx, user_message: str, top_k: int = None, loose_top_k: int = None,
          diag: dict = None):
    """Rank region messages against the query. Returns (id, score, terms, tier, coverage).

    ``diag``, when passed a dict, is filled with what the gates did to the candidates
    (how many scored, how many trailed the coverage floor, how many were dropped for a
    single term or for crowding another hit). That is the only honest way to see the
    false negatives: material was there and a threshold refused it.
    """
    top_k = TOP_K if top_k is None else top_k
    loose_top_k = LOOSE_TOP_K if loose_top_k is None else loose_top_k
    if diag is not None:
        # merge, do not clear: `recall` fills this same dict for the report, and a
        # clear here would wipe what the cross pass and the shape check already wrote.
        diag.update({"strict_cands": 0, "strict_below": 0, "strict_gated_terms": 0,
                     "strict_gated_span": 0, "loose_cands": 0, "loose_below": 0,
                     "loose_gated": 0})
    words = set(tokens(user_message))
    if not words or idx is None:
        return []
    live = [t for t in words if t in idx["idf"]]
    if not live:
        return []
    # The denominator is the query's *matchable* mass, deliberately: a stray word the
    # region never uses (a typo, a name, an aside) must not veto a good hit. What was
    # wrong was the single-term escape hatch below, not the coverage arithmetic --
    # "zzqqxx nonsense control" fired STRICT at cov=1.00 on "control" alone, because
    # one low-df word was treated as proof of identity. Absent words are now handled
    # where the damage was: a lone term has to look like an identifier to stand alone.
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
    gated_terms = gated_span = 0

    def _rank(mid, sc):
        # (-score, user before assistant, newest first).
        # Without a named tie-break the winner was decided by set-iteration order: one
        # query over one session returned three different pointers (836990, 839161,
        # 846927, all scoring 7.40) across five runs. A pointer you cannot reproduce is
        # a pointer you cannot test or audit.
        # A question and its answer tie on every word they share, so the ask wins: it
        # is unique to the moment, while the answer gets restated elsewhere.
        role = idx["preview"].get(mid, ("", ""))[0]
        return (-sc, 0 if role == "user" else 1, -mid)

    for mid, sc in sorted(scores.items(), key=lambda kv: _rank(kv[0], kv[1])):
        if sc < need:
            break
        terms = why[mid]
        # Two agreeing terms, or one term that both LOOKS like a handle and is
        # carried by at most two turns. "Rare here" is not the same as "an
        # identifier": on the live store "control" appears in 2 turns, so a query
        # whose other 3 words were absent fired STRICT on it. Length alone is not
        # a handle either: "capitulation" is 12 letters and appears in 47 turns.
        if len(terms) < MIN_TERMS and not (
                len(terms) == 1 and _identifier_like(terms[0])
                and len(idx["inv"][terms[0]]) <= 2):
            gated_terms += 1
            continue
        if any(abs(mid - p[0]) < SPAN for p in picked):
            gated_span += 1
            continue
        picked.append((mid, sc, terms, "strict", sc / qmass))
        if len(picked) >= min(top_k, 4):
            break
    if diag is not None:
        diag["strict_cands"] = len(scores)
        diag["strict_below"] = sum(1 for v in scores.values() if v < need)
        diag["strict_gated_terms"] = gated_terms
        diag["strict_gated_span"] = gated_span
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
    lgated = 0

    def _lrank(mid, sc):
        role = idx["preview"].get(mid, ("", ""))[0]
        return (-sc, 0 if role == "user" else 1, -mid)

    for mid, sc in sorted(lsc.items(), key=lambda kv: _lrank(kv[0], kv[1])):
        if sc < need_loose:
            break
        terms = lwhy[mid]
        rare_hit = [t for t in terms if t in idx["rare"]]
        # a single word is enough only when it is near-identifier rare
        if len(terms) < 2 and not (rare_hit and len(idx["inv"][rare_hit[0]]) <= 5):
            lgated += 1
            continue
        if any(abs(mid - p[0]) < SPAN for p in out):
            lgated += 1
            continue
        out.append((mid, sc, terms, "loose", sc / qmass))
        if len(out) >= max(1, min(loose_top_k, 2)):
            break
    if diag is not None:
        diag["loose_cands"] = len(lsc)
        diag["loose_below"] = sum(1 for v in lsc.values() if v < need_loose)
        diag["loose_gated"] = lgated
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
    return " OR ".join(_fts_term(t) for t in terms)


def _fts_term(t: str) -> str:
    """One query term as an FTS5 phrase, prefix-wildcarded on the cheapstem.

    The star has to sit OUTSIDE the quotes: FTS5 reads "proboo*" as the literal
    token 'proboo*' and matches nothing, while "proboo"* is a real prefix query.
    The old form put it inside, so every query word longer than 6 characters was a
    dead anchor -- measured on a 60-row index: '"proboo*"' -> 0 rows, '"proboo"*' -> 1.
    Cross-session recall was only ever finding its pool through the short words.
    """
    if len(t) > 6:
        return '"%s"*' % _key(t)
    return '"%s"' % t


def _store_df(con, terms) -> dict:
    """Document frequency of each term, bounded by the cap it is compared against.

    ``count(*)`` over a ubiquitous word's full posting list is what cost 14.6 ms
    per term on a live store ("output" = 283,728 rows), and the words that cost
    the most are exactly the ones this probe exists to identify. The caller only
    ever asks "is df <= df_cap", so stop counting at df_cap + 1 rows.
    Measured: 0.67 ms for "output" instead of 14.6 ms. A prefix-heavy term
    ("configuration" -> "config"*) still costs ~6.5 ms, because expanding the
    prefix is itself the work.
    """
    out = {}
    limit = max(1, DF_CAP) + 1
    for t in terms:
        try:
            out[t] = int(con.execute(
                "select count(*) from (select rowid from messages_fts "
                "where messages_fts match ? limit ?)",
                (_fts_term(t), limit)).fetchone()[0])
        except Exception:
            out[t] = 0
    return out


def _cross_pool(con, user_message: str, exclude_session_id: str, pool: int = None,
                user_id: str = None, score=None):
    """Candidate rows, the MATCH that produced them, and which rung that was.

    Cost and accuracy were conflated here. The expensive part of the old OR was never
    "too many terms" -- it was that ONE ubiquitous word set the size of the match set:
    "ubuntu" alone is 48,376 rows on an 846k-message store, and the join plus bm25 over
    that measured 95-135 ms per call, 342 ms per turn end to end.

    What this must NOT do is demand that the rarest terms co-occur. An earlier attempt
    ANDed the three rarest terms and loosened only on an EMPTY result; it was fast, and
    it silently dropped pointers whenever the winning row was tied together by a common
    word instead of a rare one. Measured on a 25.6k-message store: 7 of 15 queries lost
    every foreign pointer (the row matching {monthly, merge, savings} can never appear
    in an AND of {monthly, spreadsheet, merge}, and a rung that returns rows the scorer
    then rejects leaves the turn with no pointer at all).

    Fitness is the scorer's job, so the MATCH only has to be affordable and broad. The
    ladder, each rung accepted only when it yields a row the scorer can actually use:

      1. OR over the probed terms the store does not use everywhere
         (df <= cross_session.df_cap), bm25-ordered while the union stays small;
      2. OR over EVERY query term (probed or not, up to ``max_terms``), newest-first
         and unsorted -- reaches rows that only a common word ties together, and
         keeps a rare anchor past the probe window retrievable;
      3. nothing: the pool is handed back anyway so `diag` can see what was considered.
    """
    score = score_candidates if score is None else score
    terms = list(dict.fromkeys(tokens(user_message)))[:CROSS_MAX_TERMS]
    if len(terms) < CROSS_MIN_TERMS:
        return [], '', '', []
    # Probe at most DF_PROBE_TERMS for cost, but the fallback MATCH carries the whole
    # vocabulary. A prefix-only MATCH silently dropped a rare anchor at position 7 of
    # a long query -- and the generic rows the fallback did fetch could still score on
    # common words, so the turn got a WRONG pointer instead of none.
    probe = max(1, min(len(terms), DF_PROBE_TERMS))
    df = _store_df(con, terms[:probe])
    informative = [t for t in terms[:probe] if df.get(t) and df[t] <= DF_CAP]
    rungs = []
    if informative:
        # one informative term is enough: a rare anchor is cheap to fetch, and the
        # scorer still demands CROSS_MIN_TERMS agreeing words before a pointer.
        rungs.append((
            'capped-or',
            ' OR '.join(_fts_term(t) for t in informative),
            sum(df[t] for t in informative) > DF_CAP * 4,
        ))
    rungs.append(('all-or', ' OR '.join(_fts_term(t) for t in terms), True))
    seen = set()
    rows: list = []
    query = ''
    for name, q, recent in rungs:
        if q in seen:
            continue
        seen.add(q)
        rows = _cross_candidates(con, q, exclude_session_id, pool, user_id, recent=recent)
        query = q
        scored = score(rows, user_message) if rows else []
        if scored:
            return rows, q, name, scored
    return rows, query, 'no-score', []


def _asker_source(con, session_id: str) -> str:
    """The ``source`` of the session asking the question ("" when it is not recorded)."""
    try:
        row = con.execute("select coalesce(source,'') as s from sessions where id = ?",
                          (session_id,)).fetchone()
        return (row["s"] if row else "") or ""
    except Exception:
        return ""


def _cross_candidates(con, query: str, exclude_session_id: str, pool: int = None,
                      user_id: str = None, recent: bool = False):
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
    if CROSS_SAME_USER:
        # A recorded principal sees its own rows, rows with no principal (local CLI/API on
        # this host), and the principals/sources the operator declared his own.
        #
        # An UNRECORDED asker (user_id NULL — the CLI, the api_server, a replayed turn)
        # used to take the same branch, which matched every row with no principal AND
        # every trusted principal: alice's and bob's chats were quoted to an anonymous
        # asker. It now sees only rows that are also unrecorded, unless its own source is
        # one of ``unattributed_sources`` (surfaces that run as this operator and record
        # nothing) -- a Telegram session with no user_id is the case that was leaking.
        ors = []
        if user_id:
            ors.append("coalesce(s.user_id,'') = ?")
            args.append(user_id)
            ors.append("coalesce(s.user_id,'') = ''")
            if CROSS_TRUSTED_PRINCIPALS:
                ors.append("coalesce(s.user_id,'') in (%s)"
                           % ",".join("?" * len(CROSS_TRUSTED_PRINCIPALS)))
                args.extend(CROSS_TRUSTED_PRINCIPALS)
            if CROSS_TRUSTED:
                ors.append("coalesce(s.source,'') in (%s)"
                           % ",".join("?" * len(CROSS_TRUSTED)))
                args.extend(CROSS_TRUSTED)
        else:
            ors.append("coalesce(s.user_id,'') = ''")
            asker_source = _asker_source(con, exclude_session_id)
            if asker_source and asker_source in CROSS_UNATTRIBUTED:
                if CROSS_TRUSTED:
                    ors.append("coalesce(s.source,'') in (%s)"
                               % ",".join("?" * len(CROSS_TRUSTED)))
                    args.extend(CROSS_TRUSTED)
        sql += " and (" + " or ".join(ors) + ")"
    # bm25 ordering must sort every row the MATCH set contains: fine for a tightened
    # AND (hundreds), ruinous for the OR fallback (tens of thousands, measured 800+ ms
    # per turn). The scorer ranks on its own pool statistics anyway, so the fallback
    # takes the newest `pool` rows instead -- no sort, and recency is the better prior.
    sql += " order by " + ("m.id desc" if recent else "bm25(messages_fts)") + " limit ?"
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
    # (-score, -id): bm25 order is not a defined order among equal scores, so name
    # the tie-break instead of inheriting whatever SQLite happened to return
    out.sort(key=lambda c: (-c["score"], -int(c.get("id") or 0)))
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


def cross_hits(session_id: str, user_message: str, db_path: str = None, top_k: int = None,
               diag: dict = None):
    """Spans in *other* sessions that overlap this query, one pointer per session.

    ``diag`` (optional dict) receives what the pass saw -- the MATCH it used, which
    rung produced it, and how many candidates the gates refused. It is passed, not held
    in a module global: the gateway runs turns on threads, and a global raced.
    """
    if not CROSS or not session_id:
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
        cands, query, rung, ranked = _cross_pool(
            con, user_message, session_id, user_id=uid)
        if diag is not None:
            diag.update({"match": query, "rung": rung, "pool": len(cands or []),
                         "scored": len(ranked or [])})
        if not ranked:
            return []
        best: dict = {}
        for c in ranked:
            best.setdefault(c["session_id"], c)          # ranked, so first wins
        picks = list(best.values())[:max(1, top_k)]
        for p in picks:
            lo, hi = _session_range(con, p["session_id"])
            # the handle MUST be the matched turn. It used to be id-2, and on this
            # store id-2 is often a [CONTEXT COMPACTION] block -- the reader opened
            # the paraphrase the tool exists to replace. window=20 already supplies
            # the surrounding context, so lo has no reason to guess.
            p["lo"] = max(lo, int(p["id"]))
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


# ---------------------------------------------------------------- metrics
#
# Measurement, not telemetry: one JSON line per event in a local directory — no
# network, no message bodies, no credentials. Four event kinds:
#
#   fire    pointers went into the context this turn (how many, which tier, how far)
#   miss    nothing to say this turn, and why (no_index, no_match, quiet, error)
#   use     the agent read a named span back — the only observable proof of value
#   search  the agent searched this session with no pointer in hand
#
# The three questions a report has to answer, and how far it honestly can:
#   is it firing?      exact — fire vs miss on every turn of every session.
#   false positive?    a *proxy*: a fire whose span was never read back. That is not
#                      the same as irrelevant (the agent may not have needed it), so
#                      the report calls it "unread", never "wrong".
#   false negative?    *candidates*: turns where a gate refused material that scored
#                      (see ``diag`` in match()), or where the agent searched this
#                      session without a pointer. Both say "look here", not "missed".

_MET_LOCK = threading.Lock()
_met_pruned = False
_turn_no: dict = {}
_pending: dict = {}     # session_id -> [{fire, turn, spans:[[owner, lo, hi]], used}]


def metrics_dir() -> str:
    """``metrics.path``, else <hermes home>/logs/verbatim-recall."""
    return (METRICS_DIR or "").strip() or os.path.join(_hermes_home(), "logs",
                                                       "verbatim-recall")


def _metrics_file(day: str = None) -> str:
    day = day or time.strftime("%Y-%m-%d")
    return os.path.join(metrics_dir(), "events-%s.jsonl" % day)


def _prune_metrics() -> None:
    global _met_pruned
    if _met_pruned or METRICS_RETENTION <= 0:
        return
    _met_pruned = True
    try:
        cutoff = time.time() - METRICS_RETENTION * 86400
        d = metrics_dir()
        for name in os.listdir(d):
            if not (name.startswith("events-") and name.endswith(".jsonl")):
                continue
            p = os.path.join(d, name)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
    except Exception:
        pass


def metric(ev: str, **fields) -> None:
    """Append one event line. Fail-open: measurement must never break a turn."""
    if not METRICS:
        return
    try:
        rec = {"ev": ev, "ts": round(time.time(), 3),
               "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        # a harness that forgets to isolate its log dir is still identifiable, so a
        # suite can never be read as live traffic in vr_stats/vr_value
        if METRICS_TAG:
            rec["src"] = METRICS_TAG
        rec.update({k: v for k, v in fields.items() if v is not None})
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with _MET_LOCK:
            _prune_metrics()
            path = _metrics_file()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def _day_window(days: int = 30, since: str = None, until: str = None) -> tuple:
    """Resolve ``--since``/``--until`` (YYYY-MM-DD, inclusive) or a day count into a
    ``(lo_ts, hi_ts)`` pair. ``until`` covers the whole named day."""
    def _ts(s, end=False):
        try:
            t = time.mktime(time.strptime(str(s)[:10], "%Y-%m-%d"))
        except Exception:
            return None
        return t + 86399.999 if end else t
    hi = _ts(until, end=True) if until else None
    lo = _ts(since) if since else None
    if lo is None:
        lo = (hi if hi is not None else time.time()) - max(1, days) * 86400
    if hi is None:
        hi = float("inf")
    return float(lo), float(hi)


def load_metrics(days: int = 30, since: str = None, until: str = None) -> list:
    """Every event from the day files inside the window, oldest first."""
    out = []
    d = metrics_dir()
    if not os.path.isdir(d):
        return out
    cutoff, ceiling = _day_window(days, since, until)
    try:
        names = sorted(os.listdir(d))
    except Exception:
        return out
    for name in names:
        if not (name.startswith("events-") and name.endswith(".jsonl")):
            continue
        p = os.path.join(d, name)
        try:
            if os.path.getmtime(p) < cutoff:
                continue
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    ts = float(rec.get("ts") or 0)
                    if cutoff <= ts <= ceiling:
                        out.append(rec)
        except Exception:
            continue
    out.sort(key=lambda r: r.get("ts") or 0)
    return out


def _next_turn(session_id: str) -> int:
    turn = _turn_no.get(session_id, 0) + 1
    _turn_no[session_id] = turn
    if len(_turn_no) > 512:
        _turn_no.clear()
        _turn_no[session_id] = turn
    return turn


def _add_pending(session_id: str, fire: str, turn: int, spans: list) -> None:
    """Remember what this turn offered, so a later read can be attributed to it."""
    lst = _pending.setdefault(session_id, [])
    lst.append({"fire": fire, "turn": turn, "spans": spans, "used": False})
    del lst[:-12]
    if len(_pending) > 512:
        _pending.clear()
        _pending[session_id] = lst


def _arg(payload, name: str):
    """Field from a tool call's arguments, whether dict or raw JSON text."""
    try:
        if isinstance(payload, dict):
            return payload.get(name)
        if isinstance(payload, str):
            m = re.search(r'["\']?%s["\']?\s*[:=]\s*["\']?(-?\d+|[^,\s}\'\"]+)' % name, payload)
            return m.group(1) if m else None
    except Exception:
        pass
    return None


def on_tool_call(tool_name: str = "", arguments=None, args=None, session_id: str = "",
                 **kwargs) -> None:
    """Attribute a ``session_search`` to the pointer that named that span, if any.

    This is the whole usefulness measurement: a pointer is only demonstrably useful
    when the id it printed is the id that got read. A search that matches nothing we
    offered is recorded too — either the pointers went unread, or the span the agent
    wanted was not offered (a false-negative candidate).
    """
    if not METRICS:
        return None
    try:
        if (tool_name or "").strip() != "session_search":
            return None
        # The hook carries session_id, and conversation_id stays stable across a
        # compression rotation — look under all of them, or a fire recorded before
        # the rotation would look unread for ever.
        keys = [k for k in (session_id,
                            kwargs.get("conversation_id"),
                            kwargs.get("task_id")) if k]
        sess = keys[0] if keys else ""
        payload = arguments if arguments is not None else args
        asked = _arg(payload, "around_message_id")
        owner = _arg(payload, "session_id")
        try:
            asked = int(asked)
        except (TypeError, ValueError):
            asked = None
        if isinstance(owner, str):
            owner = owner.strip().strip("'\"") or None
        rec = span = None
        for k in keys:
            for cand in _pending.get(k) or []:
                if cand.get("used"):
                    continue
                for sp in cand["spans"]:
                    if owner and sp[0] and owner != sp[0]:
                        continue
                    if asked is not None:
                        if sp[1] <= asked <= sp[2]:
                            rec, span = cand, sp
                            break
                    elif owner:
                        rec, span = cand, sp
                        break
                if rec:
                    break
            if rec:
                break
        if rec and span:
            rec["used"] = True
            metric("use", session=sess, fire=rec["fire"], owner=span[0],
                   asked=asked, turn=rec["turn"])
        else:
            pending = [c for lst in (_pending.get(k) or [] for k in keys)
                       for c in lst if not c.get("used")]
            metric("search", session=sess, owner=owner, asked=asked,
                   after_fire=bool(pending), pending=len(pending))
    except Exception:
        pass
    return None


def _pct(part, whole) -> str:
    return "%d%%" % round(100.0 * part / whole) if whole else "—"


def report(days: int = 30, since: str = None, until: str = None,
           session: str = None) -> dict:
    """Aggregate the event log into the answer to "is this thing working?"."""
    evs = load_metrics(days, since, until)
    if session:
        evs = [e for e in evs if not e.get("session") or e.get("session") == session]
    sources = {}
    for e in evs:
        k = str(e.get("src") or "live")
        sources[k] = sources.get(k, 0) + 1
    turns = [e for e in evs if e.get("ev") in ("fire", "miss", "announce")]
    fires = [e for e in evs if e.get("ev") == "fire"]
    misses = [e for e in evs if e.get("ev") == "miss"]
    uses = [e for e in evs if e.get("ev") == "use"]
    searches = [e for e in evs if e.get("ev") == "search"]

    why: dict = {}
    for e in misses:
        why[str(e.get("why") or "?")] = why.get(str(e.get("why") or "?"), 0) + 1

    ptrs = sum(int(e.get("ptr") or 0) for e in fires)
    strict = sum(int(e.get("strict") or 0) for e in fires)
    loose = sum(int(e.get("loose") or 0) for e in fires)
    same = sum(int(e.get("same") or 0) for e in fires)
    other = sum(int(e.get("cross") or 0) for e in fires)
    tokens = sum(int(e.get("tokens") or 0) for e in fires)
    ms = [float(e.get("ms") or 0) for e in fires]

    by_fire = {}
    for e in fires:
        by_fire[str(e.get("fire"))] = e
    read_fires = set()
    read_tier = {"strict": 0, "loose": 0, "announce": 0, "other": 0}
    for e in uses:
        fid = str(e.get("fire"))
        f = by_fire.get(fid)
        if not f:
            continue
        read_fires.add(fid)
        asked = e.get("asked")
        tier = None
        if asked is not None:
            for p in f.get("ptrs") or []:
                if p.get("o") != e.get("owner"):
                    continue
                if int(p.get("lo") or 0) <= int(asked) <= int(p.get("hi") or 0):
                    tier = ("other" if p.get("o") != f.get("session")
                            else (p.get("tier") or "strict"))
                    break
        if tier is None:
            tier = "strict"
        if tier in read_tier:
            read_tier[tier] += 1

    gated_turns = 0
    gated_total = 0
    for e in turns:
        d = e.get("diag") or {}
        g = int(d.get("strict_gated_terms") or 0) + int(d.get("strict_gated_span") or 0) \
            + int(d.get("loose_gated") or 0)
        if g:
            gated_turns += 1
            gated_total += g

    terms: dict = {}
    for e in fires:
        for p in e.get("ptrs") or []:
            for t in p.get("terms") or []:
                terms[str(t)] = terms.get(str(t), 0) + 1

    foreign: dict = {}
    for e in fires:
        for p in e.get("ptrs") or []:
            if p.get("o") and p.get("o") != e.get("session"):
                foreign[str(p["o"])] = foreign.get(str(p["o"]), 0) + 1

    per_day: dict = {}
    for e in turns:
        day = str(e.get("at") or "")[:10]
        row = per_day.setdefault(day, {"turns": 0, "fires": 0, "ptrs": 0, "reads": 0})
        row["turns"] += 1
        if e.get("ev") == "fire":
            row["fires"] += 1
            row["ptrs"] += int(e.get("ptr") or 0)
    for day, row in per_day.items():
        row["reads"] = len({str(e.get("fire")) for e in uses
                            if str(e.get("at") or "")[:10] == day})

    rep = {
        "sources": sources,
        "window_days": days,
        "window": {"since": since, "until": until, "session": session},
        "log_dir": metrics_dir(),
        "log_files": len([n for n in (os.listdir(metrics_dir())
                                      if os.path.isdir(metrics_dir()) else [])
                          if n.startswith("events-")]),
        "events": len(evs),
        "first": (evs[0].get("at") if evs else None),
        "last": (evs[-1].get("at") if evs else None),
        "turns": len(turns),
        "fires": len(fires),
        "fire_rate": (len(fires) / float(len(turns))) if turns else 0.0,
        "announce_only": len([e for e in turns if e.get("ev") == "announce"]),
        "miss_reasons": why,
        "pointers": ptrs,
        "pointer_tiers": {"strict": strict, "low_confidence": loose},
        "pointer_origins": {"same_session": same, "other_session": other},
        "other_sessions": sorted(foreign.items(), key=lambda kv: -kv[1])[:10],
        "tokens": {
            "total": tokens,
            "per_fire": round(tokens / float(len(fires)), 1) if fires else 0,
            "per_turn": round(tokens / float(len(turns)), 1) if turns else 0,
            "max": max([int(e.get("tokens") or 0) for e in fires] or [0]),
        },
        "latency_ms": {
            "mean": round(sum(ms) / len(ms), 1) if ms else 0,
            "max": round(max(ms), 1) if ms else 0,
        },
        "reads": {
            "read_back_events": len(uses),
            "fires_with_a_read": len(read_fires),
            "read_rate": (len(read_fires) / float(len(fires))) if fires else 0.0,
            "by_tier": read_tier,
            "unread_fires": max(0, len(fires) - len(read_fires)),
        },
        "false_negative_candidates": {
            "searches_without_pointer": len([e for e in searches
                                             if not e.get("after_fire")]),
            "searches_while_pointers_pending": len([e for e in searches
                                                    if e.get("after_fire")]),
            "gated_turns": gated_turns,
            "gated_candidates": gated_total,
        },
        "top_terms": sorted(terms.items(), key=lambda kv: -kv[1])[:12],
        "per_day": [dict(day=k, **v) for k, v in sorted(per_day.items())],
    }
    return rep


def format_report(rep: dict) -> str:
    """The same numbers, as the block of text a person reads."""
    L = []
    L.append("verbatim-recall report — last %d days" % rep["window_days"])
    L.append("  log   %s (%d file(s), %d events)"
             % (rep["log_dir"], rep["log_files"], rep["events"]))
    if not rep["events"]:
        L.append("")
        L.append("  no events yet. The plugin writes one line per turn; if this stays")
        L.append("  empty, check metrics.enabled and that the dir is writable.")
        return "\n".join(L)
    L.append("  span  %s → %s" % (rep["first"], rep["last"]))
    L.append("")
    L.append("firing")
    L.append("  turns observed        %d" % rep["turns"])
    L.append("  turns with pointers   %d  (%s)"
             % (rep["fires"], _pct(rep["fires"], rep["turns"])))
    L.append("  pointers injected     %d  (high confidence %d, low confidence %d)"
             % (rep["pointers"], rep["pointer_tiers"]["strict"],
                rep["pointer_tiers"]["low_confidence"]))
    L.append("  origin                %d same-session, %d other-session"
             % (rep["pointer_origins"]["same_session"],
                rep["pointer_origins"]["other_session"]))
    if rep["other_sessions"]:
        L.append("  other sessions        " + ", ".join(
            "%s×%d" % (k, v) for k, v in rep["other_sessions"]))
    L.append("  tokens injected       %d total, %s per fire, %s per turn, max %d"
             % (rep["tokens"]["total"], rep["tokens"]["per_fire"],
                rep["tokens"]["per_turn"], rep["tokens"]["max"]))
    L.append("  latency               %s ms mean, %s ms max"
             % (rep["latency_ms"]["mean"], rep["latency_ms"]["max"]))
    L.append("")
    L.append("usefulness (proxy: did the agent read the id we printed?)")
    L.append("  fires read back       %d of %d  (read rate %s)"
             % (rep["reads"]["fires_with_a_read"], rep["fires"],
                _pct(rep["reads"]["fires_with_a_read"], rep["fires"])))
    _bt = rep["reads"]["by_tier"]
    L.append("  read by tier          high confidence %d, low confidence %d, "
             "other-session %d, announcement %d"
             % (_bt.get("strict", 0), _bt.get("loose", 0),
                _bt.get("other", 0), _bt.get("announce", 0)))
    L.append("  unread fires          %d  (a false-positive *proxy*, not proof)"
             % rep["reads"]["unread_fires"])
    fn = rep["false_negative_candidates"]
    L.append("")
    L.append("false-negative watch")
    L.append("  searched, no pointer  %d  (agent looked anyway — candidate miss)"
             % fn["searches_without_pointer"])
    L.append("  searched, pending     %d  (pointers were offered and not read)"
             % fn["searches_while_pointers_pending"])
    L.append("  gated by thresholds   %d candidates across %d turns  (material scored, a gate refused it)"
             % (fn["gated_candidates"], fn["gated_turns"]))
    if rep["miss_reasons"]:
        L.append("  turns, no pointer     " + ", ".join(
            "%s %d" % (k, v) for k, v in sorted(rep["miss_reasons"].items(),
                                                key=lambda kv: -kv[1])))
    if rep["top_terms"]:
        L.append("")
        L.append("top matched terms       " + ", ".join(
            "%s(%d)" % (k, v) for k, v in rep["top_terms"]))
    if rep["per_day"]:
        L.append("")
        L.append("per day")
        for row in rep["per_day"][-14:]:
            L.append("  %s  turns %3d  fires %3d  pointers %3d  read %3d"
                     % (row["day"], row["turns"], row["fires"], row["ptrs"], row["reads"]))
    return "\n".join(L)


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
    if not session_id or not (user_message or "").strip():
        metric("miss", session=session_id or "",
               why="no_session" if not session_id else "empty_message")
        return None
    window = WINDOW if window is None else window
    announce = ANNOUNCE if announce is None else announce
    cross = CROSS if cross is None else cross
    t0 = time.perf_counter()
    turn = _next_turn(session_id) if METRICS else 0
    diag: dict = {}
    try:
        idx = index_for(session_id, source)
        if QUIET:
            # documented meaning of ``quiet``: build the index, inject nothing.
            metric("miss", session=session_id, why="quiet", turn=turn,
                   turns=idx["n"] if idx else 0,
                   ms=round((time.perf_counter() - t0) * 1000, 1))
            return None
        if not idx and not cross:
            metric("miss", session=session_id, why="no_index", turn=turn,
                   diag=diag if METRICS_SHADOW else None,
                   ms=round((time.perf_counter() - t0) * 1000, 1))
            return None

        def reader(mid: int, owner: str = None) -> str:
            owner = owner or session_id
            if reader_hint:
                return reader_hint.format(session_id=owner, owner_session_id=owner,
                                          message_id=mid, window=window)
            return (f"session_search(session_id='{owner}', "
                    f"around_message_id={mid}, window={window})")

        lines = []
        ptrs = []
        # per-turn diagnosis, filled by the stages below and written on every outcome.
        # An empty list is the whole reason this exists: the report used to say
        # "no pointer" every turn with no way to tell a near miss from a non-starter.
        diag.update({"terms": len(set(tokens(user_message))),
                     "turns": idx["n"] if idx else 0,
                     "informative": 0, "pool": 0, "gated": 0, "rungs": []})
        if idx and announce and _announced.get(session_id, 0) < idx["hi"]:
            _announced[session_id] = idx["hi"]
            lines.append(
                f"[verbatim-recall] {idx['n']} turns of this session are summarized away but "
                f"kept verbatim in the store (ids {idx['lo']}-{idx['hi']}) — any span is "
                f"readable with {reader(idx['lo'])}."
            )
            ptrs.append({"o": session_id, "lo": idx["lo"], "hi": idx["hi"],
                         "tier": "announce", "terms": []})

        qwords = len(set(tokens(user_message)))
        # `diag` now exists on every path (it is created above), so the gates fill it in
        # whether or not shadow logging is on -- the report reads it either way.
        hits = match(idx, user_message, diag=diag) if idx else []
        for mid, _sc, terms, tier, _cov in hits:
            role, ask = idx["preview"].get(mid, ("", ""))
            # the handle is the matched turn itself; window=20 supplies the context
            start = max(idx["lo"], mid)
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
            ptrs.append({"o": session_id, "lo": start,
                         "hi": min(idx["hi"], mid + 3), "tier": tier,
                         # metrics.store_terms=false keeps the words out of the LOG only.
                         # They stay in `terms` because the context line above prints them
                         # anyway; blanking them is done once, when the event is written.
                         "terms": sorted(terms)[:5]})

        if cross:
            db_for_cross = cross_db or getattr(source, "path", None)
            if not db_for_cross and source is None:
                # The plugin hook passes no source. The default source knows the store
                # path, and the cross pass needs it; a caller-supplied callable stays
                # single-session unless it opts in with cross_db (documented).
                db_for_cross = getattr(hermes_source(), "path", None)
            clines, cptrs = _cross_lines(session_id, user_message, reader, qwords,
                                         db_for_cross, diag=diag)
            lines.extend(clines)
            ptrs.extend(cptrs)

        if not lines:
            metric("miss", session=session_id, why="no_match", turn=turn,
                   turns=idx["n"] if idx else 0,
                   diag=diag if METRICS_SHADOW else None,
                   ms=round((time.perf_counter() - t0) * 1000, 1))
            return None
        text = "\n".join(lines)
        fire = "%s#%d" % (session_id, turn)
        if METRICS:
            metric("fire", session=session_id, turn=turn, fire=fire,
                   ptr=len(ptrs),
                   strict=len([p for p in ptrs if p["tier"] == "strict"]),
                   loose=len([p for p in ptrs if p["tier"] == "loose"]),
                   same=len([p for p in ptrs if p["o"] == session_id]),
                   cross=len([p for p in ptrs if p["o"] != session_id]),
                   tokens=max(1, len(text) // 4), chars=len(text),
                   turns=idx["n"] if idx else 0,
                   diag=diag if METRICS_SHADOW else None,
                   terms=[t for p in ptrs for t in p["terms"]][:8]
                   if METRICS_TERMS else None,
                   # store_terms=false must hold for the pointers too: they are the same
                   # words, and they were being written regardless of the setting.
                   ptrs=([{**p, "terms": []} for p in ptrs] if not METRICS_TERMS
                         else ptrs),
                   ms=round((time.perf_counter() - t0) * 1000, 1))
            _add_pending(session_id, fire, turn,
                         [[p["o"], p["lo"], p["hi"]] for p in ptrs])
        return {"context": text}
    except Exception:
        metric("miss", session=session_id, why="error", turn=turn,
               ms=round((time.perf_counter() - t0) * 1000, 1))
        return None


def _cross_lines(session_id: str, user_message: str, reader, qwords: int,
                 db_path: str | None, diag: dict = None):
    """Foreign pointers, cooled down so one old session cannot nag on every turn."""
    try:
        if not db_path:
            if diag is not None:
                diag["cross"] = "no-store"
            return [], []
        hits = cross_hits(session_id, user_message, db_path=db_path, diag=diag)
        if not hits:
            return [], []
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
        out, ptrs = [], []
        for hit in hits:
            key = (session_id, hit["session_id"])
            last = _cross_seen.get(key)
            if CROSS_COOLDOWN and last is not None and turn - last < CROSS_COOLDOWN:
                continue
            _cross_seen[key] = turn
            out.append(_cross_line(hit, reader, words, kmap))
            ptrs.append({"o": hit["session_id"], "lo": hit["lo"], "hi": hit["hi"],
                         "tier": "loose",
                         "terms": list(hit["terms"])[:5]})
        return out, ptrs
    except Exception:
        return [], []


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
    """Hermes plugin entry point: adopt the YAML settings, then hook."""
    configure(settings_from_context(ctx))
    ctx.register_hook("pre_llm_call", recall)
    ctx.register_hook("post_compaction", on_post_compaction)
    # post_tool_call is what makes the usefulness metric possible: it is the only
    # place where a pointer is provably followed, because the agent's read of a named
    # span arrives here as a session_search carrying around_message_id.
    ctx.register_hook("post_tool_call", on_tool_call)


# ---------------------------------------------------------------- cli

def _main(argv) -> int:
    cmd = argv[1] if len(argv) > 1 else "sessions"
    src = hermes_source()
    if cmd in ("report", "metrics", "r"):
        days = 30
        for a in argv[2:]:
            try:
                days = int(a)
            except ValueError:
                pass
        rep = report(days)
        if "--json" in argv:
            print(json.dumps(rep, indent=2, ensure_ascii=False, default=str))
        else:
            print(format_report(rep))
        return 0
    if cmd == "sessions":
        path = getattr(src, "path", None)
        if not path or not os.path.exists(path):
            print(f"no store at {path} — set plugins.entries.verbatim-recall.settings.db_path")
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
        d: dict = {}
        hits = cross_hits(sid, rest, db_path=getattr(src, "path", None), diag=d)
        dt = (time.time() - t0) * 1000.0
        print(f"match: {d.get('match') or q}  [{d.get('rung') or '?'}]"
              f"  pool {d.get('pool', 0)} rows, {d.get('scored', 0)} scored")
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
        print(f"{idx['raw']} compacted msgs, {idx['n']} indexed "
              f"({idx.get('tools', 0)} tool rows skipped), ids {idx['lo']}-{idx['hi']}\n"
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
